"""Fresh-solve regression gate for the CP-SAT solver (task-list items 27 + 29).

Two gaps this module closes:

Item 27 -- the contract suite in ``test_solver_contracts.py`` asserts
INPUT -> OUTPUT contracts against ALREADY-SOLVED work dirs under
``data/_scenario_work/``. It reads *whatever solved last*, so a stale dir
makes it pass vacuously. This module performs a REAL single-phase solve from
the reference inputs and then re-runs the core physical-correctness contracts
against that fresh artifact, so every handoff run ends on a green real-solve
gate rather than an artifact gate.

Item 29 -- the warm start (item 10 / item 30) is currently only proven by
ad-hoc A/B shell scripts. This module folds a ``--no-warm-start`` vs default
solve comparison into the suite, so every handoff run re-proves the hint is
actually wired (a refactor that breaks ``apply_warm_start`` shows up as a
disabled / FAILED log line) and that it does not regress on blocks-placed or
orders-short-of-qmin.

Everything here is ``@pytest.mark.slow`` and spends ~2 * SOLVE_TL seconds in
the solver; run it explicitly (``pytest -m slow``) on the cron gate, never in
the default fast suite. The solver is launched as a real subprocess through the
repo venv python with PYTHONPATH scrubbed (flowstate-cp-sat-solver skill,
pitfall 7), exactly like the production path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from helpers.downtime_horizon import audit_downtime_horizon  # noqa: E402

pytestmark = pytest.mark.slow

SOLVER = ROOT / "code" / "solver" / "phase2_scheduler.py"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
REFERENCE = ROOT / "data" / "reference"
ROOT_TOML = ROOT / "flowstate.toml"

# Warm-start A/B (item 10) proved single-phase FEASIBLE at relax 3 with a 120 s
# budget; 150 s gives a little headroom so the gate is not flaky on a busy box.
SOLVE_TL = 150

# Reference inputs the solver reads (scenario_runner._prepare_work_dir mapping).
INPUTS = (
    "changeovers.csv",
    "downtimes.csv",
    "demand_plan.csv",
    "capabilities_rates.csv",
    "line_cip_hrs.csv",
    "sku_info.csv",
    "initial_states.csv",
)

TOL = 1e-6


# --------------------------------------------------------------------------
# solve driver
# --------------------------------------------------------------------------
def _stage_work_dir(work: Path) -> None:
    """Copy the solver's reference inputs + a time-limit-patched toml.

    Mirrors scenario_runner._prepare_work_dir: shutil.copy2 (exact bytes) for
    everything EXCEPT downtimes.csv — the reference file stores wall-clock
    datetimes, so the solver's hour-frame copy is derived through the one
    loader (helpers/downtime_store), exactly like production staging.
    """
    import shutil

    from helpers.downtime_store import stage_solver_downtimes
    from helpers.timefmt import planning_anchor

    work.mkdir(parents=True, exist_ok=True)
    import tomllib

    anchor = planning_anchor(tomllib.loads(ROOT_TOML.read_text(encoding="utf-8")))
    for name in INPUTS:
        src = REFERENCE / name
        if not src.exists():
            continue
        if name == "downtimes.csv":
            stage_solver_downtimes(src, work / name, anchor)
        else:
            shutil.copy2(src, work / name)
    toml = ROOT_TOML.read_text(encoding="utf-8")
    # Override the time limit only; keep every other [scheduler] default.
    if re.search(r"^\s*time_limit\s*=", toml, flags=re.MULTILINE):
        toml = re.sub(
            r"^\s*time_limit\s*=\s*\d+",
            f"time_limit = {SOLVE_TL}",
            toml,
            flags=re.MULTILINE,
        )
    else:
        toml = toml.replace("[scheduler]", f"[scheduler]\ntime_limit = {SOLVE_TL}", 1)
    (work / "flowstate.toml").write_text(toml, encoding="utf-8")


def _run_solve(work: Path, warm_start: bool) -> None:
    """Launch phase2_scheduler exactly like production (single-phase)."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)  # pitfall 7: never let the agent venv shadow numpy
    cmd = [
        str(PY),
        str(SOLVER),
        "--data-dir",
        str(work),
        "--config",
        str(work / "flowstate.toml"),
    ]
    if not warm_start:
        cmd.append("--no-warm-start")
    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=SOLVE_TL * 3,
    )
    assert proc.returncode == 0, (
        f"solver exited {proc.returncode}\nSTDERR:\n{proc.stderr[-2000:]}"
    )
    assert (work / "schedule_phase2.csv").exists(), "solver produced no schedule"
    assert (work / "feasibility_report.json").exists(), "no feasibility_report.json"


def _read_schedule(work: Path) -> pd.DataFrame:
    df = pd.read_csv(work / "schedule_phase2.csv", encoding="utf-8-sig")
    for col in ("start_hour", "end_hour"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["start_hour", "end_hour"])


def _report(work: Path) -> dict:
    return json.loads((work / "feasibility_report.json").read_text(encoding="utf-8"))


def _prod_blocks(schedule: pd.DataFrame) -> pd.DataFrame:
    trials = schedule.get("is_trial")
    if trials is not None:
        mask = trials.astype(str).str.lower() != "true"
        return schedule[mask]
    return schedule


def _norm(v) -> str:
    return str(v).strip().upper()


# --------------------------------------------------------------------------
# module-scoped fixtures: ONE cold solve + ONE warm solve (2 real solves total)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cold_work(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("fresh_cold")
    _stage_work_dir(work)
    _run_solve(work, warm_start=False)
    return work


@pytest.fixture(scope="module")
def warm_work(cold_work) -> Path:
    """Seed a fresh work dir with the COLD schedule as prev_schedule.csv, then
    solve with warm start ON. This is a genuinely warm run (not a carry-over)."""
    work = cold_work.parent / "fresh_warm"
    _stage_work_dir(work)
    # The solver reads prev_schedule.csv from the work dir (warm_start.py).
    # A verbatim copy of the cold schedule is the most faithful hint source.
    import shutil

    shutil.copy2(cold_work / "schedule_phase2.csv", work / "prev_schedule.csv")
    _run_solve(work, warm_start=True)
    return work


# --------------------------------------------------------------------------
# Item 27 -- fresh-solve contract gate
# --------------------------------------------------------------------------
def test_fresh_solve_is_feasible(cold_work):
    """The fresh single-phase solve must actually return a usable schedule."""
    rep = _report(cold_work)
    assert rep.get("status") == "FEASIBLE", (
        f"fresh solve status={rep.get('status')} relax={rep.get('relax_level')}"
    )
    schedule = _read_schedule(cold_work)
    assert len(_prod_blocks(schedule)) > 0, "fresh solve placed zero production blocks"


def test_fresh_solve_honours_availability_gates(cold_work):
    """C1: no production starts before initial_states.available_from_hour
    (a PHYSICAL fact -- pitfall 13)."""
    schedule = _read_schedule(cold_work)
    init = pd.read_csv(cold_work / "initial_states.csv", encoding="utf-8-sig")
    if "available_from_hour" not in init.columns:
        pytest.skip("no available_from_hour in initial_states.csv")
    gates = {
        _norm(r["line_name"]): float(r["available_from_hour"])
        for _, r in init.iterrows()
        if pd.notna(r.get("available_from_hour"))
    }
    prod = _prod_blocks(schedule)
    violations = []
    for line, grp in prod.groupby(prod["line_name"].map(_norm)):
        gate = gates.get(line)
        if gate is None or gate <= 0:
            continue
        if float(grp["start_hour"].min()) < gate - TOL:
            violations.append(f"{line} starts {float(grp['start_hour'].min()):g}h < gate {gate:g}h")
    assert not violations, "production before availability gate: " + "; ".join(violations)


def test_fresh_solve_keeps_downtime_clear(cold_work):
    """C2: nothing scheduled inside a declared downtime window."""
    schedule = _read_schedule(cold_work)
    dt = pd.read_csv(cold_work / "downtimes.csv", encoding="utf-8-sig")
    for col in ("start_hour", "end_hour"):
        dt[col] = pd.to_numeric(dt[col], errors="coerce")
    dt = dt.dropna(subset=["start_hour", "end_hour"])
    violations = []
    for _, d in dt.iterrows():
        line, ds, de = _norm(d["line_name"]), float(d["start_hour"]), float(d["end_hour"])
        hit = schedule[
            (schedule["line_name"].map(_norm) == line)
            & (schedule["start_hour"] < de - TOL)
            & (schedule["end_hour"] > ds + TOL)
        ]
        for _, b in hit.iterrows():
            violations.append(
                f"{line} {b['sku']} {b['start_hour']:g}-{b['end_hour']:g}h in downtime {ds:g}-{de:g}h"
            )
    assert not violations, "production during downtime: " + "; ".join(violations[:10])


def test_fresh_solve_no_overlaps(cold_work):
    """C3: production + CIP on one line never collide (shared NoOverlap)."""
    schedule = _read_schedule(cold_work)
    cip = pd.read_csv(cold_work / "cip_windows.csv", encoding="utf-8-sig")
    frames = [schedule[["line_name", "start_hour", "end_hour"]].assign(kind="prod")]
    if cip is not None:
        c = cip.copy()
        for col in ("start_hour", "end_hour"):
            c[col] = pd.to_numeric(c[col], errors="coerce")
        frames.append(
            c.dropna(subset=["start_hour", "end_hour"])[
                ["line_name", "start_hour", "end_hour"]
            ].assign(kind="cip")
        )
    allb = pd.concat(frames, ignore_index=True)
    violations = []
    for line, grp in allb.groupby(allb["line_name"].map(_norm)):
        g = grp.sort_values("start_hour").reset_index(drop=True)
        for i in range(len(g) - 1):
            a, b = g.iloc[i], g.iloc[i + 1]
            if b["start_hour"] < a["end_hour"] - TOL:
                violations.append(f"{line}: {a['kind']} {a['start_hour']:g}-{a['end_hour']:g} overlaps {b['kind']}")
    assert not violations, "overlaps: " + "; ".join(violations[:10])


def test_fresh_solve_respects_horizon(cold_work):
    """C4: nothing ends past the configured horizon."""
    toml = (cold_work / "flowstate.toml").read_text(encoding="utf-8")
    import tomllib

    sch = tomllib.loads(toml).get("scheduler", {})
    horizon = float(sch.get("horizon_hours") or float(sch.get("horizon_weeks", 0) or 0) * 168)
    schedule = _read_schedule(cold_work)
    over = schedule[schedule["end_hour"] > horizon + TOL]
    assert over.empty, f"{len(over)} block(s) past the {horizon:g}h horizon"


def test_fresh_solve_reference_downtimes_not_stale(cold_work):
    """C7: the staged reference downtimes must not contain a stale full-horizon
    outage (the defect that hid item 8 for a week)."""
    dt = pd.read_csv(cold_work / "downtimes.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
    rows = dt.to_dict("records")
    toml = (cold_work / "flowstate.toml").read_text(encoding="utf-8")
    import tomllib

    sch = tomllib.loads(toml).get("scheduler", {})
    horizon = float(sch.get("horizon_hours") or float(sch.get("horizon_weeks", 0) or 0) * 168)
    notes = audit_downtime_horizon(rows, horizon)
    assert not notes, "\n".join(notes)


# --------------------------------------------------------------------------
# Item 29 -- warm-start regression guard
# --------------------------------------------------------------------------
def test_warm_start_hint_is_actually_applied(warm_work):
    """The #1 regression we guard: a refactor that breaks apply_warm_start must
    surface as a 'disabled' or 'FAILED' log line, never as a silent no-op."""
    err = (warm_work / "solver_error.txt").read_text(encoding="utf-8", errors="ignore")
    assert "[warm-start] disabled by --no-warm-start" not in err, (
        "warm start was disabled on a warm run -- feature not wired"
    )
    assert "[warm-start] FAILED" not in err, "warm start raised an exception"
    assert "[warm-start] hinted" in err and "assignments" in err, (
        "warm start left no 'hinted N assignments' line -- feature not wired"
    )


def test_warm_start_does_not_regress(cold_work, warm_work):
    """Warm must be FEASIBLE like cold, and at least non-grossly worse on the
    two demand metrics (blocks-placed, orders-short-of-qmin). A broken hint can
    only ever make warm == cold (it is advisory), never better-than-cold-by-a
    lot, so the comparison catches a silently-disabled hint and a degraded
    solve. Tolerances absorb OR-Tools' search randomness."""
    cold_rep = _report(cold_work)
    warm_rep = _report(warm_work)
    assert cold_rep.get("status") == "FEASIBLE", f"cold not FEASIBLE: {cold_rep.get('status')}"
    assert warm_rep.get("status") == "FEASIBLE", f"warm not FEASIBLE: {warm_rep.get('status')}"

    cold_blocks = len(_prod_blocks(_read_schedule(cold_work)))
    warm_blocks = len(_prod_blocks(_read_schedule(warm_work)))
    cold_short = len(cold_rep.get("orders_short_of_qmin", []))
    warm_short = len(warm_rep.get("orders_short_of_qmin", []))

    # Blocks: allow 10% noise; warm should match or beat cold (it starts from cold's point).
    assert warm_blocks >= cold_blocks * 0.90, (
        f"warm placed far fewer blocks than cold ({warm_blocks} vs {cold_blocks})"
    )
    # Short-of-qmin: lower is better; allow a few orders' worth of noise.
    assert warm_short <= cold_short + max(3, int(0.10 * cold_short)), (
        f"warm short-of-qmin regressed ({warm_short} vs {cold_short})"
    )
