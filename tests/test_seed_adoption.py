# tests/test_seed_adoption.py — pass 1 must START from the greedy seed
# (fixes C1-C3, 2026-09-16 Scenario F solver-gaps investigation).
#
# The 09-16 board: a 3.70 Mt greedy seed, yet pass 1's first solution was
# 0 kg at 28.6 s and the board was built out of 4-h pieces. Three stacked
# defects, each pinned here:
#   C1  warm_start hinted seg_b_start = 0 on present pairs, but the model's
#       availability floor on seg_b_start is gated on `present` -> the hint
#       broke every gated line (all 14 live lines).
#   C2  greedy_fill started a line's first block AT the gate (no initial
#       changeover, no long-shutdown extra) and took setup gaps from the
#       PLACEMENT-order tail -> 8 of 106 seed rows broke the model.
#   C3  pass 1 received only a partial hint, which CP-SAT 9.15 never adopts;
#       phase2_scheduler now anchors it into a complete hint like pass 2.

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ortools.sat.python import cp_model  # noqa: E402

import phase2_scheduler as p2  # noqa: E402
from data_loader import Data, Files, Params  # noqa: E402
from helpers.greedy_fill import build_greedy_fill, free_segments  # noqa: E402
from model_builder import build_model  # noqa: E402
from warm_start import apply_warm_start, build_hint_plan  # noqa: E402

PY = ROOT / ".venv" / "Scripts" / "python.exe"
SOLVER_SRC = ROOT / "code" / "solver" / "phase2_scheduler.py"


@pytest.fixture(autouse=True)
def _log_to_tmp(tmp_path, monkeypatch):
    """phase2_scheduler.log() appends to ERR_FILE, which defaults to the repo's
    code/solver/solver_error.txt when the module is imported."""
    monkeypatch.setattr(p2, "ERR_FILE", tmp_path / "solver_error.txt")
    monkeypatch.setattr(p2, "KPI_FILE", tmp_path / "solver_kpis.txt")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _f_params(**kw) -> Params:
    base = dict(
        horizon_h=168, min_run_hours=4, min_run_pct_of_qty=0.5,
        max_lines_per_order=2, allow_week1_in_week0=True, soft_demand=True,
        objective_makespan_weight=6, objective_changeover_weight=300,
        objective_cip_defer_weight=5, objective_idle_weight=3,
        objective_late_weight=200, objective_week_deviation_weight=40,
        co_topload_weight=450, co_ttp_weight=5, co_ffs_weight=600,
        co_casepacker_weight=20, co_base_weight=5, co_conv_org_weight=30,
        co_cinn_weight=20, co_flavor_weight=5, co_cip_req_weight=150,
        cip_interval_h=120, cip_duration_h=6,
    )
    base.update(kw)
    return Params(**base)


def _mk_data(P, lines, skus, orders, *, avail=None, init=None, setup=None,
             long_extra=None, downtimes=None, rate=1000.0):
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = list(lines)
    d.line_names = {l: f"L{l}" for l in lines}
    for l in lines:
        for s in skus:
            d.capable[(l, s)] = 1
            d.rate[(l, s)] = rate
    avail, init, long_extra = avail or {}, init or {}, long_extra or {}
    d.init_map = {l: {"available_from": avail.get(l, 0),
                      "initial_sku": init.get(l, "CLEAN"),
                      "carryover_run_hours": 0,
                      "long_shutdown_flag": 1 if l in long_extra else 0,
                      "long_shutdown_extra": long_extra.get(l, 0)} for l in lines}
    d.downtimes = list(downtimes or [])
    d.cip_interval_map = {l: 100000 for l in lines}   # F stand-down
    d.setup = dict(setup or {})
    d.machine_changes = {}
    d.orders = [dict(o) for o in orders]
    return d


def _order(oid, sku, ds, de, target, lo=0.9, hi=1.1):
    return dict(order_id=oid, sku=sku, due_start=ds, due_end=de,
                qty_min=int(math.floor(target * lo)),
                qty_max=int(math.ceil(target * hi)),
                qty_target=int(target), priority=3)


def _dem(oid, sku, ds, de, target, week=0, **kw):
    row = {"order_id": oid, "sku": sku, "week_index": week,
           "qty_target": float(target), "lower_pct": 0.9, "upper_pct": 1.1,
           "due_start_hour": ds, "due_end_hour": de}
    row.update(kw)
    return row


def _write_prev(work: Path, rows):
    pd.DataFrame(rows).to_csv(work / "prev_schedule.csv", index=False)


def _anchor(model, vd, data, P, tl=30.0):
    return p2._anchor_warm_start_hint(model, vd, data, P, tl, level=0)


def _by_order(rows):
    return {r["order_id"]: r for r in rows}


# ═══════════════════════════════════════════════════════════════════════════
# C1 — warm_start.build_hint_plan
# ═══════════════════════════════════════════════════════════════════════════
def test_unsplit_present_pair_parks_seg_b_at_seg_a_end():
    """model_builder: seg_b_start >= available_from OnlyEnforceIf(present).
    The old hint (seg_b_start = seg_b_end = 0) broke that on every gated
    line; seg_a_end is >= the gate whenever seg_a_start is."""
    data = SimpleNamespace(orders=[{"order_id": "A-W0"}], lines=[0])
    plan, _ = build_hint_plan(data, 168, [{"line_id": 0, "order_id": "A-W0",
                                           "start_hour": 117, "end_hour": 125,
                                           "run_hours": 8}])
    e = plan[(0, 0)]
    assert e["seg_a_end"] == 125
    assert e["seg_b_present"] == 0 and e["seg_b_run"] == 0
    assert e["seg_b_start"] == 125 and e["seg_b_end"] == 125
    assert e["eff_end"] == 125 and e["run_h"] == 8


def test_split_pair_hint_keeps_its_second_segment():
    data = SimpleNamespace(orders=[{"order_id": "A-W0"}], lines=[0])
    rows = [{"line_id": 0, "order_id": "A-W0", "start_hour": 40,
             "end_hour": 48, "run_hours": 8},
            {"line_id": 0, "order_id": "A-W0", "start_hour": 10,
             "end_hour": 30, "run_hours": 20}]
    e = build_hint_plan(data, 168, rows)[0][(0, 0)]
    assert (e["seg_a_start"], e["seg_a_end"]) == (10, 30)
    assert (e["seg_b_present"], e["seg_b_start"], e["seg_b_end"]) == (1, 40, 48)
    assert e["run_h"] == 28 and e["eff_end"] == 48


def test_fixed_hint_anchor_is_feasible_on_a_gated_line(tmp_path):
    """One line gated at h10, a feasible previous row [10, 18): the anchor
    (every hinted var fixed) must be FEASIBLE. Old hint: INFEASIBLE."""
    P = _f_params()
    data = _mk_data(P, [0], ["111"], [_order("A-W0", "111", 0, 167, 8000)],
                    avail={0: 10})
    model, vd = build_model(P, data, "full", False, False,
                            maximize_production=True, objective_mode="balanced")
    _write_prev(tmp_path, [{"line_id": 0, "order_id": "A-W0", "start_hour": 10,
                            "end_hour": 18, "run_hours": 8}])
    apply_warm_start(model, vd, data, P.horizon_h, tmp_path)
    rec = _anchor(model, vd, data, P)
    assert rec["status"] in ("OPTIMAL", "FEASIBLE"), rec
    assert rec["installed"] is True
    assert rec["seed_placed_kg"] == 8000


# ═══════════════════════════════════════════════════════════════════════════
# C2 — greedy_fill mirrors the model's setup floors
# ═══════════════════════════════════════════════════════════════════════════
def test_first_block_pays_initial_changeover_and_long_shutdown_at_the_gate():
    """model_builder: start_first >= gate + setup(initial_sku, sku) + extra.
    Setup 1.5 h is reserved as 2 whole hours; long shutdown adds 3 h."""
    kw = dict(rates={("P09", "111"): 1000.0},
              setups={"999": {"111": 1.5}},
              line_segments={"P09": free_segments(10, [], 168.0)},
              line_ids={"P09": 0}, initial_sku={"P09": "999"},
              horizon_h=168.0, line_gates={"P09": 10.0})
    rows, _ = build_greedy_fill([_dem("A-W0", "111", 0, 167, 8000)], **kw)
    assert rows[0]["start_hour"] == 12                     # 10 + ceil(1.5)
    rows, _ = build_greedy_fill([_dem("A-W0", "111", 0, 167, 8000)],
                                initial_extra_hours={"P09": 3.0}, **kw)
    assert rows[0]["start_hour"] == 15                     # 10 + 2 + 3
    # CLEAN holds no SKU: only the long-shutdown extra applies
    kw["initial_sku"] = {"P09": "CLEAN"}
    rows, _ = build_greedy_fill([_dem("A-W0", "111", 0, 167, 8000)],
                                initial_extra_hours={"P09": 3.0}, **kw)
    assert rows[0]["start_hour"] == 13


def test_gate_defaults_to_the_first_free_segment():
    """No line_gates: the first free span's start is the (conservative) gate,
    so a line opening at h150 still pays its initial changeover there."""
    rows, _ = build_greedy_fill(
        [_dem("A-W0", "111", 0, 503, 8000)], rates={("P09", "111"): 1000.0},
        setups={"999": {"111": 2.0}}, line_segments={"P09": [(150.0, 504.0)]},
        line_ids={"P09": 0}, initial_sku={"P09": "999"}, horizon_h=504.0)
    assert rows[0]["start_hour"] == 152


def test_setup_comes_from_the_left_neighbour_in_time_not_placement_order():
    """The live P18 case. X (largest) is placed first at [60, 80); Y is placed
    SECOND but to the LEFT (window [40, 60)); Z (window from h80) sits right
    of X. The old seed took Z's gap from the last PLACED sku (Y, no setup)
    -> Z at h80, 3 h short of the X->Z setup the model demands; and it ran Y
    up to h59, 1 h into the 2-h Y->X setup owed to the block on its right."""
    dem = [_dem("X-W0", "X", 60, 79, 20000),
           _dem("Y-W0", "Y", 40, 59, 19000),
           _dem("Z-W0", "Z", 80, 167, 8000)]
    rows, _ = build_greedy_fill(
        dem, rates={("P09", s): 1000.0 for s in "XYZ"},
        setups={"X": {"Z": 3.0}, "Y": {"X": 2.0}},
        line_segments={"P09": [(0.0, 168.0)]}, line_ids={"P09": 0},
        initial_sku={"P09": "CLEAN"}, horizon_h=168.0, min_run_hours=4)
    by = _by_order(rows)
    assert (by["X-W0"]["start_hour"], by["X-W0"]["end_hour"]) == (60, 80)
    assert by["Z-W0"]["start_hour"] >= 80 + 3
    # ...and the block placed on the LEFT leaves the setup it owes X
    assert by["Y-W0"]["end_hour"] <= 60 - 2


def test_setup_floor_is_pairwise_across_a_block_in_between():
    """model_builder posts start_j >= end_i + setup(i, j) for EVERY two orders
    on a line, not only neighbours: A [0,10) -> B [10,14) -> C needs
    end_A + setup(A, C) = 16 even though A->B and B->C are free."""
    dem = [_dem("A-W0", "A", 0, 9, 10000),
           _dem("B-W0", "B", 10, 13, 4000),
           _dem("C-W0", "C", 14, 40, 4000)]
    rows, _ = build_greedy_fill(
        dem, rates={("P09", s): 1000.0 for s in "ABC"},
        setups={"A": {"C": 6.0}},
        line_segments={"P09": [(0.0, 168.0)]}, line_ids={"P09": 0},
        initial_sku={"P09": ""}, horizon_h=168.0, min_run_hours=4)
    by = _by_order(rows)
    assert by["B-W0"]["start_hour"] == 10
    assert by["C-W0"]["start_hour"] == 16


def test_a_committed_window_waives_no_setup_time_between_fill_blocks():
    """A 2-h committed CIP between X [0,40) and Y: the model's pairwise floor
    still wants 4 h after X (a committed CIP waives the changeover PRICE,
    never the hours) -> Y at h44, not at the window's end h42."""
    segs = free_segments(0, [(40, 42)], 168.0)
    rows, _ = build_greedy_fill(
        [_dem("X-W0", "X", 0, 39, 40000), _dem("Y-W0", "Y", 0, 167, 8000, week=1)],
        rates={("P09", "X"): 1000.0, ("P09", "Y"): 1000.0},
        setups={"X": {"Y": 4.0}}, line_segments={"P09": segs},
        line_ids={"P09": 0}, initial_sku={"P09": ""}, horizon_h=168.0,
        line_gates={"P09": 0.0})
    assert _by_order(rows)["Y-W0"]["start_hour"] == 44
    # a window as long as the setup already covers it: no extra gap
    segs6 = free_segments(0, [(40, 46)], 168.0)
    rows, _ = build_greedy_fill(
        [_dem("X-W0", "X", 0, 39, 40000), _dem("Y-W0", "Y", 0, 167, 8000, week=1)],
        rates={("P09", "X"): 1000.0, ("P09", "Y"): 1000.0},
        setups={"X": {"Y": 4.0}}, line_segments={"P09": segs6},
        line_ids={"P09": 0}, initial_sku={"P09": ""}, horizon_h=168.0,
        line_gates={"P09": 0.0})
    assert _by_order(rows)["Y-W0"]["start_hour"] == 46


def test_committed_production_after_the_gate_charges_no_setup():
    """A committed PRODUCTION window carries no SKU in the model: the first
    fill block after it only owes gate + initial changeover (already past)."""
    rows, _ = build_greedy_fill(
        [_dem("A-W0", "A", 0, 167, 8000)], rates={("P09", "A"): 1000.0},
        setups={"I": {"A": 4.0}},
        line_segments={"P09": free_segments(0, [(0, 30)], 168.0)},
        line_ids={"P09": 0}, initial_sku={"P09": "I"}, horizon_h=168.0,
        line_gates={"P09": 0.0})
    assert rows[0]["start_hour"] == 30


def test_no_seed_row_is_shorter_than_min_run_hours():
    """[scheduler] min_run_hours = 8 (planner decision 2026-09-16): windows
    or qty_max that allow < 8 h place nothing rather than a short row."""
    dem = [_dem("SHORT-W0", "A", 0, 5, 20000),              # 6 h window
           _dem("TINY-W0", "B", 0, 167, 5000),              # qmax 5500 -> 5 h
           _dem("OK-W0", "C", 20, 167, 20000)]
    rows, _ = build_greedy_fill(
        dem, rates={("P09", s): 1000.0 for s in "ABC"}, setups={},
        line_segments={"P09": [(0.0, 168.0)]}, line_ids={"P09": 0},
        initial_sku={"P09": ""}, horizon_h=168.0, min_run_hours=8)
    assert rows and all(r["run_hours"] >= 8 for r in rows)
    assert {r["order_id"] for r in rows} == {"OK-W0"}


def test_qty_max_holds_at_the_models_integer_rate_across_lines():
    """prod == sum(round(rate) x run_h) <= qty_max. Rate 775.6 (model 776):
    the old float cap floor(7756 / 775.6) = 10 h made 7,760 kg > 7,756."""
    rows, _ = build_greedy_fill(
        [_dem("A-W0", "A", 0, 167, 7756, lower_pct=float("nan"),
              upper_pct=float("nan"), qty_min=0.0, qty_max=7756.0)],
        rates={("P09", "A"): 775.6}, setups={},
        line_segments={"P09": [(0.0, 168.0)]}, line_ids={"P09": 0},
        initial_sku={"P09": ""}, horizon_h=168.0, min_run_hours=4)
    assert sum(776 * r["run_hours"] for r in rows) <= 7756
    # two lines: the second line's cap is what the FIRST left of qty_max.
    # P09 (holds B) fills its 6-h window with 6,000 kg; P10 (3,900 kg/h) may
    # add only 1 h. The old per-line cap floor(qmax / 3900) = 2 h gave 13,800.
    qmax = int(math.ceil(10000 * 1.1))
    rows, _ = build_greedy_fill(
        [_dem("B-W0", "B", 0, 5, 10000)],
        rates={("P09", "B"): 1000.0, ("P10", "B"): 3900.0},
        setups={"Q": {"B": 1.0}},
        line_segments={"P09": [(0.0, 168.0)], "P10": [(0.0, 168.0)]},
        line_ids={"P09": 0, "P10": 1}, initial_sku={"P09": "B", "P10": "Q"},
        horizon_h=168.0, min_run_hours=1, min_run_pct=0.0)
    by_line = {r["line_name"]: r for r in rows}
    assert by_line["P09"]["run_hours"] == 6
    total = sum((1000 if r["line_name"] == "P09" else 3900) * r["run_hours"]
                for r in rows)
    assert total <= qmax


def test_blank_pct_reads_explicit_bounds_and_pct_wins_when_present():
    """data_loader precedence: both pct -> pct; else explicit qty_min/qty_max
    (PREBUILD stages partly-covered weeks with blank pct + explicit bounds)."""
    kw = dict(rates={("P09", "A"): 1000.0}, setups={},
              line_segments={"P09": [(0.0, 504.0)]}, line_ids={"P09": 0},
              initial_sku={"P09": ""}, horizon_h=504.0, min_run_hours=8)
    explicit = _dem("A-W3", "A", 0, 503, 40000, lower_pct=float("nan"),
                    upper_pct=float("nan"), qty_min=2000.0, qty_max=12000.0)
    rows, _ = build_greedy_fill([explicit], **kw)
    assert sum(r["qty_kg"] for r in rows) == 12000.0          # capped by qty_max
    blank_str = {**explicit, "lower_pct": "", "upper_pct": None}
    rows, _ = build_greedy_fill([blank_str], **kw)
    assert sum(r["qty_kg"] for r in rows) == 12000.0
    both = {**explicit, "lower_pct": 0.9, "upper_pct": 1.1}
    rows, _ = build_greedy_fill([both], **kw)
    assert sum(r["qty_kg"] for r in rows) == 40000.0          # pct wins


# ═══════════════════════════════════════════════════════════════════════════
# C3 — phase2_scheduler._anchor_warm_start_hint
# ═══════════════════════════════════════════════════════════════════════════
def _toy_model():
    m = cp_model.CpModel()
    x = m.NewIntVar(0, 10, "x")
    y = m.NewIntVar(0, 10, "y")
    b = m.NewBoolVar("b")
    m.Add(x >= 5)
    m.Add(y == x + 1).OnlyEnforceIf(b)
    m.Add(b == 1)
    m.Maximize(x + y)
    m.AddDecisionStrategy([b], cp_model.CHOOSE_FIRST, cp_model.SELECT_MAX_VALUE)
    return m, x, y, b


def test_anchor_installs_a_complete_hint_and_restores_the_strategy():
    m, x, y, b = _toy_model()
    m.AddHint(x, 7)
    m.AddHint(b, 1)                              # y NOT hinted: partial
    data = SimpleNamespace(orders=[])
    rec = _anchor(m, {"produced": {}, "present": {}}, data, _f_params())
    proto = m.Proto()
    assert rec["status"] in ("OPTIMAL", "FEASIBLE") and rec["installed"]
    assert rec["hinted_vars"] == 2
    assert len(proto.solution_hint.vars) == len(proto.variables) == 3
    hint = dict(zip(proto.solution_hint.vars, proto.solution_hint.values))
    assert hint[x.Index()] == 7 and hint[y.Index()] == 8     # completed
    assert len(proto.search_strategy) == 1                   # C80: restored


def test_infeasible_seed_keeps_the_partial_hint_and_says_so(tmp_path):
    m, x, y, b = _toy_model()
    m.AddHint(x, 2)                              # violates x >= 5
    rec = _anchor(m, {"produced": {}, "present": {}}, SimpleNamespace(orders=[]),
                  _f_params())
    proto = m.Proto()
    assert rec["status"] == "INFEASIBLE" and rec["installed"] is False
    assert list(proto.solution_hint.vars) == [x.Index()]     # untouched
    assert len(proto.search_strategy) == 1                   # C80: restored
    assert "VIOLATES the model" in (tmp_path / "solver_error.txt").read_text(
        encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# End to end: staged work dir -> _greedy_seed -> loader -> model -> warm
# start -> anchor
# ═══════════════════════════════════════════════════════════════════════════
SKUS = ("A", "B", "C")


def _build_work(out: Path, *, min_run: int = 8, time_limit: int = 3) -> Path:
    """2 gated lines. L1 holds A at its gate h10 (A->B 1.5 h, C->A 2.25 h);
    L2 holds B at h20 with a long-shutdown flag (+3 h); a committed 6-h CIP
    on L1 at h60. Every rule the old seed / hint broke is in play."""
    out.mkdir(parents=True, exist_ok=True)
    cap = ["line_id,sku,line_name,capable,calc_rate_kgph"]
    for lid, ln in ((0, "L1"), (1, "L2")):
        for s in SKUS:
            cap.append(f"{lid},{s},{ln},1,1000.0")
    (out / "capabilities_rates.csv").write_text("\n".join(cap) + "\n", encoding="utf-8")
    (out / "line_rates.csv").write_text("line_id,Line,rate_kgph\n0,L1,1000\n1,L2,1000\n",
                                        encoding="utf-8")
    setup = {("A", "B"): 1.5, ("C", "A"): 2.25}
    chg = ["from_sku,to_sku,setup_hours,ttp_change,ffs_change,tpld_change,"
           "cspkr_change,conv_to_org,cinn_to_non_cinn,added_flavors,cip_req_after"]
    for a in SKUS:
        for b in SKUS:
            if a != b:
                chg.append(f"{a},{b},{setup.get((a, b), 1.0)},1,0,0,0,0,0,0,0")
    (out / "changeovers.csv").write_text("\n".join(chg) + "\n", encoding="utf-8")
    (out / "sku_info.csv").write_text(
        "sku,designation,format,casepacker_format,topload_format,pouch_format,is_organic,has_cinnamon\n"
        + "\n".join(f"{s},SKU {s},4x12x90,12,4,90,0,0" for s in SKUS) + "\n",
        encoding="utf-8")
    (out / "demand_plan.csv").write_text(
        "order_id,sku,week_index,qty_target,lower_pct,upper_pct,due_start_hour,"
        "due_end_hour,priority,qty_min,qty_max\n"
        "B-W0,B,0,40000,0.9,1.1,0,167,3,,\n"
        "C-W0,C,0,30000,0.9,1.1,0,167,3,,\n"
        "A-W1,A,1,20000,,,168,335,3,5000,24000\n", encoding="utf-8")
    (out / "initial_states.csv").write_text(
        "line_id,line_name,initial_sku,available_from_hour,long_shutdown_flag,"
        "long_shutdown_extra_setup_hours,carryover_run_hours_since_last_cip_at_t0,"
        "last_cip_end_datetime,comment\n"
        "0,L1,A,10,0,0,0,,\n1,L2,B,20,1,3,0,,\n", encoding="utf-8")
    (out / "line_cip_hrs.csv").write_text(
        "line_id,line_name,max_cip_hrs\n0,L1,100000\n1,L2,100000\n", encoding="utf-8")
    (out / "downtimes.csv").write_text(
        "line_id,line_name,start_hour,end_hour,reason\n"
        "0,L1,0,10,Committed PRODUCTION 1\n0,L1,60,66,Committed CIP\n"
        "1,L2,0,20,Committed PRODUCTION 2\n", encoding="utf-8")
    (out / "flowstate.toml").write_text(f'''planning_start_date = "2026-09-07 00:00:00"

[scheduler]
planning_start_date = "2026-09-07 00:00:00"
horizon_hours = 336
time_limit = {time_limit}
min_run_hours = {min_run}
max_lines_per_order = 2
validate = false
use_sku_rates = true
use_current_mo = false
early_fill_hours = "unbounded"
due_week_policy = "hard"
soft_demand = true
two_pass_co = false
solver_random_seed = 7

[cip]
interval_h = 100000
duration_h = 6

[objective]
makespan_weight = 6
changeover_weight = 300
cip_defer_weight = 5
late_weight = 200
idle_weight = 3

[changeover]
base_changeover_weight = 5
topload_weight = 450
ttp_weight = 5
ffs_weight = 600
casepacker_weight = 20
cip_req_weight = 150
''', encoding="utf-8")
    return out


def test_staged_seed_is_model_feasible_and_becomes_a_complete_hint(tmp_path):
    import tomllib

    from helpers import scenario_runner as sr

    work = _build_work(tmp_path / "work")
    notes = sr._greedy_seed(work)
    assert any("fill block" in n for n in notes), notes
    seed = pd.read_csv(work / "prev_schedule.csv")
    assert len(seed) >= 3 and (seed["run_hours"] >= 8).all()
    first = seed.sort_values("start_hour").groupby("line_name").first()
    assert first.loc["L1", "start_hour"] >= 10 + (2 if first.loc["L1", "sku"] == "B" else 1)
    assert first.loc["L2", "start_hour"] >= 20 + 3          # long shutdown

    cfg = tomllib.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    sched = cfg["scheduler"]
    P = p2.params_from_config(cfg, max_lines_override=sched["max_lines_per_order"],
                              min_run_override=sched["min_run_hours"], allow_week1=True)
    data = Data(P, Files(work))
    data.load()
    model, vd = build_model(P, data, "full", False, False,
                            max_lines_per_order_override=2, maximize_production=True,
                            objective_mode="balanced", relax_due=False,
                            cross_week=False, cip_flex=False)
    proto = model.Proto()
    n_strategy = len(proto.search_strategy)
    assert n_strategy >= 1                                  # soft-demand fill-first
    apply_warm_start(model, vd, data, P.horizon_h, work)
    assert 0 < len(proto.solution_hint.vars) < len(proto.variables)
    rec = _anchor(model, vd, data, P)
    assert rec["status"] in ("OPTIMAL", "FEASIBLE"), rec
    assert rec["installed"] is True
    assert len(proto.solution_hint.vars) == len(proto.variables)   # complete
    assert len(proto.search_strategy) == n_strategy
    model_kg = sum(int(round(data.rate[(int(r.line_id), str(r.sku))])) * int(r.run_hours)
                   for r in seed.itertuples())
    assert rec["seed_placed_kg"] == model_kg


def _toml_edit(work: Path, old: str, new: str) -> None:
    t = work / "flowstate.toml"
    text = t.read_text(encoding="utf-8")
    assert old in text
    t.write_text(text.replace(old, new), encoding="utf-8")


def test_seed_honours_the_toml_max_lines_per_order_and_anchors(tmp_path):
    """Review fix TESTS-3 (1). Work toml max_lines_per_order = 1 plus BIG-W0
    (sku C, 200,000 kg, week 0): no single line can hold it, so a seed that
    ignored the toml (the old call: always 2 lines) spreads it over L1 + L2
    and the level-0 anchor is INFEASIBLE (the model allows one line) — pass
    1 then starts from a partial hint, the cold start C3 exists to prevent.
    Honouring the toml: every order on one line, anchor installed."""
    import tomllib

    from helpers import scenario_runner as sr

    work = _build_work(tmp_path / "work")
    _toml_edit(work, "max_lines_per_order = 2", "max_lines_per_order = 1")
    dem = work / "demand_plan.csv"
    dem.write_text(dem.read_text(encoding="utf-8")
                   + "BIG-W0,C,0,200000,0.9,1.1,0,167,3,,\n", encoding="utf-8")
    notes = sr._greedy_seed(work)
    assert any("fill block" in n for n in notes), notes
    seed = pd.read_csv(work / "prev_schedule.csv")
    assert "BIG-W0" in set(seed["order_id"])
    per_order = seed.groupby("order_id")["line_id"].nunique()
    assert (per_order == 1).all(), per_order.to_dict()

    cfg = tomllib.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    sched = cfg["scheduler"]
    P = p2.params_from_config(cfg, max_lines_override=sched["max_lines_per_order"],
                              min_run_override=sched["min_run_hours"], allow_week1=True)
    data = Data(P, Files(work))
    data.load()
    model, vd = build_model(P, data, "full", False, False,
                            max_lines_per_order_override=1, maximize_production=True,
                            objective_mode="balanced")
    apply_warm_start(model, vd, data, P.horizon_h, work)
    rec = _anchor(model, vd, data, P)
    assert rec["status"] in ("OPTIMAL", "FEASIBLE"), rec
    assert rec["installed"] is True


def test_seed_share_floor_reads_the_toml_min_run_pct_of_qty(tmp_path):
    """Review fix TESTS-3 (2). Work toml min_run_pct_of_qty = 0.8, one order
    X-W0 (sku C, target 60,000, band 0.9/1.1 -> qmin 54,000) due [0,47].
    Per-line share floor ceil(0.8 x 54,000 / 1,000) = 44 h, but the longest
    window is 37 h (L1 [10 + setup A->C 1 h = 11, 48)); L2 has 24 h
    ([20 + 3 long shutdown + 1 h setup, 48)). So the seed places nothing.
    The old call used the build default 0.5 (27 h) and put a 37 h row on L1.

    The share floor is the seed's own and holds in BOTH in-window passes only
    under HARD demand (soft_demand = false). Under soft demand (Scenario F)
    the model's only floor is min_run_hours, and the in-window REMAINDER pass
    (review 2026-09-16, PREBUILD-NEARER-WEEK-STEAL) uses exactly that: X-W0
    is placed at >= 8 h per row and the level-0 anchor accepts it."""
    import tomllib

    from helpers import scenario_runner as sr

    def _work(soft: bool) -> Path:
        work = _build_work(tmp_path / ("soft" if soft else "hard"))
        _toml_edit(work, "max_lines_per_order = 2",
                   "max_lines_per_order = 2\nmin_run_pct_of_qty = 0.8")
        if not soft:
            _toml_edit(work, "soft_demand = true", "soft_demand = false")
        (work / "demand_plan.csv").write_text(
            "order_id,sku,week_index,qty_target,lower_pct,upper_pct,due_start_hour,"
            "due_end_hour,priority,qty_min,qty_max\n"
            "X-W0,C,0,60000,0.9,1.1,0,47,3,,\n", encoding="utf-8")
        return work

    work = _work(soft=False)
    notes = sr._greedy_seed(work)
    assert notes == ["greedy seed: nothing to place"], notes
    assert not (work / "prev_schedule.csv").exists()

    work = _work(soft=True)
    notes = sr._greedy_seed(work)
    assert any("in-window remainder" in n for n in notes), notes
    seed = pd.read_csv(work / "prev_schedule.csv")
    assert set(seed["order_id"]) == {"X-W0"}
    assert (seed["run_hours"] >= 8).all() and (seed["run_hours"] < 44).all()
    assert (seed["end_hour"] <= 48).all()
    cfg = tomllib.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    sched = cfg["scheduler"]
    P = p2.params_from_config(cfg, max_lines_override=sched["max_lines_per_order"],
                              min_run_override=sched["min_run_hours"], allow_week1=True)
    assert P.soft_demand is True
    data = Data(P, Files(work))
    data.load()
    model, vd = build_model(P, data, "full", False, False,
                            max_lines_per_order_override=2, maximize_production=True,
                            objective_mode="balanced")
    apply_warm_start(model, vd, data, P.horizon_h, work)
    rec = _anchor(model, vd, data, P)
    assert rec["status"] in ("OPTIMAL", "FEASIBLE"), rec
    assert rec["installed"] is True


@pytest.fixture(scope="module")
def solver_w2(tmp_path_factory) -> Path:
    """phase2_scheduler.py with the 8-worker portfolio cut to 2 workers."""
    src = SOLVER_SRC.read_text(encoding="utf-8")
    needle = "SEARCH_WORKERS = 1 if DETERMINISTIC else 8\n"
    assert src.count(needle) == 1
    dst = tmp_path_factory.mktemp("solver") / "phase2_scheduler_w2.py"
    dst.write_text(src.replace(needle, "SEARCH_WORKERS = 1 if DETERMINISTIC else 2\n"),
                   encoding="utf-8")
    return dst


@pytest.mark.skipif(not PY.exists(), reason="repo venv python not found")
def test_single_phase_run_anchors_the_seed_and_reports_it(tmp_path, solver_w2):
    """main(): the ladder applies the warm start, anchors it BEFORE the pass-1
    solve and stamps seed_anchor into feasibility_report.json."""
    from helpers import scenario_runner as sr

    work = _build_work(tmp_path / "work")
    sr._greedy_seed(work)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "code" / "solver"), str(ROOT / "code")])
    proc = subprocess.run(
        [str(PY), str(solver_w2), "--data-dir", str(work),
         "--config", str(work / "flowstate.toml")],
        capture_output=True, text=True, timeout=240, env=env)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    log = (work / "solver_error.txt").read_text(encoding="utf-8")
    assert "[warm-start] hinted" in log
    assert "[seed-anchor] level 0: anchor " in log and "hint installed" in log
    assert "a warm start adds a seed anchor <= 3s" in log
    i_anchor = log.index("[seed-anchor] level 0")
    i_solver = log.index("SOLVER level=0")
    assert i_anchor < i_solver
    rep = json.loads((work / "feasibility_report.json").read_text(encoding="utf-8"))
    sa = rep["seed_anchor"]
    assert sa["ran"] is True and sa["installed"] is True and sa["level"] == 0
    assert sa["status"] in ("OPTIMAL", "FEASIBLE")
    assert sa["complete_hint_vars"] == sa["model_vars"] > sa["hinted_vars"]
    assert sa["seed_placed_kg"] > 0
    assert sa["pass1_first_placed_kg"] is not None and sa["pass1_first_placed_kg"] > 0
