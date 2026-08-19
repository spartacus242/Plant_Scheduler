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
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return {"stages": [], "solutions": [], "solver_stats": {}, "data_summary": {}}


def _write(data_dir: Path, state: dict) -> None:
    """Atomic write: write to a temp file then os.replace() so the reader
    never sees a partial JSON document."""
    p = _progress_path(data_dir)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(data_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, str(p))
    except OSError:
        try:
            p.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError:
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
    **extra: Any,
) -> None:
    """Append an intermediate solution found by the solver.

    *extra* carries optional structured fields the UI feed can use without
    re-parsing the label (pass_id, placed_kg, co_load). Every entry gets a
    wall-clock "ts" so a reader can compute time-since-last-improvement
    without knowing when this solve pass started.
    """
    state = _read(data_dir)
    entry = {
        "wall_time": round(wall_time, 2),
        "objective": round(objective, 1),
        "label": label,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    state["solutions"].append(entry)
    _write(data_dir, state)


def update_solver_stats(data_dir: Path, **kwargs: Any) -> None:
    """Update live solver statistics (status, best_objective, bound, gap, etc.)."""
    state = _read(data_dir)
    state["solver_stats"].update(kwargs)
    _write(data_dir, state)


def reset_solver_stats(data_dir: Path, **kwargs: Any) -> None:
    """REPLACE solver_stats at the start of a solve pass.

    A two-pass run reuses the same progress file; merging pass-2's STARTING
    status over pass-1's final gap/objective would show pass-1 numbers as if
    they described pass 2. Dropping them is the honest reset.
    """
    state = _read(data_dir)
    state["solver_stats"] = dict(kwargs)
    _write(data_dir, state)


# ── Solution labels (pure — unit-tested in tests/test_solver_live.py) ─────
#
# The old label compared every solution against the FIRST feasible one. In
# the fill pass the first incumbent's objective is ~0, so percentages read
# like "+51831991155400.0%" — pure noise. Each pass now gets a baseline that
# means something for its direction: the fill pass reports real kg placed
# (evaluated from the model's produced-vars, never derived from the weighted
# objective), the changeover pass reports load relative to its own start
# (= pass 1's plan), and the generic path keeps vs-first percentages only
# when the baseline makes them meaningful.


def fmt_kg(kg: float) -> str:
    """Planner-readable kilograms: 3_327_132 -> '3.33M kg'."""
    kg = float(kg)
    if abs(kg) >= 1_000_000:
        return f"{kg / 1_000_000:.2f}M kg"
    return f"{kg:,.0f} kg"


def fill_solution_label(
    count: int,
    placed_kg: int,
    demand_kg: int,
    prev_placed_kg: int | None = None,
    pass_name: str = "fill",
    prefix: str = "",
) -> str:
    """Label for a fill-maximizing solution, in placed-demand terms.

    placed_kg is the model's own produced-var total (real kg), demand_kg the
    netted demand it was asked to place. A later solution can carry FEWER raw
    kg and still be an improvement (the weighted score pays nearest weeks and
    under-target orders first) — say that instead of pretending kg went up.
    """
    pct = f" ({100 * placed_kg / demand_kg:.0f}% of netted demand)" if demand_kg > 0 else ""
    body = f"{fmt_kg(placed_kg)} placed{pct}"
    if count == 1 or prev_placed_kg is None:
        return f"{prefix}{pass_name} · first plan on the board — {body}"
    delta = placed_kg - prev_placed_kg
    if delta > 0:
        return f"{prefix}{pass_name} · more demand placed — {body}, +{delta:,} kg"
    if delta == 0:
        return f"{prefix}{pass_name} · same tonnage, better placed — {body}"
    return (f"{prefix}{pass_name} · better week/target placement — {body}, "
            f"{delta:,} kg raw but scored higher")


def co_solution_label(
    count: int,
    objective: float,
    first_objective: float,
    co_load: int | None = None,
    first_co_load: int | None = None,
    prefix: str = "",
) -> str:
    """Label for a changeover-minimizing (pass 2) solution.

    Baseline = the first pass-2 incumbent, which IS pass 1's plan (installed
    as a complete hint), so "down X% since pass 2 started" means "vs the plan
    pass 1 handed over". Prefers the real weighted changeover load when the
    caller watches it; falls back to the pass objective (changeover load
    dominates it 100:1) and says so.
    """
    if co_load is not None and first_co_load is not None:
        if count == 1:
            return (f"{prefix}pass 2 · starting from pass 1's plan — "
                    f"changeover load {co_load:,}")
        if first_co_load > 0:
            pct = 100 * (first_co_load - co_load) / first_co_load
            if pct >= 0:
                return (f"{prefix}pass 2 · changeover load down {pct:.1f}% "
                        f"since pass 2 started (now {co_load:,})")
            # Objective improved but raw CO load rose: tiebreakers paid for it.
            return (f"{prefix}pass 2 · plan improved on tiebreakers — "
                    f"changeover load {co_load:,} ({-pct:.1f}% above pass-2 start)")
        return f"{prefix}pass 2 · changeover load {co_load:,}"
    # No watched CO expression — use the pass objective, honestly named.
    if count == 1:
        return f"{prefix}pass 2 · starting from pass 1's plan"
    if abs(first_objective) > 0:
        pct = 100 * (first_objective - objective) / abs(first_objective)
        return (f"{prefix}pass 2 · changeover objective down {pct:.1f}% "
                "since pass 2 started")
    return f"{prefix}pass 2 · improved plan #{count}"


def generic_solution_label(
    count: int,
    objective: float,
    first_objective: float,
    direction: str = "min",
    prefix: str = "",
) -> str:
    """Vs-first label for passes without a watched planner metric.

    The percentage is only shown when the first objective is a meaningful
    baseline; a near-zero or wildly-off-scale baseline (fill passes start at
    ~0) yields nonsense percentages, so those solutions just say "improved".
    """
    if count == 1:
        return f"{prefix}first feasible plan"
    if abs(first_objective) > 0:
        pct = 100 * (objective - first_objective) / abs(first_objective)
        if abs(pct) <= 500:
            word = "up" if pct >= 0 else "down"
            if direction == "min":
                # Minimize: down is the win; a CP-SAT incumbent only improves.
                word = "down" if pct <= 0 else "up"
            return f"{prefix}plan #{count} — objective {word} {abs(pct):.1f}% vs first"
    return f"{prefix}improved plan #{count}"


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
