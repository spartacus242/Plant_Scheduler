# tests/test_greedy_fill.py — greedy tail-packing seed (Scenario F warm start).

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.greedy_fill import build_greedy_fill, free_segments  # noqa: E402


def test_free_segments_invert_blocked_windows():
    segs = free_segments(10, [(0, 20), (30, 40), (100, 504)], 504)
    assert segs == [(20, 30), (40, 100)]


def test_free_segments_no_blocks():
    assert free_segments(150, [], 504) == [(150.0, 504)]


def _demand(**kw):
    base = {"order_id": "111-W1", "sku": "111", "week_index": 1,
            "qty_target": 35000, "lower_pct": 0.9, "upper_pct": 1.1,
            "due_start_hour": 168, "due_end_hour": 335}
    base.update(kw)
    return base


def test_places_inside_window_and_respects_qmax():
    rows, summary = build_greedy_fill(
        [_demand()],
        rates={("P09", "111"): 700.0},
        setups={},
        line_segments={"P09": [(150.0, 504.0)]},
        line_ids={"P09": 0},
        initial_sku={"P09": "999"},
        horizon_h=504.0,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["start_hour"] >= 168          # due window respected
    assert r["end_hour"] <= 336
    assert r["qty_kg"] <= 35000 * 1.1 + 1  # qmax cap
    assert r["qty_kg"] >= 35000 * 0.9      # target-ish fill
    assert summary["kg_by_week"].get(1, 0) > 0


def test_same_sku_tail_preferred_over_faster_line():
    rows, _ = build_greedy_fill(
        [_demand(qty_target=14000)],
        rates={("P09", "111"): 700.0, ("P10", "111"): 1400.0},
        setups={"999": {"111": 6.0}},
        line_segments={"P09": [(168.0, 504.0)], "P10": [(168.0, 504.0)]},
        line_ids={"P09": 0, "P10": 1},
        initial_sku={"P09": "111", "P10": "999"},  # P09 already runs 111
        horizon_h=504.0,
    )
    assert rows[0]["line_name"] == "P09"   # zero changeover beats 2x rate


def test_setup_gap_inserted_when_sku_differs_mid_segment():
    # first placement in a fresh segment: no gap (boundary = committed/CIP).
    # second placement on the same line, different sku: gap = setup hours.
    rows, _ = build_greedy_fill(
        [_demand(order_id="A-W1", sku="111", qty_target=7000),
         _demand(order_id="B-W1", sku="222", qty_target=7000)],
        rates={("P09", "111"): 700.0, ("P09", "222"): 700.0},
        setups={"111": {"222": 3.0}},
        line_segments={"P09": [(168.0, 504.0)]},
        line_ids={"P09": 0},
        initial_sku={"P09": ""},
        horizon_h=504.0,
    )
    assert len(rows) == 2
    a, b = sorted(rows, key=lambda r: r["start_hour"])
    assert b["start_hour"] - a["end_hour"] >= 3   # setup gap honoured


def test_max_two_lines_per_order():
    # target too big for one line's window -> spills to a second, never a third
    rows, _ = build_greedy_fill(
        [_demand(qty_target=700.0 * 300)],  # 300h of work
        rates={("P09", "111"): 700.0, ("P10", "111"): 700.0,
               ("P11", "111"): 700.0},
        setups={},
        line_segments={"P09": [(168.0, 288.0)], "P10": [(168.0, 288.0)],
                       "P11": [(168.0, 288.0)]},
        line_ids={"P09": 0, "P10": 1, "P11": 2},
        initial_sku={},
        horizon_h=504.0,
    )
    assert len({r["line_name"] for r in rows}) <= 2


def test_deterministic():
    args = dict(
        rates={("P09", "111"): 700.0, ("P10", "111"): 700.0},
        setups={}, line_ids={"P09": 0, "P10": 1}, initial_sku={},
        horizon_h=504.0)
    a, _ = build_greedy_fill([_demand()], line_segments={
        "P09": [(168.0, 504.0)], "P10": [(168.0, 504.0)]}, **args)
    b, _ = build_greedy_fill([_demand()], line_segments={
        "P09": [(168.0, 504.0)], "P10": [(168.0, 504.0)]}, **args)
    assert a == b
