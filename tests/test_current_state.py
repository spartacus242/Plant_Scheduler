# tests/test_current_state.py — ground-truth calendar from manprg + cip_info.

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.cip_import import CipInfo, CipInfoResult  # noqa: E402
from helpers.current_state import (  # noqa: E402
    QUEUED,
    RUNNING,
    build_current_state,
    classify_rows,
    cip_interval_for,
    line_id_for,
    project_cips,
)
from helpers.horizon import Horizon  # noqa: E402
from helpers.manprg_import import ManprgResult, read_manprg  # noqa: E402

NOW = datetime(2026, 8, 10, 12, 0)
ANCHOR = datetime(2026, 8, 10, 0, 0)


def _hz(hours: int = 504) -> Horizon:
    return Horizon(anchor=ANCHOR, start=ANCHOR, end=ANCHOR + timedelta(hours=hours),
                   now=NOW, mode="today", hours=hours, config_anchor=ANCHOR)


def _frame(rows: list[dict]) -> pd.DataFrame:
    base = {"line": "P09", "mo": "1", "item": "280581", "designation": "D",
            "hours": 10.0, "fct_cas": 100.0, "made_cas": None, "left_cas": 100.0,
            "fct_kg": 5000.0, "start_dt": pd.Timestamp(NOW)}
    return pd.DataFrame([{**base, **r} for r in rows])


# --------------------------------------------------------------- classify

def test_classify_blank_made_is_queued_not_zero_production():
    rows = classify_rows(_frame([{"mo": "A", "made_cas": ""}]))
    assert rows[0]["kind"] == QUEUED
    assert rows[0]["started"] is False


def test_classify_partial_made_is_running():
    rows = classify_rows(_frame([{"mo": "A", "made_cas": 40.0, "left_cas": 60.0}]))
    assert rows[0]["kind"] == RUNNING
    assert rows[0]["completion_pct"] == 40.0


def test_classify_full_made_is_completed():
    rows = classify_rows(_frame([{"mo": "A", "made_cas": 100.0, "left_cas": 0.0}]))
    assert rows[0]["kind"] == "completed"


def test_classify_cip_pseudo_mo_is_never_production():
    rows = classify_rows(_frame([{"mo": "A", "item": "CIP", "fct_cas": 1042.0}]))
    assert rows[0]["kind"] == "cip"


def test_classify_strips_float_suffix_from_codes():
    rows = classify_rows(_frame([{"mo": "29901.0", "item": "280351.0"}]))
    assert rows[0]["mo"] == "29901" and rows[0]["item"] == "280351"


# --------------------------------------------------------------- placement

def _state(rows, cips=None, cfg=None, hz=None):
    mp = ManprgResult(frame=_frame(rows))
    return build_current_state(hz or _hz(), manprg=mp,
                               cips=cips or CipInfoResult(), cfg=cfg, now=NOW)


def test_completed_mos_are_dropped_from_the_calendar():
    st = _state([{"mo": "DONE", "made_cas": 100.0, "left_cas": 0.0},
                 {"mo": "NEXT", "made_cas": ""}])
    assert "DONE" not in set(st.blocks["order_id"])
    assert "NEXT" in set(st.blocks["order_id"])
    assert len(st.completed) == 1


def test_running_mo_is_locked_and_never_ends_in_the_past():
    st = _state([{"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=40)}])
    blk = st.blocks[st.blocks["order_id"] == "RUN"].iloc[0]
    assert bool(blk["locked"]) is True
    assert blk["end_h"] > (NOW - ANCHOR).total_seconds() / 3600.0


def test_running_mo_started_before_the_anchor_is_clamped_into_the_window():
    st = _state([{"mo": "OLD", "made_cas": 50.0, "left_cas": 50.0, "hours": 200.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=190)}])
    blk = st.blocks[st.blocks["order_id"] == "OLD"].iloc[0]
    assert blk["start_h"] >= 0.0, "block must render inside the horizon"
    assert "clamped_to_anchor" in blk["attrs"]
    assert "started=" in blk["attrs"], "true start must be preserved"


def test_queued_mos_are_sequential_and_do_not_overlap():
    st = _state([
        {"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0, "hours": 10.0},
        {"mo": "Q1", "made_cas": "", "hours": 8.0,
         "start_dt": pd.Timestamp(NOW) + timedelta(hours=1)},
        {"mo": "Q2", "made_cas": "", "hours": 6.0,
         "start_dt": pd.Timestamp(NOW) + timedelta(hours=2)},
    ])
    prod = st.blocks[st.blocks["block_type"] == "production"].sort_values("start_h")
    starts = list(prod["start_h"])
    ends = list(prod["end_h"])
    for i in range(1, len(prod)):
        assert starts[i] >= ends[i - 1] - 1e-6, "queued MO overlaps its predecessor"
    assert bool(prod[prod["order_id"] == "Q1"].iloc[0]["locked"]) is False


def test_queued_mo_never_starts_before_now():
    st = _state([{"mo": "STALE", "made_cas": "",
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=30)}])
    blk = st.blocks.iloc[0]
    assert blk["start_h"] >= (NOW - ANCHOR).total_seconds() / 3600.0 - 1e-6


def test_line_free_hour_is_after_the_last_placed_block():
    st = _state([{"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0, "hours": 10.0}])
    free = st.line_free_h["P09"]
    prod = st.blocks[st.blocks["block_type"] == "production"]
    assert free >= prod["end_h"].max() - 1e-6


def test_two_started_mos_on_a_line_keep_only_the_latest_as_running():
    st = _state([
        {"mo": "OLD", "made_cas": 10.0, "left_cas": 90.0,
         "start_dt": pd.Timestamp(NOW) - timedelta(hours=50)},
        {"mo": "NEW", "made_cas": 20.0, "left_cas": 80.0,
         "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)},
    ])
    assert [r["mo"] for r in st.running] == ["NEW"]
    assert any("superseded" in w for w in st.warnings)


# --------------------------------------------------------------- CIP

def test_scheduled_cip_is_drawn_and_locked():
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=None, max_hours_between=120,
        scheduled_cip=pd.Timestamp(NOW) + timedelta(hours=5), notes="WO#1")})
    st = _state([], cips=cips)
    cip = st.blocks[st.blocks["block_type"] == "cip"]
    sched = cip[cip["label"] == "CIP"]
    assert len(sched) == 1 and bool(sched.iloc[0]["locked"]) is True


def test_future_cips_are_projected_at_the_per_line_interval():
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=pd.Timestamp(NOW), max_hours_between=120,
        scheduled_cip=None, notes="")})
    st = _state([], cips=cips)
    starts = sorted(st.blocks[st.blocks["block_type"] == "cip"]["start_h"])
    assert len(starts) >= 3, "weeks 2-3 must not be CIP-free"
    gaps = [round(b - a) for a, b in zip(starts, starts[1:])]
    assert set(gaps) == {120}


def test_per_line_max_hours_overrides_global_interval():
    cips = CipInfoResult(by_line={"P10": CipInfo(
        line="P10", previous_cip=None, max_hours_between=144,
        scheduled_cip=None, notes="")})
    assert cip_interval_for("P10", cips, {"cip": {"interval_h": 120}}) == 144
    assert cip_interval_for("P99", cips, {"cip": {"interval_h": 120}}) == 120


def test_projected_cips_stay_inside_the_horizon():
    info = CipInfo(line="P09", previous_cip=NOW, max_hours_between=120,
                   scheduled_cip=None, notes="")
    hz = _hz()
    for when, _kind in project_cips("P09", info, hz, 6.0, 120.0):
        assert when < hz.end


# --------------------------------------------------------------- plumbing

def test_line_id_resolves_from_lines_csv_then_falls_back():
    lines = pd.DataFrame({"line_id": [0, 5], "line_name": ["P09", "P14"]})
    assert line_id_for("P14", lines) == 5
    assert line_id_for("P11", None) == 2


def test_blocks_match_the_calendar_schema():
    from helpers.calendar_io import CALENDAR_COLUMNS
    st = _state([{"mo": "A", "made_cas": ""}])
    assert list(st.blocks.columns) == CALENDAR_COLUMNS


@pytest.mark.skipif(not (ROOT / "data" / "reference" / "manprg.txt").exists(),
                    reason="real manprg export not present")
def test_real_manprg_and_cip_files_build_a_state():
    ref = ROOT / "data" / "reference"
    mp = read_manprg([ref / "manprg.txt", ref / "manprg2.txt"])
    assert mp.frame is not None and len(mp.frame) > 0
    st = build_current_state(_hz(), manprg=mp, cips=None,
                             cip_path=ref / "cip_info.csv", now=NOW)
    assert len(st.blocks) > 0
    # no production block overlaps another on the same line
    prod = st.blocks[st.blocks["block_type"] == "production"]
    for line, grp in prod.groupby("line_name"):
        g = grp.sort_values("start_h")
        for a, b in zip(g.itertuples(), list(g.itertuples())[1:]):
            assert b.start_h >= a.end_h - 1e-6, f"{line}: overlap {a.order_id}/{b.order_id}"
