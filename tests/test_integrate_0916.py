# tests/test_integrate_0916.py — cross-agent integration of the 2026-09-16
# Scenario F decisions ("4 hrs is too short. Make it 8 hrs." and "the solver
# can pre-build the full week 41 into the idle tail as inventory") plus the
# seed-adoption fixes C1-C3. Each test pins a hand-off integration request:
#   * MINRUN  -> feasibility_report.json carries min_run_too_small
#   * PREBUILD -> overnight scoring reads a prebuild row's full target, and a
#                real 0.0 pct is not read as the 0.9 / 1.1 default
#   * PREBUILD / SEED -> the rulebook rows say what the code now does

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import phase2_scheduler as p2  # noqa: E402
from helpers.overnight_score import demand_orders  # noqa: E402
from helpers.solver_rules import solver_rules  # noqa: E402

import test_seed_adoption as tsa  # noqa: E402  (tiny gated work-dir builder)

PY = ROOT / ".venv" / "Scripts" / "python.exe"
SOLVER_SRC = ROOT / "code" / "solver" / "phase2_scheduler.py"


# ── MINRUN request 1: the structured too-small list reaches the report ─────

def test_min_run_too_small_helper_is_a_safe_copy():
    assert p2._min_run_too_small(None) == []
    assert p2._min_run_too_small({}) == []
    src = [{"order_id": "X", "why": "w"}]
    out = p2._min_run_too_small({"min_run_too_small": src})
    assert out == src and out[0] is not src[0]
    json.dumps(out)          # report-safe


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
def test_report_lists_an_order_too_small_for_the_minimum_run(tmp_path, solver_w2):
    """An order whose qty_max (3,300 kg) is below ONE 8 h run on every
    capable line (1,000 kg/h x 8 = 8,000 kg) is dead everywhere: the run
    stays FEASIBLE, the greedy seed skips it, the anchor still adopts the
    seed, and feasibility_report.json names the order in min_run_too_small
    next to the one-line model warning."""
    from helpers import scenario_runner as sr

    work = tsa._build_work(tmp_path / "work")
    dem = work / "demand_plan.csv"
    dem.write_text(dem.read_text(encoding="utf-8")
                   + "D-W0,A,0,3000,0.9,1.1,0,167,3,,\n", encoding="utf-8")
    sr._greedy_seed(work)
    seed = pd.read_csv(work / "prev_schedule.csv")
    assert "D-W0" not in set(seed["order_id"].astype(str))
    assert (seed["run_hours"] >= 8).all()

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "code" / "solver"), str(ROOT / "code")])
    proc = subprocess.run(
        [str(PY), str(solver_w2), "--data-dir", str(work),
         "--config", str(work / "flowstate.toml")],
        capture_output=True, text=True, timeout=240, env=env)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    rep = json.loads((work / "feasibility_report.json").read_text(encoding="utf-8"))
    assert rep["status"] in ("OPTIMAL", "FEASIBLE")
    small = rep["min_run_too_small"]
    assert [t["order_id"] for t in small] == ["D-W0"]
    assert small[0]["reason"] == "qty_max"
    # qty_max = data_loader's ceil(3000 x 1.1) = 3,301 (float 3300.0000000000005)
    assert small[0]["best_line_min_run_kg"] == 8000 and small[0]["qty_max"] in (3300, 3301)
    assert any("D-W0" in w and "minimum" in w for w in rep["model_warnings"])
    assert rep["seed_anchor"]["installed"] is True
    sched = pd.read_csv(work / "schedule_phase2.csv")
    assert "D-W0" not in set(sched["order_id"].astype(str))


@pytest.mark.skipif(not PY.exists(), reason="repo venv python not found")
def test_two_pass_report_keeps_seed_anchor_and_too_small_whichever_pass_wins(tmp_path, solver_w2):
    """Scenario F's real shape (soft demand + two_pass_co): the pass-1 report
    is written before pass 2 and rewritten (pass 1 kept) or replaced (pass 2
    adopted); seed_anchor and min_run_too_small must survive either way."""
    from helpers import scenario_runner as sr

    work = tsa._build_work(tmp_path / "work")
    toml = work / "flowstate.toml"
    toml.write_text(toml.read_text(encoding="utf-8")
                    .replace("two_pass_co = false", "two_pass_co = true"), encoding="utf-8")
    dem = work / "demand_plan.csv"
    dem.write_text(dem.read_text(encoding="utf-8")
                   + "D-W0,A,0,3000,0.9,1.1,0,167,3,,\n", encoding="utf-8")
    sr._greedy_seed(work)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "code" / "solver"), str(ROOT / "code")])
    proc = subprocess.run(
        [str(PY), str(solver_w2), "--data-dir", str(work), "--config", str(toml)],
        capture_output=True, text=True, timeout=240, env=env)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    rep = json.loads((work / "feasibility_report.json").read_text(encoding="utf-8"))
    assert rep["two_pass"]["adopted"] in ("pass 1", "pass 2")
    assert rep["two_pass"].get("decision") != "pass 2 pending"
    assert rep["seed_anchor"]["installed"] is True
    assert rep["seed_anchor"]["pass1_first_placed_kg"] > 0
    assert [t["order_id"] for t in rep["min_run_too_small"]] == ["D-W0"]


# ── PREBUILD request 3: overnight scoring of prebuild rows ─────────────────

def _staged(rows: list[dict]) -> pd.DataFrame:
    base = {"order_id": "", "sku": "111", "week_index": 0, "qty_target": 0.0,
            "lower_pct": float("nan"), "upper_pct": float("nan"),
            "qty_min": float("nan"), "qty_max": float("nan"),
            "due_start_hour": 0, "due_end_hour": 167}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_overnight_target_of_a_prebuild_row_is_its_full_week():
    """plan_fill prebuild: W3 [456, 503] keeps target 1,715,000 and cap
    1,886,500, floor pro-rated 48/168 x 1,543,500 = 441,000, pct blank.
    The midpoint (1,163,750) under-counted the staged week."""
    dem = _staged([
        {"order_id": "A-W3", "week_index": 3, "qty_target": 1_715_000.0,
         "qty_min": 441_000.0, "qty_max": 1_886_500.0,
         "due_start_hour": 456, "due_end_hour": 503},
        # a pct row and a C56-netted row score exactly as before
        {"order_id": "B-W0", "qty_target": 1000.0, "lower_pct": 0.9, "upper_pct": 1.1},
        {"order_id": "C-W1", "qty_target": 700.0, "qty_min": 600.0, "qty_max": 800.0},
    ])
    got = {o["order_id"]: o["target"] for o in demand_orders(dem)}
    assert got["A-W3"] == pytest.approx(1_715_000.0)
    assert got["B-W0"] == pytest.approx(1000.0)
    assert got["C-W1"] == pytest.approx(700.0)


def test_overnight_reads_a_trimmed_zero_pct_as_zero_not_the_default():
    """A DNS-trimmed row: lower_pct 0.0, upper_pct 0.5 -> band [0, 500];
    `float(0.0) or 0.9` read it as [900, 500] and scored 900 kg."""
    dem = _staged([{"order_id": "D-W0", "qty_target": 1000.0,
                    "lower_pct": 0.0, "upper_pct": 0.5}])
    assert demand_orders(dem)[0]["target"] == pytest.approx(500.0)


def test_overnight_target_rule_change_is_version_v3():
    """Review fix TESTS-2: the target rule changed, so the frozen score must
    carry a new version. The regression row is the live 280563-W0 on the
    09-16 09:23 staging: a netted row whose floor clamped to 0 (gross 30,000,
    credit 29,937.6 -> target 62.4, qty_min 0, qty_max 3,062.4, pct blank).
    v2's order_target(0, 3,062.4) took the max() branch and scored it as
    3,062.4 kg of demand; v3 scores min(62.4, 3,062.4) = 62.4."""
    from helpers.overnight_score import OVERNIGHT_SCORE_VERSION

    assert OVERNIGHT_SCORE_VERSION == "v3"
    dem = _staged([{"order_id": "280563-W0", "sku": "280563", "qty_target": 62.4,
                    "qty_min": 0.0, "qty_max": 3062.4}])
    assert demand_orders(dem)[0]["target"] == pytest.approx(62.4)


# ── rulebook rows (PREBUILD request 1, SEED request 1) ─────────────────────

def test_rulebook_states_the_partial_week_mode_and_the_seed_anchor():
    cfg = tomllib.loads((ROOT / "flowstate.toml").read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in solver_rules(cfg)}
    pw = rows["partial_week_demand"]
    assert pw["config"] == "scheduler.partial_week_demand"
    assert pw["value"] == "prebuild"                    # live from the repo toml
    assert rows["partial_week_demand"]["group"] == rows["horizon"]["group"]
    assert {r["id"]: r for r in solver_rules({})}["partial_week_demand"]["value"] \
        == "prebuild (default)"
    assert "partial_week_demand" in rows["horizon"]["planner"]
    assert "staging pro-rates a partial last week" not in rows["horizon"]["planner"]
    assert "partial_week_demand" in rows["week_grid"]["planner"]
    assert "pro-rates the partial fourth week" not in rows["week_grid"]["planner"]
    gs = rows["greedy_seed"]
    assert "seed_anchor" in gs["planner"] and "_anchor_warm_start_hint" in gs["where"]
