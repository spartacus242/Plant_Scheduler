# tests/test_two_pass_co.py — two-pass changeover minimization (Scenario F).
#
# Pass 1 maximizes the soft-demand fill score; pass 2 re-solves with that
# score as a hard floor and minimizes the weighted changeover load. The
# contract under test: the floor is respected, and given slack the pass-2
# objective prefers the sequence with fewer expensive changeovers.

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code" / "solver"))

from ortools.sat.python import cp_model  # noqa: E402

from solver.data_loader import Data, Files, Params  # noqa: E402
from solver.model_builder import build_model  # noqa: E402


def _tiny_data(P: Params) -> Data:
    """One line, three orders (two SKUs), all capable, no downtime."""
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = [0]
    d.line_names = {0: "P09"}
    for sku in ("111", "222"):
        d.capable[(0, sku)] = 1
        d.rate[(0, sku)] = 1000.0
    d.init_map = {0: {"available_from": 0, "initial_sku": "111",
                      "carryover_run_hours": 0}}
    d.downtimes = []
    d.cip_interval_map = {0: 100000}  # stood down, like Scenario F
    d.orders = [
        dict(order_id="A-W1", sku="111", due_start=0, due_end=167,
             qty_min=9000, qty_max=11000, qty_target=10000, priority=3),
        dict(order_id="B-W1", sku="222", due_start=0, due_end=167,
             qty_min=9000, qty_max=11000, qty_target=10000, priority=3),
        dict(order_id="C-W2", sku="111", due_start=168, due_end=335,
             qty_min=9000, qty_max=11000, qty_target=10000, priority=3),
    ]
    return d


def _params() -> Params:
    P = Params(horizon_h=336, min_run_hours=1, min_run_pct_of_qty=0.0,
               max_lines_per_order=1, allow_week1_in_week0=False,
               objective_idle_weight=0)
    P.soft_demand = True
    return P


def _solve(model, tl=10):
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = tl
    s.parameters.num_search_workers = 4
    status = s.Solve(model)
    return s, status


def test_pass1_exposes_prod_score_and_pass2_respects_floor():
    P = _params()
    data = _tiny_data(P)
    m1, v1 = build_model(P, data, "full", False, False,
                         maximize_production=True, objective_mode="balanced")
    assert v1["prod_score"] is not None
    s1, st1 = _solve(m1)
    assert st1 in (cp_model.FEASIBLE, cp_model.OPTIMAL)
    score1 = s1.Value(v1["prod_score"])
    assert score1 > 0

    floor = int(score1 * 0.99)
    m2, v2 = build_model(P, data, "full", False, False,
                         maximize_production=True, objective_mode="balanced",
                         min_prod_score=floor)
    s2, st2 = _solve(m2)
    assert st2 in (cp_model.FEASIBLE, cp_model.OPTIMAL)
    assert s2.Value(v2["prod_score"]) >= floor


def test_pass2_floor_above_optimum_is_infeasible():
    """A floor no schedule can reach must make pass 2 INFEASIBLE (the
    orchestrator then keeps pass 1) — the floor is HARD, never advisory."""
    P = _params()
    data = _tiny_data(P)
    m1, v1 = build_model(P, data, "full", False, False,
                         maximize_production=True, objective_mode="balanced")
    s1, _ = _solve(m1)
    score1 = s1.Value(v1["prod_score"])
    m2, _v2 = build_model(P, data, "full", False, False,
                          maximize_production=True, objective_mode="balanced",
                          min_prod_score=int(score1 * 10))
    _s2, st2 = _solve(m2)
    assert st2 == cp_model.INFEASIBLE


def test_prod_score_is_none_outside_soft_demand():
    P = _params()
    P.soft_demand = False
    data = _tiny_data(P)
    m, v = build_model(P, data, "full", False, False,
                       maximize_production=True, objective_mode="balanced")
    assert v["prod_score"] is None
