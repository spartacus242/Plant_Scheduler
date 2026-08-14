# pages/compare.py — Side-by-side version / scenario scorecard compare.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.calendar_io import load_calendar, save_calendar
from helpers.labels import display_name
from helpers.paths import data_dir
from helpers.scorecard_engine import ScorecardResult, delta_narrative, score_calendar
from helpers.scorecard_ui import render_delta_strip, render_scorecard
from helpers.version_manager import (
    delete_all_versions,
    delete_version,
    export_version_excel,
    list_versions,
    load_version,
    promote_version,
    rename_version,
    update_notes,
)

st.header("Version Compare")
st.caption(
    "Compare named options and solver scenarios against the current schedule. "
    "Raw metrics drive 'show me why'; the composite is only a conversation starter."
)

dd = data_dir()
versions = list_versions(dd)

# Official baseline score for reference
official = load_calendar(dd / "calendar_blocks.csv")
baseline = score_calendar(official, week_label="official", data_dir=dd) if not official.empty else None

OFFICIAL_KEY = "__official__"

if not versions and baseline is None:
    st.info("No versions yet. Save one from the Plant Calendar or Generate Scenarios.")
    st.stop()

if baseline:
    st.subheader("Current schedule (official calendar)")
    render_scorecard(baseline, show_formulas=False, show_contribution=True)

if not versions:
    st.info("No named versions yet — official calendar is shown above. Save options from Plant Calendar or Generate Scenarios.")
    st.stop()

# Side-by-side picker — default left = official AZAP when available
# display_name() remaps legacy on-disk names (e.g. the azap_baseline slug)
# without renaming anything under data/versions/.
names = {v["slug"]: display_name(v["slug"], v.get("name")) for v in versions}
left_options = ([OFFICIAL_KEY] if baseline else []) + list(names.keys())

def _fmt_left(s: str) -> str:
    if s == OFFICIAL_KEY:
        return "Current schedule (official)"
    return names.get(s, s)

default_left = OFFICIAL_KEY if baseline else list(names.keys())[0]
c1, c2 = st.columns(2)
with c1:
    left_slug = st.selectbox(
        "Baseline (left)",
        left_options,
        index=left_options.index(default_left) if default_left in left_options else 0,
        format_func=_fmt_left,
        key="cmp_left",
    )
with c2:
    right_opts = [s for s in names if s != left_slug] or list(names.keys())
    # Prefer something other than the imported-schedule snapshot when left is official
    preferred = next((s for s in right_opts if s != "azap_baseline"), right_opts[0])
    right_slug = st.selectbox(
        "Proposed (right)",
        right_opts,
        index=right_opts.index(preferred) if preferred in right_opts else 0,
        format_func=lambda s: names[s],
        key="cmp_right",
    )

if left_slug == OFFICIAL_KEY:
    left_cal = official
    left_sc = baseline.to_dict() if baseline else {}
    left_label = "Current schedule (official)"
    left_res = baseline
else:
    left = load_version(left_slug, dd)
    left_cal = left["calendar"]
    left_sc = left["metadata"].get("scorecard") or score_calendar(left_cal, week_label=left_slug, data_dir=dd).to_dict()
    left_label = names[left_slug]
    left_res = score_calendar(left_cal, week_label=left_label, data_dir=dd)

right = load_version(right_slug, dd)
right_cal = right["calendar"]
right_sc = right["metadata"].get("scorecard") or score_calendar(right_cal, week_label=right_slug, data_dir=dd).to_dict()
right_label = names[right_slug]
right_res = score_calendar(right_cal, week_label=right_label, data_dir=dd)

# KPI comparison table with Δ
rows = []
sections = [
    ("composite", None, "Composite", True),
    ("changeovers", "recipe_changes", "Recipe COs", False),
    ("changeovers", "format_changes", "Format COs", False),
    ("changeovers", "total_co_hours", "CO hours", False),
    ("cip", "cip_count", "CIP count", False),
    ("cip", "cip_hours", "CIP hours", False),
    ("cip", "cip_forfeited_h", "CIP forfeited h", False),
    ("cip", "cip_forfeited_kg", "CIP forfeited kg", False),
    ("trials", "trial_hours", "Trial hours", False),
    ("trials", "trial_disruptions", "Trial disruptions", False),
    ("campaigns", "avg_run_h", "Avg run h", True),
    ("campaigns", "short_run_count", "Short runs", False),
    ("service", "orders_late", "Orders late", False),
    ("service", "orders_at_risk", "Orders at risk", False),
]
for section, key, label, higher_better in sections:
    if key is None:
        lv, rv = left_sc.get("composite"), right_sc.get("composite")
    else:
        lv = (left_sc.get(section) or {}).get(key)
        rv = (right_sc.get(section) or {}).get(key)
    delta = None
    verdict = ""
    if lv is not None and rv is not None:
        try:
            delta = float(rv) - float(lv)
            if abs(delta) < 1e-9:
                verdict = "same"
            else:
                improved = (delta > 0) if higher_better else (delta < 0)
                # Composite: higher score is better
                if key is None:
                    improved = delta > 0
                verdict = "better" if improved else "worse"
        except (TypeError, ValueError):
            delta = None
    rows.append({
        "Metric": label,
        left_label: lv,
        right_label: rv,
        "Δ": None if delta is None else round(delta, 2),
        "vs baseline": verdict,
    })

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

if left_res is not None:
    render_delta_strip(left_res, right_res, title=f"Δ {right_label} vs {left_label}")

# Delta narrative
deltas = delta_narrative(left_res, right_res) if left_res is not None else []
st.subheader("Show me why")
if deltas:
    for d in deltas:
        st.write(f"- {d}")
else:
    st.caption("No material differences.")

if baseline and left_slug != OFFICIAL_KEY:
    st.subheader("vs current schedule (official)")
    for label, res in ((left_label, left_res), (right_label, right_res)):
        with st.expander(f"{label} vs official"):
            for d in delta_narrative(baseline, res):
                st.write(f"- {d}")

# Per-version management
st.divider()
st.subheader("Manage versions")
for v in versions:
    slug = v["slug"]
    with st.expander(f"{display_name(slug, v.get('name'))}  ·  composite={(v.get('scorecard') or {}).get('composite', '—')}  ·  {v.get('source', '')}"):
        st.caption(v.get("timestamp", ""))
        pros = st.text_area("Pros", value=v.get("pros", ""), key=f"pros_{slug}")
        cons = st.text_area("Cons", value=v.get("cons", ""), key=f"cons_{slug}")
        notes = st.text_area("Decision notes", value=v.get("notes", ""), key=f"notes_{slug}")
        if st.button("Save notes", key=f"save_notes_{slug}"):
            update_notes(slug, dd, pros=pros, cons=cons, notes=notes)
            st.success("Notes saved")

        sc = v.get("scorecard")
        if sc:
            render_scorecard(sc, show_formulas=False)

        new_name = st.text_input("Rename", value=display_name(slug, v.get("name")), key=f"rename_{slug}")
        a, b, c, d = st.columns(4)
        if a.button("Rename", key=f"do_rename_{slug}"):
            rename_version(slug, new_name, dd)
            st.rerun()
        if b.button("Load into calendar", key=f"load_{slug}"):
            data = load_version(slug, dd)
            save_calendar(data["calendar"], dd / "calendar_blocks.csv")
            st.success("Loaded into official calendar_blocks.csv")
        if c.button("Promote to official", key=f"promo_{slug}"):
            promote_version(slug, dd)
            st.success("Promoted")
        if d.button("Delete", key=f"del_{slug}"):
            delete_version(slug, dd)
            st.rerun()
        xbytes = export_version_excel(slug, dd)
        st.download_button("Export Excel", data=xbytes, file_name=f"{slug}.xlsx", key=f"xl_{slug}")

if st.button("Delete all versions", type="secondary"):
    delete_all_versions(dd)
    st.rerun()

# ---------------------------------------------------------------------------
# Plant write-back (mo_changes) — the charter's "changes go back to VIF" step.
# The solver records every delta against committed manprg MOs (tonnage trims,
# splits, reorders) in mo_changes.csv per scenario run. Review here, export
# for VIF.
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Plant write-back — MO changes (VIF export)")
st.caption(
    "What the selected solver run changed against the plant's committed MOs "
    "(from manprg): tonnage trims, splits, reorders. Scenario **E** is the "
    "current-state re-optimization — that is the one the plant cares about."
)

_scen_root = dd / "_scenario_work"
_mo_files = sorted(
    (p for p in _scen_root.glob("*/mo_changes.csv")),
    key=lambda p: p.stat().st_mtime, reverse=True,
) if _scen_root.exists() else []

if not _mo_files:
    st.info("No solver run has produced mo_changes.csv yet — run a scenario "
            "(Generate Scenarios), typically E (current state + demand).")
else:
    from datetime import datetime as _dt
    _labels = {
        str(p): (f"{p.parent.name} — "
                 f"{_dt.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}")
        for p in _mo_files
    }
    _sel = st.selectbox(
        "Solver run", [str(p) for p in _mo_files],
        format_func=lambda s: _labels.get(s, s), key="mo_changes_run")
    _mo = pd.read_csv(_sel, dtype={"mo": str, "sku": str})
    if _mo.empty:
        st.caption("This run changed nothing against the committed MOs.")
    else:
        _changed = _mo[_mo["reason"] != "unmoved"]
        st.markdown(
            f"**{len(_changed)} of {len(_mo)} committed MO(s) changed** — "
            f"total tonnage delta "
            f"**{_mo['delta_kg'].sum():+,.0f} kg**")
        st.dataframe(_mo, use_container_width=True, hide_index=True)
        st.download_button(
            "Download mo_changes.csv (VIF write-back)",
            data=Path(_sel).read_bytes(),
            file_name=f"mo_changes_{Path(_sel).parent.name}.csv",
            mime="text/csv",
        )
