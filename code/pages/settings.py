# pages/settings.py — Edit flowstate.toml parameters.

from __future__ import annotations
import sys
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

# ── TOML helpers ────────────────────────────────────────────────────────
def _load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _save_toml(path: Path, cfg: dict) -> None:
    import tomli_w
    with open(path, "wb") as f:
        tomli_w.dump(cfg, f)


# ── Page ────────────────────────────────────────────────────────────────
st.header("Settings")
st.caption("Edit scheduler parameters stored in `flowstate.toml`.")

toml_path = data_dir().parent / "flowstate.toml"
if not toml_path.exists():
    toml_path = data_dir() / "flowstate.toml"

cfg = _load_toml(toml_path)
sched = cfg.get("scheduler", {})
cip = cfg.get("cip", {})
obj = cfg.get("objective", {})

with st.form("settings_form"):
    st.subheader("Scheduler")
    col1, col2, col3 = st.columns(3)
    with col1:
        time_limit = st.number_input(
            "Solver time limit (s)", value=sched.get("time_limit", 120),
            min_value=10, max_value=3600, step=10,
            help="How long the solver searches for an optimal schedule. Longer = better solutions but slower. 120s is a good default; try 300s for complex problems.",
        )
        min_run = st.number_input(
            "Min run hours", value=sched.get("min_run_hours", 4),
            min_value=1, max_value=24, step=1,
            help="Minimum consecutive production hours per line/order assignment. Lower values allow more flexibility but increase changeovers. Higher values reduce changeovers but may leave demand unmet.",
        )
    with col2:
        max_lines = st.number_input(
            "Max lines per order", value=sched.get("max_lines_per_order", 3),
            min_value=1, max_value=14, step=1,
            help="How many lines can produce the same SKU in parallel. Higher values spread load but increase changeovers. Lower values concentrate production.",
        )
        planning_start = st.text_input(
            "Planning start date",
            value=sched.get("planning_start_date", "2026-02-15 00:00:00"),
            help="Anchor datetime for hour-0 of the planning horizon. All start/end hours are offsets from this timestamp.",
        )
    with col3:
        validate = st.checkbox(
            "Auto-validate after solve",
            value=sched.get("validate", True),
            help="Automatically run post-solve checks for demand bounds, overlaps, CIP spacing, and changeover timing.",
        )

    st.subheader("CIP")
    col4, col5 = st.columns(2)
    with col4:
        cip_interval = st.number_input(
            "CIP interval (hours)", value=cip.get("interval_h", 120),
            min_value=24, max_value=336, step=1,
            help="Maximum consecutive production hours before a CIP is required. Shorter intervals = more frequent cleaning but less risk of contamination.",
        )
    with col5:
        cip_duration = st.number_input(
            "CIP duration (hours)", value=cip.get("duration_h", 6),
            min_value=1, max_value=24, step=1,
            help="Length of each CIP block in hours. Directly reduces available production time on every line.",
        )

    st.subheader("Objective weights")
    col6, col7, col8 = st.columns(3)
    with col6:
        w_makespan = st.number_input(
            "Makespan weight", value=obj.get("makespan_weight", 1),
            min_value=0, max_value=10000, step=1,
            help="How strongly the solver tries to finish all production early. Higher = tighter schedules with less idle time.",
        )
    with col7:
        w_changeover = st.number_input(
            "Changeover weight", value=obj.get("changeover_weight", 100),
            min_value=0, max_value=10000, step=10,
            help="Penalty per SKU switch on a line. Higher = fewer changeovers but potentially longer makespans or unmet demand.",
        )
    with col8:
        w_cip_defer = st.number_input(
            "CIP defer weight", value=obj.get("cip_defer_weight", 10),
            min_value=0, max_value=10000, step=1,
            help="Reward for pushing CIPs as late as allowed. Higher = CIPs cluster near the interval deadline rather than occurring early.",
        )

    submitted = st.form_submit_button("Save settings", type="primary")

if submitted:
    cfg["scheduler"] = {
        "time_limit": int(time_limit),
        "min_run_hours": int(min_run),
        "max_lines_per_order": int(max_lines),
        "planning_start_date": planning_start,
        "validate": validate,
    }
    cfg["cip"] = {
        "interval_h": int(cip_interval),
        "duration_h": int(cip_duration),
    }
    cfg["objective"] = {
        "makespan_weight": int(w_makespan),
        "changeover_weight": int(w_changeover),
        "cip_defer_weight": int(w_cip_defer),
    }
    _save_toml(toml_path, cfg)
    st.success(f"Saved to `{toml_path}`")
