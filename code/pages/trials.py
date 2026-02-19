# pages/trials.py — Add / edit / remove trial runs.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("Trials")
st.caption(
    "Define trial production runs pinned to specific lines and times. "
    "Provide either `end_datetime` or `target_kgs` (the solver computes the other)."
)

csv_path = data_dir() / "trials.csv"
caps_path = data_dir() / "capabilities_rates.csv"

# Load line names and SKU capabilities for validation hints
line_names = []
capable_skus: dict[str, list[str]] = {}
if caps_path.exists():
    caps = pd.read_csv(caps_path)
    line_names = sorted(caps["line_name"].dropna().unique().tolist())
    for ln in line_names:
        sub = caps[(caps["line_name"] == ln) & (caps["capable"] == 1)]
        capable_skus[ln] = sorted(sub["sku"].astype(str).unique().tolist())

if csv_path.exists():
    df = pd.read_csv(csv_path)
    # Cast numeric-looking columns to str so TextColumn config works
    if "sku" in df.columns:
        df["sku"] = df["sku"].astype(str)
    if "start_datetime" in df.columns:
        df["start_datetime"] = df["start_datetime"].fillna("").astype(str)
    if "end_datetime" in df.columns:
        df["end_datetime"] = df["end_datetime"].fillna("").astype(str)
else:
    df = pd.DataFrame(columns=["line_name", "sku", "start_datetime", "end_datetime", "target_kgs"])
    st.info("No trials file found. Add rows below to create one.")

edited = st.data_editor(
    df,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "line_name": st.column_config.SelectboxColumn(
            "Line", options=line_names, required=True,
            help="Production line for this trial. Must be capable of producing the chosen SKU.",
        ),
        "sku": st.column_config.TextColumn(
            "SKU", required=True,
            help="Product to produce during the trial. Must be capable on the selected line.",
        ),
        "start_datetime": st.column_config.TextColumn(
            "Start Datetime",
            help="When the trial begins (e.g. 2026-02-16 07:00). The solver pins production to start at this time.",
        ),
        "end_datetime": st.column_config.TextColumn(
            "End Datetime",
            help="When the trial ends. Leave empty to let the solver compute duration from target_kgs and the line rate.",
        ),
        "target_kgs": st.column_config.NumberColumn(
            "Target (kg)", min_value=0, step=1000,
            help="Desired production quantity. If end_datetime is empty, the solver calculates the run time from this value and the line rate.",
        ),
    },
    key="trials_editor",
)

# Validation feedback
if not edited.empty:
    with st.expander("Validation", expanded=True):
        for idx, row in edited.iterrows():
            ln = str(row.get("line_name", ""))
            sku = str(row.get("sku", ""))
            if ln and sku and ln in capable_skus:
                if sku not in capable_skus.get(ln, []):
                    st.warning(f"Row {idx}: SKU `{sku}` is not capable on line `{ln}`")
                else:
                    st.success(f"Row {idx}: `{sku}` on `{ln}` — OK")

if st.button("Save trials", type="primary"):
    edited.to_csv(csv_path, index=False)
    st.success(f"Saved {len(edited)} trial(s) to `{csv_path.name}`")
