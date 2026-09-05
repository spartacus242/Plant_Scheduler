# helpers/naive_baseline.py -- Strawman schedule built straight from the demand plan.
#
# AZAP (data/reference/demand_plan.csv) is a DEMAND PLAN: which SKU, how many kg,
# which week. It does not schedule lines. This module does the dumbest possible
# thing with it -- take AZAP literally -- so the planner has a strawman to argue
# with and a floor to measure real schedules against.
#
# Rules (deliberately naive, no optimization, no solver):
#   * each order stays inside the week AZAP asked for: the order's OWN due
#     window [due_start_hour, due_end_hour + 1) shifted into the planning
#     frame exactly as scorecard_engine.load_demand shifts it (fix Q /
#     scorecard-13, 2026-09-03: the old 168 h grid from hour 0 was out of
#     phase with the graded deadlines by the anchor gap, so part of the
#     strawman's lateness was a frame artefact). Rows without due hours
#     fall back to the week_index * 168 grid.
#   * assign to the fastest capable line (capabilities_rates.capable == 1)
#   * run hours = qty_target / calc_rate_kgph
#   * lay orders out back-to-back per line, no overlaps, no changeover/CIP logic
#   * an order that will not fit is SKIPPED and reported, never crashes
#
# Pure Python + pandas: it returns instantly.

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from helpers.calendar_io import CALENDAR_COLUMNS, empty_calendar

WEEK_HOURS = 168.0
DEFAULT_HORIZON_H = 336

NAIVE_VERSION_NAME = "Naive (demand plan as-is)"
NAIVE_VERSION_SLUG = "naive_demand_plan"


@dataclass
class NaiveResult:
    calendar: pd.DataFrame = field(default_factory=empty_calendar)
    placed: list[dict[str, Any]] = field(default_factory=list)
    unplaced: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.calendar.empty


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        if not Path(path).exists():
            return pd.DataFrame()
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        return pd.DataFrame()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def _new_id() -> str:
    return f"naive_{uuid.uuid4().hex[:10]}"


def _capable_lines(caps: pd.DataFrame) -> dict[str, list[tuple[float, int, str]]]:
    """SKU -> [(rate_kgph, line_id, line_name), ...] fastest first."""
    out: dict[str, list[tuple[float, int, str]]] = {}
    if caps.empty:
        return out
    for _, r in caps.iterrows():
        if int(_num(r.get("capable"), 0)) != 1:
            continue
        rate = _num(r.get("calc_rate_kgph"), 0.0)
        if rate <= 0:
            continue
        sku = str(r.get("sku", "")).strip()
        if not sku:
            continue
        try:
            lid = int(_num(r.get("line_id"), -1))
        except (TypeError, ValueError):
            continue
        out.setdefault(sku, []).append((rate, lid, str(r.get("line_name", "")).strip()))
    for sku in out:
        out[sku].sort(key=lambda t: -t[0])
    return out


def _demand_frame_shift_h(dd: Path, anchor=None) -> float:
    """Hours to SUBTRACT from the demand file's due hours to land in the
    planning frame: planning anchor - demand_plan.source.json anchor (the
    load_demand rule). 0 when either anchor is unknown; never raises."""
    try:
        from helpers.demand_coverage import demand_source_anchor

        dem_anchor = demand_source_anchor(dd)
        if dem_anchor is None:
            return 0.0
        if anchor is None:
            from helpers import horizon as _hz
            from helpers.config import load_toml as _lt

            anchor = _hz.resolve(_lt()).anchor
        return (anchor - dem_anchor).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001 — the strawman must never crash
        return 0.0


def build_naive_calendar(
    data_dir: Path,
    *,
    horizon_hours: int = DEFAULT_HORIZON_H,
    strategy: str = "fastest",
    anchor=None,
) -> NaiveResult:
    """Lay the demand plan out as-is. Never raises on missing/malformed CSVs.

    `anchor` (datetime) pins the planning frame the due windows are shifted
    into; default = the resolved live frame (helpers.horizon.resolve).
    """
    res = NaiveResult()
    dd = Path(data_dir)
    ref = dd / "reference"
    shift_h = _demand_frame_shift_h(dd, anchor)

    demand = _read_csv(ref / "demand_plan.csv")
    caps = _read_csv(ref / "capabilities_rates.csv")
    if "calc_rate_kgph" not in caps.columns and "rate_kgph" in caps.columns:
        caps = caps.rename(columns={"rate_kgph": "calc_rate_kgph"})
    sku_info = _read_csv(ref / "sku_info.csv")

    if demand.empty:
        res.notes.append("data/reference/demand_plan.csv is missing or empty -- nothing to lay out.")
        return res
    if caps.empty:
        res.notes.append("data/reference/capabilities_rates.csv is missing or empty -- no line rates.")
        return res

    for col in ("order_id", "sku", "qty_target"):
        if col not in demand.columns:
            res.notes.append(f"demand_plan.csv has no '{col}' column.")
            return res

    descriptions: dict[str, str] = {}
    if not sku_info.empty and "sku" in sku_info.columns:
        desc_col = next(
            (c for c in ("sku_description", "description", "name",
                         "designation", "ediact_sku_description")
             if c in sku_info.columns),
            None,
        )
        if desc_col:
            for _, r in sku_info.iterrows():
                descriptions[str(r.get("sku", "")).strip()] = str(r.get(desc_col, "") or "")

    lines_for_sku = _capable_lines(caps)
    if not lines_for_sku:
        res.notes.append("No capable line/rate rows found (capable == 1 with a positive rate).")
        return res

    # Cursor per line: next free hour. Reset implicitly by the week floor.
    cursor: dict[int, float] = {}
    load: dict[int, float] = {}
    rows: list[dict[str, Any]] = []

    # AZAP order: week first, then priority (1 = most important), then order_id.
    work = demand.copy()
    work["_week"] = [int(_num(v, 0.0)) for v in work.get("week_index", pd.Series([0] * len(work)))]
    work["_prio"] = [_num(v, 99.0) for v in work.get("priority", pd.Series([99] * len(work)))]
    work = work.sort_values(["_week", "_prio", "order_id"], kind="stable")

    for _, r in work.iterrows():
        order_id = str(r.get("order_id", "")).strip()
        sku = str(r.get("sku", "")).strip()
        qty = _num(r.get("qty_target"), 0.0)
        week = int(_num(r.get("_week"), 0.0))

        if qty <= 0:
            res.unplaced.append({"order_id": order_id, "sku": sku, "reason": "qty_target is zero or unreadable"})
            continue

        options = lines_for_sku.get(sku)
        if not options:
            res.unplaced.append({"order_id": order_id, "sku": sku, "reason": "no capable line in capabilities_rates.csv"})
            continue

        # The order's own due window in the planning frame (inclusive
        # due_end_hour -> exclusive +1, the solver/scorecard convention);
        # the 168 h grid only when the file carries no due hours.
        ds = r.get("due_start_hour")
        de = r.get("due_end_hour")
        if ds is not None and de is not None and _num(ds, -1.0) >= 0 and _num(de, -1.0) >= 0:
            week_start = max(0.0, _num(ds) - shift_h)
            week_end = min(float(horizon_hours), _num(de) + 1.0 - shift_h)
        else:
            week_start = week * WEEK_HOURS
            week_end = min(float(horizon_hours), (week + 1) * WEEK_HOURS)
        if week_end <= week_start:
            res.unplaced.append({"order_id": order_id, "sku": sku,
                                 "reason": "due window lies outside the horizon"})
            continue

        if strategy == "least_loaded":
            options = sorted(options, key=lambda t: (load.get(t[1], 0.0), -t[0]))

        placed = False
        for rate, lid, lname in options:
            run_h = qty / rate
            start = max(cursor.get(lid, 0.0), week_start)
            end = start + run_h
            if end > week_end + 1e-9:
                continue
            cursor[lid] = end
            load[lid] = load.get(lid, 0.0) + run_h
            rows.append({
                "block_id": _new_id(),
                "block_type": "production",
                "line_id": lid,
                "line_name": lname,
                "start_h": round(start, 3),
                "end_h": round(end, 3),
                "label": sku,
                "order_id": order_id,
                "sku": sku,
                "sku_description": descriptions.get(sku, ""),
                "qty_kg": round(qty, 3),
                "locked": False,
                "attrs": f"naive:week{week}",
            })
            res.placed.append({
                "order_id": order_id,
                "sku": sku,
                "line": lname,
                "week": week,
                "run_h": round(run_h, 2),
                "start_h": round(start, 2),
                "end_h": round(end, 2),
            })
            placed = True
            break

        if not placed:
            res.unplaced.append({
                "order_id": order_id,
                "sku": sku,
                "reason": f"no room left in week {week} (h{week_start:g}-{week_end:g}) on any capable line",
            })

    if not rows:
        res.notes.append("Nothing could be placed -- see the unplaced list.")
        return res

    cal = pd.DataFrame(rows)
    for col in CALENDAR_COLUMNS:
        if col not in cal.columns:
            cal[col] = None
    res.calendar = cal[CALENDAR_COLUMNS].sort_values(["line_id", "start_h"]).reset_index(drop=True)
    res.notes.append(
        f"Placed {len(res.placed)} of {len(work)} orders on "
        f"{res.calendar['line_id'].nunique()} line(s). No changeovers, CIP or optimization applied."
    )
    return res
