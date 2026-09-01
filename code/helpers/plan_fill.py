# helpers/plan_fill.py — Scenario F: committed plan fixed, fill the tail.
#
# The user's process (design doc scenario-f-fill-the-tail-2026-08-14):
# manprg + cip_info ARE the plan for the committed stretch — laid out as
# FIXED, non-negotiable line-time. The solver's only job is placing
# demand_plan orders into what remains: right tonnage on the right ISO week,
# minimum changeovers, tails packed. Nothing here ever rewrites the plant's
# own plan — there is no current_mo.csv and no mo_changes in Scenario F.
#
# Pure functions; the IO shell lives in scenario_runner._overlay_fill.

from __future__ import annotations

import math
from typing import Any

import pandas as pd

# Layer-1 CIPs are projected at the line's real max interval through the
# whole horizon, so consecutive cleans are never further apart than that
# interval — any fill production between them is bounded by construction.
# The solver's own CIP generator must therefore stand down: a huge interval
# means its dirty-clock never forces a clean of its own (which would double
# up with the projected ones).
SOLVER_CIP_INTERVAL_STANDDOWN_H = 100_000


def pinned_blocks(calendar: pd.DataFrame) -> pd.DataFrame:
    """Planner-pinned production blocks: demand orders fixed to a line/time
    on the Plant Calendar (attrs token 'pinned', toggled in the block popup).

    They join the committed layer — blocked windows the solver must plan
    around, and their kg credits the demand targets — but they must NEVER
    move the fill gate or the changeover base: a block pinned deep in W35
    would otherwise block fill of the whole line before it (the same trap
    projected CIPs hit; see line_free_from).

    Exact-token match on the ';'-separated attrs — a substring test would
    also match future tokens like 'unpinned'. Blocks carrying a
    current_state:* token are EXCLUDED even if pinned: they ARE manprg MOs,
    already committed via build_current_state — staging them again would
    double-block the window and double-credit their kg against demand.
    """
    if calendar is None or calendar.empty or "attrs" not in calendar.columns:
        return (calendar.iloc[0:0] if calendar is not None
                else pd.DataFrame())
    attrs = calendar["attrs"].astype(str)
    mask = (
        attrs.str.split(";").apply(lambda ts: "pinned" in ts)
        & ~attrs.str.contains("current_state:", regex=False)
        & (calendar["block_type"].astype(str) == "production")
    )
    return calendar[mask]


def planner_cip_blocks(calendar: pd.DataFrame) -> pd.DataFrame:
    """CIP blocks the PLANNER placed on the Plant Calendar (block popup /
    gap picker, 2026-09-01) — including the re-forecast projected cleans
    they trigger (attrs token planner:cip_projected). Committed windows the
    solver plans around, exactly like pinned production. Plant-derived CIPs
    (current_state:* tokens) and display overlays (cipinfo_/dt_ ids) are
    excluded: those are re-staged from cip_info / downtimes.csv."""
    if calendar is None or calendar.empty or "attrs" not in calendar.columns:
        return (calendar.iloc[0:0] if calendar is not None
                else pd.DataFrame())
    attrs = calendar["attrs"].astype(str)
    ids = calendar["block_id"].astype(str)
    mask = (
        (calendar["block_type"].astype(str) == "cip")
        & ~attrs.str.contains("current_state:", regex=False)
        & ~ids.str.startswith(("cipinfo_", "dt_"))
    )
    return calendar[mask]


def materialize_required_cips(
    calendar: pd.DataFrame,
    co_map: dict,
    cip_h: float,
    *,
    attrs: str = "solver:cip_req",
) -> tuple[pd.DataFrame, list[str]]:
    """Draw the clean the solver left room for (user rule 2026-09-01).

    For every adjacent production pair on a line whose changeovers.csv row
    is cip_req_after == 1 and whose gap holds no CIP block, insert a cip
    block at the earlier run's end. The solver's setup floor (data_loader)
    guarantees the slot; a gap that still comes up short (committed
    boundaries, rounding) is REPORTED, never filled with an overlapping
    block. Gap containment mirrors score_changeovers' waiver rule exactly,
    so what this draws is precisely what the scorecard then waives.
    Returns (calendar with the new rows, human notes).
    """
    import uuid

    notes: list[str] = []
    if calendar is None or calendar.empty:
        return calendar, notes
    cal = calendar.copy()
    cips: dict[Any, list[tuple[float, float]]] = {}
    for _, c in cal[cal["block_type"].astype(str) == "cip"].iterrows():
        cips.setdefault(c["line_id"], []).append(
            (float(c["start_h"]), float(c["end_h"])))
    prod = cal[cal["block_type"].astype(str) == "production"].sort_values(
        ["line_id", "start_h"])
    new_rows: list[dict] = []
    for line_id, grp in prod.groupby("line_id"):
        rows = grp.to_dict("records")
        line_cips = list(cips.get(line_id, []))
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            fs, ts = str(a.get("sku", "")), str(b.get("sku", ""))
            if fs == ts:
                continue
            flags = co_map.get((fs, ts))
            if not flags or int(flags.get("cip_req_after", 0) or 0) != 1:
                continue
            a_end, b_start = float(a["end_h"]), float(b["start_h"])
            if any(cs >= a_end - 1e-6 and ce <= b_start + 1e-6
                   for cs, ce in line_cips):
                continue
            gap = b_start - a_end
            if gap + 1e-6 < cip_h:
                notes.append(
                    f"{a.get('line_name')}: {fs}->{ts} at {a_end:.1f}h needs a "
                    f"{cip_h:g}h clean but the gap is {gap:.1f}h — left for the "
                    "planner")
                continue
            new_rows.append({
                "block_id": "cip_" + uuid.uuid4().hex[:10],
                "block_type": "cip",
                "line_id": a.get("line_id"),
                "line_name": a.get("line_name"),
                "start_h": a_end,
                "end_h": a_end + float(cip_h),
                "label": "CIP",
                "order_id": "",
                "sku": "CIP",
                "sku_description": f"required clean {fs}->{ts}",
                "qty_kg": None,
                "locked": False,
                "attrs": attrs,
            })
            line_cips.append((a_end, a_end + float(cip_h)))
            notes.append(f"{a.get('line_name')}: CIP drawn at {a_end:.1f}h "
                         f"for {fs}->{ts}")
    if new_rows:
        cal = pd.concat([cal, pd.DataFrame(new_rows, columns=cal.columns)],
                        ignore_index=True)
    return cal, notes


def committed_windows(blocks: pd.DataFrame, horizon_h: float) -> list[dict]:
    """Every committed block (production, trial, cip) as a blocked window.

    Rows in downtimes.csv shape. Clipped to [0, horizon]; windows are rounded
    OUTWARD (floor start, ceil end) so a fill block can never shave an hour
    off committed work. Fully-past windows are dropped.
    """
    out: list[dict] = []
    if blocks is None or not len(blocks):
        return out
    for _, b in blocks.iterrows():
        s = math.floor(max(0.0, float(b["start_h"])))
        e = math.ceil(min(float(horizon_h), float(b["end_h"])))
        if e <= 0 or e <= s:
            continue
        label = str(b.get("block_type", "")).upper()
        ref = str(b.get("order_id") or b.get("sku") or "")
        out.append({
            "line_id": int(b.get("line_id", 0) or 0),
            "line_name": str(b["line_name"]).upper(),
            "start_hour": int(s),
            "end_hour": int(e),
            "reason": f"Committed {label} {ref}".strip(),
        })
    return out


def last_sku_per_line(blocks: pd.DataFrame) -> dict[str, str]:
    """SKU of the LAST committed production block per line (changeover base
    for the first fill block). Trials/CIPs have no changeover identity."""
    out: dict[str, str] = {}
    if blocks is None or not len(blocks):
        return out
    prod = blocks[blocks["block_type"] == "production"]
    for line, grp in prod.groupby(prod["line_name"].astype(str).str.upper()):
        last = grp.sort_values("end_h").iloc[-1]
        sku = str(last.get("sku") or "").strip()
        if sku and sku.upper() not in ("CIP", "TRIALS"):
            out[line] = sku
    return out


def line_free_from(blocks: pd.DataFrame, horizon_h: float) -> dict[str, float]:
    """First hour each line is free of committed WORK (production + trials),
    clipped to [0, horizon]. Fill may not start before this — gaps INSIDE the
    committed stretch belong to the plant team, not the solver.

    Projected CIPs are deliberately EXCLUDED: they are spaced through the
    whole horizon, so counting them gated every line at ~504h, clamped every
    order's window capacity to zero and made an EMPTY schedule "OPTIMAL"
    (measured on F run 2). Future CIPs block fill via their windows instead.
    """
    out: dict[str, float] = {}
    if blocks is None or not len(blocks):
        return out
    work = blocks[blocks["block_type"].isin(["production", "trial"])]
    for line, grp in work.groupby(work["line_name"].astype(str).str.upper()):
        out[line] = float(min(horizon_h, max(0.0, grp["end_h"].max())))
    return out


def rebase_demand(
    demand: pd.DataFrame,
    shift_h: float,
    horizon_h: float,
) -> tuple[pd.DataFrame, list[str]]:
    """Shift demand due windows from the demand file's anchor frame into the
    staging frame (staging hour 0 = shift_h hours after the demand anchor).

    The demand import is self-anchoring (Monday of its earliest ISO week),
    but the rolling horizon anchors at TODAY. Staged unshifted, every due
    window read late by the gap — with a Friday anchor the whole demand grid
    slid +4 days, so "this week's" leftovers looked placeable through next
    Thursday and next week's demand looked due a week out (user report
    2026-08-14: holding buckets inverted vs reality).

    Weeks whose shifted window ends at/before hour 0 are OVER — their unmet
    tonnage is a miss, not a plan item — and are dropped with a note.
    """
    notes: list[str] = []
    if demand is None or demand.empty or not shift_h:
        return demand, notes
    df = demand.copy()
    df["due_start_hour"] = (
        pd.to_numeric(df["due_start_hour"], errors="coerce") - shift_h
    ).clip(lower=0)
    df["due_end_hour"] = (
        pd.to_numeric(df["due_end_hour"], errors="coerce") - shift_h)
    past = df["due_end_hour"] <= 0
    if past.any():
        gone = df[past]
        notes.append(
            f"{int(past.sum())} order(s) dropped: their demand week ended "
            f"before the horizon starts ({gone['qty_target'].sum():,.0f} kg "
            "unmet is a MISS, not a plan item)")
        df = df[~past].copy()
    beyond = pd.to_numeric(df["due_start_hour"], errors="coerce") >= horizon_h
    if beyond.any():
        notes.append(f"{int(beyond.sum())} order(s) dropped: due week starts "
                     "beyond the horizon")
        df = df[~beyond].copy()
    df["due_end_hour"] = df["due_end_hour"].clip(upper=horizon_h - 1)
    return df, notes


def subtract_committed(
    demand: pd.DataFrame,
    blocks: pd.DataFrame,
    week_bounds: list[float] | None = None,
    *,
    anchor=None,
    completed: list[dict] | None = None,
    history_demand: dict[tuple[str, int], float] | None = None,
    lookback_weeks: int = 4,
) -> tuple[pd.DataFrame, list[str]]:
    """Reduce demand targets by what the committed plan already produces.

    Thin wrapper over helpers.demand_coverage — the SKU×week ledger is the
    single netting engine (Reconcile and the holding cards read the same
    math). Pass `anchor` (the frame of the block/demand hour offsets) to
    key by TRUE ISO weeks; `week_bounds` / the 168h grid are the anchorless
    fallback. `completed` (current_state's dropped completed-MO rows) adds
    kg already MADE; `history_demand` lets past production settle against
    its own week's demand first. Targets never go below zero; the pct
    bounds stay, so qty_min/qty_max scale with the reduced target.
    """
    if demand is None or demand.empty:
        return demand, []
    from helpers.demand_coverage import apply_ledger, build_ledger

    ledger = build_ledger(
        demand, blocks, completed=completed, anchor=anchor,
        week_bounds=week_bounds, history_demand=history_demand,
        lookback_weeks=lookback_weeks)
    return apply_ledger(demand, ledger)


def coalesce_windows(rows: list[dict]) -> list[dict]:
    """Union of blocked windows per line -> disjoint rows.

    Committed MO windows can OVERLAP existing line-downs (the plant plans
    onto P11/P13 even while downtimes.csv says they are down - the exact
    inconsistency Reconcile flags). Two overlapping FIXED intervals make
    NoOverlap instantly infeasible at every relax level (measured on the
    first F run), so everything blocked is merged into one interval union.
    """
    by_line: dict[str, list[dict]] = {}
    for r in rows:
        by_line.setdefault(str(r["line_name"]).upper(), []).append(r)
    out: list[dict] = []
    for line, rs in sorted(by_line.items()):
        rs = sorted(rs, key=lambda r: (float(r["start_hour"]), float(r["end_hour"])))
        cur = None
        for r in rs:
            s0, e0 = float(r["start_hour"]), float(r["end_hour"])
            if cur is None:
                cur = dict(r)
                continue
            if s0 <= float(cur["end_hour"]) + 1e-9:   # overlap or touch: merge
                cur["end_hour"] = max(float(cur["end_hour"]), e0)
                if str(r["reason"]) not in str(cur["reason"]):
                    cur["reason"] = f"{cur['reason']} + {r['reason']}"[:120]
            else:
                out.append(cur)
                cur = dict(r)
        if cur is not None:
            out.append(cur)
    for r in out:
        r["start_hour"] = int(r["start_hour"])
        r["end_hour"] = int(round(float(r["end_hour"])))
    return out
