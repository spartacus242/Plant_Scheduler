# pages/reconcile.py — Reconcile: what needs attention BEFORE you plan.
#
# The daily loop's second step (charter §2.2): Connect → RECONCILE → Plan →
# Lock & Export → Track. This page is a thin renderer over
# helpers.reconcile_engine — the same findings the future planning agent will
# read; the engine holds every rule, the page only draws.

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers import data_health as dh
from helpers import reconcile_engine as rec
from helpers.config import load_toml
from helpers.paths import data_dir

st.header("Reconcile")
st.caption(
    "Everything that needs attention **before** you plan: stock that won't "
    "cover a run, demand with nothing scheduled, cleaning coming due, "
    "capability conflicts, physical impossibilities. Fix or acknowledge, "
    "then move to the Plant Calendar."
)

dd = data_dir()
cfg = load_toml()

# --- Live-feed staleness banner (planning on stale data is the #1 trap) ---
_live = [h for h in dh.assess(dd, cfg)
         if h.key in ("manprg", "cip_info", "demand_summary")]
_bad = [h for h in _live if h.state in (dh.MISSING, dh.ERROR)]
_stale = [h for h in _live if h.state == dh.STALE]
if _bad:
    st.error("**Live data missing/unreadable:** "
             + "; ".join(f"{h.name} — {h.detail}" for h in _bad))
elif _stale:
    st.warning("**Live data stale:** "
               + "; ".join(f"{h.name} — {h.detail}" for h in _stale))

# --- Stock report: the ONE persisted report every page shares -------------
# (~35s to compute, milliseconds to load). This page never recomputes on
# its own: the saved report renders instantly, a stale signature only
# raises the banner, and the Refresh button recomputes + saves for every
# surface (Home and Stock Check included). Findings and the ledger derive
# from the report in well under a second, so they stay live per rerun.
from helpers.stock_report_cache import load_cached, refresh_report

_btn_l, _btn_r = st.columns([5, 1.3])
with _btn_r:
    _refresh = st.button(
        "Refresh", type="primary",
        help="Recompute the shared stock report (also feeds Home and Stock "
             "Check), then re-derive every finding from it.")
_cached = load_cached(dd)
if _refresh or _cached is None:
    with st.spinner("Checking component stock…"):
        _cached = refresh_report(dd)
stock_report = _cached.report
_when = _cached.computed_at.replace("T", " ")
if _cached.stale:
    _btn_l.warning(f"Inputs changed since the saved stock report (computed "
                   f"{_when}) — the findings below still read the saved "
                   "one. Press **Refresh** to recompute.")
else:
    _btn_l.caption(f"Stock report computed {_when} — saved; this page loads "
                   "instantly until the inputs change.")

# --- Coverage ledger: demand plan vs committed MOs (built once, shared) ---
try:
    from helpers.demand_coverage import build_ledger_from_data
    ledger = build_ledger_from_data(dd, cfg)
except Exception as _exc:  # noqa: BLE001 — a broken feed must not kill the page
    ledger = None
    st.caption(f"Demand coverage ledger unavailable: {_exc}")

findings = rec.assess_plan(dd, cfg, stock_report=stock_report,
                           coverage_ledger=ledger)
counts = rec.summary(findings)

# --- Summary strip --------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Blocking", counts.get(rec.BLOCKING, 0))
c2.metric("Needs attention", counts.get(rec.WARN, 0))
c3.metric("Info", counts.get(rec.INFO, 0))
with c4:
    st.page_link("pages/calendar.py", label="Go plan →",
                 icon=":material/drag_indicator:")

# --- Demand coverage table (committed MOs + made kg vs the demand plan) ---
if ledger is not None and ledger.rows:
    ldf = ledger.to_frame()
    # default view: this ISO week onward; the past is history, not a plan item
    _cur = ledger.anchor_week_key
    with st.expander("Demand coverage — committed MOs vs the demand plan",
                     expanded=False):
        st.caption(
            "The demand plan is **gross** — Flowstate subtracts committed MO "
            "kg (and kg already made this window) per SKU per ISO week; "
            "surplus carries **forward** only. This is exactly the netting "
            "Scenario F stages with, so a COVERED week will not be re-planned."
        )
        show_past = st.checkbox("Show past weeks", value=False,
                                key="rec_ledger_past")
        view = ldf if (show_past or _cur is None) else \
            ldf[ldf["week_key"] >= _cur]
        n_cov = int((view["status"] == "COVERED").sum())
        m1, m2, m3 = st.columns(3)
        m1.metric("Demand kg (shown weeks)", f"{view['gross_kg'].sum():,.0f}")
        m2.metric("Already covered", f"{view['applied_kg'].sum():,.0f}")
        m3.metric("Net still to plan", f"{view['net_kg'].sum():,.0f}")
        st.caption(f"{n_cov} of {len(view)} order(s) fully covered."
                   + (f" {ledger.unknown_kg_blocks} committed block(s) have "
                      "unknown kg and credit nothing."
                      if ledger.unknown_kg_blocks else ""))
        st.dataframe(
            view.drop(columns=["week_key"]).rename(columns={
                "week_label": "Week", "order_id": "Order", "sku": "SKU",
                "gross_kg": "Demand kg", "committed_kg": "Committed kg",
                "produced_kg": "Made kg", "carry_in_kg": "Carry-in kg",
                "applied_kg": "Covered kg", "net_kg": "Net to plan",
                "status": "Status"}),
            use_container_width=True, hide_index=True)

if not findings:
    st.success("Nothing needs attention — the plan is clean. Go plan.")
    st.stop()

_SEV_LABEL = {rec.BLOCKING: "🟥 Blocking", rec.WARN: "🟨 Needs attention",
              rec.INFO: "ℹ️ Informational"}

for sev in (rec.BLOCKING, rec.WARN, rec.INFO):
    group = [f for f in findings if f.severity == sev]
    if not group:
        continue
    st.subheader(_SEV_LABEL[sev])
    for f in group:
        box = st.error if sev == rec.BLOCKING else (
            st.warning if sev == rec.WARN else st.info)
        box(f"**{f.title}**\n\n{f.detail}\n\n→ {f.action}")
        cols = st.columns([1, 5])
        with cols[0]:
            if f.page:
                st.page_link(f.page, label="Open", icon=":material/arrow_forward:")
        with cols[1]:
            if f.context:
                with st.expander("Detail", expanded=False):
                    # Order lists render as tables; everything else as JSON.
                    ctx = f.context
                    if "orders" in ctx and isinstance(ctx["orders"], list):
                        import pandas as pd
                        st.dataframe(pd.DataFrame(ctx["orders"]),
                                     use_container_width=True, hide_index=True)
                    else:
                        st.json(ctx, expanded=False)
