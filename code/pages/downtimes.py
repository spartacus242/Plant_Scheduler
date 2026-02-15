# pages/downtimes.py — Add / edit scheduled downtimes.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("Downtimes")
st.caption("Scheduled maintenance windows that block production on a line.")

csv_path = data_dir() / "Downtimes.csv"
caps_path = data_dir() / "Capabilities & Rates.csv"

line_names: list[str] = []
name_to_id: dict[str, int] = {}
if caps_path.exists():
    caps = pd.read_csv(caps_path)
    for _, r in caps.drop_duplicates("line_id").iterrows():
        lid = int(r["line_id"])
        ln = str(r["line_name"])
        line_names.append(ln)
        name_to_id[ln] = lid
    line_names.sort()

if csv_path.exists():
    df = pd.read_csv(csv_path)
    # Ensure line_name column exists (backfill from line_id if needed)
    if "line_name" not in df.columns:
        id_to_name = {v: k for k, v in name_to_id.items()}
        df["line_name"] = df["line_id"].map(id_to_name).fillna("")
else:
    df = pd.DataFrame(columns=["line_name", "start_hour", "end_hour", "reason"])
    st.info("No downtimes file found. Add rows below.")

# Show only user-facing columns (hide line_id)
display_cols = ["line_name", "start_hour", "end_hour", "reason"]
display_df = df[[c for c in display_cols if c in df.columns]].copy()

edited = st.data_editor(
    display_df,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "line_name": st.column_config.SelectboxColumn(
            "Line", options=line_names, required=True,
            help="Select the production line that will be unavailable during this window.",
        ),
        "start_hour": st.column_config.NumberColumn(
            "Start Hour", min_value=0, max_value=336, step=1,
            help="Planning-horizon hour when the downtime begins. The solver will not schedule production on this line during the window.",
        ),
        "end_hour": st.column_config.NumberColumn(
            "End Hour", min_value=0, max_value=336, step=1,
            help="Planning-horizon hour when the downtime ends and the line becomes available again.",
        ),
        "reason": st.column_config.TextColumn("Reason", help="Description of the downtime (e.g. 'Planned maintenance')."),
    },
    key="dt_editor",
)

# Visual preview
if not edited.empty and len(edited.dropna(subset=["line_name", "start_hour", "end_hour"])) > 0:
    with st.expander("Timeline preview", expanded=True):
        import plotly.express as px
        preview = edited.dropna(subset=["line_name", "start_hour", "end_hour"]).copy()
        anchor = pd.Timestamp("2026-02-15 00:00:00")
        preview["Start"] = anchor + pd.to_timedelta(preview["start_hour"].astype(float), unit="h")
        preview["End"] = anchor + pd.to_timedelta(preview["end_hour"].astype(float), unit="h")
        preview["label"] = preview["reason"].fillna("Downtime")
        fig = px.timeline(
            preview, x_start="Start", x_end="End", y="line_name",
            text="label", title="Downtime blocks",
        )
        fig.update_traces(marker_color="salmon")
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(height=max(200, 30 * preview["line_name"].nunique()), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

if st.button("Save downtimes", type="primary"):
    # Derive line_id from line_name for the CSV
    save_df = edited.copy()
    save_df["line_id"] = save_df["line_name"].map(name_to_id)
    # Reorder columns: line_id first for backward compat with scheduler
    out_cols = ["line_id", "line_name", "start_hour", "end_hour", "reason"]
    save_df = save_df[[c for c in out_cols if c in save_df.columns]]
    save_df.to_csv(csv_path, index=False)
    st.success(f"Saved to `{csv_path.name}`")
