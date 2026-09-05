# tests/test_fix_SB.py — regression tests for the SB fix set (solver model:
# CIP and week/time semantics), forensic audit 2026-09-03.
#
# Every expected value below is derived BY HAND in the comment above the
# assertion. Models are tiny (1 line, <= 5 orders) and solve in well under a
# second; CP-SAT runs with 2 workers and a <= 15 s cap (CPU discipline).
#
# Fix index (ids: scratchpad/audit round2_dump/SUMMARY.md, bench/FINDINGS.md):
#   SB-1  C19 / time-2 / modelcore-2 / F2 / F3  demand-derived week frame,
#         week_dev priced in every mode, legacy one-sided stitch off by
#         default. Its "48 h early fill for the SECOND week only" floor was
#         SUPERSEDED by the plant decision of 2026-09-04 ("early fill can go
#         as far into the previous weeks as needed, except cannot be placed
#         before an already scheduled MO"): Params.early_fill_hours, None =
#         unbounded (default), int = allowance for EVERY demand order. The
#         SB-1 tests below are re-derived for that policy; the bounded cases
#         use early_fill_hours = 48 / 0 explicitly. tests/test_early_fill_policy.py
#         carries the policy's own tests (gate, committed windows, Scenario E).
#   SB-2  C44 / cip-4   CIP slots = ceil((H + carry)/interval) + 1
#   SB-3  C43 / cip-3   wall-clock CIP trigger + deadline, gate warning,
#         pre-anchor last_cip_end_datetime
#   SB-4  C42 / cip-2   cip_defer reward retired, per-CIP cost
#   SB-5  C46 / cip-7   seg_b may follow a FIXED committed CIP window
#   SB-6  C50 / cip-11  week-1 initial states: wall-clock carry, line clamp
#   SB-7  C51 / cip-12  idle identity counts only CIPs inside the span

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code" / "solver"))

from ortools.sat.python import cp_model  # noqa: E402

from data_loader import Data, Files, Params  # noqa: E402
from model_builder import (  # noqa: E402
    EARLY_FILL_HOURS,
    _producible_kg_in_window,
    build_model,
    effective_due_start,
    week_frame,
)


# ── Harness ───────────────────────────────────────────────────────────────

def params(**kw) -> Params:
    """Balanced hard-demand model with the live objective weights (makespan
    6, changeover 120) and idle 0 unless a test says otherwise."""
    base = dict(
        horizon_h=336, min_run_hours=1, min_run_pct_of_qty=0.0,
        max_lines_per_order=1, allow_week1_in_week0=True,
        objective_makespan_weight=6, objective_changeover_weight=120,
        objective_cip_defer_weight=5, objective_idle_weight=0,
        objective_late_weight=200, objective_week_deviation_weight=40,
        co_topload_weight=450, co_ttp_weight=5, co_ffs_weight=600,
        co_casepacker_weight=20, co_base_weight=5, co_conv_org_weight=30,
        co_cinn_weight=20, co_flavor_weight=5, co_cip_req_weight=150,
        cip_interval_h=120, cip_duration_h=6,
        planning_start_date="2026-09-07 00:00:00",
    )
    base.update(kw)
    return Params(**base)


def mk_data(P, lines, skus, orders, *, interval=120, carry=None, avail=None,
            init=None, last_cip_dt=None, downtimes=None, setup=None, mc=None,
            rate=1000.0):
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = list(lines)
    d.line_names = {l: f"L{l}" for l in lines}
    for l in lines:
        for s in skus:
            d.capable[(l, s)] = 1
            d.rate[(l, s)] = rate
    carry = carry or {}
    avail = avail or {}
    init = init or {}
    last_cip_dt = last_cip_dt or {}
    d.init_map = {l: {"available_from": avail.get(l, 0),
                      "initial_sku": init.get(l, "CLEAN"),
                      "carryover_run_hours": carry.get(l, 0),
                      "long_shutdown_flag": 0, "long_shutdown_extra": 0,
                      "last_cip_end_datetime": last_cip_dt.get(l, "")}
                  for l in lines}
    d.downtimes = list(downtimes or [])
    d.cip_interval_map = ({l: interval for l in lines}
                          if isinstance(interval, int) else dict(interval))
    d.setup = dict(setup or {})
    d.machine_changes = dict(mc or {})
    d.orders = [dict(o) for o in orders]
    return d


def exact(oid, sku, ds, de, kg):
    """Hard-demand order with qty_min == qty_max (a fixed run length)."""
    return dict(order_id=oid, sku=sku, due_start=ds, due_end=de,
                qty_min=kg, qty_max=kg, qty_target=kg, priority=3)


def solve(model, tl=15):
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 2
    s.parameters.max_time_in_seconds = tl
    st = s.Solve(model)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    return s


def status_of(model, tl=15):
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 2
    s.parameters.max_time_in_seconds = tl
    return s, s.Solve(model)


def cips(s, v, l=0):
    """Present CIP windows on line l as (start, end)."""
    return sorted((s.Value(cs), s.Value(ce)) for cs, ce, b in v["cip_vars"].get(l, [])
                  if s.Value(b))


def block(s, v, l, o_idx):
    """(seg_a_start, seg_a_end, seg_b_present, seg_b_start, seg_b_end, run_h)."""
    k = (l, o_idx)
    return (s.Value(v["seg_a_start"][k]), s.Value(v["seg_a_end"][k]),
            bool(s.Value(v["seg_b_present"][k])), s.Value(v["seg_b_start"][k]),
            s.Value(v["seg_b_end"][k]), s.Value(v["run_h"][k]))


def _val(s, expr):
    return expr if isinstance(expr, int) else s.Value(expr)


# Wednesday-anchored staged demand (the live Scenario F frame): W0 = 0-119,
# W1 = 120-287, W2 = 288-455, W3 = 456-503 (48 h of the last week in a 504 h
# horizon).
WED = dict(W0=(0, 119), W1=(120, 287), W2=(288, 455), W3=(456, 503))


def wed_orders(kg=10_000):
    return [exact(f"A-{w}", "A", ds, de, kg) for w, (ds, de) in WED.items()]


# ═══════════════════════════════════════════════════════════════════════════
# SB-1  week frame (C19 / time-2 / modelcore-2, bench F2 + F3)
# ═══════════════════════════════════════════════════════════════════════════

def test_week_frame_is_derived_from_the_demand_windows():
    """Hand: distinct due_starts of the demand orders are 0, 120, 288, 456;
    the first week's due_end is 119; the SECOND week starts at 120. A
    current MO (due 130-200) and a trial are NOT part of the frame."""
    orders = wed_orders() + [
        dict(exact("MO1|CUR", "A", 130, 200, 5000), is_current_mo=True),
        dict(exact("T1", "A", 300, 320, 1000), is_trial=True, trial_line=0),
    ]
    f = week_frame(orders)
    assert f["starts"] == [0, 120, 288, 456]
    assert f["first_end"] == 119
    assert f["second_start"] == 120
    # single week -> no early-fill week at all
    assert week_frame(wed_orders()[:1])["second_start"] is None


def test_effective_due_start_follows_the_early_fill_policy():
    """Plant decision 2026-09-04 (supersedes SB-1's second-week-only 48 h).
    Default Params.early_fill_hours = None = UNBOUNDED: every demand order's
    due-window start floor is 0 (the gate / committed work bound it):
      W0 (ds 0) -> 0, W1 (ds 120) -> 0, W2 (ds 288) -> 0, W3 (ds 456) -> 0.
    early_fill_hours = 48 (the legacy value, now for EVERY later week):
      W0 -> 0, W1 -> 72, W2 -> 240, W3 -> 408.
    early_fill_hours = 0 -> every order at its own due_start.
    allow_week1_in_week0 off -> due_start whatever the knob. Current MOs
    keep their own start (plant fact). Monday frame under 48:
      [0, 168, 336] -> [0, 120, 288] (120 = the legacy WEEK0_FILL_START)."""
    assert EARLY_FILL_HOURS == 48   # documented legacy value, still exported
    orders = wed_orders()
    f = week_frame(orders)
    P = params(allow_week1_in_week0=True)
    assert P.early_fill_hours is None
    assert [effective_due_start(P, o, f) for o in orders] == [0, 0, 0, 0]
    P48 = params(allow_week1_in_week0=True, early_fill_hours=48)
    assert [effective_due_start(P48, o, f) for o in orders] == [0, 72, 240, 408]
    P0 = params(allow_week1_in_week0=True, early_fill_hours=0)
    assert [effective_due_start(P0, o, f) for o in orders] == [0, 120, 288, 456]
    P_off = params(allow_week1_in_week0=False)
    assert [effective_due_start(P_off, o, f) for o in orders] == [0, 120, 288, 456]
    mo = dict(exact("MO|CUR", "A", 120, 287, 5000), is_current_mo=True)
    assert effective_due_start(P, mo, f) == 120
    assert effective_due_start(P48, mo, f) == 120
    mon = [exact("A-W0", "A", 0, 167, 1), exact("A-W1", "A", 168, 335, 1),
           exact("A-W2", "A", 336, 503, 1)]
    fm = week_frame(mon)
    assert [effective_due_start(P48, o, fm) for o in mon] == [0, 120, 288]
    assert [effective_due_start(P, o, fm) for o in mon] == [0, 0, 0]


def test_producible_bound_uses_the_same_start_floor():
    """1 line at 1,000 kg/h, H 504, Wednesday frame. The bound's window
    starts at effective_due_start, so it follows the policy (2026-09-04),
    and ENDS at effective_due_end: due_end + 1 under due_week_policy = hard
    (the shipped default since 2026-09-04 night), the horizon under the
    OPT-IN soft policy (2026-09-04 #2 -- a demand order may finish late
    inside the horizon, priced per kg-week, so its provable window is
    [ds_eff, H)). The hard values are asserted with the policy both spelled
    out and left at its default; the soft cases opt in explicitly.
    Hard end wall, unbounded early fill: every window opens at 0 ->
      W0 [0,120) 120,000; W1 [0,288) 288,000; W2 [0,456) 456,000; W3 [0,504) 504,000.
    Hard end wall, early_fill_hours = 48 (every week 48 h early):
      W1 [72,288) 216,000; W2 [240,456) 216,000; W3 [408,504) 96,000.
    Soft weeks (opt-in), unbounded: every window is [0, 504) -> 504,000 each;
    soft + 48 h: [72,504) 432,000; [240,504) 264,000; [408,504) 96,000."""
    P = params(horizon_h=504, due_week_policy="hard")
    orders = wed_orders(kg=1_000_000)
    d = mk_data(P, [0], ["A"], orders, interval=100000)
    got = {o["order_id"]: _producible_kg_in_window(P, d, o, d.lines) for o in orders}
    assert got == {"A-W0": 120_000, "A-W1": 288_000, "A-W2": 456_000, "A-W3": 504_000}
    Pd = params(horizon_h=504)                   # the key left at its default
    assert Pd.due_week_policy == "hard"
    d = mk_data(Pd, [0], ["A"], orders, interval=100000)
    got = {o["order_id"]: _producible_kg_in_window(Pd, d, o, d.lines) for o in orders}
    assert got == {"A-W0": 120_000, "A-W1": 288_000, "A-W2": 456_000, "A-W3": 504_000}
    P48 = params(horizon_h=504, early_fill_hours=48, due_week_policy="hard")
    d = mk_data(P48, [0], ["A"], orders, interval=100000)
    got = {o["order_id"]: _producible_kg_in_window(P48, d, o, d.lines) for o in orders}
    assert got == {"A-W0": 120_000, "A-W1": 216_000, "A-W2": 216_000, "A-W3": 96_000}
    Ps = params(horizon_h=504, due_week_policy="soft")
    d = mk_data(Ps, [0], ["A"], orders, interval=100000)
    got = {o["order_id"]: _producible_kg_in_window(Ps, d, o, d.lines) for o in orders}
    assert got == {"A-W0": 504_000, "A-W1": 504_000, "A-W2": 504_000, "A-W3": 504_000}
    Ps48 = params(horizon_h=504, early_fill_hours=48, due_week_policy="soft")
    d = mk_data(Ps48, [0], ["A"], orders, interval=100000)
    got = {o["order_id"]: _producible_kg_in_window(Ps48, d, o, d.lines) for o in orders}
    assert got == {"A-W0": 504_000, "A-W1": 432_000, "A-W2": 264_000, "A-W3": 96_000}


def test_early_fill_reaches_back_as_far_as_needed_but_no_further_than_priced():
    """Wednesday frame, 1 line at 1,000 kg/h, H 504, CIP stand-down.
    Plant decision 2026-09-04 (unbounded early fill, default Params).

    (a) a W1 order of 216,000 kg = 216 h in the 168 h window [120, 288) must
        start by 72. The floor no longer stops it there, the PRICE does:
        every hour before 120 costs 40 and every hour of makespan 6, so
        with the end wall at 288 the latest start wins -> exactly [72, 288),
        early deviation 120 - 72 = 48 h (same block as under SB-1, for a
        different reason).
    (b) a W2 order asking 169 h in its 168 h window [288, 456): the third
        week may now reach back too -> it runs [287, 456), 169 h, produced
        169,000 (the producible clamp, same ds_eff = 0, no longer cuts it),
        early deviation 1 h and priced (week_dev exists for it).
    (c) the SB-1 behaviour is a knob away: early_fill_hours = 0 pins W2 to
        its own due_start -> the clamp floors it at 168,000, the run is
        [288, 456), and pinning the start at 287 is INFEASIBLE (the hard
        end wall -- the shipped default, spelled out here for clarity).
        Under the OPT-IN soft policy (2026-09-04 #2) the same knob keeps the
        start at 288 but the 169th hour is allowed and priced (1 h late)
        instead of clamped: [288, 457), 169,000 produced, lateness 1 h,
        1,000 kg-weeks late."""
    P = params(horizon_h=504)
    orders = [exact("A-W0", "A", 0, 119, 1000), exact("A-W1", "A", 120, 287, 216_000)]
    d = mk_data(P, [0], ["A"], orders, interval=100000)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    sa_s, sa_e, sb, _, _, run = block(s, v, 0, 1)
    assert (sa_s, sa_e, sb, run) == (72, 288, False, 216)
    early, late = v["week_dev"][(0, 1)]
    assert s.Value(early) == 48 and late == 0

    orders = [exact("A-W0", "A", 0, 119, 1000), exact("A-W1", "A", 120, 287, 1000),
              exact("A-W2", "A", 288, 455, 169_000)]
    d = mk_data(P, [0], ["A"], orders, interval=100000)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    sa_s, sa_e, sb, _, _, run = block(s, v, 0, 2)
    assert (sa_s, sa_e, sb, run) == (287, 456, False, 169)
    assert s.Value(v["produced"][2]) == 169_000
    early, late = v["week_dev"][(0, 2)]
    assert s.Value(early) == 1 and late == 0

    P0 = params(horizon_h=504, early_fill_hours=0, due_week_policy="hard")
    d = mk_data(P0, [0], ["A"], orders, interval=100000)
    m, v = build_model(P0, d, "full", False, False)
    s = solve(m)
    sa_s, sa_e, sb, _, _, run = block(s, v, 0, 2)
    assert (sa_s, sa_e, sb, run) == (288, 456, False, 168)
    assert s.Value(v["produced"][2]) == 168_000
    assert (0, 2) not in v["week_dev"]  # no early-fill window at all
    m, v = build_model(P0, d, "full", False, False)
    m.Add(v["seg_a_start"][(0, 2)] == 287)
    _, st = status_of(m)
    assert st == cp_model.INFEASIBLE
    # soft due weeks (opt-in): same start floor, the end is priced not walled
    P0s = params(horizon_h=504, early_fill_hours=0, due_week_policy="soft")
    d = mk_data(P0s, [0], ["A"], orders, interval=100000)
    m, v = build_model(P0s, d, "full", False, False)
    s = solve(m)
    sa_s, sa_e, sb, _, _, run = block(s, v, 0, 2)
    assert (sa_s, sa_e, sb, run) == (288, 457, False, 169)
    assert s.Value(v["produced"][2]) == 169_000
    assert s.Value(v["lateness"][(0, 2)]) == 1
    assert s.Value(v["late_kg_weeks"]) == 1_000


def test_early_fill_is_priced_per_hour_in_the_default_mode():
    """Monday frame, 1 line, balanced hard demand, idle weight 0, same SKU
    (no changeover terms). A-W0 10 h [0,167]; A-W1 20 h [168,335]. Under the
    unbounded policy (2026-09-04) A-W1 may start anywhere from hour 0 -- but
    every hour before 168 costs objective_week_deviation_weight = 40 while
    an hour of makespan saves only 6, so the early start never pays:
      start 168: makespan 188 x 6 = 1,128 + 0                    -> 1,128 <- optimum
      start 10 : makespan  30 x 6 =   180 + early 158 x 40 = 6,320 -> 6,500
    With the weight at 0 the early start is free and makespan decides: the
    two runs pack from hour 0 (A-W1 at 0 or at 10 -- an exact tie), makespan
    30 -> 180. This is the "right week first" preference the plant kept.
    UPDATED 2026-09-04 (decision #2): the kg-week price early_kg_week_weight
    (v2 default 50,000 per tonne-week = 50 per kg-week in the pass-2
    currency; in this balanced branch rescaled by W2 / (100 x K) = 120 /
    3,300 -> 2 per kg) sits ON TOP of the per-hour term -- under BOTH
    due-week policies, so also under the shipped default "hard" -- so
    week_deviation_weight = 0 alone no longer frees the early start
    (20,000 kg x 2 = 40,000 >> the 6/h makespan saving): A-W1 stays at 168.
    Both prices at 0 -> the old free move, objective 180. The default case is
    unchanged (start 168 has no early kg, objective 1,128)."""
    orders = [exact("A-W0", "A", 0, 167, 10_000), exact("A-W1", "A", 168, 335, 20_000)]
    P = params()
    d = mk_data(P, [0], ["A"], orders, interval=100000)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    assert block(s, v, 0, 1)[0] == 168
    assert s.ObjectiveValue() == 1128

    P0 = params(objective_week_deviation_weight=0)
    d = mk_data(P0, [0], ["A"], orders, interval=100000)
    m, v = build_model(P0, d, "full", False, False)
    s = solve(m)
    assert block(s, v, 0, 1)[0] == 168  # the kg-week price alone keeps the week
    assert s.ObjectiveValue() == 1128

    P00 = params(objective_week_deviation_weight=0, early_kg_week_weight=0)
    d = mk_data(P00, [0], ["A"], orders, interval=100000)
    m, v = build_model(P00, d, "full", False, False)
    s = solve(m)
    assert block(s, v, 0, 1)[0] in (0, 10)
    assert s.ObjectiveValue() == 180


def test_legacy_week_stitch_is_off_by_default_and_opt_in():
    """Monday frame, 1 line, A-W0 44 h [0,167], A-W1 40 h [168,335] (bench B1
    geometry). The legacy one-sided stitch says first_w1_start - last_w0_end
    <= 1 with first_w1_start >= 120, i.e. last_w0_end >= 119, so a 44 h W0
    run could not START before 75 (bench B1: both lines idle to h75-76).
    Default: pinning A-W0 to start at the gate (hour 0) is FEASIBLE.
    Params.legacy_week_stitch = True: the same pin is INFEASIBLE — with the
    legacy 48 h floor (early_fill_hours = 48 -> W1 opens at 120) that the
    stitch presumes. Under the plant's unbounded policy (2026-09-04) W1 may
    open at 0, so the stitch alone would be satisfiable ([0,44) + [44,84));
    the legacy stitch only ever made sense together with the legacy floor,
    which is why the opt-in case sets the knob explicitly."""
    orders = [exact("A-W0", "A", 0, 167, 44_000), exact("A-W1", "A", 168, 335, 40_000)]
    P = params()
    d = mk_data(P, [0], ["A"], orders, interval=100000)
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["seg_a_start"][(0, 0)] == 0)
    _, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    P_legacy = params(early_fill_hours=48)
    P_legacy.legacy_week_stitch = True   # read with getattr, default False
    d = mk_data(P_legacy, [0], ["A"], orders, interval=100000)
    m, v = build_model(P_legacy, d, "full", False, False)
    m.Add(v["seg_a_start"][(0, 0)] == 0)
    _, st = status_of(m)
    assert st == cp_model.INFEASIBLE


# ═══════════════════════════════════════════════════════════════════════════
# SB-2  CIP slots scale with the horizon (C44 / cip-4)
# ═══════════════════════════════════════════════════════════════════════════

def test_cip_slot_count_follows_the_horizon():
    """H 504, interval 100, carry 50, five 90 h orders (450 h, five SKUs,
    setup 0) on one line, hard demand. Hand:
      slots = ceil((504 + 50) / 100) + 1 = 7 (old: 3);
      with 3 slots the chain c1s <= 50, c2s <= 56 + 100, c3s <= 162 + 100
      caps production at last_end <= 268 + 100 = 368 -> 350 h -> INFEASIBLE;
      wall clock at the last end: 450 h + 5 x 6 h = 480 -> 480 + 50 = 530
      >= 5 x 100 and < 6 x 100 -> exactly 5 cleans; 480 <= 504 fits.
    Every order produces its 90,000 kg."""
    P = params(horizon_h=504)
    skus = [f"S{i}" for i in range(5)]
    orders = [exact(f"O{i}", s, 0, 503, 90_000) for i, s in enumerate(skus)]
    setup = {(a, b): 0 for a in skus for b in skus if a != b}
    d = mk_data(P, [0], skus, orders, interval=100, carry={0: 50}, setup=setup)
    m, v = build_model(P, d, "full", False, False)
    assert len(v["cip_vars"][0]) == 7
    s = solve(m)
    assert len(cips(s, v)) == 5
    assert sum(s.Value(v["run_h"][(0, i)]) for i in range(5)) == 450
    assert all(s.Value(v["produced"][i]) == 90_000 for i in range(5))
    # every gap between cleans and the tail respect the 100 h interval
    win = cips(s, v)
    assert win[0][0] <= 50
    for (_, e_prev), (s_next, _) in zip(win, win[1:]):
        assert s_next - e_prev <= 100
    last_end = max(block(s, v, 0, i)[4] or block(s, v, 0, i)[1] for i in range(5))
    assert last_end - win[-1][1] <= 100


# ═══════════════════════════════════════════════════════════════════════════
# SB-3  wall-clock CIP semantics (C43 / cip-3)
# ═══════════════════════════════════════════════════════════════════════════

def test_line_already_past_its_interval_gets_the_cip_at_the_gate_with_a_warning():
    """carry 100, interval 120, gate 60, one 30 h order (audit exp1b). Plant:
    the clean was due at hour 120 - 100 = 20, before the line is even free;
    the model pins it AT the gate: CIP [60, 66), then the run [66, 96)
    (makespan 96). Wall clock at the last end 96 + 100 = 196 >= 120 -> one
    clean, < 240 -> not two. Old model: deadline avail + remaining = 80, CIP
    anywhere in [60, 80] and no warning."""
    P = params()
    d = mk_data(P, [0], ["A"], [exact("A", "A", 0, 335, 30_000)],
                interval=120, carry={0: 100}, avail={0: 60})
    m, v = build_model(P, d, "full", False, False)
    assert len(v["warnings"]) == 1
    assert "already past its CIP interval at the gate" in v["warnings"][0]
    s = solve(m)
    assert cips(s, v) == [(60, 66)]
    assert block(s, v, 0, 0)[:2] == (66, 96)
    assert s.Value(v["run_h"][(0, 0)]) == 30


def test_committed_and_idle_hours_count_on_the_cip_clock():
    """carry 0, interval 120, one 99 h order whose window opens at hour 100
    (audit exp1d/e). Old model: span 99 < 120 -> no CIP, the line ran dirty
    to wall-clock hour 199. Plant: the clock started at t0, so a clean is due
    by hour 120 -> exactly one CIP starting <= 120, production still 99 h,
    and the tail after the clean is <= 120 h. (early_fill_hours = 0 keeps the
    window opening at 100 as the geometry needs; under the plant's unbounded
    early-fill policy of 2026-09-04 the order would simply run [0, 99) and
    need no clean — a different, equally valid, plan.)"""
    P = params(early_fill_hours=0)
    d = mk_data(P, [0], ["A"], [exact("A", "A", 100, 335, 99_000)], interval=120)
    m, v = build_model(P, d, "full", False, False)
    assert v["warnings"] == []
    s = solve(m)
    win = cips(s, v)
    assert len(win) == 1 and win[0][0] <= 120
    sa_s, sa_e, sb, sb_s, sb_e, run = block(s, v, 0, 0)
    assert run == 99 and sa_s >= 100
    last_end = sb_e if sb else sa_e
    assert last_end - win[0][1] <= 120


def test_pre_anchor_last_cip_datetime_is_the_dirtier_source():
    """planning_start 2026-09-07 00:00, last_cip_end_datetime 2026-09-02
    20:00 = hour -100 (a clean BEFORE the anchor; the old `0 <= hour < H`
    guard ignored it), carry column 0 (inconsistent on purpose), interval
    120, one 30 h order. The dirtier reading wins: carry_eff = 100 -> the
    clean is due by hour 20 and the run's end 30 + 100 >= 120 triggers it:
    exactly one CIP with start <= 20. With the datetime blank the same
    model needs no CIP (30 < 120)."""
    P = params()
    d = mk_data(P, [0], ["A"], [exact("A", "A", 0, 335, 30_000)], interval=120,
                last_cip_dt={0: "2026-09-02 20:00:00"})
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    win = cips(s, v)
    assert len(win) == 1 and win[0][0] <= 20

    d = mk_data(P, [0], ["A"], [exact("A", "A", 0, 335, 30_000)], interval=120)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    assert cips(s, v) == []


# ═══════════════════════════════════════════════════════════════════════════
# SB-4  cip_defer reward retired, per-CIP cost (C42 / cip-2, bench F6)
# ═══════════════════════════════════════════════════════════════════════════

def test_short_run_needs_no_cip_exp6():
    """Audit exp6: one 30 h order, carry 0, interval 120, live weights
    (makespan 6, changeover 120, cip_defer 5, idle 0). Old model: the defer
    REWARD paid 5/h for a later CIP start, so the solver split the run
    0-4 / 214-240 to stretch the clock across 120 and 240, placed TWO cleans
    and reported a NEGATIVE objective (-390). Now: 30 < 120 -> no clean, run
    [0, 30), objective = makespan 30 x 6 = 180, cip cost 0."""
    P = params()
    d = mk_data(P, [0], ["A"], [exact("A", "A", 0, 335, 30_000)], interval=120)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    assert cips(s, v) == []
    sa_s, sa_e, sb, _, _, run = block(s, v, 0, 0)
    # (seg_b_start / seg_b_end are unconstrained when seg_b is absent)
    assert (sa_s, sa_e, sb, run) == (0, 30, False, 30)
    assert s.ObjectiveValue() == 180
    assert _val(s, v["cip_cost"]) == 0


def test_cip_still_placed_between_a_conv_org_pair_when_due_exp5():
    """Audit exp5: A (S1, 70 h) then B (S2, 70 h), S1->S2 is a conv->org
    change (setup 1 h, cost base 5 + conv_org 30 = 35), interval 120, carry
    0, weights makespan 6 / changeover 120. 140 h of work >= 120 -> one clean
    is mandatory; placed between A and B it absorbs the conv->org penalty
    (refund 30) and its 6 h cover the 1 h setup:
      A [0, 70) -> CIP [70, 76) -> B [76, 146): makespan 146.
    Objective = 146 x 6 + (35 - 30) x 120 + CIP cost
              = 876 + 600 + 6 h x 1,000 kg/h x 1,000 = 6,001,476.
    Old model: B pushed to 170 and a second phantom CIP at 246 (defer
    reward). The per-CIP cost is exposed as vars['cip_cost']."""
    P = params()
    mc = {("S1", "S2"): {"ttp": 0, "ffs": 0, "topload": 0, "casepacker": 0,
                         "conv_to_org": 1, "cinn_to_non": 0, "added_flavors": 0,
                         "cip_req_after": 0}}
    orders = [exact("A", "S1", 0, 335, 70_000), exact("B", "S2", 0, 335, 70_000)]
    d = mk_data(P, [0], ["S1", "S2"], orders, interval=120,
                setup={("S1", "S2"): 1, ("S2", "S1"): 1}, mc=mc)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    assert cips(s, v) == [(70, 76)]
    assert block(s, v, 0, 0)[:2] == (0, 70)
    assert block(s, v, 0, 1)[:2] == (76, 146)
    assert _val(s, v["cip_cost"]) == 6_000_000
    assert s.ObjectiveValue() == 6_001_476


def test_cip_defer_weight_no_longer_reaches_the_model():
    """Retired knob: doubling objective_cip_defer_weight (and cip_flex) must
    leave the model byte-identical. Mirrors test_solver_weights'
    test_retired_cip_defer_knobs_are_inert on the exp5 geometry, where a
    CIP is actually present."""
    orders = [exact("A", "S1", 0, 335, 70_000), exact("B", "S2", 0, 335, 70_000)]

    def proto(P, **kw):
        d = mk_data(P, [0], ["S1", "S2"], orders, interval=120)
        m, _ = build_model(P, d, "full", False, False, **kw)
        return str(m.Proto())

    assert proto(params(objective_cip_defer_weight=5)) == proto(params(objective_cip_defer_weight=10))
    assert (proto(params(objective_cip_flex_weight=20), cip_flex=True)
            == proto(params(objective_cip_flex_weight=40), cip_flex=True))


# ═══════════════════════════════════════════════════════════════════════════
# SB-5  seg_b may follow a FIXED committed CIP window (C46 / cip-7)
# ═══════════════════════════════════════════════════════════════════════════

def test_fill_order_spans_a_committed_cip_window_in_stand_down():
    """Scenario F stand-down (interval 100,000 -> no solver CIP), H 300, a
    committed CIP window [100, 106) on the line, one 200 h order. The free
    gaps are 100 h and 194 h, so 200 h fits neither whole: the order must
    split around the clean -- seg_a ends <= 100, seg_b starts >= 106, run
    200 h (produced 200,000). Old model: seg_b needed a SOLVER CIP, so this
    was INFEASIBLE (audit exp4: fill capped at the largest gap). Control: the
    same window with reason 'Maintenance' is not a clean -> INFEASIBLE."""
    P = params(horizon_h=300)
    order = [exact("BIG", "A", 0, 299, 200_000)]
    d = mk_data(P, [0], ["A"], order, interval=100000,
                downtimes=[dict(line_id=0, start=100, end=106, reason="Committed CIP")])
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    sa_s, sa_e, sb, sb_s, sb_e, run = block(s, v, 0, 0)
    assert sb and run == 200 and sa_e <= 100 and sb_s >= 106
    assert s.Value(v["produced"][0]) == 200_000
    assert cips(s, v) == []

    d = mk_data(P, [0], ["A"], order, interval=100000,
                downtimes=[dict(line_id=0, start=100, end=106, reason="Maintenance")])
    m, v = build_model(P, d, "full", False, False)
    _, st = status_of(m)
    assert st == cp_model.INFEASIBLE


# ═══════════════════════════════════════════════════════════════════════════
# SB-6  week-1 initial states carry (C50 / cip-11)
# ═══════════════════════════════════════════════════════════════════════════

def test_week1_states_keep_the_wall_clock_carry_and_clamp_to_the_line_interval(tmp_path):
    """phase2_scheduler.write_week1_initial_states on 144 h lines (anchor
    2026-09-07 00:00). Hand:
      L0: pre-horizon carry 100, produced [0, 40), no CIP -> carry stays 100
          (old: replaced by the 40 run hours -> pre-horizon dirty time lost);
      L1: pre-horizon carry 150, nothing produced -> clamp to the LINE's
          interval - 1 = 143 (old: hard-coded 119);
      L2: CIP [50, 56) then production to 120 -> the clean is 56 h AFTER t0,
          carry = -56 and last_cip_end_datetime = anchor + 56 h =
          2026-09-09 08:00:00 (old: 64 run hours since the clean).
    available_from (set_available_from_schedule=True) = last end, or the
    later CIP end: L0 40, L1 0 (original gate 0), L2 120."""
    import phase2_scheduler as p2

    P = params(horizon_h=336)
    d = mk_data(P, [0, 1, 2], ["A"], [], interval=144,
                carry={0: 100, 1: 150, 2: 20})
    rows = [
        dict(line_id=0, start_hour=0, end_hour=40, run_hours=40, sku="A"),
        dict(line_id=2, start_hour=0, end_hour=50, run_hours=50, sku="A"),
        dict(line_id=2, start_hour=56, end_hour=120, run_hours=64, sku="A"),
    ]
    cip_rows = [dict(line_id=2, start_hour=50, end_hour=56)]
    p2.write_week1_initial_states(rows, cip_rows, d, P, tmp_path,
                                  set_available_from_schedule=True)
    out = pd.read_csv(tmp_path / "week1_initial_states.csv", keep_default_na=False)
    by = {int(r.line_id): r for r in out.itertuples()}
    assert int(by[0].carryover_run_hours_since_last_cip_at_t0) == 100
    assert int(by[1].carryover_run_hours_since_last_cip_at_t0) == 143
    assert int(by[2].carryover_run_hours_since_last_cip_at_t0) == -56
    assert by[2].last_cip_end_datetime == "2026-09-09 08:00:00"
    assert [int(by[l].available_from_hour) for l in (0, 1, 2)] == [40, 0, 120]
    # ...and model_builder reads the negative carry as "clean at hour 56":
    # a 30 h order gated at 120 ends at 150; 150 - 56 = 94 < 144 -> no CIP.
    d2 = mk_data(P, [2], ["A"], [exact("A", "A", 0, 335, 30_000)], interval=144,
                 carry={2: -56}, avail={2: 120})
    m, v = build_model(P, d2, "full", False, False)
    s = solve(m)
    assert cips(s, v, l=2) == [] and v["warnings"] == []


# ═══════════════════════════════════════════════════════════════════════════
# SB-7  idle identity counts only CIPs inside the span (C51 / cip-12)
# ═══════════════════════════════════════════════════════════════════════════

def test_trailing_cip_does_not_force_phantom_idle():
    """carry 90, interval 120, one 30 h order, idle weight 3, makespan 6.
    The run [0, 30) ends with the clock at 30 + 90 = 120 -> a clean is due by
    hour 120 - 90 = 30, i.e. it may TRAIL the run: CIP [30, 36).
      trailing: makespan 30 x 6 = 180, idle = span 30 - prod 30 - inside 0 = 0
                -> 180 + 6,000,000 cip cost = 6,000,180  <- optimum
      inside  : run [0,14) CIP [14,20) run [20,36): makespan 36 x 6 = 216,
                idle 36 - 30 - 6 = 0 -> 6,000,216
    Old identity idle = span - prod - 6 x (every present CIP) went NEGATIVE
    for the trailing clean (30 - 30 - 6), so the solver was forced into the
    dearer inside/leading placement (audit exp3b: 81 h of forced idle)."""
    P = params(objective_idle_weight=3)
    d = mk_data(P, [0], ["A"], [exact("A", "A", 0, 335, 30_000)], interval=120,
                carry={0: 90})
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    sa_s, sa_e, sb, _, _, run = block(s, v, 0, 0)
    # (seg_b_start / seg_b_end are unconstrained when seg_b is absent)
    assert (sa_s, sa_e, sb, run) == (0, 30, False, 30)
    assert cips(s, v) == [(30, 36)]
    assert s.Value(v["line_idle"][0]) == 0
    assert s.ObjectiveValue() == 6_000_180


def test_leading_cip_at_the_gate_has_no_forced_idle_exp3b():
    """Audit exp3b: carry 119, interval 120, one 40 h order, idle weight 3.
    The clean is due by hour 120 - 119 = 1 (>= gate 0, no warning), so it
    LEADS the run: CIP [0, 6) then [6, 46). Span from the gate 46, prod 40,
    CIP inside 6 -> idle 0; makespan 46 x 6 = 276 + 6,000,000 = 6,000,276.
    EXACT TIE (verified by pinning cs == 0 and cs == 1, both OPTIMAL at
    6,000,276): CIP [1, 7) with a 1 h seg_a [0, 1) and seg_b [7, 46) has
    the same makespan 46, the same 0 idle and the same one clean, so CP-SAT
    may return either; the test pins the invariants (clean starts by the
    deadline 1, last end 46, 40 h produced, 0 idle, the objective), not the
    tie-break. Old model at idle_weight 3: run 6-10 + 91-127 with a second
    clean and 81 h of forced idle inside the span."""
    P = params(objective_idle_weight=3)
    d = mk_data(P, [0], ["A"], [exact("A", "A", 0, 335, 40_000)], interval=120,
                carry={0: 119})
    m, v = build_model(P, d, "full", False, False)
    assert v["warnings"] == []
    s = solve(m)
    win = cips(s, v)
    assert len(win) == 1 and win[0][0] <= 1 and win[0][1] == win[0][0] + 6
    sa_s, sa_e, sb, sb_s, sb_e, run = block(s, v, 0, 0)
    assert run == 40 and (sb_e if sb else sa_e) == 46
    assert s.Value(v["line_idle"][0]) == 0
    assert s.ObjectiveValue() == 6_000_276
    # pinned at the gate the optimum is unchanged (the tie, made explicit)
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["cip_vars"][0][0][0] == 0)
    s = solve(m)
    assert cips(s, v) == [(0, 6)]
    assert block(s, v, 0, 0)[:3] == (6, 46, False)
    assert s.ObjectiveValue() == 6_000_276
