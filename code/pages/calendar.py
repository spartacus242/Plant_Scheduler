# pages/calendar.py — Phase 1 Digital Twin: DnD what-if with live rescoring.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from components.gantt import gantt_calendar
from helpers.calendar_io import (
    calendar_to_gantt_payload,
    ensure_lines_from_calendar,
    gantt_payload_to_calendar,
    load_calendar,
    load_lines,
    save_calendar,
)
from helpers.config import load_toml
from helpers.paths import data_dir, reference_dir
from helpers.scorecard_engine import score_calendar
from helpers.scorecard_ui import render_scorecard
from helpers.version_manager import list_versions, save_version

st.header("Plant Calendar")
st.caption(
    "Phase 1 — Digital Twin. Move blocks and verify the scorecard changes. "
    "Still no auto-scheduling — you're building trust in the model."
)

dd = data_dir()
cal_path = dd / "calendar_blocks.csv"
cfg = load_toml()
sched_cfg = cfg.get("scheduler", {})
cip_cfg = cfg.get("cip", {})

cal = load_calendar(cal_path)
if cal.empty:
    st.warning("No calendar yet. Import a schedule on the **Schedule Scorecard** page.")
    st.stop()

ensure_lines_from_calendar(cal, dd / "lines.csv")
lines_df = load_lines(dd / "lines.csv")
lines = lines_df.copy()
if "active" in lines.columns:
    lines = lines[lines["active"] != False]
lines = lines[["line_id", "line_name"]].to_dict("records")

# Capabilities map line_name -> sku -> rate
caps: dict = {}
caps_path = reference_dir(dd) / "capabilities_rates.csv"
if caps_path.exists():
    cdf = pd.read_csv(caps_path)
    for _, r in cdf.iterrows():
        if int(r.get("capable", 0) or 0) != 1:
            continue
        ln = str(r["line_name"])
        sku = str(r["sku"])
        rate = float(r.get("calc_rate_kgph") or r.get("nominal_rate_kgph") or 0)
        caps.setdefault(ln, {})[sku] = rate

changeovers: dict = {}
co_path = reference_dir(dd) / "changeovers.csv"
if co_path.exists():
    codf = pd.read_csv(co_path)
    for _, r in codf.iterrows():
        changeovers.setdefault(str(r["from_sku"]), {})[str(r["to_sku"])] = float(r.get("setup_hours") or 0)

demand_targets = []
dem_path = reference_dir(dd) / "demand_plan.csv"
if dem_path.exists():
    ddf = pd.read_csv(dem_path)
    for _, r in ddf.iterrows():
        target = float(r.get("qty_target", 0) or 0)
        lo = float(r.get("lower_pct", 0.9) or 0.9)
        hi = float(r.get("upper_pct", 1.1) or 1.1)
        demand_targets.append({
            "order_id": str(r["order_id"]),
            "sku": str(r["sku"]),
            "qty_min": target * lo,
            "qty_max": target * hi,
        })

schedule, windows = calendar_to_gantt_payload(cal)

if "cal_reset_gen" not in st.session_state:
    st.session_state["cal_reset_gen"] = 0

c1, c2, c3 = st.columns(3)
with c1:
    if st.button("Reload from disk", use_container_width=True):
        st.session_state["cal_reset_gen"] += 1
        st.rerun()
with c2:
    save_name = st.text_input("Save as version name", value="Option 1", label_visibility="collapsed")
with c3:
    pass

state = gantt_calendar(
    schedule=schedule,
    cip_windows=windows,
    capabilities=caps,
    changeovers=changeovers,
    demand_targets=demand_targets,
    lines=lines,
    holding_area=st.session_state.get("cal_holding", []),
    config={
        "planning_anchor": sched_cfg.get("planning_start_date", "2026-02-15 00:00:00"),
        "cip_duration_h": int(cip_cfg.get("duration_h", 6)),
        "min_run_hours": int(sched_cfg.get("min_run_hours", 4)),
        "horizon_hours": int(sched_cfg.get("horizon_hours", 336)),
    },
    height=820,
    key=f"gantt_calendar_{st.session_state['cal_reset_gen']}",
)

working = cal
if state and state.get("schedule") is not None:
    working = gantt_payload_to_calendar(state.get("schedule") or [], state.get("cipWindows") or [])
    st.session_state["cal_holding"] = state.get("holdingArea") or []
    if state.get("lastAction"):
        st.caption(f"Last action: {state['lastAction']}")

st.divider()
st.subheader("Live scorecard (same engine as Phase 0)")
live = score_calendar(working, week_label="what-if", data_dir=dd)
render_scorecard(live, show_formulas=False)

b1, b2 = st.columns(2)
with b1:
    if st.button("Save calendar to disk", type="primary", use_container_width=True):
        save_calendar(working, cal_path)
        st.success("Saved calendar_blocks.csv")
with b2:
    if st.button("Save as named version", use_container_width=True):
        try:
            slug = save_version(
                save_name or "Option",
                working,
                live.to_dict(),
                dd,
                source="digital_twin",
            )
            st.success(f"Saved version `{slug}` — see Version Compare")
        except ValueError as e:
            st.error(str(e))

n_ver = len(list_versions(dd))
st.caption(f"{n_ver} / 5 versions saved")
