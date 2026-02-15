# helpers/paths.py — shared data-directory resolver for all pages.

from __future__ import annotations
from pathlib import Path
import streamlit as st

_CODE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DATA = _CODE_DIR.parent / "data"


def data_dir() -> Path:
    """Return the current data directory as a Path."""
    return Path(st.session_state.get("data_dir", str(_DEFAULT_DATA)))
