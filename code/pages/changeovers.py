# pages/changeovers.py — View / edit changeovers.csv (filterable).

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("Changeovers")
st.caption("SKU-to-SKU changeover setup times (hours). Filter by SKU to manage the large matrix.")

csv_path = data_dir() / "changeovers.csv"
if not csv_path.exists():
    st.warning(f"File not found: `{csv_path}`")
    st.stop()

df = pd.read_csv(csv_path)
# Clean trailing empty columns
df = df.loc[:, ~df.columns.str.startswith("Unnamed")]

all_skus = sorted(set(df["from_sku"].astype(str).unique()) | set(df["to_sku"].astype(str).unique()))

col1, col2 = st.columns(2)
with col1:
    from_filter = st.multiselect("Filter from_sku", options=all_skus, default=[], placeholder="All")
with col2:
    to_filter = st.multiselect("Filter to_sku", options=all_skus, default=[], placeholder="All")

view = df.copy()
if from_filter:
    view = view[view["from_sku"].astype(str).isin(from_filter)]
if to_filter:
    view = view[view["to_sku"].astype(str).isin(to_filter)]

st.caption(f"Showing {len(view)} of {len(df)} rows")

edited = st.data_editor(
    view,
    use_container_width=True,
    height=min(600, 35 * min(len(view), 50) + 50),
    disabled=["from_sku", "to_sku"],
    column_config={
        "from_sku": st.column_config.TextColumn("From SKU"),
        "to_sku": st.column_config.TextColumn("To SKU"),
        "setup_hours": st.column_config.NumberColumn(
            "Setup Hours", min_value=0.0, step=0.5, format="%.1f",
            help="Hours of lost production when switching between these SKUs. Higher values discourage the solver from making this switch.",
        ),
    },
    key="co_editor",
)

if st.button("Save changes", type="primary"):
    if from_filter or to_filter:
        full = pd.read_csv(csv_path)
        full = full.loc[:, ~full.columns.str.startswith("Unnamed")]
        full.set_index(["from_sku", "to_sku"], inplace=True)
        edited_idx = edited.set_index(["from_sku", "to_sku"])
        full.update(edited_idx)
        full.reset_index(inplace=True)
        full.to_csv(csv_path, index=False)
    else:
        edited.to_csv(csv_path, index=False)
    st.success(f"Saved to `{csv_path.name}`")
