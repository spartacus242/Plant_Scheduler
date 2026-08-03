# helpers/scorecard_ui.py — Shared Streamlit rendering for scorecards.

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from helpers.scorecard_engine import FORMULA_HELP, ScorecardResult


def render_scorecard(result: ScorecardResult | dict[str, Any], *, show_formulas: bool = True) -> None:
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

    if show_formulas:
        with st.expander("How these metrics are calculated (draft v0)"):
            for k, text in (data.get("formulas") or FORMULA_HELP).items():
                st.markdown(f"**{k}** — {text}")


def scorecard_table(results: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append({
            "week": r.get("week_label"),
            "scored_at": r.get("scored_at"),
            "composite": r.get("composite"),
            "recipe_co": (r.get("changeovers") or {}).get("recipe_changes"),
            "format_co": (r.get("changeovers") or {}).get("format_changes"),
            "co_hours": (r.get("changeovers") or {}).get("total_co_hours"),
            "cip_count": (r.get("cip") or {}).get("cip_count"),
            "cip_forfeited": (r.get("cip") or {}).get("cip_forfeited_h"),
            "short_runs": (r.get("campaigns") or {}).get("short_run_count"),
            "late": (r.get("service") or {}).get("orders_late"),
        })
    return pd.DataFrame(rows)
