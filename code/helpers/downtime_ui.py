# helpers/downtime_ui.py -- STEP 1 of the scheduling workflow: per-side downtime.
#
# The Bossar double lines P17-P22 have two independent sides, A and B. Either
# side can be down on its own, and the line then runs at exactly half rate.
# The planner enters this downtime FIRST; the duration maths for production
# blocks then already knows which hours are one-sided.
#
# Rendered on the Data Files page and on the Plant Calendar.

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from helpers.lines_model import (
    DOUBLE_GROUPS,
    group_of,
    is_double,
    side_of,
    sides_of,
)
from helpers.safe_io import safe_write_csv

DOWNTIME_COLUMNS = ["line_id", "line_name", "start_hour", "end_hour", "reason"]

STEP1_CAPTION = (
    "**STEP 1 - Set scheduled downtime per side first, then schedule production.** "
    "The Bossar double lines (P17-P22) are two parallel sides, A and B. Either side "
    "can be down alone; the line then runs at exactly half rate. Enter the downtime "
    "here and the Gantt will stretch any production block that crosses a one-sided "
    "stretch automatically."
)


def downtimes_path(dd: Path) -> Path:
    return dd / "reference" / "downtimes.csv"


def load_downtimes(dd: Path) -> pd.DataFrame:
    path = downtimes_path(dd)
    if not path.exists():
        return pd.DataFrame(columns=DOWNTIME_COLUMNS)
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    for col in DOWNTIME_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df


def line_options(dd: Path) -> list[str]:
    """Every schedulable name: single lines plus each side of each double line."""
    path = dd / "lines.csv"
    names: list[str] = []
    if path.exists():
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        if "active" in df.columns:
            df = df[df["active"].astype(str).str.lower() != "false"]
        names = [str(v).strip() for v in df.get("line_name", []) if str(v).strip()]
    out: list[str] = []
    for n in names:
        if is_double(n) and side_of(n) is None:
            # Pre-migration lines.csv still lists the group; offer the sides.
            out.extend(sides_of(n))
        elif n not in out:
            out.append(n)
    return sorted(dict.fromkeys(out))


def line_id_for(dd: Path, line_name: str) -> str:
    """Resolve the line_id for a name from lines.csv; fall back to the model."""
    path = dd / "lines.csv"
    if path.exists():
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        hit = df[df["line_name"].astype(str).str.strip().str.upper() == line_name.upper()]
        if not hit.empty:
            return str(hit.iloc[0]["line_id"])
    from helpers.lines_model import GROUP_LINE_IDS, side_line_id

    s = side_of(line_name)
    if s:
        return str(side_line_id(group_of(line_name), s))
    return str(GROUP_LINE_IDS.get(group_of(line_name), ""))


def _backup(path: Path, dd: Path) -> str:
    bdir = dd / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest.name


def side_downtime_summary(dd: Path) -> pd.DataFrame:
    """One row per recorded downtime window, annotated with group and side."""
    df = load_downtimes(dd)
    if df.empty:
        return pd.DataFrame(columns=["line_name", "group", "side", "start_hour", "end_hour", "reason"])
    out = pd.DataFrame({
        "line_name": df["line_name"],
        "group": [group_of(v) for v in df["line_name"]],
        "side": [side_of(v) or "(whole line)" for v in df["line_name"]],
        "start_hour": df["start_hour"],
        "end_hour": df["end_hour"],
        "reason": df["reason"],
    })
    return out


def render_side_downtime_editor(dd: Path, *, key_prefix: str = "dt") -> None:
    """Add / review per-side scheduled downtime. Writes reference/downtimes.csv."""
    st.markdown(STEP1_CAPTION)

    options = line_options(dd)
    if not options:
        st.info("No lines defined yet -- add data/lines.csv first.")
        return

    doubles = [o for o in options if is_double(o)]
    if doubles:
        st.caption(
            "Double-line sides available: " + ", ".join(doubles) +
            "  |  Single lines (P09-P16) have no sides."
        )
    else:
        st.caption(
            "No per-side rows found in lines.csv yet -- run "
            "`python scripts/migrate_ab_lines.py` to expand P17-P22 into A/B rows."
        )

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        line_name = st.selectbox("Line / side to take down", options, key=f"{key_prefix}_line")
    with c2:
        start_h = st.number_input("Start hour", min_value=0, value=0, step=1, key=f"{key_prefix}_start")
    with c3:
        end_h = st.number_input("End hour", min_value=0, value=24, step=1, key=f"{key_prefix}_end")
    reason = st.text_input("Reason", value="Down", key=f"{key_prefix}_reason")

    grp = group_of(line_name)
    if is_double(line_name) and side_of(line_name):
        other = [s for s in sides_of(grp) if s != line_name][0]
        st.caption(
            f"{line_name} down means {grp} runs one-sided at **half rate** for "
            f"h{int(start_h)}-h{int(end_h)} (unless {other} is also down, which "
            f"stops {grp} completely)."
        )

    if st.button("Add downtime", key=f"{key_prefix}_add", type="primary"):
        if end_h <= start_h:
            st.error("End hour must be after start hour.")
        else:
            df = load_downtimes(dd)
            row = {
                "line_id": line_id_for(dd, line_name),
                "line_name": line_name,
                "start_hour": str(int(start_h)),
                "end_hour": str(int(end_h)),
                "reason": reason or "Down",
            }
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            path = downtimes_path(dd)
            note = ""
            if path.exists():
                note = f"  Backup: {_backup(path, dd)}"
            safe_write_csv(df[DOWNTIME_COLUMNS], path)
            st.success(f"{line_name} down h{int(start_h)}-h{int(end_h)} ({reason}).{note}")
            st.rerun()

    summary = side_downtime_summary(dd)
    if summary.empty:
        st.caption("No scheduled downtime recorded yet.")
        return

    st.caption("Scheduled downtime on record (edit or delete rows, then Save):")
    edited = st.data_editor(
        summary,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        disabled=["group", "side"],
        key=f"{key_prefix}_grid",
    )
    if st.button("Save downtime table", key=f"{key_prefix}_save"):
        out = pd.DataFrame(edited)
        rows = []
        for rec in out.to_dict("records"):
            name = str(rec.get("line_name", "") or "").strip()
            if not name:
                continue
            rows.append({
                "line_id": line_id_for(dd, name),
                "line_name": name,
                "start_hour": rec.get("start_hour", ""),
                "end_hour": rec.get("end_hour", ""),
                "reason": rec.get("reason", "") or "Down",
            })
        path = downtimes_path(dd)
        note = ""
        if path.exists():
            note = f"  Backup: {_backup(path, dd)}"
        safe_write_csv(pd.DataFrame(rows, columns=DOWNTIME_COLUMNS), path)
        st.success(f"Saved {len(rows)} downtime row(s).{note}")
        st.rerun()


def downtime_map_for_calendar(dd: Path, calendar: pd.DataFrame | None = None) -> dict:
    """Merged downtime map (line/side name -> intervals) from CSV + calendar."""
    from helpers.lines_model import downtime_from_rows

    rows: list[dict] = load_downtimes(dd).to_dict("records")
    if calendar is not None and not calendar.empty:
        rows.extend(calendar.to_dict("records"))
    out = downtime_from_rows(rows)
    # A downtime recorded against the whole group (pre-migration rows) means
    # BOTH sides are down; expand it so effective_rate sees it on each side.
    for g in DOUBLE_GROUPS:
        if g in out:
            for s in sides_of(g):
                out.setdefault(s, []).extend(out[g])
    return out
