# helpers/greedy_fill.py — deterministic tail-packing seed for Scenario F.
#
# CP-SAT improves incumbents slowly on the joint fill+changeover structure
# (measured 2026-08-14: W35 fill plateaued at ~240-310t of 1,364t across
# 300s/600s budgets, dead-pair pruning and a fill-first decision strategy).
# So we CONSTRUCT a complete fill greedily — earliest week first, biggest
# orders first, same-SKU adjacency preferred, setup gaps inserted, CIP/
# committed windows respected — and hand it to the solver as a full warm
# start (prev_schedule.csv). The solver then spends its whole budget
# IMPROVING a dense plan instead of discovering one. Hints never change the
# feasible set: a flawed seed costs search time, never correctness.
#
# Pure functions — the IO shell lives in scenario_runner.

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class _LineState:
    line_id: int
    segments: list[tuple[float, float]] = field(default_factory=list)
    tail_sku: str = ""          # sku of the last block placed (or committed)
    fresh_segment: set = field(default_factory=set)  # seg start hours never used


def free_segments(gate_h: float, blocked: list[tuple[float, float]],
                  horizon_h: float) -> list[tuple[float, float]]:
    """Invert blocked windows over [gate, horizon] -> disjoint free spans."""
    s = max(0.0, float(gate_h))
    out: list[tuple[float, float]] = []
    for b0, b1 in sorted((max(0.0, a), min(horizon_h, b)) for a, b in blocked):
        if b1 <= s:
            continue
        if b0 > s:
            out.append((s, min(b0, horizon_h)))
        s = max(s, b1)
        if s >= horizon_h:
            break
    if s < horizon_h:
        out.append((s, horizon_h))
    return [(a, b) for a, b in out if b - a >= 1.0]


def build_greedy_fill(
    demand: list[dict],
    rates: dict[tuple[str, str], float],
    setups: dict[str, dict[str, float]],
    line_segments: dict[str, list[tuple[float, float]]],
    line_ids: dict[str, int],
    initial_sku: dict[str, str],
    *,
    co_cost: dict[str, dict[str, float]] | None = None,
    min_run_hours: int = 4,
    min_run_pct: float = 0.5,
    horizon_h: float = 504.0,
    max_lines_per_order: int = 2,
) -> tuple[list[dict], dict]:
    """Greedy fill. Returns (schedule_rows, summary).

    demand rows: order_id, sku, week_index, qty_target, lower_pct, upper_pct,
    due_start_hour, due_end_hour. Orders are placed earliest week first,
    largest first; per order the best lines are those with the CHEAPEST
    transition from their current tail SKU, then highest rate. A placement in
    a segment whose start was never used before needs no setup gap (segment
    boundaries are committed blocks/CIPs — the wash absorbs the changeover).

    co_cost is the format-aware transition cost (from_sku -> to_sku ->
    weighted machine cost: FFS/topload expensive, TTP cheap — mirroring the
    solver's [changeover] weights). Ranking by it makes lines develop format
    identities: a topload SKU chains onto a topload tail instead of splitting
    an FFS campaign. Without it (None) the ranking falls back to setup hours,
    which is format-BLIND — a topload swap and a TTP swap both read "2h"
    (measured 2026-08-14: 67-72 topload changes survived the solver because
    the seed baked them in). Physical gap time always comes from `setups`.
    """
    lines: dict[str, _LineState] = {}
    for ln, segs in line_segments.items():
        st = _LineState(line_id=line_ids.get(ln, 0),
                        segments=sorted(segs),
                        tail_sku=str(initial_sku.get(ln, "") or ""))
        st.fresh_segment = {a for a, _ in st.segments}
        lines[ln] = st

    def setup_h(frm: str, to: str) -> float:
        if not frm or frm == to:
            return 0.0
        return float((setups.get(frm) or {}).get(to, 0) or 0)

    def trans_cost(frm: str, to: str) -> float:
        """Ranking cost of switching a line's tail from `frm` to `to`."""
        if not frm or frm == to:
            return 0.0
        if co_cost is not None:
            c = (co_cost.get(frm) or {}).get(to)
            if c is not None:
                return float(c)
            # pair missing from the standards: assume worse than any known
            # transition (F weights top out ~360 for ffs+extras) but not so
            # absurd that rate ordering vanishes among several unknowns
            return 500.0
        return setup_h(frm, to)

    rows: list[dict] = []
    placed_kg_by_week: dict[int, float] = {}
    short_orders: list[str] = []

    orders = [d for d in demand
              if float(d.get("qty_target", 0) or 0) > 0]
    orders.sort(key=lambda d: (int(d.get("week_index", 0)),
                               -float(d.get("qty_target", 0) or 0)))

    for d in orders:
        sku = str(d["sku"])
        target = float(d["qty_target"])
        qmin = target * float(d.get("lower_pct", 0.9) or 0.9)
        qmax = target * float(d.get("upper_pct", 1.1) or 1.1)
        ds = float(d.get("due_start_hour", 0) or 0)
        de = min(horizon_h, float(d.get("due_end_hour", 0) or 0) + 1)
        remaining = target
        used_lines = 0

        cands = [(ln, rates.get((ln, sku), 0.0)) for ln in lines]
        cands = [(ln, r) for ln, r in cands if r > 0]
        # cheapest tail transition first (format-aware when co_cost given:
        # same SKU = 0, same format cheap, topload/FFS swaps expensive),
        # then physical setup hours, then fastest line
        cands.sort(key=lambda t: (
            trans_cost(lines[t[0]].tail_sku, sku),
            setup_h(lines[t[0]].tail_sku, sku),
            -t[1]))

        for ln, rate in cands:
            if remaining <= 0 or used_lines >= max_lines_per_order:
                break
            st = lines[ln]
            floor_h = max(min_run_hours,
                          math.ceil(min_run_pct * qmin / rate) if rate > 0 else 0)
            placed_here = False
            new_segs: list[tuple[float, float]] = []
            for (a, b) in list(st.segments):
                if remaining <= 0 or placed_here:
                    new_segs.append((a, b))
                    continue
                w0, w1 = max(a, ds), min(b, de)
                if w1 - w0 < 1.0:
                    new_segs.append((a, b))
                    continue
                gap = 0.0 if a in st.fresh_segment and w0 == a \
                    else setup_h(st.tail_sku, sku)
                start = w0 + gap
                avail = w1 - start
                need_h = math.ceil(remaining / rate)
                run = min(avail, need_h)
                # never place more kg than qmax allows
                run = min(run, math.floor(qmax / rate) if rate > 0 else run)
                if run < floor_h or run < 1:
                    new_segs.append((a, b))
                    continue
                run = math.floor(run)
                end = start + run
                kg = round(run * rate, 1)
                rows.append({
                    "line_id": st.line_id, "line_name": ln,
                    "order_id": str(d["order_id"]), "sku": sku,
                    "start_hour": int(start), "end_hour": int(end),
                    "run_hours": int(run), "qty_kg": kg,
                })
                wk = int(((start + end) / 2) // 168)
                placed_kg_by_week[wk] = placed_kg_by_week.get(wk, 0.0) + kg
                remaining -= kg
                st.tail_sku = sku
                st.fresh_segment.discard(a)
                # split the segment around the placement (+ its gap)
                if w0 - a >= 1.0:
                    new_segs.append((a, w0))
                if b - end >= 1.0:
                    new_segs.append((end, b))
                    st.fresh_segment.discard(end)  # continuation, not fresh
                placed_here = True
                used_lines += 1
            st.segments = sorted(new_segs)
        if remaining > max(0.0, target - qmin):
            short_orders.append(
                f"{d['order_id']} short {remaining:,.0f} kg of target")

    summary = {
        "rows": len(rows),
        "kg_by_week": {k: round(v) for k, v in sorted(placed_kg_by_week.items())},
        "orders_short": short_orders,
    }
    return rows, summary
