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

    Netted rows (fix C58 / netting-6): after Scenario F staging a row that
    received committed credit carries explicit qty_min/qty_max, blank pct
    columns and a `credit_kg` column (demand_coverage.apply_ledger,
    explicit_bounds). The stock check computes `achievable` against the
    GROSS demand row, so the cap belongs on the gross too:
    ``qty_max = min(qty_max, max(0, achievable x gross - credit))`` with
    gross = qty_target + credit_kg. Applying the ratio to the NET target
    let 120437-W0 keep a 5,196 kg cap although components supported only
    ~1,127 kg more after the committed MO.
    """
    if demand is None or demand.empty or not dns:
        return demand, []
    df = demand.copy()
    notes: list[str] = []
    has_explicit = "qty_max" in df.columns
    if has_explicit:
        for col in ("qty_min", "qty_max"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    for idx, r in df.iterrows():
        sku = str(r.get("sku", ""))
        if sku not in dns:
            continue
        achievable = dns[sku]
        target = float(r.get("qty_target", 0) or 0)
        lo_raw, hi_raw = r.get("lower_pct"), r.get("upper_pct")
        pct_mode = pd.notna(lo_raw) and pd.notna(hi_raw) if has_explicit \
            else True
        if pct_mode:
            old_lo = float(lo_raw if pd.notna(lo_raw) else 0.9)
            old_hi = float(hi_raw if pd.notna(hi_raw) else 1.1)
            new_hi = round(min(old_hi, achievable), 4)
            df.at[idx, "lower_pct"] = 0.0
            df.at[idx, "upper_pct"] = new_hi
            notes.append(
                f"{r.get('order_id')}: components support {achievable:.0%} — "
                f"qty_min {target * old_lo:,.0f}→0 kg, "
                f"qty_max {target * old_hi:,.0f}→{target * new_hi:,.0f} kg")
            continue
        # explicit (netted) bounds: cap on the GROSS, minus the credit
        credit = float(pd.to_numeric(pd.Series([r.get("credit_kg")]),
                                     errors="coerce").fillna(0.0).iloc[0])
        gross = target + credit
        old_min = float(r.get("qty_min")) if pd.notna(r.get("qty_min")) else target
        old_max = float(r.get("qty_max")) if pd.notna(r.get("qty_max")) else target
        new_max = round(min(old_max, max(0.0, achievable * gross - credit)), 1)
        df.at[idx, "qty_min"] = 0.0
        df.at[idx, "qty_max"] = new_max
        notes.append(
            f"{r.get('order_id')}: components support {achievable:.0%} of "
            f"{gross:,.0f} kg gross ({credit:,.0f} kg already committed) — "
            f"qty_min {old_min:,.0f}→0 kg, qty_max {old_max:,.0f}→{new_max:,.0f} kg")
    return df, notes
