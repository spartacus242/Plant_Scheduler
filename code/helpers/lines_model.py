# helpers/lines_model.py -- A/B double-line model for the Bossar lines P17-P22.
#
# Domain truth (from the plant owner):
#   * P09-P16 are "Volpak" SINGLE lines.
#   * P17-P22 are "Bossar" DOUBLE lines: each is physically two parallel sides,
#     A and B (P17A/P17B ... P22A/P22B).
#   * The two sides share common upstream and downstream equipment, so both
#     sides ALWAYS run the same SKU at the same time.
#   * Either side can be down independently. With exactly one side down the
#     line keeps producing at EXACTLY HALF rate. With both sides down it
#     produces nothing.
#   * Confirmed in the data: P17 nominal=2246 / calc=1080 is exactly 2x the
#     P09 single-line 1123 / 540. So per-side rate = line rate / 2.
#
# EVERY other module must use this helper instead of re-deriving A/B logic.

from __future__ import annotations

import math
import re
from typing import Iterable, Mapping, Optional, Sequence

# Groups that are physically double (two sides sharing common equipment).
DOUBLE_GROUPS: tuple[str, ...] = ("P17", "P18", "P19", "P20", "P21", "P22")

SIDES: tuple[str, str] = ("A", "B")

# Stable line_id allocation.
#   0..7   -> P09..P16   (single lines, unchanged)
#   8..13  -> P17..P22   RESERVED for the *group* (never emitted as a row after
#                        migration; kept so historical line_id references still
#                        resolve to the right group).
#   100+   -> the per-side rows: P17A=100, P17B=101, P18A=102, ... P22B=111.
GROUP_LINE_IDS: dict[str, int] = {g: 8 + i for i, g in enumerate(DOUBLE_GROUPS)}
SIDE_ID_BASE = 100

_SIDE_RE = re.compile(r"^(?P<group>[A-Za-z]*\d+)(?P<side>[AB])$")


def side_line_id(group: str, side: str) -> int:
    """Stable line_id for a side row, e.g. P17A -> 100, P22B -> 111."""
    g = str(group).strip().upper()
    s = str(side).strip().upper()
    if g not in DOUBLE_GROUPS or s not in SIDES:
        raise ValueError(f"not a double-line side: {group}/{side}")
    return SIDE_ID_BASE + 2 * DOUBLE_GROUPS.index(g) + SIDES.index(s)


def group_of(line: str) -> str:
    """'P17A' -> 'P17'; 'P17' -> 'P17'; 'P09' -> 'P09'."""
    name = str(line or "").strip()
    m = _SIDE_RE.match(name)
    if m and m.group("group").upper() in DOUBLE_GROUPS:
        return m.group("group").upper()
    return name.upper()


def side_of(line: str) -> Optional[str]:
    """'P17A' -> 'A'; 'P17' -> None (whole line); 'P09' -> None."""
    name = str(line or "").strip()
    m = _SIDE_RE.match(name)
    if m and m.group("group").upper() in DOUBLE_GROUPS:
        return m.group("side").upper()
    return None


def is_double(line: str) -> bool:
    """True for a double-line group or either of its sides."""
    return group_of(line) in DOUBLE_GROUPS


def sides_of(group: str) -> list[str]:
    """['P17A', 'P17B'] for a double line; ['P09'] for a single line."""
    g = group_of(group)
    if g in DOUBLE_GROUPS:
        return [f"{g}{s}" for s in SIDES]
    return [g]


def n_sides(group: str) -> int:
    return 2 if is_double(group) else 1


def per_side_rate(line_rate: float, group: str = "") -> float:
    """Rate of ONE side. Half the whole-line rate on a double line."""
    r = float(line_rate or 0.0)
    if group and not is_double(group):
        return r
    return r / 2.0


def whole_line_rate(side_rate: float, group: str = "") -> float:
    """Inverse of per_side_rate: both sides running."""
    r = float(side_rate or 0.0)
    if group and not is_double(group):
        return r
    return r * 2.0


# ---------------------------------------------------------------------------
# Downtime handling
# ---------------------------------------------------------------------------

Interval = tuple[float, float]
DowntimeMap = Mapping[str, Sequence[Interval]]


def _covers(intervals: Optional[Sequence[Interval]], hour: float) -> bool:
    if not intervals:
        return False
    for start, end in intervals:
        if float(start) <= hour < float(end):
            return True
    return False


def sides_down_at(group: str, hour: float, downtime: DowntimeMap) -> int:
    """How many sides of `group` are down at `hour` (0/1/2, or 0/1 single)."""
    g = group_of(group)
    down = 0
    whole_down = _covers(downtime.get(g), hour)
    for ln in sides_of(g):
        if whole_down or _covers(downtime.get(ln), hour):
            down += 1
    return down


def effective_rate(group: str, hour: float, downtime: DowntimeMap, full_rate: float) -> float:
    """Throughput of `group` at `hour`.

    Full rate when both sides are up, exactly half when one side is down,
    zero when both are down. Single lines are all-or-nothing.
    """
    g = group_of(group)
    rate = float(full_rate or 0.0)
    if rate <= 0:
        return 0.0
    down = sides_down_at(g, hour, downtime)
    total = n_sides(g)
    up = total - down
    if up <= 0:
        return 0.0
    return rate * (up / total)


def hours_for_qty(
    group: str,
    qty_kg: float,
    start_hour: float,
    downtime: DowntimeMap,
    full_rate: float,
    *,
    max_hours: int = 20000,
) -> Optional[float]:
    """Hours needed to make `qty_kg` on `group` starting at `start_hour`.

    Integrates available capacity hour by hour: full rate when both sides are
    up, half when exactly one side is down, zero when both are down. Returns
    None when the line can never produce the quantity (rate 0 / fully down).
    """
    rate = float(full_rate or 0.0)
    remaining = float(qty_kg or 0.0)
    if rate <= 0:
        return None
    if remaining <= 0:
        return 0.0
    h = float(start_hour or 0.0)
    elapsed = 0.0
    guard = 0
    while remaining > 1e-9:
        guard += 1
        if guard > max_hours:
            return None
        cap = effective_rate(group, h + elapsed, downtime, rate)
        if cap > 0:
            if cap >= remaining:
                elapsed += remaining / cap
                remaining = 0.0
                break
            remaining -= cap
        elapsed += 1.0
    return math.ceil(elapsed)


def qty_over_window(
    group: str,
    start_hour: float,
    end_hour: float,
    downtime: DowntimeMap,
    full_rate: float,
) -> float:
    """Kg producible on `group` between start_hour and end_hour."""
    rate = float(full_rate or 0.0)
    if rate <= 0 or end_hour <= start_hour:
        return 0.0
    total = 0.0
    h = float(start_hour)
    while h < end_hour:
        step = min(1.0, float(end_hour) - h)
        total += effective_rate(group, h, downtime, rate) * step
        h += step
    return total


# ---------------------------------------------------------------------------
# Frame / payload helpers
# ---------------------------------------------------------------------------

def downtime_from_rows(rows: Iterable[Mapping]) -> dict[str, list[Interval]]:
    """Build a downtime map from any iterable of dict-like rows.

    Accepts both downtimes.csv rows (line_name/start_hour/end_hour) and
    calendar_blocks rows (line_name/start_h/end_h). Only line_down and
    maintenance block types count as downtime.
    """
    out: dict[str, list[Interval]] = {}
    for r in rows:
        btype = str(r.get("block_type", "") or "").strip().lower()
        if btype and btype not in ("line_down", "maintenance"):
            continue
        name = str(r.get("line_name", "") or "").strip().upper()
        if not name:
            continue
        start = r.get("start_hour", r.get("start_h"))
        end = r.get("end_hour", r.get("end_h"))
        try:
            s = float(start)
            e = float(end)
        except (TypeError, ValueError):
            continue
        if e <= s:
            continue
        out.setdefault(name, []).append((s, e))
    return out


def group_rate_from_caps(
    caps: Mapping[str, Mapping[str, float]],
    group: str,
    sku: str,
) -> float:
    """Whole-line rate for (group, sku) from a line_name -> sku -> rate map.

    Works whether the caps map is keyed by the group (pre-migration) or by
    the individual sides (post-migration, halved rates).
    """
    g = group_of(group)
    direct = float((caps.get(g) or {}).get(str(sku), 0) or 0)
    if direct > 0:
        return direct
    total = 0.0
    for ln in sides_of(g):
        total += float((caps.get(ln) or {}).get(str(sku), 0) or 0)
    return total


def expand_caps_with_groups(
    caps: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Ensure both group-level and side-level keys exist in a caps map.

    * Group present, sides missing  -> add sides at half rate.
    * Sides present, group missing  -> add the group at the summed rate.
    Existing keys are never overwritten.
    """
    for g in DOUBLE_GROUPS:
        side_names = sides_of(g)
        group_map = caps.get(g)
        side_maps = [caps.get(s) for s in side_names]
        if group_map and not any(side_maps):
            for s in side_names:
                caps[s] = {sku: per_side_rate(rate, g) for sku, rate in group_map.items()}
        elif any(side_maps) and not group_map:
            merged: dict[str, float] = {}
            for sm in side_maps:
                for sku, rate in (sm or {}).items():
                    merged[sku] = merged.get(sku, 0.0) + float(rate or 0.0)
            caps[g] = merged
    return caps


def line_rows_to_groups(rows: Iterable[Mapping]) -> list[dict]:
    """Collapse a lines.csv record list into one display row per group.

    Returns dicts with line_id / line_name / line_group / is_double / sides,
    preserving input order and using the reserved group line_id for doubles.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        name = str(r.get("line_name", "") or "").strip()
        if not name:
            continue
        g = group_of(r.get("line_group") or name)
        if g in seen:
            continue
        seen.add(g)
        double = is_double(g)
        try:
            lid = int(r.get("line_id"))
        except (TypeError, ValueError):
            lid = GROUP_LINE_IDS.get(g, len(out))
        if double:
            lid = GROUP_LINE_IDS.get(g, lid)
        out.append({
            "line_id": lid,
            "line_name": g,
            "line_group": g,
            "is_double": double,
            "sides": sides_of(g) if double else [],
        })
    return out
