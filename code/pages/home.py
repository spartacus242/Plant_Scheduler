# pages/home.py -- Command Center: the daily loop with live status.
#
# The sidebar walks the loop; this page answers "where am I in it today?"
# One row per step (Connect -> Reconcile -> Plan -> Lock & Export -> Track),
# each with a live status chip, a one-line detail and a deep link. The old
# 7-stage SVG pipeline predated the Reconcile step and read as a developer
# diagram — the loop list IS the process (user: "the UI should follow our
# process for scheduling").

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers import data_health as dh
from helpers import overnight_results as onr
from helpers.config import load_toml
from helpers.paths import data_dir
from helpers.week_lock import read_lock

st.header("Flowstate — Command Center")
st.caption("Operational truth → digital twin → optimizer. Walk the loop top to bottom.")

dd = data_dir()
cfg = load_toml()

# Cache health for ~60s so the page doesn't stat the disk on every rerun.
@st.cache_data(ttl=60, show_spinner=False)
def _health(data_dir_str: str) -> list[dict]:
    return [vars(h) for h in dh.assess(Path(data_dir_str), cfg)]


# Reconcile counts must match the Reconcile page's headline numbers, so the
# stock report is built with the SAME inputs (walkthrough 2026-08-17: Home
# said "2 blocking, 3 attention" while Reconcile said 3/7/1). Building the
# VIF snapshot can be slow or fail — then Home falls back to the no-stock
# counts and SAYS so instead of silently under-counting.
@st.cache_data(ttl=60, show_spinner=False)
def _reconcile_counts(data_dir_str: str) -> tuple[dict[int, int], bool]:
    from helpers import reconcile_engine as rec
    dd_ = Path(data_dir_str)
    stock = None
    try:
        vif, toggles = rec.stock_report_inputs(dd_)
        if Path(vif).exists():
            from stockcheck.api import stock_check_report
            stock = stock_check_report(dd_, vif, toggles=toggles or None)
    except Exception:  # noqa: BLE001 — a broken VIF share must not take Home down
        stock = None
    findings = rec.assess_plan(dd_, cfg, stock_report=stock)
    return rec.summary(findings), stock is None


health = [dh.HealthStatus(**h) for h in _health(str(dd))]

# ---------------------------------------------------------------------------
# Freshness banner
# ---------------------------------------------------------------------------
counts = dh.summary(health)
n_bad = counts[dh.MISSING] + counts[dh.ERROR]
n_stale = counts[dh.STALE]
if n_bad:
    st.error(f"**{n_bad} data source(s) missing or unreadable**, {n_stale} stale — see below.")
elif n_stale:
    st.warning(f"**{n_stale} data source(s) stale** — see the loop and actions below.")
else:
    st.success(f"All data sources are present and fresh ({counts[dh.OK]} OK).")

# ---------------------------------------------------------------------------
# The daily loop — one row per step, live status
# ---------------------------------------------------------------------------
_CHIP = {"ok": ":green[● OK]", "warn": ":orange[▲ ATTENTION]",
         "bad": ":red[✕ BLOCKED]", "off": ":gray[· NOT SET]"}


def _worst(states: list[str]) -> str:
    if any(s in (dh.MISSING, dh.ERROR) for s in states):
        return "bad"
    if any(s == dh.STALE for s in states):
        return "warn"
    return "ok"


def _step_connect() -> tuple[str, str]:
    # Judge the SAME rows the banner counts (walkthrough 2026-08-17: the
    # banner said "3 stale" over an "all fresh" Connect chip because this
    # step filtered out the semantic rules).
    state = _worst([h.state for h in health])
    stale = [h.name for h in health if h.state == dh.STALE]
    bad = [h.name for h in health if h.state in (dh.MISSING, dh.ERROR)]
    if bad:
        more = f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""
        return state, "missing/unreadable: " + ", ".join(bad[:3]) + more
    if stale:
        more = f" (+{len(stale) - 3} more)" if len(stale) > 3 else ""
        return state, "stale: " + ", ".join(stale[:3]) + more
    return state, "all inputs present and fresh"


def _step_reconcile() -> tuple[str, str]:
    try:
        rc, no_stock = _reconcile_counts(str(dd))
    except Exception as exc:  # noqa: BLE001
        return "warn", f"could not assess: {exc}"
    suffix = " (without stock findings)" if no_stock else ""
    blocking, warn = rc.get(2, 0), rc.get(1, 0)
    if blocking:
        return "bad", f"{blocking} blocking, {warn} needing attention{suffix}"
    if warn:
        return "warn", f"{warn} finding(s) need attention{suffix}"
    return "ok", f"nothing needs attention{suffix}"


def _step_plan() -> tuple[str, str]:
    anchor = [h for h in health if h.key == "calendar_anchor"]
    cal_path = dd / "calendar_blocks.csv"
    if not cal_path.exists():
        return "bad", "no calendar yet — import or generate one"
    try:
        with open(cal_path, encoding="utf-8") as fh:
            n = max(0, sum(1 for _ in fh) - 1)
    except OSError:
        n = 0
    if anchor and anchor[0].state == dh.STALE:
        return "warn", f"{n} blocks — {anchor[0].detail}"
    return "ok", f"{n} blocks on the calendar"


def _step_lock() -> tuple[str, str]:
    lock = read_lock(dd)
    if lock is None:
        return "off", "no lock set — lock weeks 1–2 when the plan is ready"
    return "ok", f"locked through {lock:%a %Y-%m-%d %H:%M}"


def _step_track() -> tuple[str, str]:
    sc = [h for h in health if h.key == "scorecard"]
    if sc and sc[0].state == dh.STALE:
        return "warn", sc[0].detail
    if sc:
        return "ok", sc[0].detail
    return "off", "no scorecard history yet"


_STEPS = [
    ("1 · Connect", "pages/data.py", _step_connect),
    ("2 · Reconcile", "pages/reconcile.py", _step_reconcile),
    ("3 · Plan", "pages/calendar.py", _step_plan),
    # Lock & Export moved onto the Plant Calendar (2026-08-19); step 4 is the
    # compare/promote gate, still chipped with the lock state — "locked
    # through X" IS the ready-to-ship signal.
    ("4 · Compare & Promote", "pages/compare.py", _step_lock),
    ("5 · Track", "pages/scorecard.py", _step_track),
]

st.subheader("Today")
for title, page, fn in _STEPS:
    state, detail = fn()
    c1, c2, c3, c4 = st.columns([2, 2, 6, 1.5])
    c1.markdown(f"**{title}**")
    c2.markdown(_CHIP[state])
    c3.caption(detail)
    with c4:
        st.page_link(page, label="Open", icon=":material/arrow_forward:")

# Overnight optimizer — not a numbered step (the batch runs while nobody is
# here), but the same row grammar so the morning scan stays one pass:
# NOT SET (no generation yet) / OK (< 26h) / STALE (older).
_ov = onr.summarize(dd)
c1, c2, c3, c4 = st.columns([2, 2, 6, 1.5])
c1.markdown("**☾ Overnight optimizer**")
c2.markdown(_CHIP[{onr.OK: "ok", onr.STALE: "warn"}.get(_ov.state, "off")])
c3.caption(_ov.detail)
with c4:
    st.page_link("pages/generate.py", label="Review",
                 icon=":material/arrow_forward:")
_brief = onr.load_brief(dd) if _ov.state != onr.NOT_SET else None
if _brief:
    with st.expander("Morning brief (data/optimizer/brief.md)"):
        st.markdown(_brief)

# ---------------------------------------------------------------------------
# What you need to do next
# ---------------------------------------------------------------------------
actions = dh.next_actions(health, limit=5)
st.subheader("What you need to do next")
if actions:
    for i, a in enumerate(actions, 1):
        st.markdown(f"{i}. {a}")
else:
    st.success("Nothing blocking right now. Score the week to snapshot history.")

# ---------------------------------------------------------------------------
# Data status table (full detail, collapsed — the loop rows carry the summary)
# ---------------------------------------------------------------------------
st.divider()
with st.expander("Data status (every source, age and cadence)", expanded=False):
    st.caption(f"Data folder: `{dd}`")
    rows = []
    for h in health:
        rows.append({
            "Status": h.state,
            "Source": h.name,
            "Age": dh._fmt_age(h.age_h) if h.age_h is not None else "—",
            "Cadence": f"{h.cadence_h:g} h" if h.cadence_h is not None else "—",
            "Detail": h.detail,
        })
    # st.table, not st.dataframe: the glide data grid never mounts inside an
    # initially-collapsed expander (verified live 2026-08-18 — the expander
    # opened onto an empty 400px box), a static table always renders.
    st.table(pd.DataFrame(rows).set_index("Source"))
    st.page_link("pages/data.py", label="Upload / edit data files", icon=":material/upload_file:")

with st.expander("About Flowstate"):
    st.markdown(
        """
The optimizer is maybe 20-30% of the project. The hard part is the **operational truth model**
that scores a schedule the same way every week.

The daily loop (the sidebar walks it): **Connect** live data → **Reconcile** what needs
attention → **Plan** (solver ↔ drag & drop; lock 2 weeks and export the final schedule
right on the calendar) → **Compare & Promote** (versions side by side, MO changes back
to VIF) → **Track** the honest scorecard. Each week the lock rolls forward.

AZAP (`data/reference/demand_plan.csv`) is the corporate **demand plan**: which SKUs,
how many kg, which week. It never assigns lines. The line schedule
(`data/calendar_blocks.csv`) is the plant's own, built by the production planner.

The optimizer lives in `code/solver/` (CP-SAT).
"""
    )
