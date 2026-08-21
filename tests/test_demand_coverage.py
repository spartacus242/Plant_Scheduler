# tests/test_demand_coverage.py — the SKU×week coverage ledger (one netting
# engine for Scenario F staging, Reconcile, and the holding cards).

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.demand_coverage import (  # noqa: E402
    COVERED,
    PARTIAL,
    UNCOVERED,
    apply_ledger,
    build_ledger,
    demand_week_grid,
    iso_week_key,
)

# 2026 ISO calendar facts used throughout: W33 = Aug 10–16, W34 = Aug 17–23,
# W35 = Aug 24–30 (matches the live dataset this slice was built against).
SUN_AUG_16 = datetime(2026, 8, 16)   # Sunday, W33
MON_AUG_17 = datetime(2026, 8, 17)   # Monday, W34
THU_AUG_20 = datetime(2026, 8, 20)   # Thursday, W34


def _blocks(rows):
    base = {"block_id": "b", "block_type": "production", "line_id": 0,
            "line_name": "P09", "start_h": 0.0, "end_h": 10.0, "label": "",
            "order_id": "MO1", "sku": "111", "sku_description": "",
            "qty_kg": None, "locked": False, "attrs": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


def _demand(rows):
    base = {"order_id": "X-W0", "sku": "X", "week_index": 0,
            "qty_target": 10000, "lower_pct": 0.9, "upper_pct": 1.1,
            "due_start_hour": 0, "due_end_hour": 167, "priority": 3}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_iso_week_key_sorts_across_years():
    assert iso_week_key(datetime(2026, 8, 12)) == 202633
    assert iso_week_key(datetime(2026, 12, 30)) == 202653
    assert iso_week_key(datetime(2027, 1, 6)) == 202701
    assert 202653 < 202701


# ── ISO keying: the frame-mismatch regression ─────────────────────────────

def test_stale_week_index_does_not_break_iso_netting():
    """THE live bug (2026-08-16): demand file anchored at W32, staging frame
    anchored today — week_index said 2 while the bucket grid said 1, so the
    old keying netted NOTHING. ISO keying via the due window midpoint and
    the block midpoint must agree regardless of week_index."""
    dem = _demand([
        # W34 in a Sunday-Aug-16 staging frame: [24, 191]
        {"order_id": "280480-W2", "sku": "280480", "week_index": 2,
         "qty_target": 397000, "due_start_hour": 24, "due_end_hour": 191},
    ])
    blocks = _blocks([{"sku": "280480", "qty_kg": 88000.0,
                       "start_h": 26.0, "end_h": 100.0}])  # mid Aug 18 → W34
    ledger = build_ledger(dem, blocks, anchor=SUN_AUG_16)
    assert len(ledger.rows) == 1
    row = ledger.rows[0]
    assert row.week_label == "WW34"
    assert row.applied_kg == 88000.0
    assert row.net_kg == 309000.0
    assert row.status == PARTIAL
    out, notes = apply_ledger(dem, ledger)
    assert out.iloc[0]["qty_target"] == 309000.0
    assert len(notes) == 1 and "88,000 kg" in notes[0]


def test_surplus_carries_forward_by_true_iso_week():
    dem = _demand([
        {"order_id": "S-W34", "sku": "S", "qty_target": 50000,
         "due_start_hour": 24, "due_end_hour": 191},
        {"order_id": "S-W35", "sku": "S", "qty_target": 30000,
         "due_start_hour": 192, "due_end_hour": 359},
    ])
    blocks = _blocks([{"sku": "S", "qty_kg": 70000.0,
                       "start_h": 30.0, "end_h": 90.0}])   # W34
    ledger = build_ledger(dem, blocks, anchor=SUN_AUG_16)
    by = {r.order_id: r for r in ledger.rows}
    assert by["S-W34"].status == COVERED and by["S-W34"].net_kg == 0.0
    assert by["S-W35"].carry_in_kg == 20000.0
    assert by["S-W35"].applied_kg == 20000.0
    assert by["S-W35"].net_kg == 10000.0
    # and never backward: a W35 block leaves W34 untouched
    blocks_late = _blocks([{"sku": "S", "qty_kg": 70000.0,
                            "start_h": 200.0, "end_h": 260.0}])  # W35
    led2 = build_ledger(dem, blocks_late, anchor=SUN_AUG_16)
    by2 = {r.order_id: r for r in led2.rows}
    assert by2["S-W34"].status == UNCOVERED and by2["S-W34"].applied_kg == 0.0
    assert by2["S-W35"].status == COVERED


# ── pro-rating: a block straddling the Monday boundary splits by overlap ──

def test_boundary_straddling_block_prorates_by_overlap_share():
    """THE live bug (2026-08-21): the midpoint rule bucketed a committed
    block's ENTIRE kg into one week, so the MO re-forecast stretching blocks
    flipped whole tonnages across the Monday boundary — W35 read 1.91M kg
    of committed credit against ~1.1M kg of weekly plant capacity. A block
    must credit each week by TIME-OVERLAP share instead.

    Sunday-Aug-16 anchor: W34 starts at hour 24, W35 at hour 192. Block
    [4, 104] runs 20h in W33 and 80h in W34 -> 20%/80% of its 10 t."""
    dem = _demand([
        {"order_id": "P-W33", "sku": "P", "qty_target": 5000,
         "due_start_hour": 0, "due_end_hour": 23},
        {"order_id": "P-W34", "sku": "P", "qty_target": 8000,
         "due_start_hour": 24, "due_end_hour": 191},
    ])
    blocks = _blocks([{"sku": "P", "qty_kg": 10000.0,
                       "start_h": 4.0, "end_h": 104.0}])
    led = build_ledger(dem, blocks, anchor=SUN_AUG_16)
    by = {r.order_id: r for r in led.rows}
    assert by["P-W33"].committed_kg == 2000.0    # 20/100 x 10000
    assert by["P-W33"].applied_kg == 2000.0
    assert by["P-W33"].net_kg == 3000.0
    assert by["P-W33"].status == PARTIAL
    assert by["P-W34"].committed_kg == 8000.0    # 80/100 x 10000
    assert by["P-W34"].status == COVERED
    # shares always sum to the block's full kg — pro-rating re-buckets,
    # never invents or loses tonnage
    assert sum(r.committed_kg for r in led.rows) == 10000.0


def test_block_inside_one_week_is_not_split():
    """Pro-rating only touches boundary-straddlers — a block wholly inside
    a week credits exactly as before (midpoint and overlap agree)."""
    dem = _demand([{"order_id": "Q-W34", "sku": "Q", "qty_target": 9000,
                    "due_start_hour": 24, "due_end_hour": 191}])
    blocks = _blocks([{"sku": "Q", "qty_kg": 7000.0,
                       "start_h": 24.0, "end_h": 100.0}])  # W34 edge-to-mid
    led = build_ledger(dem, blocks, anchor=SUN_AUG_16)
    assert led.rows[0].committed_kg == 7000.0
    assert led.rows[0].applied_kg == 7000.0


# ── completed-MO actuals (the Thursday trap) ──────────────────────────────

def test_completed_mo_actuals_net_demand_midweek():
    """Thursday of W34: an MO that FINISHED Monday is gone from the calendar
    (current_state drops completed rows), but its made kg must still credit
    W34 demand or the solver re-plans tonnage already in the warehouse."""
    dem = _demand([
        # W34 remainder in a Thursday-anchored frame: [0, 95]
        {"order_id": "111-W34", "sku": "111", "qty_target": 60000,
         "due_start_hour": 0, "due_end_hour": 95},
    ])
    completed = [{
        "item": "111", "mo": "29900", "start_dt": pd.Timestamp(2026, 8, 17, 6),
        "hours": 20.0, "qty_kg": 55000.0, "made_kg": 50000.0,
    }]
    ledger = build_ledger(dem, _blocks([]), completed=completed,
                          anchor=THU_AUG_20)
    row = ledger.rows[0]
    assert row.produced_kg == 50000.0
    assert row.applied_kg == 50000.0 and row.net_kg == 10000.0
    assert ledger.produced_total_kg == 50000.0
    assert any("already-made" in n for n in ledger.notes)


def test_completed_made_kg_wins_over_fct_kg():
    """A superseded MO never finishes its Fct qty — made_kg is the truth.
    Only a truly blank made column falls back to Fct kg."""
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 90000,
                    "due_start_hour": 0, "due_end_hour": 95}])
    completed = [
        {"item": "111", "start_dt": pd.Timestamp(2026, 8, 17, 6),
         "hours": 10.0, "qty_kg": 40000.0, "made_kg": 15000.0},   # superseded
        {"item": "111", "start_dt": pd.Timestamp(2026, 8, 18, 6),
         "hours": 10.0, "qty_kg": 20000.0, "made_kg": 0.0},       # blank made
    ]
    ledger = build_ledger(dem, _blocks([]), completed=completed,
                          anchor=THU_AUG_20)
    assert ledger.rows[0].produced_kg == 35000.0   # 15k actual + 20k fct


def test_completed_beyond_lookback_ignored():
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 60000,
                    "due_start_hour": 0, "due_end_hour": 95}])
    ancient = [{"item": "111", "start_dt": pd.Timestamp(2026, 6, 1, 6),
                "hours": 10.0, "qty_kg": 50000.0, "made_kg": 50000.0}]
    ledger = build_ledger(dem, _blocks([]), completed=ancient,
                          anchor=THU_AUG_20, lookback_weeks=4)
    assert ledger.rows[0].applied_kg == 0.0
    assert ledger.produced_total_kg == 0.0


def test_completed_trials_and_cip_never_credit():
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 60000,
                    "due_start_hour": 0, "due_end_hour": 95}])
    completed = [
        {"item": "TRIALS", "start_dt": pd.Timestamp(2026, 8, 17, 6),
         "hours": 24.0, "qty_kg": 18000.0, "made_kg": 18000.0},
        {"item": "CIP", "start_dt": pd.Timestamp(2026, 8, 17, 6),
         "hours": 6.0, "qty_kg": 4500.0, "made_kg": 4500.0},
    ]
    ledger = build_ledger(dem, _blocks([]), completed=completed,
                          anchor=THU_AUG_20)
    assert ledger.rows[0].applied_kg == 0.0


# ── history: past production settles against past demand first ────────────

def test_history_demand_absorbs_past_production():
    """A pre-build MO completed in W33 must NOT credit W34 unless W33's own
    (dropped-from-staging) demand is satisfied first — the reference demand
    file supplies that history."""
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 50000,
                    "due_start_hour": 0, "due_end_hour": 167}])
    completed = [{"item": "111", "start_dt": pd.Timestamp(2026, 8, 12, 6),
                  "hours": 10.0, "qty_kg": 80000.0, "made_kg": 80000.0}]  # W33
    # without history: all 80k rolls into W34
    free = build_ledger(dem, _blocks([]), completed=completed,
                        anchor=MON_AUG_17)
    assert free.rows[0].applied_kg == 50000.0
    # with W33 history demand of 60k: only the 20k true surplus carries
    hist = {("111", 202633): 60000.0}
    led = build_ledger(dem, _blocks([]), completed=completed,
                       anchor=MON_AUG_17, history_demand=hist)
    assert led.rows[0].carry_in_kg == 20000.0
    assert led.rows[0].applied_kg == 20000.0
    assert led.rows[0].net_kg == 30000.0


def test_history_colliding_with_live_week_is_skipped():
    """History is only for weeks the live frame no longer carries — a
    colliding entry would double-charge the demand."""
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 50000,
                    "due_start_hour": 0, "due_end_hour": 167}])
    blocks = _blocks([{"sku": "111", "qty_kg": 50000.0,
                       "start_h": 20.0, "end_h": 60.0}])   # W34
    hist = {("111", 202634): 50000.0}   # same week as the live row
    led = build_ledger(dem, blocks, anchor=MON_AUG_17, history_demand=hist)
    assert led.rows[0].applied_kg == 50000.0   # history did not eat it


# ── murky-case flags ──────────────────────────────────────────────────────

def test_overcommitted_sku_is_flagged_not_guessed():
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 100000,
                    "due_start_hour": 0, "due_end_hour": 167}])
    blocks = _blocks([{"sku": "111", "qty_kg": 200000.0,
                       "start_h": 20.0, "end_h": 60.0}])
    led = build_ledger(dem, blocks, anchor=MON_AUG_17)
    assert led.rows[0].status == COVERED
    assert "111" in led.overcommitted
    flag = led.overcommitted["111"]
    assert flag["surplus_kg"] == 100000.0
    assert flag["last_week_label"] == "WW34"


def test_prebuild_within_tolerance_is_not_flagged():
    """The live 280480 shape: 731 t committed vs 738 t cumulative demand
    W34–W36 — pre-building future weeks is the plant's normal pattern."""
    dem = _demand([
        {"order_id": "280480-W34", "sku": "280480", "qty_target": 397000,
         "due_start_hour": 0, "due_end_hour": 167},
        {"order_id": "280480-W35", "sku": "280480", "qty_target": 76938,
         "due_start_hour": 168, "due_end_hour": 335},
        {"order_id": "280480-W36", "sku": "280480", "qty_target": 264251,
         "due_start_hour": 336, "due_end_hour": 503},
    ])
    blocks = _blocks([{"sku": "280480", "qty_kg": 731000.0,
                       "start_h": 20.0, "end_h": 160.0}])
    led = build_ledger(dem, blocks, anchor=MON_AUG_17)
    by = {r.order_id: r for r in led.rows}
    assert by["280480-W34"].status == COVERED
    assert by["280480-W35"].status == COVERED
    assert by["280480-W36"].status == PARTIAL
    assert by["280480-W36"].net_kg == 7189.0
    assert "280480" not in led.overcommitted


def test_committed_sku_missing_from_demand_plan_is_flagged():
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 10000,
                    "due_start_hour": 0, "due_end_hour": 167}])
    blocks = _blocks([{"sku": "999", "qty_kg": 5000.0,
                       "start_h": 20.0, "end_h": 60.0}])
    led = build_ledger(dem, blocks, anchor=MON_AUG_17)
    assert led.no_demand == {"999": 5000.0}


def test_unknown_kg_blocks_counted_and_noted():
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 10000,
                    "due_start_hour": 0, "due_end_hour": 167}])
    blocks = _blocks([{"sku": "111", "qty_kg": None,
                       "start_h": 20.0, "end_h": 60.0}])
    led = build_ledger(dem, blocks, anchor=MON_AUG_17)
    assert led.unknown_kg_blocks == 1
    assert led.rows[0].applied_kg == 0.0
    assert any("unknown kg" in n for n in led.notes)


# ── anchorless fallbacks (legacy call sites) ──────────────────────────────

def test_week_index_fallback_without_due_columns():
    """The pinned-blocks call site passes demand with no due windows —
    week_index keys the row, week_bounds key the block. The block [300, 340]
    straddles the 336h bound: 36/40 of its kg (4500) lands in bucket 1, the
    500 kg beyond the demand file carries forward and credits nothing."""
    dem = pd.DataFrame([{"order_id": "O2", "sku": "S2", "week_index": 1,
                         "qty_target": 6000.0, "lower_pct": 0.9,
                         "upper_pct": 1.1}])
    blocks = _blocks([{"sku": "S2", "qty_kg": 5000.0,
                       "start_h": 300.0, "end_h": 340.0}])
    led = build_ledger(dem, blocks, week_bounds=[0.0, 168.0, 336.0])
    assert led.rows[0].applied_kg == 4500.0
    assert led.rows[0].week_label == "W+1"


def test_apply_ledger_int64_targets_and_bounds_survive():
    dem = _demand([{"order_id": "111-W34", "sku": "111", "qty_target": 50000,
                    "due_start_hour": 24, "due_end_hour": 191}])
    assert str(dem["qty_target"].dtype).startswith("int")
    blocks = _blocks([{"sku": "111", "qty_kg": 12345.6,
                       "start_h": 30.0, "end_h": 60.0}])
    led = build_ledger(dem, blocks, anchor=SUN_AUG_16)
    out, notes = apply_ledger(dem, led)
    assert out.iloc[0]["qty_target"] == 37654.4
    assert out.iloc[0]["lower_pct"] == 0.9 and out.iloc[0]["upper_pct"] == 1.1
    assert "->" in notes[0]


def test_demand_week_grid_uses_due_midpoints():
    dem = _demand([
        {"order_id": "A-W33", "sku": "A", "qty_target": 1000,
         "due_start_hour": 0, "due_end_hour": 167},
        {"order_id": "A-W34", "sku": "A", "qty_target": 2000,
         "due_start_hour": 168, "due_end_hour": 335},
    ])
    grid = demand_week_grid(dem, anchor=datetime(2026, 8, 10))  # Monday W33
    assert grid == {("A", 202633): 1000.0, ("A", 202634): 2000.0}


def test_empty_demand_returns_empty_ledger():
    led = build_ledger(pd.DataFrame(), _blocks([{"sku": "1", "qty_kg": 10.0}]),
                       anchor=MON_AUG_17)
    assert led.rows == [] and led.no_demand == {}


# ── downstream consumers: Reconcile findings + holding cards ──────────────

def _live_shaped_ledger():
    dem = _demand([
        {"order_id": "280480-W34", "sku": "280480", "qty_target": 397000,
         "due_start_hour": 24, "due_end_hour": 191},
        {"order_id": "280490-W34", "sku": "280490", "qty_target": 34748,
         "due_start_hour": 24, "due_end_hour": 191},
    ])
    blocks = _blocks([
        {"sku": "280480", "qty_kg": 731000.0, "start_h": 26, "end_h": 160},
        {"sku": "570468", "qty_kg": 15048.0, "start_h": 30, "end_h": 50},
    ])
    return dem, build_ledger(dem, blocks, anchor=SUN_AUG_16)


def test_reconcile_findings_from_ledger():
    from helpers.reconcile_engine import COVERAGE, INFO, WARN, \
        demand_ledger_findings

    _, led = _live_shaped_ledger()
    finds = demand_ledger_findings(led)
    by_key = {f.key: f for f in finds}
    cov = by_key["coverage_committed"]
    assert cov.category == COVERAGE and cov.severity == INFO
    assert "280480-W34" in cov.detail
    over = by_key["coverage_overcommitted"]      # 731 t vs 397 t on file
    assert over.severity == WARN and "280480" in over.detail
    nodem = by_key["coverage_no_demand"]
    assert nodem.severity == INFO and "570468" in nodem.detail
    assert demand_ledger_findings(None) == []


def test_holding_cards_credit_committed_kg():
    """The 280480-W34 screenshot: gross demand card sat in holding even
    though committed MOs covered the week. Covered kg now credits the card
    exactly like solver-produced kg."""
    from helpers.holding_builder import build_holding

    dem, led = _live_shaped_ledger()
    produced = pd.DataFrame([
        {"order_id": "280480-W34", "sku": "280480", "qty_min": 357300.0,
         "qty_max": 436700.0, "produced": 0.0},
        {"order_id": "280490-W34", "sku": "280490", "qty_min": 31273.2,
         "qty_max": 38222.8, "produced": 0.0},
    ])
    gross = build_holding(dem, produced, rates={"280480": 1200.0,
                                                "280490": 1200.0})
    assert {b.order_id for b in gross} == {"280480-W34", "280490-W34"}
    credited = build_holding(dem, produced, rates={"280480": 1200.0,
                                                   "280490": 1200.0},
                             committed_by_order=led.applied_by_order())
    assert {b.order_id for b in credited} == {"280490-W34"}   # 280480 covered
