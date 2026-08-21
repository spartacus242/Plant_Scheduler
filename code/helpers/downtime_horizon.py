# helpers/downtime_horizon.py -- stale-horizon audit for staged downtimes.
#
# WHY THIS EXISTS
# ---------------
# The solver's WORK-DIR downtimes.csv speaks HOUR OFFSET pairs (start_hour,
# end_hour) against the staging anchor. (The reference file now stores
# wall-clock datetimes -- helpers/downtime_store -- but the staged copy, and
# any pre-migration or replayed work dir, is still hours.) An hour pair is
# silently dependent on two moving parts:
#
#   * the anchor (`anchor_mode = "today"` re-anchors weekly), and
#   * the horizon length (`[scheduler] horizon_hours`).
#
# When the horizon grew from 336 h (2 weeks) to 504 h (3 weeks) on
# 2026-08-10, the two rows meaning "P11 and P13 are down for the WHOLE
# horizon" still said `0 -> 336`. The solver read them literally, decided both
# lines came free at hour 336, and scheduled real production on lines that are
# physically down (measured: 6 blocks on P11 in scenario E, 3 on P11/P13 in
# scenario C).
#
# Nothing in the stack complained, because every component was individually
# correct: the planner entered a true window, the solver honoured it exactly.
# The defect only exists in the gap between them -- which is precisely the
# class of silent failure this module makes loud.
#
# POLICY: detect and REPORT, never silently rewrite. An outage window is a
# statement about the physical plant; guessing that the planner "meant"
# a longer one would be inventing plant state. The audit surfaces the
# suspicion, a human confirms it in the data.

from __future__ import annotations

from typing import Any, Iterable

# Any horizon boundary the tool has historically shipped. A window ending
# exactly on one of these, in a horizon that is now longer, is the signature
# of a stale full-horizon outage rather than a deliberate partial one.
LEGACY_HORIZON_BOUNDARIES = (168.0, 336.0)

# Tolerance in hours when comparing a window edge against a boundary.
EDGE_TOL = 1.0


def _as_float(value: Any) -> float | None:
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def audit_downtime_horizon(
    rows: Iterable[dict],
    horizon_h: float,
    *,
    boundaries: Iterable[float] = LEGACY_HORIZON_BOUNDARIES,
) -> list[str]:
    """Return one note per suspicious downtime window.

    A window is suspicious when it starts at (or before) hour 0 -- i.e. the
    line is down from the very start of the plan -- and ends exactly on a
    legacy horizon boundary that is now strictly inside the current horizon.
    That row almost certainly meant "down for the whole horizon" under a
    shorter horizon, and now silently frees the line mid-plan.

    Pure function: takes dict rows (as read from downtimes.csv) and the
    configured horizon, returns human-readable strings. No I/O, no rewriting.
    """
    horizon = _as_float(horizon_h)
    if horizon is None or horizon <= 0:
        return []
    bounds = [b for b in (_as_float(b) for b in boundaries) if b is not None]

    notes: list[str] = []
    for idx, row in enumerate(rows):
        start = _as_float(row.get("start_hour"))
        end = _as_float(row.get("end_hour"))
        if start is None or end is None or end <= start:
            continue
        if start > 0.0 + EDGE_TOL:
            continue  # a genuine mid-plan outage, not a horizon artifact
        if end >= horizon - EDGE_TOL:
            continue  # already covers the horizon
        if not any(abs(end - b) <= EDGE_TOL for b in bounds):
            continue  # ends somewhere arbitrary -> planner meant it
        line = str(row.get("line_name") or row.get("line_id") or f"row {idx}")
        reason = str(row.get("reason") or "Down").strip() or "Down"
        notes.append(
            f"downtime STALE HORIZON: {line} is down 0-{end:g}h ({reason}) but the "
            f"horizon is {horizon:g}h -- the solver will treat {line} as AVAILABLE "
            f"from hour {end:g}. If the line is down for the whole plan, extend "
            f"its end date in the Start-of-day downtime editor."
        )
    return notes


def uncovered_tail_hours(rows: Iterable[dict], horizon_h: float) -> dict[str, float]:
    """For each line down from hour 0, how many horizon hours it is left free.

    Convenience for tests and the data-health panel: {line_name: hours}. Only
    lines with a suspicious tail (>0) appear.
    """
    horizon = _as_float(horizon_h) or 0.0
    out: dict[str, float] = {}
    for row in rows:
        start = _as_float(row.get("start_hour"))
        end = _as_float(row.get("end_hour"))
        if start is None or end is None or start > EDGE_TOL or end >= horizon:
            continue
        line = str(row.get("line_name") or row.get("line_id") or "?")
        tail = horizon - end
        if tail > EDGE_TOL:
            out[line] = max(out.get(line, 0.0), tail)
    return out
