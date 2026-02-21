# pages/capabilities.py — View / edit capabilities_rates.csv.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir
from helpers.safe_io import safe_write_csv

st.header("Capabilities")
st.caption("Line-SKU capability matrix and production rates (kg/h). **Rate** and **Capable** columns are editable — line IDs, names, and SKUs are locked.")

dd = data_dir()
csv_path = dd / "capabilities_rates.csv"
if not csv_path.exists():
    st.warning(f"File not found: `{csv_path}`")
    st.stop()

df = pd.read_csv(csv_path)
df["line_id"] = pd.to_numeric(df["line_id"], errors="coerce").fillna(0).astype(int)
df["sku"] = df["sku"].astype(str)
df["capable"] = pd.to_numeric(df["capable"], errors="coerce").fillna(0).astype(int)
# Support both old (rate_uph) and new (calc_rate_kgph) column names
if "calc_rate_kgph" not in df.columns and "rate_uph" in df.columns:
    df = df.rename(columns={"rate_uph": "calc_rate_kgph"})

# Join SKU descriptions from sku_info.csv
sku_info_path = dd / "sku_info.csv"
if sku_info_path.exists():
    si = pd.read_csv(sku_info_path)
    si["sku"] = si["sku"].astype(str)
    desc_map = dict(zip(si["sku"], si["ediact_sku_description"].fillna("")))
    df.insert(df.columns.get_loc("sku") + 1, "sku_description", df["sku"].map(desc_map).fillna(""))

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
    if not view.empty and "calc_rate_kgph" in view.columns:
        pivot = view.pivot_table(
            index="line_name", columns="sku", values="calc_rate_kgph", fill_value=0,
        )
        st.dataframe(pivot, use_container_width=True, height=min(400, 35 * len(pivot) + 50))

st.divider()
st.caption(f"Showing {len(view)} of {len(df)} rows")
st.info(
    "Editing the **Nominal Rate** or **Rate** columns here will **not** change the rates "
    "used for the schedule unless you select **Enable SKU-specific rates** on the "
    "Run Solver settings page."
)

# Determine which columns exist for the editor
disabled_cols = [c for c in ["line_id", "line_name", "sku", "sku_description", "nominal_rate_kgph"] if c in view.columns]
col_config: dict = {
    "line_id": st.column_config.NumberColumn("Line ID"),
    "line_name": st.column_config.TextColumn("Line"),
    "sku": st.column_config.TextColumn("SKU"),
    "sku_description": st.column_config.TextColumn("Description"),
    "capable": st.column_config.SelectboxColumn(
        "Capable", options=[0, 1], required=True,
        help="1 = line can produce this SKU, 0 = not capable.",
    ),
    "calc_rate_kgph": st.column_config.NumberColumn(
        "Rate (kg/h)", min_value=0, step=10,
        help="Calculated production rate in kg per hour for this line/SKU combination.",
    ),
    "nominal_rate_kgph": st.column_config.NumberColumn(
        "Nominal Rate (kg/h)", min_value=0, step=10,
        help="Nominal (nameplate) production rate. Read-only reference value.",
    ),
}

edited = st.data_editor(
    view,
    use_container_width=True,
    height=min(600, 35 * len(view) + 50),
    disabled=disabled_cols,
    column_config=col_config,
    key="caps_editor",
)

if st.button("Save changes", type="primary"):
    to_save = edited.drop(columns=["sku_description"], errors="ignore")
    if sel_lines or sel_skus:
        full = pd.read_csv(csv_path)
        full["line_id"] = pd.to_numeric(full["line_id"], errors="coerce").fillna(0).astype(int)
        if "calc_rate_kgph" not in full.columns and "rate_uph" in full.columns:
            full = full.rename(columns={"rate_uph": "calc_rate_kgph"})
        full.set_index(["line_id", "sku"], inplace=True)
        edited_idx = to_save.set_index(["line_id", "sku"])
        full.update(edited_idx)
        full.reset_index(inplace=True)
        safe_write_csv(full, csv_path)
    else:
        safe_write_csv(to_save, csv_path)
    st.success(f"Saved to `{csv_path.name}`")
