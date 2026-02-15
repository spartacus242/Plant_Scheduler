# app.py — Flowstate GUI entry point.
# Launch: streamlit run code/app.py --server.address 0.0.0.0 --server.port 8501

from __future__ import annotations
import sys
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

DATA_DIR = BASE_DIR.parent / "data"

st.set_page_config(
    page_title="Flowstate Scheduler",
    page_icon=":material/factory:",
    layout="wide",
)

# ── Navigation ──────────────────────────────────────────────────────────
pg = st.navigation(
    {
        "Setup": [
            st.Page("pages/settings.py", title="Settings", icon=":material/settings:"),
            st.Page("pages/demand_plan.py", title="Demand Plan", icon=":material/list_alt:"),
            st.Page("pages/inventory_check.py", title="Inventory Check", icon=":material/inventory_2:"),
        ],
        "Configuration": [
            st.Page("pages/capabilities.py", title="Capabilities & Rates", icon=":material/precision_manufacturing:"),
            st.Page("pages/changeovers.py", title="Changeovers", icon=":material/swap_horiz:"),
            st.Page("pages/trials.py", title="Trials", icon=":material/science:"),
            st.Page("pages/downtimes.py", title="Downtimes", icon=":material/event_busy:"),
            st.Page("pages/initial_states.py", title="Initial States", icon=":material/play_arrow:"),
        ],
        "Solve": [
            st.Page("pages/run_solver.py", title="Run Solver", icon=":material/calculate:"),
            st.Page("pages/schedule_viewer.py", title="Schedule Viewer", icon=":material/view_timeline:"),
        ],
        "Adjust & Export": [
            st.Page("pages/sandbox.py", title="Sandbox", icon=":material/drag_indicator:"),
            st.Page("pages/export.py", title="Export", icon=":material/download:"),
        ],
    },
    expanded=True,
)

# ── Shared state ────────────────────────────────────────────────────────
if "data_dir" not in st.session_state:
    st.session_state["data_dir"] = str(DATA_DIR)

pg.run()
