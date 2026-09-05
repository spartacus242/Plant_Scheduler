# tests/test_solver_contracts.py -- INPUT->OUTPUT contracts for the CP-SAT solve.
#
# Item 9 of the handoff log. `scripts/check_solver_current_state.py` proved one
# contract by hand (lines must not start before their availability gate). Two
# 2026-08-10 defects would have been caught instantly by that check running
# automatically, so this module generalises it: for every input column that is
# supposed to constrain the solve, assert the OUTPUT honours it.
#
# Contracts covered
#   C1 availability gate   initial_states.available_from_hour
#   C2 downtime windows    downtimes.csv start_hour/end_hour
#   C3 no overlaps         production + CIP on one line never collide
#   C4 horizon bound       nothing scheduled past [scheduler] horizon_hours
#   C5 locked line         current_mo.csv locked_line MOs stay on their line
#   C6 CIP spacing         gaps between CIPs <= line_cip_hrs.max_cip_hrs
#   C7 downtime staleness  no outage silently expires inside the horizon
#
# Design notes
#   * Since 2026-09-03 (fix T-1, audit tests-1) these run against a
#     DETERMINISTIC artifact: data/test_fixtures/solver_tiny/ solved once per
#     session by tests/conftest.py (in-process CP-SAT, 2 workers, <= 15 s,
#     seed 7). Before that they asserted on "whatever solved last" under the
#     gitignored data/_scenario_work/ and skipped on a clean checkout, so the
#     regression gate was vacuous on CI and could re-target a stale dir here.
#     Set FLOWSTATE_LIVE_WORK=1 (or =<path>) to ALSO run them on a real work
#     dir; the pure-function tests for the downtime audit always run.
#   * The one solve is tiny (3 lines, 8 orders, ~2 s). A real solve is
#     60-600 s (flowstate-cp-sat-solver skill, pitfall 9) and stays in the
#     slow marker (tests/test_solver_fresh_solve.py).
#     `python scripts/check_solver_current_state.py` remains the live-run gate.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.downtime_horizon import (  # noqa: E402
    audit_downtime_horizon,
    uncovered_tail_hours,
)

from conftest import resolve_solver_work, solver_work_params  # noqa: E402

WORK_ROOT = ROOT / "data" / "_scenario_work"
TOL = 1e-6


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _solved_work_dirs() -> list[Path]:
    if not WORK_ROOT.exists():
        return []
    return sorted(
        (d for d in WORK_ROOT.iterdir()
         if d.is_dir() and (d / "schedule_phase2.csv").exists()),
        key=lambda d: (d / "schedule_phase2.csv").stat().st_mtime,
        reverse=True,
    )


def _read(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:  # noqa: BLE001
        return None
    return df if not df.empty else None


def _horizon(work: Path) -> float:
    toml_path = work / "flowstate.toml"
    if not toml_path.exists():
        return 0.0
    try:
        import tomllib

        sch = tomllib.loads(toml_path.read_text(encoding="utf-8")).get("scheduler", {})
        return float(sch.get("horizon_hours") or float(sch.get("horizon_weeks", 0) or 0) * 168)
    except Exception:  # noqa: BLE001
        return 0.0


def _norm(v) -> str:
    return str(v).strip().upper()


@pytest.fixture(scope="module", params=solver_work_params())
def work(request, solver_tiny_work: Path) -> Path:
    """The artifact under test.

    Fix T-1 (audit tests-1, 2026-09-03): `solver_tiny` is the deterministic
    fixture data/test_fixtures/solver_tiny/ solved ONCE per session by
    tests/conftest.py (2 workers, <= 15 s, seed 7, relax level 0). It exists
    on every checkout, so these contracts never skip. `live_work` (the newest
    dir under data/_scenario_work/, or FLOWSTATE_LIVE_WORK=<path>) is an
    EXTRA parametrisation, present only when FLOWSTATE_LIVE_WORK is set, and
    its absence is then a failure -- never a silent skip against a stale
    three-week-old artifact.
    """
    return resolve_solver_work(request.param, solver_tiny_work)


@pytest.fixture(scope="module")
def schedule(work: Path) -> pd.DataFrame:
    df = _read(work / "schedule_phase2.csv")
    if df is None:
        pytest.skip(f"{work.name}: empty schedule_phase2.csv")
    for col in ("start_hour", "end_hour"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["start_hour", "end_hour"])


# --------------------------------------------------------------------------
# C1 -- availability gate
# --------------------------------------------------------------------------
def test_c1_no_line_starts_before_its_availability_gate(work, schedule):
    """initial_states.available_from_hour is a PHYSICAL fact (the line is busy
    with a running MO). It must hold at every relax level -- pitfalls 13/16."""
    init = _read(work / "initial_states.csv")
    if init is None or "available_from_hour" not in init.columns:
        pytest.skip("no initial_states.csv with available_from_hour")

    gates = {
        _norm(r["line_name"]): float(r["available_from_hour"])
        for _, r in init.iterrows()
        if pd.notna(r.get("available_from_hour"))
    }
    trials = schedule.get("is_trial")
    prod = schedule[trials.astype(str).str.lower() != "true"] if trials is not None else schedule

    violations = []
    for line, grp in prod.groupby(prod["line_name"].map(_norm)):
        gate = gates.get(line)
        if gate is None or gate <= 0:
            continue
        first = float(grp["start_hour"].min())
        if first < gate - TOL:
            violations.append(f"{line} starts {first:g}h but gate is {gate:g}h")
    assert not violations, "production before the availability gate: " + "; ".join(violations)


# --------------------------------------------------------------------------
# C2 -- downtime windows
# --------------------------------------------------------------------------
def test_c2_no_production_inside_a_declared_downtime(work, schedule):
    dt = _read(work / "downtimes.csv")
    if dt is None:
        pytest.skip("no downtimes.csv")
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
                f"{line} sku {b['sku']} {b['start_hour']:g}-{b['end_hour']:g}h "
                f"inside downtime {ds:g}-{de:g}h"
            )
    assert not violations, "production scheduled during downtime: " + "; ".join(violations[:10])


# --------------------------------------------------------------------------
# C3 -- no overlaps (production + CIP share one NoOverlap per line)
# --------------------------------------------------------------------------
def test_c3_no_overlapping_blocks_on_a_line(work, schedule):
    cip = _read(work / "cip_windows.csv")
    frames = [schedule[["line_name", "start_hour", "end_hour"]].assign(kind="production")]
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
                violations.append(
                    f"{line}: {a['kind']} {a['start_hour']:g}-{a['end_hour']:g} overlaps "
                    f"{b['kind']} {b['start_hour']:g}-{b['end_hour']:g}"
                )
    assert not violations, "overlapping blocks: " + "; ".join(violations[:10])


# --------------------------------------------------------------------------
# C4 -- horizon bound
# --------------------------------------------------------------------------
def test_c4_nothing_scheduled_past_the_configured_horizon(work, schedule):
    horizon = _horizon(work)
    if horizon <= 0:
        pytest.skip("no horizon in work-dir flowstate.toml")
    over = schedule[schedule["end_hour"] > horizon + TOL]
    assert over.empty, (
        f"{len(over)} block(s) end past the {horizon:g}h horizon "
        f"(max {float(schedule['end_hour'].max()):g}h)"
    )


# --------------------------------------------------------------------------
# C5 -- locked line for current MOs (scenario E)
# --------------------------------------------------------------------------
def test_c5_locked_current_mos_stay_on_their_line(work, schedule):
    cm = _read(work / "current_mo.csv")
    if cm is None or "locked_line" not in cm.columns:
        pytest.skip("no current_mo.csv with locked_line (not a scenario-E work dir)")

    locked = cm[pd.to_numeric(cm["locked_line"], errors="coerce").fillna(0) > 0]
    if locked.empty:
        pytest.skip("current_mo.csv has no locked rows")

    id_col = "order_id" if "order_id" in locked.columns else locked.columns[0]
    placed = {
        _norm(r["order_id"]): _norm(r["line_name"]) for _, r in schedule.iterrows()
    }
    violations = []
    for _, r in locked.iterrows():
        oid = _norm(r[id_col])
        want = _norm(r["line_name"])
        got = placed.get(oid)
        if got is not None and got != want:
            violations.append(f"{oid} locked to {want} but placed on {got}")
    assert not violations, "locked MO moved line: " + "; ".join(violations[:10])


# --------------------------------------------------------------------------
# C6 -- CIP spacing
# --------------------------------------------------------------------------
def test_c6_cip_gaps_respect_max_hours_between_cip(work):
    cip = _read(work / "cip_windows.csv")
    lim = _read(work / "line_cip_hrs.csv")
    if cip is None or lim is None or "max_cip_hrs" not in lim.columns:
        pytest.skip("no cip_windows.csv / line_cip_hrs.csv")

    limits = {
        _norm(r["line_name"]): float(r["max_cip_hrs"])
        for _, r in lim.iterrows()
        if pd.notna(r.get("max_cip_hrs"))
    }
    for col in ("start_hour", "end_hour"):
        cip[col] = pd.to_numeric(cip[col], errors="coerce")
    cip = cip.dropna(subset=["start_hour", "end_hour"])

    violations = []
    for line, grp in cip.groupby(cip["line_name"].map(_norm)):
        limit = limits.get(line)
        if not limit:
            continue
        g = grp.sort_values("start_hour").reset_index(drop=True)
        for i in range(len(g) - 1):
            gap = float(g.iloc[i + 1]["start_hour"]) - float(g.iloc[i]["end_hour"])
            if gap > limit + TOL:
                violations.append(f"{line}: {gap:g}h between CIPs (limit {limit:g}h)")
    assert not violations, "CIP spacing exceeded: " + "; ".join(violations[:10])


# --------------------------------------------------------------------------
# C7 -- downtime staleness audit (pure function; always runs)
# --------------------------------------------------------------------------
def test_c7_audit_flags_a_stale_full_horizon_outage():
    rows = [{"line_name": "P11", "start_hour": "0", "end_hour": "336", "reason": "Down"}]
    notes = audit_downtime_horizon(rows, 504)
    assert len(notes) == 1
    assert "P11" in notes[0] and "504" in notes[0]
    assert uncovered_tail_hours(rows, 504) == {"P11": 168.0}


def test_c7_audit_is_quiet_when_the_outage_covers_the_horizon():
    rows = [{"line_name": "P11", "start_hour": "0", "end_hour": "504", "reason": "Down"}]
    assert audit_downtime_horizon(rows, 504) == []
    assert uncovered_tail_hours(rows, 504) == {}


def test_c7_audit_ignores_a_deliberate_mid_plan_outage():
    """Only windows starting at hour 0 AND ending on a legacy horizon boundary
    are suspicious. A real 2-day maintenance stop must not be flagged."""
    rows = [
        {"line_name": "P09", "start_hour": "48", "end_hour": "96", "reason": "Maint"},
        {"line_name": "P10", "start_hour": "0", "end_hour": "72", "reason": "Maint"},
    ]
    assert audit_downtime_horizon(rows, 504) == []


def test_c7_audit_tolerates_junk_rows():
    rows = [
        {"line_name": "P11", "start_hour": "", "end_hour": "336"},
        {"line_name": "P12", "start_hour": "0", "end_hour": "NULL"},
        {"line_name": "P13", "start_hour": "0", "end_hour": "0"},
        {},
    ]
    assert audit_downtime_horizon(rows, 504) == []


@pytest.mark.live_data
def test_c7_real_reference_downtimes_cover_the_configured_horizon():
    """The live data must not contain a stale full-horizon outage. This is the
    check that caught P11/P13 being scheduled on hours 336-504 of a 504 h plan
    while both lines were physically down.

    Marked `live_data` on purpose (fix T-4 / audit tests-6): this is a data
    audit of the bridge-refreshed export, the ONE legitimate reason to read
    data/reference in the suite; the conftest guard fails any unmarked read.

    The reference file stores wall-clock datetimes now; the audit speaks
    hours, so derive them through the one loader (migrate=False: a test must
    never rewrite the live file)."""
    dt_path = ROOT / "data" / "reference" / "downtimes.csv"
    toml_path = ROOT / "flowstate.toml"
    if not dt_path.exists() or not toml_path.exists():
        pytest.skip("reference downtimes.csv / flowstate.toml missing")
    import tomllib

    from helpers.downtime_store import load_downtimes_file
    from helpers.timefmt import planning_anchor

    cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    sch = cfg.get("scheduler", {})
    horizon = float(sch.get("horizon_hours") or float(sch.get("horizon_weeks", 0) or 0) * 168)
    if horizon <= 0:
        pytest.skip("no horizon configured")
    anchor = planning_anchor(cfg)
    rows = load_downtimes_file(
        dt_path, anchor=anchor, storage_anchor=anchor, migrate=False,
    ).to_dict("records")
    notes = audit_downtime_horizon(rows, horizon)
    assert not notes, "\n".join(notes)


def test_c7_runner_hook_reports_the_stale_window(tmp_path):
    """The audit must actually fire from scenario_runner, reading the WORK-DIR
    files the solver will read. The first version of this hook raised
    NameError (missing pandas import) and only the try/except note revealed it
    -- so the hook itself gets a test, not just the pure function."""
    from helpers.scenario_runner import _audit_work_downtimes

    (tmp_path / "downtimes.csv").write_text(
        "line_id,line_name,start_hour,end_hour,reason\n2,P11,0,336,Down\n",
        encoding="utf-8",
    )
    (tmp_path / "flowstate.toml").write_text(
        "[scheduler]\nhorizon_weeks = 3\nhorizon_hours = 504\n", encoding="utf-8"
    )
    notes = _audit_work_downtimes(tmp_path)
    assert len(notes) == 1, notes
    assert "P11" in notes[0] and "FAILED" not in notes[0]


def test_c7_runner_hook_is_silent_on_clean_data(tmp_path):
    from helpers.scenario_runner import _audit_work_downtimes

    (tmp_path / "downtimes.csv").write_text(
        "line_id,line_name,start_hour,end_hour,reason\n2,P11,0,504,Down\n",
        encoding="utf-8",
    )
    (tmp_path / "flowstate.toml").write_text(
        "[scheduler]\nhorizon_hours = 504\n", encoding="utf-8"
    )
    assert _audit_work_downtimes(tmp_path) == []


# --------------------------------------------------------------------------
# meta -- the feasibility report must exist and be readable for every solve
# --------------------------------------------------------------------------
def test_meta_feasibility_report_is_present_and_parseable(work):
    fp = work / "feasibility_report.json"
    if not fp.exists():
        pytest.skip(f"{work.name}: no feasibility_report.json")
    report = json.loads(fp.read_text(encoding="utf-8"))
    assert "status" in report and "relax_level" in report
