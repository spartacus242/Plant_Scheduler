# helpers/scenario_runner.py — Phase 2: generate solver scenarios A–D vs AZAP baseline.
#
# Wraps Flowstate-legacy CP-SAT with different objective modes, imports results
# into calendar_blocks shape, and scores with the same scorecard engine.

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from helpers.calendar_io import import_legacy_schedule
from helpers.paths import legacy_dir
from helpers.scorecard_engine import score_calendar
from helpers.version_manager import list_versions, save_version

# Scenario definitions — mapped to legacy --objective modes (+ notes).
SCENARIOS = [
    {
        "id": "A",
        "name": "Scenario A — Minimum changeovers",
        "objective": "min-changeovers",
        "two_phase": True,
        "intent": "Minimize SKU switches above all else.",
    },
    {
        "id": "B",
        "name": "Scenario B — Maximum throughput",
        "objective": "spread-load",
        "two_phase": True,
        "intent": "Spread load / keep lines productive (proxy for throughput).",
    },
    {
        "id": "C",
        "name": "Scenario C — Maintenance friendly",
        "objective": "balanced",
        "two_phase": True,
        "intent": (
            "Balanced solve; planner should align maintenance with CIP in the twin. "
            "v0 uses balanced weights — tune CIP defer / idle in legacy config for stronger maint bias."
        ),
    },
    {
        "id": "D",
        "name": "Scenario D — Balanced",
        "objective": "balanced",
        "two_phase": True,
        "intent": "Minimize makespan + weighted changeovers (recommended default).",
    },
]


def _prepare_work_dir(data_dir: Path, work: Path) -> None:
    """Copy reference + legacy data needed by the solver into a work directory."""
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    leg_data = legacy_dir() / "data"
    ref = data_dir / "reference"

    skip_names = {
        "schedule_phase2.csv",
        "cip_windows.csv",
        "produced_vs_bounds.csv",
        "idle_kpis.csv",
        "solver_error.txt",
        "solver_kpis.txt",
        "solver_progress.json",
        "validation_report.txt",
        "week1_initial_states.csv",
        "diag_blockages.csv",
        "diag_blockages.txt",
        "diag_order_feasibility.csv",
        "diag_order_linecap.csv",
        "diag_summary.txt",
        "diag_unique_line_load.csv",
        "schedule_meta.json",
        "feasibility_report.json",
    }

    # Prefer legacy full dataset for solver feasibility; overlay reference when present.
    # Normalize Windows-style case (Changeovers.csv) to what data_loader expects on Linux.
    rename_map = {
        "Changeovers.csv": "changeovers.csv",
        "Downtimes.csv": "downtimes.csv",
    }
    if leg_data.exists():
        for f in leg_data.iterdir():
            if f.is_file() and f.name not in skip_names and not f.name.startswith("diag_"):
                dest_name = rename_map.get(f.name, f.name)
                shutil.copy2(f, work / dest_name)

    if ref.exists():
        mapping = {
            "changeovers.csv": "changeovers.csv",
            "downtimes.csv": "downtimes.csv",
            "demand_plan.csv": "demand_plan.csv",
            "capabilities_rates.csv": "capabilities_rates.csv",
            "line_cip_hrs.csv": "line_cip_hrs.csv",
            "trials.csv": "trials.csv",
            "sku_info.csv": "sku_info.csv",
            "initial_states.csv": "initial_states.csv",
        }
        for src_name, dst_name in mapping.items():
            src = ref / src_name
            if src.exists():
                shutil.copy2(src, work / dst_name)

    root_toml = data_dir.parent / "flowstate.toml"
    if root_toml.exists():
        shutil.copy2(root_toml, work / "flowstate.toml")


def _read_feasibility(work: Path) -> dict[str, Any] | None:
    """Read feasibility_report.json from a solver work dir (written every run)."""
    import json
    path = work / "feasibility_report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_diag_blockages(work: Path) -> str:
    """Human-readable blockages summary from the solver work dir (if any)."""
    path = work / "diag_blockages.txt"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def run_scenario(
    scenario: dict[str, Any],
    data_dir: Path,
    *,
    time_limit: int | None = None,
    python_exe: str | None = None,
) -> dict[str, Any]:
    """Run one scenario. Returns {ok, calendar, scorecard, log, returncode,
    feasibility, relax_level}."""
    work = (Path(data_dir) / "_scenario_work" / scenario["id"]).resolve()
    _prepare_work_dir(Path(data_dir).resolve(), work)

    scheduler = (legacy_dir() / "code" / "phase2_scheduler.py").resolve()
    toml = work / "flowstate.toml"
    cmd = [
        python_exe or sys.executable,
        str(scheduler),
        "--data-dir", str(work),
        "--objective", scenario["objective"],
        "--config", str(toml.resolve()),
    ]
    if scenario.get("two_phase"):
        cmd.append("--two-phase")
    if time_limit is not None:
        # legacy reads time_limit from toml; patch file if requested
        try:
            text = toml.read_text(encoding="utf-8")
            import re
            text = re.sub(r"time_limit\s*=\s*\d+", f"time_limit = {int(time_limit)}", text)
            toml.write_text(text, encoding="utf-8")
        except Exception:
            pass

    proc = subprocess.run(
        cmd,
        cwd=str(legacy_dir() / "code"),
        capture_output=True,
        text=True,
        timeout=max(120, (time_limit or 60) * 4 + 60),
    )
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    err_file = work / "solver_error.txt"
    if err_file.exists():
        log += "\n" + err_file.read_text(encoding="utf-8")

    sched = work / "schedule_phase2.csv"
    cip = work / "cip_windows.csv"
    feas = _read_feasibility(work)
    relax_level = feas.get("relax_level") if feas else None
    diag_blockages = _read_diag_blockages(work)
    if proc.returncode != 0 or not sched.exists():
        return {
            "ok": False,
            "returncode": proc.returncode,
            "log": log,
            "calendar": None,
            "scorecard": None,
            "feasibility": feas,
            "relax_level": relax_level,
            "diag_blockages": diag_blockages,
        }

    calendar = import_legacy_schedule(
        sched,
        cip if cip.exists() else None,
        work / "downtimes.csv" if (work / "downtimes.csv").exists() else None,
    )
    score = score_calendar(calendar, week_label=scenario["name"], data_dir=data_dir)
    return {
        "ok": True,
        "returncode": proc.returncode,
        "log": log,
        "calendar": calendar,
        "scorecard": score,
        "feasibility": feas,
        "relax_level": relax_level,
        "diag_blockages": diag_blockages,
    }


def save_scenario_version(
    scenario: dict[str, Any],
    result: dict[str, Any],
    data_dir: Path,
) -> str:
    """Persist scenario as a named version (may delete oldest if at capacity — caller should manage slots)."""
    if not result.get("ok") or result.get("calendar") is None:
        raise ValueError("Scenario did not produce a calendar")
    # If full, delete a previous scenario with same id prefix
    existing = list_versions(data_dir)
    slug_hint = f"scenario_{scenario['id'].lower()}"
    for v in existing:
        if v.get("slug", "").startswith(slug_hint) or v.get("name", "").startswith(f"Scenario {scenario['id']}"):
            from helpers.version_manager import delete_version
            delete_version(v["slug"], data_dir)
            break
    # Still at max? raise
    if len(list_versions(data_dir)) >= 5:
        raise ValueError("Version slots full (5). Delete a version before generating scenarios.")

    sc = result["scorecard"]
    return save_version(
        scenario["name"],
        result["calendar"],
        sc.to_dict() if hasattr(sc, "to_dict") else sc,
        data_dir,
        notes=scenario.get("intent", ""),
        source=f"solver:{scenario['objective']}",
        pros="",
        cons="",
    )
