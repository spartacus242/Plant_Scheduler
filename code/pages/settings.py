# pages/settings.py — Edit flowstate.toml parameters (advanced / standalone view).
# Note: settings are also accessible directly on the Run Solver page.

from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir
from helpers.safe_io import safe_write_toml


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


# ── Page ────────────────────────────────────────────────────────────────
st.header("Settings")
st.caption("Edit scheduler parameters stored in `flowstate.toml`.")
st.info(
    "These settings are also embedded on the **Run Solver** page and are saved automatically "
    "when you click Run Solver. Changes made here take effect on the next solve.",
    icon=":material/info:",
)

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
            help=(
                "How long the solver searches before returning the best solution found. "
                "**120 s** is a good default. Increase to **300–600 s** for large or complex schedules."
            ),
        )
        min_run = st.number_input(
            "Min run hours", value=sched.get("min_run_hours", 4),
            min_value=1, max_value=24, step=1,
            help=(
                "Minimum consecutive production hours per line/order assignment. "
                "**Suggested: 4 h.** Lower = more flexibility but more changeovers. "
                "Higher = fewer changeovers but may leave demand unmet if capacity is tight."
            ),
        )
    with col2:
        max_lines = st.number_input(
            "Max lines per order", value=sched.get("max_lines_per_order", 3),
            min_value=1, max_value=14, step=1,
            help=(
                "How many lines can simultaneously produce the same SKU. "
                "**Suggested: 2–3.** Higher = more load spreading but more changeovers. "
                "Set to 1 to keep each SKU on a single line."
            ),
        )
        planning_start = st.text_input(
            "Planning start date",
            value=sched.get("planning_start_date", "2026-02-15 00:00:00"),
            help=(
                "Anchor timestamp for hour 0 (format: YYYY-MM-DD HH:MM:SS). "
                "All start/end hours in every CSV are offsets from this point. "
                "The line rates table is filtered by the month of this date."
            ),
        )
    with col3:
        validate = st.checkbox(
            "Auto-validate after solve",
            value=sched.get("validate", True),
            help="Run post-solve checks (demand bounds, overlaps, CIP spacing, changeover timing). Adds only a few seconds. Recommended.",
        )

    st.subheader("CIP")
    col4, col5 = st.columns(2)
    with col4:
        cip_interval = st.number_input(
            "CIP interval — global fallback (hours)", value=cip.get("interval_h", 120),
            min_value=24, max_value=336, step=1,
            help=(
                "Maximum consecutive production hours before a CIP is required, "
                "for any line not listed in `line_cip_hrs.csv`. "
                "**Typical range: 96–144 h.** Per-line values in `line_cip_hrs.csv` take priority."
            ),
        )
    with col5:
        cip_duration = st.number_input(
            "CIP duration (hours)", value=cip.get("duration_h", 6),
            min_value=1, max_value=24, step=1,
            help=(
                "Length of each CIP block. **Typical: 4–8 h.** "
                "Every scheduled CIP removes this many hours from a line's available production time."
            ),
        )

    st.subheader("Objective weights")
    st.caption("All weights are unit-less scalars — only their ratios matter. Doubling one weight is equivalent to halving all others.")
    col6, col7, col8 = st.columns(3)
    with col6:
        w_makespan = st.number_input(
            "Makespan weight", value=obj.get("makespan_weight", 1),
            min_value=0, max_value=10000, step=1,
            help=(
                "Penalty per hour on the total schedule span. "
                "**Suggested: 1–5.** Raise to compress the schedule and finish production sooner. "
                "Set to 0 to ignore makespan entirely."
            ),
        )
    with col7:
        w_changeover = st.number_input(
            "Changeover weight", value=obj.get("changeover_weight", 100),
            min_value=0, max_value=10000, step=10,
            help=(
                "Multiplier on the total weighted changeover cost across all lines. "
                "**Suggested: 50–200.** At 100, one changeover costs as much as 100 extra hours of makespan. "
                "Raise to 500+ to strongly prioritize minimizing SKU switches."
            ),
        )
    with col8:
        w_cip_defer = st.number_input(
            "CIP defer weight", value=obj.get("cip_defer_weight", 10),
            min_value=0, max_value=10000, step=1,
            help=(
                "Reward per hour of CIP start time for pushing CIPs as late as the interval allows. "
                "**Suggested: 5–20.** Higher = longer uninterrupted production runs before each CIP. "
                "Set to 0 if CIP placement timing doesn't matter."
            ),
        )

    st.subheader("Changeover type penalties")
    st.caption(
        "Soft penalties for specific transition types flagged in `changeovers.csv`. "
        "These discourage complex changeovers without adding hard time constraints."
    )
    col9, col10, col11 = st.columns(3)
    with col9:
        w_conv_org = st.number_input(
            "Conv → Organic penalty", value=obj.get("co_conv_org_weight", 30),
            min_value=0, max_value=10000, step=5,
            help=(
                "Extra penalty when `conv_to_org_change = 1` (requires flush & rinse, typically 1–2 h). "
                "**Suggested: 20–50.** At 30, this changeover costs 30 extra makespan-hours in the objective."
            ),
        )
    with col10:
        w_cinn = st.number_input(
            "Cinn → Non-Cinn penalty", value=obj.get("co_cinn_weight", 20),
            min_value=0, max_value=10000, step=5,
            help=(
                "Extra penalty when `cinn_to_non = 1` (requires flush, typically ~1 h). "
                "**Suggested: 15–30.** Raise if cinnamon contamination is a quality risk."
            ),
        )
    with col11:
        w_flavor = st.number_input(
            "Added-flavor penalty (per flavor)", value=obj.get("co_flavor_weight", 5),
            min_value=0, max_value=1000, step=1,
            help=(
                "Penalty per unit of `added_flavors` in `changeovers.csv`. "
                "Negative values in the data become a reward (solver favors reducing flavor count). "
                "The net penalty never goes below zero. "
                "**Suggested: 3–10.** At 5, adding 3 flavors costs 15 extra makespan-hours."
            ),
        )

    submitted = st.form_submit_button("Save settings", type="primary")

if submitted:
    try:
        datetime.strptime(planning_start, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        st.error("Invalid **Planning start date** — must be `YYYY-MM-DD HH:MM:SS`.")
        st.stop()
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
        "co_conv_org_weight": int(w_conv_org),
        "co_cinn_weight": int(w_cinn),
        "co_flavor_weight": int(w_flavor),
    }
    safe_write_toml(cfg, toml_path)
    st.success(f"Saved to `{toml_path}`")
