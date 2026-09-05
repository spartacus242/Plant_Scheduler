# tests/test_overnight_score.py — overnight_score v2 (frozen composite).
#
# Every subscore is asserted against a HAND-COMPUTED fixture: the formula is
# frozen per version, so these numbers must never drift. A change in
# expectation here means a version bump, not an edit. v1 -> v2 (2026-09-03,
# fix Q): changeover rule aligned with the scorecard - CIP-in-gap waiver and
# a recipe-only (1.0) missing-pair default; see test_transition_cost_* and
# tests/test_fix_Q.py.

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from helpers.overnight_score import (CAMPAIGN_CAP_H,  # noqa: E402
                                     CO_LOAD_CAP_PER_100H,
                                     OVERNIGHT_SCORE_VERSION,
                                     campaign_runs, campaign_subscore,
                                     capacity_bound_kg, changeover_subscore,
                                     demand_orders, fill_subscore,
                                     iso_week_marks, order_target,
                                     overnight_score, solver_fill_blocks,
                                     transition_cost, weighted_co_load,
                                     _normalize)

ANCHOR = datetime(2026, 8, 17)  # a Monday — clean ISO week marks


def _cal(rows: list[dict]) -> pd.DataFrame:
    base = {"block_id": "b", "block_type": "production", "line_id": "1",
            "line_name": "P09", "start_h": 0.0, "end_h": 1.0, "label": "",
            "order_id": "", "sku": "", "sku_description": "", "qty_kg": None,
            "locked": False, "attrs": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


def _demand(rows: list[dict]) -> pd.DataFrame:
    base = {"order_id": "", "sku": "", "qty_target": 0.0, "lower_pct": 0.9,
            "upper_pct": 1.1, "due_start_hour": 0, "due_end_hour": 167,
            "week_index": 0}
    return pd.DataFrame([{**base, **r} for r in rows])


# ── order target: the weekly-fulfillment capping convention ───────────────

def test_order_target_midpoint_and_fallbacks():
    assert order_target(900, 1100) == 1000.0          # midpoint
    assert order_target(0, 1100) == 1100.0            # no floor -> max
    assert order_target(900, 0) == 900.0              # unbounded max -> min
    assert order_target(1100, 900) == 1100.0          # inverted band -> max


def test_demand_orders_derives_bounds_from_pcts():
    dem = _demand([{"order_id": "O1", "sku": "111", "qty_target": 1000}])
    orders = demand_orders(dem)
    # qty_min=900, qty_max=1100 -> target 1000; due mid = 83.5h
    assert orders[0]["target"] == pytest.approx(1000.0)
    assert orders[0]["due_mid_h"] == pytest.approx(83.5)


# ── fill: capped credit / min(net demand, capacity bound) ─────────────────

def test_fill_capped_credit_and_capacity_bound_denominator():
    orders = [
        {"order_id": "O1", "sku": "111", "target": 1000.0, "due_mid_h": 80.0},
        {"order_id": "O2", "sku": "222", "target": 1000.0, "due_mid_h": 80.0},
    ]
    # O1 overshoots (credit caps at 1000), O2 places 500 -> credit 1500.
    placed = {"O1": 1400.0, "O2": 500.0}
    # net demand 2000 but the windows only hold 1500 -> denominator 1500.
    score, det = fill_subscore(placed, orders, capacity_bound=1500.0)
    assert score == pytest.approx(100.0)  # 1500 / 1500
    assert det["denominator_kg"] == 1500.0
    # unconstrained capacity -> denominator is net demand
    score2, _ = fill_subscore(placed, orders, capacity_bound=99999.0)
    assert score2 == pytest.approx(75.0)  # 1500 / 2000


def test_fill_no_demand_scores_100():
    score, _ = fill_subscore({}, [], capacity_bound=1000.0)
    assert score == 100.0


# ── changeovers: frozen scoring weights, per-100h cap ──────────────────────

def test_transition_cost_uses_scoring_weights_not_solver_weights():
    flags = {"ffs_change": 1, "topload_change": 1, "conv_to_org_change": 0,
             "casepacker_change": 1, "cinn_to_non": 0, "ttp_change": 1,
             "added_flavors": 2}
    # 10 + 8 + 4 + 1 + 0.5*2 = 24
    assert transition_cost(flags) == pytest.approx(24.0)
    # v2 (fix Q / C30, changeover-2, 2026-09-03): the missing-pair default is
    # the SCORECARD's - a pair with no standards row, or a row with no flag
    # set, is a recipe-only change costing CO_SCORE_RECIPE_ONLY_WEIGHT (1.0),
    # never 0. v1 priced an unknown pair at 0, so a plan built from SKUs the
    # standards file does not know read as changeover-free. Flavor removal
    # is still clamped (never a reward) - it now falls to the recipe-only
    # floor instead of 0.
    assert transition_cost({"added_flavors": -3}) == 1.0
    assert transition_cost(None) == 1.0
    assert transition_cost({"ffs_change": 0, "ttp_change": 0}) == 1.0


def test_weighted_co_load_counts_incoming_fill_pairs_only():
    cal = _normalize(_cal([
        # committed tail (order not in demand) then two fill blocks
        {"order_id": "MO1", "sku": "AAA", "start_h": 0, "end_h": 10},
        {"order_id": "O1", "sku": "BBB", "start_h": 10, "end_h": 20},
        {"order_id": "O2", "sku": "CCC", "start_h": 20, "end_h": 30},
        # second line: committed -> committed pair must NOT count
        {"order_id": "MO2", "sku": "AAA", "line_name": "P10",
         "start_h": 0, "end_h": 10},
        {"order_id": "MO3", "sku": "BBB", "line_name": "P10",
         "start_h": 10, "end_h": 20},
    ]))
    fill = solver_fill_blocks(cal, {"O1", "O2"})
    co_map = {("AAA", "BBB"): {"ffs_change": 1},           # cost 10
              ("BBB", "CCC"): {"topload_change": 1,
                               "ttp_change": 1}}           # cost 9
    load, transitions = weighted_co_load(cal, fill.index, co_map)
    assert transitions == 2  # committed->fill boundary + fill->fill
    assert load == pytest.approx(19.0)


def test_changeover_subscore_cap():
    # 20 h placed, load 8 -> 40 per 100h -> 1 - 40/80 = 50
    score, det = changeover_subscore(8.0, 20.0)
    assert det["co_load_per_100h"] == pytest.approx(40.0)
    assert score == pytest.approx(50.0)
    # beyond the cap clamps to 0
    score2, _ = changeover_subscore(CO_LOAD_CAP_PER_100H, 100.0)
    assert score2 == 0.0
    # zero transitions on placed production = perfect
    score3, _ = changeover_subscore(0.0, 50.0)
    assert score3 == 100.0


# ── campaign: same-SKU runs vs the fixed 40h cap ───────────────────────────

def test_campaign_runs_coalesce_same_sku_across_gaps():
    cal = _normalize(_cal([
        {"order_id": "O1", "sku": "AAA", "start_h": 0, "end_h": 10},
        # projected CIP splits the run — still ONE campaign
        {"order_id": "O1", "sku": "AAA", "start_h": 16, "end_h": 26},
        {"order_id": "O2", "sku": "BBB", "start_h": 26, "end_h": 36},
        {"order_id": "O3", "sku": "AAA", "line_name": "P10",
         "start_h": 0, "end_h": 20},
    ]))
    fill = solver_fill_blocks(cal, {"O1", "O2", "O3"})
    runs = sorted(campaign_runs(fill))
    assert runs == [10.0, 20.0, 20.0]
    # avg 50/3 h -> clamp(16.667/40)*100 = 41.67
    score, det = campaign_subscore(runs)
    assert score == pytest.approx(100.0 * (50.0 / 3) / CAMPAIGN_CAP_H,
                                  abs=0.01)
    assert det["campaigns"] == 3


def test_campaign_cap_saturates_at_100():
    score, _ = campaign_subscore([80.0, 40.0])
    assert score == 100.0


# ── on_time: kg in the demanded ISO week, capped, kg-weighted ──────────────

def test_on_time_kg_weighted_and_capped():
    marks = iso_week_marks(ANCHOR, 504.0)
    assert [m for m, _ in marks] == [0.0, 168.0, 336.0]
    dem = _demand([
        {"order_id": "O1", "sku": "111", "qty_target": 1000,
         "due_start_hour": 0, "due_end_hour": 167},       # week 0
        {"order_id": "O2", "sku": "222", "qty_target": 3000,
         "due_start_hour": 168, "due_end_hour": 335},     # week 1
    ])
    cal = _normalize(_cal([
        # O1: 1200 kg in week 0 -> credit caps at 1000
        {"order_id": "O1", "sku": "111", "start_h": 0, "end_h": 12,
         "qty_kg": 1200},
        # O2: 2000 kg in week 1 (counts) + 1000 kg in week 0 (wrong week)
        {"order_id": "O2", "sku": "222", "start_h": 200, "end_h": 220,
         "qty_kg": 2000},
        {"order_id": "O2", "sku": "222", "start_h": 20, "end_h": 30,
         "qty_kg": 1000},
    ]))
    score, det = overnight_score(
        cal, dem, {}, capacity_bound=None, week_marks=marks, horizon_h=504.0)
    # credits: O1 1000 + O2 2000 = 3000; targets 1000 + 3000 = 4000 -> 75
    assert score["on_time"] == pytest.approx(75.0)
    assert det["on_time_kg"] == pytest.approx(3000.0)


def test_on_time_ignores_orders_due_beyond_horizon():
    marks = iso_week_marks(ANCHOR, 336.0)
    dem = _demand([
        {"order_id": "O1", "sku": "111", "qty_target": 1000},
        {"order_id": "OX", "sku": "222", "qty_target": 5000,
         "due_start_hour": 500, "due_end_hour": 600},  # beyond horizon
    ])
    cal = _normalize(_cal([
        {"order_id": "O1", "sku": "111", "start_h": 0, "end_h": 10,
         "qty_kg": 1000},
    ]))
    score, _ = overnight_score(
        cal, dem, {}, capacity_bound=None, week_marks=marks, horizon_h=336.0)
    assert score["on_time"] == pytest.approx(100.0)


# ── composite: 40/30/15/15, version stamped, edge rules ────────────────────

def test_composite_weights_and_version():
    marks = iso_week_marks(ANCHOR, 336.0)
    dem = _demand([{"order_id": "O1", "sku": "111", "qty_target": 1000}])
    cal = _normalize(_cal([
        {"order_id": "O1", "sku": "111", "start_h": 0, "end_h": 40,
         "qty_kg": 1000},
    ]))
    score, _ = overnight_score(
        cal, dem, {}, capacity_bound=None, week_marks=marks, horizon_h=336.0)
    # fill 100 (1000/1000), co 100 (no transitions), campaign 100 (40h run),
    # on_time 100 -> composite exactly 100
    assert score == {
        "version": OVERNIGHT_SCORE_VERSION, "composite": 100.0,
        "fill": 100.0, "changeovers": 100.0, "campaign": 100.0,
        "on_time": 100.0,
    }


def test_composite_is_weighted_sum():
    marks = iso_week_marks(ANCHOR, 336.0)
    dem = _demand([
        {"order_id": "O1", "sku": "111", "qty_target": 2000},
    ])
    co_map = {("111", "222"): {"ffs_change": 1},
              ("222", "111"): {"ffs_change": 1}}
    # 20h placed, 1000 of 2000 kg, one ffs transition inside the fill,
    # 2 campaigns of 10h, all kg in the right week.
    cal = _normalize(_cal([
        {"order_id": "O1", "sku": "111", "start_h": 0, "end_h": 10,
         "qty_kg": 500},
        {"order_id": "OX", "sku": "222", "start_h": 10, "end_h": 20,
         "qty_kg": 100},   # not a demand order -> not solver-placed
        {"order_id": "O1", "sku": "111", "start_h": 20, "end_h": 30,
         "qty_kg": 500},
    ]))
    score, _ = overnight_score(
        cal, dem, co_map, capacity_bound=None, week_marks=marks,
        horizon_h=336.0)
    # hand-computed: fill = 1000/2000 = 50. Placed h = 20. Transitions with
    # incoming fill blocks: 222->111 (ffs, 10) and 111->222 is NOT counted
    # (incoming OX not fill). load 10 -> 50/100h -> 1 - 50/80 = 37.5.
    # campaigns: 111 run split by the OX block -> the two O1 blocks are
    # separate? No: campaign_runs walks FILL blocks only, both are sku 111
    # and consecutive in the fill sequence -> ONE 20h campaign -> 50.
    # on_time: 1000/2000 = 50.
    assert score["fill"] == pytest.approx(50.0)
    assert score["changeovers"] == pytest.approx(37.5)
    assert score["campaign"] == pytest.approx(50.0)
    assert score["on_time"] == pytest.approx(50.0)
    expected = 0.40 * 50 + 0.30 * 37.5 + 0.15 * 50 + 0.15 * 50
    assert score["composite"] == pytest.approx(round(expected, 2))


def test_empty_fill_with_demand_scores_zero_everywhere():
    marks = iso_week_marks(ANCHOR, 336.0)
    dem = _demand([{"order_id": "O1", "sku": "111", "qty_target": 1000}])
    cal = _normalize(_cal([
        {"order_id": "MO9", "sku": "999", "start_h": 0, "end_h": 30,
         "qty_kg": 4000},  # committed only — no solver-placed block
    ]))
    score, _ = overnight_score(
        cal, dem, {}, capacity_bound=None, week_marks=marks, horizon_h=336.0)
    assert score["composite"] == 0.0
    assert (score["fill"], score["changeovers"], score["campaign"],
            score["on_time"]) == (0.0, 0.0, 0.0, 0.0)


def test_pinned_and_current_state_blocks_are_not_solver_placed():
    cal = _normalize(_cal([
        {"order_id": "O1", "sku": "111", "attrs": "pinned",
         "start_h": 0, "end_h": 10, "qty_kg": 999},
        {"order_id": "O1", "sku": "111", "attrs": "current_state:running",
         "start_h": 10, "end_h": 20, "qty_kg": 999},
        {"order_id": "O1", "sku": "111", "attrs": "unpinned",
         "start_h": 20, "end_h": 30, "qty_kg": 111},
    ]))
    fill = solver_fill_blocks(cal, {"O1"})
    # exact-token match: 'unpinned' is NOT 'pinned'
    assert len(fill) == 1
    assert float(fill.iloc[0]["qty_kg"]) == 111.0


# ── capacity bound: free fill hours x flat rate, CIP windows deducted ──────

def test_capacity_bound_hand_computed():
    gates = {"P09": 24.0, "P10": 0.0, "P11": 0.0}
    blocked = {
        "P09": [(48.0, 54.0)],            # a projected CIP: 6h off
        "P10": [(0.0, 100.0)],            # committed through hour 100
    }
    rates = {"P09": 100.0, "P10": 200.0, "P11": 50.0, "P12": 500.0}
    capable = {"P09": {"111"}, "P10": {"222"}, "P11": {"999"},
               "P12": {"111"}}
    demand_skus = {"111", "222"}
    bound, det = capacity_bound_kg(
        gates, blocked, rates, capable, demand_skus, horizon_h=168.0)
    # P09: (168-24) - 6 = 138h x 100 = 13,800
    # P10: 68h x 200 = 13,600
    # P11: capable only of a SKU nobody demands -> excluded
    # P12: no gate entry -> gate 0, 168h x 500 = 84,000
    assert det["per_line"]["P09"]["free_h"] == pytest.approx(138.0)
    assert det["per_line"]["P10"]["free_h"] == pytest.approx(68.0)
    assert "P11" not in det["per_line"]
    assert bound == pytest.approx(13800.0 + 13600.0 + 84000.0)


def test_capacity_bound_drops_sub_hour_segments():
    # free_segments drops fragments under 1h — a 30-minute sliver holds 0 kg
    bound, det = capacity_bound_kg(
        {"P09": 0.0}, {"P09": [(0.5, 168.0)]}, {"P09": 1000.0},
        {"P09": {"111"}}, {"111"}, horizon_h=168.0)
    assert bound == 0.0
