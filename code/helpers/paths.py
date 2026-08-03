# helpers/paths.py — data-directory resolver for Flowstate.

from __future__ import annotations

from pathlib import Path
from typing import Optional

import streamlit as st

_CODE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DATA = _CODE_DIR.parent / "data"
_REPO_ROOT = _CODE_DIR.parent


def data_dir() -> Path:
    return Path(st.session_state.get("data_dir", str(_DEFAULT_DATA)))


def repo_root() -> Path:
    return _REPO_ROOT


def legacy_dir() -> Path:
    return _REPO_ROOT / "Flowstate-legacy"


def reference_dir(dd: Optional[Path] = None) -> Path:
    return (dd or data_dir()) / "reference"


def scorecards_dir(dd: Optional[Path] = None) -> Path:
    d = (dd or data_dir()) / "scorecards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def versions_dir(dd: Optional[Path] = None) -> Path:
    d = (dd or data_dir()) / "versions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def toml_path() -> Path:
    return _REPO_ROOT / "flowstate.toml"
