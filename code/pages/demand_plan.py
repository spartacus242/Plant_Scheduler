# pages/demand_plan.py — View / edit DemandPlan.csv.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("Demand Plan")
st.caption("Review and edit order demand. Changes are saved back to `DemandPlan.csv`.")

csv_path = data_dir() / "DemandPlan.csv"
if not csv_path.exists():
    st.warning(f"File not found: `{csv_path}`")
    st.stop()

df = pd.read_csv(csv_path)

# Cast sku to str so TextColumn config works (pandas may infer as int)
if "sku" in df.columns:
    df["sku"] = df["sku"].astype(str)

# Summary metrics
w0 = df[df.get("due_end_hour", pd.Series(dtype=float)).fillna(999).astype(int) <= 167] if "due_end_hour" in df.columns else df[df.get("week_index", pd.Series(dtype=float)).fillna(0).astype(int) == 0]
w1 = df.drop(w0.index)
col1, col2, col3 = st.columns(3)
col1.metric("Total orders", len(df))
col2.metric("Week-0", len(w0))
col3.metric("Week-1", len(w1))

st.divider()

edited = st.data_editor(
    df,
    num_rows="dynamic",
    use_container_width=True,
    height=min(600, 35 * len(df) + 50),
    disabled=["order_id"],
    column_config={
        "order_id": st.column_config.TextColumn("Order ID", help="Auto-generated from SKU and week index. Cannot be edited directly."),
        "sku": st.column_config.TextColumn("SKU", help="Product identifier. Set this when adding a new row."),
        "week_index": st.column_config.NumberColumn("Week", help="0 = Week-0 (first 168 hours), 1 = Week-1 (hours 168-335)."),
        "priority": st.column_config.SelectboxColumn(
            "Priority", options=[1, 2, 3], required=True,
            help="1 = highest, 3 = lowest. Higher-priority orders are scheduled first by the solver.",
        ),
        "qty_target": st.column_config.NumberColumn(
            "Qty Target", min_value=0, step=100,
            help="Target production quantity in units. The solver tries to produce between lower% and upper% of this value.",
        ),
        "lower_pct": st.column_config.NumberColumn(
            "Lower %", min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
            help="Minimum acceptable fraction of qty_target (e.g. 0.90 = 90%). The solver penalizes solutions below this.",
        ),
        "upper_pct": st.column_config.NumberColumn(
            "Upper %", min_value=0.0, max_value=5.0, step=0.01, format="%.2f",
            help="Maximum acceptable fraction of qty_target (e.g. 1.10 = 110%). Production above this is flagged as overproduction.",
        ),
        "due_start_hour": st.column_config.NumberColumn(
            "Due Start (h)", min_value=0, step=1,
            help="Earliest planning-horizon hour when production for this order can begin.",
        ),
        "due_end_hour": st.column_config.NumberColumn(
            "Due End (h)", min_value=0, step=1,
            help="Latest planning-horizon hour by which production must finish.",
        ),
    },
    key="demand_editor",
)

if st.button("Save changes", type="primary"):
    # Auto-populate order_id for new rows
    for i, row in edited.iterrows():
        if pd.isna(row.get("order_id")) or str(row.get("order_id", "")).strip() == "":
            sku = str(row.get("sku", ""))
            wi = int(row.get("week_index", 0)) if pd.notna(row.get("week_index")) else 0
            edited.at[i, "order_id"] = f"{sku}-W{wi}"
    edited.to_csv(csv_path, index=False)
    st.success(f"Saved {len(edited)} rows to `{csv_path.name}`")
