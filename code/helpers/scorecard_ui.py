# helpers/scorecard_ui.py — Shared Streamlit rendering for scorecards.

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from helpers.scorecard_engine import FORMULA_HELP, ScorecardResult, contribution_breakdown


def render_scorecard(
    result: ScorecardResult | dict[str, Any],
    *,
    show_formulas: bool = True,
    show_contribution: bool = True,
) -> None:
    if isinstance(result, ScorecardResult):
        data = result.to_dict()
    else:
        data = result

    composite = data.get("composite")
    cats = data.get("category_scores") or {}

    top = st.columns(7)
    top[0].metric("Composite", f"{composite:.0f}" if composite is not None else "n/a")
    for i, key in enumerate(["service", "changeovers", "cip", "campaigns", "maintenance", "trials"], start=1):
        v = cats.get(key)
        top[i].metric(key.title(), f"{v:.0f}" if v is not None else "n/a")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.subheader("Changeovers")
        co = data.get("changeovers") or {}
        st.write(f"**Recipe changes:** {co.get('recipe_changes', '—')}")
        st.write(f"**Format changes:** {co.get('format_changes', '—')}")
        st.write(f"**Total CO hours:** {co.get('total_co_hours', '—')}")
        st.subheader("CIP")
        cip = data.get("cip") or {}
        st.write(f"**Count:** {cip.get('cip_count', '—')}")
        st.write(f"**Hours:** {cip.get('cip_hours', '—')}")
        st.write(f"**Forfeited CIP hours:** {cip.get('cip_forfeited_h', '—')}")
    with c2:
        st.subheader("Trials")
        tr = data.get("trials") or {}
        st.write(f"**Hours:** {tr.get('trial_hours', '—')}")
        st.write(f"**Disruptions:** {tr.get('trial_disruptions', '—')}")
        st.subheader("Maintenance")
        m = data.get("maintenance") or {}
        st.write(f"**Aligned with CIP:** {m.get('maint_aligned', '—')}")
        st.write(f"**Aligned hours:** {m.get('maint_aligned_hours', '—')}")
        st.write(f"**Conflicts:** {m.get('maint_conflicts', '—')}")
    with c3:
        st.subheader("Campaigns")
        camp = data.get("campaigns") or {}
        st.write(f"**Avg run size (h):** {camp.get('avg_run_h', '—')}")
        st.write(f"**Short-run count:** {camp.get('short_run_count', '—')}")
        st.write(f"**Production hours:** {camp.get('total_prod_h', '—')}")
        st.subheader("Service")
        svc = data.get("service") or {}
        if not svc.get("available", True) and svc.get("orders_late") is None:
            st.write("n/a — load demand_plan in `data/reference/`")
        else:
            st.write(f"**Orders at risk:** {svc.get('orders_at_risk', '—')}")
            st.write(f"**Orders late:** {svc.get('orders_late', '—')}")
            st.write(f"**Excess inventory (kg):** {svc.get('excess_inventory_kg', '—')}")

    notes = data.get("notes") or []
    if notes:
        for n in notes:
            st.caption(n)

    if show_contribution:
        render_contribution(data)

    if show_formulas:
        with st.expander("How these metrics are calculated (draft v0)"):
            for k, text in (data.get("formulas") or FORMULA_HELP).items():
                st.markdown(f"**{k}** — {text}")


def render_contribution(result: ScorecardResult | dict[str, Any]) -> None:
    rows = contribution_breakdown(result)
    if not rows:
        return
    with st.expander("Why this score (category contribution)", expanded=False):
        st.caption(
            "Each category score (0–100) × weight → contribution to composite. "
            "Cap saturation means a raw metric is already at/above its draft-v0 cap."
        )
        st.dataframe(
            pd.DataFrame(rows)[["category", "score", "weight", "contribution", "cap_saturation"]],
            use_container_width=True,
            hide_index=True,
        )


def render_delta_strip(
    baseline: ScorecardResult | dict[str, Any],
    proposed: ScorecardResult | dict[str, Any],
    *,
    title: str = "Δ vs baseline",
) -> None:
    b = baseline.to_dict() if isinstance(baseline, ScorecardResult) else baseline
    p = proposed.to_dict() if isinstance(proposed, ScorecardResult) else proposed
    b_comp = b.get("composite")
    p_comp = p.get("composite")
    b_cats = b.get("category_scores") or {}
    p_cats = p.get("category_scores") or {}

    st.markdown(f"**{title}**")
    cols = st.columns(7)
    if b_comp is not None and p_comp is not None:
        delta = float(p_comp) - float(b_comp)
        cols[0].metric("Composite", f"{p_comp:.0f}", f"{delta:+.1f} vs AZAP {b_comp:.0f}")
    else:
        cols[0].metric("Composite", f"{p_comp:.0f}" if p_comp is not None else "n/a")

    for i, key in enumerate(["service", "changeovers", "cip", "campaigns", "maintenance", "trials"], start=1):
        pv = p_cats.get(key)
        bv = b_cats.get(key)
        if pv is None:
            cols[i].metric(key.title(), "n/a")
        elif bv is None:
            cols[i].metric(key.title(), f"{pv:.0f}")
        else:
            cols[i].metric(key.title(), f"{pv:.0f}", f"{float(pv) - float(bv):+.1f}")


def scorecard_table(
    results: list[dict[str, Any]],
    *,
    azap_composite: float | None = None,
) -> pd.DataFrame:
    # Chronological for prior-week delta (list_scorecards is reverse chrono)
    chronological = list(reversed(results))
    prev_comp: float | None = None
    rows = []
    for r in chronological:
        comp = r.get("composite")
        delta_prev = None
        if comp is not None and prev_comp is not None:
            try:
                delta_prev = round(float(comp) - float(prev_comp), 1)
            except (TypeError, ValueError):
                delta_prev = None
        delta_azap = None
        if comp is not None and azap_composite is not None:
            try:
                delta_azap = round(float(comp) - float(azap_composite), 1)
            except (TypeError, ValueError):
                delta_azap = None
        rows.append({
            "week": r.get("week_label"),
            "scored_at": r.get("scored_at"),
            "composite": comp,
            "Δ vs prior": delta_prev,
            "Δ vs AZAP": delta_azap,
            "recipe_co": (r.get("changeovers") or {}).get("recipe_changes"),
            "format_co": (r.get("changeovers") or {}).get("format_changes"),
            "co_hours": (r.get("changeovers") or {}).get("total_co_hours"),
            "cip_count": (r.get("cip") or {}).get("cip_count"),
            "cip_forfeited": (r.get("cip") or {}).get("cip_forfeited_h"),
            "short_runs": (r.get("campaigns") or {}).get("short_run_count"),
            "late": (r.get("service") or {}).get("orders_late"),
        })
        if comp is not None:
            try:
                prev_comp = float(comp)
            except (TypeError, ValueError):
                pass
    # Show newest first again
    rows.reverse()
    return pd.DataFrame(rows)
