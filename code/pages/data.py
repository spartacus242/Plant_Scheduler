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


# ── Line/SKU capability check (manprg is ground truth) ────────────────────
# If manprg shows an SKU on a line the capabilities table doesn't allow, the
# table is out of date — flag it and offer a one-click fix (user decision B).
try:
    from helpers.capability_check import (
        check_capabilities,
        fix_rows_for,
        load_capabilities,
        load_manprg_mos,
    )
    _caps_df = load_capabilities(dd / "reference" / "capabilities_rates.csv")
    _cfg_local = load_toml()
    _ds_cfg = _cfg_local.get("datasources", {})
    _mp_paths = [p.strip() for p in str(_ds_cfg.get("manprg_files", "")).split(";")
                 if p.strip()] or [str(dd / "reference" / "manprg.txt"),
                                   str(dd / "reference" / "manprg2.txt")]
    _mos = load_manprg_mos(_mp_paths)
    _cap_res = check_capabilities(_caps_df, _mos)
    if _cap_res.count:
        st.warning(
            f"⚠️ **{_cap_res.count} Line/SKU capability conflict(s)** — manprg shows "
            "these SKUs running on lines the capabilities table does not allow. "
            "The plant physically ran them, so the table is out of date."
        )
        st.dataframe(_cap_res.as_frame(), use_container_width=True, hide_index=True)
        _fix = st.button("Add these manprg-proven SKU/line pairs to capabilities",
                         type="primary")
        if _fix:
            # default rate for NEW rows (SKU_MISSING): average rate of all
            # SKUs on each proven line (user decision) — existing rows keep
            # their rate and just flip capable=1.
            _default_by_line = {
                ln: float(_caps_df.loc[
                    (_caps_df["line_name"] == ln)
                    & (pd.to_numeric(_caps_df.get("capable", 0),
                                     errors="coerce").fillna(0) == 1),
                    "calc_rate_kgph"].mean() or 0.0)
                for ln in sorted({c.line_name for c in _cap_res.conflicts})
            }
            _changed = fix_rows_for(_caps_df, _cap_res.conflicts,
                                    default_rate=0.0)
            # apply the per-line average default where fix_rows_for left 0
            _zero_rate = (_changed["capable"].astype(int) == 1) \
                & (_changed["calc_rate_kgph"].astype(float) == 0.0)
            if _zero_rate.any():
                _changed.loc[_zero_rate, "calc_rate_kgph"] = [
                    _default_by_line.get(ln, 0.0)
                    for ln in _changed.loc[_zero_rate, "line_name"]]
            # Merge: flip capable / set rate on the existing rows, append new
            # (sku, line) pairs that were missing entirely.
            _out = _caps_df.copy()
            _out = _out.set_index(["sku", "line_name"])
            for _, r in _changed.iterrows():
                sku_l = (r["sku"], r["line_name"])
                if sku_l in _out.index:
                    _out.loc[sku_l, "capable"] = int(r["capable"])
                    _out.loc[sku_l, "calc_rate_kgph"] = float(r["calc_rate_kgph"])
                else:
                    _new_row = {c: float("nan") for c in _out.columns}
                    if "line_id" in _new_row:
                        _lid_map = _caps_df.drop_duplicates("line_name") \
                            .set_index("line_name")["line_id"]
                        _new_row["line_id"] = int(
                            _lid_map.get(r["line_name"], 99))
                    _new_row["capable"] = int(r["capable"])
                    _new_row["calc_rate_kgph"] = float(r["calc_rate_kgph"])
                    _out.loc[sku_l] = _new_row
            _out = _out.reset_index()
            if "line_id" in _caps_df.columns:
                _lid_map = _caps_df.drop_duplicates("line_name") \
                    .set_index("line_name")["line_id"]
                _out["line_id"] = _out["line_name"].map(_lid_map).fillna(99).astype(int)
                _out = _out[["line_id", "sku", "line_name", "capable",
                             "calc_rate_kgph"]]
            _bdir = dd / "_backups"
            _bdir.mkdir(parents=True, exist_ok=True)
            _dst = _bdir / f"capabilities_rates.{datetime.now():%Y%m%d-%H%M%S}.csv"
            _dst.write_bytes((dd / "reference" / "capabilities_rates.csv").read_bytes())
            _out.to_csv(dd / "reference" / "capabilities_rates.csv", index=False)
            st.success(
                f"Updated capabilities_rates.csv ({len(_changed)} rows changed). "
                "Backup saved. Reloading…")
            st.rerun()
    else:
        st.caption("✅ Line/SKU capability check: manprg SKU/line pairs all match "
                   "the capabilities table.")
except Exception as _cap_exc:  # noqa: BLE001
    st.caption(f"Capability check unavailable: {_cap_exc}")


def backup(path: Path) -> Path:
    """Copy path into data/_backups/<stem>.<timestamp>.csv. Returns the backup path."""
    bdir = dd / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest


st.divider()
st.subheader("Import the demand plan (demand_plan_summary.csv)")
st.caption(
    "The demand source of truth is the planner's summary file: **Week, Product, "
    "kg_tons** — no machine column, no Hours field. Importing it rebuilds "
    "`data/reference/demand_plan.csv` (the canonical demand the scorecard, "
    "calendar and solver all read) and re-anchors the planning calendar to the "
    "Monday of the file's earliest week."
)

with st.expander("Import demand_plan_summary.csv", expanded=True):
    from helpers.demand_summary_import import import_summary as _imps
    sum_up = st.file_uploader("demand_plan_summary.csv", type=["csv"],
                              key="summary_csv_upload",
                              help="Columns: Week (ISO), Product (SKU), kg_tons")
    if sum_up is not None:
        try:
            _payload = sum_up.getvalue()
            # Write to a temp path; the importer reads a path
            import tempfile as _tf
            with _tf.NamedTemporaryFile(delete=False, suffix=".csv") as _t:
                _t.write(_payload)
                _tmp = _t.name
            dem, meta = _imps(_tmp)
        except Exception as exc:
            st.error(f"Not a readable summary: {type(exc).__name__}: {exc}")
        else:
            st.caption(f"{meta.rows} orders, {len(meta.skus)} SKUs, "
                       f"weeks {meta.weeks}, {meta.warnings or 'no warnings'}")
            st.dataframe(dem.head(8), use_container_width=True, hide_index=True)
            if meta.warnings:
                for w in meta.warnings:
                    st.warning(w)
            if meta.anchor is not None:
                st.warning(
                    "Re-anchoring moves `planning_start_date` to "
                    f"**{meta.anchor.strftime('%Y-%m-%d %H:%M:%S')}** "
                    f"(Monday of ISO week {meta.anchor_iso_week}). Saved "
                    "schedules/scorecards under the old anchor become stale. "
                    "The current `demand_plan.csv` will be backed up before overwrite."
                )
            if st.button("Write to demand_plan.csv + re-anchor", key="write_summary",
                         type="primary", disabled=not len(dem)):
                ref_dir = reference_dir(dd)
                dem_path = ref_dir / "demand_plan.csv"
                if dem_path.exists():
                    b = backup(dem_path)
                    st.caption(f"Backed up old demand plan to `{b.name}`")

                def _set_anchor(s: str) -> None:
                    from helpers.paths import toml_path
                    tp = toml_path()
                    txt = tp.read_text(encoding="utf-8")
                    import re as _re
                    txt2 = _re.sub(r'planning_start_date\s*=\s*"[^"]*"',
                                   f'planning_start_date = "{s}"', txt)
                    tp.write_text(txt2, encoding="utf-8")

                dem2, meta2 = _imps(_tmp, update_anchor=_set_anchor)
                dem2.to_csv(dem_path, index=False)
                import json as _json
                (ref_dir / "demand_plan.source.json").write_text(
                    _json.dumps({
                        "source": "demand_plan_summary.csv",
                        "imported": pd.Timestamp.now().isoformat(),
                        "rows": meta2.rows,
                        "weeks": [int(w) for w in meta2.weeks],
                        "skus": len(meta2.skus),
                        "anchor": meta2.anchor.strftime("%Y-%m-%d %H:%M:%S") if meta2.anchor else None,
                        "anchor_iso_week": meta2.anchor_iso_week,
                    }, indent=2),
                    encoding="utf-8")
                st.success(f"Wrote {meta2.rows} orders to demand_plan.csv "
                           f"(weeks {[int(w) for w in meta2.weeks]}), "
                           f"anchor {meta2.anchor.strftime('%Y-%m-%d') if meta2.anchor else '—'}.")
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
