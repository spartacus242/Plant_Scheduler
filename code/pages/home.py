# pages/home.py -- Home: the weekly workflow plus a data-status panel.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.data_catalog import CATALOG, status
from helpers.paths import data_dir

st.header("Flowstate")
st.caption("Operational truth -> digital twin -> optimizer. Start at step 1 and work down.")

STEPS = (
    (
        "1. Load / refresh data",
        "Check the input files below, then upload or edit any that are out of date.",
        "pages/data.py",
        "Open Data Files",
    ),
    (
        "2. Score the AZAP schedule",
        "Rate this week's planned schedule so you know where it hurts before you change it.",
        "pages/scorecard.py",
        "Open Schedule Scorecard",
    ),
    (
        "3. What-if in the Plant Calendar",
        "Drag blocks around in the digital twin and watch the score move.",
        "pages/calendar.py",
        "Open Plant Calendar",
    ),
    (
        "4. Generate and compare scenarios",
        "Let the optimizer build alternatives, then compare them against the baseline.",
        "pages/generate.py",
        "Open Generate Scenarios",
    ),
    (
        "5. Promote the chosen version",
        "Pick the winning version side by side and make it the plan of record.",
        "pages/compare.py",
        "Open Version Compare",
    ),
)

st.subheader("Weekly workflow")
for title, blurb, target, label in STEPS:
    with st.container(border=True):
        left, right = st.columns([3, 1])
        with left:
            st.markdown(f"**{title}**")
            st.caption(blurb)
        with right:
            st.page_link(target, label=label, icon=":material/arrow_forward:")

st.divider()

st.subheader("Data status")
dd = data_dir()
st.caption(f"Data folder: `{dd}`")

rows = []
problems = 0
for spec in CATALOG:
    info = status(spec, dd)
    if info["exists"] and not info["error"]:
        badge = "OK"
    else:
        badge = "MISSING" if not info["exists"] else "ERROR"
        problems += 1
    rows.append(
        {
            "Status": badge,
            "File": info["name"],
            "Path": info["rel"],
            "Rows": info["rows"] if info["rows"] is not None else "-",
            "Last modified": info["modified"] or "-",
            "Note": info["error"] or "",
        }
    )

if problems:
    st.warning(f"{problems} of {len(CATALOG)} input files need attention.")
else:
    st.success(f"All {len(CATALOG)} input files are present and readable.")

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
st.page_link("pages/data.py", label="Upload / edit data files", icon=":material/upload_file:")

st.divider()
with st.expander("About Flowstate"):
    st.markdown(
        """
The optimizer is maybe 20-30% of the project. The hard part is the **operational truth model**
that scores a schedule the same way every week.

| Phase | Question |
| --- | --- |
| **Schedule Scorecard** | How good is this week's (AZAP) schedule? |
| **Plant Calendar** | If I move this block, what happens to the score? |
| **Generate Scenarios** | Which solver alternatives beat the baseline -- and why? |

CIP stays critical. One planner enters production, maintenance, trials, contractor work, and line-downs.

Legacy solver-first app: `Flowstate-legacy/` (deprecated, kept for reference).
"""
    )
