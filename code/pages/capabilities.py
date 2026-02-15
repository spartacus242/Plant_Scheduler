# pages/capabilities.py — View / edit Capabilities & Rates.csv.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("Capabilities & Rates")
st.caption("Line-SKU capability matrix and production rates (UPH). **Rate** and **Capable** columns are editable — line IDs, names, and SKUs are locked.")

csv_path = data_dir() / "Capabilities & Rates.csv"
if not csv_path.exists():
    st.warning(f"File not found: `{csv_path}`")
    st.stop()

df = pd.read_csv(csv_path)

# Filters to manage the large table
col_f1, col_f2 = st.columns(2)
with col_f1:
    lines = sorted(df["line_name"].dropna().unique().tolist())
    sel_lines = st.multiselect("Filter by line", options=lines, default=[], placeholder="All lines")
with col_f2:
    skus = sorted(df["sku"].astype(str).unique().tolist())
    sel_skus = st.multiselect("Filter by SKU", options=skus, default=[], placeholder="All SKUs")

view = df.copy()
if sel_lines:
    view = view[view["line_name"].isin(sel_lines)]
if sel_skus:
    view = view[view["sku"].astype(str).isin(sel_skus)]

# Pivot view
with st.expander("Pivot view (lines x SKUs)", expanded=False):
    if not view.empty:
        pivot = view.pivot_table(
            index="line_name", columns="sku", values="rate_uph", fill_value=0,
        )
        st.dataframe(pivot, use_container_width=True, height=min(400, 35 * len(pivot) + 50))

st.divider()
st.caption(f"Showing {len(view)} of {len(df)} rows")

edited = st.data_editor(
    view,
    use_container_width=True,
    height=min(600, 35 * len(view) + 50),
    disabled=["line_id", "line_name", "sku"],
    column_config={
        "line_id": st.column_config.NumberColumn("Line ID"),
        "line_name": st.column_config.TextColumn("Line"),
        "sku": st.column_config.TextColumn("SKU"),
        "capable": st.column_config.SelectboxColumn(
            "Capable", options=[0, 1], required=True,
            help="1 = line can produce this SKU, 0 = not capable. Setting to 0 prevents the solver from assigning this SKU to this line.",
        ),
        "rate_uph": st.column_config.NumberColumn(
            "Rate (UPH)", min_value=0, step=50,
            help="Units produced per hour on this line for this SKU. Changing the rate affects how long production blocks last for a given quantity.",
        ),
    },
    key="caps_editor",
)

if st.button("Save changes", type="primary"):
    if sel_lines or sel_skus:
        # Merge edits back into full dataframe
        full = pd.read_csv(csv_path)
        full.set_index(["line_id", "sku"], inplace=True)
        edited_idx = edited.set_index(["line_id", "sku"])
        full.update(edited_idx)
        full.reset_index(inplace=True)
        full.to_csv(csv_path, index=False)
    else:
        edited.to_csv(csv_path, index=False)
    st.success(f"Saved to `{csv_path.name}`")
