# pages/inventory_check.py — BOM, On-Hand, Inbound editors + inventory check.

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

st.header("Inventory Check")
st.caption(
    "Edit Bills of Material, on-hand inventory, and inbound shipments, "
    "then run an inventory feasibility check against the schedule."
)

dd = data_dir()
bom_path = dd / "bom_by_sku.csv"
onhand_path = dd / "on_hand_inventory.csv"
inbound_path = dd / "inbound_inventory.csv"

tab_bom, tab_oh, tab_ib, tab_check = st.tabs(["BOM", "On-Hand", "Inbound", "Run Check"])

# ── BOM tab ─────────────────────────────────────────────────────────────
with tab_bom:
    if bom_path.exists():
        bom_df = pd.read_csv(bom_path)
    else:
        bom_df = pd.DataFrame(columns=["sku", "material_id", "qty_per_unit", "material_description"])
        st.info("No BOM file found. Add rows below to create one.")
    edited_bom = st.data_editor(
        bom_df, num_rows="dynamic", use_container_width=True, key="bom_editor",
        disabled=["sku", "material_id"],
        column_config={
            "sku": st.column_config.TextColumn("SKU"),
            "material_id": st.column_config.TextColumn("Material ID"),
            "qty_per_unit": st.column_config.NumberColumn(
                "Qty/Unit", min_value=0.0, step=0.1, format="%.2f",
                help="Amount of this material consumed per unit of finished product. Used to calculate total material demand.",
            ),
            "material_description": st.column_config.TextColumn("Description"),
        },
    )
    if st.button("Save BOM", key="save_bom"):
        safe_write_csv(edited_bom, bom_path)
        st.success(f"Saved to `{bom_path.name}`")

# ── On-Hand tab ─────────────────────────────────────────────────────────
with tab_oh:
    if onhand_path.exists():
        oh_df = pd.read_csv(onhand_path)
    else:
        oh_df = pd.DataFrame(columns=["material_id", "quantity", "location", "uom", "as_of_date"])
        st.info("No On-Hand file found. Add rows below to create one.")
    edited_oh = st.data_editor(
        oh_df, num_rows="dynamic", use_container_width=True, key="oh_editor",
        disabled=["material_id"],
        column_config={
            "material_id": st.column_config.TextColumn("Material ID"),
            "quantity": st.column_config.NumberColumn(
                "Quantity", min_value=0, step=100,
                help="Current on-hand stock of this material. The inventory check compares this against BOM-driven consumption.",
            ),
        },
    )
    if st.button("Save On-Hand", key="save_oh"):
        safe_write_csv(edited_oh, onhand_path)
        st.success(f"Saved to `{onhand_path.name}`")

# ── Inbound tab ─────────────────────────────────────────────────────────
with tab_ib:
    if inbound_path.exists():
        ib_df = pd.read_csv(inbound_path)
    else:
        ib_df = pd.DataFrame(columns=["material_id", "quantity", "arrival_hour", "arrival_date", "shipment_id", "notes"])
        st.info("No Inbound file found. Add rows below to create one.")
    edited_ib = st.data_editor(
        ib_df, num_rows="dynamic", use_container_width=True, key="ib_editor",
        disabled=["material_id"],
        column_config={
            "material_id": st.column_config.TextColumn("Material ID"),
            "quantity": st.column_config.NumberColumn(
                "Quantity", min_value=0, step=100,
                help="Incoming quantity of this material. Added to on-hand at the arrival hour during the inventory check.",
            ),
            "arrival_hour": st.column_config.NumberColumn(
                "Arrival Hour", min_value=0, step=1,
                help="Planning-horizon hour when this shipment arrives. Material is only available for orders starting after this hour.",
            ),
        },
    )
    if st.button("Save Inbound", key="save_ib"):
        safe_write_csv(edited_ib, inbound_path)
        st.success(f"Saved to `{inbound_path.name}`")

# ── Run Check tab ───────────────────────────────────────────────────────
with tab_check:
    if not bom_path.exists() or not onhand_path.exists():
        st.info("Add BOM and On-Hand data first, then run the check.")
    else:
        schedule_path = dd / "schedule_phase2.csv"
        if not schedule_path.exists():
            st.info("Run the solver first to generate a schedule, then check inventory.")
        elif st.button("Run Inventory Check", type="primary"):
            from inventory_checker import run_inventory_check, results_to_dataframe
            results = run_inventory_check(dd)
            inv_df = results_to_dataframe(results)
            if inv_df.empty:
                st.info("No orders with BOM-defined materials in schedule.")
            else:
                n_flag = (inv_df["status"] == "FLAG").sum()
                n_plan = (inv_df["status"] == "PLAN").sum()
                c1, c2, _ = st.columns([1, 1, 2])
                c1.metric("Plan (OK)", n_plan)
                c2.metric("Flag (review)", n_flag)
                display_cols = ["order_id", "sku", "produced", "start_hour", "status", "message"]
                if "shortfall_detail" in inv_df.columns:
                    display_cols.append("shortfall_detail")
                st.dataframe(
                    inv_df[[c for c in display_cols if c in inv_df.columns]],
                    use_container_width=True,
                    height=min(400, 35 * len(inv_df) + 50),
                )
                if n_flag > 0:
                    st.warning(
                        "Flagged orders have insufficient inventory. "
                        "Adjust demand targets or confirm inbound deliveries."
                    )
