# pages/sandbox.py — Interactive sandbox for manual schedule editing.
#
# Mounts the custom React Gantt sandbox component for full drag-and-drop
# editing.  Save/Reset/Export buttons remain in Streamlit below the component.
# All real-time KPIs and interactions are handled client-side in the React
# component for instant feedback.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.paths import data_dir
from helpers.sandbox_engine import (
    load_capabilities,
    load_changeovers,
    load_demand_targets,
    save_sandbox_to_files,
)
from components.gantt_sandbox import gantt_sandbox

st.header("Sandbox")
st.caption(
    "Drag blocks horizontally or between lines.  Resize by dragging edges.  "
    "Right-click for split/remove.  Changes update KPIs live.  "
    "Use Save below to write changes to files."
)

dd = data_dir()

# ── Load TOML config ────────────────────────────────────────────────────
try:
    import tomli

    with open(Path(BASE_DIR).parent / "flowstate.toml", "rb") as f:
        toml_cfg = tomli.load(f)
except Exception:
    toml_cfg = {}

planning_anchor = toml_cfg.get("scheduler", {}).get("planning_start_date", "2026-02-15 00:00:00")
cip_duration_h = toml_cfg.get("cip", {}).get("duration_h", 6)
min_run_hours = toml_cfg.get("scheduler", {}).get("min_run_hours", 4)
horizon_hours = 336

# ── Load reference data ─────────────────────────────────────────────────
caps_tuples = load_capabilities(dd)  # {(line_name, sku): rate}
changeovers_tuples = load_changeovers(dd)  # {(from_sku, to_sku): hours}
demand = load_demand_targets(dd)  # [{order_id, sku, qty_min, qty_max}]

# Convert capabilities to nested dict for React: {line: {sku: rate}}
caps_nested: dict[str, dict[str, float]] = {}
for (ln, sku), rate in caps_tuples.items():
    caps_nested.setdefault(ln, {})[sku] = rate

# Convert changeovers to nested dict: {from_sku: {to_sku: hours}}
co_nested: dict[str, dict[str, float]] = {}
for (f_sku, t_sku), hrs in changeovers_tuples.items():
    co_nested.setdefault(f_sku, {})[t_sku] = float(hrs)


# ── Load schedule blocks from CSV ──────────────────────────────────────
def _load_schedule_blocks() -> list[dict]:
    path = dd / "schedule_phase2.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    blocks = []
    for i, r in df.iterrows():
        blocks.append({
            "id": f"sched_{i}",
            "line_id": int(r.get("line_id", 0)),
            "line_name": str(r.get("line_name", "")),
            "order_id": str(r.get("order_id", "")),
            "sku": str(r.get("sku", "")),
            "start_hour": float(r.get("start_hour", 0)),
            "end_hour": float(r.get("end_hour", 0)),
            "run_hours": float(r.get("run_hours", 0)),
            "is_trial": bool(r.get("is_trial", False)),
            "block_type": "trial" if bool(r.get("is_trial", False)) else "sku",
        })
    return blocks


def _load_cip_blocks() -> list[dict]:
    path = dd / "cip_windows.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    blocks = []
    for i, r in df.iterrows():
        blocks.append({
            "id": f"cip_{i}",
            "line_id": int(r.get("line_id", 0)),
            "line_name": str(r.get("line_name", "")),
            "order_id": "CIP",
            "sku": "CIP",
            "start_hour": float(r.get("start_hour", 0)),
            "end_hour": float(r.get("end_hour", 0)),
            "run_hours": float(r.get("end_hour", 0)) - float(r.get("start_hour", 0)),
            "is_trial": False,
            "block_type": "cip",
        })
    return blocks


# Build line info list
def _load_lines() -> list[dict]:
    caps_path = dd / "Capabilities & Rates.csv"
    if not caps_path.exists():
        return []
    df = pd.read_csv(caps_path).drop_duplicates("line_name").sort_values("line_id")
    return [{"line_id": int(r["line_id"]), "line_name": str(r["line_name"])} for _, r in df.iterrows()]


# ── Initialize / refresh session state ──────────────────────────────────
# Reload from CSV if: (a) first visit, or (b) CSV is newer than cached data
_sched_path = dd / "schedule_phase2.csv"
_csv_mtime = _sched_path.stat().st_mtime if _sched_path.exists() else 0
_cached_mtime = st.session_state.get("sb_csv_mtime", 0)

if "sb_schedule" not in st.session_state or _csv_mtime > _cached_mtime:
    st.session_state["sb_schedule"] = _load_schedule_blocks()
    st.session_state["sb_cips"] = _load_cip_blocks()
    st.session_state["sb_holding"] = []
    st.session_state["sb_csv_mtime"] = _csv_mtime

schedule = st.session_state["sb_schedule"]
cip_blocks = st.session_state["sb_cips"]
holding = st.session_state["sb_holding"]
lines = _load_lines()

if not schedule and not cip_blocks:
    st.info("No schedule loaded. Run the solver first, then return here.")
    st.stop()

# ── Mount React component ──────────────────────────────────────────────
component_state = gantt_sandbox(
    schedule=schedule,
    cip_windows=cip_blocks,
    capabilities=caps_nested,
    changeovers=co_nested,
    demand_targets=demand,
    lines=lines,
    holding_area=holding,
    config={
        "planning_anchor": planning_anchor,
        "cip_duration_h": cip_duration_h,
        "min_run_hours": min_run_hours,
        "horizon_hours": horizon_hours,
    },
    height=800,
)

# ── Update state from React ────────────────────────────────────────────
# Skip component_state processing right after a reset so the cached/stale
# component return value doesn't overwrite freshly loaded CSV data.
if st.session_state.pop("sb_just_reset", False):
    pass  # consume the flag; ignore stale component_state this cycle
elif component_state is not None:
    if "schedule" in component_state:
        st.session_state["sb_schedule"] = component_state["schedule"]
        schedule = component_state["schedule"]
    if "cipWindows" in component_state:
        st.session_state["sb_cips"] = component_state["cipWindows"]
        cip_blocks = component_state["cipWindows"]
    if "holdingArea" in component_state:
        st.session_state["sb_holding"] = component_state["holdingArea"]
        holding = component_state["holdingArea"]
    if component_state.get("lastAction"):
        st.caption(f"_Last action: {component_state['lastAction']}_")

# ── Save / Reset / Export ──────────────────────────────────────────────
st.divider()
col_save, col_reset, col_export = st.columns(3)

with col_save:
    if st.button("Save to Schedule", type="primary", use_container_width=True):
        st.session_state["sb_confirm_save"] = True

    if st.session_state.get("sb_confirm_save"):
        st.warning(
            "This will overwrite `schedule_phase2.csv` and `cip_windows.csv` "
            "with your sandbox changes. The solver solution will be replaced. "
            "You will need to re-run the solver to restore the optimized schedule."
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Confirm save", type="primary"):
                save_sandbox_to_files(schedule, cip_blocks, caps_tuples, demand, dd)
                st.session_state["schedule_source"] = "sandbox"
                st.session_state.pop("sb_confirm_save", None)
                st.success("Schedule saved to files.")
        with c2:
            if st.button("Cancel"):
                st.session_state.pop("sb_confirm_save", None)
                st.rerun()

with col_reset:
    if st.button("Reset from solver", use_container_width=True):
        st.session_state["sb_schedule"] = _load_schedule_blocks()
        st.session_state["sb_cips"] = _load_cip_blocks()
        st.session_state["sb_holding"] = []
        st.session_state["sb_csv_mtime"] = _csv_mtime
        st.session_state["sb_just_reset"] = True
        st.rerun()

with col_export:
    if st.button("Go to Export", use_container_width=True):
        st.switch_page("pages/export.py")
