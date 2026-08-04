# Flowstate — Operational truth → Digital twin → Optimizer
#
# Phase 0: Schedule Scorecard
# Phase 1: Digital Twin (DnD what-if)
# Phase 2: Optimizer scenarios vs AZAP baseline

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

pg = st.navigation(
    {
        "Home": [
            st.Page("pages/home.py", title="Home", icon=":material/home:", default=True),
        ],
        "Data": [
            st.Page("pages/data.py", title="Data Files", icon=":material/table:"),
        ],
        "Score": [
            st.Page("pages/scorecard.py", title="Schedule Scorecard", icon=":material/analytics:"),
        ],
        "Twin": [
            st.Page("pages/calendar.py", title="Plant Calendar", icon=":material/drag_indicator:"),
        ],
        "Compare": [
            st.Page("pages/compare.py", title="Version Compare", icon=":material/compare:"),
        ],
        "Optimize": [
            st.Page("pages/generate.py", title="Generate Scenarios", icon=":material/auto_awesome:"),
        ],
        "Setup": [
            st.Page("pages/lines.py", title="Lines", icon=":material/view_week:"),
        ],
    },
    expanded=True,
)

if "data_dir" not in st.session_state:
    st.session_state["data_dir"] = str(DATA_DIR)

pg.run()
