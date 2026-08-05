# pages/data.py -- Data Files: upload, edit and download every input CSV.

from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.data_catalog import CATALOG, CSV_ENCODING, missing_columns, read_csv, status
from helpers.downtime_ui import render_side_downtime_editor
from helpers.paths import data_dir
from helpers.safe_io import safe_write_csv

st.header("Data Files")
st.caption("Replace, edit or download the CSVs Flowstate reads. Every write backs up the old file first.")

dd = data_dir()
st.caption(f"Data folder: `{dd}`  |  backups: `{dd / '_backups'}`")

st.subheader("STEP 1 - Scheduled downtime per side")
with st.expander("Set downtime per line / side (do this before scheduling production)", expanded=False):
    render_side_downtime_editor(dd, key_prefix="data_dt")

st.divider()
st.subheader("All input files")


def backup(path: Path) -> Path:
    """Copy path into data/_backups/<stem>.<timestamp>.csv. Returns the backup path."""
    bdir = dd / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest


for spec in CATALOG:
    info = status(spec, dd)
    badge = "OK" if info["exists"] and not info["error"] else ("MISSING" if not info["exists"] else "ERROR")
    rows = info["rows"] if info["rows"] is not None else "-"
    header = f"[{badge}] {spec.name}  --  {spec.rel()}  ({rows} rows)"

    with st.expander(header):
        st.caption(spec.blurb)
        if spec.key_columns:
            st.caption("Required columns: " + ", ".join(spec.key_columns))

        path = spec.path(dd)

        # (a) Replace the file with an upload.
        up = st.file_uploader(
            "Replace this file",
            type=["csv"],
            key=f"up_{spec.key}",
            label_visibility="collapsed",
        )
        if up is not None:
            raw = up.getvalue()
            try:
                new_df = pd.read_csv(io.BytesIO(raw), encoding=CSV_ENCODING, dtype=str, keep_default_na=False)
            except Exception as exc:
                new_df = None
                st.error(f"Not a readable CSV: {type(exc).__name__}: {exc}")
            if new_df is not None:
                gaps = missing_columns(new_df, spec)
                if gaps:
                    st.error("Missing required column(s): " + ", ".join(gaps))
                else:
                    st.success(f"Parsed OK: {len(new_df)} rows, {len(new_df.columns)} columns.")
                    st.dataframe(new_df.head(10), use_container_width=True, hide_index=True)
                    if st.button("Overwrite " + spec.filename, key=f"ovr_{spec.key}"):
                        if path.exists():
                            b = backup(path)
                            st.caption(f"Backed up to `{b.name}`")
                        safe_write_csv(new_df, path)
                        st.success(f"Replaced {spec.rel()}")
                        st.rerun()

        if not info["exists"]:
            st.warning("File does not exist yet -- upload one above.")
            continue
        if info["error"]:
            st.error(f"Could not read the file: {info['error']}")
            continue

        df = read_csv(spec, dd)

        # (b) Editable grid + Save.
        edited = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            key=f"ed_{spec.key}",
        )
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("Save", key=f"save_{spec.key}"):
                out = pd.DataFrame(edited)
                gaps = missing_columns(out, spec)
                if gaps:
                    st.error("Missing required column(s): " + ", ".join(gaps))
                else:
                    b = backup(path)
                    safe_write_csv(out, path)
                    st.success(f"Saved {spec.rel()} ({len(out)} rows). Backup: {b.name}")
        with c2:
            # (c) Download the file as it is on disk.
            st.download_button(
                "Download",
                data=path.read_bytes(),
                file_name=spec.filename,
                mime="text/csv",
                key=f"dl_{spec.key}",
            )
