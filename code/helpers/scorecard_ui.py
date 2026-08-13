# helpers/scorecard_ui.py — Shared Streamlit rendering for scorecards.

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from helpers.labels import display_label
from helpers.scorecard_engine import (
    CATEGORY_DOCS,
    CATEGORY_ORDER,
    FORMULA_HELP,
    KNOWN_LIMITATIONS,
    ScorecardResult,
    category_weights,
    contribution_breakdown,
    metric_reference_rows,
)


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

    # Visual layer first: category bars + composite gauge (Command Center).
    render_scorecard_bars(data)

    top = st.columns(7)
    top[0].metric("Composite", f"{composite:.0f}" if composite is not None else "n/a")
    for i, key in enumerate(["service", "changeovers", "cip", "campaigns", "trials"], start=1):
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
        st.write(f"**Forfeited CIP (kg lost):** {cip.get('cip_forfeited_kg', '—')}")
        st.write(f"**Forfeited CIP hours:** {cip.get('cip_forfeited_h', '—')}")
    with c2:
        st.subheader("Trials")
        tr = data.get("trials") or {}
        st.write(f"**Hours:** {tr.get('trial_hours', '—')}")
        st.write(f"**Disruptions:** {tr.get('trial_disruptions', '—')}")
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
        render_metric_reference(data)


# Metric "no data" signals per category: when these raw values are empty/zero
# the category score is NOT evidence of good performance — it is absence of data.
_NO_DATA_KEYS: dict[str, tuple[str, ...]] = {
    "trials": ("trial_hours",),
    "cip": ("cip_count",),
    "service": ("orders_late",),
}


def _category_has_data(cat: str, data: dict[str, Any]) -> bool:
    """True when a category's raw metrics indicate real data (not absence).

    Trials with zero hours and CIP with zero blocks are 'no data' — the score
    of 100 is an artifact, not an achievement.
    """
    raw = data.get(cat) or {}
    keys = _NO_DATA_KEYS.get(cat, ())
    if not keys:
        return True
    for k in keys:
        v = raw.get(k)
        if v is not None and float(v or 0) > 0:
            return True
    # service: demand present means data exists even if all zero
    if cat == "service":
        return bool(raw.get("available", False))
    return False


def render_scorecard_bars(data: dict[str, Any]) -> None:
    """Category contribution bars + composite gauge.

    'No data' categories render gray with a 'no data' label instead of a green
    bar — absence of trials/CIP is not good performance.
    """
    composite = data.get("composite")
    cats = data.get("category_scores") or {}
    if not cats:
        return

    with st.container(border=True):
        g1, g2 = st.columns([1, 4])
        with g1:
            st.markdown("**Composite**")
            if composite is not None:
                st.progress(max(0.0, min(1.0, float(composite) / 100.0)))
                st.markdown(f"**{composite:.0f} / 100**")
            else:
                st.markdown("n/a")
        with g2:
            for key in CATEGORY_ORDER:
                score = cats.get(key)
                if score is None:
                    st.markdown(f"**{key.title()}** — n/a")
                    continue
                has_data = _category_has_data(key, data)
                if has_data:
                    st.markdown(f"**{key.title()}** — {score:.0f}")
                    st.progress(max(0.0, min(1.0, float(score) / 100.0)))
                else:
                    st.markdown(f"**{key.title()}** — :gray[no data]")
                    st.progress(0.0)
        st.caption(
            "Gray = no data for that category (e.g. zero CIP blocks), "
            "not a good score. Saturation notes appear in the contribution table."
        )


def render_metric_reference(
    result: ScorecardResult | dict[str, Any] | None = None,
    *,
    expanded: bool = False,
) -> None:
    """Full metric reference: formula, cap/target, scoring, weight and why.

    Documentation only - it reads the live config and the rendered scorecard and
    never recomputes a score. Pass `result` to show each metric's actual value
    next to its cap and flag cap saturation.
    """
    if isinstance(result, ScorecardResult):
        result = result.to_dict()
    try:
        rows_by_cat = metric_reference_rows(result)
        weights = category_weights()
    except Exception as e:  # config/reference problems must not kill the page
        with st.expander("How these metrics are calculated"):
            st.warning(f"Metric reference unavailable: {e}")
            for k, text in ((result or {}).get("formulas") or FORMULA_HELP).items():
                st.markdown(f"**{k}** - {text}")
        return

    with st.expander("How these metrics are calculated (full reference)", expanded=expanded):
        st.markdown(
            "**Composite = sum(weight x category_score) / sum(weight of scored "
            "categories).** Each category score is the plain MEAN of its sub-metric "
            "scores. Every sub-metric is normalised to 0-100 before averaging:\n\n"
            "- *lower is better*: `score = clamp(100 * (1 - value / cap), 0, 100)` - "
            "a value **at or above its cap scores 0**.\n"
            "- *higher is better*: `score = clamp(100 * value / target, 0, 100)` - "
            "used for `avg_run_h` against `campaign_run_floor_h`: the score ramps 0 -> 100 up "
            "to the floor and **stays 100 above it**, so only short runs lose "
            "points and long campaigns are never penalised.\n\n"
            "Caps and targets are read live from `flowstate.toml [scorecard]`, so the "
            "numbers below are the ones actually in force."
        )

        wdf = pd.DataFrame(
            [
                {
                    "category": c,
                    "weight": weights[c],
                    "share of composite": f"{weights[c] / sum(weights.values()):.0%}",
                    "what it answers": CATEGORY_DOCS[c],
                }
                for c in CATEGORY_ORDER
            ]
        )
        st.markdown("**Category weights**")
        st.dataframe(wdf, use_container_width=True, hide_index=True)
        st.caption(
            "A category that scores n/a (currently only Service, when demand_plan.csv "
            "is missing) is dropped from the composite and the remaining weights "
            "renormalise - so the same composite number can mean different things."
        )

        cats = (result or {}).get("category_scores") or {}
        saturated_all = [
            r for rows in rows_by_cat.values() for r in rows if r.get("saturation_note")
        ]
        if saturated_all:
            st.markdown("**Cap saturation in this scorecard**")
            for r in saturated_all:
                st.markdown(f"- `{r['saturation_note']}`")
            st.caption(
                "A saturated metric has hit the floor of its scale: it can get worse "
                "in reality without the score moving."
            )

        st.divider()
        for cat in CATEGORY_ORDER:
            rows = rows_by_cat.get(cat) or []
            if not rows:
                continue
            score = cats.get(cat)
            score_txt = f"{score:.0f}/100" if score is not None else "n/a"
            st.markdown(
                f"#### {cat.title()} - weight {weights[cat]:.2f}"
                + (f" - score {score_txt}" if result else "")
            )
            st.caption(CATEGORY_DOCS[cat])
            table = pd.DataFrame(
                [
                    {
                        "metric": r["metric"],
                        "value": "n/a" if r["value"] is None else f"{r['value']:g}",
                        "cap / target": r["cap_or_target"],
                        "flag": "AT CAP -> 0" if r["saturated"] else "",
                        "formula": r["formula"],
                        "how it is scored": r["how_scored"],
                        "why it matters": r["why_it_matters"],
                    }
                    for r in rows
                ]
            )
            if not result:
                table = table.drop(columns=["value", "flag"])
            st.dataframe(table, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("**Known limitations of draft v0 - read the score with these in mind**")
        for title, text in KNOWN_LIMITATIONS:
            st.markdown(f"- **{title}.** {text}")


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
        cols[0].metric("Composite", f"{p_comp:.0f}", f"{delta:+.1f} vs base {b_comp:.0f}")
    else:
        cols[0].metric("Composite", f"{p_comp:.0f}" if p_comp is not None else "n/a")

    for i, key in enumerate(["service", "changeovers", "cip", "campaigns", "trials"], start=1):
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
    baseline_composite: float | None = None,
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
        delta_base = None
        if comp is not None and baseline_composite is not None:
            try:
                delta_base = round(float(comp) - float(baseline_composite), 1)
            except (TypeError, ValueError):
                delta_base = None
        rows.append({
            "week": display_label(r.get("week_label")),
            "scored_at": r.get("scored_at"),
            "composite": comp,
            "Δ vs prior": delta_prev,
            "Δ vs current": delta_base,
            "recipe_co": (r.get("changeovers") or {}).get("recipe_changes"),
            "format_co": (r.get("changeovers") or {}).get("format_changes"),
            "co_hours": (r.get("changeovers") or {}).get("total_co_hours"),
            "cip_count": (r.get("cip") or {}).get("cip_count"),
            "cip_forfeited_kg": (r.get("cip") or {}).get("cip_forfeited_kg"),
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
