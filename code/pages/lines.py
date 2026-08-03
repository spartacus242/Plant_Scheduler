# pages/lines.py — Line list editor.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.calendar_io import ensure_lines_from_calendar, load_calendar, load_lines
from helpers.paths import data_dir
from helpers.safe_io import safe_write_csv

st.header("Lines")
st.caption("Gantt rows for the plant calendar.")

dd = data_dir()
cal = load_calendar(dd / "calendar_blocks.csv")
ensure_lines_from_calendar(cal, dd / "lines.csv")
lines = load_lines(dd / "lines.csv")

edited = st.data_editor(lines, num_rows="dynamic", use_container_width=True, key="lines_editor")
if st.button("Save lines"):
    safe_write_csv(pd.DataFrame(edited), dd / "lines.csv")
    st.success("Saved lines.csv")
