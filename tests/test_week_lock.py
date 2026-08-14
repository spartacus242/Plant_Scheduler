# tests/test_week_lock.py — 2-week lock state + display-overlay stripping
# (P4 slice 3).

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.calendar_io import drop_display_overlays  # noqa: E402
from helpers.week_lock import (  # noqa: E402
    default_lock_through,
    locked_through_h,
    read_lock,
    write_lock,
)

ANCHOR = datetime(2026, 8, 10, 0, 0, 0)


def test_lock_round_trip(tmp_path):
    dt = datetime(2026, 8, 24, 0, 0, 0)
    write_lock(tmp_path, dt)
    assert read_lock(tmp_path) == dt


def test_clear_lock_removes_the_file(tmp_path):
    write_lock(tmp_path, datetime(2026, 8, 24))
    write_lock(tmp_path, None)
    assert read_lock(tmp_path) is None
    assert not (tmp_path / "lock_state.json").exists()


def test_missing_and_corrupt_lock_read_as_none(tmp_path):
    assert read_lock(tmp_path) is None
    (tmp_path / "lock_state.json").write_text("{not json", encoding="utf-8")
    assert read_lock(tmp_path) is None


def test_locked_through_h_offsets_from_anchor():
    assert locked_through_h(None, ANCHOR) is None
    assert locked_through_h(datetime(2026, 8, 24), ANCHOR) == 336.0
    # a lock behind the anchor (stale after a roll) clamps to 0 — locks nothing
    assert locked_through_h(datetime(2026, 8, 1), ANCHOR) == 0.0


def test_default_lock_is_two_whole_weeks():
    assert default_lock_through(ANCHOR) == datetime(2026, 8, 24, 0, 0, 0)


def test_drop_display_overlays_strips_only_cipinfo_rows():
    df = pd.DataFrame([
        {"block_id": "prod_1", "block_type": "production"},
        {"block_id": "cipinfo_P09", "block_type": "cip"},
        {"block_id": "cip_real", "block_type": "cip"},
        {"block_id": "cipinfo_P20", "block_type": "cip"},
    ])
    out = drop_display_overlays(df)
    assert list(out["block_id"]) == ["prod_1", "cip_real"]


def test_drop_display_overlays_is_safe_on_empty():
    empty = pd.DataFrame()
    assert drop_display_overlays(empty) is empty
