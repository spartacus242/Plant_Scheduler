# code/helpers/demand_summary_import.py — demand_plan_summary.csv -> demand_plan.csv.
#
# The planner's cleaner demand file (handoff WW32 item 4):
#   cols: Week, Product, kg_tons
#   Week = ISO week number (33/34/35…)
#   Product = SKU
#   kg_tons = metric tons (×1000 = kg)
#   No machine column — line assignment is the solver's job.
#
# The AZAP raw export (with Machine/Hours per line) was confusing things;
# this is the shape the solver should consume going forward.

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

# Self-anchoring year inference (2026-09-15): a week whose Monday is more than
# this far in the past cannot be what a fresh demand file means.
SELF_ANCHOR_LOOKBACK_WEEKS = 12
# (2026-09-16) The look-back picks the year of the file's FIRST week only;
# every other week lands on the first Monday of its number on or after that
# one (forward, 0..52 weeks). A week mapped more than this many weeks after
# the first one is reported in the warnings (a long horizon, or a file whose
# weeks are not one run).
SELF_ANCHOR_SPAN_WEEKS = 26


def _run_start_order(weeks: list[int]) -> list[int]:
    """The file's distinct valid weeks (1..53), best run start first
    (2026-09-16): the week after the widest gap on the 53-slot ISO week
    circle leads, so a window in order (38..44), a New-Year wrap (51, 52, 53,
    1, 2) and the same wrap sorted by number (1, 2, 51, 52, 53) all start
    where the plan starts. Ties go to the week seen first in the file."""
    seen: list[int] = []
    for w in weeks:
        w = int(w)
        if 1 <= w <= 53 and w not in seen:
            seen.append(w)
    ordered = sorted(seen)
    keyed = []                                   # ((gap, -file position), week)
    for i, w in enumerate(ordered):
        prev = ordered[i - 1] if i else ordered[-1] - 53
        keyed.append(((w - prev, -seen.index(w)), w))
    return [w for _, w in sorted(keyed, reverse=True)]


def _run_start_week(weeks: list[int]) -> int | None:
    """The week that starts the file's run of weeks (the first of
    _run_start_order). None for no valid week."""
    order = _run_start_order(weeks)
    return order[0] if order else None


def _today() -> date:
    """The clock the self-anchoring year inference reads (patched in tests)."""
    return datetime.now().date()


@dataclass
class DemandSummaryResult:
    rows: int
    weeks: list[int]
    skus: list[str]
    warnings: list[str]
    anchor: datetime | None = None   # Monday of the earliest ISO week in the file
    anchor_iso_week: int | None = None


def import_summary(
    path: str | Path,
    *,
    anchor: datetime | None = None,
    cfg: dict | None = None,
    update_anchor: Any = None,
) -> tuple[pd.DataFrame, DemandSummaryResult]:
    """Read demand_plan_summary.csv and emit a demand_plan DataFrame.

    Returns (demand_plan, metadata). The demand_plan has the canonical columns
    (order_id, sku, week_index, qty_target, lower_pct, upper_pct,
     due_start_hour, due_end_hour, priority).

    Anchor: by default the summary file is SELF-ANCHORING — the Monday of its
    earliest ISO week becomes hour 0 (week_index 0 = that week), so the demand
    grid always lines up with the file, regardless of flowstate.toml. Pass an
    explicit `anchor` to override (used by tests / callers that want a
    specific base). `update_anchor` is an optional callable(anchor_str) invoked
    after a successful parse so the caller can persist planning_start_date —
    the same re-anchor contract the old AZAP importer had.

    Week → hour mapping: ISO weeks are always Monday-to-Sunday. We compute the
    Monday of each ISO week and convert it to hours from the anchor.

    ISO week → year (2026-09-15, revised 2026-09-16): the file carries week
    NUMBERS only, so the year is inferred, ONCE for the whole file.
    Self-anchoring (anchor None): the week that starts the file's run of
    weeks (_run_start_order: 38 for 38..44, 51 for 51,52,53,1,2) takes the
    year among (this year - 1, this year, this year + 1) whose Monday is the
    EARLIEST one on or after today minus 12 weeks; every other week takes
    the first Monday of its number on or after that reference Monday
    (FORWARD, 0..52 weeks: the run start is by construction the week the
    others follow). So a window that crosses New Year (51, 52, 1, 2) puts
    week 1 in the coming January, a contiguous window stays contiguous
    however late it is re-imported (the per-week look-back of 2026-09-15
    split W38..W44 re-imported on 2026-12-14 into week_index [52, 0, 1, ...])
    and however long it is (the "nearest Monday" rule of 2026-09-16 put the
    tail of a 28+ week horizon in the past). One exception: when the file
    holds the CURRENT ISO week, the run starts at the best-ranked week that
    maps it onto this week's Monday, so [20, 38] read in W38 of 2026 is W38
    now and W20 of 2027 (35 weeks on), never W38 of 2027. A week mapped more
    than SELF_ANCHOR_SPAN_WEEKS after the run start is listed in warnings.
    With an explicit `anchor` the legacy mapping stays: the anchor's year,
    then the next. `_today()` is the clock, so tests can freeze it.
    """
    path = Path(path)
    src = path if path.suffix == ".csv" else path
    raw = pd.read_csv(src, encoding="utf-8-sig", dtype=str)
    raw.columns = [c.strip().lower() for c in raw.columns]
    # bridge exports drift on the tonnage header: kg_tons vs Tons
    raw = raw.rename(columns={"product": "sku", "tons": "kg_tons"})

    warnings: list[str] = []

    # Validate required columns
    for col in ("week", "sku", "kg_tons"):
        if col not in raw.columns:
            raise ValueError(
                f"demand_plan_summary.csv missing column '{col}'. "
                f"Expected: Week, Product, kg_tons"
            )

    # Clean
    raw["week"] = pd.to_numeric(raw["week"], errors="coerce")
    raw["kg_tons"] = pd.to_numeric(raw["kg_tons"], errors="coerce")
    raw["sku"] = raw["sku"].astype(str).str.strip()

    bad_week = raw["week"].isna()
    bad_kg = raw["kg_tons"].isna() | (raw["kg_tons"] <= 0)
    if bad_week.any():
        warnings.append(f"dropped {bad_week.sum()} row(s) with invalid week")
    if bad_kg.any():
        warnings.append(f"dropped {bad_kg.sum()} row(s) with invalid kg_tons")
    raw = raw[~(bad_week | bad_kg)].copy()

    raw["week"] = raw["week"].astype(int)
    raw["kg_tons"] = raw["kg_tons"].astype(float)

    # ISO week → Monday datetime.
    # Python's date.fromisocalendar(year, week, 1) returns the Monday.
    def _floor_monday(iso_week: int) -> date:
        # the look-back rule of 2026-09-15: the earliest Monday on or after
        # today - 12 weeks among 3 years
        today = _today()
        floor = today - timedelta(weeks=SELF_ANCHOR_LOOKBACK_WEEKS)
        cands = []
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                monday = date.fromisocalendar(year, iso_week, 1)
            except ValueError:
                continue
            if monday >= floor:
                cands.append(monday)
        if not cands:
            raise ValueError(f"ISO week {iso_week} not found in years "
                             f"{today.year - 1}-{today.year + 1}")
        return min(cands)

    def _forward_monday(iso_week: int, ref: date) -> date | None:
        # (2026-09-16) the first Monday of `iso_week` on or after `ref`
        ref_year = ref.isocalendar()[0]
        for year in (ref_year, ref_year + 1):
            try:
                monday = date.fromisocalendar(year, iso_week, 1)
            except ValueError:
                continue
            if monday >= ref:
                return monday
        return None

    self_ref: tuple[int, date] | None = None
    far: list[tuple[int, date, int]] = []        # (week, Monday, weeks after the run start)
    if anchor is None:
        starts = _run_start_order(list(pd.unique(raw["week"])))
        today = _today()
        this_week, this_monday = today.isocalendar()[1], today - timedelta(days=today.weekday())
        for s in starts:
            try:
                ref = _floor_monday(s)
            except ValueError:
                continue
            if this_week in starts:
                # (2026-09-16) the current ISO week in the file is never pushed a year out
                lands = ref if s == this_week else _forward_monday(this_week, ref)
                if lands != this_monday:
                    continue
            self_ref = (s, ref)
            break
        if self_ref is None and starts:
            self_ref = (starts[0], _floor_monday(starts[0]))   # raises for an impossible week

    def _iso_monday(iso_week: int) -> datetime:
        if anchor is None:
            # self-anchoring (see the docstring): the file's first week by the
            # look-back rule, every other week forward from it
            if self_ref is None:
                monday = _floor_monday(iso_week)        # no valid week: raises
            elif iso_week == self_ref[0]:
                monday = self_ref[1]
            else:
                monday = _forward_monday(iso_week, self_ref[1])
                if monday is None:
                    raise ValueError(f"ISO week {iso_week} not found within a year after the "
                                     f"file's first week W{self_ref[0]} ({self_ref[1]})")
                ahead = (monday - self_ref[1]).days // 7
                if ahead > SELF_ANCHOR_SPAN_WEEKS:
                    far.append((iso_week, monday, ahead))
            return datetime(monday.year, monday.month, monday.day)
        # explicit anchor: legacy mapping (the anchor's year, then the next)
        anchor_year = anchor.year
        for year in (anchor_year, anchor_year + 1):
            try:
                monday = date.fromisocalendar(year, iso_week, 1)
                return datetime(monday.year, monday.month, monday.day)
            except ValueError:
                continue
        raise ValueError(f"ISO week {iso_week} not found in years "
                         f"{anchor_year}-{anchor_year+1}")

    week_starts = {w: _iso_monday(w) for w in raw["week"].unique()}
    if far and self_ref is not None:
        far.sort(key=lambda f: f[1])
        warnings.append(
            f"{len(far)} ISO week(s) more than {SELF_ANCHOR_SPAN_WEEKS} weeks after the file's "
            f"first week W{self_ref[0]} ({self_ref[1]}), mapped forward: "
            + ", ".join(f"W{w} -> {m} (+{n})" for w, m, n in far))
    invalid = {w for w in week_starts if w < 1 or w > 53}
    if invalid:
        warnings.append(f"invalid ISO weeks ignored: {sorted(invalid)}")

    raw["week_start"] = raw["week"].map(week_starts)
    raw = raw.dropna(subset=["week_start"])

    if raw.empty:
        raise ValueError("demand_plan_summary.csv has no usable rows after cleaning.")

    # Self-anchor: Monday of the earliest ISO week in the file.
    if anchor is None:
        earliest = min(raw["week_start"])
        anchor = earliest
        result_anchor = earliest
    else:
        result_anchor = anchor

    # week_index = whole weeks between the ISO Monday of that week and
    # the anchor's ISO Monday, so week 0 = the ISO week containing the anchor.
    anchor_iso = anchor.isocalendar()
    anchor_iso_monday = datetime.strptime(
        f"{anchor_iso[0]}-W{anchor_iso[1]:02d}-1", "%G-W%V-%u")

    raw["week_index"] = raw["week_start"].apply(
        lambda d: round((d - anchor_iso_monday).total_seconds() / (7 * 86400))
    )

    raw["qty_target"] = (raw["kg_tons"] * 1000.0).round(0).astype(int)
    raw["lower_pct"] = 0.9
    raw["upper_pct"] = 1.1
    raw["due_start_hour"] = raw["week_index"] * 168
    raw["due_end_hour"] = raw["due_start_hour"] + 167
    raw["priority"] = 3

    raw["order_id"] = raw.apply(
        lambda r: f"{r['sku']}-W{r['week_index']}", axis=1
    )

    out = raw[[
        "order_id", "sku", "week_index", "qty_target",
        "lower_pct", "upper_pct", "due_start_hour", "due_end_hour", "priority"
    ]].copy()

    result = DemandSummaryResult(
        rows=len(out),
        weeks=sorted(out["week_index"].unique()),
        skus=sorted(out["sku"].unique()),
        warnings=warnings,
        anchor=result_anchor,
        anchor_iso_week=result_anchor.isocalendar()[1] if result_anchor else None,
    )
    if update_anchor is not None and result.anchor is not None:
        update_anchor(result.anchor.strftime("%Y-%m-%d 00:00:00"))
    return out, result
