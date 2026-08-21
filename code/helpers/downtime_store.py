# helpers/downtime_store.py -- downtimes are STATIC wall-clock plant data.
#
# WHY THIS EXISTS
# ---------------
# reference/downtimes.csv used to store anchor-relative hour offsets
# (start_hour/end_hour against [scheduler] planning_start_date). "Roll
# calendar to today" moves that anchor WITHOUT touching this file, so every
# roll silently dragged every outage forward in wall-clock terms (user
# report 2026-08-21: a P11 end entered as 08/24 00:00 rendered as 08/26
# after a +2-day roll). An outage is a statement about the physical plant --
# it happens on a DATE, not at "hour 120 of whatever the anchor is today".
#
# The file therefore stores ABSOLUTE datetimes (start_datetime/end_datetime,
# "YYYY-MM-DD HH:MM") as the single source of truth. Anchor-relative hours
# are a LOAD-TIME derivation: hour = (datetime - anchor) / 1h, for whichever
# frame the consumer works in. No consumer may read raw hour columns from
# this file any more -- every read goes through load_downtimes /
# load_downtimes_file, every write through save_downtimes, and the solver
# work-dir copy is produced by stage_solver_downtimes.
#
# The file is edited in exactly ONE place: the Start-of-day downtime strip
# (helpers/downtime_ui.py). It is deliberately NOT in the live-data bridge
# list (scripts/fs-live-data.conf.json) -- the nightly pull would clobber
# the planner's edits.
#
# MIGRATION: the first load of an old-format file (hour columns, no datetime
# columns) converts once using the CURRENT planning anchor -- the frame those
# hours were stored in -- backs the file up to data/_backups/ and rewrites it
# with datetime columns. Subsequent loads never write.

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from helpers.safe_io import safe_write_csv
from helpers.timefmt import parse_anchor, parse_datetime

LOG = logging.getLogger(__name__)

DT_FMT = "%Y-%m-%d %H:%M"

# On-disk schema (source of truth).
STORE_COLUMNS = ["line_id", "line_name", "start_datetime", "end_datetime", "reason"]
# In-memory schema returned by the loaders: STORE_COLUMNS plus hours derived
# against the requested anchor. start_hour/end_hour are floats (NaN when the
# datetime cell cannot be parsed -- consumers already guard float()).
DERIVED_COLUMNS = ["start_hour", "end_hour"]
LOAD_COLUMNS = STORE_COLUMNS + DERIVED_COLUMNS
# Solver work-dir schema (unchanged): the staged copy speaks hours.
SOLVER_COLUMNS = ["line_id", "line_name", "start_hour", "end_hour", "reason"]

# One-shot data correction applied during the hour->datetime migration only
# (user statement 2026-08-21): P11's on-disk hour pair was written under the
# 08-19 anchor to mean "ends 08/24 00:00"; the roll to 08-21 dragged the
# rendered end to 08/26. The user told us the TRUE end, so the migration
# restores it instead of preserving the roll artifact.
_MIGRATION_END_OVERRIDES = {"P11": datetime(2026, 8, 24, 0, 0)}


def downtimes_path(dd: Path) -> Path:
    return Path(dd) / "reference" / "downtimes.csv"


def _default_anchor() -> datetime:
    from helpers.config import load_toml
    from helpers.timefmt import planning_anchor

    return planning_anchor(load_toml())


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=LOAD_COLUMNS)


def _fmt(dt: datetime | None) -> str:
    return dt.strftime(DT_FMT) if dt is not None else ""


def _derive_hours(df: pd.DataFrame, anchor: datetime) -> pd.DataFrame:
    """Attach start_hour/end_hour derived from the datetime columns."""
    out = df.copy()
    for dt_col, h_col in (("start_datetime", "start_hour"),
                          ("end_datetime", "end_hour")):
        hours: list[float] = []
        for v in out[dt_col]:
            dt = parse_datetime(v)
            hours.append(
                round((dt - anchor).total_seconds() / 3600.0, 3)
                if dt is not None else float("nan"))
        out[h_col] = hours
    return out


def _backup(path: Path) -> Path:
    """Back up next to the app's other backups: data/_backups/."""
    base = path.parent.parent if path.parent.name == "reference" else path.parent
    bdir = base / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest


def _migrate_rows(df: pd.DataFrame, storage_anchor: datetime) -> pd.DataFrame:
    """hour columns -> datetime columns, in the frame the hours were stored in."""
    out = df.copy()
    starts: list[str] = []
    ends: list[str] = []
    for _, r in out.iterrows():
        line = str(r.get("line_name", "") or "").strip().upper()
        try:
            s_dt = storage_anchor + timedelta(hours=float(r.get("start_hour")))
        except (TypeError, ValueError):
            s_dt = None
        try:
            e_dt = storage_anchor + timedelta(hours=float(r.get("end_hour")))
        except (TypeError, ValueError):
            e_dt = None
        override = _MIGRATION_END_OVERRIDES.get(line)
        if override is not None and e_dt is not None and e_dt != override:
            LOG.warning(
                "downtimes migration: %s end corrected %s -> %s "
                "(roll artifact; user-stated true end)",
                line, _fmt(e_dt), _fmt(override))
            e_dt = override
        starts.append(_fmt(s_dt))
        ends.append(_fmt(e_dt))
    out["start_datetime"] = starts
    out["end_datetime"] = ends
    return out


def load_downtimes_file(
    path: Path,
    *,
    anchor: datetime | str | None = None,
    storage_anchor: datetime | str | None = None,
    migrate: bool = True,
) -> pd.DataFrame:
    """Read a downtimes file; return STORE_COLUMNS + derived hour columns.

    ``anchor`` is the frame the caller works in (hour 0); defaults to the
    toml planning anchor. ``storage_anchor`` is only used when migrating an
    old hour-format file -- the frame those hours were stored in -- and also
    defaults to the toml planning anchor. ``migrate=False`` converts an old
    file in memory without rewriting it (read-only callers, tests).
    """
    path = Path(path)
    if not path.exists():
        return _empty()
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str,
                         keep_default_na=False)
    except pd.errors.EmptyDataError:
        return _empty()

    frame_anchor = parse_anchor(anchor) if anchor is not None else _default_anchor()

    has_dt = {"start_datetime", "end_datetime"}.issubset(df.columns)
    has_hours = {"start_hour", "end_hour"}.issubset(df.columns)

    if not has_dt and has_hours:
        # Old format: one-time hour -> datetime conversion.
        store_anchor = (parse_anchor(storage_anchor)
                        if storage_anchor is not None else _default_anchor())
        df = _migrate_rows(df, store_anchor)
        if migrate and len(df):
            backup = _backup(path)
            save_downtimes_file(path, df)
            LOG.warning(
                "downtimes.csv MIGRATED to wall-clock datetimes (anchor %s, "
                "%d row(s)); old file backed up to %s",
                _fmt(store_anchor), len(df), backup)
    elif not has_dt:
        if len(df):
            raise ValueError(
                f"{path} has neither datetime nor hour columns "
                f"(found: {', '.join(df.columns)})")
        return _empty()

    for col in STORE_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return _derive_hours(df[STORE_COLUMNS], frame_anchor)


def load_downtimes(dd: Path, *, anchor: datetime | str | None = None
                   ) -> pd.DataFrame:
    """The one reference-file loader: data/reference/downtimes.csv."""
    return load_downtimes_file(downtimes_path(dd), anchor=anchor)


def save_downtimes_file(path: Path, df: pd.DataFrame) -> None:
    """Write STORE_COLUMNS only -- derived hour columns never hit the disk."""
    out = df.copy()
    for col in STORE_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    for col in ("start_datetime", "end_datetime"):
        out[col] = [
            _fmt(parse_datetime(v)) if parse_datetime(v) is not None else str(v or "")
            for v in out[col]]
    safe_write_csv(out[STORE_COLUMNS], Path(path))


def save_downtimes(dd: Path, df: pd.DataFrame) -> None:
    save_downtimes_file(downtimes_path(dd), df)


def stage_solver_downtimes(
    src: Path,
    dst: Path,
    anchor: datetime | str | None,
) -> int:
    """Derive the solver work-dir copy: wall-clock file -> hour-frame CSV.

    Writes ``dst`` with SOLVER_COLUMNS (start_hour/end_hour relative to
    ``anchor``). Rows whose datetimes cannot be parsed are dropped loudly.
    Returns the number of rows staged.
    """
    df = load_downtimes_file(src, anchor=anchor)
    bad = df[df["start_hour"].isna() | df["end_hour"].isna()]
    if len(bad):
        LOG.warning("stage_solver_downtimes: dropped %d unparseable row(s): %s",
                    len(bad), bad["line_name"].tolist())
        df = df.drop(bad.index)
    out = df[SOLVER_COLUMNS].copy()
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(dst, index=False)
    return len(out)
