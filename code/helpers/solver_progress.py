# helpers/solver_progress.py — Structured JSON progress writer for the solver.
#
# The solver subprocess calls these functions to write progress events to
# data/solver_progress.json.  The Run Solver page polls this file to render
# a live visual dashboard.  The file is overwritten atomically on each update
# so the reader always sees valid JSON.

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


_FILENAME = "solver_progress.json"


def _progress_path(data_dir: Path) -> Path:
    return Path(data_dir) / _FILENAME


def _read(data_dir: Path) -> dict:
    p = _progress_path(data_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stages": [], "solutions": [], "solver_stats": {}, "data_summary": {}}


def _write(data_dir: Path, state: dict) -> None:
    """Atomic write: write to a temp file then rename so the reader never
    sees a partial JSON document."""
    p = _progress_path(data_dir)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(data_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        # On Windows, target must not exist for os.rename
        if p.exists():
            p.unlink()
        os.rename(tmp, str(p))
    except Exception:
        # Fall back to direct write if atomic fails
        try:
            p.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass


# ── Public API ────────────────────────────────────────────────────────────


def init_progress(data_dir: Path, stages: list[dict[str, str]]) -> None:
    """Create the progress file with all stages set to 'pending'.

    *stages* is a list of dicts with 'id' and 'label' keys, e.g.
    [{"id": "loading_data", "label": "Loading Data"}, ...]
    """
    state = {
        "stages": [
            {"id": s["id"], "label": s["label"], "status": "pending", "detail": "", "ts": ""}
            for s in stages
        ],
        "solutions": [],
        "solver_stats": {},
        "data_summary": {},
    }
    _write(data_dir, state)


def update_stage(
    data_dir: Path,
    stage_id: str,
    status: str,
    detail: str = "",
) -> None:
    """Set a stage's status ('active', 'done', 'error') and optional detail string."""
    state = _read(data_dir)
    for s in state["stages"]:
        if s["id"] == stage_id:
            s["status"] = status
            if detail:
                s["detail"] = detail
            s["ts"] = datetime.now().isoformat(timespec="seconds")
            break
    _write(data_dir, state)


def set_data_summary(data_dir: Path, **kwargs: Any) -> None:
    """Store data summary stats (lines, orders, skus, etc.)."""
    state = _read(data_dir)
    state["data_summary"].update(kwargs)
    _write(data_dir, state)


def add_solution(
    data_dir: Path,
    wall_time: float,
    objective: float,
    label: str,
) -> None:
    """Append an intermediate solution found by the solver."""
    state = _read(data_dir)
    state["solutions"].append({
        "wall_time": round(wall_time, 2),
        "objective": round(objective, 1),
        "label": label,
    })
    _write(data_dir, state)


def update_solver_stats(data_dir: Path, **kwargs: Any) -> None:
    """Update live solver statistics (status, best_objective, bound, gap, etc.)."""
    state = _read(data_dir)
    state["solver_stats"].update(kwargs)
    _write(data_dir, state)


# ── Predefined stage lists ────────────────────────────────────────────────


STAGES_SINGLE = [
    {"id": "loading_data", "label": "Loading Data"},
    {"id": "building_model", "label": "Building Model"},
    {"id": "solving", "label": "Solving"},
    {"id": "writing_output", "label": "Writing Output"},
    {"id": "validating", "label": "Validating"},
]

STAGES_TWO_PHASE = [
    {"id": "loading_data", "label": "Loading Data"},
    {"id": "building_model_w0", "label": "Building Model (Week 0)"},
    {"id": "solving_week0", "label": "Solving Week 0"},
    {"id": "building_model_w1", "label": "Building Model (Week 1)"},
    {"id": "solving_week1", "label": "Solving Week 1"},
    {"id": "writing_output", "label": "Writing Output"},
    {"id": "validating", "label": "Validating"},
]
