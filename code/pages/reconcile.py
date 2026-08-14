# pages/reconcile.py — Reconcile: what needs attention BEFORE you plan.
#
# The daily loop's second step (charter §2.2): Connect → RECONCILE → Plan →
# Lock & Export → Track. This page is a thin renderer over
# helpers.reconcile_engine — the same findings the future planning agent will
# read; the engine holds every rule, the page only draws.

from __future__ import annotations

import json
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

# --- Stock report: reuse the Stock Check page's settings + cache ----------
SC_SETTINGS = Path(dd) / "stockcheck" / "settings.json"


@st.cache_data(ttl=300, show_spinner="Checking component stock…")
def _stock_report(vif_folder: str, toggles_json: str) -> dict | None:
    try:
        from stockcheck.api import stock_check_report
        return stock_check_report(dd, vif_folder,
                                  toggles=json.loads(toggles_json) or None)
    except Exception as exc:  # noqa: BLE001 — VIF share may be unreachable
        return {"error": str(exc)}


stock_report = None
try:
    _sc = json.loads(SC_SETTINGS.read_text(encoding="utf-8")) \
        if SC_SETTINGS.exists() else {}
except Exception:  # noqa: BLE001
    _sc = {}
_vif = str(_sc.get("vif_folder", "")).strip() or str(
    Path(dd) / "stockcheck" / "dev_vif")
if Path(_vif).exists():
    stock_report = _stock_report(_vif, json.dumps(_sc.get("toggles", {}),
                                                  sort_keys=True))
else:
    st.caption(f"Stock check skipped — VIF folder not reachable (`{_vif}`). "
               "Set it on the Stock Check page.")

findings = rec.assess_plan(dd, cfg, stock_report=stock_report)
counts = rec.summary(findings)

# --- Summary strip --------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Blocking", counts.get(rec.BLOCKING, 0))
c2.metric("Needs attention", counts.get(rec.WARN, 0))
c3.metric("Info", counts.get(rec.INFO, 0))
with c4:
    st.page_link("pages/calendar.py", label="Go plan →",
                 icon=":material/drag_indicator:")

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
