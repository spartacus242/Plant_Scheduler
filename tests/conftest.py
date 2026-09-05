# tests/conftest.py -- shared fixtures and suite-wide guards (fix agent T,
# 2026-09-03; audit findings tests-1, tests-6, tests-9).
#
#   solver_tiny_work   ONE deterministic CP-SAT solve per pytest session of the
#                      committed fixture data/test_fixtures/solver_tiny/ (2
#                      workers, <= 15 s, seed 7, relax level 0) into a tmp work
#                      dir. tests/test_solver_contracts.py and
#                      tests/test_constraint_probes.py assert on THAT artifact
#                      instead of on "whatever solved last" under the
#                      gitignored data/_scenario_work/ (tests-1). Set
#                      FLOWSTATE_LIVE_WORK=1 (newest work dir) or =<path> to
#                      ALSO run them against a real work dir; its absence is
#                      then a failure, never a silent skip.
#   live-data guard    An autouse fixture wraps builtins.open / io.open and
#                      records every READ of a file under data/reference/
#                      (the bridge-refreshed live export). A test that reads
#                      one without the explicit `@pytest.mark.live_data`
#                      marker (or a module-level allowlist entry, see
#                      LIVE_READ_ALLOWED_MODULES) fails at teardown with the
#                      offending paths (tests-6). The pinned fixtures under
#                      data/test_fixtures/ are not live data and never trip it.
#   node gate          A parity test that SKIPS "because node is missing"
#                      while node and the frontend toolchain ARE present is
#                      turned into a failure (tests-9): 55 client-side rule
#                      tests must not disappear from a green run unnoticed.
#                      FLOWSTATE_ALLOW_NODE_SKIPS=1 restores the skip (a box
#                      where node exists but npm install was not run).

from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SOLVER_TINY_DIR = ROOT / "data" / "test_fixtures" / "solver_tiny"
LIVE_REFERENCE_DIR = (ROOT / "data" / "reference").resolve()
WORK_ROOT = ROOT / "data" / "_scenario_work"
FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"

# CPU discipline: other agents share the box. One solve per session.
SOLVER_TINY_WORKERS = 2
SOLVER_TINY_TIME_S = 15.0

# Modules that are ALLOWED to read data/reference without the marker. Every
# entry is a known debt, not a blessing (audit tests-6 named the first six;
# they are owned by other agents / nobody and could not be edited here --
# see scratchpad/fixes/T/CHANGES.md HANDOFF). Remove an entry once the module
# reads the pinned snapshot data/test_fixtures/live_2026-08-13 instead.
LIVE_READ_ALLOWED_MODULES = {
    "test_changeover_cache.py": "stages a COPY of the live matrix into tmp, asserts nothing about its values",
    "test_current_state.py": "tests-6: builds ROOT/'data'/'reference' (lines 413-416); owner CA",
    "test_current_state_overlay.py": "tests-6: REF = ROOT/'data'/'reference' (line 22); owner CA",
    "test_line_rates.py": "tests-6: copies the live dir into tmp (line 18); owner SA",
    "test_sku_picker_payload.py": "tests-6: REF = ROOT/'data'/'reference' (line 115); owner FE",
    "test_solver_fresh_solve.py": "tests-6: slow-marked real solve from the live inputs (line 50); owner V",
    "test_no_live_data_reads.py": "the guard's own self-test opens a live path on purpose",
}
# Single tests allowed to read live data (nodeid suffix "<file>::<test>") --
# owned by other agents; the right fix is `@pytest.mark.live_data` on them.
LIVE_READ_ALLOWED_TESTS = {
    "test_fix_SA.py::test_live_matrix_only_quarter_hour_rows_change":
        "SA-4 structural check of the live changeover matrix (skips when absent); owner SA",
    # Caught by this guard on its first full-suite run (2026-09-03): the test
    # passes ROOT/'data' to stock_check_report, which reads
    # data/reference/demand_plan.csv and capabilities_rates.csv (api.py ~239
    # / load_rates) -- a live read the static scan could never see because
    # the path is built inside production code. Owner K: stage a tmp data
    # dir with the pinned snapshot (or mark it live_data). Until then: debt.
    "test_stockcheck_engine.py::test_report_end_to_end":
        "tests-6 (runtime catch): stock_check_report(ROOT/'data') reads live demand_plan/capabilities; owner K",
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_data: the test deliberately reads the live data/reference export "
        "(bridge-refreshed); without this marker such a read fails the test",
    )
    config.addinivalue_line(
        "markers",
        "node_gated: TypeScript parity test that needs node + frontend node_modules",
    )


# ---------------------------------------------------------------------------
# solver_tiny: one deterministic solve per session
# ---------------------------------------------------------------------------
def _solver_tiny_solve(work: Path) -> dict:
    """Copy the fixture inputs into `work`, solve relax level 0 in-process and
    write the solver's own output files (schedule_phase2.csv, cip_windows.csv,
    produced_vs_bounds.csv, mo_changes.csv, feasibility_report.json).

    Mirrors phase2_scheduler.main's single-phase level-0 build
    (maximize_production=True, objective 'balanced') and uses the solver's own
    writers (_solution_to_rows / extract_cip_windows / write_mo_changes) so the
    artifact has exactly the production shape. No log() call: the module's
    ERR_FILE would land in code/solver/ when imported outside __main__.
    """
    import tomllib

    from ortools.sat.python import cp_model

    import phase2_scheduler as p2
    from data_loader import Data, Files
    from model_builder import build_model

    work.mkdir(parents=True, exist_ok=True)
    for p in SOLVER_TINY_DIR.iterdir():
        if p.is_file() and p.suffix in (".csv", ".toml"):
            shutil.copy2(p, work / p.name)
    cfg = tomllib.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    sched = cfg.get("scheduler", {})
    P = p2.params_from_config(cfg, allow_week1=bool(sched.get("allow_week1_in_week0", True)))
    data = Data(P, Files(work))
    data.load()
    t0 = time.time()
    model, vars_dict = build_model(
        P, data, "full", False, False,
        max_lines_per_order_override=P.max_lines_per_order,
        maximize_production=True, objective_mode="balanced",
    )
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = SOLVER_TINY_WORKERS
    solver.parameters.max_time_in_seconds = SOLVER_TINY_TIME_S
    p2.apply_solver_seed(solver, P)
    status = solver.Solve(model)
    name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            f"solver_tiny fixture did not solve at relax level 0: {name} "
            f"({time.time() - t0:.1f}s). The fixture is designed to be "
            "feasible with hard demand -- a model change made it infeasible."
        )
    rows, bounds = p2._solution_to_rows(solver, data, P, vars_dict)
    cip_rows = p2.extract_cip_windows(solver, data, vars_dict.get("cip_vars") or {})
    import pandas as pd

    cols = ["line_id", "line_name", "order_id", "sku", "sku_description",
            "start_hour", "end_hour", "run_hours", "qty_kg", "start_dt",
            "end_dt", "is_trial"]
    pd.DataFrame(rows, columns=cols).to_csv(work / "schedule_phase2.csv", index=False)
    pd.DataFrame(cip_rows, columns=["line_id", "line_name", "start_hour", "end_hour"]
                 ).to_csv(work / "cip_windows.csv", index=False)
    pd.DataFrame(bounds).to_csv(work / "produced_vs_bounds.csv", index=False)
    p2.write_mo_changes(work, data, rows, bounds, P=P)
    report = {
        "status": name,
        "relax_level": 0,
        "relax_mode": p2.RELAX_LABELS[0],
        "soft_demand": bool(P.soft_demand),
        "cross_week": False,
        "ignore_co": False,
        "setup_times_enforced": True,
        "objective": solver.ObjectiveValue(),
        "wall_s": round(time.time() - t0, 2),
        "workers": SOLVER_TINY_WORKERS,
        "seed": P.solver_random_seed,
        "orders_short_of_qmin": [b["order_id"] for b in bounds
                                 if b["produced"] < b["qty_min"]],
        "fixture": str(SOLVER_TINY_DIR),
    }
    (work / "feasibility_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return {"work": work, "status": name, "P": P, "data": data, "solver": solver,
            "vars": vars_dict, "rows": rows, "bounds": bounds, "cip_rows": cip_rows}


@pytest.fixture(scope="session")
def solver_tiny_solution(tmp_path_factory) -> dict:
    """The solved fixture: work dir + in-memory solver/vars for property tests."""
    if not SOLVER_TINY_DIR.exists():
        pytest.fail(f"solver fixture missing: {SOLVER_TINY_DIR} (committed inputs; "
                    "regenerate with scratchpad/fixes/T/gen_solver_tiny.py)")
    work = tmp_path_factory.mktemp("solver_tiny_work")
    return _solver_tiny_solve(work)


@pytest.fixture(scope="session")
def solver_tiny_work(solver_tiny_solution) -> Path:
    return solver_tiny_solution["work"]


def solver_work_params() -> list[str]:
    """Parametrisation of the contract/probe `work` fixtures: always the
    deterministic fixture; the live work dir only when asked for."""
    params = ["solver_tiny"]
    if os.environ.get("FLOWSTATE_LIVE_WORK"):
        params.append("live_work")
    return params


def resolve_solver_work(param: str, solver_tiny_work: Path) -> Path:
    if param == "solver_tiny":
        return solver_tiny_work
    want = os.environ.get("FLOWSTATE_LIVE_WORK", "")
    if want and want != "1":
        p = Path(want)
        if not (p / "schedule_phase2.csv").exists():
            pytest.fail(f"FLOWSTATE_LIVE_WORK={want}: no schedule_phase2.csv there")
        return p
    dirs = []
    if WORK_ROOT.exists():
        dirs = sorted(
            (d for d in WORK_ROOT.iterdir()
             if d.is_dir() and (d / "schedule_phase2.csv").exists()),
            key=lambda d: (d / "schedule_phase2.csv").stat().st_mtime,
            reverse=True,
        )
    if not dirs:
        pytest.fail("FLOWSTATE_LIVE_WORK is set but no solved work dir exists under "
                    f"{WORK_ROOT} -- run a scenario first or unset the variable")
    return dirs[0]


# ---------------------------------------------------------------------------
# live-data read guard (tests-6)
# ---------------------------------------------------------------------------
_LIVE_REF_KEY = str(LIVE_REFERENCE_DIR).lower()


def _is_live_read(file, mode) -> bool:
    if not isinstance(file, (str, bytes, os.PathLike)):
        return False  # file descriptors
    if any(ch in str(mode) for ch in ("w", "a", "x", "+")):
        return False  # writes are a different sin (and rare in tests)
    s = os.fsdecode(file) if not isinstance(file, str) else file
    if "reference" not in s.lower():
        return False
    try:
        full = os.path.abspath(s).lower()
    except (TypeError, ValueError):
        return False
    return full.startswith(_LIVE_REF_KEY + os.sep) or full == _LIVE_REF_KEY


@pytest.fixture(autouse=True)
def _guard_live_data_reads(request):
    """Record reads of data/reference/* during the test; fail at teardown
    unless the test carries @pytest.mark.live_data or its module is listed
    in LIVE_READ_ALLOWED_MODULES."""
    hits: list[str] = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if _is_live_read(file, mode):
            hits.append(os.fsdecode(file) if not isinstance(file, str) else file)
        return real_open(file, mode, *args, **kwargs)

    builtins.open = spy_open
    io.open = spy_open
    try:
        yield hits
    finally:
        builtins.open = real_open
        io.open = real_open
    if not hits:
        return
    if request.node.get_closest_marker("live_data") is not None:
        return
    mod = Path(str(request.node.fspath)).name
    if mod in LIVE_READ_ALLOWED_MODULES:
        return
    short = f"{mod}::{request.node.name.split('[')[0]}"
    if short in LIVE_READ_ALLOWED_TESTS:
        return
    uniq = sorted(set(hits))
    pytest.fail(
        f"{request.node.nodeid} read the LIVE data/reference export "
        f"({len(uniq)} file(s)) without @pytest.mark.live_data:\n  "
        + "\n  ".join(uniq[:10])
        + "\nUse data/test_fixtures/ (pinned) or mark the test live_data on purpose."
    )


# ---------------------------------------------------------------------------
# node gate (tests-9): a node-reason skip while the toolchain is present fails
# ---------------------------------------------------------------------------
def node_toolchain_present() -> bool:
    return (shutil.which("node") is not None
            and (FRONTEND / "node_modules" / "typescript" / "bin" / "tsc").exists())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "setup" and rep.when != "call":
        return
    if not rep.skipped or os.environ.get("FLOWSTATE_ALLOW_NODE_SKIPS"):
        return
    reason = ""
    lr = rep.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        reason = str(lr[2])
    else:
        reason = str(lr)
    if "node" not in reason.lower():
        return
    if not node_toolchain_present():
        return
    rep.outcome = "failed"
    rep.longrepr = (
        f"{item.nodeid} SKIPPED with a node-related reason ({reason.strip()}) although "
        "node and code/components/gantt/frontend/node_modules/typescript are present "
        "(tests-9: TypeScript parity tests must not vanish silently). Fix the skip "
        "condition, or set FLOWSTATE_ALLOW_NODE_SKIPS=1 on a box without the toolchain."
    )
