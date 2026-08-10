# tests/test_current_state_overlay.py — handoff WW32 item 3.
#
# The solver's work dir must start from the REAL plant state. A previous
# version of _overlay_current_state imported load_lines from the wrong module
# and swallowed the ImportError, so the overlay was a silent no-op and the
# solver kept planning from the seeded fixture. These tests fail loudly if
# that ever regresses.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers import scenario_runner as sr  # noqa: E402

REF = ROOT / "data" / "reference"
LIVE = (REF / "manprg.txt").exists() and (REF / "cip_info.csv").exists()


def test_overlay_imports_resolve():
    """Every module the overlay reaches for must actually exist."""
    from helpers.calendar_io import load_lines  # noqa: F401
    from helpers.cip_import import read_cip_info  # noqa: F401
    from helpers.config import datasources_config, load_toml  # noqa: F401
    from helpers.current_state import build_current_state  # noqa: F401
    from helpers.horizon import resolve  # noqa: F401


def test_overlay_reports_instead_of_silently_skipping(tmp_path):
    notes = sr._overlay_current_state(tmp_path, ROOT / "data")
    assert notes, "overlay must always report what it did or why it didn't"
    assert "skipped" in notes[0]


@pytest.mark.skipif(not LIVE, reason="live manprg/cip_info exports not present")
def test_overlay_actually_gates_lines_from_the_running_mo(tmp_path):
    src = REF / "initial_states.csv"
    init = pd.read_csv(src)
    init["available_from_hour"] = 0
    init["initial_sku"] = "CLEAN"
    (tmp_path / "initial_states.csv").write_text(
        init.to_csv(index=False), encoding="utf-8")

    notes = sr._overlay_current_state(tmp_path, ROOT / "data")
    out = pd.read_csv(tmp_path / "initial_states.csv")

    assert notes and "overlaid" in notes[0], notes
    gated = out[out["available_from_hour"].astype(float) > 0]
    assert len(gated) > 0, "no line was gated — the overlay did nothing"
    assert (out["initial_sku"] != "CLEAN").any(), \
        "no running SKU carried over — changeovers would be mis-costed"
    assert len(out) == len(init), "overlay must not add or drop lines"
