# pages/home.py — Dashboard landing page for Flowstate Scheduler.

from __future__ import annotations
import sys
from pathlib import Path
from datetime import datetime

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.paths import data_dir
from theme import metric_card, action_card, workflow_step, info_card

# ── Data helpers ─────────────────────────────────────────────────────────

dd = data_dir()

_REQUIRED_FILES = {
    "demand_plan.csv":        "Demand Plan",
    "capabilities_rates.csv": "Capabilities",
    "changeovers.csv":        "Changeovers",
    "initial_states.csv":     "Initial States",
    "downtimes.csv":          "Downtimes",
    "line_rates.csv":         "Line Rates",
    "line_cip_hrs.csv":       "CIP Intervals",
    "sku_info.csv":           "SKU Info",
}


def _count_rows(path: Path) -> int | None:
    """Return row count or None if file is missing / unreadable."""
    if not path.exists():
        return None
    try:
        import pandas as pd
        return len(pd.read_csv(path))
    except Exception:
        return None


def _file_ready(name: str) -> bool:
    return (dd / name).exists()


def _files_loaded() -> tuple[int, int]:
    ready = sum(1 for f in _REQUIRED_FILES if _file_ready(f))
    return ready, len(_REQUIRED_FILES)


def _schedule_status() -> str:
    if (dd / "schedule_phase2.csv").exists():
        return "Ready"
    return "Not generated"


def _active_lines() -> str:
    rows = _count_rows(dd / "initial_states.csv")
    return str(rows) if rows is not None else "—"


def _planning_horizon() -> str:
    toml_path = dd.parent / "flowstate.toml"
    if toml_path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError:
                return "2 weeks"
        try:
            cfg = tomllib.loads(toml_path.read_text())
            raw = cfg.get("scheduler", {}).get("planning_start_date", "")
            if raw:
                dt = datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S")
                return dt.strftime("%b %d, %Y")
        except Exception:
            pass
    return "2 weeks"


# ── Hero Banner ──────────────────────────────────────────────────────────

st.markdown(
    """
    <div class="fs-hero">
        <div class="fs-hero-title">Flowstate</div>
        <div class="fs-hero-subtitle">Production Scheduling Optimizer</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── KPI Status Cards ────────────────────────────────────────────────────

loaded, total = _files_loaded()
sched = _schedule_status()
lines = _active_lines()
horizon = _planning_horizon()

sched_detail = (
    "View in Schedule Viewer" if sched == "Ready" else "Run the solver to generate"
)

c1, c2, c3, c4 = st.columns(4)

with c1:
    st.markdown(
        metric_card(
            icon="📂",
            value=f"{loaded} / {total}",
            label="Data Files Loaded",
            detail="All required inputs" if loaded == total else f"{total - loaded} files missing",
            accent="green",
        ),
        unsafe_allow_html=True,
    )
with c2:
    st.markdown(
        metric_card(
            icon="📊",
            value=sched,
            label="Schedule Status",
            detail=sched_detail,
            accent="blue",
        ),
        unsafe_allow_html=True,
    )
with c3:
    st.markdown(
        metric_card(
            icon="🏭",
            value=lines,
            label="Active Lines",
            detail="From initial states",
            accent="pink",
        ),
        unsafe_allow_html=True,
    )
with c4:
    st.markdown(
        metric_card(
            icon="📅",
            value=horizon,
            label="Planning Start",
            detail="336 hours · 2-week horizon",
            accent="coral",
        ),
        unsafe_allow_html=True,
    )

st.markdown("<div style='height: 0.5rem'></div>", unsafe_allow_html=True)

# ── File Status Detail ───────────────────────────────────────────────────

with st.expander("Data file details", expanded=False):
    cols = st.columns(4)
    file_items = list(_REQUIRED_FILES.items())
    for i, (fname, label) in enumerate(file_items):
        with cols[i % 4]:
            exists = _file_ready(fname)
            rows = _count_rows(dd / fname) if exists else None
            if exists:
                badge_text = f"{rows} rows" if rows is not None else "found"
                st.markdown(
                    f"<span style='color:#00f2c3'>●</span>&ensp;**{label}**&ensp;"
                    f"<span style='color:#9a9a9a;font-size:0.8rem'>{badge_text}</span>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<span style='color:#6c6c8a'>○</span>&ensp;**{label}**&ensp;"
                    f"<span style='color:#6c6c8a;font-size:0.8rem'>not found</span>",
                    unsafe_allow_html=True,
                )

# ── Quick Actions ────────────────────────────────────────────────────────

st.markdown('<div class="fs-section-heading">Quick Actions</div>', unsafe_allow_html=True)

qa1, qa2, qa3, qa4 = st.columns(4)

with qa1:
    st.markdown(
        action_card("📋", "Demand Plan", "Upload or edit orders"),
        unsafe_allow_html=True,
    )
    st.page_link("pages/demand_plan.py", label="Open Demand Plan", icon=":material/arrow_forward:")

with qa2:
    st.markdown(
        action_card("⚙️", "Run Solver", "Configure & optimize"),
        unsafe_allow_html=True,
    )
    st.page_link("pages/run_solver.py", label="Open Run Solver", icon=":material/arrow_forward:")

with qa3:
    st.markdown(
        action_card("📈", "View Schedule", "Interactive Gantt chart"),
        unsafe_allow_html=True,
    )
    st.page_link("pages/schedule_viewer.py", label="Open Schedule Viewer", icon=":material/arrow_forward:")

with qa4:
    st.markdown(
        action_card("🔧", "Sandbox", "Drag-and-drop editing"),
        unsafe_allow_html=True,
    )
    st.page_link("pages/sandbox.py", label="Open Sandbox", icon=":material/arrow_forward:")

# ── Workflow Steps ───────────────────────────────────────────────────────

st.markdown('<div class="fs-section-heading">Workflow</div>', unsafe_allow_html=True)

_step_defs = [
    ("Load demand plan",        "Upload SKU targets with quantities, priorities, and due windows.",
     "demand_plan.csv"),
    ("Verify capabilities",     "Confirm which lines can run which SKUs and at what rate.",
     "capabilities_rates.csv"),
    ("Set initial states",      "Define each line's starting SKU, availability, and CIP status.",
     "initial_states.csv"),
    ("Add downtimes & trials",  "Block maintenance windows and pin trial runs (optional).",
     "downtimes.csv"),
    ("Run the solver",          "Configure objective weights and time limit, then optimize.",
     None),
    ("Review the schedule",     "Inspect the Gantt chart, filter by line or SKU, check validation.",
     "schedule_phase2.csv"),
    ("Adjust & export",         "Fine-tune in the Sandbox editor, then download the final schedule.",
     None),
]

schedule_exists = (dd / "schedule_phase2.csv").exists()

step_html_parts: list[str] = []
for i, (title, desc, check_file) in enumerate(_step_defs, start=1):
    if check_file and _file_ready(check_file):
        state = "completed"
    elif check_file is None and i == 5 and schedule_exists:
        state = "completed"
    elif check_file is None and i == 7 and schedule_exists:
        state = "active"
    else:
        prev_done = True
        for _, _, pf in _step_defs[: i - 1]:
            if pf and not _file_ready(pf):
                prev_done = False
                break
        state = "active" if prev_done and not (check_file and _file_ready(check_file)) else "pending"
    step_html_parts.append(workflow_step(i, title, desc, state))

st.markdown("".join(step_html_parts), unsafe_allow_html=True)

# ── Key Concepts ─────────────────────────────────────────────────────────

st.markdown('<div class="fs-section-heading">Key Concepts</div>', unsafe_allow_html=True)

kc1, kc2, kc3 = st.columns(3)

with kc1:
    st.markdown(
        info_card(
            "Planning Horizon",
            "336 hours (2 weeks) starting from the configured planning start date. "
            "All times are hour-offsets from this anchor.",
            accent="green",
        ),
        unsafe_allow_html=True,
    )

with kc2:
    st.markdown(
        info_card(
            "CIP (Clean-In-Place)",
            "Mandatory sanitation cycle. Each line has a max run interval before "
            "a CIP is required. CIPs are auto-scheduled and can split a production run.",
            accent="blue",
        ),
        unsafe_allow_html=True,
    )

with kc3:
    st.markdown(
        info_card(
            "Changeovers",
            "Setup time when switching SKUs. Organic-conversion, cinnamon, and flavor "
            "complexity flags apply soft penalties in the objective function.",
            accent="pink",
        ),
        unsafe_allow_html=True,
    )

st.markdown("<div style='height: 0.5rem'></div>", unsafe_allow_html=True)

kc4, kc5, kc6 = st.columns(3)

with kc4:
    st.markdown(
        info_card(
            "Two-Phase Solve",
            "Week-0 is solved first to lock line availability, then Week-1 is solved "
            "with updated constraints for tighter schedules.",
            accent="coral",
        ),
        unsafe_allow_html=True,
    )

with kc5:
    st.markdown(
        info_card(
            "Demand Bounds",
            "Each order has lower/upper percentage of its target. The solver must "
            "produce within this range — relax bounds if demand shows 'under'.",
            accent="green",
        ),
        unsafe_allow_html=True,
    )

with kc6:
    st.markdown(
        info_card(
            "Line Rates",
            "Monthly production rates per line override per-SKU rates automatically. "
            "Update line_rates.csv when rates change seasonally.",
            accent="blue",
        ),
        unsafe_allow_html=True,
    )
