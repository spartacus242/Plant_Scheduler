# helpers/agent_policy.py — the planning agent's input policies (pure).
#
# Dry-run 1 (2026-08-14) showed the CP-SAT solver is component-blind: it
# poured 1,093 t across 49 blocks into SKUs the stock check marks
# DO_NOT_SCHEDULE. These policies are the agent's approved macro adjustments
# (user sign-off 2026-08-14): they rewrite the solver's WORK-DIR demand copy
# — never data/reference — and every change is returned as a note so the
# proposal's reasoning shows exactly what the agent excluded and why.

from __future__ import annotations

import math

import pandas as pd


def dns_ratios(stock_report: dict) -> dict[str, float]:
    """SKU -> worst achievable ratio, for demand rows stock marks
    DO_NOT_SCHEDULE. A missing/None/inf ratio reads as 0.0 (unknown supply
    is treated as none — the conservative direction for a trim policy)."""
    out: dict[str, float] = {}
    for d in stock_report.get("demand_view", []):
        if d.get("status") != "DO_NOT_SCHEDULE":
            continue
        sku = str(d.get("sku", ""))
        r = d.get("achievable_ratio")
        r = 0.0 if r is None or (isinstance(r, float) and math.isinf(r)) else max(0.0, float(r))
        out[sku] = min(out.get(sku, r), r)
    return out


def trim_dns_demand(
    demand: pd.DataFrame, dns: dict[str, float]
) -> tuple[pd.DataFrame, list[str]]:
    """Trim component-blocked demand in the solver's demand copy (pure).

    For every order whose SKU is in `dns`:
      * qty_min drops to 0 (lower_pct = 0) — the solver must never be FORCED
        to schedule a run the components cannot support;
      * qty_max is capped at the achievable ratio (upper_pct =
        min(upper_pct, achievable)) — it MAY still fill spare capacity up to
        what stock can actually build, but no further.

    Returns (new_frame, notes). Untouched orders are returned as-is.
    """
    if demand is None or demand.empty or not dns:
        return demand, []
    df = demand.copy()
    notes: list[str] = []
    for idx, r in df.iterrows():
        sku = str(r.get("sku", ""))
        if sku not in dns:
            continue
        achievable = dns[sku]
        old_lo = float(r.get("lower_pct", 0.9) or 0)
        old_hi = float(r.get("upper_pct", 1.1) or 0)
        new_hi = round(min(old_hi, achievable), 4)
        df.at[idx, "lower_pct"] = 0.0
        df.at[idx, "upper_pct"] = new_hi
        target = float(r.get("qty_target", 0) or 0)
        notes.append(
            f"{r.get('order_id')}: components support {achievable:.0%} — "
            f"qty_min {target * old_lo:,.0f}→0 kg, "
            f"qty_max {target * old_hi:,.0f}→{target * new_hi:,.0f} kg")
    return df, notes
