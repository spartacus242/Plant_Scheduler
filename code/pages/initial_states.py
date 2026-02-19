# pages/initial_states.py — Edit InitialStates.csv for Week-0.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("Initial States")
st.caption(
    "Per-line starting conditions: which SKU is loaded, availability hour, "
    "CIP carryover, and long-shutdown flags."
)

dd = data_dir()
csv_path = dd / "initial_states.csv"
w1_path = dd / "week1_initial_states.csv"

if not csv_path.exists():
    st.warning(f"File not found: `{csv_path}`")
    st.stop()

# Option to load Week-1 initial states
if w1_path.exists():
    if st.button("Load Week-1 Initial States as starting point"):
        import shutil
        shutil.copy2(w1_path, csv_path)
        st.success("Loaded Week-1 states into initial_states.csv")
        st.rerun()

df = pd.read_csv(csv_path)

# Cast columns that pandas may read as float/int to str for TextColumn compat
if "last_cip_end_datetime" in df.columns:
    df["last_cip_end_datetime"] = df["last_cip_end_datetime"].fillna("").astype(str)
if "comment" in df.columns:
    df["comment"] = df["comment"].fillna("").astype(str)
if "initial_sku" in df.columns:
    df["initial_sku"] = df["initial_sku"].astype(str)

# Collect known SKUs for dropdown
caps_path = dd / "capabilities_rates.csv"
sku_options = ["CLEAN"]
if caps_path.exists():
    caps = pd.read_csv(caps_path)
    sku_options += sorted(caps["sku"].astype(str).unique().tolist())

edited = st.data_editor(
    df,
    use_container_width=True,
    height=min(600, 35 * len(df) + 50),
    disabled=["line_id", "line_name"],
    column_config={
        "line_id": st.column_config.NumberColumn("Line ID"),
        "line_name": st.column_config.TextColumn("Line"),
        "initial_sku": st.column_config.SelectboxColumn(
            "Initial SKU", options=sku_options,
            help="SKU loaded on the line at planning start. If the first scheduled order matches, no changeover is needed.",
        ),
        "available_from_hour": st.column_config.NumberColumn(
            "Available From (h)", min_value=0, max_value=336, step=1,
            help="Earliest hour the line can begin production. Use this to account for ongoing maintenance or delayed starts.",
        ),
        "long_shutdown_flag": st.column_config.CheckboxColumn(
            "Long Shutdown",
            help="If checked, the line requires extra setup time before starting (e.g. after a weekend shutdown).",
        ),
        "long_shutdown_extra_setup_hours": st.column_config.NumberColumn(
            "Extra Setup (h)", min_value=0, step=1,
            help="Additional setup hours needed before production if long_shutdown_flag is set.",
        ),
        "carryover_run_hours_since_last_cip_at_t0": st.column_config.NumberColumn(
            "CIP Carryover (h)", min_value=0, max_value=119, step=1,
            help="Production hours accumulated since the last CIP. Affects when the next CIP will be required. Higher values mean a CIP is due sooner.",
        ),
        "last_cip_end_datetime": st.column_config.TextColumn(
            "Last CIP End",
            help="Datetime when the last CIP finished (e.g. 2026-02-14 18:00). Used for CIP scheduling continuity.",
        ),
        "comment": st.column_config.TextColumn("Comment"),
    },
    key="init_editor",
)

if st.button("Save initial states", type="primary"):
    edited.to_csv(csv_path, index=False)
    st.success(f"Saved to `{csv_path.name}`")
