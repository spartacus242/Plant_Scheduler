# tests/test_card_credit_trust.py — the Plant Calendar's week cards must not
# drift when MOs are running or have completed (planner report 2026-09-15:
# "especially for W0 the data seems to be a bit off ... when MOs have been
# completed or are currently running").
#
# Two independent drifts were confirmed on live data and are pinned here:
#
#   1. BOARD SNAPSHOT vs LIVE MANPRG. A running MO's board block carries the
#      REMAINING kg and freezes the made kg in its attrs (`made_kg=`); the
#      credit map is rebuilt from the CURRENT manprg on every render. The old
#      rule credited the LIVE made kg on top of the FROZEN remaining kg, so
#      the sum drifted above the MO's Fct with every pull (a finished order
#      read OVER), and credited NOTHING once the plant finished the MO (the
#      row stopped being `made_part` while the MO was still on the board), so
#      a fully-made order read ~10% with a phantom holding card.
#      reconcile_completed_to_board credits the FROZEN value instead, which
#      makes board kg + credit == Fct in BOTH drift directions.
#
#   2. SUNDAY PRODUCTION DISCARDED. Made kg is keyed by the ISO week of the
#      production midpoint, so an MO the plant ran on Sunday keys to the
#      PREVIOUS ISO week; the demand file is re-imported each Monday anchored
#      at W0, so there is no row to settle it against and carry_unsettled_past
#      threw it away (live: 139 t, five W0 orders reading 0%). The solver's
#      netting must keep refusing to guess (fix C54), but the calendar's
#      question is "is this week's demand covered?" and that product is in the
#      warehouse — build_ledger_from_data now carries it forward on the
#      made_only path only.

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers import demand_coverage as dc  # noqa: E402
from helpers.demand_coverage import (  # noqa: E402
    board_made_kg, build_ledger, keep_completed_row,
    reconcile_completed_to_board)

# A running MO as current_state writes it: the block holds Fct - made, the
# attrs freeze the made share. Every piece of a split MO repeats the token,
# and the page joins the pieces' attrs with ";".
RUNNING_ATTRS = ("current_state:running;pct=94.3;started=2026-09-13T06:06;"
                 "fct_kg=26001;made_kg=24509;clamped_to_anchor;split")
QUEUED_ATTRS = "current_state:queued"
FCT, FROZEN_MADE = 26001.0, 24509.0
FROZEN_REMAINING = FCT - FROZEN_MADE          # 1,492 kg — the board block's kg


def _row(mo="30384", kind="made_part", made=FROZEN_MADE, item="280584"):
    return {"mo": mo, "kind": kind, "item": item, "made_kg": made,
            "qty_kg": FCT, "start_dt": pd.Timestamp("2026-09-13 06:06"),
            "hours": 30.0}


# ---------------------------------------------------------------------------
# 1a. the frozen-made parser
# ---------------------------------------------------------------------------

def test_board_made_kg_reads_the_frozen_token():
    attrs = {"30384": RUNNING_ATTRS}
    assert board_made_kg("30384", attrs) == pytest.approx(FROZEN_MADE)
    assert board_made_kg("30384", None) is None          # no board known
    assert board_made_kg("99999", attrs) is None         # MO not on the board
    assert board_made_kg("30384", {"30384": QUEUED_ATTRS}) is None


def test_board_made_kg_handles_joined_split_pieces_and_decimals():
    # the page concatenates every piece's attrs for one MO
    joined = {"30384": f";{RUNNING_ATTRS};{RUNNING_ATTRS}"}
    assert board_made_kg("30384", joined) == pytest.approx(FROZEN_MADE)
    assert board_made_kg("1", {"1": "made_kg=1234.5;x"}) == pytest.approx(1234.5)
    # an int MO key still matches (order ids arrive as strings elsewhere)
    assert board_made_kg(30384, {"30384": RUNNING_ATTRS}) == pytest.approx(FROZEN_MADE)


# ---------------------------------------------------------------------------
# 1b. the reconciliation rule — board kg + credit == Fct, both directions
# ---------------------------------------------------------------------------

def _credited(rows, exclude, attrs):
    out = reconcile_completed_to_board(rows, exclude, attrs)
    return {r["mo"]: float(r["made_kg"]) for r in out}


def test_running_mo_credits_the_frozen_made_not_the_live_value():
    """The plant has produced 1,000 kg more since the board was rebuilt."""
    live = _row(made=FROZEN_MADE + 1000.0)
    got = _credited([live], {"30384"}, {"30384": RUNNING_ATTRS})
    assert got["30384"] == pytest.approx(FROZEN_MADE)
    # the invariant the cards depend on
    assert FROZEN_REMAINING + got["30384"] == pytest.approx(FCT)


def test_mo_completed_since_the_rebuild_still_credits_its_made_share():
    """manprg now calls it complete, so the row is 'completed', not
    'made_part' — the old rule dropped it outright and the order read UNDER
    with a phantom holding card."""
    done = _row(kind="completed", made=FCT)
    assert keep_completed_row(done, {"30384"}, {"30384": RUNNING_ATTRS}) is False
    got = _credited([done], {"30384"}, {"30384": RUNNING_ATTRS})
    assert got["30384"] == pytest.approx(FROZEN_MADE)
    assert FROZEN_REMAINING + got["30384"] == pytest.approx(FCT)


def test_mo_not_on_the_board_keeps_its_live_made_kg():
    """A completed MO current_state hides: no block represents any of it."""
    got = _credited([_row(mo="30999", kind="completed", made=5000.0)],
                    {"30384"}, {"30384": RUNNING_ATTRS})
    assert got["30999"] == pytest.approx(5000.0)


def test_queued_block_at_full_fct_is_credited_nothing():
    """A queued MO's block carries the WHOLE Fct — crediting made kg on top
    would double count (the live board has 18 such blocks)."""
    assert _credited([_row()], {"30384"}, {"30384": QUEUED_ATTRS}) == {}


def test_pre_ca3_board_without_the_token_is_credited_nothing():
    assert _credited([_row()], {"30384"}, {"30384": "current_state:running"}) == {}


def test_zero_frozen_made_drops_the_row_rather_than_zeroing_it():
    """_supply_from_completed falls back to qty_kg when made_kg is 0, so a
    zero credit MUST drop the row or the whole Fct would be credited."""
    assert _credited([_row()], {"30384"}, {"30384": "fct_kg=1;made_kg=0"}) == {}


def test_without_board_attrs_the_legacy_filter_stands():
    rows = [_row(), _row(mo="30380", kind="completed")]
    got = _credited(rows, {"30384", "30380"}, None)
    assert got == {"30384": pytest.approx(FROZEN_MADE)}   # made_part kept, completed dropped


def _two_line_rows():
    """The shape current_state explicitly supports and warns about: one MO
    number running on two lines, one completed row per line."""
    return [
        {**_row(mo="30384", made=25000.0), "line": "P09", "mo_uid": "30384@P09"},
        {**_row(mo="30384", made=9000.0), "line": "P10", "mo_uid": "30384@P10"},
    ]


def test_one_mo_on_two_lines_is_credited_per_line_not_per_mo():
    """Each line has its OWN board block and its own frozen token, so each
    row takes its own. Crediting the MO-level value per row would have
    credited 20,000 kg twice for a 24,000 kg MO and pushed the surplus onto
    the SKU's later weeks."""
    attrs = {"30384": ";made_kg=20000;made_kg=4000",
             "30384@P09": "current_state:running;fct_kg=26001;made_kg=20000",
             "30384@P10": "current_state:running;fct_kg=10000;made_kg=4000"}
    got = reconcile_completed_to_board(_two_line_rows(), {"30384"}, attrs)
    assert [r["made_kg"] for r in got] == [20000.0, 4000.0]


def test_ambiguous_mo_blob_credits_nothing_rather_than_multiplying():
    """Without per-line keys, two DIFFERENT tokens under one MO cannot be
    attributed to a row. Under-crediting is survivable; double crediting
    silently marks demand as covered."""
    attrs = {"30384": ";made_kg=20000;made_kg=4000"}
    assert reconcile_completed_to_board(_two_line_rows(), {"30384"}, attrs) == []


def test_bare_mo_fallback_credits_at_most_one_row():
    """Identical tokens are indistinguishable too, so the MO is credited once."""
    attrs = {"30384": ";made_kg=20000;made_kg=20000"}
    got = reconcile_completed_to_board(_two_line_rows(), {"30384"}, attrs)
    assert [r["made_kg"] for r in got] == [20000.0]


def test_no_exclusions_is_a_passthrough():
    rows = [_row()]
    assert reconcile_completed_to_board(rows, set(), {"30384": RUNNING_ATTRS}) == rows
    assert reconcile_completed_to_board(None, {"30384"}, {}) == []


def test_reconciliation_never_mutates_the_caller_rows():
    live = _row(made=FROZEN_MADE + 1000.0)
    reconcile_completed_to_board([live], {"30384"}, {"30384": RUNNING_ATTRS})
    assert live["made_kg"] == pytest.approx(FROZEN_MADE + 1000.0)


# ---------------------------------------------------------------------------
# 2. Sunday production: keyed to the previous ISO week, then discarded
# ---------------------------------------------------------------------------

ANCHOR = datetime(2026, 9, 14)        # Monday, ISO week 38 — the demand W0
DEMAND = pd.DataFrame([{
    "order_id": "280104-W0", "sku": "280104", "week_index": 0,
    "qty_target": 40000.0, "lower_pct": 0.9, "upper_pct": 1.1,
    "due_start_hour": 0, "due_end_hour": 167, "priority": 3,
}])
# 12 h run starting Sunday 06:00 -> midpoint Sunday noon -> ISO week 37
SUNDAY_MADE = [{
    "mo": "30360", "kind": "completed", "item": "280104",
    "made_kg": 36000.0, "qty_kg": 36000.0, "hours": 12.0,
    "start_dt": pd.Timestamp("2026-09-13 06:00"),
}]


def _ledger(carry: bool):
    return build_ledger(DEMAND, None, completed=SUNDAY_MADE, anchor=ANCHOR,
                        demand_anchor=ANCHOR, carry_unsettled_past=carry)


def test_sunday_production_is_discarded_without_the_carry():
    led = _ledger(False)
    assert led.applied_by_order() == {}                 # the live 0% W0 card
    assert sum(led.unsettled_past.values()) == pytest.approx(36000.0)
    assert any(n.startswith("WARNING") for n in led.notes)


def test_sunday_production_settles_the_current_week_with_the_carry():
    led = _ledger(True)
    assert led.applied_by_order()["280104-W0"] == pytest.approx(36000.0)
    # The quarantine runs in BOTH modes now, so the kg is still RECORDED;
    # carrying moves it to carried_past and narrates it instead of warning.
    assert sum(led.carried_past.values()) == pytest.approx(36000.0)
    assert any(n.startswith("NOTE") for n in led.notes)
    assert not any(n.startswith("WARNING") for n in led.notes)


def test_carrying_never_silences_the_diagnostic():
    """Switching the carry on used to disable the ONLY producer of
    unsettled_past and its note, so the calendar surfaced nothing."""
    assert _ledger(True).unsettled_past      # recorded in both modes
    assert _ledger(False).unsettled_past


# 12 h run three weeks before the plan: too old to be this week's pre-build
OLD_MADE = [{**SUNDAY_MADE[0], "start_dt": pd.Timestamp("2026-08-24 06:00")}]


def test_production_older_than_the_week_before_is_never_carried():
    """The carry covers the pre-build of THIS plan, which keys to the week
    immediately before the anchor week. Older production most likely served
    its own week's demand, so it stays quarantined even when carrying is on
    (live 2026-09-15: only ~42% of the 139 t was a current-week pre-build)."""
    led = build_ledger(DEMAND, None, completed=OLD_MADE, anchor=ANCHOR,
                       demand_anchor=ANCHOR, carry_unsettled_past=True)
    assert led.applied_by_order() == {}
    assert led.carried_past == {}
    assert sum(led.unsettled_past.values()) == pytest.approx(36000.0)
    assert any(n.startswith("WARNING") for n in led.notes)


def test_carry_never_credits_more_than_the_order_asks_for():
    """Carry-forward must not turn a big pre-build into an OVER card: each
    order takes at most its gross target, the surplus stays in the carry."""
    big = [{**SUNDAY_MADE[0], "made_kg": 90000.0, "qty_kg": 90000.0}]
    led = build_ledger(DEMAND, None, completed=big, anchor=ANCHOR,
                       demand_anchor=ANCHOR, carry_unsettled_past=True)
    assert led.applied_by_order()["280104-W0"] == pytest.approx(40000.0)


def test_build_ledger_from_data_defaults_the_carry_to_the_made_only_path(
        monkeypatch, tmp_path):
    """The solver's netting must keep refusing to guess (fix C54); only the
    calendar's made-only credit carries past production forward."""
    from types import SimpleNamespace

    ref = tmp_path / "reference"
    ref.mkdir(parents=True)
    DEMAND.to_csv(ref / "demand_plan.csv", index=False)
    seen: dict = {}

    def _spy(demand, blocks, **kw):
        seen["carry"] = kw.get("carry_unsettled_past")
        return dc.CoverageLedger()

    monkeypatch.setattr(dc, "build_ledger", _spy)
    monkeypatch.setattr(dc, "demand_source_anchor", lambda dd: ANCHOR)
    state = SimpleNamespace(completed=[], blocks=None)

    dc.build_ledger_from_data(tmp_path, {}, state=state, made_only=True)
    assert seen["carry"] is True
    dc.build_ledger_from_data(tmp_path, {}, state=state, made_only=False)
    assert seen["carry"] is False
    dc.build_ledger_from_data(tmp_path, {}, state=state, made_only=True,
                              carry_unsettled_past=False)
    assert seen["carry"] is False


# ---------------------------------------------------------------------------
# 3. "Hide blocks that already finished" must not eat half of a split MO
# ---------------------------------------------------------------------------

BOARD_COLS = ["block_id", "block_type", "line_id", "line_name", "start_h",
              "end_h", "order_id", "sku", "qty_kg", "attrs"]


def _board(rows):
    return pd.DataFrame(rows, columns=BOARD_COLS)


def _blk(bid, oid, s, e, kg, attrs="", btype="production"):
    return [bid, btype, 0, "P09", s, e, oid, "280584", kg, attrs]


def test_split_running_mo_is_never_half_hidden():
    """The live shape: MO 30384 runs across a CIP, so its first piece ends
    at h1 while the MO itself runs to h22. At now = 3 h the old rule hid the
    finished piece and its 93 kg left the card — the credit could not grow
    to cover it, because the MO was still on the board."""
    from helpers.calendar_io import split_finished_rows
    cal = _board([
        _blk("cs_a", "30384", 0.0, 1.0, 93.08, RUNNING_ATTRS),
        _blk("cs_a", "30384", 7.0, 22.0, 1398.76, RUNNING_ATTRS),
    ])
    visible, hidden = split_finished_rows(cal, 3.0)
    assert len(hidden) == 0
    assert float(visible["qty_kg"].sum()) == pytest.approx(93.08 + 1398.76)
    # board kg + the frozen credit still reconstructs the MO's Fct. The board
    # stores kg rounded to 2 dp and the token to whole kg, so the identity
    # holds to within that rounding (here 0.16 kg), never exactly.
    assert float(visible["qty_kg"].sum()) + FROZEN_MADE == pytest.approx(FCT, abs=1.0)


def test_fully_finished_mo_still_disappears():
    """When every piece is done the block leaves the board and the credit
    map takes over the MO's whole made kg — the behaviour that was already
    right, and the reason the exception is per MO, not blanket."""
    from helpers.calendar_io import split_finished_rows
    cal = _board([
        _blk("cs_a", "30352", 0.0, 1.0, 93.08, RUNNING_ATTRS),
        _blk("cs_a", "30352", 7.0, 22.0, 1398.76, RUNNING_ATTRS),
    ])
    visible, hidden = split_finished_rows(cal, 30.0)
    assert len(visible) == 0 and len(hidden) == 2


def test_non_mo_blocks_are_still_hidden_row_by_row():
    """A planner/solver block carries no current_state token and no frozen
    credit, so the per-MO grouping must not keep it on the board."""
    from helpers.calendar_io import split_finished_rows
    cal = _board([
        _blk("b1", "280584-W0", 0.0, 5.0, 1000.0, ""),
        _blk("b2", "280584-W0", 10.0, 20.0, 2000.0, ""),
    ])
    visible, hidden = split_finished_rows(cal, 7.0)
    assert list(hidden["block_id"]) == ["b1"]
    assert list(visible["block_id"]) == ["b2"]


def test_split_finished_rows_handles_an_empty_board():
    from helpers.calendar_io import split_finished_rows
    visible, hidden = split_finished_rows(_board([]), 10.0)
    assert len(visible) == 0 and len(hidden) == 0


def test_finished_cip_blocks_still_hide_despite_a_later_clean():
    """CIP rows carry current_state: attrs and a BLANK order_id, so grouping
    on the order_id alone bucketed every clean on the board together and one
    future clean kept all the finished ones visible (live 2026-09-15: 55 CIP
    rows, 17 finished rows wrongly kept at hour 120). A blank id is not an
    identity."""
    from helpers.calendar_io import split_finished_rows
    cal = _board([
        _blk("cip1", "", 1.0, 7.0, 0.0, "current_state:cip_scheduled", "cip"),
        _blk("cip2", "", 100.0, 106.0, 0.0, "current_state:cip_projected", "cip"),
        _blk("p1", "280584-W0", 0.0, 5.0, 900.0, ""),
    ])
    visible, hidden = split_finished_rows(cal, 50.0)
    assert sorted(hidden["block_id"]) == ["cip1", "p1"]
    assert list(visible["block_id"]) == ["cip2"]


def test_blank_and_nan_order_ids_never_group_together():
    """load_calendar can leave an unnamed id as the string 'nan'; it must not
    become a shared key either."""
    from helpers.calendar_io import split_finished_rows
    cal = _board([
        _blk("a", "nan", 1.0, 5.0, 0.0, "current_state:cip_scheduled", "cip"),
        _blk("b", "None", 200.0, 206.0, 0.0, "current_state:cip_projected", "cip"),
    ])
    visible, hidden = split_finished_rows(cal, 50.0)
    assert list(hidden["block_id"]) == ["a"] and list(visible["block_id"]) == ["b"]
