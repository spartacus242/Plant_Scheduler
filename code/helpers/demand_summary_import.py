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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


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
    def _iso_monday(iso_week: int) -> datetime:
        anchor_year = (anchor or datetime.now()).year
        for year in (anchor_year, anchor_year + 1):
            try:
                import datetime as _dt_mod
                monday = _dt_mod.date.fromisocalendar(year, iso_week, 1)
                return datetime(monday.year, monday.month, monday.day)
            except ValueError:
                continue
        raise ValueError(f"ISO week {iso_week} not found in years "
                         f"{anchor_year}-{anchor_year+1}")

    week_starts = {w: _iso_monday(w) for w in raw["week"].unique()}
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
