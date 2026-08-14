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

import pandas as pd

# Layer-1 CIPs are projected at the line's real max interval through the
# whole horizon, so consecutive cleans are never further apart than that
# interval — any fill production between them is bounded by construction.
# The solver's own CIP generator must therefore stand down: a huge interval
# means its dirty-clock never forces a clean of its own (which would double
# up with the projected ones).
SOLVER_CIP_INTERVAL_STANDDOWN_H = 100_000


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
    """First hour each line is free of committed work (max committed end,
    clipped to [0, horizon]). Fill may not start before this — gaps INSIDE
    the committed stretch belong to the plant team, not the solver."""
    out: dict[str, float] = {}
    if blocks is None or not len(blocks):
        return out
    for line, grp in blocks.groupby(blocks["line_name"].astype(str).str.upper()):
        out[line] = float(min(horizon_h, max(0.0, grp["end_h"].max())))
    return out


def subtract_committed(
    demand: pd.DataFrame,
    blocks: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str]]:
    """Reduce demand targets by what the committed plan already produces.

    Committed production kg is bucketed into the ISO-horizon week of the
    block's midpoint. Per SKU, weeks are consumed in order with surplus
    carrying FORWARD (this week's extra production is next week's
    inventory, never last week's). Targets never go below zero; the pct
    bounds stay, so qty_min/qty_max scale with the reduced target.
    """
    notes: list[str] = []
    if demand is None or demand.empty:
        return demand, notes
    committed: dict[tuple[str, int], float] = {}
    if blocks is not None and len(blocks):
        prod = blocks[blocks["block_type"] == "production"]
        for _, b in prod.iterrows():
            kg = pd.to_numeric(pd.Series([b.get("qty_kg")]), errors="coerce").iloc[0]
            if pd.isna(kg) or kg <= 0:
                continue
            mid = (float(b["start_h"]) + float(b["end_h"])) / 2.0
            wk = max(0, int(mid // 168))
            sku = str(b.get("sku") or "").strip()
            if not sku or sku.upper() in ("CIP", "TRIALS"):
                continue
            committed[(sku, wk)] = committed.get((sku, wk), 0.0) + float(kg)

    df = demand.copy()
    for sku, grp in df.groupby(df["sku"].astype(str)):
        carry = 0.0
        for idx in grp.sort_values("week_index").index:
            wk = int(df.at[idx, "week_index"])
            avail = committed.pop((sku, wk), 0.0) + carry
            if avail <= 0:
                carry = 0.0
                continue
            target = float(df.at[idx, "qty_target"] or 0)
            consumed = min(target, avail)
            carry = avail - consumed
            if consumed > 0:
                df.at[idx, "qty_target"] = round(target - consumed, 1)
                notes.append(
                    f"{df.at[idx, 'order_id']}: committed plan already makes "
                    f"{consumed:,.0f} kg — fill target {target:,.0f}→"
                    f"{target - consumed:,.0f} kg")
    return df, notes
