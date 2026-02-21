# pages/line_rates.py — View / edit line_rates.csv (Demand Planning Line Rates).

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("Demand Planning Line Rates")
st.caption(
    "These are the rates the schedule will be based on **by default**. "
    "These are the rates used by the Demand Planning team when creating the "
    "plan for the plant. To use per-SKU historical rates instead, enable "
    "**SKU-specific rates** on the Run Solver settings page."
)

dd = data_dir()
csv_path = dd / "line_rates.csv"

if not csv_path.exists():
    st.warning(f"File not found: `{csv_path}`")
    st.stop()

df = pd.read_csv(csv_path)
df["line_id"] = pd.to_numeric(df["line_id"], errors="coerce").fillna(0).astype(int)
df["line_name"] = df["line_name"].astype(str)
df["Month"] = pd.to_numeric(df["Month"], errors="coerce").fillna(0).astype(int)
df["rate_kgph"] = pd.to_numeric(df["rate_kgph"], errors="coerce").fillna(0.0)

# Filters
col_f1, col_f2 = st.columns(2)
with col_f1:
    lines = sorted(df["line_name"].dropna().unique().tolist())
    sel_lines = st.multiselect("Filter by line", options=lines, default=[], placeholder="All lines")
with col_f2:
    months = sorted(df["Month"].unique().tolist())
    sel_months = st.multiselect("Filter by month", options=months, default=[], placeholder="All months")

view = df.copy()
if sel_lines:
    view = view[view["line_name"].isin(sel_lines)]
if sel_months:
    view = view[view["Month"].isin(sel_months)]

st.caption(f"Showing {len(view)} of {len(df)} rows")

edited = st.data_editor(
    view,
    use_container_width=True,
    height=min(600, 35 * len(view) + 50),
    disabled=["line_id", "line_name"],
    column_config={
        "line_id": st.column_config.NumberColumn("Line ID"),
        "line_name": st.column_config.TextColumn("Line"),
        "Month": st.column_config.NumberColumn("Month", min_value=1, max_value=12, step=1),
        "rate_kgph": st.column_config.NumberColumn(
            "Rate (kg/h)", min_value=0, step=10,
            help="Production rate in kg per hour for this line during the specified month.",
        ),
    },
    key="line_rates_editor",
)

if st.button("Save changes", type="primary"):
    if sel_lines or sel_months:
        full = pd.read_csv(csv_path)
        full["line_id"] = pd.to_numeric(full["line_id"], errors="coerce").fillna(0).astype(int)
        full.set_index(["line_id", "Month"], inplace=True)
        edited_idx = edited.set_index(["line_id", "Month"])
        full.update(edited_idx)
        full.reset_index(inplace=True)
        full.to_csv(csv_path, index=False)
    else:
        edited.to_csv(csv_path, index=False)
    st.success(f"Saved to `{csv_path.name}`")
