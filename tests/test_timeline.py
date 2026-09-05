# tests/test_timeline.py — golden cases for the supply timeline engine.
#
# stockcheck/timeline.py nets component consumption across blocks on one
# per-item curve, applies gated PO receipts as steps and grades each block
# OK / DEPENDENT / SHORT / NO_DATA (contract §3). The cases here are the
# same ones scripts/gen_stock_risk_fixture.py writes to
# data/test_fixtures/stock_risk/cases.json for the TypeScript parity test —
# the numbers are the plan's worked 280351/754751 table (hypothetical
# sleeves stock, all synthetic). test_fixture_in_sync fails when the JSON
# drifts from the engine: rerun the generator and commit the file.

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "scripts"))

from stockcheck import timeline as tl  # noqa: E402
import gen_stock_risk_fixture as gen  # noqa: E402

FIXTURE = ROOT / "data" / "test_fixtures" / "stock_risk" / "cases.json"
CASES = {c["name"]: c for c in gen.CASES}
ANCHOR = "2026-09-01 00:00:00"

P10_RUN = gen.P10_RUN["key"]
P19 = gen.P19["key"]
P10_TAIL = gen.P10_TAIL["key"]
ITEM = gen.ITEM


def run(name: str) -> dict:
    return gen.run_case(CASES[name])


# --------------------------------------------------------------------------
# golden cases
# --------------------------------------------------------------------------

def test_case_a_worked_table():
    r = run("A_worked_table_L96")
    board = r["board"]
    assert set(board) == {P10_RUN, P19, P10_TAIL}

    run_mo = board[P10_RUN]
    assert run_mo["verdict"] == "DEPENDENT"
    assert run_mo["mid_run"] is True
    assert run_mo["backed"] is False
    assert run_mo["action"] == "chase_po"
    assert run_mo["lead_h"] < 0          # truck lands after the MO started

    p19 = board[P19]
    assert p19["verdict"] == "DEPENDENT"
    assert p19["item"] == ITEM
    assert 0.38 <= p19["covered_frac"] <= 0.41
    assert p19["depletion_h"] == pytest.approx(95.3, abs=0.2)
    assert p19["lead_h"] == pytest.approx(31.85, abs=1e-6)
    assert p19["safe_from_h"] == 136.0
    assert p19["mid_run"] is False
    assert p19["action"] == "move"
    assert p19["binding"]["po8"] == "30043543"
    assert p19["binding"]["ready_h"] == 40.0
    assert p19["lead_from"] == "block_start"
    ent = p19["items"][0]
    assert ent["item"] == ITEM and ent["unit"] == "EA"
    assert ent["need"] == 41435.0
    assert ent["opening_at_start"] == pytest.approx(22046, abs=5)
    assert ent["minH"] < 0 and ent["minP"] >= 0 and ent["minR"] < 0
    assert ent["covered_frac_planned"] == 1.0
    assert ent["depletion_planned_h"] is None
    assert 0.7 < ent["dependent_frac"] < 0.8
    # everyone else drawing the item inside P19's window
    assert p19["co_consumers"] == {ITEM: [P10_RUN, P10_TAIL]}
    assert run_mo["co_consumers"] == {ITEM: [P19]}
    for s in ("39%", "PO 30043543", "safe from h136.0", "1.3 d before start",
              "(min 4 d)"):
        assert s in p19["text"], p19["text"]

    tail = board[P10_TAIL]
    assert tail["verdict"] == "DEPENDENT"
    assert tail["lead_h"] == pytest.approx(85.7, abs=0.05)
    assert tail["covered_frac"] == 0.0
    assert tail["depletion_h"] == pytest.approx(125.733)
    assert tail["safe_from_h"] == 136.0

    # planned end-of-board balance: 36,000 - 25,667 - 41,435 - 15,258 + 67,200
    assert tl.balance_at(r["timelines"], ITEM, 188.112) == pytest.approx(
        20840, abs=5)
    assert tl.balance_at(r["timelines"], ITEM, 188.112, planned=False
                         ) == pytest.approx(20840 - 67200, abs=5)


def test_case_b_zero_buffer():
    board = run("B_zero_buffer")["board"]
    assert board[P19]["verdict"] == "OK" and board[P19]["backed"] is True
    assert board[P19]["action"] == "none"
    assert board[P10_TAIL]["verdict"] == "OK" and board[P10_TAIL]["backed"]
    # a receipt after a running MO's start can never make it backed
    assert board[P10_RUN]["verdict"] == "DEPENDENT"
    assert board[P10_RUN]["mid_run"] is True
    assert tl.supply_rank(board[P19]) == 1
    assert "backed" in board[P19]["text"]


def test_case_c_receipt_after_crossing():
    board = run("C_receipt_after_crossing")["board"]
    assert board[P10_RUN]["verdict"] == "SHORT"
    p19 = board[P19]
    assert p19["verdict"] == "SHORT"
    assert 0.38 <= p19["covered_frac"] <= 0.41
    # block-level depletion for SHORT is the PLANNED crossing (d_P)
    assert p19["depletion_h"] == pytest.approx(95.3, abs=0.2)
    assert p19["items"][0]["depletion_planned_h"] == pytest.approx(95.3, abs=0.2)
    assert p19["items"][0]["covered_frac_planned"] == pytest.approx(
        p19["covered_frac"])
    assert p19["binding"]["ready_h"] == 112.0   # latest counted, for the text
    assert p19["text"].startswith("⛔")
    tail = board[P10_TAIL]
    assert tail["verdict"] == "DEPENDENT"
    assert tail["lead_h"] == pytest.approx(13.7, abs=0.05)
    assert tail["safe_from_h"] == 208.0
    # a lead under a day is quoted in whole hours, not "0.6 d"
    assert "— 14 h before start (min 4 d)" in tail["text"], tail["text"]


def test_case_d_no_data():
    # Updated 2026-09-03 (fix K-4 / audit stock-1). D1 used to pin NO_DATA:
    # the PO window (h20) ends before P19 starts, so the crossing "was
    # beyond the feed". But a FRESH open-PO extract that lists no truck at
    # all for this item is a proven shortage on the data we have — the old
    # rule turned every provable shortage on the live board into a grey "?"
    # (67 of 146 blocks, 0 SHORT). NO_DATA now needs an inbound line AND a
    # horizon that reaches into the run yet ends before the crossing.
    board = run("D1_window_exhausted")["board"]
    assert board[P19]["verdict"] == "SHORT"
    assert board[P10_RUN]["verdict"] == "SHORT"
    assert board[P19]["binding"] is None and board[P19]["safe_from_h"] is None
    assert board[P19]["depletion_h"] == pytest.approx(95.3, abs=0.2)
    assert board[P19]["text"].startswith("⛔")
    assert "no inbound counted" in board[P19]["text"]
    # a block whose on-hand covers it is OK whatever the feed says
    assert board["ok_blk@30.00"]["verdict"] == "OK"
    assert board["ok_blk@30.00"]["backed"] is False

    board = run("D2_feed_missing")["board"]
    assert board[P19]["verdict"] == "NO_DATA"
    assert board["ok_blk@30.00"]["verdict"] == "OK"
    assert tl.supply_rank(board[P19]) == 2


def test_case_e_stacked_receipts():
    p19 = run("E1_stacked_both_needed")["board"][P19]
    assert p19["verdict"] == "DEPENDENT"
    assert p19["binding"]["po8"] == "30043602"      # the later of the two
    assert p19["lead_h"] == pytest.approx(21.85)
    assert p19["safe_from_h"] == pytest.approx(146.0)

    p19 = run("E2_stacked_later_not_needed")["board"][P19]
    assert p19["verdict"] == "DEPENDENT"
    assert p19["binding"]["po8"] == "30043601"      # later one dropped
    assert p19["lead_h"] == pytest.approx(41.85)
    assert p19["safe_from_h"] == pytest.approx(126.0)


def test_case_f_concurrent_lines():
    board = run("F1_concurrent_lines_dependent")["board"]
    assert board[P19]["verdict"] == "DEPENDENT"
    assert board["ln2@80.00"]["verdict"] == "DEPENDENT"
    assert board[P19]["co_consumers"] == {ITEM: ["ln2@80.00"]}
    assert board["ln2@80.00"]["co_consumers"] == {ITEM: [P19]}

    board = run("F2_concurrent_lines_short")["board"]
    assert board[P19]["verdict"] == "SHORT"
    assert board["ln2@80.00"]["verdict"] == "SHORT"
    assert "no inbound counted" in board[P19]["text"]


def test_case_g_running_clip():
    r = run("G1_running_cases_left")
    draws = r["timelines"]["items"][ITEM]["draws"]
    assert draws == [{"key": "run_cl@0.02", "a": 14.8, "e": 119.733,
                      "qty": 20000.0}]
    done = r["board"]["done@0.00"]
    assert done["verdict"] == "OK"
    assert done["items"][0]["need"] == 0.0
    assert done["covered_frac"] == 1.0
    assert r["board"]["run_cl@0.02"]["verdict"] == "OK"

    r = run("G2_running_fallback")
    draws = r["timelines"]["items"][ITEM]["draws"]
    assert len(draws) == 1 and draws[0]["key"] == "run_fb@0.02"
    assert draws[0]["a"] == 14.8
    assert draws[0]["qty"] == pytest.approx(25667, abs=0.5)
    # 29283 x (119.733 - 14.8) / (119.733 - 0.017)
    assert draws[0]["qty"] == pytest.approx(
        29283.0 * (119.733 - 14.8) / (119.733 - 0.017))


def test_case_h_alternate_pooling():
    r = run("H1_alt_pool_covers")
    h1 = r["board"]["h1@20.00"]
    assert h1["verdict"] == "OK" and h1["untracked"] == []
    assert h1["items"][0]["opening_at_start"] == 50000.0
    assert r["timelines"]["groups"][gen.ALT_SKU][0]["members"] == [
        gen.ALT_PRIMARY, gen.ALT_ITEM]

    r = run("H2_alt_pool_drained")
    h1 = r["board"]["h1@20.00"]
    assert h1["verdict"] == "SHORT"
    assert h1["item"] == gen.ALT_PRIMARY
    assert h1["co_consumers"] == {gen.ALT_ITEM: ["h2@20.00"]}
    # Updated 2026-09-03 (fix K-3 / audit stock-5). h1's 10,000 used to sit
    # on the PRIMARY 752700 (on hand 0), so the SKU whose only member is
    # 752701 saw 50,000 - 45,000 >= 0 and read OK while the two runs
    # together needed 55,000 of the 50,000 physical units. Draws are now
    # netted sequentially over the group: 752700 has nothing, so h1's
    # 10,000 is booked on 752701 and h2 sees 50,000 - 10,000 - 45,000 =
    # -5,000 at h40 -> SHORT, with h1 as its co-consumer.
    assert r["timelines"]["items"][gen.ALT_PRIMARY]["draws"] == []
    assert [(d["key"], d["qty"]) for d in
            r["timelines"]["items"][gen.ALT_ITEM]["draws"]] == [
        ("h1@20.00", 10000.0), ("h2@20.00", 45000.0)]
    h2 = r["board"]["h2@20.00"]
    assert h2["verdict"] == "SHORT"
    assert h2["items"][0]["minH"] == pytest.approx(-5000.0)
    assert h2["co_consumers"] == {gen.ALT_ITEM: ["h1@20.00"]}


def test_case_i_minor_dependent():
    p19 = run("I_minor_dependent")["board"][P19]
    assert p19["verdict"] == "DEPENDENT"
    assert p19["minor"] is True
    assert p19["items"][0]["dependent_frac"] == pytest.approx(1435 / 41435)
    assert tl.supply_rank(p19) == 1
    assert "minor share" in p19["text"]


def test_case_j_earliest_clear_start():
    ecs = run("J1_earliest_clear_start")["ecs"]
    assert ecs["safe_from_h"] == 136.0
    assert ecs["verdict_now"]["verdict"] == "DEPENDENT"
    assert ecs["verdict_at_safe"]["verdict"] == "OK"
    assert ecs["verdict_at_safe"]["backed"] is True
    assert ecs["verdict_at_safe"]["key"] == f"virtual:{gen.SKU}@136.00"

    ecs = run("J2_earliest_clear_start_no_receipts")["ecs"]
    assert ecs["safe_from_h"] is None
    assert ecs["verdict_now"]["verdict"] == "SHORT"
    assert ecs["verdict_at_safe"] is None


def test_earliest_clear_start_leaves_timelines_untouched():
    r = run("J1_earliest_clear_start")
    before = json.dumps(r["timelines"], sort_keys=True)
    tl.earliest_clear_start(gen.SKU, 41435.0, 59.65, r["timelines"], {}, "ok",
                            800.0, 71.85)
    assert json.dumps(r["timelines"], sort_keys=True) == before


def test_case_k_lead_from_depletion():
    p19 = run("K_lead_from_depletion")["board"][P19]
    assert p19["verdict"] == "DEPENDENT"
    assert p19["lead_from"] == "depletion"
    assert p19["lead_h"] == pytest.approx(95.3 - 40.0, abs=0.2)
    assert p19["safe_from_h"] == 136.0
    assert "before it runs out" in p19["text"]


def test_case_l_unknown_and_untracked():
    board = run("L_unknown_and_untracked")["board"]
    unk = board["unk@10.00"]
    assert unk["verdict"] == "NO_DATA" and unk["item"] is None
    assert unk["items"] == [] and unk["action"] == "none"
    assert board["nocases@10.00"]["verdict"] == "NO_DATA"
    ink = board["ink@10.00"]
    assert ink["verdict"] == "OK"
    assert ink["untracked"] == ["758001"]
    assert ink["items"] == []
    assert "no tracked components" in ink["text"]


def test_exact_exhaustion_is_ok():
    # draws that consume exactly the opening must not flip on float noise
    blocks = [gen.block("x@0.00", "x", gen.SKU, "P19", 20.0, 30.0, 12000.0),
              gen.block("y@0.00", "y", gen.SKU, "P21", 20.0, 30.0, 24000.0)]
    t = tl.build_timelines(blocks, gen.needs(gen.SKU, ITEM), {ITEM: 36000.0},
                           {}, {ITEM: 0.0}, tracked=[ITEM], in_house=[])
    board = tl.evaluate_board(blocks, t, {}, "ok", 800.0)
    assert all(s["verdict"] == "OK" for s in board.values())
    assert tl.balance_at(t, ITEM, 30.0) == pytest.approx(0.0, abs=1e-9)


def test_in_house_and_zero_per_case_groups_skipped():
    sn = {gen.SKU: {"kg_per_case": 2.0, "items": [
        {"item": "BT001", "per_case": 0.5, "unit": "KG", "alts": []},
        {"item": "752420", "per_case": 1.0, "unit": "M2", "alts": ["BT002"]},
        {"item": ITEM, "per_case": 0.0, "unit": "EA", "alts": []},
    ]}}
    t = tl.build_timelines([], sn, {}, {}, {}, tracked=[ITEM, "752420"],
                           in_house=["BT001", "BT002"])
    assert t["groups"][gen.SKU] == []
    assert t["items"] == {}


def test_verdict_text_with_anchor():
    p19 = run("A_worked_table_L96")["board"][P19]
    text = tl.verdict_text(p19, ANCHOR)
    assert "PO 30043543 lands Wed 9/2 16:00" in text
    assert "safe from Sun 9/6 16:00" in text
    assert "runs out Fri 9/4 23:" in text
    assert text.startswith("🚚 754751: on hand covers 39%")
    # engine text (no anchor) uses hour stamps
    assert "h136.0" in p19["text"]


def test_verdict_text_sub_day_lead_reads_in_hours():
    """A truck under a day out is quoted in whole hours (half-up) — live
    finding: 1 h before start read "0.0 d before start" — and from 24 h on
    the sentence keeps tenths of a day. Same form for the OK-backed line."""
    base = run("A_worked_table_L96")["board"][P19]

    def text(lead_h, **over):
        return tl.verdict_text({**base, "lead_h": lead_h, **over})

    assert "— 1 h before start (min 4 d)" in text(1.0)
    assert "— 13 h before start" in text(13.4)
    assert "— 14 h before start" in text(13.5)
    assert "— 0 h before start" in text(0.4)
    assert "— 24 h before start" in text(23.6)
    assert "— 1.0 d before start" in text(24.0)
    assert "— 1.3 d before start" in text(31.85)
    assert "— 3 h after start (mid-run)" in text(-3.0, mid_run=True)
    assert "— 1.7 d after start (mid-run)" in text(-39.983, mid_run=True)
    assert "— 1 h before start" in text(1.0, verdict="OK", backed=True)
    assert "— 1 h before it runs out" in text(1.0, lead_from="depletion")
    for h in (0.4, 1.0, 13.4, 23.6, -3.0):
        assert "0.0 d" not in text(h) and " d before" not in text(h), text(h)
    assert tl._lead(1.0) == "1 h" and tl._lead(-13.7) == "14 h"
    assert tl._lead(24.0) == "1.0 d" and tl._lead(-96.0) == "4.0 d"


def test_supply_rank():
    def s(verdict, backed=False, minor=False):
        return {"verdict": verdict, "backed": backed, "minor": minor}
    assert tl.supply_rank(s("SHORT")) == 4
    assert tl.supply_rank(s("DEPENDENT")) == 3
    assert tl.supply_rank(s("DEPENDENT", minor=True)) == 1
    assert tl.supply_rank(s("NO_DATA")) == 2
    assert tl.supply_rank(s("OK", backed=True)) == 1
    assert tl.supply_rank(s("OK")) == 0


def test_default_rules_match_contract():
    assert tl.DEFAULT_RULES == {
        "min_days_after_delivery": 4, "lead_measured_from": "block_start",
        "receipt_ready_hour": 16, "raw_qc_offset_h": 72,
        "appt_ready_offset_h": 2, "po_ignore_after_h": 168,
        "landed_match_frac": 0.95, "dependent_frac_floor": 0.05,
        "hard_block": False, "use_board_qty_kg": True,
        "raw_areas": ["RB1", "AMB", "RC1"], "offsite_areas": ["SL3"],
    }


# --------------------------------------------------------------------------
# gate_receipts (§3.6)
# --------------------------------------------------------------------------

BOM = {"754751", "752420", "735009-A", "730021"}
UNITS = {"754751": "EA", "752420": "M2", "735009-A": "EA", "730021": "KG"}
TODAY = date(2026, 9, 1)
SNAP = "2026-09-01"


def _line(**over) -> dict:
    line = {"po": "2 02 1ACDE 30043543", "po8": "30043543", "item": "754751",
            "designation": "SLV 24x90", "qty": 67200.0, "unit": "EA",
            "receipt_date": "2026-09-02", "initial_receipt_date": "2026-08-27",
            "slip_days": 6, "arrival_area": "RP1", "supplier": "GPI",
            "supplier_id": "48", "received": False,
            "order_date": "2026-07-24", "row": 12}
    line.update(over)
    return line


def _gate(lines, **kw):
    args = {"bom_items": BOM, "unit_by_item": UNITS, "snapshot_date": SNAP,
            "today": TODAY, "anchor": ANCHOR, "rules": {}}
    args.update(kw)
    return tl.gate_receipts(lines, **args)


def test_gate_fates_in_order():
    lines = [
        _line(row=1, received=True),
        _line(row=2, item="899999"),
        _line(row=3, unit="M2"),
        _line(row=4, qty=0),
        _line(row=5, receipt_date="garbage"),
        _line(row=6, receipt_date="2026-08-28"),
        _line(row=7),                                   # plain ERP line
        _line(row=8, item="735009", po8="30043544",
              receipt_date="2026-09-04"),               # suffix key
    ]
    receipts, fates, feed = _gate(lines)
    by_row = {f["row"]: f for f in fates}
    assert len(fates) == len(lines)
    assert by_row[1]["fate"] == "received"
    assert by_row[2]["fate"] == "unjoinable" and by_row[2]["item_key"] is None
    assert by_row[3]["fate"] == "unit_mismatch"
    assert "M2" in by_row[3]["reason"] and "EA" in by_row[3]["reason"]
    assert by_row[4]["fate"] == "bad_qty"
    assert by_row[5]["fate"] == "bad_date"
    assert by_row[6]["fate"] == "overdue"
    assert by_row[7]["fate"] == "used"
    assert by_row[7]["tier"] == "erp"
    assert by_row[7]["ready_h"] == 40.0            # Wed 9/2 16:00
    assert by_row[8]["fate"] == "used" and by_row[8]["item_key"] == "735009-A"
    assert set(receipts) == {"754751", "735009-A"}
    r = receipts["754751"][0]
    assert r == {"ready_h": 40.0, "qty": 67200.0, "po8": "30043543",
                 "tier": "erp", "receipt_date": "2026-09-02",
                 "label": "PO 30043543 · 754751 · 67,200 EA · 2026-09-02 · "
                          "ERP date"}
    assert feed["state"] == "ok"
    assert feed["n_used"] == 2 and feed["n_lines"] == 8
    assert feed["max_receipt_date"] == "2026-09-04"
    # latest used ready is 9/4 16:00 = 88.0, but the extract's last receipt
    # date (9/4) vouches for that whole day: the window ends 9/4 23:59
    assert feed["receipts_window_end_h"] == pytest.approx(95 + 59 / 60)
    assert feed["errors"] == []
    assert feed["fates"]["overdue"] == 1


def test_gate_landed_rule():
    lines = [_line(row=1, item="752420", unit="M2", qty=1000.0,
                   receipt_date="2026-09-03"),
             _line(row=2, item="730021", unit="KG", qty=500.0,
                   receipt_date="2026-09-03", po8="30043550"),
             _line(row=3, item="754751", receipt_date="2026-09-03",
                   po8="30043551")]
    lots = {
        # N6 245 -> 2026-09-02, inside receipt_date - 3 d; 960 >= 95% of 1000
        "752420": [("N62450001", 960.0), ("N62300001", 5000.0)],
        # S-batches (aseptic) cannot be dated -> counted, flagged
        "730021": [("S12345", 400.0)],
        # no lots at all -> rule does not fire
    }
    receipts, fates, feed = _gate(lines, stock_lots=lots)
    by_row = {f["row"]: f for f in fates}
    assert by_row[1]["fate"] == "landed" and "752420" not in receipts
    assert by_row[2]["fate"] == "landed_unverifiable"
    assert by_row[2]["ready_h"] is not None and "730021" in receipts
    assert by_row[3]["fate"] == "used"
    assert feed["n_used"] == 2

    # lots dated before the cutoff do not count as the same delivery
    receipts, fates, _ = _gate(
        [lines[0]], stock_lots={"752420": [("N62300001", 5000.0)]})
    assert fates[0]["fate"] == "used"
    # no stock_lots -> rule skipped entirely
    _, fates, _ = _gate([lines[0]])
    assert fates[0]["fate"] == "used"


def test_gate_offsite_and_appointments():
    appts = {
        "30043600": [{"date": "2026-09-03", "time": "08:00",
                      "category": "SL3 transefer"}],
        "30043543": [{"date": "2026-09-03", "time": "09:00",
                      "category": "GPI"}],
        "30043700": [{"date": "2026-09-10", "time": "09:00",
                      "category": "GPI"}],           # outside +-1 d
    }
    lines = [
        _line(row=1, arrival_area="SL3", po8="30043601"),   # no appt
        _line(row=2, arrival_area="SL3", po8="30043600",
              receipt_date="2026-09-03"),                   # SL3 TRANSFER appt
        _line(row=3, po8="30043543"),                       # appt within +-1 d
        _line(row=4, po8="30043700"),                       # appt too far
        _line(row=5, item="730021", unit="KG", qty=500.0,
              arrival_area="RB1", receipt_date="2026-09-03",
              po8="30043552"),                              # raw: +72 h QC
    ]
    receipts, fates, feed = _gate(lines, appts=appts)
    by_row = {f["row"]: f for f in fates}
    assert by_row[1]["fate"] == "offsite_no_transfer"
    assert by_row[2]["fate"] == "used" and by_row[2]["tier"] == "appt"
    assert by_row[2]["ready_h"] == 58.0        # 9/3 08:00 + 2 h
    assert by_row[3]["tier"] == "appt" and by_row[3]["ready_h"] == 59.0
    assert by_row[4]["tier"] == "erp" and by_row[4]["ready_h"] == 40.0
    assert by_row[5]["tier"] == "erp"
    assert by_row[5]["ready_h"] == 136.0       # 9/3 16:00 + 72 h = 9/6 16:00
    assert feed["receipts_window_end_h"] == 136.0
    assert [r["ready_h"] for r in receipts["754751"]] == [40.0, 58.0, 59.0]
    assert receipts["754751"][1]["label"].endswith("appt")


def test_gate_feed_states():
    _, fates, feed = _gate(None)
    assert feed["state"] == "missing" and fates == []
    _, fates, feed = _gate([])
    assert feed["state"] == "empty" and fates == []
    # every receipt date behind today -> stale (content rule, not mtime)
    receipts, fates, feed = _gate(
        [_line(receipt_date="2026-08-30")], snapshot_date="2026-08-25")
    assert feed["state"] == "stale"
    assert feed["max_receipt_date"] == "2026-08-30"
    assert fates[0]["fate"] == "used"          # still gated, only flagged stale
    assert receipts["754751"][0]["ready_h"] == -32.0   # 8/30 16:00 vs 9/1 anchor
    # unparseable dates only -> nothing vouches for the window
    _, _, feed = _gate([_line(receipt_date=None)])
    assert feed["state"] == "stale"
    # unit unknown in the BOM -> accepted (best-effort join)
    _, fates, _ = _gate([_line()], unit_by_item={})
    assert fates[0]["fate"] == "used"


def test_batch_and_key_helpers_come_from_po_import():
    from stockcheck import po_import
    # one implementation, no divergent local twin
    assert tl.decode_batch_date is po_import.decode_batch_date
    assert tl.resolve_item_key is po_import.resolve_item_key
    assert not hasattr(tl, "_decode_batch_date_local")
    assert not hasattr(tl, "_resolve_item_key_local")
    assert tl.decode_batch_date("N62420340") == date(2026, 8, 30)
    assert tl.decode_batch_date("n62450001") == date(2026, 9, 2)
    assert tl.decode_batch_date("S12345") is None
    assert tl.decode_batch_date("N63990001") is None     # DOY 399
    assert tl.decode_batch_date("N33660001") is None     # DOY 366, 2023 not leap
    assert tl.decode_batch_date("N43660001") == date(2024, 12, 31)
    assert tl.decode_batch_date(None) is None
    assert tl.resolve_item_key("754751", BOM) == "754751"
    assert tl.resolve_item_key("735009", BOM) == "735009-A"
    assert tl.resolve_item_key("899999", BOM) is None
    assert tl.resolve_item_key("7350", BOM) is None      # not a whole code
    assert tl.resolve_item_key("735009", {"735009-A", "735009-B"}) is None


# --------------------------------------------------------------------------
# input normalisation / never-raise (verifier findings)
# --------------------------------------------------------------------------

def test_gate_never_raises_on_bad_lots_and_rows():
    lines = [None, "garbage", 42,
             _line(row=4, item="752420", unit="M2", qty=1000.0,
                   receipt_date="2026-09-03"),
             _line(row=5, item="752420", unit="M2", qty=1000.0,
                   receipt_date="2026-09-03", po8="30043560"),
             _line(row=6, item="730021", unit="KG", qty=500.0,
                   receipt_date="2026-09-03", po8="30043561")]
    lots = {
        # a dated lot with a garbage qty counts as 0 -> not landed
        "752420": [("N62450001", "abc"), ("N62450002", None), "notapair"],
        "730021": 7,                                  # not a list at all
    }
    receipts, fates, feed = _gate(lines, stock_lots=lots,
                                  rules={"receipt_ready_hour": "16",
                                         "appt_ready_offset_h": "x",
                                         "raw_areas": "RB1"})
    assert len(fates) == len(lines) and feed["n_lines"] == 6
    bad = [f for f in fates if f["fate"] == "bad_row"]
    assert len(bad) == 3
    assert all(f["ready_h"] is None and f["item_key"] is None for f in bad)
    # bad rows still carry the PoLine columns
    assert set(bad[0]) >= {"po8", "item", "qty", "unit", "receipt_date", "row"}
    by_row = {f["row"]: f for f in fates if f["row"] is not None}
    assert by_row[4]["fate"] == "used" and by_row[5]["fate"] == "used"
    assert by_row[6]["fate"] == "used"                 # rule skipped, not flagged
    assert [r["ready_h"] for r in receipts["752420"]] == [64.0, 64.0]
    assert feed["fates"] == {"bad_row": 3, "used": 3}
    assert feed["state"] == "ok"
    errs = feed["errors"]
    assert any("lines[0]" in e and "NoneType" in e for e in errs)
    assert any("'abc'" in e and "752420" in e for e in errs)
    assert any("notapair" in e for e in errs)
    assert any("730021" in e for e in errs)
    # the same bad lot behind two PO lines is reported once
    assert len([e for e in errs if "'abc'" in e]) == 1
    # a None qty is a blank, not an error
    assert not any("N62450002" in e for e in errs)
    # stock_lots itself not a dict -> rule skipped, noted, nothing raised
    _, fates, feed = _gate([lines[3]], stock_lots=[("N62450001", 1000.0)])
    assert fates[0]["fate"] == "used" and feed["errors"]
    # lines that cannot even be iterated -> missing, noted
    for bad_lines in (42, "open_pos.xlsx"):        # a path is not the lines
        receipts, fates, feed = _gate(bad_lines)
        assert receipts == {} and fates == []
        assert feed["state"] == "missing" and feed["errors"]


def test_gate_feed_info_always_has_errors_list():
    keys = {"state", "receipts_window_end_h", "n_used", "n_lines",
            "max_receipt_date", "fates", "errors"}
    for lines in (None, [], [_line()]):
        _, _, feed = _gate(lines)
        assert feed["errors"] == []
        assert set(feed) >= keys


def test_gate_window_end_bounded_by_last_receipt_date():
    # fresh feed whose only lines are unjoinable: nothing used, but the
    # extract still vouches that no truck is booked after its last date
    lines = [_line(row=1, item="899999", receipt_date="2026-09-10"),
             _line(row=2, item="899998", receipt_date="2026-09-08")]
    receipts, fates, feed = _gate(lines)
    assert receipts == {} and feed["n_used"] == 0
    assert feed["state"] == "ok"
    assert feed["max_receipt_date"] == "2026-09-10"
    assert feed["receipts_window_end_h"] == pytest.approx(9 * 24 + 23 + 59 / 60)
    # a used line whose ready hour is past the file's last day wins
    # (raw QC pushes 9/10 16:00 to 9/13 16:00)
    lines.append(_line(row=3, item="730021", unit="KG", qty=500.0,
                       arrival_area="RB1", receipt_date="2026-09-10"))
    _, _, feed = _gate(lines)
    assert feed["receipts_window_end_h"] == 9 * 24 + 16 + 72
    # nothing parseable at all -> 0.0 (and stale: nothing vouches)
    _, _, feed = _gate([_line(receipt_date="garbage")])
    assert feed["receipts_window_end_h"] == 0.0 and feed["state"] == "stale"
    # a stale feed's bound is honest even when it lies behind the anchor
    _, _, feed = _gate([_line(item="899999", receipt_date="2026-08-30")],
                       snapshot_date="2026-08-25")
    assert feed["state"] == "stale"
    assert feed["receipts_window_end_h"] == pytest.approx(-2 * 24 + 23 + 59 / 60)


def test_gate_blank_appt_time_falls_back_to_erp():
    appts = {
        "30043543": [{"date": "2026-09-02", "time": "", "category": "GPI"}],
        "30043544": [{"date": "2026-09-02", "time": "TBD", "category": "GPI"}],
        "30043545": [{"date": "2026-09-02", "time": None, "category": "GPI"}],
        "30043546": [{"date": "2026-09-02", "time": "25:00", "category": "GPI"}],
        # same day: the timed slot beats the blank one
        "30043547": [{"date": "2026-09-02", "time": "", "category": "GPI"},
                     {"date": "2026-09-02", "time": "8AM", "category": "GPI"}],
        "30043600": [{"date": "2026-09-03", "time": "",
                      "category": "SL3 transefer"}],
        "30043552": [{"date": "2026-09-03", "time": "", "category": "GPI"}],
    }
    lines = [_line(row=1, po8="30043543"),
             _line(row=2, po8="30043544"),
             _line(row=3, po8="30043545"),
             _line(row=4, po8="30043546"),
             _line(row=5, po8="30043547"),
             _line(row=6, arrival_area="SL3", po8="30043600",
                   receipt_date="2026-09-03"),
             _line(row=7, item="730021", unit="KG", qty=500.0,
                   arrival_area="RB1", receipt_date="2026-09-03",
                   po8="30043552")]
    receipts, fates, feed = _gate(lines, appts=appts)
    by_row = {f["row"]: f for f in fates}
    for row in (1, 2, 3, 4):
        f = by_row[row]
        assert f["fate"] == "used" and f["tier"] == "erp", row
        assert f["ready_h"] == 40.0, row               # 9/2 16:00, no +2 h
        assert "ERP date used" in f["reason"], row
    assert by_row[5]["tier"] == "appt" and by_row[5]["ready_h"] == 34.0
    assert by_row[5]["reason"] == "counted"
    # offsite: the SL3 appointment still admits the line, the ERP rule times it
    assert by_row[6]["fate"] == "used" and by_row[6]["tier"] == "erp"
    assert by_row[6]["ready_h"] == 64.0
    # raw: ERP fallback + QC offset, never appointment date + guessed hour
    assert by_row[7]["tier"] == "erp" and by_row[7]["ready_h"] == 136.0
    for r in receipts["754751"]:
        assert r["label"].endswith("appt" if r["tier"] == "appt" else "ERP date")
    assert feed["errors"] == []


@pytest.mark.parametrize("raw, ready_h", [
    ("8AM", 58.0), ("11am", 61.0), ("1PM", 63.0), ("07:00", 57.0),
    ("7:00", 57.0), (" 8:30 PM ", 70.5), ("12PM", 62.0), ("12AM", 50.0),
    ("9:00:00", 59.0), ("p.m. 3", None), ("13PM", None), ("8:75", None),
    (True, None), (time(9, 0), 59.0), (datetime(2026, 9, 3, 9, 15), 59.25),
])
def test_gate_appt_time_forms(raw, ready_h):
    appts = {"30043543": [{"date": "2026-09-03", "time": raw,
                           "category": "GPI"}]}
    _, fates, _ = _gate([_line(receipt_date="2026-09-03")], appts=appts)
    f = fates[0]
    assert f["fate"] == "used"
    if ready_h is None:
        assert f["tier"] == "erp" and f["ready_h"] == 64.0     # 9/3 16:00
    else:
        assert f["tier"] == "appt" and f["ready_h"] == ready_h


def test_build_timelines_canonicalises_item_keys():
    ref = run("A_worked_table_L96")
    inp = gen.base_inputs()
    t = tl.build_timelines(
        inp["blocks"],
        {gen.SKU: {"kg_per_case": 2.16, "items": [
            {"item": f" {ITEM} ", "per_case": 1.0, "unit": "EA",
             "alts": ["", None]}]}},
        {int(ITEM): 36000.0},
        {f"{ITEM} ": inp["receipts"][ITEM]},
        {f" {ITEM}": gen.SNAPSHOT_H},
        tracked=[f"{ITEM}\t", ""], in_house=[" BT001", "BT002 ", None])
    assert set(t["items"]) == {ITEM}
    assert t["tracked"] == [ITEM] and t["in_house"] == ["BT001", "BT002"]
    assert t["groups"][gen.SKU][0]["members"] == [ITEM]
    assert t["items"] == ref["timelines"]["items"]
    board = tl.evaluate_board(inp["blocks"], t, {}, "ok", gen.WINDOW_END)
    assert board == ref["board"]
    # keys that collide after trimming pool: openings add, receipts
    # concatenate, the first snapshot hour wins
    t = tl.build_timelines([], gen.needs(gen.SKU, ITEM),
                           {ITEM: 1000.0, f"{ITEM} ": 500.0},
                           {ITEM: [gen.R40], f" {ITEM}": [gen.R112]},
                           {ITEM: 14.8, f"{ITEM} ": 99.0},
                           tracked=[ITEM], in_house=[])
    it = t["items"][ITEM]
    assert it["opening"] == 1500.0 and it["snapshot_h"] == 14.8
    assert [r["ready_h"] for r in it["receipts"]] == [40.0, 112.0]
    # alts trimmed and de-duplicated
    t = tl.build_timelines(
        [], gen.needs(gen.ALT_SKU, gen.ALT_PRIMARY,
                      alts=[f" {gen.ALT_ITEM}", gen.ALT_ITEM]),
        {}, {}, {}, tracked=[gen.ALT_ITEM], in_house=[])
    assert t["groups"][gen.ALT_SKU][0]["members"] == [gen.ALT_PRIMARY,
                                                     gen.ALT_ITEM]
    # a trimmed in-house code still silences its group
    t = tl.build_timelines([], gen.needs(gen.SKU, ITEM, alts=["BT002"]),
                           {}, {}, {}, tracked=[ITEM], in_house=[" BT002 "])
    assert t["groups"][gen.SKU] == []


def test_duplicate_block_keys_keep_first():
    def build(blocks):
        inp = gen.base_inputs(blocks=blocks)
        return tl.build_timelines(
            inp["blocks"], inp["sku_needs"], inp["opening"], inp["receipts"],
            inp["snapshot_h"], tracked=inp["tracked"], in_house=inp["in_house"])

    once = build([gen.P19])
    same = [gen.P19, dict(gen.P19)]
    twice = build(same)
    assert twice["items"] == once["items"]           # one draw, not two
    assert twice["blocks"] == once["blocks"]
    assert (tl.evaluate_board(same, twice, {}, "ok", 800.0)
            == tl.evaluate_board([gen.P19], once, {}, "ok", 800.0))
    # a later block with the same key but other content is ignored too
    t3 = build([gen.P19, {**gen.P19, "end_h": 500.0, "cases": 9e9}])
    assert t3["items"] == once["items"]
    assert t3["blocks"][P19]["end_h"] == 131.5
    # add_block on a registered key hands back the registered block, no draw
    got = tl.add_block(t3, {**gen.P19, "cases": 9e9})
    assert got is t3["blocks"][P19] and got["cases"] == 41435.0
    assert t3["items"][ITEM]["draws"] == once["items"][ITEM]["draws"]


def test_no_data_action_split_is_documented():
    doc = tl.evaluate_block.__doc__ or ""
    assert "NO_DATA" in doc and '"none"' in doc and "chase_po" in doc
    # the fixture pins both halves: no recipe -> none; tracked group -> move
    board = run("L_unknown_and_untracked")["board"]
    assert board["unk@10.00"]["action"] == "none"
    assert board["nocases@10.00"]["action"] == "none"
    # D2 (feed missing) is the NO_DATA case since fix K-4 (audit stock-1):
    # D1's exhausted window with no inbound at all now reads SHORT.
    board = run("D2_feed_missing")["board"]
    assert board[P19]["verdict"] == "NO_DATA"
    assert board[P19]["action"] == "move"
    assert board[P10_RUN]["action"] == "chase_po"


# --------------------------------------------------------------------------
# fixture parity
# --------------------------------------------------------------------------

def test_fixture_in_sync():
    assert FIXTURE.exists(), "run scripts/gen_stock_risk_fixture.py"
    on_disk = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fresh = gen.build_fixture()
    assert on_disk["rules"] == fresh["rules"]
    assert [c["name"] for c in on_disk["cases"]] == [c["name"] for c in fresh["cases"]]
    for a, b in zip(on_disk["cases"], fresh["cases"]):
        assert a == b, f"fixture drift in case {a['name']}"


def test_fixture_shape():
    fix = gen.build_fixture()
    inputs = {"blocks", "sku_needs", "opening", "tracked", "in_house",
              "receipts", "snapshot_h", "feed_state", "receipts_window_end_h"}
    supply_keys = {"key", "verdict", "backed", "minor", "mid_run", "item",
                   "covered_frac", "depletion_h", "lead_h", "safe_from_h",
                   "binding", "action", "lead_from", "items", "untracked",
                   "co_consumers", "text"}
    names = [c["name"] for c in fix["cases"]]
    assert len(names) == len(set(names))
    for c in fix["cases"]:
        assert set(c["inputs"]) == inputs
        assert set(c["rules"]) == set(tl.DEFAULT_RULES)
        for key, sup in c["expected"].items():
            assert sup["key"] == key
            assert set(sup) == supply_keys
            assert sup["verdict"] in {"OK", "DEPENDENT", "SHORT", "NO_DATA"}
            assert sup["action"] in {"none", "move", "chase_po"}
