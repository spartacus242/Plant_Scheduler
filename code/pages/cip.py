# pages/cip.py — View / edit per-line CIP intervals (line_cip_hrs.csv).

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("CIP Intervals")
st.caption(
    "Per-line Clean-In-Place intervals. Each line must complete a CIP within "
    "the specified number of production hours. Lines not listed here fall back "
    "to the global CIP interval configured in `flowstate.toml`."
)

dd = data_dir()
csv_path = dd / "line_cip_hrs.csv"

if not csv_path.exists():
    st.warning(f"File not found: `{csv_path}`")
    st.stop()

df = pd.read_csv(csv_path)
df["line_id"] = pd.to_numeric(df["line_id"], errors="coerce").fillna(0).astype(int)
df["line_name"] = df["line_name"].astype(str)
df["max_cip_hrs"] = pd.to_numeric(df["max_cip_hrs"], errors="coerce").fillna(120).astype(int)

edited = st.data_editor(
    df,
    use_container_width=True,
    height=min(600, 35 * len(df) + 50),
    disabled=["line_id", "line_name"],
    column_config={
        "line_id": st.column_config.NumberColumn("Line ID"),
        "line_name": st.column_config.TextColumn("Line"),
        "max_cip_hrs": st.column_config.NumberColumn(
            "Max Hours Between CIPs",
            min_value=24, max_value=336, step=1,
            help="Maximum consecutive production hours before a CIP is required on this line.",
        ),
    },
    key="cip_editor",
)

if st.button("Save changes", type="primary"):
    edited.to_csv(csv_path, index=False)
    st.success(f"Saved to `{csv_path.name}`")
