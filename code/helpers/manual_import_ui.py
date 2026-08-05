# helpers/manual_import_ui.py -- Shared Streamlit widget for importing the
# production planner's own manual line schedule.
#
# Rendered on both the Data Files page and the Schedule Scorecard page so the
# planner finds it wherever he looks. All parsing/validation lives in
# helpers/manual_import.py; this module is presentation plus the two write
# actions (set as base model / save as version only).

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from helpers.calendar_io import ensure_lines_from_calendar, save_calendar
from helpers.manual_import import parse_manual_schedule, template_csv
from helpers.scorecard_engine import score_calendar
from helpers.timefmt import with_display_times
from helpers.version_manager import save_version, upsert_version

# Kept for backward compatibility with versions already on disk. The directory
# data/versions/azap_baseline/ is never renamed -- only its display name is.
CURRENT_SCHEDULE_SLUG = "azap_baseline"
CURRENT_SCHEDULE_NAME = "Current schedule (imported)"

INTRO = (
    "AZAP is the customer / corporate **demand plan**: which SKUs, how many kg, which "
    "week (`data/reference/demand_plan.csv`). It never assigns lines, sequence or "
    "equipment. The production planner builds the real line schedule himself, outside "
    "this tool -- upload it here."
)

WHY_BASE = (
    "**Recommended: set it as the base model.** The planner's manual schedule is "
    "known-feasible -- it is what the plant actually intends to run -- so it is the "
    "safest baseline to compare everything else against. Solver scenarios and the "
    "naive demand-plan strawman are both scored relative to it."
)


def backup_file(path: Path, data_dir: Path) -> str:
    """Copy path into data/_backups/<stem>.<timestamp><suffix>. Returns the name."""
    if not path.exists():
        return ""
    bdir = Path(data_dir) / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest.name


def snapshot_current_schedule(cal, data_dir: Path) -> None:
    """Keep Compare / Generate in sync with the schedule now on disk."""
    result = score_calendar(cal, week_label="current schedule", data_dir=data_dir)
    try:
        upsert_version(
            CURRENT_SCHEDULE_SLUG,
            CURRENT_SCHEDULE_NAME,
            cal,
            result.to_dict(),
            data_dir,
            source="schedule_import",
            notes="calendar_blocks.csv snapshot taken when the schedule was imported.",
        )
    except ValueError as exc:
        st.warning(f"Could not save the current-schedule version: {exc}")


def render_manual_import(data_dir: Path, anchor: str, horizon_hours: int, *, key: str = "man") -> None:
    """Full upload -> validate -> preview -> commit flow. Never raises."""
    dd = Path(data_dir)
    cal_path = dd / "calendar_blocks.csv"

    st.markdown(INTRO)
    st.markdown(WHY_BASE)
    st.caption(
        "Accepted layouts: **planner style** (`line`, `sku`, `start`, `end` dates/times, "
        f"converted to hour offsets from the planning anchor `{anchor}`; optional `qty_kg`, "
        "`block_type`, `order_id`) or **native** `calendar_blocks.csv` columns "
        "(`block_id, block_type, line_id, line_name, start_h, end_h, ...`). "
        "Column names match case- and spacing-insensitively; a UTF-8 BOM is fine."
    )
    st.download_button(
        "Download an example CSV",
        data=template_csv().encode("utf-8"),
        file_name="manual_schedule_example.csv",
        mime="text/csv",
        key=f"{key}_tpl",
    )

    up = st.file_uploader(
        "Manual schedule (CSV or XLSX)",
        type=["csv", "xlsx", "xlsm", "xls"],
        key=f"{key}_upload",
    )
    if up is None:
        return

    imp = parse_manual_schedule(
        up.getvalue(),
        up.name,
        dd / "lines.csv",
        planning_anchor=anchor,
        horizon_hours=horizon_hours,
    )
    st.caption(f"Layout detected: {imp.layout}  |  rows read: {imp.rows_in}")

    if imp.errors:
        st.error(f"Rejected - {len(imp.errors)} problem(s) found. Nothing was written to disk.")
        for msg in imp.errors:
            st.write(f"- {msg}")
        return

    for msg in imp.warnings:
        st.warning(msg)
    st.success(f"Parsed OK: {len(imp.calendar)} block(s).")
    st.dataframe(
        with_display_times(imp.calendar.head(25), anchor),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("**What do you want to do with it?**")
    c1, c2 = st.columns(2)
    with c1:
        if st.button(
            "Set as base model (recommended)",
            type="primary",
            use_container_width=True,
            key=f"{key}_set_base",
        ):
            bname = backup_file(cal_path, dd)
            save_calendar(imp.calendar, cal_path)
            ensure_lines_from_calendar(imp.calendar, dd / "lines.csv")
            snapshot_current_schedule(imp.calendar, dd)
            if bname:
                st.caption(f"Previous calendar backed up to `_backups/{bname}`")
            st.success(
                f"{len(imp.calendar)} block(s) written to data/calendar_blocks.csv as the "
                "current schedule, and saved as a version so it is never lost."
            )
        st.caption(
            "Writes `data/calendar_blocks.csv` (backing up the old one first) **and** saves "
            "a version. This becomes the schedule every scenario is measured against."
        )
    with c2:
        ver_name = st.text_input(
            "Version name",
            value="Planner manual schedule",
            key=f"{key}_ver_name",
        )
        if st.button("Save as version only", use_container_width=True, key=f"{key}_save_ver"):
            try:
                sc = score_calendar(imp.calendar, week_label=ver_name, data_dir=dd)
                slug = save_version(
                    ver_name,
                    imp.calendar,
                    sc.to_dict(),
                    dd,
                    source="manual_upload",
                    notes=f"Uploaded from {up.name} ({imp.layout}).",
                )
                comp = f"{sc.composite:.0f}" if sc.composite is not None else "n/a"
                st.success(
                    f"Saved version `{slug}` (composite={comp}). "
                    "The current schedule on disk was not touched."
                )
            except ValueError as exc:
                st.error(str(exc))
        st.caption("Keeps it as an option to compare; does not change the current schedule.")
