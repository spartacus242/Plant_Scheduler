# tests/test_stock_policy_solver.py — slice 3: the stock policy's
# earliest-start floor (demand_plan.csv `earliest_start_hour`) through the
# loader, the CP-SAT model, the window-capacity clamp, the greedy seed and
# the independent validator.

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ortools.sat.python import cp_model  # noqa: E402

from data_loader import Data, Files, Params  # noqa: E402
from model_builder import (  # noqa: E402
    _producible_kg_in_window, build_model, effective_due_start)

from test_fix_SA import blocks, f_params, mk_data, order, status_of  # noqa: E402


# ---------------------------------------------------------------------------
# data_loader
# ---------------------------------------------------------------------------
def _demand_df(rows):
    return pd.DataFrame(rows, columns=["order_id", "sku", "week_index",
                                       "qty_target", "lower_pct", "upper_pct",
                                       "due_start_hour", "due_end_hour",
                                       "priority", "earliest_start_hour"])


def test_loader_reads_the_floor_rounded_up_and_blank_as_none():
    d = mk_data(f_params(), [0], ["X"], [])
    out = d._parse_demand(_demand_df([
        ["A-W0", "X", 0, 1000.0, 0.9, 1.1, 0, 167, 3, 100.4],
        ["B-W0", "X", 0, 1000.0, 0.9, 1.1, 0, 167, 3, float("nan")],
        ["C-W0", "X", 0, 1000.0, 0.9, 1.1, 0, 167, 3, -5.0],
    ]))
    assert out[0]["earliest_start"] == 101        # ceil: never crossed
    assert out[1]["earliest_start"] is None
    assert out[2]["earliest_start"] == 0          # clamped


def test_loader_without_the_column_gives_none():
    d = mk_data(f_params(), [0], ["X"], [])
    df = _demand_df([["A-W0", "X", 0, 1000.0, 0.9, 1.1, 0, 167, 3, None]]) \
        .drop(columns=["earliest_start_hour"])
    assert d._parse_demand(df)[0]["earliest_start"] is None


# ---------------------------------------------------------------------------
# effective_due_start / window clamp
# ---------------------------------------------------------------------------
def test_floor_raises_effective_due_start_under_every_policy():
    o = {**order("A", "111", 168, 335, 10000), "earliest_start": 200}
    assert effective_due_start(f_params(), o) == 200                      # unbounded early fill
    assert effective_due_start(f_params(early_fill_hours=48), o) == 200   # 120 < 200
    assert effective_due_start(f_params(allow_week1_in_week0=False), o) == 200
    o2 = {**order("A", "111", 168, 335, 10000), "earliest_start": 100}
    assert effective_due_start(f_params(allow_week1_in_week0=False), o2) == 168
    assert effective_due_start(f_params(), o2) == 100
    # committed MOs / trials keep their plant-fact start
    mo = {**o, "is_current_mo": True}
    assert effective_due_start(f_params(), mo) == 168


def test_window_capacity_bound_shrinks_with_the_floor():
    P = f_params(horizon_h=168)
    o = {**order("A", "111", 0, 167, 80000), "earliest_start": 100}
    data = mk_data(P, [0], ["111"], [o], rate=1000.0)
    assert _producible_kg_in_window(P, data, o, [0]) == 68 * 1000
    o0 = order("A", "111", 0, 167, 80000)
    data0 = mk_data(P, [0], ["111"], [o0], rate=1000.0)
    assert _producible_kg_in_window(P, data0, o0, [0]) == 168 * 1000


# ---------------------------------------------------------------------------
# CP-SAT model
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cross_week", [False, True])
def test_model_never_starts_an_order_before_its_floor(cross_week):
    P = f_params(horizon_h=168)
    o = {**order("A", "111", 0, 167, 10000), "earliest_start": 100}
    data = mk_data(P, [0], ["111"], [o], rate=1000.0)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced",
                       cross_week=cross_week)
    s, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    placed = blocks(s, v, data)
    assert placed, "order must still be placed (floor leaves 68 h)"
    assert all(b[2] >= 100 for b in placed), placed
    assert s.Value(v["produced"][0]) >= 9000


def test_model_without_floor_starts_earlier():
    P = f_params(horizon_h=168)
    o = order("A", "111", 0, 167, 10000)
    data = mk_data(P, [0], ["111"], [o], rate=1000.0)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    assert min(b[2] for b in blocks(s, v, data)) < 100


def test_floor_beyond_the_window_zeroes_the_demand_floor_not_feasibility():
    """A floor past the due window (the truck lands too late) must not turn
    the model INFEASIBLE: the producible clamp reads the same floor and
    drops qty_min to 0, the order simply produces nothing."""
    P = f_params(horizon_h=168)
    o = {**order("A", "111", 0, 119, 10000), "earliest_start": 150}
    data = mk_data(P, [0], ["111"], [o], rate=1000.0)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    assert s.Value(v["produced"][0]) == 0


# ---------------------------------------------------------------------------
# greedy seed
# ---------------------------------------------------------------------------
def test_greedy_seed_respects_the_floor():
    from helpers.greedy_fill import build_greedy_fill
    dem = {"order_id": "111-W1", "sku": "111", "week_index": 1,
           "qty_target": 14000, "lower_pct": 0.9, "upper_pct": 1.1,
           "due_start_hour": 168, "due_end_hour": 335,
           "earliest_start_hour": 250.0}
    rows, _ = build_greedy_fill(
        [dem], rates={("P09", "111"): 700.0}, setups={},
        line_segments={"P09": [(150.0, 504.0)]}, line_ids={"P09": 0},
        initial_sku={"P09": "999"}, horizon_h=504.0)
    assert rows and rows[0]["start_hour"] >= 250
    dem_nan = {**dem, "earliest_start_hour": float("nan")}
    rows, _ = build_greedy_fill(
        [dem_nan], rates={("P09", "111"): 700.0}, setups={},
        line_segments={"P09": [(150.0, 504.0)]}, line_ids={"P09": 0},
        initial_sku={"P09": "999"}, horizon_h=504.0)
    assert rows and rows[0]["start_hour"] == 168


# ---------------------------------------------------------------------------
# independent validator
# ---------------------------------------------------------------------------
def test_validator_flags_a_start_before_the_floor(tmp_path):
    from independent_validator import ERROR, validate_work_dir
    from test_independent_validator import (build_world, valid_schedule,
                                            write_outputs)
    world = build_world(tmp_path / "w")
    dem = pd.read_csv(world / "demand_plan.csv")
    dem["earliest_start_hour"] = [50.0, float("nan"), float("nan"), float("nan")]
    dem.to_csv(world / "demand_plan.csv", index=False)
    write_outputs(world)                       # O1 on L1 starts at h0
    rep = validate_work_dir(world)
    hits = [v for v in rep.by_code("STOCK_FLOOR") if v.order == "O1"]
    assert hits and all(v.severity == ERROR for v in hits)
    assert any(v.hours == 50.0 and v.line == "L1" for v in hits)
    assert rep.stats["stock_floor_orders"] == 1
    assert rep.ok is False
    # move both O1 pieces past the floor → clean
    s = valid_schedule()
    s.loc[0, ["start_hour", "end_hour", "run_hours"]] = [50.0, 70.0, 20.0]
    s.loc[1, ["start_hour", "end_hour", "run_hours"]] = [72.0, 92.0, 20.0]
    s.loc[4, ["start_hour", "end_hour", "run_hours"]] = [50.0, 82.0, 32.0]
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    assert rep.by_code("STOCK_FLOOR") == []


def test_validator_without_the_column_skips_the_check(tmp_path):
    from independent_validator import validate_work_dir
    from test_independent_validator import build_world, write_outputs
    world = build_world(tmp_path / "w")
    write_outputs(world)
    rep = validate_work_dir(world)
    assert rep.by_code("STOCK_FLOOR") == []
    assert any(c.startswith("STOCK_FLOOR (skipped") for c in rep.checks_run)
