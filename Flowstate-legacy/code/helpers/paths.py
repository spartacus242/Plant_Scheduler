# helpers/paths.py — shared data-directory resolver for all pages.

from __future__ import annotations
import json
from pathlib import Path
from typing import Optional
import streamlit as st

_CODE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DATA = _CODE_DIR.parent / "data"


def data_dir() -> Path:
    """Return the current data directory as a Path."""
    return Path(st.session_state.get("data_dir", str(_DEFAULT_DATA)))


def load_schedule_meta(dd: Optional[Path] = None) -> dict:
    """Read schedule_meta.json.  Returns ``{"solver_ran_at": str|None, "edited": bool}``."""
    dd = dd or data_dir()
    meta_path = dd / "schedule_meta.json"
    default = {"solver_ran_at": None, "edited": False}
    if not meta_path.exists():
        return default
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {
            "solver_ran_at": meta.get("solver_ran_at"),
            "edited": bool(meta.get("edited", False)),
        }
    except (OSError, ValueError):
        return default


def schedule_provenance_label(dd: Optional[Path] = None) -> str:
    """One-line provenance string for display and export headers."""
    meta = load_schedule_meta(dd)
    parts = []
    ran = meta.get("solver_ran_at")
    if ran:
        parts.append(f"Solver ran: {ran}")
    if meta.get("edited"):
        parts.append("Manually edited")
    return "  |  ".join(parts) if parts else ""
