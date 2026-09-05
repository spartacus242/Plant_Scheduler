# tests/test_version_frames.py — version time-frame reconciliation.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))


def test_promote_rebases_into_board_frame(tmp_path, monkeypatch):
    """A version solved on a newer rolling anchor than the board must land
    with its hours shifted by the frame gap — promoting verbatim rendered a
    Monday solve one week in the past (user report 2026-08-17)."""
    from helpers import version_manager as vm

    dd = tmp_path / "data"
    (dd / "versions" / "v1").mkdir(parents=True)
    cal = pd.DataFrame([{
        "block_id": "b1", "block_type": "production", "line_id": 0,
        "line_name": "P09", "start_h": 10.0, "end_h": 20.0, "label": "111",
        "order_id": "111-W1", "sku": "111", "sku_description": "",
        "qty_kg": 1000.0, "locked": False, "attrs": "",
    }])
    from helpers.calendar_io import save_calendar
    save_calendar(cal, dd / "versions" / "v1" / "calendar_blocks.csv")
    (dd / "versions" / "v1" / "metadata.json").write_text(json.dumps({
        "name": "v1", "planning_anchor": "2026-08-17 00:00:00"}),
        encoding="utf-8")
    # board frame: one week older
    monkeypatch.setattr(vm, "planning_anchor",
                        lambda *a, **k: __import__("datetime").datetime(
                            2026, 8, 10))
    shifted, shift = vm.calendar_in_board_frame(cal, "v1", dd)
    assert shift == 168.0
    assert float(shifted.iloc[0]["start_h"]) == 178.0
    assert float(shifted.iloc[0]["end_h"]) == 188.0


def test_unstamped_version_refuses_unless_told_the_frame(tmp_path):
    """UPDATED 2026-09-03 (fix quality-12, agent W). This test used to pin
    the OLD behaviour — an unstamped version silently 'assumed the board
    frame' (shift 0). That silently re-created the very bug the stamp was
    introduced for: a version whose stamp failed to write promoted a whole
    week into the past with no symptom. A missing/unreadable stamp must now
    REFUSE (ValueError) unless the caller explicitly asserts the frame with
    assume_board_frame=True, in which case the hours pass through unshifted
    (5.0 stays 5.0)."""
    import pytest

    from helpers import version_manager as vm

    dd = tmp_path / "data"
    (dd / "versions" / "v2").mkdir(parents=True)
    (dd / "versions" / "v2" / "metadata.json").write_text(
        json.dumps({"name": "v2"}), encoding="utf-8")
    cal = pd.DataFrame([{"start_h": 5.0, "end_h": 9.0}])
    assert vm.board_frame_shift_h("v2", dd) is None
    with pytest.raises(ValueError, match="planning_anchor"):
        vm.calendar_in_board_frame(cal, "v2", dd)
    shifted, shift = vm.calendar_in_board_frame(
        cal, "v2", dd, assume_board_frame=True)
    assert shift == 0.0
    assert float(shifted.iloc[0]["start_h"]) == 5.0
