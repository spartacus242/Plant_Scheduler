# helpers/timefmt.py - Render horizon hour offsets as human date + time.
#
# The whole model runs on integer hour offsets from a planning anchor
# (flowstate.toml [scheduler] planning_start_date). Stored CSV/JSON keeps those
# raw hours - this module is DISPLAY ONLY. Durations stay in hours; only
# moments in time get stamped.

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DEFAULT_ANCHOR = "2026-02-15 00:00:00"

_ANCHOR_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def parse_anchor(value: Any = None) -> datetime:
    """Coerce a planning anchor (str/datetime/None) to a datetime."""
    if isinstance(value, datetime):
        return value
    text = str(value or DEFAULT_ANCHOR).strip()
    for fmt in _ANCHOR_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.strptime(DEFAULT_ANCHOR, "%Y-%m-%d %H:%M:%S")


def planning_anchor(cfg: dict | None = None) -> datetime:
    """Planning anchor from flowstate.toml [scheduler].planning_start_date."""
    if cfg is None:
        from helpers.config import load_toml

        cfg = load_toml()
    raw = (cfg.get("scheduler") or {}).get("planning_start_date", DEFAULT_ANCHOR)
    return parse_anchor(raw)


def hour_to_datetime(hour: float, anchor: datetime | str | None = None) -> datetime:
    """Absolute datetime for an hour offset from the anchor."""
    return parse_anchor(anchor) + timedelta(hours=float(hour))


def hour_to_stamp(hour: float, anchor: datetime | str | None = None) -> str:
    """Human stamp for an hour offset, e.g. 'Wed 2/18 11:00'."""
    d = hour_to_datetime(hour, anchor)
    return f"{d.strftime('%a')} {d.month}/{d.day} {d.hour:02d}:{d.minute:02d}"


def hour_to_short_stamp(hour: float, anchor: datetime | str | None = None) -> str:
    """Compact stamp for tight columns, e.g. '2/18 11:00'."""
    d = hour_to_datetime(hour, anchor)
    return f"{d.month}/{d.day} {d.hour:02d}:{d.minute:02d}"


def hour_to_iso(hour: float, anchor: datetime | str | None = None) -> str:
    """Sortable stamp for exports, e.g. '2026-02-18 11:00'."""
    return hour_to_datetime(hour, anchor).strftime("%Y-%m-%d %H:%M")


def with_display_times(
    df: Any,
    anchor: datetime | str | None = None,
    *,
    start_col: str = "start_h",
    end_col: str = "end_h",
    iso: bool = False,
) -> Any:
    """Return a copy of a calendar frame with human start/end columns added.

    The raw hour columns are kept untouched - the solver round-trips on them.
    Display columns are inserted right after the hour columns so the table
    reads left-to-right. Returns the frame unchanged if the hour columns are
    missing.
    """
    if df is None or not hasattr(df, "columns"):
        return df
    if start_col not in df.columns or end_col not in df.columns:
        return df
    fmt = hour_to_iso if iso else hour_to_stamp
    a = parse_anchor(anchor)
    out = df.copy()
    out["start"] = [fmt(h, a) for h in out[start_col]]
    out["end"] = [fmt(h, a) for h in out[end_col]]
    cols = [c for c in out.columns if c not in ("start", "end")]
    pos = cols.index(end_col) + 1
    ordered = cols[:pos] + ["start", "end"] + cols[pos:]
    return out[ordered]
