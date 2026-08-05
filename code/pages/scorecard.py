# pages/scorecard.py — Phase 0: How good is this week's schedule?

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.calendar_io import (
    ensure_lines_from_calendar,
    import_legacy_schedule,
    load_calendar,
    save_calendar,
)
from helpers.config import load_toml
from helpers.labels import display_label
from helpers.manual_import_ui import (
    backup_file,
    render_manual_import,
    snapshot_current_schedule,
)
from helpers.paths import data_dir, legacy_dir, reference_dir
from helpers.scorecard_engine import list_scorecards, save_scorecard, score_calendar
from helpers.scorecard_ui import render_scorecard, scorecard_table
from helpers.timefmt import with_display_times

st.header("Schedule Scorecard")
st.caption(
    "Phase 0 - score the plant's own line schedule consistently every week. "
    "No optimization. Answer: **How good is this week's schedule?**"
)
st.info(
    "**AZAP is the demand plan, not a schedule.** AZAP tells the plant which SKUs to make, "
    "how many kg, and in which week (`data/reference/demand_plan.csv`). It never assigns "
    "lines, sequence or equipment. The schedule scored on this page is the plant's own "
    "line schedule (`data/calendar_blocks.csv`), built by the production planner.",
    icon=":material/info:",
)

dd = data_dir()
cal_path = dd / "calendar_blocks.csv"
cfg = load_toml()
anchor = cfg.get("scheduler", {}).get("planning_start_date", "2026-02-15 00:00:00")
horizon_h = int(cfg.get("scheduler", {}).get("horizon_hours", 336))
week_label = st.text_input("Week label", value=f"Week-{date.today().isoformat()}")


# -- Import the planner's own manual schedule ---------------------------
with st.expander(
    "Upload the planner's manual line schedule (recommended base model)",
    expanded=not cal_path.exists(),
):
    render_manual_import(dd, anchor, horizon_h, key="sc_manual")


# -- Import the legacy seed data ----------------------------------------
with st.expander("Import legacy seed schedule (developer / first run)"):
    st.markdown(
        "Import converts legacy `schedule_phase2.csv` + `cip_windows.csv` + downtimes "
        "into the unified `calendar_blocks.csv` used by scoring, the digital twin, and scenarios."
    )
    src_choice = st.radio(
        "Source",
        ["Flowstate-legacy data (seed)", "Upload CSVs", "Already imported"],
        horizontal=True,
    )

    if src_choice == "Flowstate-legacy data (seed)":
        leg = legacy_dir() / "data"
        if st.button("Import from Flowstate-legacy"):
            cal = import_legacy_schedule(
                leg / "schedule_phase2.csv",
                leg / "cip_windows.csv",
                reference_dir(dd) / "downtimes.csv",
                planning_anchor=anchor,
            )
            backup_file(cal_path, dd)
            save_calendar(cal, cal_path)
            ensure_lines_from_calendar(cal, dd / "lines.csv")
            snapshot_current_schedule(cal, dd)
            st.success(f"Imported {len(cal)} blocks into calendar_blocks.csv (+ saved version)")
            st.rerun()
    elif src_choice == "Upload CSVs":
        up_sched = st.file_uploader("schedule_phase2.csv", type=["csv"])
        up_cip = st.file_uploader("cip_windows.csv (optional)", type=["csv"])
        if st.button("Import uploads", disabled=up_sched is None):
            tmp = dd / "_upload"
            tmp.mkdir(exist_ok=True)
            sched_p = tmp / "schedule_phase2.csv"
            sched_p.write_bytes(up_sched.getvalue())
            cip_p = None
            if up_cip is not None:
                cip_p = tmp / "cip_windows.csv"
                cip_p.write_bytes(up_cip.getvalue())
            cal = import_legacy_schedule(sched_p, cip_p, reference_dir(dd) / "downtimes.csv", anchor)
            backup_file(cal_path, dd)
            save_calendar(cal, cal_path)
            ensure_lines_from_calendar(cal, dd / "lines.csv")
            snapshot_current_schedule(cal, dd)
            st.success(f"Imported {len(cal)} blocks (+ saved version)")
            st.rerun()

# ── Score ───────────────────────────────────────────────────────────────
cal = load_calendar(cal_path)
if cal.empty:
    st.warning("No calendar loaded yet. Import a schedule above.")
    st.stop()

st.write(f"**Blocks loaded:** {len(cal)}  |  "
         + ", ".join(f"{t}: {(cal['block_type']==t).sum()}" for t in sorted(cal['block_type'].unique())))

col_a, col_b = st.columns([1, 3])
with col_a:
    if st.button("Score this week", type="primary", use_container_width=True):
        result = score_calendar(cal, week_label=week_label, data_dir=dd)
        path = save_scorecard(result, dd)
        st.session_state["last_scorecard"] = result.to_dict()
        st.success(f"Saved {path.name}")

result_dict = st.session_state.get("last_scorecard")
if result_dict is None:
    result_dict = score_calendar(cal, week_label=week_label, data_dir=dd).to_dict()

# Current-schedule composite for history deltas
current_live = score_calendar(cal, week_label="official", data_dir=dd)

st.divider()
history = list_scorecards(dd)
if history:
    labels = [
        f"{display_label(h.get('week_label', '?'))} · {h.get('scored_at', '')} · composite={h.get('composite', '—')}"
        for h in history
    ]
    pick = st.selectbox("View scorecard", ["Current / latest"] + labels, index=0)
    if pick != "Current / latest":
        result_dict = history[labels.index(pick)]

render_scorecard(result_dict)

# ── History ─────────────────────────────────────────────────────────────
st.divider()
st.subheader("Prior weeks")
if history:
    st.dataframe(
        scorecard_table(history, baseline_composite=current_live.composite),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.caption("No saved scorecards yet. Click **Score this week** to snapshot history.")

with st.expander("Calendar preview"):
    st.caption(
        "`start_h` / `end_h` are horizon hour offsets from the planning anchor "
        f"({anchor}); `start` / `end` are the same moments as date + time."
    )
    st.dataframe(with_display_times(cal, anchor), use_container_width=True, hide_index=True)
