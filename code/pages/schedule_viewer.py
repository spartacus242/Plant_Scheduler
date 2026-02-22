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
    compute_changeover_details,
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
        export_h = min(4000, max(600, 30 * len(all_lines)))
        png = fig.to_image(format="png", width=1800, height=export_h, scale=2)
        st.download_button("Download PNG", data=png, file_name="gantt.png", mime="image/png")
    except (ImportError, ValueError, OSError, RuntimeError) as exc:
        st.caption(f"PNG export unavailable: {exc}")
with c2:
    try:
        export_h = min(4000, max(600, 30 * len(all_lines)))
        pdf = fig.to_image(format="pdf", width=1800, height=export_h, scale=2)
        st.download_button("Download PDF", data=pdf, file_name="gantt.pdf", mime="application/pdf")
    except (ImportError, ValueError, OSError, RuntimeError) as exc:
        st.caption(f"PDF export unavailable: {exc}")

# ── Changeovers ─────────────────────────────────────────────────────────
total_co, co_df = compute_changeovers(filt)
st.caption("Changeovers")
mc, mt = st.columns([1, 4])
mc.metric("Total", total_co)
mt.dataframe(
    co_df[["line_name", "changeovers"]].rename(columns={"line_name": "Line", "changeovers": "CO"}),
    use_container_width=True, height=min(120, 28 * min(5, len(co_df))),
)

# Detailed changeover breakdown by type
chg_csv = dd / "changeovers.csv"
if chg_csv.exists():
    with st.expander("Changeover breakdown by type", expanded=False):
        detail_df = compute_changeover_details(filt, changeovers_path=chg_csv, cip_df=cip_df)
        if not detail_df.empty:
            totals = detail_df[["ttp", "ffs", "topload", "casepacker", "conv_to_org", "cinn_to_non"]].sum()
            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("TTP", int(totals["ttp"]))
            m2.metric("FFS", int(totals["ffs"]))
            m3.metric("Topload", int(totals["topload"]))
            m4.metric("Casepacker", int(totals["casepacker"]))
            m5.metric("Conv to Org", int(totals["conv_to_org"]))
            m6.metric("Cinn to Non-Cinn", int(totals["cinn_to_non"]))
            st.dataframe(
                detail_df.rename(columns={
                    "line_name": "Line", "changeovers": "Total CO",
                    "ttp": "TTP", "ffs": "FFS", "topload": "Topload",
                    "casepacker": "Casepacker", "conv_to_org": "Conv→Org",
                    "cinn_to_non": "Cinn→Non-Cinn",
                }),
                use_container_width=True,
                hide_index=True,
            )

# ── Idle-gap KPIs ──────────────────────────────────────────────────────
idle_kpi_path = dd / "idle_kpis.csv"
if idle_kpi_path.exists():
    with st.expander("Idle-gap KPIs per line", expanded=False):
        idle_df = pd.read_csv(idle_kpi_path)
        st.dataframe(idle_df, use_container_width=True, hide_index=True)

# ── Validation report ───────────────────────────────────────────────────
val_path = dd / "validation_report.txt"
if val_path.exists():
    with st.expander("Validation report"):
        st.code(val_path.read_text(encoding="utf-8"), language="text")

with st.expander("Schedule summary"):
    summary_cols = ["line_name", "order_id", "sku", "sku_description", "start_dt", "end_dt", "run_hours"]
    avail_cols = [c for c in summary_cols if c in filt.columns]
    summary_df = filt[avail_cols].copy()
    if cip_df is not None and not cip_df.empty:
        cip_table = cip_df.copy()
        if "line_name" not in cip_table.columns:
            cip_table["line_name"] = cip_table["Task"]
        cip_table["order_id"] = "CIP"
        cip_table["sku"] = "CIP"
        cip_table["sku_description"] = ""
        cip_table["start_dt"] = cip_table["Start"].dt.strftime("%Y-%m-%d %H:%M")
        cip_table["end_dt"] = cip_table["Finish"].dt.strftime("%Y-%m-%d %H:%M")
        cip_table["run_hours"] = (cip_table["Finish"] - cip_table["Start"]).dt.total_seconds() / 3600
        cip_avail = [c for c in avail_cols if c in cip_table.columns]
        summary_df = pd.concat([summary_df[avail_cols], cip_table[cip_avail]], ignore_index=True)
    st.dataframe(
        summary_df.sort_values(["line_name", "start_dt"]),
        use_container_width=True,
        hide_index=True,
    )

# ── Inventory ───────────────────────────────────────────────────────────
st.divider()
bom_path = dd / "bom_by_sku.csv"
onhand_path = dd / "on_hand_inventory.csv"
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
