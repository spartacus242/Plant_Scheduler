# tests/test_fix_CB.py — regression tests for fix group CB (staging, netting,
# plan_fill), forensic audit 2026-09-02/03. Every expected value below is
# derived BY HAND in the comment next to it, never copied from the code.
#
# Findings covered: C41 (P0), C90, C32, C11, C20, C54, C55, C29 (staging
# side), C64, C21, C56, C58, C82, netting-7, staging-4/9/10/12, quality-5,
# quality-7, adversarial-11.

from __future__ import annotations

import json
import math
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code" / "solver"))

from helpers import scenario_runner as sr  # noqa: E402
from helpers.calendar_io import CALENDAR_COLUMNS  # noqa: E402
from helpers.demand_coverage import apply_ledger, build_ledger  # noqa: E402
from helpers.plan_fill import (  # noqa: E402
    cip_grid_gaps,
    coalesce_windows,
    committed_windows,
    filter_known_lines,
    gate_warnings,
    materialize_required_cips,
    planner_cip_blocks,
    rebase_board,
    rebase_demand,
    subtract_committed,
)


def _blk(bid, line, s, e, sku, btype="production", attrs="", line_id=1,
         order_id=None, qty=100.0):
    return {"block_id": bid, "block_type": btype, "line_id": line_id,
            "line_name": line, "start_h": float(s), "end_h": float(e),
            "label": sku, "order_id": bid.upper() if order_id is None else order_id,
            "sku": sku, "sku_description": "", "qty_kg": qty, "locked": False,
            "attrs": attrs}


def _cal(rows):
    return pd.DataFrame(rows, columns=CALENDAR_COLUMNS)


def _dem(rows):
    """(order_id, sku, week_index, target) in a Monday-anchored 168h grid."""
    return pd.DataFrame([{
        "order_id": o, "sku": s, "week_index": w, "qty_target": t,
        "lower_pct": 0.9, "upper_pct": 1.1,
        "due_start_hour": 168 * w, "due_end_hour": 168 * w + 167, "priority": 3,
    } for o, s, w, t in rows])


MON36 = datetime(2026, 8, 31)   # ISO 2026-W36 Monday
MON37 = datetime(2026, 9, 7)    # ISO 2026-W37 Monday


# ═══════════════════════════════════════════════════════════════════════════
# C41 (P0) — solver-drawn cleans are NOT planner CIPs; the grid must survive
# ═══════════════════════════════════════════════════════════════════════════

def test_planner_cip_blocks_requires_positive_planner_token():
    rows = [
        _blk("cip_3cff597fb3", "P12", 236, 242, "CIP", btype="cip",
             attrs="solver:cip_req"),                       # pipeline-drawn
        _blk("cip_a1b2c3d4e5", "P12", 300, 306, "CIP", btype="cip",
             attrs=""),                                     # cip_windows.csv import
        _blk("cs_955d884a2e", "P12", 360, 366, "CIP", btype="cip",
             attrs="current_state:cip_projected"),          # plant grid
        _blk("cipinfo_P12", "P12", 480, 486, "CIP", btype="cip", attrs=""),
        _blk("blk_1", "P10", 100, 106, "CIP", btype="cip", attrs="planner:cip"),
        _blk("blk_2", "P10", 220, 226, "CIP", btype="cip",
             attrs="planner:cip_projected;foo"),
        _blk("prod_9", "P10", 0, 10, "S", attrs="planner:cip"),  # not a cip
    ]
    got = planner_cip_blocks(_cal(rows))
    # only the two blocks a HUMAN placed (planner: token) survive
    assert set(got["block_id"]) == {"blk_1", "blk_2"}


def test_cip_grid_gaps_flags_missing_and_stretched_grids():
    H = 504.0
    intact = _cal([
        _blk("p", "P12", 21, 234, "S"),
        *[_blk(f"c{k}", "P12", k * 120, k * 120 + 6, "CIP", btype="cip")
          for k in (1, 2, 3, 4)],
    ])
    # gaps: 0->120 =120, 120->240 =120, 240->360, 360->480 =120, 480->504 =24
    assert cip_grid_gaps(intact, {"P12": 120}, H) == []
    # grid deleted (the live C41 outcome): production, zero cleans
    missing = _cal([_blk("p", "P12", 21, 234, "S")])
    notes = cip_grid_gaps(missing, {"P12": 120}, H)
    assert len(notes) == 1 and "CIP GRID MISSING on P12" in notes[0]
    # cleans at 120 and 360 only: 240h between them > 120 + 1 tolerance
    stretched = _cal([
        _blk("p", "P12", 21, 234, "S"),
        _blk("c1", "P12", 120, 126, "CIP", btype="cip"),
        _blk("c3", "P12", 360, 366, "CIP", btype="cip"),
    ])
    notes = cip_grid_gaps(stretched, {"P12": 120}, H)
    assert len(notes) == 1 and "240h between cleans" in notes[0]
    # a line without committed production is not checked (nothing to protect)
    idle = _cal([_blk("c1", "P10", 120, 126, "CIP", btype="cip")])
    assert cip_grid_gaps(idle, {}, H) == []


# ═══════════════════════════════════════════════════════════════════════════
# C90 + C32 — materialize_required_cips: committed predecessors, no overlap
# ═══════════════════════════════════════════════════════════════════════════

FLAG = {("P", "X"): {"cip_req_after": 1}}


def test_materialize_sees_committed_predecessor_across_line_id_dtypes():
    """Live P15: committed 280611 (line_id '6' str from load_calendar) ->
    fill 280104 (line_id 6 int from import_solver_schedule), 6.835h gap,
    cip_req_after=1, NO clean drawn. Grouping by line NAME fixes it: the
    clean lands flush at the committed run's end, 199.165 -> 205.165."""
    cal = _cal([
        _blk("m2", "P15", 168.295, 199.165, "P", attrs="current_state:queued",
             line_id="6"),
        _blk("f1", "P15", 206.0, 240.0, "X", line_id=6),
    ])
    out, notes = materialize_required_cips(cal, FLAG, 6.0)
    cips = out[out["block_type"] == "cip"]
    assert len(cips) == 1
    assert (float(cips.iloc[0]["start_h"]), float(cips.iloc[0]["end_h"])) \
        == (199.165, 205.165)
    assert cips.iloc[0]["attrs"] == "solver:cip_req"
    assert any("drawn" in n for n in notes)


def test_materialize_never_draws_over_a_trial_and_reports():
    """Gap 10-30h holds a trial 10-16h and a line-down 16-30h: neither slot
    (flush at 10 or flush before 30) is free -> nothing drawn, reported."""
    cal = _cal([
        _blk("a", "P10", 0, 10, "P"),
        _blk("t", "P10", 10, 16, "TRIALS", btype="trial"),
        _blk("d", "P10", 16, 30, "Down", btype="line_down"),
        _blk("b", "P10", 30, 40, "X"),
    ])
    out, notes = materialize_required_cips(cal, FLAG, 6.0)
    assert (out["block_type"] == "cip").sum() == 0
    assert any("holds trial" in n and "left for the planner" in n for n in notes)


def test_materialize_falls_back_to_the_slot_before_the_next_run():
    """Trial 10-12h sits at the earlier run's end; the gap is 20h, so the
    clean goes flush before the next run: 30-6 = 24 -> [24, 30]."""
    cal = _cal([
        _blk("a", "P10", 0, 10, "P"),
        _blk("t", "P10", 10, 12, "TRIALS", btype="trial"),
        _blk("b", "P10", 30, 40, "X"),
    ])
    out, _ = materialize_required_cips(cal, FLAG, 6.0)
    c = out[out["block_type"] == "cip"].iloc[0]
    assert (float(c["start_h"]), float(c["end_h"])) == (24.0, 30.0)


def test_materialize_does_not_duplicate_a_touching_cip():
    """Existing clean 235.9-241.9 next to a run ending 236.0 and one starting
    242.0 is not strictly CONTAINED in the gap (old test) but separates the
    runs within the 0.5h tolerance -> no second, overlapping clean."""
    cal = _cal([
        _blk("a", "P12", 200, 236.0, "P"),
        _blk("c", "P12", 235.9, 241.9, "CIP", btype="cip"),
        _blk("b", "P12", 242.0, 260, "X"),
    ])
    out, _ = materialize_required_cips(cal, FLAG, 6.0)
    assert (out["block_type"] == "cip").sum() == 1


# ═══════════════════════════════════════════════════════════════════════════
# C11 — blocked time rounds OUTWARD; C29 — committed CIP rows stay pure
# ═══════════════════════════════════════════════════════════════════════════

def test_coalesce_rounds_blocked_time_outward():
    rows = [{"line_id": 2, "line_name": "P11", "start_hour": 10.5,
             "end_hour": 20.5, "reason": "Down"}]
    out = coalesce_windows(rows)
    # floor(10.5) = 10, ceil(20.5) = 21 — never int()/round() into the outage
    assert (out[0]["start_hour"], out[0]["end_hour"]) == (10, 21)
    # the live row: 0.0..47.983 must block hour 47 too
    out = coalesce_windows([{"line_id": 2, "line_name": "P11", "start_hour": 0.0,
                             "end_hour": 47.983, "reason": "Down"}])
    assert (out[0]["start_hour"], out[0]["end_hour"]) == (0, 48)


def test_committed_cip_rows_stay_separate_and_cut_production_windows():
    """P09 live shape: production 0-73, CIP 73-79, production 79-142 used to
    merge into ONE row 'Committed PRODUCTION ... + Committed CIP' (0-142).
    With keep_cips_separate the clean is its own pure row and the production
    union is cut around it: [0,73], [73,79] CIP, [79,142]. Two overlapping
    solver cleans (236-242, 240-246) union into one [236, 246]."""
    rows = [
        {"line_id": 0, "line_name": "P09", "start_hour": 0, "end_hour": 73,
         "reason": "Committed PRODUCTION 30142"},
        {"line_id": 0, "line_name": "P09", "start_hour": 73, "end_hour": 79,
         "reason": "Committed CIP"},
        {"line_id": 0, "line_name": "P09", "start_hour": 79, "end_hour": 142,
         "reason": "Committed PRODUCTION 30145"},
        {"line_id": 3, "line_name": "P12", "start_hour": 230, "end_hour": 250,
         "reason": "Committed PRODUCTION 30255"},
        {"line_id": 3, "line_name": "P12", "start_hour": 236, "end_hour": 242,
         "reason": "Committed CIP"},
        {"line_id": 3, "line_name": "P12", "start_hour": 240, "end_hour": 246,
         "reason": "Committed CIP"},
    ]
    out = coalesce_windows(rows, keep_cips_separate=True)
    p09 = [(r["start_hour"], r["end_hour"], r["reason"]) for r in out
           if r["line_name"] == "P09"]
    assert p09 == [(0, 73, "Committed PRODUCTION 30142"),
                   (73, 79, "Committed CIP"),
                   (79, 142, "Committed PRODUCTION 30145")]
    p12 = [(r["start_hour"], r["end_hour"], r["reason"]) for r in out
           if r["line_name"] == "P12"]
    assert p12 == [(230, 236, "Committed PRODUCTION 30255"),
                   (236, 246, "Committed CIP"),
                   (246, 250, "Committed PRODUCTION 30255")]
    # legacy default still merges touching rows (other callers unchanged)
    legacy = coalesce_windows(rows[:3])
    assert [(r["start_hour"], r["end_hour"]) for r in legacy] == [(0, 142)]


def test_committed_windows_labels_cips_purely():
    w = committed_windows(_cal([
        _blk("c", "P09", 73.0, 79.0, "CIP", btype="cip", order_id="", qty=None),
        _blk("m", "P09", 0.0, 73.0, "111", order_id="30142"),
    ]), 504)
    reasons = {r["reason"] for r in w}
    assert "Committed CIP" in reasons
    assert "Committed PRODUCTION 30142" in reasons


# ═══════════════════════════════════════════════════════════════════════════
# C20 — a due week only partly inside the horizon is pro-rated
# ═══════════════════════════════════════════════════════════════════════════

def test_rebase_demand_prorates_the_horizon_clipped_week():
    """Wednesday anchor (shift +48h): W3 = raw [504, 671] -> [456, 623],
    clipped to [456, 503] = 504-456 = 48 covered hours of 168.
    1,715,000 x 48/168 = 490,000 kept; 1,225,000 deferred."""
    dem = _dem([("A-W0", "A", 0, 1000), ("A-W3", "A", 3, 1_715_000)])
    out, notes = rebase_demand(dem, 48.0, 504.0)
    w3 = out[out["order_id"] == "A-W3"].iloc[0]
    assert (w3["due_start_hour"], w3["due_end_hour"]) == (456.0, 503.0)
    assert w3["qty_target"] == 490_000.0
    assert any("1,225,000 kg DEFERRED" in n for n in notes)
    # a week fully inside keeps its target and pct bounds
    w0 = out[out["order_id"] == "A-W0"].iloc[0]
    assert w0["qty_target"] == 1000.0 and w0["lower_pct"] == 0.9
    # explicit bounds (when present) scale with the same fraction
    dem2 = dem.assign(qty_min=[900.0, 1_543_500.0], qty_max=[1100.0, 1_886_500.0])
    out2, _ = rebase_demand(dem2, 48.0, 504.0)
    w3b = out2[out2["order_id"] == "A-W3"].iloc[0]
    assert w3b["qty_min"] == 441_000.0 and w3b["qty_max"] == 539_000.0  # x 48/168


# ═══════════════════════════════════════════════════════════════════════════
# C55 — stale board re-based before staging
# ═══════════════════════════════════════════════════════════════════════════

def test_rebase_board_shifts_hours_and_drops_past_blocks():
    """Board stamped yesterday (config anchor 09-01), staging today (09-02):
    shift 24h. Pinned 100-120 -> 76-96; a block ending at hour 20 -> -4: gone."""
    from helpers import horizon as hz
    cfg = {"scheduler": {"planning_start_date": "2026-09-01 00:00:00",
                         "anchor_mode": "today", "horizon_weeks": 3}}
    h = hz.resolve(cfg, now=datetime(2026, 9, 2, 3, 0))
    assert h.shift_h == 24.0 and h.stale
    cal = _cal([_blk("pin", "P09", 100, 120, "S", attrs="pinned"),
                _blk("old", "P09", 0, 20, "S", attrs="pinned")])
    out, dropped = rebase_board(cal, h.shift_h)
    assert dropped == 1
    assert list(out["block_id"]) == ["pin"]
    assert (float(out.iloc[0]["start_h"]), float(out.iloc[0]["end_h"])) == (76.0, 96.0)
    # a fresh board is returned untouched
    same, d0 = rebase_board(cal, 0.0)
    assert d0 == 0 and same is cal


# ═══════════════════════════════════════════════════════════════════════════
# C54 — past production without a demand week is not carried forward
# ═══════════════════════════════════════════════════════════════════════════

def test_completed_mo_from_undemanded_past_week_is_held_back():
    """e3 T4: file anchored W37 (Mon 09-07), 25 t made Thu 09-03 (W36), no
    W36 demand row -> W37 net must stay 30,000 (was 5,000), with a warning.
    The legacy carry is opt-in."""
    dem = _dem([("S-W37", "S", 0, 30000)])
    completed = [{"item": "S", "mo": "M9", "start_dt": pd.Timestamp(2026, 9, 3, 8),
                  "hours": 20.0, "qty_kg": 25000.0, "made_kg": 25000.0}]
    led = build_ledger(dem, _cal([]), completed=completed, anchor=MON37)
    assert led.rows[0].net_kg == 30000.0 and led.rows[0].applied_kg == 0.0
    assert led.unsettled_past == {("S", 202636): 25000.0}
    assert any(n.startswith("WARNING") and "25,000 kg" in n for n in led.notes)
    legacy = build_ledger(dem, _cal([]), completed=completed, anchor=MON37,
                          carry_unsettled_past=True)
    assert legacy.rows[0].net_kg == 5000.0
    # with a history row for W36 the kg settles there first (unchanged path)
    hist = {("S", 202636): 20000.0}
    led2 = build_ledger(dem, _cal([]), completed=completed, anchor=MON37,
                        history_demand=hist)
    assert led2.rows[0].applied_kg == 5000.0 and not led2.unsettled_past


# ═══════════════════════════════════════════════════════════════════════════
# C56 + netting-7 — explicit bounds = planner band minus credit; sub-kg zeroed
# ═══════════════════════════════════════════════════════════════════════════

def test_explicit_bounds_are_gross_band_minus_credit_T9():
    """G = 100,000, C = 89,000 committed in the same ISO week:
    target 11,000; qty_min = max(0, 0.9G - C) = 90,000 - 89,000 = 1,000;
    qty_max = 1.1G - C = 110,000 - 89,000 = 21,000; pct blanked so
    data_loader takes the explicit pair; credit_kg 89,000."""
    dem = _dem([("S-W0", "S", 0, 100000.0)])
    blocks = _cal([_blk("m", "P09", 10, 30, "S", attrs="current_state:queued",
                        order_id="MO1", qty=89000.0)])
    out, notes = subtract_committed(dem, blocks, anchor=MON36, explicit_bounds=True)
    r = out.iloc[0]
    assert r["qty_target"] == 11000.0
    assert (r["qty_min"], r["qty_max"]) == (1000.0, 21000.0)
    assert pd.isna(r["lower_pct"]) and pd.isna(r["upper_pct"])
    assert r["credit_kg"] == 89000.0
    # legacy default: pct untouched (scorecard residual path relies on it)
    legacy, _ = subtract_committed(dem, blocks, anchor=MON36)
    assert legacy.iloc[0]["lower_pct"] == 0.9 and "qty_min" not in legacy.columns


def test_subkg_residual_is_zeroed_T10():
    """8,999 - 8,998.6 = 0.4 kg <= 0.5 -> target 0.0 (not a qmin 0/qmax 1
    phantom order); qty_min forced to 0."""
    dem = _dem([("S-W0", "S", 0, 8999.0)])
    blocks = _cal([_blk("m", "P09", 10, 30, "S", attrs="current_state:queued",
                        qty=8998.6)])
    out, _ = subtract_committed(dem, blocks, anchor=MON36, explicit_bounds=True)
    assert out.iloc[0]["qty_target"] == 0.0
    assert out.iloc[0]["qty_min"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# C58 — DNS cap applied on the GROSS minus credit for netted rows
# ═══════════════════════════════════════════════════════════════════════════

def test_dns_trim_caps_netted_row_on_gross_minus_credit():
    """Live 120437-W0: gross 23,198, committed 16,271.3, achievable 0.75.
    Components support 0.75 x 23,198 = 17,398.5 kg in total; 16,271.3 is
    already committed, so the fill may add at most 1,127.2 kg (was 5,196 =
    0.75 x net)."""
    from helpers.agent_policy import trim_dns_demand
    dem = pd.DataFrame([{
        "order_id": "120437-W0", "sku": "120437", "week_index": 0,
        "qty_target": 6926.7, "lower_pct": float("nan"), "upper_pct": float("nan"),
        "qty_min": 4606.9, "qty_max": 9246.5, "credit_kg": 16271.3,
        "due_start_hour": 0, "due_end_hour": 119, "priority": 3,
    }, {
        "order_id": "280480-W0", "sku": "280480", "week_index": 0,
        "qty_target": 25800.0, "lower_pct": 0.9, "upper_pct": 1.1,
        "qty_min": float("nan"), "qty_max": float("nan"), "credit_kg": 0.0,
        "due_start_hour": 0, "due_end_hour": 119, "priority": 3,
    }])
    out, notes = trim_dns_demand(dem, {"120437": 0.75, "280480": 0.5})
    netted = out.iloc[0]
    assert netted["qty_min"] == 0.0
    assert netted["qty_max"] == 1127.2
    assert pd.isna(netted["lower_pct"])          # stays explicit
    # pct row keeps the legacy rule
    pct = out.iloc[1]
    assert (pct["lower_pct"], pct["upper_pct"]) == (0.0, 0.5)
    assert len(notes) == 2 and "already committed" in notes[0]


# ═══════════════════════════════════════════════════════════════════════════
# greedy seed reads explicit bounds (needed once C56 blanks the pct columns)
# ═══════════════════════════════════════════════════════════════════════════

def test_greedy_uses_explicit_bounds_when_pct_blank():
    """target 1000 @ 100 kg/h in a 50h segment: need 10h, qmax 1200 -> 12h
    cap -> run 10h = 1000 kg. With pct NaN the old code computed nan bounds."""
    from helpers.greedy_fill import build_greedy_fill
    rows, summary = build_greedy_fill(
        [{"order_id": "S-W0", "sku": "S", "week_index": 0, "qty_target": 1000.0,
          "lower_pct": float("nan"), "upper_pct": float("nan"),
          "qty_min": 500.0, "qty_max": 1200.0,
          "due_start_hour": 0, "due_end_hour": 167}],
        {("P09", "S"): 100.0}, {}, {"P09": [(0.0, 50.0)]}, {"P09": 0}, {"P09": ""},
        min_run_hours=4, horizon_h=504.0)
    assert len(rows) == 1 and rows[0]["qty_kg"] == 1000.0
    assert summary["orders_short"] == []


# ═══════════════════════════════════════════════════════════════════════════
# staging-10 / adversarial-11 helpers
# ═══════════════════════════════════════════════════════════════════════════

def test_gate_warnings_name_the_blocked_line_and_its_block():
    blocks = _cal([_blk("m", "P21", 0, 528.1, "280324", order_id="30176"),
                   _blk("n", "P10", 0, 300, "280351", order_id="30235")])
    notes = gate_warnings({"P21": 504.0, "P10": 300.0, "P09": 100.0}, blocks, 504.0)
    assert len(notes) == 2
    assert "LINE P21 FULLY BLOCKED" in notes[1] and "30176" in notes[1]
    assert notes[0].startswith("line P10") and "60%" in notes[0]   # 300/504


def test_filter_known_lines_drops_and_reports_unknown():
    blocks = _cal([_blk("a", "P09", 0, 10, "S"), _blk("b", "P99", 0, 10, "S"),
                   _blk("c", "P23", 5, 9, "S")])
    kept, dropped = filter_known_lines(blocks, {"P09", "P10"})
    assert list(kept["line_name"]) == ["P09"] and dropped == ["P23", "P99"]
    same, none = filter_known_lines(blocks, set())      # no lines.csv: no filter
    assert len(same) == 3 and none == []


# ═══════════════════════════════════════════════════════════════════════════
# C21 / quality-5 — time-frame stamp lands in [scheduler]; config writes raise
# ═══════════════════════════════════════════════════════════════════════════

_TOML = textwrap.dedent("""\
    [scheduler]
    planning_start_date = "2026-08-25 00:00:00"
    anchor_mode = "today"
    horizon_weeks = 3
    horizon_hours = 504
    time_limit = 60

    [cip]
    interval_h = 120
    duration_h = 6
    """)


def test_stage_time_frame_writes_scheduler_anchor_the_solver_reads(tmp_path):
    from helpers import horizon as hz
    from helpers.timefmt import planning_anchor
    import phase2_scheduler

    work = tmp_path / "work"
    work.mkdir()
    (work / "flowstate.toml").write_text(_TOML, encoding="utf-8")
    cfg = {"scheduler": {"planning_start_date": "2026-08-25 00:00:00",
                         "anchor_mode": "today", "horizon_weeks": 3}}
    h = hz.resolve(cfg, now=datetime(2026, 9, 2, 10, 0))   # anchor 09-02 00:00
    notes = sr._stage_time_frame(work, tmp_path / "nodata", h)
    assert any("[scheduler].planning_start_date = 2026-09-02" in n for n in notes)
    loaded = phase2_scheduler._load_config(work / "flowstate.toml", work)
    P = phase2_scheduler.params_from_config(loaded)
    assert P.planning_start_date == "2026-09-02 00:00:00"
    assert planning_anchor(loaded) == datetime(2026, 9, 2)
    assert loaded["planning_start_date"] == "2026-09-02 00:00:00"  # legacy copy
    assert loaded["scheduler"]["time_limit"] == 60                  # rest intact


def test_stage_time_frame_raises_on_unwritable_toml(tmp_path):
    from helpers import horizon as hz
    work = tmp_path / "work"
    work.mkdir()
    (work / "flowstate.toml").write_text("this is = not [valid toml", encoding="utf-8")
    h = hz.resolve({"scheduler": {"anchor_mode": "today"}}, now=datetime(2026, 9, 2))
    with pytest.raises(sr.StagingError):
        sr._stage_time_frame(work, tmp_path / "nodata", h)


def test_patch_work_toml_raises_instead_of_dropping_overrides(tmp_path):
    toml = tmp_path / "flowstate.toml"
    toml.write_text("broken = [toml\ntime_limit = 60\n", encoding="utf-8")
    with pytest.raises(sr.StagingError, match="idle_weight"):
        sr._patch_work_toml(toml, 600, {"idle_weight": 3, "topload_weight": 450})
    with pytest.raises(sr.StagingError):
        sr._set_work_scheduler_flag(toml, "soft_demand", True)
    with pytest.raises(sr.StagingError):
        sr._set_work_use_current_mo(toml, False)


# ═══════════════════════════════════════════════════════════════════════════
# quality-7 / C82 — live work dir is not wiped; resume honours the exit status
# ═══════════════════════════════════════════════════════════════════════════

SCN_F = {"id": "F", "name": "Fill the tail", "objective": "balanced",
         "fill_mode": True}


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait(timeout=60)
    return p.pid


def _live_proc():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_run_scenario_refuses_to_wipe_a_live_work_dir(tmp_path):
    dd = tmp_path / "data"
    work = dd / "_scenario_work" / "F"
    work.mkdir(parents=True)
    (work / "schedule_phase2.csv").write_text("x\n", encoding="utf-8")
    live = _live_proc()
    try:
        sr._write_pending_manifest(work, SCN_F, time_limit=600, timeout_s=3000.0,
                                   pid=live.pid, cs_notes=None)
        result = sr.run_scenario(dict(SCN_F), dd)
    finally:
        live.kill()
        live.wait(timeout=60)
    assert result["ok"] is False and result["returncode"] == -2
    assert "refused" in result["log"] and str(live.pid) in result["log"]
    # nothing was wiped
    assert (work / "schedule_phase2.csv").exists()
    assert sr.read_pending_manifest(work) is not None


def _fake_outputs(work: Path) -> None:
    pd.DataFrame([{"line_id": 0, "line_name": "P09", "start_hour": 0.0,
                   "end_hour": 10.0, "sku": "111", "order_id": "111-W1",
                   "qty_kg": 5400.0}]).to_csv(work / "schedule_phase2.csv", index=False)
    (work / "_solver_stdout.txt").write_text("solver done\n", encoding="utf-8")


def test_resume_treats_infeasible_report_as_failure(tmp_path, monkeypatch):
    """Week-1 failure leaves a PARTIAL schedule_phase2.csv plus
    feasibility_report.json status INFEASIBLE (the solver exited 2)."""
    dd = tmp_path / "data"
    work = dd / "_scenario_work" / "F"
    work.mkdir(parents=True)
    _fake_outputs(work)
    (work / "feasibility_report.json").write_text(
        json.dumps({"relax_level": 3, "status": "INFEASIBLE"}), encoding="utf-8")
    sr._write_pending_manifest(work, SCN_F, time_limit=600, timeout_s=3000.0,
                               pid=_dead_pid(), cs_notes=None)
    monkeypatch.setattr(sr, "score_calendar", lambda cal, **kw: {"composite": 1.0})
    result = sr.resume_scenario(dict(SCN_F), dd)
    assert result["ok"] is False and result["returncode"] == 2
    assert "INFEASIBLE" in result["log"]
    assert sr.read_pending_manifest(work) is None   # nothing to save: retired


def test_resume_kills_an_over_budget_solver(tmp_path):
    dd = tmp_path / "data"
    work = dd / "_scenario_work" / "F"
    work.mkdir(parents=True)
    _fake_outputs(work)
    live = _live_proc()
    try:
        started = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        sr._write_pending_manifest(work, SCN_F, time_limit=1, timeout_s=1.0,
                                   pid=live.pid, cs_notes=None, started_at=started)
        result = sr.resume_scenario(dict(SCN_F), dd)
        assert result["ok"] is False and result["returncode"] == -9
        assert "killed" in result["log"]
        live.wait(timeout=30)
        assert not sr._pid_alive(live.pid)
    finally:
        if live.poll() is None:
            live.kill()
            live.wait(timeout=60)


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end staging on a deterministic sandbox (F and E overlays)
# ═══════════════════════════════════════════════════════════════════════════
#
# Clock: anchor Tue 2027-03-02 00:00 (ISO 2027-W09; Mon 03-01 is the demand
# anchor and the board's stored frame -> shift +24h). now = 03:00. Both dates
# are in the future so the overlay's real-clock now-floor stays inactive.
NOW = datetime(2027, 3, 2, 3, 0)

_SANDBOX_TOML = textwrap.dedent("""\
    [scheduler]
    planning_start_date = "2027-03-01 00:00:00"
    anchor_mode = "today"
    horizon_weeks = 3
    horizon_hours = 504
    time_limit = 60
    min_run_hours = 4
    max_lines_per_order = 2
    use_current_mo = true
    reforecast_running_mo_ends = false

    [cip]
    interval_h = 120
    duration_h = 6
    """)

_MANPRG_HEADER = ("Start date;Start time;Line;MO No.;Item;Designation;Pal;Type;"
                  "Hours;Fct qty (Cas);Qty made (Cas);Fct qty [Kg];;Qty made [Kg];;Left (Cas)")


def _manprg_row(date, time, line, mo, item, hours, fct, made, fct_kg, made_kg, left):
    return (f"{date};{time};LMH-{line};{mo};{item};{item} desc;640;GGS;{hours};"
            f"{fct};{made};{fct_kg};Kg;{made_kg};Kg;{left}")


def _make_sandbox(tmp_path: Path, *, with_trial: bool = False) -> Path:
    root = tmp_path / "sandbox"
    dd = root / "plant"            # NOT named 'data' (staging-9)
    ref = dd / "reference"
    ref.mkdir(parents=True)
    (root / "flowstate.toml").write_text(_SANDBOX_TOML, encoding="utf-8")
    pd.DataFrame([{"line_id": 0, "line_name": "P09", "active": True},
                  {"line_id": 1, "line_name": "P10", "active": True},
                  {"line_id": 3, "line_name": "P12", "active": True},
                  {"line_id": 4, "line_name": "P13", "active": True}]
                 ).to_csv(dd / "lines.csv", index=False)
    rows = [
        # P09 completed MO in W08 (start 02-24): 7,000 kg made, no W08 demand
        _manprg_row("02/24/2027", "10:00", "P09", "30000", "777", 10.0, 500, 500, 7000.0, 7000.0, 0),
        # P09 RUNNING MO sku 110: start Mon 20:00, 20.5h nominal -> ends
        # Tue 16:30 = hour 16.5 (reforecast off; remaining 10.25h from 03:00
        # = 13:15 < nominal end, so nominal wins)
        _manprg_row("03/01/2027", "20:00", "P09", "30001", "110", 20.5, 1000, 500, 8000.0, 4000.0, 500),
        # P09 queued 111: start 17:00 -> [17, 37], 10,000 kg
        _manprg_row("03/02/2027", "17:00", "P09", "30003", "111", 20.0, 1000, "", 10000.0, "", 1000),
        # P12 queued 222: start 00:00 but the cursor is now (03:00) -> [3, 23]
        _manprg_row("03/02/2027", "00:00", "P12", "30002", "222", 20.0, 1000, "", 4999.6, "", 1000),
        # P13 queued 888: 300h -> [3, 303] -> gate at 60% of the horizon
        _manprg_row("03/02/2027", "00:00", "P13", "30004", "888", 300.0, 9000, "", 90000.0, "", 9000),
        # unknown line P99 (not in lines.csv): must be skipped, never credited
        _manprg_row("03/02/2027", "05:00", "P99", "30099", "111", 10.0, 300, "", 3000.0, "", 300),
    ]
    if with_trial:
        # P10 trial 08:00 + 4h -> [8, 12]; overlaps the P10 line-down 10:30-20:30
        rows.append(_manprg_row("03/02/2027", "08:00", "P10", "30077", "TRIALS", 4.0, 1, "", 0.0, "", 1))
    (ref / "manprg.txt").write_text("\n".join([_MANPRG_HEADER, *rows]) + "\n",
                                    encoding="cp1252")
    (ref / "cip_info.csv").write_text(textwrap.dedent("""\
        ID,LineEquipment,PreviousCIP,MaxHoursBetweenCIP,ScheduledCIP,Notes
        1,P09,2027-02-28 00:00:00,120,,
        2,P10,,144,,
        3,P12,2027-03-01 00:00:00,120,,
        4,P13,2027-03-01 00:00:00,120,,
        """), encoding="utf-8")
    dem = _dem([("111-W0", "111", 0, 20000), ("222-W0", "222", 0, 5000),
                ("333-W0", "333", 0, 1000), ("333-W1", "333", 1, 8000),
                ("444-W3", "444", 3, 1_715_000), ("555-W4", "555", 4, 1000),
                ("666-W0", "666", 0, 3000), ("777-W0", "777", 0, 9000)])
    dem.to_csv(ref / "demand_plan.csv", index=False)
    (ref / "demand_plan.source.json").write_text(json.dumps({
        "source": "demand_plan_summary.csv", "anchor": "2027-03-01 00:00:00"}),
        encoding="utf-8")
    pd.DataFrame([{"line_id": 1, "line_name": "P10",
                   "start_datetime": "2027-03-02 10:30",
                   "end_datetime": "2027-03-02 20:30", "reason": "Down"}]
                 ).to_csv(ref / "downtimes.csv", index=False)
    skus = ["110", "111", "222", "333", "444", "555", "666", "777", "888"]
    pd.DataFrame([{"line_id": lid, "sku": s, "line_name": ln, "capable": 1,
                   "calc_rate_kgph": 500.0}
                  for lid, ln in ((0, "P09"), (1, "P10"), (3, "P12"), (4, "P13"))
                  for s in skus]).to_csv(ref / "capabilities_rates.csv", index=False)
    pd.DataFrame([{"line_id": 0, "Line": "P09", "rate_kgph": 500},
                  {"line_id": 1, "Line": "P10", "rate_kgph": 500},
                  {"line_id": 3, "Line": "P12", "rate_kgph": 500},
                  {"line_id": 4, "Line": "P13", "rate_kgph": 500}]
                 ).to_csv(ref / "line_rates.csv", index=False)
    pd.DataFrame([{"from_sku": "111", "to_sku": "222", "setup_hours": 1,
                   "ttp_change": 0, "ffs_change": 0, "tpld_change": 0,
                   "cspkr_change": 0, "conv_to_org": 0, "cinn_to_non_cinn": 0,
                   "added_flavors": 0, "cip_req_after": 0}]
                 ).to_csv(ref / "changeovers.csv", index=False)
    pd.DataFrame([{"sku": s, "designation": s} for s in skus]
                 ).to_csv(ref / "sku_info.csv", index=False)
    pd.DataFrame([{"line_id": 0, "line_name": "P09", "max_cip_hrs": 120},
                  {"line_id": 1, "line_name": "P10", "max_cip_hrs": 144},
                  {"line_id": 3, "line_name": "P12", "max_cip_hrs": 120},
                  {"line_id": 4, "line_name": "P13", "max_cip_hrs": 120}]
                 ).to_csv(ref / "line_cip_hrs.csv", index=False)
    pd.DataFrame([
        {"line_id": 0, "line_name": "P09", "initial_sku": "999", "available_from_hour": 0,
         "long_shutdown_flag": 0, "long_shutdown_extra_setup_hours": 0,
         "carryover_run_hours_since_last_cip_at_t0": 50},
        {"line_id": 1, "line_name": "P10", "initial_sku": "999", "available_from_hour": 0,
         "long_shutdown_flag": 1, "long_shutdown_extra_setup_hours": 2,
         "carryover_run_hours_since_last_cip_at_t0": 0},
        {"line_id": 3, "line_name": "P12", "initial_sku": "999", "available_from_hour": 0,
         "long_shutdown_flag": 0, "long_shutdown_extra_setup_hours": 0,
         "carryover_run_hours_since_last_cip_at_t0": 0},
        {"line_id": 4, "line_name": "P13", "initial_sku": "999", "available_from_hour": 0,
         "long_shutdown_flag": 0, "long_shutdown_extra_setup_hours": 0,
         "carryover_run_hours_since_last_cip_at_t0": 0},
    ]).to_csv(ref / "initial_states.csv", index=False)
    # Board in the CONFIG frame (anchor Mon 03-01): hours are 24h "late"
    board = _cal([
        _blk("cip_3cff597fb3", "P12", 236, 242, "CIP", btype="cip",
             attrs="solver:cip_req", line_id=3, order_id="", qty=None),
        _blk("cip_a27f3085c3", "P09", 300, 306, "CIP", btype="cip", attrs="",
             line_id=0, order_id="", qty=None),
        _blk("pin_1", "P09", 160, 180, "333", attrs="pinned", line_id=0,
             order_id="333-W1", qty=5000.0),
        _blk("pin_old", "P09", -10, 10, "333", attrs="pinned", line_id=0,
             order_id="333-W1", qty=5000.0),
        _blk("blk_planner", "P10", 200, 206, "CIP", btype="cip",
             attrs="planner:cip", line_id=1, order_id="", qty=None),
        _blk("cs_copy", "P12", 24, 44, "222", attrs="current_state:queued;pct=0.0",
             line_id=3, order_id="30002", qty=4999.6),
    ])
    board.to_csv(dd / "calendar_blocks.csv", index=False)
    return dd


@pytest.fixture
def frozen_clock(monkeypatch):
    from helpers import horizon as hzmod
    real = hzmod.resolve

    def _resolve(cfg=None, now=None):
        return real(cfg, now=NOW)

    monkeypatch.setattr(hzmod, "resolve", _resolve)


def test_overlay_fill_end_to_end(tmp_path, frozen_clock):
    dd = _make_sandbox(tmp_path)
    work = dd / "_scenario_work" / "F"
    sr._prepare_work_dir(dd, work)
    notes = sr._overlay_fill(work, dd)
    joined = "\n".join(notes)

    # staging-9: the sandbox (not the live repo) was read — its lines are
    # P09/P10/P12/P13 and its unknown line P99 was reported (by current_state
    # itself — CA's adversarial-11 fix — or by the staging fallback filter)
    # and never staged
    assert "P99" in joined and "unknown" in joined and "lines.csv" in joined
    assert not (pd.read_csv(work / "committed_blocks.csv", dtype=str)["line_name"] == "P99").any()
    # C55: stale board re-based +24h, the fully-past pin dropped
    assert "board re-based +24h" in joined and "1 fully-past block(s) dropped" in joined
    # C41: only P10 carries a genuine planner CIP; P12's solver clean is not one
    assert "1 planner CIP(s) held FIXED" in joined and "cleans on P10 " in joined
    assert "CIP CHECK" not in joined                       # every grid intact
    committed = pd.read_csv(work / "committed_blocks.csv", dtype=str, keep_default_na=False)
    p12_cips = committed[(committed["line_name"] == "P12") & (committed["block_type"] == "cip")]
    assert len(p12_cips) >= 3
    assert p12_cips["attrs"].str.contains("current_state:cip_projected").all()
    assert not committed["attrs"].str.contains("solver:").any()
    pin = committed[committed["block_id"] == "pin_1"].iloc[0]
    assert (float(pin["start_h"]), float(pin["end_h"])) == (136.0, 156.0)   # 160-24

    # C29 + C11: work downtimes — pure CIP rows, outward-rounded line-down
    dt = pd.read_csv(work / "downtimes.csv")
    p10 = sorted((int(r.start_hour), int(r.end_hour), r.reason) for r in dt.itertuples()
                 if r.line_name == "P10")
    assert p10 == [(10, 21, "Down"), (176, 182, "Committed CIP")]   # 10.5/20.5 -> 10/21; 200-24
    p09 = sorted((int(r.start_hour), int(r.end_hour), r.reason) for r in dt.itertuples()
                 if r.line_name == "P09")
    assert (0, 37, "Committed PRODUCTION 30001 + Committed PRODUCTION 30003") in p09
    assert (136, 156, "Committed PRODUCTION 333-W1") in p09
    p09_cips = [(s, e) for s, e, r in p09 if r == "Committed CIP"]
    assert (72, 78) in p09_cips and all(e - s == 6 for s, e in p09_cips)
    # no two fixed intervals overlap on any line
    for ln, grp in dt.groupby("line_name"):
        spans = sorted(zip(grp["start_hour"], grp["end_hour"]))
        assert all(b0 >= a1 for (_, a1), (b0, _) in zip(spans, spans[1:])), (ln, spans)

    # staging-4 + gates (+ staging-10 warning for P13)
    init = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str}).set_index("line_name")
    assert init.loc["P09", "initial_sku"] == "111" and init.loc["P09", "available_from_hour"] == 37
    assert init.loc["P10", "initial_sku"] == "CLEAN" and init.loc["P10", "available_from_hour"] == 0
    assert init.loc["P10", "long_shutdown_flag"] == 0
    assert init.loc["P10", "long_shutdown_extra_setup_hours"] == 0
    assert init.loc["P12", "initial_sku"] == "222" and init.loc["P12", "available_from_hour"] == 23
    # P13: 300h queued MO split around two 6h projected cleans (CA's C09 fix
    # pushes production past a clean instead of deleting hours): tail ends
    # at 3 + 300 + 2 x 6 = 315
    assert init.loc["P13", "initial_sku"] == "888" and init.loc["P13", "available_from_hour"] == 315
    assert (init["carryover_run_hours_since_last_cip_at_t0"] == 0).all()
    assert "P10->CLEAN" in joined
    assert "GATE CHECK" in joined and "line P13: fill gate 315h blocks 62%" in joined  # 315/504
    gates = json.loads((work / "fill_gates.json").read_text(encoding="utf-8"))["gates"]
    assert gates == {"P09": 37.0, "P10": 0.0, "P12": 23.0, "P13": 315.0}

    # netting: C56 explicit bounds, netting-7 drops, C54 warning, C20 pro-rate
    dem = pd.read_csv(work / "demand_plan.csv", dtype={"sku": str}).set_index("order_id")
    assert set(dem.index) == {"111-W0", "333-W1", "444-W3", "666-W0", "777-W0"}
    r = dem.loc["111-W0"]        # 20,000 gross, 10,000 committed (P09 [17,37])
    assert r["qty_target"] == 10000.0
    assert (r["qty_min"], r["qty_max"]) == (8000.0, 12000.0)   # 18,000-10,000 / 22,000-10,000
    assert pd.isna(r["lower_pct"]) and r["credit_kg"] == 10000.0
    r = dem.loc["333-W1"]        # pin [136,156]: 8h W09 (2,000) + 12h W10 (3,000);
    assert r["qty_target"] == 4000.0     # W09 covers 333-W0 (1,000), 1,000 carries: 4,000 applied
    assert (r["qty_min"], r["qty_max"]) == (3200.0, 4800.0)   # 7,200-4,000 / 8,800-4,000
    r = dem.loc["444-W3"]        # raw [504,671] -> [480,647]; 24h of 168 inside
    assert r["qty_target"] == 245000.0   # 1,715,000 x 24/168
    assert (r["due_start_hour"], r["due_end_hour"]) == (480.0, 503.0)
    assert (r["lower_pct"], r["upper_pct"]) == (0.9, 1.1)      # no credit: pct kept
    assert dem.loc["777-W0", "qty_target"] == 9000.0          # W08 7,000 kg NOT carried
    assert "WARNING: 7,000 kg of past production" in joined
    assert "2 fully-covered order(s) dropped" in joined        # 222-W0 (0.4 kg), 333-W0
    assert "1,470,000 kg DEFERRED" in joined                  # 1,715,000 - 245,000
    assert (work / "coverage_ledger.csv").exists()
    assert not (work / "current_mo.csv").exists()


def test_overlay_current_state_end_to_end(tmp_path, frozen_clock):
    dd = _make_sandbox(tmp_path, with_trial=True)
    work = dd / "_scenario_work" / "E"
    sr._prepare_work_dir(dd, work)
    notes = sr._overlay_current_state(work, dd, lock_current_mo=True)
    joined = "\n".join(notes)

    init = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str}).set_index("line_name")
    # C11: running MO ends 16.5h -> gate ceil = 17 (int() gave 16)
    assert init.loc["P09", "available_from_hour"] == 17
    assert init.loc["P09", "initial_sku"] == "110"
    # staging-4 in the E path: no running MO -> CLEAN, long-shutdown zeroed
    assert init.loc["P10", "initial_sku"] == "CLEAN"
    assert init.loc["P10", "long_shutdown_flag"] == 0
    assert init.loc["P12", "initial_sku"] == "CLEAN"
    assert "P10->CLEAN" in joined
    # C64: the running MO is the gate ONLY — not a locked current_mo order
    cmo = pd.read_csv(work / "current_mo.csv", dtype={"mo": str, "sku": str})
    assert "30001" not in set(cmo["mo"])
    assert set(cmo["mo"]) == {"30003", "30002", "30004"}        # queued only; P99 skipped
    assert "1 running MO(s) kept as the line gate only" in joined
    assert "P99" in joined and "unknown" in joined       # cs warning surfaced in E too
    # staging-12: trial [8,12] unioned with the line-down [10,21] -> one row
    dt = pd.read_csv(work / "downtimes.csv")
    p10 = sorted((int(r.start_hour), int(r.end_hour)) for r in dt.itertuples()
                 if r.line_name == "P10")
    assert p10 == [(8, 21)]
    # C21: the work toml carries the staging anchor where the solver reads it
    import tomllib
    cfg = tomllib.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    assert cfg["scheduler"]["planning_start_date"] == "2027-03-02 00:00:00"


# ═══════════════════════════════════════════════════════════════════════════
# greedy seed setup rule == solver setup rule; board identity in _collect_result
# ═══════════════════════════════════════════════════════════════════════════

def test_seed_setup_hours_round_up_like_the_solver():
    """Solver rule (changeover_cache.round_setup_hours, SA-4 / C36): whole
    hours rounded UP — 0.25 -> 1, 1.25 -> 2, 2.5 -> 3, 3.0 -> 3, 0 -> 0.
    The loader's (memoised) source dict is left untouched."""
    src = {"A": {"B": 0.25, "C": 1.25, "D": 3.0}, "B": {"A": 2.5, "Z": 0.0}}
    out = sr._seed_setup_hours(src)
    assert out == {"A": {"B": 1.0, "C": 2.0, "D": 3.0}, "B": {"A": 3.0, "Z": 0.0}}
    assert src["A"]["B"] == 0.25 and out is not src
    assert sr._seed_setup_hours({}) == {}


def test_collect_result_keeps_board_identity_across_frames(tmp_path, monkeypatch):
    """Board stored in the CONFIG frame (anchor Mon 2027-03-01) carries a
    LOCKED block blk_keep for 111-W1 on P09 at 24-34h. The schedule was
    staged on Tue 03-02 (work toml [scheduler].planning_start_date, C21) and
    places 111-W1 on P09 at 0-10h. board_shift_h = 03-01 - 03-02 = -24h, so
    board start 24 + (-24) = 0 = schedule start -> the proposal row keeps
    block_id blk_keep and locked=True; the other order gets a fresh id."""
    root = tmp_path / "sandbox"
    dd = root / "plant"
    work = dd / "_scenario_work" / "E"
    work.mkdir(parents=True)
    (root / "flowstate.toml").write_text(_TOML.replace("2026-08-25", "2027-03-01"),
                                         encoding="utf-8")
    (work / "flowstate.toml").write_text(_TOML.replace("2026-08-25", "2027-03-02"),
                                         encoding="utf-8")
    _cal([_blk("blk_keep", "P09", 24, 34, "111", order_id="111-W1", line_id=0)]
         ).assign(locked=True).to_csv(dd / "calendar_blocks.csv", index=False)
    pd.DataFrame([
        {"line_id": 0, "line_name": "P09", "start_hour": 0.0, "end_hour": 10.0,
         "sku": "111", "order_id": "111-W1", "qty_kg": 5000.0},
        {"line_id": 1, "line_name": "P10", "start_hour": 0.0, "end_hour": 10.0,
         "sku": "222", "order_id": "222-W1", "qty_kg": 5000.0},
    ]).to_csv(work / "schedule_phase2.csv", index=False)
    monkeypatch.setattr(sr, "score_calendar", lambda cal, **kw: {"composite": 1.0})
    kw = sr._board_identity_kwargs(work, dd)
    assert kw["board_shift_h"] == -24.0
    res = sr._collect_result({"id": "E", "name": "E"}, work, dd, 0, "", "", [])
    assert res["ok"] is True
    cal = res["calendar"].set_index("order_id")
    assert cal.loc["111-W1", "block_id"] == "blk_keep"
    assert bool(cal.loc["111-W1", "locked"]) is True
    assert cal.loc["222-W1", "block_id"] != "blk_keep"
    assert bool(cal.loc["222-W1", "locked"]) is False
    # no board -> the import is exactly what it always was
    assert sr._board_identity_kwargs(work, tmp_path / "nowhere") == {}
