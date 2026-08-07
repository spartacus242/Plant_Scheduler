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

from helpers.config import load_toml
from helpers.data_catalog import CATALOG, CSV_ENCODING, missing_columns, read_csv, status
from helpers.downtime_ui import render_side_downtime_editor
from helpers.manual_import_ui import render_manual_import
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


st.divider()
st.subheader("Import the raw corporate AZAP (CSV)")
st.caption(
    "The corporate 'Demand Plan Raw' export (pdp export AZAP) is the source of "
    "truth for demand. Importing it rebuilds `data/reference/demand_plan.csv` "
    "for a 3-week rolling window and re-anchors the planning calendar."
)

with st.expander("Import raw AZAP (CSV or .xlsx)", expanded=False):
    from helpers.azap_import import aggregate, build_demand_plan, read_raw, iso_week
    from helpers.timefmt import week_label

    azap_up = st.file_uploader(
        "Demand Plan Raw (CSV or .xlsx)", type=["csv", "xlsx"],
        key="azap_raw_upload")
    if azap_up is not None:
        import io as _io
        try:
            raw_df = read_raw(_io.BytesIO(azap_up.getvalue()))
            agg, warns, dropped_machine, _ = aggregate(raw_df)
            demand, anchor, dropped_window, weeks = build_demand_plan(agg)
        except Exception as exc:
            st.error(f"Not a readable AZAP export: {type(exc).__name__}: {exc}")
            st.stop()

        iso = [iso_week(w) for w in weeks]
        st.success(
            f"Parsed **{len(raw_df):,}** rows → kept P09–P22, anchored to "
            f"**{anchor}** (WW{iso[0]:02d}), keeping weeks "
            f"**{', '.join(f'WW{w:02d}' for w in iso)}** — **{len(demand)}** "
            f"orders, **{demand['qty_target'].sum():,.0f} kg** total. "
            f"Dropped {dropped_machine:,} non-plant rows and "
            f"{dropped_window:,} beyond the 3-week window.")
        if warns:
            st.warning("Data warnings: " + "; ".join(warns))
        st.warning(
            "Re-anchoring moves `planning_start_date` to "
            f"**{anchor} 00:00:00**. Saved schedules/scorecards under the old "
            "anchor become stale. The current `demand_plan.csv` will be "
            "backed up before overwrite.")
        st.dataframe(demand.head(10), use_container_width=True, hide_index=True)

        if st.button("Write demand_plan.csv + re-anchor", key="azap_commit",
                     type="primary"):
            from helpers.azap_import import import_azap
            ref = dd / "reference"
            old = ref / "demand_plan.csv"
            if old.exists():
                b = backup(old)
                st.caption(f"Backed up old demand plan to `{b.name}`")

            def _set_anchor(s: str) -> None:
                from helpers.paths import toml_path
                tp = toml_path()
                txt = tp.read_text(encoding="utf-8")
                import re as _re
                txt2 = _re.sub(r'planning_start_date\s*=\s*"[^"]*"',
                               f'planning_start_date = "{s}"', txt)
                tp.write_text(txt2, encoding="utf-8")

            res = import_azap(_io.BytesIO(azap_up.getvalue()), ref,
                              update_anchor=_set_anchor)
            st.success(
                f"Wrote {res.rows_kept} orders to demand_plan.csv "
                f"(weeks {', '.join(f'WW{w:02d}' for w in res.iso_weeks)}), "
                f"anchor {res.anchor}. Provenance in demand_plan.source.json.")
            st.rerun()

st.divider()
st.subheader("Import a weekly production schedule (PDF)")
st.caption(
    "Upload the plant's weekly 'Production Schedule' PDF (the VIF print) to bring "
    "the actual line schedule in as a version to compare against, or as the "
    "current baseline.")

with st.expander("Import production schedule PDF", expanded=False):
    from helpers.schedule_pdf_import import parse_schedule_pdf, to_calendar_rows
    from helpers.timefmt import planning_anchor

    pdf_up = st.file_uploader("Production Schedule (.pdf)", type=["pdf"],
                              key="sched_pdf_upload")
    if pdf_up is not None:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
            tf.write(pdf_up.getvalue())
            tmp_pdf = tf.name
        try:
            pres = parse_schedule_pdf(tmp_pdf)
        except Exception as exc:
            st.error(f"Could not parse PDF: {type(exc).__name__}: {exc}")
            st.stop()
        if not pres.blocks:
            st.error("No schedule blocks recognized in this PDF.")
            st.stop()

        # anchor: Monday of the week the PDF covers (earliest date -> its Monday)
        from datetime import datetime as _dt, timedelta as _td
        dates = sorted({_dt.strptime(f"{pres.year}-{b.date}", "%Y-%m/%d")
                        for b in pres.blocks})
        monday = dates[0] - _td(days=dates[0].weekday())
        st.success(
            f"Parsed **{len(pres.blocks)}** blocks "
            f"({sum(1 for b in pres.blocks if b.block_type=='production')} production, "
            f"{sum(1 for b in pres.blocks if b.block_type=='cip')} CIP, "
            f"{sum(1 for b in pres.blocks if b.block_type=='trial')} trial) "
            f"across {len({b.line for b in pres.blocks})} lines, week of "
            f"**{monday.date()}** ({pres.year}).")
        n_prod = sum(1 for b in pres.blocks if b.block_type == "production")
        st.dataframe(
            [{"line": b.line, "date": b.date, "time": b.time, "item": b.item,
              "hours": b.hours, "kg": b.qty_kg, "type": b.block_type}
             for b in pres.blocks[:12]],
            use_container_width=True, hide_index=True)

        mode = st.radio(
            "Add as:",
            ["Version to compare against", "Current baseline (overwrite calendar_blocks.csv)"],
            key="pdf_mode", horizontal=True)
        ver_name = st.text_input("Version name", value=f"WW schedule {monday.date()}",
                                 key="pdf_ver_name")
        if st.button("Import schedule PDF", key="pdf_commit", type="primary"):
            rows = to_calendar_rows(pres, monday)
            import pandas as _pd
            cal = _pd.DataFrame(rows)
            if mode.startswith("Current baseline"):
                old = dd / "calendar_blocks.csv"
                if old.exists():
                    b = backup(old)
                    st.caption(f"Backed up to `{b.name}`")
                cal.to_csv(dd / "calendar_blocks.csv", index=False)
                st.success(f"Wrote {len(cal)} blocks to calendar_blocks.csv.")
            else:
                from helpers.version_manager import upsert_version
                from helpers.scorecard_engine import score_calendar
                sc = score_calendar(cal, week_label=ver_name, data_dir=dd)
                slug = upsert_version(
                    ver_name.lower().replace(" ", "_"), ver_name, cal,
                    sc.to_dict(), dd, source="pdf_import",
                    notes=f"Imported from production schedule PDF, week of {monday.date()}.")
                st.success(f"Saved version `{slug}` - {ver_name}")
            st.rerun()

st.divider()
st.subheader("Upload the planner's manual line schedule")
st.caption(
    "AZAP (the demand plan, data/reference/demand_plan.csv) says which SKUs and how many kg -- "
    "it never assigns lines. The production planner builds the real line schedule himself. "
    "Upload it here to make it the base model."
)
_cfg = load_toml()
_sched = _cfg.get("scheduler", {})
with st.expander("Import manual line schedule (CSV / Excel)", expanded=False):
    render_manual_import(
        dd,
        _sched.get("planning_start_date", "2026-02-15 00:00:00"),
        int(_sched.get("horizon_hours", 336)),
        key="data_manual",
    )

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
