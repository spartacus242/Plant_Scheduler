# tests/test_seed_prebuild.py — the greedy seed's PRE-BUILD pass (2026-09-16).
#
# Planner decisions 2026-09-16: "the solver can pre-build the full week 41
# into the idle tail as inventory" ([scheduler] partial_week_demand =
# "prebuild") on top of the plant rule early_fill_hours = "unbounded". The
# seed placed every order only inside [due_start, due_end]: on the staged
# 09-16 board all 19 week-41 seed rows sat in [456, 504) and ~1.2 Mt of the
# optional pre-build was left for pass 1 to discover. The pass pinned here
# adds runs BEFORE the due week in time the in-window pass left free, and
# every row set it emits must stay a feasible, COMPLETE-able hint (the
# pass-1 anchor fixes every hinted variable):
#   * start >= the model's own floor (model_builder.effective_due_start),
#     gate + initial changeover, setups against the blocks on the left;
#   * one assignment per (line, order): a contiguous EXTENSION of the row, a
#     seg_a in front of a committed CIP window with the row as seg_b (nothing
#     in between), or a NEW row on a line the order does not use yet;
#   * nearest week-step first, then the cheapest transition, then latest;
#   * BEFORE any pre-build, an in-window REMAINDER pass gives every short
#     order (W0 included) its own window on lines it does not use yet, at
#     the soft-demand model's floor (min_run_hours), so a later week never
#     pre-builds into hours a nearer, capable, short order could have used.
# Plus the version telemetry: a saved proposal keeps seed_anchor and
# min_run_too_small.

from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import model_builder as mb  # noqa: E402
import phase2_scheduler as p2  # noqa: E402
from data_loader import Data, Files  # noqa: E402
from helpers.greedy_fill import build_greedy_fill, free_segments, model_start_floor  # noqa: E402
from model_builder import build_model  # noqa: E402
from warm_start import apply_warm_start, build_hint_plan  # noqa: E402

import test_seed_adoption as tsa  # noqa: E402  (params / Data / work-dir builders)

NAN = float("nan")


@pytest.fixture(autouse=True)
def _log_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(p2, "ERR_FILE", tmp_path / "solver_error.txt")
    monkeypatch.setattr(p2, "KPI_FILE", tmp_path / "solver_kpis.txt")


def _dem(oid, sku, ds, de, target, week, *, qmin=None, qmax=None, **kw):
    """A staged demand row with explicit bounds (the prebuild rows' shape)."""
    row = {"order_id": oid, "sku": sku, "week_index": week,
           "qty_target": float(target), "lower_pct": NAN, "upper_pct": NAN,
           "qty_min": float(qmin if qmin is not None else math.floor(target * 0.25)),
           "qty_max": float(qmax if qmax is not None else math.ceil(target * 1.1)),
           "due_start_hour": float(ds), "due_end_hour": float(de)}
    row.update(kw)
    return row


def _one_line(**kw):
    base = dict(rates={("P09", s): 1000.0 for s in "ABCI"}, setups={},
                line_segments={"P09": [(100.0, 504.0)]}, line_ids={"P09": 0},
                initial_sku={"P09": ""}, horizon_h=504.0, min_run_hours=8,
                line_gates={"P09": 100.0})
    base.update(kw)
    return base


def _span(rows, oid):
    rs = [r for r in rows if r["order_id"] == oid]
    return min(r["start_hour"] for r in rs), max(r["end_hour"] for r in rs)


# ═══════════════════════════════════════════════════════════════════════════
# the legal early window mirrors the model
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("efh", [None, 0, 24, 500])
@pytest.mark.parametrize("es", [None, 100.0, 440.2, 470.0])
@pytest.mark.parametrize("allow", [True, False])
def test_start_floor_is_the_models_effective_due_start(efh, es, allow):
    """model_start_floor reads the raw CSV fields the loader reads and must
    equal model_builder.effective_due_start on the loaded order for every
    policy: unbounded (None), bounded hours, no early fill, stock floor."""
    P = tsa._f_params(horizon_h=504, early_fill_hours=efh, allow_week1_in_week0=allow)
    row = _dem("A-W3", "A", 456.0, 503.0, 10000, 3,
               earliest_start_hour=(NAN if es is None else es))
    loaded = {"order_id": "A-W3", "sku": "A", "due_start": 456, "due_end": 503,
              "earliest_start": None if es is None else max(0, math.ceil(es))}
    assert model_start_floor(row, allow_early_fill=allow, early_fill_hours=efh) \
        == mb.effective_due_start(P, loaded)


# ═══════════════════════════════════════════════════════════════════════════
# extension / setup / initial changeover
# ═══════════════════════════════════════════════════════════════════════════
def test_prebuild_extends_the_row_back_into_idle_time():
    """W3 order due [456, 503], 100 t at 1 t/h, line free from h100. The
    in-window pass fits 48 h at [456, 504); the rest (52 h) goes directly in
    front of it as ONE contiguous row (latest possible, no changeover)."""
    rows, summary = build_greedy_fill([_dem("A-W3", "A", 456, 503, 100000, 3)],
                                      **_one_line())
    assert len(rows) == 1
    r = rows[0]
    assert (r["start_hour"], r["end_hour"], r["run_hours"]) == (404, 504, 100)
    assert r["qty_kg"] == 100000.0
    pb = summary["prebuild"]
    assert (pb["runs"], pb["extended"], pb["kg"]) == (1, 1, 52000)
    assert pb["kg_by_week_index"] == {3: 52000}
    assert summary["orders_short"] == []
    # prebuild off: the old in-window seed, bit for bit
    rows0, s0 = build_greedy_fill([_dem("A-W3", "A", 456, 503, 100000, 3)],
                                  prebuild=False, **_one_line())
    assert [(x["start_hour"], x["end_hour"]) for x in rows0] == [(456, 504)]
    assert s0["prebuild"]["runs"] == 0


def test_prebuild_never_starts_before_the_policy_floor():
    """early_fill_hours = 24 -> floor 432; a stock floor at h440 wins over it;
    the extension stops there and the order stays short."""
    d = _dem("A-W3", "A", 456, 503, 100000, 3)
    rows, _ = build_greedy_fill([d], early_fill_hours=24, **_one_line())
    assert _span(rows, "A-W3") == (432, 504)
    rows, _ = build_greedy_fill([{**d, "earliest_start_hour": 440.0}], **_one_line())
    assert _span(rows, "A-W3") == (440, 504)


def test_prebuild_pays_the_setup_owed_to_the_block_on_its_left():
    """B-W1 sits at [168, 188); A-W3 (400 t) extends back until it meets the
    B->A setup (1.5 h -> 2 whole hours): start 190, never 188."""
    dem = [_dem("B-W1", "B", 168, 335, 20000, 1, qmax=20000),
           _dem("A-W3", "A", 456, 503, 400000, 3)]
    rows, _ = build_greedy_fill(dem, **_one_line(setups={"B": {"A": 1.5}}))
    assert _span(rows, "B-W1") == (168, 188)
    assert _span(rows, "A-W3") == (190, 504)


def test_prebuild_first_block_pays_the_initial_changeover_and_long_shutdown():
    """Alone on a line gated at h100 holding I (I->A 3 h, long shutdown +2):
    the pre-built row may not start before 100 + 3 + 2."""
    rows, _ = build_greedy_fill(
        [_dem("A-W3", "A", 456, 503, 900000, 3)],
        **_one_line(setups={"I": {"A": 3.0}}, initial_sku={"P09": "I"},
                    initial_extra_hours={"P09": 2.0}))
    assert _span(rows, "A-W3") == (105, 504)


# ═══════════════════════════════════════════════════════════════════════════
# seg_a / seg_b around a committed CIP window
# ═══════════════════════════════════════════════════════════════════════════
def test_prebuild_pairs_the_row_across_a_committed_cip_window():
    """Committed CIP [400, 406): A-W3 (150 t, one line allowed) extends its
    row to the CIP's end (406) and puts the remaining 52 h in front of the
    clean as seg_a, latest possible: [348, 400) + [406, 504). The rows map
    onto ONE assignment with seg_b present."""
    segs = free_segments(100, [(400, 406)], 504.0)
    rows, summary = build_greedy_fill(
        [_dem("A-W3", "A", 456, 503, 150000, 3)],
        **_one_line(line_segments={"P09": segs}, max_lines_per_order=1),
        cip_windows={"P09": [(400, 406)]})
    got = sorted((r["start_hour"], r["end_hour"]) for r in rows)
    assert got == [(348, 400), (406, 504)]
    assert summary["prebuild"]["cip_pairs"] == 1 and summary["prebuild"]["extended"] == 1
    data = type("D", (), {"orders": [{"order_id": "A-W3"}], "lines": [0]})()
    plan, stats = build_hint_plan(data, 504, [
        {"line_id": r["line_id"], "order_id": r["order_id"], "start_hour": r["start_hour"],
         "end_hour": r["end_hour"], "run_hours": r["run_hours"]} for r in rows])
    e = plan[(0, 0)]
    assert (e["seg_a_start"], e["seg_a_end"], e["seg_b_present"],
            e["seg_b_start"], e["seg_b_end"]) == (348, 400, 1, 406, 504)
    assert stats["split_assignments"] == 1
    # a non-CIP committed window (production) is no clean: no seg_b allowed
    rows, _ = build_greedy_fill(
        [_dem("A-W3", "A", 456, 503, 150000, 3)],
        **_one_line(line_segments={"P09": segs}, max_lines_per_order=1),
        cip_windows={})
    assert sorted((r["start_hour"], r["end_hour"]) for r in rows) == [(406, 504)]


def test_prebuild_pair_never_straddles_another_orders_block():
    """B-W2 at [380, 395) sits between the free time and A-W3's row behind
    the CIP [400, 406): the model's pairwise ordering forbids any order
    inside A's span, and 5 h after B are below the minimum run -> no pair."""
    segs = free_segments(100, [(400, 406)], 504.0)
    dem = [_dem("B-W2", "B", 380, 399, 15000, 2, qmax=15000),
           _dem("A-W3", "A", 456, 503, 150000, 3)]
    rows, _ = build_greedy_fill(
        dem, **_one_line(line_segments={"P09": segs}, max_lines_per_order=1),
        cip_windows={"P09": [(400, 406)]})
    assert sorted((r["order_id"], r["start_hour"], r["end_hour"]) for r in rows) == [
        ("A-W3", 406, 504), ("B-W2", 380, 395)]


# ═══════════════════════════════════════════════════════════════════════════
# new rows: line choice, lateness, max lines, minimum run, qty_max
# ═══════════════════════════════════════════════════════════════════════════
def _two_lines(**kw):
    base = dict(rates={(ln, s): 1000.0 for ln in ("P09", "P10") for s in "BCX"},
                setups={}, line_ids={"P09": 0, "P10": 1},
                initial_sku={"P09": "", "P10": ""}, horizon_h=504.0,
                min_run_hours=8, co_cost={"B": {"C": 100.0}, "C": {"B": 100.0},
                                          "X": {"B": 100.0}})
    base.update(kw)
    return base


def test_prebuild_new_row_takes_the_cheapest_transition_and_ends_at_the_due_week():
    """Both lines are blocked from h288. B-W2 cannot run in its own week, so
    the pass opens a new row before it: P09 already runs B (free), P10 runs
    C (100) -> P09, ending exactly at h288."""
    segs = {ln: free_segments(0, [(288, 504)], 504.0) for ln in ("P09", "P10")}
    dem = [_dem("B-W1", "B", 120, 287, 20000, 1, qmax=20000),
           _dem("C-W1", "C", 120, 287, 20000, 1, qmax=20000),
           _dem("B-W2", "B", 288, 455, 30000, 2, qmax=30000)]
    rows, summary = build_greedy_fill(dem, line_segments=segs, **_two_lines())
    by = {r["order_id"]: r for r in rows}
    assert (by["B-W1"]["line_name"], by["C-W1"]["line_name"]) == ("P09", "P10")
    assert (by["B-W2"]["line_name"], by["B-W2"]["start_hour"],
            by["B-W2"]["end_hour"]) == ("P09", 258, 288)
    assert summary["prebuild"]["new_rows"] == 1


def test_prebuild_prefers_the_nearer_week_step_over_a_free_transition():
    """P09 (holding B) is only free before h100 — B-W2 there would be two
    week-steps early; P10 (holding X, X->B costs 100) is free up to h288,
    one step early. The model prices early kg per step: P10 wins."""
    segs = {"P09": free_segments(0, [(100, 504)], 504.0),
            "P10": free_segments(0, [(288, 504)], 504.0)}
    rows, _ = build_greedy_fill(
        [_dem("B-W2", "B", 288, 455, 30000, 2, qmax=30000)], line_segments=segs,
        **_two_lines(initial_sku={"P09": "B", "P10": "X"}))
    assert [(r["line_name"], r["start_hour"], r["end_hour"]) for r in rows] == [
        ("P10", 258, 288)]


def test_prebuild_respects_max_lines_minimum_run_and_qty_max():
    """(1) X-W3 already runs on both allowed lines: the pre-build extends
    those two rows and never opens a third line. (2) qty_max holds at the
    model's integer rate: 1,000.4 -> 1,000 kg/h, qty_max 60,000 -> the row
    grows from 48 h to exactly 60 h. (3) a 7 h hole in front of the due week
    on another line hosts no row (never a short row)."""
    three = ("P09", "P10", "P11")
    segs = {ln: free_segments(0, [(250, 400)], 504.0) for ln in three}
    rows, _ = build_greedy_fill(
        [_dem("X-W3", "X", 456, 503, 200000, 3, qmin=0)],
        rates={(ln, "X"): 1000.4 for ln in three}, setups={}, line_segments=segs,
        line_ids={ln: i for i, ln in enumerate(three)}, initial_sku={},
        horizon_h=504.0, min_run_hours=8, max_lines_per_order=2)
    assert {r["line_name"] for r in rows} == {"P09", "P10"}
    assert min(r["start_hour"] for r in rows) < 456             # pre-built
    assert sum(1000 * r["run_hours"] for r in rows) <= 220000

    rows, _ = build_greedy_fill(
        [_dem("X-W3", "X", 456, 503, 200000, 3, qmin=0, qmax=60000)],
        rates={("P09", "X"): 1000.4}, setups={}, line_segments={"P09": segs["P09"]},
        line_ids={"P09": 0}, initial_sku={}, horizon_h=504.0, min_run_hours=8)
    assert [(r["start_hour"], r["end_hour"], r["run_hours"]) for r in rows] == [(444, 504, 60)]

    rows, _ = build_greedy_fill(
        [_dem("Y-W3", "X", 456, 503, 100000, 3)],
        rates={("P09", "X"): 1000.0, ("P10", "X"): 1000.0}, setups={},
        line_segments={"P09": [(460.0, 504.0)], "P10": [(449.0, 456.0)]},
        line_ids={"P09": 0, "P10": 1}, initial_sku={}, horizon_h=504.0,
        min_run_hours=8)
    assert [(r["line_name"], r["start_hour"], r["end_hour"]) for r in rows] == [
        ("P09", 460, 504)]


def test_prebuild_only_adds_to_what_the_in_window_pass_placed():
    """Every in-window row survives: same line and order, same end, never a
    later start; total kg never drops."""
    segs = {ln: free_segments(20, [(150, 156), (300, 306)], 504.0)
            for ln in ("P09", "P10")}
    dem = [_dem("B-W1", "B", 168, 335, 60000, 1),
           _dem("C-W2", "C", 336, 503, 400000, 2),
           _dem("X-W2", "X", 336, 503, 200000, 2)]
    kw = dict(line_segments=segs, cip_windows={ln: [(150, 156), (300, 306)]
                                                for ln in ("P09", "P10")},
              **_two_lines(setups={"B": {"C": 2.0}, "C": {"X": 3.0}}))
    old, _ = build_greedy_fill(dem, prebuild=False, **kw)
    new, _ = build_greedy_fill(dem, **kw)
    for r in old:
        mine = [n for n in new if (n["line_name"], n["order_id"]) == (r["line_name"], r["order_id"])]
        assert mine, r
        assert max(n["end_hour"] for n in mine) == r["end_hour"]
        assert min(n["start_hour"] for n in mine) <= r["start_hour"]
    assert sum(r["qty_kg"] for r in new) > sum(r["qty_kg"] for r in old)


# ═══════════════════════════════════════════════════════════════════════════
# in-window REMAINDER before the pre-build (review 2026-09-16,
# PREBUILD-NEARER-WEEK-STEAL): a nearer week keeps its own window hours
# ═══════════════════════════════════════════════════════════════════════════
def _steal_board():
    """One SKU at 1 t/h. P09 is free only in [288, 400), P10 only in
    [420, 504). A-W2 (150 t, pct 0.9/1.1 -> seed share floor 68 h) due
    [288, 455]; A-W3 a partial-week prebuild row (blank pct, pro-rated
    qty_min 21,840) due [456, 503]. The main pass puts A-W2 on P09 [288, 400)
    (38 t short: P10's 36 in-window hours are under its share floor) and
    A-W3 on P10 [456, 504). Without the remainder pass the pre-build then
    extended A-W3 back over [420, 456) — A-W2's own week, same line, same
    SKU — and A-W2 stayed 38 t short."""
    segs = {"P09": free_segments(0, [(0, 288), (400, 504)], 504.0),
            "P10": free_segments(0, [(0, 420)], 504.0)}
    dem = [
        {"order_id": "A-W2", "sku": "A", "week_index": 2, "qty_target": 150000.0,
         "lower_pct": 0.9, "upper_pct": 1.1, "due_start_hour": 288.0,
         "due_end_hour": 455.0},
        _dem("A-W3", "A", 456, 503, 84000, 3, qmin=21840, qmax=92400),
    ]
    kw = dict(rates={("P09", "A"): 1000.0, ("P10", "A"): 1000.0}, setups={},
              line_segments=segs, line_ids={"P09": 0, "P10": 1},
              initial_sku={"P09": "", "P10": ""}, horizon_h=504.0, min_run_hours=8,
              min_run_pct=0.5, max_lines_per_order=2)
    return dem, kw


def test_remainder_gives_the_nearer_week_its_window_before_a_later_week_prebuilds():
    dem, kw = _steal_board()
    for prebuild in (True, False):
        rows, summary = build_greedy_fill(dem, prebuild=prebuild, **kw)
        got = sorted((r["order_id"], r["line_name"], r["start_hour"], r["end_hour"])
                     for r in rows)
        assert got == [("A-W2", "P09", 288, 400), ("A-W2", "P10", 420, 456),
                       ("A-W3", "P10", 456, 504)], (prebuild, got)
        assert summary["remainder"] == {"runs": 1, "kg": 36000}
        assert summary["prebuild"]["runs"] == 0
        assert summary["orders_short"] == []
    # hard demand keeps the seed's share floor in the remainder pass too: the
    # 36 h slot stays under A-W2's 68 h floor, and the pre-build takes it
    rows, summary = build_greedy_fill(dem, soft_demand=False, **kw)
    assert summary["remainder"] == {"runs": 0, "kg": 0}
    assert _span(rows, "A-W3") == (420, 504)


def test_remainder_seed_on_the_steal_board_is_model_feasible(tmp_path):
    """The two-order board through the level-0 model (soft demand, one line
    per row, 8 h floor): the fixed-hint anchor accepts the seed whole."""
    dem, kw = _steal_board()
    rows, _ = build_greedy_fill(dem, **kw)
    P = tsa._f_params(horizon_h=504, min_run_hours=8)
    orders = [dict(order_id="A-W2", sku="A", due_start=288, due_end=455,
                   qty_min=135000, qty_max=165000, qty_target=150000, priority=3),
              dict(order_id="A-W3", sku="A", due_start=456, due_end=503,
                   qty_min=21840, qty_max=92400, qty_target=84000, priority=3)]
    downtimes = [{"line_id": 0, "start": 0, "end": 288, "reason": "Committed PRODUCTION 1"},
                 {"line_id": 0, "start": 400, "end": 504, "reason": "Committed PRODUCTION 2"},
                 {"line_id": 1, "start": 0, "end": 420, "reason": "Committed PRODUCTION 3"}]
    data = tsa._mk_data(P, [0, 1], ["A"], orders, downtimes=downtimes)
    pd.DataFrame(rows).to_csv(tmp_path / "prev_schedule.csv", index=False)
    model, vd = build_model(P, data, "full", False, False,
                            maximize_production=True, objective_mode="balanced")
    apply_warm_start(model, vd, data, P.horizon_h, tmp_path)
    rec = p2._anchor_warm_start_hint(model, vd, data, P, 30.0, level=0)
    assert rec["status"] in ("OPTIMAL", "FEASIBLE"), rec
    assert rec["installed"] is True
    assert rec["seed_placed_kg"] == 112000 + 36000 + 48000


def test_remainder_reaches_week0_orders_the_prebuild_never_sees():
    """W0 orders have model floor == due start, so the pre-build skips them;
    only the remainder pass can finish them. A-W0 (100 t, pct 0.9 -> share
    floor 45 h) fills P09 [0, 70); P10's 30 h are under the share floor in
    the main pass and are taken in the remainder — before B-W1 pre-builds
    back over W0 hours on P10 (it still gets its 28 t, from h140)."""
    segs = {"P09": [(0.0, 70.0)], "P10": [(0.0, 200.0)]}
    dem = [{"order_id": "A-W0", "sku": "A", "week_index": 0, "qty_target": 100000.0,
            "lower_pct": 0.9, "upper_pct": 1.1, "due_start_hour": 0.0,
            "due_end_hour": 167.0},
           _dem("B-W1", "B", 168, 335, 60000, 1, qmin=15000, qmax=60000)]
    kw = dict(rates={(ln, s): 1000.0 for ln in ("P09", "P10") for s in "AB"},
              setups={}, line_segments=segs, line_ids={"P09": 0, "P10": 1},
              initial_sku={}, horizon_h=504.0, min_run_hours=8)
    rows, summary = build_greedy_fill(dem, **kw)
    got = sorted((r["order_id"], r["line_name"], r["start_hour"], r["end_hour"])
                 for r in rows)
    assert got == [("A-W0", "P09", 0, 70), ("A-W0", "P10", 0, 30),
                   ("B-W1", "P10", 140, 200)], got
    assert summary["remainder"]["runs"] == 1
    assert summary["orders_short"] == []
    rows, summary = build_greedy_fill(dem, soft_demand=False, **kw)
    assert ("A-W0", "P10") not in {(r["order_id"], r["line_name"]) for r in rows}
    assert summary["remainder"]["runs"] == 0


def test_remainder_never_adds_a_second_row_on_a_line_the_order_uses():
    """One line, W0 window split by a committed PRODUCTION window [50, 60)
    (no clean -> no seg_b). A-W0 (200 t, share floor 90 h) takes [60, 167+1)
    in the main pass; the remainder must NOT put [0, 50) on the same line
    (two rows around a non-CIP window are not one model assignment), and
    max_lines_per_order holds."""
    segs = {"P09": free_segments(0, [(50, 60)], 504.0)}
    dem = [{"order_id": "A-W0", "sku": "A", "week_index": 0, "qty_target": 200000.0,
            "lower_pct": 0.9, "upper_pct": 1.1, "due_start_hour": 0.0,
            "due_end_hour": 167.0}]
    rows, summary = build_greedy_fill(
        dem, rates={("P09", "A"): 1000.0}, setups={}, line_segments=segs,
        line_ids={"P09": 0}, initial_sku={}, horizon_h=504.0, min_run_hours=8)
    assert [(r["start_hour"], r["end_hour"]) for r in rows] == [(60, 168)]
    assert summary["remainder"]["runs"] == 0
    segs2 = {ln: free_segments(0, [(50, 60)], 504.0) for ln in ("P09", "P10", "P11")}
    dem2 = [{**dem[0], "qty_target": 400000.0}]
    rows, _ = build_greedy_fill(
        dem2, rates={(ln, "A"): 1000.0 for ln in segs2}, setups={}, line_segments=segs2,
        line_ids={ln: i for i, ln in enumerate(segs2)}, initial_sku={}, horizon_h=504.0,
        min_run_hours=8, max_lines_per_order=2)
    assert len({r["line_name"] for r in rows}) == 2
    assert len(rows) == 2


# ═══════════════════════════════════════════════════════════════════════════
# property: random boards -> seed -> the model's fixed-hint anchor accepts it
# ═══════════════════════════════════════════════════════════════════════════
N_BOARDS = 12


def _random_board(seed: int, *, qmin_share: float = 0.25):
    rng = random.Random(seed)
    H = 336
    skus = ["A", "B", "C", "D"]
    lines = list(range(rng.choice([2, 3])))
    avail = {l: rng.choice([0, 10, 30, 55]) for l in lines}
    init = {l: rng.choice(skus + ["CLEAN"]) for l in lines}
    long_extra = {l: 2 for l in lines if rng.random() < 0.3}
    setup = {(a, b): rng.choice([0, 1, 2, 3]) for a in skus for b in skus if a != b}
    downtimes = []
    for l in lines:
        t = rng.choice([60, 90, 110, 150, 160])
        while t + 6 <= H:
            downtimes.append({"line_id": l, "start": t, "end": t + 6,
                              "reason": "Committed CIP"})
            t += rng.choice([100, 120, 140])
        if rng.random() < 0.5:
            s = rng.choice([180, 220, 240])
            downtimes.append({"line_id": l, "start": s, "end": s + rng.choice([12, 40]),
                              "reason": "Committed PRODUCTION 1"})
    orders, dem = [], []
    for i in range(rng.choice([5, 6, 7])):
        wk = 0 if i == 0 else rng.choice([1, 1, 1, 1, 0])
        ds, de = (0, 167) if wk == 0 else (168, 335)
        target = rng.choice([12000, 30000, 60000, 110000, 160000])
        sku = rng.choice(skus)
        oid = f"O{i}-W{wk}"
        qmin, qmax = int(target * qmin_share), int(math.ceil(target * 1.1))
        es = 150 if (wk == 1 and rng.random() < 0.25) else None
        orders.append(dict(order_id=oid, sku=sku, due_start=ds, due_end=de,
                           qty_min=qmin, qty_max=qmax, qty_target=target,
                           priority=3, earliest_start=es))
        dem.append(_dem(oid, sku, ds, de, target, wk, qmin=qmin, qmax=qmax,
                        earliest_start_hour=(NAN if es is None else float(es))))
    return H, skus, lines, avail, init, long_extra, setup, downtimes, orders, dem


@pytest.mark.parametrize("seed", range(N_BOARDS))
@pytest.mark.parametrize("qmin_share", [0.25, 0.9])
def test_random_boards_seed_with_prebuild_is_model_feasible(tmp_path, seed, qmin_share):
    """qmin_share 0.9 (a pct-banded board) makes the seed's share floor bind
    in the main pass, so the in-window REMAINDER pass places rows too."""
    H, skus, lines, avail, init, long_extra, setup, downtimes, orders, dem = _random_board(
        seed, qmin_share=qmin_share)
    P = tsa._f_params(horizon_h=H, min_run_hours=8)
    data = tsa._mk_data(P, lines, skus, orders, avail=avail, init=init, setup=setup,
                        long_extra=long_extra, downtimes=downtimes)
    names = {l: f"L{l}" for l in lines}
    blocked = {names[l]: [] for l in lines}
    cipw = {names[l]: [] for l in lines}
    for d in downtimes:
        blocked[names[d["line_id"]]].append((d["start"], d["end"]))
        if "cip" in d["reason"].lower():
            cipw[names[d["line_id"]]].append((d["start"], d["end"]))
    nested: dict = {}
    for (a, b), h in setup.items():
        nested.setdefault(a, {})[b] = float(h)
    rows, summary = build_greedy_fill(
        dem, rates={(names[l], s): 1000.0 for l in lines for s in skus}, setups=nested,
        line_segments={names[l]: free_segments(avail[l], blocked[names[l]], float(H))
                       for l in lines},
        line_ids={names[l]: l for l in lines},
        initial_sku={names[l]: init[l] for l in lines},
        horizon_h=float(H), min_run_hours=8, max_lines_per_order=2,
        line_gates={names[l]: float(avail[l]) for l in lines},
        initial_extra_hours={names[l]: float(long_extra.get(l, 0)) for l in lines},
        cip_windows=cipw)
    if not rows:
        pytest.skip("nothing placeable on this board")
    pd.DataFrame(rows).to_csv(tmp_path / "prev_schedule.csv", index=False)
    model, vd = build_model(P, data, "full", False, False,
                            maximize_production=True, objective_mode="balanced")
    apply_warm_start(model, vd, data, P.horizon_h, tmp_path)
    rec = p2._anchor_warm_start_hint(model, vd, data, P, 30.0, level=0)
    assert rec["status"] in ("OPTIMAL", "FEASIBLE"), (seed, qmin_share, rec, rows)
    assert rec["installed"] is True
    assert rec["seed_placed_kg"] == sum(1000 * r["run_hours"] for r in rows)


def test_random_boards_do_pre_build():
    """The property above is only worth something if the boards exercise
    the passes: across the boards pre-build must place runs of every
    kind the model can represent, and the banded boards must place
    in-window remainder rows."""
    kinds = {"extended": 0, "cip_pairs": 0, "new_rows": 0}
    remainder = {0.25: 0, 0.9: 0}
    for seed, qmin_share in ((s, q) for s in range(N_BOARDS) for q in remainder):
        H, skus, lines, avail, init, long_extra, setup, downtimes, orders, dem = _random_board(
            seed, qmin_share=qmin_share)
        names = {l: f"L{l}" for l in lines}
        blocked = {names[l]: [(d["start"], d["end"]) for d in downtimes if d["line_id"] == l]
                   for l in lines}
        cipw = {names[l]: [(d["start"], d["end"]) for d in downtimes
                           if d["line_id"] == l and "CIP" in d["reason"]] for l in lines}
        nested: dict = {}
        for (a, b), h in setup.items():
            nested.setdefault(a, {})[b] = float(h)
        _, summary = build_greedy_fill(
            dem, rates={(names[l], s): 1000.0 for l in lines for s in skus}, setups=nested,
            line_segments={names[l]: free_segments(avail[l], blocked[names[l]], float(H))
                           for l in lines},
            line_ids={names[l]: l for l in lines},
            initial_sku={names[l]: init[l] for l in lines}, horizon_h=float(H),
            min_run_hours=8, line_gates={names[l]: float(avail[l]) for l in lines},
            initial_extra_hours={names[l]: float(long_extra.get(l, 0)) for l in lines},
            cip_windows=cipw)
        for k in kinds:
            kinds[k] += summary["prebuild"][k]
        remainder[qmin_share] += summary["remainder"]["runs"]
    assert all(v > 0 for v in kinds.values()), kinds
    assert remainder[0.9] > 0, remainder


# ═══════════════════════════════════════════════════════════════════════════
# end to end: staged work dir -> _greedy_seed -> loader -> model -> anchor
# ═══════════════════════════════════════════════════════════════════════════
def _prebuild_work(out: Path) -> Path:
    """tsa's 2-line board (L1 gate 10 holding A, L2 gate 20 holding B with a
    +3 h long shutdown, horizon 336) re-staged for the pre-build:
      L1: committed CIP [150, 156) + committed production [156, 168)
      L2: committed production [200, 336); L2 cannot run A
      D-W1 (A, 200 t): in-window L1 [168, 336) = 168 t; the rest must go in
          front of the CIP as seg_a (the production window blocks an
          extension) -> [118, 150) + [168, 336)
      E-W1 (C, 50 t): in-window L2 [168, 200) = 32 t; extension back to 150
    """
    work = tsa._build_work(out)
    cap = ["line_id,sku,line_name,capable,calc_rate_kgph"]
    for lid, ln in ((0, "L1"), (1, "L2")):
        for s in ("A", "B", "C"):
            cap.append(f"{lid},{s},{ln},{0 if (ln, s) == ('L2', 'A') else 1},1000.0")
    (work / "capabilities_rates.csv").write_text("\n".join(cap) + "\n", encoding="utf-8")
    (work / "demand_plan.csv").write_text(
        "order_id,sku,week_index,qty_target,lower_pct,upper_pct,due_start_hour,"
        "due_end_hour,priority,qty_min,qty_max\n"
        "B-W0,B,0,40000,0.9,1.1,0,167,3,,\n"
        "C-W0,C,0,30000,0.9,1.1,0,167,3,,\n"
        "D-W1,A,1,200000,0.9,1.1,168,335,3,,\n"
        "E-W1,C,1,50000,,,168,335,3,12000,50000\n", encoding="utf-8")
    (work / "downtimes.csv").write_text(
        "line_id,line_name,start_hour,end_hour,reason\n"
        "0,L1,0,10,Committed PRODUCTION 1\n0,L1,150,156,Committed CIP\n"
        "0,L1,156,168,Committed PRODUCTION 3\n"
        "1,L2,0,20,Committed PRODUCTION 2\n1,L2,200,336,Committed PRODUCTION 4\n",
        encoding="utf-8")
    return work


def _anchor_work(work: Path):
    import tomllib
    cfg = tomllib.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    sched = cfg["scheduler"]
    P = p2.params_from_config(cfg, max_lines_override=sched["max_lines_per_order"],
                              min_run_override=sched["min_run_hours"], allow_week1=True)
    data = Data(P, Files(work))
    data.load()
    model, vd = build_model(P, data, "full", False, False,
                            max_lines_per_order_override=sched["max_lines_per_order"],
                            maximize_production=True, objective_mode="balanced")
    notes = apply_warm_start(model, vd, data, P.horizon_h, work)
    rec = p2._anchor_warm_start_hint(model, vd, data, P, 30.0, level=0)
    return P, data, rec, notes


def test_staged_prebuild_seed_extends_pairs_and_anchors(tmp_path):
    from helpers import scenario_runner as sr

    work = _prebuild_work(tmp_path / "work")
    notes = sr._greedy_seed(work)
    assert any("pre-build" in n and "1 seg_a/seg_b pair" in n for n in notes), notes
    seed = pd.read_csv(work / "prev_schedule.csv")
    got = sorted((r.order_id, r.line_name, int(r.start_hour), int(r.end_hour))
                 for r in seed.itertuples())
    assert ("D-W1", "L1", 118, 150) in got and ("D-W1", "L1", 168, 336) in got
    assert ("E-W1", "L2", 150, 200) in got
    assert (seed["run_hours"] >= 8).all()

    P, data, rec, ws_notes = _anchor_work(work)
    assert any("1 CIP-split" in n for n in ws_notes), ws_notes
    assert rec["status"] in ("OPTIMAL", "FEASIBLE"), rec
    assert rec["installed"] is True
    model_kg = sum(int(round(data.rate[(int(r.line_id), str(r.sku))])) * int(r.run_hours)
                   for r in seed.itertuples())
    assert rec["seed_placed_kg"] == model_kg


def test_staged_seed_honours_the_toml_early_fill_hours(tmp_path):
    """early_fill_hours = 24: no D-W1 / E-W1 hour before h144; the 6 h left
    in front of the CIP host no seg_a; the model (same toml) accepts it."""
    from helpers import scenario_runner as sr

    work = _prebuild_work(tmp_path / "work")
    tsa._toml_edit(work, 'early_fill_hours = "unbounded"', "early_fill_hours = 24")
    sr._greedy_seed(work)
    seed = pd.read_csv(work / "prev_schedule.csv")
    w1 = seed[seed["order_id"].isin(["D-W1", "E-W1"])]
    assert (w1["start_hour"] >= 168 - 24).all(), w1
    assert len(seed[seed["order_id"] == "D-W1"]) == 1
    P, data, rec, _ = _anchor_work(work)
    assert P.early_fill_hours == 24
    assert rec["installed"] is True, rec


def test_two_phase_staging_seeds_without_prebuild(tmp_path):
    """A two-phase solve's sub-models forbid early fill: _stage_warm_start
    turns the pre-build off there and keeps it for single-phase F."""
    from helpers import scenario_runner as sr

    work = _prebuild_work(tmp_path / "work")
    sr._stage_warm_start(work, {"two_phase": True})
    seed = pd.read_csv(work / "prev_schedule.csv")
    ds = {"B-W0": 0, "C-W0": 0, "D-W1": 168, "E-W1": 168}
    assert all(int(r.start_hour) >= ds[r.order_id] for r in seed.itertuples())
    sr._stage_warm_start(work, {"two_phase": False, "fill_mode": True})
    seed = pd.read_csv(work / "prev_schedule.csv")
    assert any(int(r.start_hour) < ds[r.order_id] for r in seed.itertuples())


# ═══════════════════════════════════════════════════════════════════════════
# TASK B — a saved proposal records seed_anchor and min_run_too_small
# ═══════════════════════════════════════════════════════════════════════════
def test_saved_version_keeps_seed_anchor_and_min_run_too_small(tmp_path, monkeypatch):
    from helpers import calendar_io as cio
    from helpers import scenario_runner as sr
    from helpers import version_manager as vm

    dd = tmp_path / "data"
    (dd / "versions").mkdir(parents=True)
    cols = ["block_id", "block_type", "line_id", "line_name", "start_h", "end_h", "label",
            "order_id", "sku", "sku_description", "qty_kg", "locked", "attrs"]
    board = pd.DataFrame([dict(zip(cols, ("b1", "production", 0, "P09", 0, 10, "S", "O1",
                                          "S", "", 1000, False, "")))], columns=cols)
    cio.save_calendar(board, dd / "calendar_blocks.csv")
    monkeypatch.setattr(vm, "planning_anchor", lambda *a, **k: datetime(2026, 9, 16))
    seed_anchor = {"ran": True, "level": 0, "status": "OPTIMAL", "seconds": 2.04,
                   "installed": True, "seed_placed_kg": 4743979,
                   "pass1_first_placed_kg": 4743979}
    too_small = [{"order_id": "280563-W0", "sku": "280563", "qty_target": 62,
                  "qty_max": 3062, "reason": "qty_max", "best_line": "P15",
                  "best_line_min_run_kg": 5040, "min_run_hours": 8}]
    result = {"ok": True, "calendar": board, "scorecard": {"composite": 1.0},
              "feasibility": {"relax_level": 0, "status": "FEASIBLE", "junk": 1,
                              "seed_anchor": seed_anchor,
                              "min_run_too_small": too_small},
              "started_at": "2026-09-16T09:23:00"}
    scenario = {"id": "F", "name": "F", "objective": "balanced", "intent": "t",
                "fill_mode": True}
    res = sr.save_scenario_version(scenario, result, dd)
    meta = json.loads((dd / "versions" / res["slug"] / "metadata.json").read_text(encoding="utf-8"))
    feas = meta["feasibility"]
    assert feas["seed_anchor"] == seed_anchor
    assert feas["min_run_too_small"] == too_small
    assert "junk" not in feas
