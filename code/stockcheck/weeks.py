# code/stockcheck/weeks.py — demand week_index <-> ISO week mapping.
#
# demand_plan.csv stores week_index (0 = anchor week). The anchor is the
# Monday of the summary's earliest ISO week, recorded in
# demand_plan.source.json (anchor + anchor_iso_week) — NOT flowstate.toml's
# planning_start_date and NOT timefmt's DEFAULT_ANCHOR fallback (labeling
# with that fallback is what showed WW07 for a WW34 plan).
# Ordering is always by (iso_year, iso_week): bare week numbers sort W01
# before W52 at year end.

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from helpers.timefmt import parse_anchor


def demand_anchor(data_dir: str | Path) -> datetime:
    """Monday of week_index 0, from demand_plan.source.json.

    Falls back to the Monday of the current ISO week when the sidecar is
    missing, so labels degrade to "counting from this week" instead of a
    months-old default anchor.
    """
    p = Path(data_dir) / "reference" / "demand_plan.source.json"
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
        return parse_anchor(meta["anchor"])
    except Exception:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        return datetime(monday.year, monday.month, monday.day)


def demand_week_indices(data_dir: str | Path) -> list[int]:
    """Distinct week_index values present in the CURRENT demand plan."""
    p = Path(data_dir) / "reference" / "demand_plan.csv"
    try:
        df = pd.read_csv(p, usecols=["week_index"])
    except Exception:
        return []
    return sorted(int(w) for w in df["week_index"].dropna().unique())


def week_index_iso_parts(week_index: int,
                         anchor: datetime | str | None) -> tuple[int, int]:
    """(iso_year, iso_week) for a week_index — the only safe sort key."""
    d = parse_anchor(anchor) + timedelta(weeks=int(week_index))
    iso = d.isocalendar()
    return (iso[0], iso[1])


def week_index_label(week_index: int, anchor: datetime | str | None) -> str:
    """'WW34' — same week the holding area labels the order's -W<k> suffix."""
    return f"WW{week_index_iso_parts(week_index, anchor)[1]:02d}"


def current_week_options(weeks: list[int], anchor: datetime | str | None,
                         today: date | None = None) -> list[int]:
    """week_index values from `weeks` that are >= the current ISO week,
    ordered by (iso_year, iso_week) so W52 precedes next year's W01."""
    if today is None:
        today = date.today()
    floor = (today.isocalendar()[0], today.isocalendar()[1])
    keyed = sorted((week_index_iso_parts(w, anchor), int(w)) for w in weeks)
    return [w for parts, w in keyed if parts >= floor]
