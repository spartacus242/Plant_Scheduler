# helpers/horizon.py — "the calendar starts at TODAY".
#
# Handoff WW32 task 1. Historically the planning anchor was a fixed date in
# flowstate.toml ([scheduler] planning_start_date) and the calendar always
# opened on that date, so by mid-week the planner was staring at days that
# already happened. This module makes the horizon roll:
#
#   * anchor  = midnight of the CURRENT day when [scheduler] anchor_mode =
#               "today" (default), otherwise the fixed planning_start_date.
#   * horizon = anchor .. anchor + horizon_weeks (default 3 weeks).
#   * nothing before the anchor is shown; a block straddling "now" is kept and
#     flagged in_progress so the running MO stays visible.
#
# Storage contract is unchanged: CSV/JSON still hold integer hour offsets from
# the anchor. When the anchor moves, `rebase_calendar` shifts stored hours by
# the delta so a saved schedule keeps its real wall-clock position. Everything
# here is pure (an injectable `now`) so it is testable without a clock.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from helpers.timefmt import parse_anchor

DEFAULT_HORIZON_WEEKS = 3
HOURS_PER_WEEK = 168


def today_anchor(now: datetime | None = None) -> datetime:
    """Midnight of the current day — the rolling anchor."""
    n = now or datetime.now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def anchor_mode(cfg: dict | None) -> str:
    """'today' (rolling, default) or 'fixed' (legacy planning_start_date)."""
    sched = (cfg or {}).get("scheduler") or {}
    mode = str(sched.get("anchor_mode", "today")).strip().lower()
    return mode if mode in ("today", "fixed") else "today"


def horizon_weeks(cfg: dict | None) -> int:
    sched = (cfg or {}).get("scheduler") or {}
    try:
        w = int(sched.get("horizon_weeks", DEFAULT_HORIZON_WEEKS))
    except (TypeError, ValueError):
        w = DEFAULT_HORIZON_WEEKS
    return max(1, w)


@dataclass(frozen=True)
class Horizon:
    """Resolved planning window."""

    anchor: datetime          # hour 0
    start: datetime           # first visible moment (== anchor)
    end: datetime             # exclusive end of the window
    now: datetime             # wall clock used to resolve it
    mode: str                 # "today" | "fixed"
    hours: int                # horizon length in hours
    config_anchor: datetime   # the anchor stored in flowstate.toml

    @property
    def now_h(self) -> float:
        """Wall clock as an hour offset from the anchor (may be negative)."""
        return (self.now - self.anchor).total_seconds() / 3600.0

    @property
    def end_h(self) -> float:
        return float(self.hours)

    @property
    def shift_h(self) -> float:
        """Hours the stored anchor must move to become this anchor."""
        return (self.anchor - self.config_anchor).total_seconds() / 3600.0

    @property
    def stale(self) -> bool:
        """True when stored hour offsets are anchored to a different date."""
        return abs(self.shift_h) >= 1.0

    def week_starts(self) -> list[datetime]:
        return [self.anchor + timedelta(weeks=i) for i in range(self.hours // HOURS_PER_WEEK)]


def resolve(cfg: dict | None = None, now: datetime | None = None) -> Horizon:
    """Resolve the active planning window from config + wall clock."""
    if cfg is None:
        from helpers.config import load_toml

        cfg = load_toml()
    sched = (cfg or {}).get("scheduler") or {}
    cfg_anchor = parse_anchor(sched.get("planning_start_date"))
    n = now or datetime.now()
    mode = anchor_mode(cfg)
    anchor = today_anchor(n) if mode == "today" else cfg_anchor
    weeks = horizon_weeks(cfg)
    hours = weeks * HOURS_PER_WEEK
    # An explicit horizon_hours still wins when it is set and no weeks override.
    if "horizon_hours" in sched and "horizon_weeks" not in sched:
        try:
            hours = max(1, int(sched["horizon_hours"]))
        except (TypeError, ValueError):
            pass
    return Horizon(
        anchor=anchor,
        start=anchor,
        end=anchor + timedelta(hours=hours),
        now=n,
        mode=mode,
        hours=hours,
        config_anchor=cfg_anchor,
    )


def rebase_calendar(df: Any, shift_h: float, *, start_col: str = "start_h",
                    end_col: str = "end_h") -> Any:
    """Shift stored hour offsets so they read against a moved anchor.

    `shift_h` is how far the anchor moved forward (Horizon.shift_h); block
    hours move backwards by the same amount, keeping wall-clock position.
    """
    if df is None or not hasattr(df, "columns"):
        return df
    if start_col not in df.columns or end_col not in df.columns:
        return df
    out = df.copy()
    out[start_col] = out[start_col].astype(float) - float(shift_h)
    out[end_col] = out[end_col].astype(float) - float(shift_h)
    return out


def clip_to_horizon(df: Any, hz: Horizon, *, start_col: str = "start_h",
                    end_col: str = "end_h", drop_past: bool = True) -> Any:
    """Keep only blocks intersecting the horizon; flag ones straddling now.

    Adds two boolean columns:
      * `in_progress` — started before now, ends after now.
      * `beyond_horizon` — starts inside but runs past the window end.
    Blocks entirely in the past (end <= hour 0) are dropped when drop_past.
    """
    if df is None or not hasattr(df, "columns") or len(df) == 0:
        return df
    if start_col not in df.columns or end_col not in df.columns:
        return df
    out = df.copy()
    s = out[start_col].astype(float)
    e = out[end_col].astype(float)
    keep = (e > 0.0) & (s < hz.end_h) if drop_past else (s < hz.end_h)
    out = out[keep].copy()
    s = out[start_col].astype(float)
    e = out[end_col].astype(float)
    now_h = hz.now_h
    out["in_progress"] = (s <= now_h) & (e > now_h)
    out["beyond_horizon"] = e > hz.end_h
    return out


def caption(hz: Horizon) -> str:
    """One-line human summary for the UI."""
    from helpers.timefmt import week_index_to_iso

    weeks = [f"WW{week_index_to_iso(i, hz.anchor):02d}"
             for i in range(max(1, hz.hours // HOURS_PER_WEEK))]
    mode = "rolling — starts today" if hz.mode == "today" else "fixed anchor"
    return (f"Horizon: **{hz.start:%a %Y-%m-%d}** → {hz.end:%a %Y-%m-%d} "
            f"({' / '.join(weeks)}, {mode}).")
