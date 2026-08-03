# diagnostics.py — Diagnostic passes for Flowstate Phase 2 scheduler.

from __future__ import annotations
import math
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Tuple

import pandas as pd

from data_loader import Params, Data, available_hours_line

if TYPE_CHECKING:
    pass

WEEK0_END = 167  # due_end_hour <= 167 -> Week 0
WEEK1_START = 168


def available_hours_line_week(P: Params, data: Data, l: int, week: int) -> int:
    """Available hours on line l for a single week (168h window)."""
    if week == 0:
        w_start, w_end = 0, 168
    else:
        w_start, w_end = 168, 336
    avail_from = int(data.init_map.get(l, {}).get("available_from", 0))
    blocked = max(0, min(avail_from, w_end) - w_start) if avail_from > w_start else 0
    for dt in data.downtimes:
        if dt["line_id"] != l:
            continue
        s = max(w_start, dt["start"])
        e = min(w_end, dt["end"])
        if e > s:
            blocked += e - s
    return max(0, 168 - blocked)


def run_diagnostics(P: Params, data: Data, data_dir: Path) -> None:
    """Write diag_order_linecap.csv: per-order capacity UB with and without top-3 line cap."""
    rows = []
    for o in data.orders:
        ds, de = int(o["due_start"]), int(o["due_end"])
        sku = o["sku"]
        qmin = int(o["qty_min"])
        caps = []
        for l in data.lines:
            if not data.capable.get((l, sku)) or (data.rate.get((l, sku), 0) <= 0):
                continue
            w = max(0, min(P.horizon_h, de + 1) - max(0, ds))
            avail_from = data.init_map.get(l, {}).get("available_from", 0)
            s0 = max(ds, avail_from)
            e0 = de + 1
            w = max(0, e0 - s0)
            for dt in data.downtimes:
                if dt["line_id"] != l:
                    continue
                s = max(s0, dt["start"])
                e = min(e0, dt["end"])
                if e > s:
                    w -= e - s
            if w <= 0:
                continue
            r = int(round(data.rate.get((l, sku), 0)))
            caps.append((l, r * w))
        caps.sort(key=lambda x: x[1], reverse=True)
        ub_all = sum(v for _, v in caps)
        ub_top3 = sum(v for _, v in caps[:3])
        rows.append(
            dict(
                order_id=o["order_id"],
                sku=sku,
                qty_min=qmin,
                lines_all=len(caps),
                ub_noCIP_all=caps and ub_all or 0,
                ub_noCIP_top3=ub_top3,
            )
        )
    df = pd.DataFrame(rows)
    df.to_csv(data_dir / "diag_order_linecap.csv", index=False)


def run_unique_line_load_diagnostic(P: Params, data: Data, data_dir: Path) -> None:
    """Write diag_unique_line_load.csv: required vs available hours by line/week for single-capable-line orders."""
    rows = []
    for l in data.lines:
        avail_base = available_hours_line(P, data, l)
        carry = int(data.init_map.get(l, {}).get("carryover_run_hours", 0))
        for week in (0, 1):
            required = 0.0
            order_ids = []
            for o in data.orders:
                ds, de = int(o["due_start"]), int(o["due_end"])
                if week == 0 and de > WEEK0_END:
                    continue
                if week == 1 and de <= WEEK0_END:
                    continue
                sku = o["sku"]
                qmin = int(o["qty_min"])
                capable_lines = [
                    ll
                    for ll in data.lines
                    if data.capable.get((ll, sku)) and (data.rate.get((ll, sku)) or 0) > 0
                ]
                if len(capable_lines) != 1:
                    continue
                if l != capable_lines[0]:
                    continue
                rate = data.rate.get((l, sku)) or 0
                if rate <= 0:
                    continue
                required += qmin / rate
                order_ids.append(o["order_id"])
            required_rounded = int(math.ceil(required))
            cip_count = 0
            if carry + required_rounded >= 240:
                cip_count = 2
            elif carry + required_rounded >= 120:
                cip_count = 1
            available_hours = max(0, avail_base - cip_count * P.cip_duration_h)
            overflow = max(0, required_rounded - available_hours)
            rows.append(
                dict(
                    line_id=l,
                    week=week,
                    required_run_hours=required_rounded,
                    available_hours=available_hours,
                    overflow_hours=overflow,
                    order_count=len(order_ids),
                    order_ids="|".join(order_ids) if order_ids else "",
                )
            )
    if rows:
        pd.DataFrame(rows).to_csv(data_dir / "diag_unique_line_load.csv", index=False)


def _order_in_week(o: dict, week: int) -> bool:
    de = int(o["due_end"])
    if week == 0 and de > WEEK0_END:
        return False
    if week == 1 and de <= WEEK0_END:
        return False
    return True


def run_blockages_diagnostic(P: Params, data: Data, data_dir: Path, two_phase: bool = False) -> None:
    """
    Identify overloaded (line, week) and suggest concrete changes to
    Capabilities & Rates, DemandPlan, or InitialStates to achieve feasibility.
    Writes diag_blockages.csv and diag_blockages.txt.
    When two_phase=True, uses per-week (168h) available hours instead of full horizon.
    """
    line_names = data.line_names
    blockages: List[dict] = []
    report_lines: List[str] = []

    for l in data.lines:
        avail_base = available_hours_line(P, data, l)
        carry = int(data.init_map.get(l, {}).get("carryover_run_hours", 0))
        init = data.init_map.get(l, {})
        available_from = int(init.get("available_from", 0))
        long_shutdown = int(init.get("long_shutdown_flag", 0))

        for week in (0, 1):
            # Total required run hours on this line in this week (all capable orders in window)
            contributions: List[Tuple[str, str, int, float, int]] = []  # order_id, sku, qty_min, run_hours, num_capable_lines
            required_raw = 0.0
            for o in data.orders:
                if not _order_in_week(o, week):
                    continue
                sku = o["sku"]
                if not data.capable.get((l, sku)) or (data.rate.get((l, sku)) or 0) <= 0:
                    continue
                qmin = int(o["qty_min"])
                rate = data.rate.get((l, sku)) or 0
                run_h = qmin / rate
                required_raw += run_h
                num_capable = sum(
                    1
                    for ll in data.lines
                    if data.capable.get((ll, sku)) and (data.rate.get((ll, sku)) or 0) > 0
                )
                contributions.append((o["order_id"], sku, qmin, run_h, num_capable))

            required_rounded = int(math.ceil(required_raw))
            cip_count = 0
            if carry + required_rounded >= 240:
                cip_count = 2
            elif carry + required_rounded >= 120:
                cip_count = 1
            # Use per-week hours in two-phase mode, full horizon otherwise
            if two_phase:
                week_avail = available_hours_line_week(P, data, l, week)
            else:
                week_avail = avail_base
            available_hours = max(0, week_avail - cip_count * P.cip_duration_h)
            overflow = max(0, required_rounded - available_hours)

            if overflow <= 0:
                continue

            # Build suggestions
            cap_suggestions: List[str] = []
            demand_suggestions: List[str] = []
            init_suggestions: List[str] = []
            for order_id, sku, qty_min, run_h, num_capable in sorted(
                contributions, key=lambda x: -x[3]
            ):
                if num_capable == 1:
                    cap_suggestions.append(
                        f"capabilities_rates.csv: set capable=1 for another line (e.g. add a row line_id=<other>, sku={sku}) so order {order_id} can use a second line."
                    )
                demand_suggestions.append(
                    f"demand_plan.csv: relax order {order_id} (SKU {sku}, qty_min={qty_min}) e.g. lower lower_pct from 0.9 to 0.85–0.88 so more demand can move to Week-1 or other lines."
                )
            if available_from > 0:
                init_suggestions.append(
                    f"initial_states.csv: line_id={l} ({line_names.get(l, str(l))}) has available_from_hour={available_from}; reducing it may free capacity if the line is blocked late."
                )
            if long_shutdown == 1:
                init_suggestions.append(
                    f"initial_states.csv: line_id={l} has long_shutdown_flag=1 (extra setup); consider 0 if no longer in long shutdown."
                )

            blockages.append({
                "line_id": l,
                "line_name": line_names.get(l, f"L{l}"),
                "week": week,
                "required_run_hours": required_rounded,
                "available_hours": available_hours,
                "overflow_hours": overflow,
                "order_count": len(contributions),
                "order_ids": "|".join(c[0] for c in contributions),
                "suggestion_capability": " ".join(cap_suggestions[:3]),
                "suggestion_demand": " ".join(demand_suggestions[:2]),
                "suggestion_initial_states": " ".join(init_suggestions[:2]),
            })

            report_lines.append("")
            report_lines.append(f"--- Line {l} ({line_names.get(l, str(l))}) Week {week} ---")
            report_lines.append(f"  Required: {required_rounded} h  Available: {available_hours} h  Overflow: {overflow} h")
            report_lines.append("  Contributing orders (order_id, sku, qty_min, run_hours, num_capable_lines):")
            for order_id, sku, qty_min, run_h, num_capable in sorted(
                contributions, key=lambda x: -x[3]
            ):
                report_lines.append(f"    {order_id}  {sku}  {qty_min}  {run_h:.1f}  {num_capable}")
            report_lines.append("  Suggested changes:")
            for s in cap_suggestions[:3]:
                report_lines.append(f"    • {s}")
            for s in demand_suggestions[:2]:
                report_lines.append(f"    • {s}")
            for s in init_suggestions[:2]:
                report_lines.append(f"    • {s}")

    if blockages:
        for b in blockages:
            b.setdefault("suggestion_min_run_hours", "Try --min-run-hours 4 or 2 to relax and achieve feasibility.")
        pd.DataFrame(blockages).to_csv(data_dir / "diag_blockages.csv", index=False)
        header = [
            "Blockages: lines/weeks where required run hours exceed available (with CIP).",
            "Apply suggested changes to the listed CSV files to move toward a feasible schedule.",
            "You can also try lowering min run hours (e.g. --min-run-hours 4 or 2) to achieve feasibility.",
            "",
        ]
        with open(data_dir / "diag_blockages.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(header + report_lines))
    else:
        with open(data_dir / "diag_blockages.txt", "w", encoding="utf-8") as f:
            f.write(
                "No line/week overload found by aggregate required vs available hours.\n"
                "Infeasibility may be due to changeover sequencing, due-window overlap, or per-order line cap.\n"
            )
