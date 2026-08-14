# tests/test_plan_fill.py — Scenario F engine (committed plan fixed, fill
# the tail). Pure functions only; the IO shell is scenario_runner's overlay.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.plan_fill import (  # noqa: E402
    committed_windows,
    last_sku_per_line,
    line_free_from,
    subtract_committed,
)


def _blocks(rows):
    base = {"block_id": "b", "block_type": "production", "line_id": 0,
            "line_name": "P09", "start_h": 0.0, "end_h": 10.0, "label": "",
            "order_id": "MO1", "sku": "111", "sku_description": "",
            "qty_kg": None, "locked": False, "attrs": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


def _demand(rows):
    base = {"order_id": "X-W0", "sku": "X", "week_index": 0,
            "qty_target": 10000, "lower_pct": 0.9, "upper_pct": 1.1,
            "due_start_hour": 0, "due_end_hour": 167, "priority": 3}
    return pd.DataFrame([{**base, **r} for r in rows])


# ── committed_windows ──────────────────────────────────────────────────────

def test_windows_round_outward_and_clip():
    blocks = _blocks([
        {"start_h": 10.4, "end_h": 20.6},                    # -> [10, 21]
        {"start_h": -30.0, "end_h": -5.0},                   # fully past: gone
        {"start_h": 500.0, "end_h": 520.0},                  # clipped to 504
        {"block_type": "cip", "sku": "CIP", "start_h": 30.2, "end_h": 36.0},
        {"block_type": "trial", "sku": "TRIALS", "start_h": 40.0, "end_h": 44.0},
    ])
    w = committed_windows(blocks, 504)
    spans = [(r["start_hour"], r["end_hour"]) for r in w]
    assert (10, 21) in spans
    assert (500, 504) in spans
    assert (30, 36) in spans and (40, 44) in spans
    assert len(w) == 4  # the fully-past one is gone
    assert all("Committed" in r["reason"] for r in w)


# ── last_sku_per_line / line_free_from ─────────────────────────────────────

def test_last_sku_ignores_trials_and_cips():
    blocks = _blocks([
        {"sku": "111", "start_h": 0, "end_h": 10},
        {"sku": "222", "start_h": 10, "end_h": 30},
        {"block_type": "trial", "sku": "TRIALS", "start_h": 30, "end_h": 40},
        {"block_type": "cip", "sku": "CIP", "start_h": 40, "end_h": 46},
    ])
    assert last_sku_per_line(blocks) == {"P09": "222"}


def test_free_from_is_committed_work_only_cips_excluded():
    # Projected CIPs run through the WHOLE horizon; counting them gated
    # every line at ~504h and made an empty schedule "optimal" (F run 2).
    blocks = _blocks([
        {"sku": "111", "start_h": 0, "end_h": 30},
        {"block_type": "cip", "sku": "CIP", "start_h": 30, "end_h": 36},
        {"block_type": "cip", "sku": "CIP", "start_h": 490, "end_h": 496},
        {"line_name": "P10", "sku": "333", "start_h": 5, "end_h": 12},
        {"line_name": "P10", "block_type": "trial", "sku": "TRIALS",
         "start_h": 12, "end_h": 20},
    ])
    free = line_free_from(blocks, 504)
    assert free["P09"] == 30.0   # work tail; CIPs block via windows instead
    assert free["P10"] == 20.0   # trials ARE committed work-time


# ── subtract_committed ─────────────────────────────────────────────────────

def test_committed_kg_reduces_the_matching_week():
    blocks = _blocks([{"sku": "111", "qty_kg": 30000.0,
                       "start_h": 20, "end_h": 60}])   # midpoint w0
    dem = _demand([{"order_id": "111-W0", "sku": "111", "qty_target": 50000}])
    out, notes = subtract_committed(dem, blocks)
    assert out.iloc[0]["qty_target"] == 20000.0
    assert len(notes) == 1 and "30,000 kg" in notes[0]


def test_committed_surplus_carries_forward_never_backward():
    blocks = _blocks([{"sku": "111", "qty_kg": 80000.0,
                       "start_h": 180, "end_h": 220}])  # midpoint week 1
    dem = _demand([
        {"order_id": "111-W0", "sku": "111", "week_index": 0, "qty_target": 50000},
        {"order_id": "111-W1", "sku": "111", "week_index": 1, "qty_target": 50000},
        {"order_id": "111-W2", "sku": "111", "week_index": 2, "qty_target": 50000},
    ])
    out, _ = subtract_committed(dem, blocks)
    by = {r["order_id"]: r["qty_target"] for _, r in out.iterrows()}
    assert by["111-W0"] == 50000        # week-1 production never covers week 0
    assert by["111-W1"] == 0.0          # fully covered
    assert by["111-W2"] == 20000.0      # 30k surplus rolled forward


def test_trials_and_unknown_kg_never_reduce_demand():
    blocks = _blocks([
        {"block_type": "trial", "sku": "TRIALS", "qty_kg": 60000.0,
         "start_h": 0, "end_h": 24},
        {"sku": "111", "qty_kg": None, "start_h": 0, "end_h": 24},
    ])
    dem = _demand([{"order_id": "111-W0", "sku": "111", "qty_target": 50000}])
    out, notes = subtract_committed(dem, blocks)
    assert out.iloc[0]["qty_target"] == 50000
    assert notes == []


# ── coalesce_windows ───────────────────────────────────────────────────────

def test_coalesce_merges_overlapping_fixed_windows():
    from helpers.plan_fill import coalesce_windows
    rows = [
        {"line_id": 2, "line_name": "P11", "start_hour": 0, "end_hour": 504,
         "reason": "Down"},
        {"line_id": 2, "line_name": "P11", "start_hour": 10, "end_hour": 40,
         "reason": "Committed PRODUCTION 29901"},
        {"line_id": 0, "line_name": "P09", "start_hour": 0, "end_hour": 10,
         "reason": "Committed PRODUCTION A"},
        {"line_id": 0, "line_name": "P09", "start_hour": 10, "end_hour": 16,
         "reason": "Committed CIP"},          # touching: merges
        {"line_id": 0, "line_name": "P09", "start_hour": 30, "end_hour": 40,
         "reason": "Committed PRODUCTION B"},  # gap: stays separate
    ]
    out = coalesce_windows(rows)
    p11 = [r for r in out if r["line_name"] == "P11"]
    assert len(p11) == 1 and (p11[0]["start_hour"], p11[0]["end_hour"]) == (0, 504)
    p09 = sorted([(r["start_hour"], r["end_hour"]) for r in out
                  if r["line_name"] == "P09"])
    assert p09 == [(0, 16), (30, 40)]
