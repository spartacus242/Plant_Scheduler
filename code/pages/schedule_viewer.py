# pages/schedule_viewer.py — Gantt chart viewer (refactored from gantt_viewer.py).

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir
from gantt_viewer import (
    load_schedule,
    load_cip_windows,
    build_gantt_figure,
    compute_changeovers,
    WEEK1_START_HOUR,
)
from inventory_checker import run_inventory_check, results_to_dataframe

st.header("Schedule Viewer")

# Banner if schedule was manually edited
if st.session_state.get("schedule_source") == "sandbox":
    st.warning("This schedule was manually edited in the Sandbox. Re-run the solver to restore the optimized schedule.")

dd = data_dir()
schedule_path = dd / "schedule_phase2.csv"
cip_path = dd / "cip_windows.csv"

if not schedule_path.exists():
    st.info("No schedule found. Run the solver first.")
    st.stop()

schedule_df = load_schedule(schedule_path)
cip_df = load_cip_windows(cip_path) if cip_path.exists() else None

# ── Sidebar filters ─────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")
    all_lines = sorted(schedule_df["Task"].unique().tolist())
    all_skus = sorted(schedule_df["Resource"].unique().tolist())
    filter_lines = st.multiselect("Lines", options=all_lines, default=all_lines, key="sv_lines")
    filter_skus = st.multiselect("SKUs", options=all_skus, default=all_skus, key="sv_skus")
    filter_week = st.selectbox("Week", ["All", "Week 0", "Week 1"], key="sv_week")
    use_cip = st.checkbox("Show CIP windows", value=True, key="sv_cip")

# Apply filters
filt = schedule_df.copy()
if filter_lines:
    filt = filt[filt["Task"].isin(filter_lines)]
if filter_skus:
    filt = filt[filt["Resource"].isin(filter_skus)]
if filter_week == "Week 0":
    filt = filt[filt["start_hour"] <= WEEK1_START_HOUR]
elif filter_week == "Week 1":
    filt = filt[filt["start_hour"] >= WEEK1_START_HOUR]

filt_cip = None
if use_cip and cip_df is not None and not cip_df.empty and filter_lines:
    filt_cip = cip_df[cip_df["Task"].isin(filter_lines)]

if filt.empty:
    st.info("No data matches the current filters.")
    st.stop()

# ── Gantt ───────────────────────────────────────────────────────────────
highlighted_sku = st.session_state.get("sv_highlighted_sku")
fig = build_gantt_figure(filt, filt_cip, highlighted_sku=highlighted_sku)
st.plotly_chart(fig, use_container_width=True, key="sv_gantt", on_select="rerun", selection_mode="points")

# Selection handling
sel = st.session_state.get("sv_gantt")
if sel and isinstance(sel, dict):
    inner = sel.get("selection", sel)
    pts = (inner.get("points", []) if isinstance(inner, dict) else []) or []
    if pts and isinstance(pts[0], dict):
        cn = pts[0].get("curveNumber", pts[0].get("curve_number"))
        if cn is not None and cn < len(fig.data):
            new_sku = fig.data[cn].name
            if st.session_state.get("sv_highlighted_sku") != new_sku:
                st.session_state["sv_highlighted_sku"] = new_sku
                st.rerun()
    elif highlighted_sku:
        st.session_state.pop("sv_highlighted_sku", None)
        st.rerun()
if highlighted_sku:
    if st.button("Clear SKU highlight"):
        st.session_state.pop("sv_highlighted_sku", None)
        st.rerun()

# ── Export buttons ──────────────────────────────────────────────────────
c1, c2, _ = st.columns([1, 1, 3])
with c1:
    try:
        png = fig.to_image(format="png", width=1800, height=max(600, 30 * len(all_lines)), scale=2)
        st.download_button("Download PNG", data=png, file_name="gantt.png", mime="image/png")
    except Exception:
        st.caption("Install kaleido for PNG export")
with c2:
    try:
        pdf = fig.to_image(format="pdf", width=1800, height=max(600, 30 * len(all_lines)), scale=2)
        st.download_button("Download PDF", data=pdf, file_name="gantt.pdf", mime="application/pdf")
    except Exception:
        st.caption("Install kaleido for PDF export")

# ── Changeovers ─────────────────────────────────────────────────────────
total_co, co_df = compute_changeovers(filt)
st.caption("Changeovers")
mc, mt = st.columns([1, 4])
mc.metric("Total", total_co)
mt.dataframe(
    co_df[["line_name", "changeovers"]].rename(columns={"line_name": "Line", "changeovers": "CO"}),
    use_container_width=True, height=min(120, 28 * min(5, len(co_df))),
)

# ── Validation report ───────────────────────────────────────────────────
val_path = dd / "validation_report.txt"
if val_path.exists():
    with st.expander("Validation report"):
        st.code(val_path.read_text(encoding="utf-8"), language="text")

with st.expander("Schedule summary"):
    st.dataframe(
        filt[["line_name", "order_id", "sku", "start_dt", "end_dt", "run_hours"]],
        use_container_width=True,
    )

# ── Inventory ───────────────────────────────────────────────────────────
st.divider()
bom_path = dd / "BOM_by_SKU.csv"
onhand_path = dd / "OnHand_Inventory.csv"
if bom_path.exists() and onhand_path.exists():
    st.subheader("Inventory validation")
    results = run_inventory_check(dd)
    inv_df = results_to_dataframe(results)
    if not inv_df.empty:
        nf = (inv_df["status"] == "FLAG").sum()
        np_ = (inv_df["status"] == "PLAN").sum()
        ca, cb, _ = st.columns([1, 1, 2])
        ca.metric("Plan (OK)", np_)
        cb.metric("Flag (review)", nf)
        st.dataframe(inv_df, use_container_width=True, height=min(300, 35 * len(inv_df) + 50))

# ── Open in Sandbox ─────────────────────────────────────────────────────
st.divider()
if st.button("Open in Sandbox", type="secondary", use_container_width=True):
    st.session_state["sandbox_schedule"] = schedule_df.to_dict("records")
    st.switch_page("pages/sandbox.py")
