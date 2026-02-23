# pages/export.py — Export finalized schedule (CSV / Excel / PDF / ZIP).

from __future__ import annotations
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir, schedule_provenance_label, load_schedule_meta

st.header("Export Schedule")

dd = data_dir()
schedule_path = dd / "schedule_phase2.csv"
produced_path = dd / "produced_vs_bounds.csv"
cip_path = dd / "cip_windows.csv"
val_path = dd / "validation_report.txt"
toml_path = BASE_DIR.parent / "flowstate.toml"

if not schedule_path.exists():
    st.info("No schedule to export. Run the solver first.")
    st.stop()

# ── Provenance banner ───────────────────────────────────────────────────
_prov = schedule_provenance_label(dd)
if _prov:
    st.caption(_prov)

st.divider()

# ── Export options ──────────────────────────────────────────────────────
opt_col1, opt_col2 = st.columns(2)
with opt_col1:
    include_cip = st.checkbox("Include CIP blocks in schedule", value=True, key="export_include_cip")
with opt_col2:
    meta = load_schedule_meta(dd)
    include_settings = st.checkbox(
        "Include solver weights & settings",
        value=not meta.get("edited", False),
        key="export_include_settings",
        help="Attach flowstate.toml configuration. Disabled by default for manually edited schedules.",
    )


def _schedule_df(with_cip: bool) -> pd.DataFrame:
    """Load schedule, optionally merging CIP rows.  Always sorted by line_id, start_dt."""
    sched = pd.read_csv(schedule_path)

    # Determine planning anchor for hour→datetime conversion
    try:
        import tomli
        with open(toml_path, "rb") as _f:
            _anchor_str = tomli.load(_f).get("scheduler", {}).get("planning_start_date", "2026-02-15 00:00:00")
    except (ImportError, OSError):
        _anchor_str = "2026-02-15 00:00:00"
    _anchor = pd.Timestamp(_anchor_str)

    if with_cip and cip_path.exists():
        cip = pd.read_csv(cip_path)
        cip["order_id"] = "CIP"
        cip["sku"] = "CIP"
        cip["sku_description"] = ""
        cip["is_trial"] = False
        cip["run_hours"] = cip["end_hour"] - cip["start_hour"]
        cip["start_dt"] = (_anchor + pd.to_timedelta(cip["start_hour"], unit="h")).dt.strftime("%Y-%m-%d %H:%M:%S")
        cip["end_dt"] = (_anchor + pd.to_timedelta(cip["end_hour"], unit="h")).dt.strftime("%Y-%m-%d %H:%M:%S")
        common = [c for c in sched.columns if c in cip.columns]
        sched = pd.concat([sched[common], cip[common]], ignore_index=True)

    sort_cols = [c for c in ["line_id", "start_dt"] if c in sched.columns]
    if sort_cols:
        sched = sched.sort_values(sort_cols)
    return sched


def _settings_bytes() -> bytes | None:
    """Read flowstate.toml as bytes, or None if missing."""
    if toml_path.exists():
        return toml_path.read_bytes()
    return None


st.divider()

# ── Individual CSV downloads ────────────────────────────────────────────
st.subheader("CSV Downloads")
c1, c2, c3, c4 = st.columns(4)

with c1:
    csv_df = _schedule_df(include_cip)
    st.download_button(
        "Schedule CSV",
        data=csv_df.to_csv(index=False).encode("utf-8"),
        file_name="schedule_phase2.csv",
        mime="text/csv",
    )

with c2:
    if produced_path.exists():
        st.download_button("Demand Adherence CSV", data=produced_path.read_bytes(),
                           file_name="produced_vs_bounds.csv", mime="text/csv")

with c3:
    if cip_path.exists():
        st.download_button("CIP Windows CSV", data=cip_path.read_bytes(),
                           file_name="cip_windows.csv", mime="text/csv")

with c4:
    if include_settings:
        settings_data = _settings_bytes()
        if settings_data:
            st.download_button("Settings (TOML)", data=settings_data,
                               file_name="flowstate.toml", mime="application/toml")

# ── Excel workbook ──────────────────────────────────────────────────────
st.divider()
st.subheader("Excel Workbook")
st.caption("Combined workbook with Schedule, Demand Adherence, CIP Windows, and Changeover Summary.")

if st.button("Generate Excel", key="gen_xlsx"):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Info sheet with provenance
        info_rows = []
        if _prov:
            info_rows.append({"Key": "Provenance", "Value": _prov})
        if info_rows:
            pd.DataFrame(info_rows).to_excel(writer, sheet_name="Info", index=False)

        _schedule_df(include_cip).to_excel(writer, sheet_name="Schedule", index=False)

        if produced_path.exists():
            pd.read_csv(produced_path).to_excel(writer, sheet_name="Demand Adherence", index=False)

        if cip_path.exists():
            pd.read_csv(cip_path).to_excel(writer, sheet_name="CIP Windows", index=False)

        from gantt_viewer import load_schedule, compute_changeovers
        try:
            sdf = load_schedule(schedule_path)
            _, co_df = compute_changeovers(sdf)
            co_df.to_excel(writer, sheet_name="Changeover Summary", index=False)
        except (OSError, KeyError, ValueError):
            pass

        if include_settings and toml_path.exists():
            settings_text = toml_path.read_text(encoding="utf-8")
            settings_rows = [{"line": ln} for ln in settings_text.splitlines()]
            pd.DataFrame(settings_rows).to_excel(writer, sheet_name="Solver Settings", index=False)

    st.download_button(
        "Download Excel",
        data=buf.getvalue(),
        file_name="flowstate_schedule.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# ── Gantt PDF ───────────────────────────────────────────────────────────
st.divider()
st.subheader("Gantt PDF")

if st.button("Generate Gantt PDF", key="gen_pdf"):
    try:
        from gantt_viewer import load_schedule, load_cip_windows, build_gantt_figure
        sdf = load_schedule(schedule_path)
        cdf = load_cip_windows(cip_path) if cip_path.exists() else None
        title = "Flowstate Schedule"
        if _prov:
            title += f"  ({_prov})"
        fig = build_gantt_figure(sdf, cdf, title=title)
        n_lines = sdf["Task"].nunique()
        export_h = min(4000, max(600, 30 * n_lines))
        pdf_bytes = fig.to_image(format="pdf", width=1800, height=export_h, scale=2)
        st.download_button("Download PDF", data=pdf_bytes, file_name="flowstate_gantt.pdf", mime="application/pdf")
    except (ImportError, ValueError, OSError, RuntimeError) as e:
        st.error(f"PDF generation failed: {e}")
        st.caption("Ensure `kaleido` is installed: `pip install kaleido`")

# ── Full ZIP package ────────────────────────────────────────────────────
st.divider()
st.subheader("Full Package (ZIP)")
st.caption("All CSVs + Gantt PDF + validation report bundled into one download.")

if st.button("Generate ZIP", key="gen_zip"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Schedule (with or without CIP depending on toggle)
        csv_export = _schedule_df(include_cip).to_csv(index=False)
        zf.writestr("schedule_phase2.csv", csv_export)

        for fp in [produced_path, cip_path]:
            if fp.exists():
                zf.write(fp, fp.name)
        if val_path.exists():
            zf.write(val_path, val_path.name)

        # Provenance info
        if _prov:
            zf.writestr("provenance.txt", _prov + "\n")

        # Settings
        if include_settings and toml_path.exists():
            zf.write(toml_path, "flowstate.toml")

        # Gantt PDF
        try:
            from gantt_viewer import load_schedule, load_cip_windows, build_gantt_figure
            sdf = load_schedule(schedule_path)
            cdf = load_cip_windows(cip_path) if cip_path.exists() else None
            title = "Flowstate Schedule"
            if _prov:
                title += f"  ({_prov})"
            fig = build_gantt_figure(sdf, cdf, title=title)
            n_lines = sdf["Task"].nunique()
            export_h = min(4000, max(600, 30 * n_lines))
            pdf_bytes = fig.to_image(format="pdf", width=1800, height=export_h, scale=2)
            zf.writestr("flowstate_gantt.pdf", pdf_bytes)
        except (ImportError, ValueError, OSError, RuntimeError):
            pass

    st.download_button(
        "Download ZIP",
        data=buf.getvalue(),
        file_name="flowstate_export.zip",
        mime="application/zip",
    )
