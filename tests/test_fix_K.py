# tests/test_fix_K.py — regression tests for the 2026-09-03 stock / supply
# timeline fixes (audit findings stock-1/2/3/5/6/7/9/11/12/13/14).
#
# Every expected value is derived BY HAND in the test's docstring on a
# 2-block / 1-component / 1-PO world, then varied. World T ("tiny"):
#   item C, SKU S needs 1 C per case, kg_per_case 1.0 (so cases == kg);
#   B1 = b1 [10, 20] 100 cases, B2 = b2 [30, 40] 100 cases (10 C / h each);
#   opening C = 150, stock count S = 0 h, default rules (L = 96 h).
#   Hard curve H: 150 until h10, 50 at h20, 50 until h30, -50 at h40, so
#   H first crosses 0 at h35 inside B2 (B1 is OK: min over [10,20] is 50).

from __future__ import annotations

import math
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from stockcheck import timeline as tl  # noqa: E402
from stockcheck import explode  # noqa: E402
from stockcheck.api import remaining_cases  # noqa: E402

ANCHOR = "2026-09-01 00:00:00"
C, S = "C", "S"


def blk(bid, start, end, cases, **over) -> dict:
    b = {"block_id": bid, "sku": S, "line_name": "L1", "start_h": start,
         "end_h": end, "cases": cases, "locked": False, "running": False,
         "cases_left": None}
    b.update(over)
    return b


def rcpt(ready_h, qty, po8="PO1") -> dict:
    return {"ready_h": ready_h, "qty": qty, "po8": po8, "tier": "erp",
            "receipt_date": "2026-09-02", "label": f"PO {po8}"}


def needs(item=C, alts=(), sku=S, per_case=1.0) -> dict:
    return {sku: {"kg_per_case": 1.0, "items": [
        {"item": item, "per_case": per_case, "unit": "EA", "alts": list(alts)}]}}


def world(blocks, *, opening=None, receipts=None, snapshot=0.0, sku_needs=None,
          tracked=(C,), in_house=(), feed="ok", window=800.0, rules=None):
    sku_needs = sku_needs or needs()
    opening = {C: 150.0} if opening is None else opening
    items = sorted({m for g in sku_needs.values() for e in g["items"]
                    for m in [e["item"], *e.get("alts", [])]})
    t = tl.build_timelines(blocks, sku_needs, opening, receipts or {},
                           {i: snapshot for i in items},
                           tracked=list(tracked), in_house=list(in_house))
    board = tl.evaluate_board(blocks, t, rules or {}, feed, window)
    return t, board


B1 = blk("b1", 10.0, 20.0, 100.0)
B2 = blk("b2", 30.0, 40.0, 100.0)
K1, K2 = "b1|10", "b2|30"


# --------------------------------------------------------------------------
# stock-14 — block_key is the Gantt's `id|start`, full precision
# --------------------------------------------------------------------------

def test_block_key_is_id_pipe_start_like_the_gantt():
    """blockIdentity.blockKey is `${id}|${start_hour}` with JS String():
    integral hours print without a fraction, others in shortest round-trip
    form. 0.125 and 0.1249 used to collapse to `b@0.12`."""
    bk = tl.block_key
    assert bk({"block_id": "b", "start_h": 10.0}) == "b|10"
    assert bk({"block_id": "b", "start_h": 71.85}) == "b|71.85"
    assert bk({"block_id": "b", "start_h": 0.017}) == "b|0.017"
    assert bk({"block_id": "b", "start_h": 125.733}) == "b|125.733"
    assert bk({"block_id": "b", "start_h": 0.0}) == "b|0"
    assert bk({"block_id": "b", "start_h": 0.125}) != bk({"block_id": "b", "start_h": 0.1249})
    assert bk({"block_id": "b", "start_h": 1e-7}) == "b|1e-7"          # JS form
    assert bk({"block_id": "b", "start_h": 0.1 + 0.2}) == "b|0.30000000000000004"
    assert bk({"key": "given|1", "block_id": "b", "start_h": 3.0}) == "given|1"


def test_two_rows_of_one_mo_less_than_5_thousandths_apart_both_draw():
    """Split MO: two rows share block_id `mo`, starts 10.000 and 10.004
    (both `mo@10.00` before). Each draws 100 C over 10 h; with 150 on hand
    the second row must see the first: H(20) = 150 - 100 - 100 x (20-10.004)
    / 10 = -49.96 < 0 -> SHORT; before the fix the second row's draw was
    dropped and both read OK."""
    rows = [blk("mo", 10.0, 20.0, 100.0), blk("mo", 10.004, 20.004, 100.0)]
    t, board = world(rows)
    assert sorted(board) == ["mo|10", "mo|10.004"]
    assert len(t["items"][C]["draws"]) == 2
    assert board["mo|10"]["verdict"] == "SHORT"
    assert board["mo|10.004"]["verdict"] == "SHORT"
    # the SAME row registered twice is still one draw (add_block contract)
    t2, board2 = world([B2, dict(B2)])
    assert len(t2["items"][C]["draws"]) == 1 and list(board2) == [K2]


# --------------------------------------------------------------------------
# stock-5 — one physical lot backs at most one run
# --------------------------------------------------------------------------

def test_alternate_pool_draw_is_visible_to_the_alternates_own_group():
    """Finder's r12 example. Group for SKU A = {X primary, Y alt}; SKU B's
    group = {Y}. On hand X 0, Y 1000. Block A [0,24] needs 1000, block Cc
    [48,72] needs 1000, no receipts, fresh feed.
      old: A's 1000 booked on X -> Cc sees Y 1000 - 1000 = 0 at h72 -> OK,
           A sees pooled 1000 - 1000 = 0 -> OK: both OK on one lot.
      new: X has capacity 0, so A's 1000 is booked on Y; Cc's own window
           starts at Y = 1000 - 1000 = 0 and ends at -1000 -> SHORT; A is
           still OK (pool 1000 - 1000 = 0 >= -EPS)."""
    sn = {"A": needs("X", alts=["Y"], sku="A")["A"], "B": needs("Y", sku="B")["B"]}
    a = blk("a", 0.0, 24.0, 1000.0, sku="A")
    cc = blk("c", 48.0, 72.0, 1000.0, sku="B")
    t, board = world([a, cc], opening={"X": 0.0, "Y": 1000.0}, sku_needs=sn,
                     tracked=("X", "Y"))
    assert t["items"]["X"]["draws"] == []
    assert [(d["key"], d["qty"]) for d in t["items"]["Y"]["draws"]] == [
        ("a|0", 1000.0), ("c|48", 1000.0)]
    assert board["a|0"]["verdict"] == "OK"
    assert board["c|48"]["verdict"] == "SHORT"
    assert board["c|48"]["items"][0]["minH"] == pytest.approx(-1000.0)
    # input order must not matter: the run that starts first nets first
    t2, board2 = world([cc, a], opening={"X": 0.0, "Y": 1000.0}, sku_needs=sn,
                       tracked=("X", "Y"))
    assert _draws(t2) == _draws(t) and {k: v["verdict"] for k, v in board2.items()} == \
        {k: v["verdict"] for k, v in board.items()}


def _draws(t):
    return {i: [(d["key"], d["qty"]) for d in v["draws"]] for i, v in t["items"].items()}


def test_allocate_splits_across_members_and_keeps_the_shortfall_on_the_primary():
    """X 300 / Y 1000, need 1000 -> X gives 300, Y 700 (pieces sum to
    1000). X 0 / Y 400, need 1000 -> Y 400 and the uncoverable 600 stays on
    the PRIMARY X so the shortage shows on the curve the group is named
    after. A receipt on X landed by the draw start counts as capacity: X 0
    + 500 at h5, need 1000, block at h10 -> X 500, Y 500. Single-member
    groups book everything on the primary whatever its capacity."""
    def items(x, y, xr=()):
        return {"X": {"opening": x, "draws": [], "receipts": [rcpt(h, q) for h, q in xr],
                      "snapshot_h": 0.0},
                "Y": {"opening": y, "draws": [], "receipts": [], "snapshot_h": 0.0}}
    g = {"primary": "X", "members": ["X", "Y"]}
    assert tl._allocate(items(300.0, 1000.0), g, 10.0, 1000.0) == [("X", 300.0), ("Y", 700.0)]
    assert tl._allocate(items(0.0, 400.0), g, 10.0, 1000.0) == [("X", 600.0), ("Y", 400.0)]
    assert tl._allocate(items(0.0, 1000.0, xr=[(5.0, 500.0)]), g, 10.0, 1000.0) == \
        [("X", 500.0), ("Y", 500.0)]
    assert tl._allocate(items(0.0, 1000.0, xr=[(15.0, 500.0)]), g, 10.0, 1000.0) == \
        [("Y", 1000.0)]
    assert tl._allocate(items(0.0, 0.0), {"primary": "X", "members": ["X"]}, 10.0, 7.0) == \
        [("X", 7.0)]


# --------------------------------------------------------------------------
# stock-1 — SHORT is reachable
# --------------------------------------------------------------------------

def test_no_inbound_is_short_even_when_the_window_ends_before_the_board():
    """World T, no receipts, feed ok, window_end 5 h (the extract's last
    date is before the board, as on the live snapshot: -72 h). B2 crosses at
    h35 > 5 -> the old rule said NO_DATA; nothing inbound at all is a proven
    shortage -> SHORT. B1 stays OK (min H over [10,20] = 50)."""
    _, board = world([B1, B2], window=5.0)
    assert board[K1]["verdict"] == "OK"
    assert board[K2]["verdict"] == "SHORT"
    assert board[K2]["binding"] is None
    assert board[K2]["depletion_h"] == pytest.approx(35.0)
    assert "no inbound counted" in board[K2]["text"]
    # a genuinely stale / missing feed still cannot alarm: NO_DATA
    for feed in ("stale", "missing", "empty"):
        _, b = world([B1, B2], window=5.0, feed=feed)
        assert b[K2]["verdict"] == "NO_DATA", feed
        assert b[K1]["verdict"] == "OK"


def test_window_excuses_a_crossing_only_with_inbound_and_a_horizon_inside_the_run():
    """World T plus one PO of 20 C ready at h25 (insufficient). Planned curve
    P over B2: 50 + 20 = 70 at h30, -30 at h40 -> crossing at h37.
      window 33: reaches into the run (>= 30) but ends before h37 -> the
        next truck may not be in the extract yet -> NO_DATA;
      window 38: covers the crossing -> SHORT;
      window 25: ends before the run starts -> nothing vouched for inside
        the run -> SHORT (the finder's 'window before the block start')."""
    po = {C: [rcpt(25.0, 20.0)]}
    _, b = world([B1, B2], receipts=po, window=33.0)
    assert b[K2]["verdict"] == "NO_DATA"
    assert b[K2]["items"][0]["depletion_planned_h"] == pytest.approx(37.0)
    _, b = world([B1, B2], receipts=po, window=38.0)
    assert b[K2]["verdict"] == "SHORT"
    assert b[K2]["depletion_h"] == pytest.approx(37.0)      # SHORT quotes d_P
    _, b = world([B1, B2], receipts=po, window=25.0)
    assert b[K2]["verdict"] == "SHORT"
    # enough inbound (100 at h25): P = 150 at h30 -> 50 at h40, DEPENDENT
    # (ready 25 > start - L = -66, so not reliable) whatever the window
    _, b = world([B1, B2], receipts={C: [rcpt(25.0, 100.0)]}, window=5.0)
    assert b[K2]["verdict"] == "DEPENDENT" and b[K2]["lead_h"] == pytest.approx(5.0)


# --------------------------------------------------------------------------
# stock-2 — the board's own qty_kg is the quantity
# --------------------------------------------------------------------------

class _Bom:
    def explode(self, sku, cases):
        return SimpleNamespace(requirements=[], unk_items=[], cycles=[], status="OK",
                               cases=cases)


def _board(qty):
    return pd.DataFrame([{"block_id": "b", "block_type": "production", "line_id": 0,
                          "line_name": "P09", "start_h": 0.0, "end_h": 10.0, "sku": "S",
                          "qty_kg": qty}])


RATES = pd.DataFrame([{"line_id": 0, "sku": "S", "line_name": "P09", "calc_rate_kgph": 100.0}])
AZA = pd.DataFrame([{"sku": "S", "kg_per_case": 2.0}])


def test_schedule_requirements_prefers_the_board_kg():
    """10 h at 100 kg/h = 1,000 kg by rate; the row says 500 kg.
    board qty wins -> 500 kg, 250 cases, qty_source 'board'; a blank / NaN /
    0 / garbage qty falls back to the rate (1,000 kg, 500 cases, 'rate');
    use_board_qty_kg=False forces the rate even when the row has kg."""
    def one(qty, **kw):
        out = explode.schedule_requirements(_board(qty), _Bom(), AZA, RATES, **kw)
        assert len(out) == 1
        return out[0]
    r = one(500.0)
    assert (r["qty_kg"], r["cases"], r["qty_source"]) == (500.0, 250.0, "board")
    for bad in (None, float("nan"), 0.0, "abc"):
        r = one(bad)
        assert (r["qty_kg"], r["cases"], r["qty_source"]) == (1000.0, 500.0, "rate"), bad
    r = one(500.0, use_board_qty_kg=False)
    assert (r["qty_kg"], r["qty_source"]) == (1000.0, "rate")
    assert explode.board_qty_kg(pd.Series({"qty_kg": "1,000"})) is None   # not a number
    assert explode.board_qty_kg(pd.Series({"qty_kg": 12.5})) == 12.5


# --------------------------------------------------------------------------
# stock-3 — a running MO draws only its remainder
# --------------------------------------------------------------------------

def test_remaining_cases_by_row_generation():
    """Legacy row: full Fct cases with pct -> x (1 - pct/100): 1000 @ 45.1 %
    -> 549. Fix-C04 row (fct_kg token) already carries the remainder ->
    unchanged. Queued / no pct / garbage pct -> unchanged. pct clamps to
    [0, 100]."""
    assert remaining_cases(1000.0, "current_state:running;pct=45.1") == pytest.approx(549.0)
    assert remaining_cases(1000.0, "current_state:running;pct=100") == 0.0
    assert remaining_cases(1000.0, "current_state:running;pct=130") == 0.0
    assert remaining_cases(1000.0, "current_state:running;pct=-5") == 1000.0
    assert remaining_cases(549.0, "current_state:running;pct=45.1;fct_kg=1000;made_kg=451") == 549.0
    assert remaining_cases(1000.0, "current_state:queued;pct=45.1") == 1000.0
    assert remaining_cases(1000.0, "current_state:running") == 1000.0
    assert remaining_cases(1000.0, "current_state:running;pct=abc") == 1000.0
    assert remaining_cases(1000.0, None) == 1000.0
    assert remaining_cases(1000.0, float("nan")) == 1000.0


def test_running_block_draws_cases_left_from_the_count_hour_not_the_time_tail():
    """Finder's r11 case 6 / 6b. Running block [0, 100] of 1,000 cases, stock
    counted at S = 50, on hand 300.
      uniform tail: 1,000 x (100-50)/100 = 500 from h50 at 10/h -> H hits 0
        at h80 -> SHORT (no inbound), covered (80-50)/(100-50) = 0.6;
      cases_left 200 (the ERP's remainder): 200 from h50 -> min H = 100 ->
        OK. The remainder rides on cases_left; `cases` stays the row's own."""
    run = blk("r", 0.0, 100.0, 1000.0, locked=True, running=True)
    t, b = world([run], opening={C: 300.0}, snapshot=50.0)
    assert t["items"][C]["draws"] == [{"key": "r|0", "a": 50.0, "e": 100.0, "qty": 500.0}]
    assert b["r|0"]["verdict"] == "SHORT"
    assert b["r|0"]["covered_frac"] == pytest.approx(0.6)
    t, b = world([{**run, "cases_left": 200.0}], opening={C: 300.0}, snapshot=50.0)
    assert t["items"][C]["draws"] == [{"key": "r|0", "a": 50.0, "e": 100.0, "qty": 200.0}]
    assert b["r|0"]["verdict"] == "OK"
    # a block starting AFTER the count ignores cases_left: the count still
    # holds all of its material (start 60 > S 50 -> 1,000 from h60)
    t, _ = world([{**run, "start_h": 60.0, "cases_left": 200.0}], opening={C: 300.0},
                 snapshot=50.0)
    assert t["items"][C]["draws"][0]["qty"] == 1000.0 and t["items"][C]["draws"][0]["a"] == 60.0
    # cases_left 0 is a KNOWN zero: the MO is done, nothing is drawn
    t, _ = world([{**run, "cases_left": 0.0}], opening={C: 300.0}, snapshot=50.0)
    assert t["items"][C]["draws"] == []


# --------------------------------------------------------------------------
# stock-11 — covered_frac is measured on the remaining draw
# --------------------------------------------------------------------------

def test_covered_frac_denominator_excludes_the_already_made_part():
    """Same running block: tail 500 from h50, on hand 300 -> dip at h80.
    Quantity actually covered = 300 of the 500 still to draw = 0.6; the old
    (dH - start) / (end - start) = 80 / 100 = 0.8 counted the 50 h made
    before the count. A queued block is unchanged: B2 alone with 50 on
    hand -> dip at h35 -> (35-30)/(40-30) = 0.5 = 50 of 100."""
    run = blk("r", 0.0, 100.0, 1000.0, running=True)
    _, b = world([run], opening={C: 300.0}, snapshot=50.0)
    e = b["r|0"]["items"][0]
    assert e["depletion_h"] == pytest.approx(80.0)
    assert e["covered_frac"] == pytest.approx(0.6)
    assert "on hand covers 60%" in b["r|0"]["text"]
    _, b = world([B2], opening={C: 50.0})
    assert b[K2]["covered_frac"] == pytest.approx(0.5)


# --------------------------------------------------------------------------
# stock-13 — the lead sentence must explain the verdict
# --------------------------------------------------------------------------

def test_lead_text_never_rounds_up_to_the_buffer_it_missed():
    """B2 moved to [130, 140]; a 100-C PO ready at h35 -> lead 95 h < L 96
    -> DEPENDENT. _lead(95) = 4.0 d, which reads as meeting 'min 4 d': the
    text now prints whole hours: '95 h before start (min 4 d)'. 96 h ->
    OK backed and '4.0 d'; 100 h -> '4.2 d' (100/24 = 4.17)."""
    b2 = blk("b2", 130.0, 140.0, 100.0)
    _, b = world([B1, b2], receipts={C: [rcpt(35.0, 100.0)]})
    s = b["b2|130"]
    assert s["verdict"] == "DEPENDENT" and s["lead_h"] == pytest.approx(95.0)
    assert "— 95 h before start (min 4 d)" in s["text"], s["text"]
    assert "4.0 d" not in s["text"]
    _, b = world([B1, b2], receipts={C: [rcpt(34.0, 100.0)]})
    s = b["b2|130"]
    assert s["verdict"] == "OK" and s["backed"] and "— 4.0 d before start" in s["text"]
    _, b = world([B1, b2], receipts={C: [rcpt(30.0, 100.0)]})
    assert "— 4.2 d before start" in b["b2|130"]["text"]
    # helper edge: 23.6 h against a 24 h buffer would print "24 h" -> "23 h"
    assert tl._lead_vs_buffer(23.6, 24.0) == "23 h"
    assert tl._lead_vs_buffer(23.6, 96.0) == "24 h"
    assert tl._lead_vs_buffer(95.0, None) == "4.0 d"


# --------------------------------------------------------------------------
# gate_receipts — stock-6 / stock-7 / stock-9
# --------------------------------------------------------------------------

BOM = {"754751"}
UNITS = {"754751": "EA"}
TODAY = date(2026, 9, 1)


def _line(**over) -> dict:
    line = {"po": "2 02 1ACDE 30043543", "po8": "30043543", "item": "754751",
            "designation": "SLV", "qty": 1000.0, "unit": "EA",
            "receipt_date": "2026-09-08", "initial_receipt_date": "2026-09-08",
            "slip_days": 0, "arrival_area": "RP1", "supplier": "GPI",
            "supplier_id": "48", "received": False, "order_date": "2026-07-24",
            "row": 1}
    line.update(over)
    return line


def _gate(lines, **kw):
    args = {"bom_items": BOM, "unit_by_item": UNITS, "snapshot_date": "2026-09-01",
            "today": TODAY, "anchor": ANCHOR, "rules": {}}
    args.update(kw)
    return tl.gate_receipts(lines, **args)


def test_file_age_rule_fires_alongside_the_content_rule():
    """A single line dated 2026-10-01 kept any extract 'ok' by content.
    mtime 2026-08-20 00:00 vs today 2026-09-01 00:00 = 12 d = 288 h > 168
    -> stale, reason ['file_age'], basis 'mtime', age 288. An as-of cell
    beats the mtime: as_of 2026-08-31 -> 24 h -> ok, basis 'as_of'.
    `now` given as a datetime is the reference: 2026-09-08 12:00 - 08-31
    00:00 = 204 h -> stale. A line dated 2026-08-20 with the same old mtime
    fires BOTH rules, in order ['content', 'file_age']."""
    line = _line(receipt_date="2026-10-01")
    _, _, feed = _gate([line], source_mtime="2026-08-20 00:00:00")
    assert feed["state"] == "stale" and feed["stale_reason"] == ["file_age"]
    assert feed["age_basis"] == "mtime" and feed["source_age_h"] == pytest.approx(288.0)
    _, _, feed = _gate([line], source_mtime="2026-08-20 00:00:00", as_of="2026-08-31")
    assert feed["state"] == "ok" and feed["stale_reason"] == []
    assert feed["age_basis"] == "as_of" and feed["source_age_h"] == pytest.approx(24.0)
    _, _, feed = _gate([line], as_of="2026-08-31", now=datetime(2026, 9, 8, 12, 0))
    assert feed["state"] == "stale" and feed["source_age_h"] == pytest.approx(204.0)
    _, _, feed = _gate([line])                                 # nothing to age
    assert feed["state"] == "ok" and feed["age_basis"] is None and feed["source_age_h"] is None
    old = _line(receipt_date="2026-08-20")
    _, _, feed = _gate([old], source_mtime="2026-08-20 00:00:00", snapshot_date="2026-08-19")
    assert feed["stale_reason"] == ["content", "file_age"]
    # the rule is configurable: 400 h tolerates the 288 h old file
    _, _, feed = _gate([line], source_mtime="2026-08-20 00:00:00",
                       rules={"po_ignore_after_h": 400})
    assert feed["state"] == "ok"


def test_landed_rule_consumes_lots_and_bounds_the_batch_date_both_ways():
    """Finder's r20. Two open lines of 1,000 for 754751 dated 9/8, ONE lot
    of 1,000 on hand with batch N62500001 (= 2026 day 250 = 9/7). Old rule:
    both lines 'landed' (lot never consumed) -> zero inbound booked. New:
    line 1 landed and consumes the lot, line 2 used -> 1,000 booked at 9/8
    16:00 = h184. Window is rd-3 .. rd+1 = 9/5 .. 9/9: lots dated 9/4
    (N6247) and 9/10 (N6253) do not land the line; 9/5 (N6248) and 9/9
    (N6252) do; a 2027-06-01 lot (N71520001) used to cancel the PO."""
    lines = [_line(row=1), _line(row=2, po8="30043544")]
    lots = {"754751": [("N62500001", 1000.0)]}
    receipts, fates, feed = _gate(lines, stock_lots=lots)
    assert [f["fate"] for f in fates] == ["landed", "used"]
    assert "2026-09-05..2026-09-09" in fates[0]["reason"]
    assert receipts["754751"] == [dict(receipts["754751"][0], ready_h=184.0, qty=1000.0)]
    assert feed["n_used"] == 1
    for batch, fate in (("N62470001", "used"), ("N62480001", "landed"),
                        ("N62520001", "landed"), ("N62530001", "used"),
                        ("N71520001", "used")):
        _, fates, _ = _gate([_line()], stock_lots={"754751": [(batch, 1000.0)]})
        assert fates[0]["fate"] == fate, batch
    # 950 matches 0.95 x 1,000 exactly -> landed; 940 does not
    _, fates, _ = _gate([_line()], stock_lots={"754751": [("N62500001", 950.0)]})
    assert fates[0]["fate"] == "landed"
    _, fates, _ = _gate([_line()], stock_lots={"754751": [("N62500001", 940.0)]})
    assert fates[0]["fate"] == "used"
    # two lots of 600: line 1 consumes 600 + 400 (leaving 200), line 2 sees
    # 200 < 950 -> used
    _, fates, _ = _gate(lines, stock_lots={"754751": [("N62500001", 600.0),
                                                     ("N62510001", 600.0)]})
    assert [f["fate"] for f in fates] == ["landed", "used"]


def test_appointment_cannot_credit_a_receipt_before_the_erp_date():
    """ERP receipt 9/8 -> ready 9/8 16:00 = 7 x 24 + 16 = h184. Appointment
    9/7 08:00 + 2 h used to give h154 (a 30 h gift); it is now clamped to
    h184 and the fate says so. Same-day 9/8 08:00 -> h178 (appt tier); the
    next day 9/9 08:00 -> h202 (a delay is honoured)."""
    def run(appt_date, appt_time="8AM"):
        appts = {"30043543": [{"date": appt_date, "time": appt_time, "category": "GPI"}]}
        r, f, _ = _gate([_line()], appts=appts)
        return r["754751"][0], f[0]
    r, f = run("2026-09-07")
    assert r["ready_h"] == 184.0 and r["tier"] == "appt"
    assert "clamped to 2026-09-08 16:00" in f["reason"]
    r, f = run("2026-09-08")
    assert r["ready_h"] == 178.0 and f["reason"] == "counted"
    r, f = run("2026-09-09")
    assert r["ready_h"] == 202.0
    # an unreadable time on the earlier day falls back to the ERP rule
    r, f = run("2026-09-07", appt_time="")
    assert r["ready_h"] == 184.0 and r["tier"] == "erp"


# --------------------------------------------------------------------------
# stock-12 — the cache signature carries the compute date
# --------------------------------------------------------------------------

def test_cache_signature_changes_with_the_date(tmp_path):
    from helpers import stock_report_cache as src
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    a = src.current_signature(dd, str(tmp_path / "vif"), {}, today=date(2026, 9, 1))
    b = src.current_signature(dd, str(tmp_path / "vif"), {}, today=date(2026, 9, 1))
    c = src.current_signature(dd, str(tmp_path / "vif"), {}, today=date(2026, 9, 2))
    assert a == b and a != c
    assert a[-1] == "2026-09-01" and c[-1] == "2026-09-02"
    assert src.current_signature(dd, str(tmp_path / "vif"), {})[-1] == date.today().isoformat()
