# tests/test_horizon.py — rolling "starts today" planning window (handoff WW32 #1).

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers import horizon as hz  # noqa: E402

NOW = datetime(2026, 8, 7, 14, 30)  # Friday afternoon, mid-week
CFG_FIXED = {"scheduler": {"planning_start_date": "2026-08-03 00:00:00",
                           "horizon_hours": 504}}


def test_anchor_is_midnight_today_by_default():
    h = hz.resolve(CFG_FIXED, now=NOW)
    assert h.mode == "today"
    assert h.anchor == datetime(2026, 8, 7, 0, 0)
    assert h.start == h.anchor
    assert h.hours == 504
    assert h.end == datetime(2026, 8, 7) + timedelta(hours=504)


def test_now_offset_and_shift():
    h = hz.resolve(CFG_FIXED, now=NOW)
    assert h.now_h == 14.5                       # 14:30 into the anchor day
    assert h.shift_h == 96.0                     # Mon 8/3 -> Fri 8/7
    assert h.stale is True


def test_fixed_mode_keeps_config_anchor():
    cfg = {"scheduler": dict(CFG_FIXED["scheduler"], anchor_mode="fixed")}
    h = hz.resolve(cfg, now=NOW)
    assert h.mode == "fixed"
    assert h.anchor == datetime(2026, 8, 3)
    assert h.shift_h == 0.0
    assert h.stale is False


def test_horizon_weeks_override():
    cfg = {"scheduler": {"planning_start_date": "2026-08-03", "horizon_weeks": 3,
                         "horizon_hours": 999}}
    h = hz.resolve(cfg, now=NOW)
    assert h.hours == 504
    assert len(h.week_starts()) == 3


def _cal():
    return pd.DataFrame(
        [
            # anchored to Mon 8/3: hours are pre-rebase
            {"block_id": "past", "start_h": 0.0, "end_h": 48.0},
            {"block_id": "running", "start_h": 100.0, "end_h": 120.0},
            {"block_id": "future", "start_h": 200.0, "end_h": 260.0},
            {"block_id": "way_out", "start_h": 900.0, "end_h": 950.0},
        ]
    )


def test_rebase_shifts_hours_back_by_anchor_delta():
    h = hz.resolve(CFG_FIXED, now=NOW)
    out = hz.rebase_calendar(_cal(), h.shift_h)
    assert out.loc[0, "start_h"] == -96.0        # Mon block now in the past
    assert out.loc[1, "start_h"] == 4.0          # 8/7 04:00
    assert out.loc[2, "end_h"] == 164.0


def test_clip_drops_past_and_flags_in_progress():
    h = hz.resolve(CFG_FIXED, now=NOW)
    reb = hz.rebase_calendar(_cal(), h.shift_h)
    out = hz.clip_to_horizon(reb, h)
    ids = list(out["block_id"])
    assert "past" not in ids                     # ended before today
    assert ids == ["running", "future"]          # way_out is beyond 504h
    row = out[out["block_id"] == "running"].iloc[0]
    assert bool(row["in_progress"]) is True      # 04:00 -> 24:00 spans 14:30
    assert bool(out[out["block_id"] == "future"].iloc[0]["in_progress"]) is False


def test_clip_flags_beyond_horizon_tail():
    h = hz.resolve(CFG_FIXED, now=NOW)
    df = pd.DataFrame([{"block_id": "tail", "start_h": 500.0, "end_h": 520.0}])
    out = hz.clip_to_horizon(df, h)
    assert bool(out.iloc[0]["beyond_horizon"]) is True


def test_clip_empty_and_missing_columns_are_safe():
    h = hz.resolve(CFG_FIXED, now=NOW)
    assert hz.clip_to_horizon(pd.DataFrame(), h).empty
    df = pd.DataFrame([{"x": 1}])
    assert list(hz.clip_to_horizon(df, h).columns) == ["x"]


def test_caption_mentions_iso_weeks_and_mode():
    cap = hz.caption(hz.resolve(CFG_FIXED, now=NOW))
    assert "WW32" in cap and "WW33" in cap and "WW34" in cap
    assert "starts today" in cap
