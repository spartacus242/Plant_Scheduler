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
from helpers.paths import data_dir
from helpers.scorecard_engine import ScorecardResult, delta_narrative, score_calendar
from helpers.scorecard_ui import render_scorecard
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
    "Compare named options and solver scenarios against each other. "
    "Raw metrics drive 'show me why'; the composite is only a conversation starter."
)

dd = data_dir()
versions = list_versions(dd)

if not versions:
    st.info("No versions yet. Save one from the Plant Calendar or Generate Scenarios.")
    st.stop()

# Official baseline score for reference
official = load_calendar(dd / "calendar_blocks.csv")
baseline = score_calendar(official, week_label="official", data_dir=dd) if not official.empty else None

if baseline:
    st.subheader("Official calendar (current)")
    st.metric("Composite", f"{baseline.composite:.0f}" if baseline.composite is not None else "n/a")

# Side-by-side picker
names = {v["slug"]: v.get("name", v["slug"]) for v in versions}
c1, c2 = st.columns(2)
with c1:
    left_slug = st.selectbox("Left", list(names.keys()), format_func=lambda s: names[s], key="cmp_left")
with c2:
    right_opts = [s for s in names if s != left_slug] or list(names.keys())
    right_slug = st.selectbox("Right", right_opts, format_func=lambda s: names[s], key="cmp_right")

left = load_version(left_slug, dd)
right = load_version(right_slug, dd)
left_sc = left["metadata"].get("scorecard") or score_calendar(left["calendar"], week_label=left_slug, data_dir=dd).to_dict()
right_sc = right["metadata"].get("scorecard") or score_calendar(right["calendar"], week_label=right_slug, data_dir=dd).to_dict()

# KPI comparison table
rows = []
sections = [
    ("composite", None, "Composite"),
    ("changeovers", "recipe_changes", "Recipe COs"),
    ("changeovers", "format_changes", "Format COs"),
    ("changeovers", "total_co_hours", "CO hours"),
    ("cip", "cip_count", "CIP count"),
    ("cip", "cip_hours", "CIP hours"),
    ("cip", "cip_forfeited_h", "CIP forfeited h"),
    ("trials", "trial_hours", "Trial hours"),
    ("trials", "trial_disruptions", "Trial disruptions"),
    ("maintenance", "maint_aligned", "Maint aligned"),
    ("maintenance", "maint_conflicts", "Maint conflicts"),
    ("campaigns", "avg_run_h", "Avg run h"),
    ("campaigns", "short_run_count", "Short runs"),
    ("service", "orders_late", "Orders late"),
    ("service", "orders_at_risk", "Orders at risk"),
]
for section, key, label in sections:
    if key is None:
        lv, rv = left_sc.get("composite"), right_sc.get("composite")
    else:
        lv = (left_sc.get(section) or {}).get(key)
        rv = (right_sc.get(section) or {}).get(key)
    rows.append({"Metric": label, names[left_slug]: lv, names[right_slug]: rv})

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# Delta narrative
left_obj = ScorecardResult(**{k: left_sc.get(k) for k in ScorecardResult.__dataclass_fields__ if k in left_sc}) if "week_label" in left_sc else None
# Simpler: rebuild from score_calendar
left_res = score_calendar(left["calendar"], week_label=names[left_slug], data_dir=dd)
right_res = score_calendar(right["calendar"], week_label=names[right_slug], data_dir=dd)
deltas = delta_narrative(left_res, right_res)
st.subheader("Show me why")
if deltas:
    for d in deltas:
        st.write(f"- {d}")
else:
    st.caption("No material differences.")

if baseline:
    st.subheader("vs Official AZAP / current")
    for label, res in ((names[left_slug], left_res), (names[right_slug], right_res)):
        with st.expander(f"{label} vs official"):
            for d in delta_narrative(baseline, res):
                st.write(f"- {d}")

# Per-version management
st.divider()
st.subheader("Manage versions")
for v in versions:
    slug = v["slug"]
    with st.expander(f"{v.get('name', slug)}  ·  composite={(v.get('scorecard') or {}).get('composite', '—')}  ·  {v.get('source', '')}"):
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

        new_name = st.text_input("Rename", value=v.get("name", slug), key=f"rename_{slug}")
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
