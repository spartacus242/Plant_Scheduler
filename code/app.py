# Flowstate — Operational truth → Digital twin → Optimizer
#
# Phase 0: Schedule Scorecard
# Phase 1: Digital Twin (DnD what-if)
# Phase 2: Optimizer scenarios vs the current schedule

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

DATA_DIR = BASE_DIR.parent / "data"

st.set_page_config(
    page_title="Flowstate",
    page_icon=":material/factory:",
    layout="wide",
)

# Navigation IS the daily loop (charter §2.2): Connect → Reconcile → Plan →
# Lock & Export → Track, then weekly roll. The planner walks the sidebar top
# to bottom every morning; reference/setup pages sit below the loop.
pg = st.navigation(
    {
        "Home": [
            st.Page("pages/home.py", title="Command Center", icon=":material/home:", default=True),
        ],
        "1 · Connect": [
            st.Page("pages/data.py", title="Data Files", icon=":material/table:"),
        ],
        "2 · Reconcile": [
            st.Page("pages/reconcile.py", title="Reconcile", icon=":material/checklist:"),
            st.Page("pages/stock_check.py", title="Stock Check", icon=":material/inventory:"),
        ],
        "3 · Plan": [
            st.Page("pages/calendar.py", title="Plant Calendar", icon=":material/drag_indicator:"),
            st.Page("pages/generate.py", title="Generate Scenarios", icon=":material/auto_awesome:"),
        ],
        "4 · Lock & Export": [
            st.Page("pages/compare.py", title="Compare & Promote", icon=":material/compare:"),
        ],
        "5 · Track": [
            st.Page("pages/scorecard.py", title="Schedule Scorecard", icon=":material/analytics:"),
        ],
        "Setup": [
            st.Page("pages/lines.py", title="Lines", icon=":material/view_week:"),
            st.Page("pages/settings.py", title="Settings", icon=":material/settings:"),
        ],
    },
    expanded=True,
)

if "data_dir" not in st.session_state:
    st.session_state["data_dir"] = str(DATA_DIR)

pg.run()
