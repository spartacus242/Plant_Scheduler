# tests/test_soft_week_policy.py -- the SOFT DUE-WEEK policy (v2 pricing).
#
# OPT-IN since 2026-09-04 night: the shipped default is due_week_policy =
# "hard" (soft measured -38 % on-time kg at 600 s on the live board, see
# data_loader.Params). Every case below opts in through the params() helper;
# tests/test_default_due_week_policy.py pins the shipped default.
#
# Plant decision 2026-09-04 #2 (user, verbatim):
#   "Treat the week numbers on the demand_plan_summary.csv as preferential
#    order, but not necessarily the hard borders. We want to be able to look
#    3-4 weeks into the plan, and if a sku has tons scheduled in week 1 and
#    week 3, and it makes sense to combine all kgs for that sku in one run in
#    week 2, then that should be explored."
#
# The model implements that as a PRICE in both directions, per kg per whole
# 168 h step away from the order's own window (model_builder: WEEK_STEP_H,
# dev_kg_coefficients, early_steps / late_steps):
#
#   late  : Params.late_kg_week_weight  = 200,000 per TONNE per week
#   early : Params.early_kg_week_weight =  50,000 per TONNE per week (1/4)
#
# ONE CURRENCY (v2, 2026-09-04 evening). The weights are fill units per tonne
# per week-step with 1 kg of tier-1 fill = 1,000 units, i.e. "fill-equivalent
# kg" (kg-eq). The invariant is that the price is the same FRACTION of the
# fill value of the same kg in every pass:
#   pass 2 : Minimize co*100*K + ... + dev - prod_score, fill ~ 1,000 per kg,
#            coefficient weight / 1000 per kg-week          -> 200 late / 50 early
#   pass 1 : Maximize prod_sum * 1000 - dev, fill = 1e6 per kg,
#            coefficient weight per kg-week (x1000)         -> 200,000 / 50,000
# so one tonne one week late costs 20 % of its own fill value in both passes,
# one week early 5 %. (v1 used weight / 1000 in both passes: 0.2 % in pass 1,
# 200 % in pass 2 -- pass 1 filled wherever fill was easiest, pass 2 bought
# the deviation back with changeovers.)
#
# Changeover currency: one FFS change in pass 2 costs co_pair x 100 x K with
# K = default_fill_exchange_rate(P) = 33; the FFS pair weighs 605, so one FFS
# change ~ 605 x 100 x 33 = 1,996,500 ~ 2,000 kg-eq. At 200 kg-eq per
# tonne-week, 10 t one week late ~ one FFS change, 30 t ~ three, 100 t ~ ten.
# The consolidation cases below trade against a FULL change (FFS + topload +
# casepacker + ttp = 1,080 -> 3,564,000 = 3,564 kg-eq).
#
# Every expected value below is derived BY HAND in the comment above the
# assertion. Models are tiny (1 line, <= 3 orders); CP-SAT runs with 2
# workers and a <= 15 s cap (CPU discipline).

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code" / "solver"))

from ortools.sat.python import cp_model  # noqa: E402

from data_loader import (Data, Files, Params, parse_due_week_policy,  # noqa: E402
                         parse_kg_week_weight)
from model_builder import (PASS1_FILL_SCALE, WEEK_STEP_H, build_model,  # noqa: E402
                           default_fill_exchange_rate, dev_kg_coefficients,
                           dev_kg_coefficients_pass1, dev_kg_coefficients_pass2,
                           due_week_policy_of, effective_due_end,
                           soft_due_weeks)
import phase2_scheduler as p2  # noqa: E402

# A changeover that touches every machine -- the expensive kind the
# consolidation cases are trading against (FFS pair weight 605 = ffs 600 +
# base 5, plus topload 450 + casepacker 20 + ttp 5 on top = 1,080).
FULL_CHANGE = {"ttp": 1, "ffs": 1, "topload": 1, "casepacker": 1,
               "conv_to_org": 0, "cinn_to_non": 0, "added_flavors": 0,
               "cip_req_after": 0}

# The v1 prices (2026-09-04 morning), kept only to prove the regression test
# discriminates: the same coefficient formula in both passes.
V1_PRICES = dict(late_kg_week_weight=2_000_000, early_kg_week_weight=250_000)

# One FULL change in the pass-2 currency: 1,080 x 100 x K(33).
FULL_CHANGE_PASS2 = 1080 * 100 * 33


# -- Harness ---------------------------------------------------------------

def params(**kw) -> Params:
    """Scenario-F style params (soft demand, live objective weights, 504 h /
    3 weeks) with the soft due-week policy OPTED IN (the shipped default is
    "hard"; pass due_week_policy="hard" to get it) at the opt-in prices."""
    base = dict(
        due_week_policy="soft",
        horizon_h=504, min_run_hours=1, min_run_pct_of_qty=0.0,
        max_lines_per_order=1, allow_week1_in_week0=True,
        objective_makespan_weight=6, objective_changeover_weight=300,
        objective_cip_defer_weight=5, objective_idle_weight=3,
        objective_late_weight=200, objective_week_deviation_weight=40,
        co_topload_weight=450, co_ttp_weight=5, co_ffs_weight=600,
        co_casepacker_weight=20, co_base_weight=5, co_conv_org_weight=30,
        co_cinn_weight=20, co_flavor_weight=5, co_cip_req_weight=150,
        cip_interval_h=120, cip_duration_h=6, soft_demand=True,
        planning_start_date="2026-09-07 00:00:00",
    )
    base.update(kw)
    return Params(**base)


def mk_data(P, lines, skus, orders, *, avail=None, downtimes=None,
            rate=1000.0, pairs=()):
    """1 kg/h-per-unit flat rate (1000 kg/h), CIP stood down, no setups
    except the `pairs` given (4 h + every machine changed)."""
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = list(lines)
    d.line_names = {l: f"L{l}" for l in lines}
    for l in lines:
        for s in skus:
            d.capable[(l, s)] = 1
            d.rate[(l, s)] = rate
    avail = avail or {}
    d.init_map = {
        l: {"available_from": avail.get(l, 0), "initial_sku": "CLEAN",
            "carryover_run_hours": 0, "long_shutdown_flag": 0,
            "long_shutdown_extra": 0, "last_cip_end_datetime": ""}
        for l in lines
    }
    d.downtimes = list(downtimes or [])
    d.cip_interval_map = {l: 100000 for l in lines}   # CIP stand-down
    d.setup, d.machine_changes = {}, {}
    for a, b in pairs:
        d.setup[(a, b)] = 4
        d.machine_changes[(a, b)] = dict(FULL_CHANGE)
    d.orders = [dict(o) for o in orders]
    return d


def order(oid, sku, ds, de, kg, **kw):
    """An order whose band is a point (min = target = max = kg), so the only
    decision left is WHERE the kg go."""
    o = dict(order_id=oid, sku=sku, due_start=ds, due_end=de,
             qty_min=kg, qty_max=kg, qty_target=kg, priority=3)
    o.update(kw)
    return o


def solve(model, tl=15):
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 2
    s.parameters.max_time_in_seconds = tl
    st = s.Solve(model)
    assert s.StatusName(st) in ("OPTIMAL", "FEASIBLE"), s.StatusName(st)
    return s


def placed(s, v, d):
    """[(order_id, line, start, end, kg)] sorted by line then start."""
    out = []
    for (l, o_idx), pres in v["present"].items():
        if s.Value(pres):
            out.append((d.orders[o_idx]["order_id"], l,
                        s.Value(v["seg_a_start"][(l, o_idx)]),
                        s.Value(v["eff_end"][(l, o_idx)]),
                        s.Value(v["produced"][o_idx])))
    return sorted(out, key=lambda r: (r[1], r[2]))


def val(s, expr):
    return int(expr) if isinstance(expr, int) else s.Value(expr)


def build_pass2(P, d, **kw):
    """The shipped Scenario-F pass 2: fill in the objective at the exchange
    rate K, changeover load minimized."""
    return build_model(P, d, "full", False, False, maximize_production=True,
                       objective_mode="balanced",
                       fill_exchange_rate=default_fill_exchange_rate(P), **kw)


def build_pass1(P, d, **kw):
    return build_model(P, d, "full", False, False, maximize_production=True,
                       **kw)


def floors_from(s, v, d):
    """The shipped pass-1 -> pass-2 hand-over: order index -> min(pass-1
    produced, target)."""
    out = {}
    for i, o in enumerate(d.orders):
        made = s.Value(v["produced"][i])
        fl = min(made, int(o["qty_target"]))
        if fl > 0:
            out[i] = fl
    return out


# -- 1. Configuration + arithmetic -----------------------------------------

def test_policy_parsers_and_defaults():
    """[scheduler] due_week_policy defaults to "hard" (shipped 2026-09-04
    night) and accepts only the two documented values; the weights are whole
    non-negative integers; the opt-in v2 prices are 200,000 late / 50,000
    early per tonne-week and the pass-2 makespan knob defaults to 1
    (unchanged behaviour). The harness params() opts in to "soft"."""
    assert Params.due_week_policy == "hard"
    assert Params().due_week_policy == "hard"
    assert Params.late_kg_week_weight == 200_000
    assert Params.early_kg_week_weight == 50_000
    assert Params.pass2_makespan_weight == 1
    assert parse_due_week_policy(None) == "hard"
    assert parse_due_week_policy("") == "hard"
    assert parse_due_week_policy(" Soft ") == "soft"
    assert parse_due_week_policy(" HARD ") == "hard"
    with pytest.raises(ValueError):
        parse_due_week_policy("wall")
    assert parse_kg_week_weight(None, "late_kg_week_weight", 7) == 7
    assert parse_kg_week_weight("200000", "late_kg_week_weight", 7) == 200_000
    assert parse_kg_week_weight(0, "late_kg_week_weight", 7) == 0
    with pytest.raises(ValueError):
        parse_kg_week_weight(-1, "late_kg_week_weight", 7)
    assert due_week_policy_of(params()) == "soft"          # harness opt-in
    assert due_week_policy_of(Params()) == "hard"          # shipped default
    assert due_week_policy_of(params(due_week_policy="hard")) == "hard"
    # params_from_config wiring of the four [scheduler] keys.
    Pc = p2.params_from_config({"scheduler": {
        "due_week_policy": "soft",
        "late_kg_week_weight": 300000, "early_kg_week_weight": 60000,
        "pass2_makespan_weight": 100000}})
    assert (Pc.due_week_policy, Pc.late_kg_week_weight, Pc.early_kg_week_weight,
            Pc.pass2_makespan_weight) == ("soft", 300_000, 60_000, 100_000)
    assert p2.params_from_config({}).due_week_policy == "hard"
    assert p2.params_from_config({}).pass2_makespan_weight == 1


def test_kg_week_coefficients_per_currency():
    """dev_kg_coefficients returns the objective cost PER KG PER 168 h step
    in the currency of the branch that applies it.

    pass 2 (fill ~ 1,000 per kg): weight / 1000
      late  200,000 / 1000 = 200 per kg-week  -> 200,000 per TONNE-week
      early  50,000 / 1000 =  50 per kg-week  ->  50,000 per TONNE-week
    pass 1 (fill = 1e6 per kg, PASS1_FILL_SCALE = 1000): weight
      late 200,000, early 50,000 per kg-week
    One FFS change in the pass-2 currency is co_pair x 100 x K; here
    K = default_fill_exchange_rate = 33 and the FFS pair weighs 605, so
    605 x 100 x 33 = 1,996,500 ~ 2,000 kg-eq = 10 tonne-weeks late.
    Non-fill branches (min-changeovers, W2 = objective_changeover_weight,
    spread-load) carry no fill term and multiply the changeover load by
    co_multiplier instead of 100 x K, so the coefficient is rescaled by
    co_multiplier / (100 x K):
      late  200 x 300 / 3,300 = 18.2 -> 18 ; early 50 x 300 / 3,300 = 4.5 -> 5."""
    P = params()
    assert default_fill_exchange_rate(P) == 33
    assert PASS1_FILL_SCALE == 1000
    assert dev_kg_coefficients(P) == (50, 200)
    assert dev_kg_coefficients_pass2(P) == (50, 200)
    assert dev_kg_coefficients_pass1(P) == (50_000, 200_000)
    assert dev_kg_coefficients(P, None, 1000) == (50_000, 200_000)
    assert dev_kg_coefficients(P, 300) == (5, 18)
    # A zero weight prices nothing; a positive one never rounds away to 0.
    assert dev_kg_coefficients(params(late_kg_week_weight=0))[1] == 0
    assert dev_kg_coefficients_pass1(params(late_kg_week_weight=0))[1] == 0
    assert dev_kg_coefficients(params(late_kg_week_weight=1), 1)[1] == 1


def test_deviation_is_the_same_fraction_of_fill_in_both_passes():
    """THE INVARIANT (v2 pricing). Pass 1 values one kg of tier-1 fill at
    1,000 x 1,000 = 1e6 objective units; pass 2 at ~1e3. The deviation
    price per kg-week must be the same fraction of that in both:
        pass-1 coefficient / 1e6 == pass-2 coefficient / 1e3
    for BOTH prices -- 200,000 / 1e6 = 200 / 1e3 = 20 % late,
    50,000 / 1e6 = 50 / 1e3 = 5 % early -- at the defaults and at any other
    price. v1 violated this by a factor of 1,000 (weight / 1000 in both).
    The built model exposes both pairs and the pair its branch applied."""
    # (weights below 1,000 hit the "a positive price never rounds to 0"
    # floor in pass 2 -- 1 per kg-week -- so the ratio test uses >= 1,000)
    for kw in ({}, dict(late_kg_week_weight=4_000_000),
               dict(early_kg_week_weight=1_000), V1_PRICES):
        P = params(**kw)
        e1, l1 = dev_kg_coefficients_pass1(P)
        e2, l2 = dev_kg_coefficients_pass2(P)
        assert e1 / 1e6 == pytest.approx(e2 / 1e3)
        assert l1 / 1e6 == pytest.approx(l2 / 1e3)
    P = params()
    assert dev_kg_coefficients_pass1(P) == (50_000, 200_000)
    assert dev_kg_coefficients_pass2(P) == (50, 200)
    o = [order("X-W2", "X", 168, 335, 10_000)]
    _, v1 = build_pass1(P, mk_data(P, [0], ["X"], o))
    assert v1["dev_kg_coefficients"]["pass1"] == (50_000, 200_000)
    assert v1["dev_kg_coefficients"]["pass2"] == (50, 200)
    assert v1["dev_kg_coefficients"]["used"] == (50_000, 200_000)
    _, v2 = build_pass2(P, mk_data(P, [0], ["X"], o))
    assert v2["dev_kg_coefficients"]["used"] == (50, 200)
    # Non-fill balanced branch: rescaled to W2 / (100 K) = 300 / 3,300.
    _, v3 = build_model(P, mk_data(P, [0], ["X"], o), "full", False, False)
    assert v3["dev_kg_coefficients"]["used"] == (5, 18)


def test_effective_due_end_widens_only_for_soft_demand_orders():
    """Soft policy: a demand order may finish anywhere up to the horizon (its
    lateness is priced). Committed MOs and trials keep their hard wall, and
    so does every order under due_week_policy = "hard"."""
    P, Ph = params(), params(due_week_policy="hard")
    o = order("X-W1", "X", 168, 335, 1000)
    assert effective_due_end(P, o) == 504 and soft_due_weeks(P, o)
    assert effective_due_end(Ph, o) == 336 and not soft_due_weeks(Ph, o)
    for pinned in ({"is_current_mo": True}, {"is_trial": True}):
        oc = dict(o, **pinned)
        assert effective_due_end(P, oc) == 336
        assert not soft_due_weeks(P, oc)


# -- 2. Late production always beats no production -------------------------

def test_late_production_beats_no_production():
    """One 100 t order due week 0 [0,167]; the line is blocked for the whole
    of week 0 (a committed window 0..168). 100 t at 1000 kg/h needs 100 h,
    so nothing can be made inside the window.

    Soft: the order runs 168..268 -- 100 h past the wall, all 100 t in the
    FIRST late step (168 <= t < 336), so late_kg_weeks = 100,000 kg-weeks
    priced 100,000 x 200,000 = 2.0e10 in pass 1, against a fill reward of
    1e6 per kg = 1e11 for the tonnage: one week late costs 20 % of the fill,
    so late production is never traded away for no production (it would
    take 5 weeks of lateness to break even; the horizon allows 2).
    Hard: no capable hour exists inside [0,168), the producible clamp takes
    qty_min to 0 and the order is simply not placed."""
    dt = [dict(line_id=0, start=0, end=168, reason="Committed MO")]
    P = params()
    d = mk_data(P, [0], ["X"], [order("X-W0", "X", 0, 167, 100_000)],
                downtimes=dt)
    m, v = build_pass1(P, d)
    s = solve(m)
    rows = placed(s, v, d)
    assert rows == [("X-W0", 0, 168, 268, 100_000)]
    assert val(s, v["late_kg_weeks"]) == 100_000
    assert s.Value(v["lateness"][(0, 0)]) == 100

    Ph = params(due_week_policy="hard")
    dh = mk_data(Ph, [0], ["X"], [order("X-W0", "X", 0, 167, 100_000)],
                 downtimes=dt)
    mh, vh = build_pass1(Ph, dh)
    sh = solve(mh)
    assert placed(sh, vh, dh) == []
    assert val(sh, vh["late_kg_weeks"]) == 0


def test_late_steps_count_whole_weeks_and_straddling_runs():
    """Kg-week lateness is graded in whole 168 h steps from due_end + 1.

    (a) Order due [0,167], line blocked 0..336: the 10 t run starts at 336,
        which is in the SECOND late step (336 = 168 + 168), so every kg
        counts twice -> late_kg_weeks = 2 x 10,000 = 20,000 (priced 40 % of
        the fill in pass 1: 20,000 x 200,000 = 4e9 vs 1e10 -- still made).
    (b) Order due [0,167] for 100 t, line blocked 0..100: the run occupies
        100..200, so 32 h of it (168..200) x 1000 kg/h = 32,000 kg are late
        by one step. The kg made before the wall are not late at all."""
    P = params()
    d = mk_data(P, [0], ["X"], [order("X-W0", "X", 0, 167, 10_000)],
                downtimes=[dict(line_id=0, start=0, end=336, reason="c")])
    m, v = build_pass1(P, d)
    s = solve(m)
    assert placed(s, v, d) == [("X-W0", 0, 336, 346, 10_000)]
    assert val(s, v["late_kg_weeks"]) == 20_000

    d2 = mk_data(P, [0], ["X"], [order("X-W0", "X", 0, 167, 100_000)],
                 downtimes=[dict(line_id=0, start=0, end=100, reason="c")])
    m2, v2 = build_pass1(P, d2)
    s2 = solve(m2)
    assert placed(s2, v2, d2) == [("X-W0", 0, 100, 200, 100_000)]
    assert val(s2, v2["late_kg_weeks"]) == 32_000
    assert s2.Value(v2["late_kg"][(0, 0)]) == 32_000
    assert WEEK_STEP_H == 168


# -- 3. The nearer week wins a contested hour (the 280480 pattern) ---------
#
# Measured on the live problem under the unbounded early-fill policy with
# only the per-hour preference (bench results/policy_unbounded/REPORT.md):
# 280480-W2 took 109 t of week-1 hours while 280480-W1 -- 94.7 t due in week
# 1 -- got NOTHING. Below is that pattern in miniature.

def test_nearer_week_wins_the_contested_hour():
    """Two 100 t orders of ONE SKU, W1 [120,287] and W2 [288,455], one line
    whose only free hours are [120,220) -- capacity for exactly one of them
    (100 h x 1000 kg/h = 100 t).

    Both choices earn the same fill, so the tie is broken by the kg-week
    price: filling W1 in its own week costs 0; filling W2 there instead is
    one whole step early (120..220 all lies before 288 and after 288-168 =
    120) = 100,000 kg-weeks x 50,000 = 5e9 in pass 1, x 50 = 5e6 in pass 2
    -- 5 % of the tonnage's fill value either way. The nearer week wins, in
    pass 1 and in pass 2."""
    orders = [order("X-W1", "X", 120, 287, 100_000),
              order("X-W2", "X", 288, 455, 100_000)]
    dt = [dict(line_id=0, start=0, end=120, reason="c"),
          dict(line_id=0, start=220, end=504, reason="c")]
    P = params()
    for build in (build_pass1, build_pass2):
        d = mk_data(P, [0], ["X"], orders, downtimes=dt)
        m, v = build(P, d)
        s = solve(m)
        assert placed(s, v, d) == [("X-W1", 0, 120, 220, 100_000)]
        assert val(s, v["early_kg_weeks"]) == 0


def test_spare_capacity_still_pre_builds_the_later_week():
    """Same pair, but the line is free for 150 h [120,270). The nearer week
    is served FIRST (100 t in W1's own hours), and the 50 h left over are
    given to the later week rather than idled: 50 t one step early =
    50,000 kg-weeks x 50 = 2.5e6, against 50 t of fill = 5e7 (pass 2).
    Pre-building beats idling 20x -- the policy only orders the two, it
    never forbids the second."""
    orders = [order("X-W1", "X", 120, 287, 100_000),
              order("X-W2", "X", 288, 455, 100_000)]
    dt = [dict(line_id=0, start=0, end=120, reason="c"),
          dict(line_id=0, start=270, end=504, reason="c")]
    P = params()
    d = mk_data(P, [0], ["X"], orders, downtimes=dt)
    m, v = build_pass2(P, d)
    s = solve(m)
    assert placed(s, v, d) == [("X-W1", 0, 120, 220, 100_000),
                               ("X-W2", 0, 220, 270, 50_000)]
    assert val(s, v["early_kg_weeks"]) == 50_000


# -- 4. Consolidation: two runs of one SKU become one campaign -------------
#
# Layout for every case below: one line, X due week 1 [0,167], Y due week 2
# [168,335] for 100 t, X due week 3 [336,503]. X <-> Y is a full changeover
# (setup 4 h; pair weight = base 5 + ttp 5 + ffs 600 + topload 450 +
# casepacker 20 = 1,080). Order floors pin every order's tonnage so the
# question is only WHEN each runs, never whether.
#
#   X, Y, X  (each in its own week) = TWO X<->Y changeovers, co_load 2,160
#   Y, X, X  or  X, X, Y            = ONE, co_load 1,080
# The saving is 1,080 pass-2 units x 100 x K(33) = 3,564,000 = 3,564 kg-eq.
# X-W3 defaults to 100 t so that pulling IT a week early (100,000 kg-weeks x
# 50 = 5,000,000 > 3,564,000) is never the cheaper move; the cases about the
# early direction size it explicitly.

def consolidation_case(x1_kg, x3_kg=100_000, **kw):
    P = params(**kw)
    orders = [order("X-W1", "X", 0, 167, x1_kg),
              order("Y-W2", "Y", 168, 335, 100_000),
              order("X-W3", "X", 336, 503, x3_kg)]
    d = mk_data(P, [0], ["X", "Y"], orders,
                pairs=[("X", "Y"), ("Y", "X")])
    m, v = build_pass2(P, d, order_floors={0: x1_kg, 1: 100_000, 2: x3_kg})
    s = solve(m)
    return s, v, d, placed(s, v, d)


def test_small_week1_order_is_delayed_to_join_the_week3_campaign():
    """X-W1 is ONE tonne, X-W3 is 100 t. Running the tonne in its own week
    costs two changeovers (2,160 -> 7,128,000 pass-2 units); pushing it past
    Y so the sequence is Y, X, X costs one (1,080 -> 3,564,000) plus one
    tonne one week late (1,000 kg-weeks x 200 = 200,000). Consolidating is
    cheaper by 3,564,000 - 200,000 = 3,364,000, so the solver delays the
    tonne -- exactly the move the plant asked for."""
    s, v, d, rows = consolidation_case(1_000)
    by_id = {r[0]: r for r in rows}
    assert set(by_id) == {"X-W1", "Y-W2", "X-W3"}
    # Y first, then both X runs: one changeover instead of two.
    assert by_id["Y-W2"][3] <= by_id["X-W1"][2]
    assert by_id["X-W1"][3] <= by_id["X-W3"][2]
    assert s.Value(v["co_load"]) == 1080
    # exactly one tonne, exactly one week-step late
    assert val(s, v["late_kg_weeks"]) == 1_000
    assert val(s, v["early_kg_weeks"]) == 0


def test_big_week1_order_is_not_delayed():
    """The mirror case: X-W1 is 100 t. The same single changeover saves
    3,564,000, but delaying 100 t by a week costs 100,000 kg-weeks x 200 =
    20,000,000 -- 5.6x the saving (ten FFS changes' worth). Pulling the
    100 t X-W3 a week early instead costs 100,000 x 50 = 5,000,000, also
    more than the saving. The solver keeps every order in its own week and
    pays the second changeover (co_load 2,160)."""
    s, v, d, rows = consolidation_case(100_000)
    by_id = {r[0]: r for r in rows}
    assert by_id["X-W1"][3] <= by_id["Y-W2"][2]      # X first, in week 1
    assert by_id["X-W1"][2] < 168 and by_id["X-W1"][4] == 100_000
    assert s.Value(v["co_load"]) == 2160
    assert val(s, v["late_kg_weeks"]) == 0
    assert val(s, v["early_kg_weeks"]) == 0


def test_small_vs_large_acceptance_criterion():
    """User's acceptance criterion (2026-09-04): a small run (<= 10 t) may
    slip one week to save ONE major changeover; a large order (>= 30 t) must
    NOT move a week for fewer than several.

    10 t X-W1: slipping past Y costs 10,000 kg-weeks x 200 = 2,000,000 and
    saves one full change (3,564,000) -> it slips: Y, X, X; co_load 1,080;
    late_kg_weeks 10,000. (At the default price 10 t-weeks is exactly one
    FFS-only change, 1,996,500; the change saved here is FFS + topload.)
    30 t X-W1: the identical situation costs 30,000 x 200 = 6,000,000 for
    the same 3,564,000 saving -> it stays: X, Y, X; co_load 2,160; nothing
    late. It would take two full changes saved (7,128,000) to move it."""
    s, v, d, rows = consolidation_case(10_000)
    by_id = {r[0]: r for r in rows}
    assert by_id["Y-W2"][3] <= by_id["X-W1"][2]
    assert by_id["X-W1"][3] <= by_id["X-W3"][2]
    assert s.Value(v["co_load"]) == 1080
    assert val(s, v["late_kg_weeks"]) == 10_000
    assert val(s, v["early_kg_weeks"]) == 0

    s, v, d, rows = consolidation_case(30_000)
    by_id = {r[0]: r for r in rows}
    assert by_id["X-W1"][3] <= by_id["Y-W2"][2]
    assert by_id["X-W1"][2] < 168 and by_id["X-W1"][4] == 30_000
    assert s.Value(v["co_load"]) == 2160
    assert val(s, v["late_kg_weeks"]) == 0
    assert val(s, v["early_kg_weeks"]) == 0


def test_small_week3_order_is_pulled_early_into_the_week1_campaign():
    """Consolidation works in the EARLY direction too. X-W1 is 10 t and
    X-W3 is 5 t: pulling the 5 t back next to the 10 t (sequence X, X, Y)
    saves the same 3,564,000 and costs 5,000 kg-weeks x 50 = 250,000 (one
    step early: the run lands inside [336-168, 336) = week 2). Delaying the
    10 t instead would cost 2,000,000. The solver pre-builds, because early
    is 1/4 the price."""
    s, v, d, rows = consolidation_case(10_000, 5_000)
    by_id = {r[0]: r for r in rows}
    assert by_id["X-W1"][3] <= by_id["X-W3"][2]
    assert by_id["X-W3"][3] <= by_id["Y-W2"][2]
    assert s.Value(v["co_load"]) == 1080
    assert val(s, v["late_kg_weeks"]) == 0
    assert val(s, v["early_kg_weeks"]) == 5_000


def test_early_break_even_is_seventy_tonne_weeks_per_full_change():
    """The early price is 1/4 of late, so the early side's break-even against
    one FULL change is 3,564,000 / 50 = 71,280 kg-weeks: a 50 t X-W3 IS
    pulled one week early to join a 100 t X-W1 (50,000 x 50 = 2,500,000 <
    3,564,000; sequence X, X, Y with Y still on time at [222,322]), while a
    100 t X-W3 is not (5,000,000). Documented so the trade is visible: to
    keep big orders from pre-building a week, raise early_kg_week_weight
    (100,000 puts the break-even at 35 t-weeks)."""
    s, v, d, rows = consolidation_case(100_000, 50_000)
    by_id = {r[0]: r for r in rows}
    assert by_id["X-W1"][3] <= by_id["X-W3"][2]
    assert by_id["X-W3"][3] <= by_id["Y-W2"][2]
    assert s.Value(v["co_load"]) == 1080
    assert val(s, v["early_kg_weeks"]) == 50_000
    assert val(s, v["late_kg_weeks"]) == 0

    s, v, d, rows = consolidation_case(100_000, 50_000,
                                       early_kg_week_weight=100_000)
    assert s.Value(v["co_load"]) == 2160
    assert val(s, v["early_kg_weeks"]) == 0


def test_late_weight_knob_switches_the_consolidation_decision():
    """The trade is a PRICE, and the price is a knob. With
    late_kg_week_weight = 4,000,000 the same one-tonne delay costs 1,000 x
    4,000 = 4,000,000 > the 3,564,000 changeover saving, so the identical
    problem is solved the other way: X in its own week, two changeovers,
    nothing late."""
    s, v, d, rows = consolidation_case(1_000, late_kg_week_weight=4_000_000)
    by_id = {r[0]: r for r in rows}
    assert by_id["X-W1"][3] <= by_id["Y-W2"][2]
    assert s.Value(v["co_load"]) == 2160
    assert val(s, v["late_kg_weeks"]) == 0


def test_hard_policy_restores_the_week_wall():
    """due_week_policy = "hard" (the shipped default): the week end is a
    wall, so the one-tonne order cannot leave week 1 whatever the changeover
    saving. Two changeovers, no lateness anywhere."""
    s, v, d, rows = consolidation_case(1_000, due_week_policy="hard")
    by_id = {r[0]: r for r in rows}
    assert by_id["X-W1"][3] <= 168                 # finished inside week 1
    assert s.Value(v["co_load"]) == 2160
    assert val(s, v["late_kg_weeks"]) == 0
    # No lateness variables are created for demand orders under "hard"
    # (relax level 0): the wall is a constraint, not a price.
    assert not v["lateness"]


# -- 5. The v1 pathology: pass 2 buying deviation back with changeovers ----
#
# Measured on live_F at 600 s under v1 (bench results/policy_soft): pass 1
# placed 925 t-weeks early, then pass 2 -- pricing the same kg-week at
# 1,000x the fraction of fill -- unwound it with changeovers: co_load 39,350
# vs 27,570 for the fixed-week solver, utilisation 76.9 vs 83.1 %. Below is
# the mechanism in miniature.

def pathology_case(P, tail_open):
    """One line. X-W1 100 t [0,167]; X-W2 20 t and Y-W2 100 t both due
    [168,335]. Week 2's free hours are [168,268) -- exactly Y's 100 h -- and,
    when `tail_open`, [300,336) as well; week 3 is blocked. X <-> Y is a
    full change (1,080). X-W2 therefore has two homes: EARLY, in week 1's
    idle hours right after X-W1 (same SKU, no changeover; 20,000 kg-weeks
    early), or -- tail open only -- in its own week after Y, behind a
    second changeover."""
    dt = [dict(line_id=0, start=268, end=300 if tail_open else 336, reason="c"),
          dict(line_id=0, start=336, end=504, reason="c")]
    orders = [order("X-W1", "X", 0, 167, 100_000),
              order("X-W2", "X", 168, 335, 20_000),
              order("Y-W2", "Y", 168, 335, 100_000)]
    return mk_data(P, [0], ["X", "Y"], orders, downtimes=dt,
                   pairs=[("X", "Y"), ("Y", "X")])


def test_pass2_does_not_buy_earliness_back_with_a_changeover():
    """(a) Tail closed: X-W2 can only be made early. Pass 1 legitimately
    fills week 1's idle hours with it (20 t of fill = 2e10 >> 20,000
    kg-weeks x 50,000 = 1e9 early), one changeover. Pass 2, handed pass 1's
    floors, keeps the placement: co_load 1,080, early_kg_weeks 20,000 --
    there is nothing to buy.
    (b) Tail open: pass 2 could put X-W2 in its own week at [304,324] behind
    a Y -> X change. The earliness costs 20,000 x 50 = 1,000,000 (1,000
    kg-eq) and the change 3,564,000 (3,564 kg-eq): unwinding is NOT worth
    it, pass 2 keeps X-W2 early -- co_load 1,080, early 20,000. Under the v1
    prices (early 250 per kg-week in pass 2) the same earliness cost
    5,000,000 > 3,564,000 and pass 2 bought the changeover: co_load 2,160,
    nothing early -- the measured pathology. (Pass 1 in (b), fill-first,
    parks X-W2 in its week -- in its currency 1e9 of earliness dwarfs a
    324,000 changeover -- and pass 2 consolidates it; the two-pass result
    is the same plan as (a).)"""
    P = params()
    # (a) tail closed: pass 1 -> pass 2 keeps the early placement
    d = pathology_case(P, tail_open=False)
    m1, v1 = build_pass1(P, d)
    s1 = solve(m1)
    by1 = {r[0]: r for r in placed(s1, v1, d)}
    assert by1["X-W2"][3] <= 168 and by1["X-W2"][4] == 20_000
    assert s1.Value(v1["co_load"]) == 1080
    assert val(s1, v1["early_kg_weeks"]) == 20_000
    m2, v2 = build_pass2(P, d, order_floors=floors_from(s1, v1, d))
    s2 = solve(m2)
    by2 = {r[0]: r for r in placed(s2, v2, d)}
    assert by2["X-W2"][3] <= 168 and by2["X-W2"][4] == 20_000
    assert by2["Y-W2"][2] == 168 and by2["Y-W2"][3] == 268
    assert s2.Value(v2["co_load"]) == 1080
    assert val(s2, v2["early_kg_weeks"]) == 20_000

    # (b) tail open: v2 keeps the earliness, v1 bought a changeover
    d = pathology_case(P, tail_open=True)
    m1, v1 = build_pass1(P, d)
    s1 = solve(m1)
    by1 = {r[0]: r for r in placed(s1, v1, d)}
    assert by1["X-W2"][2] >= 300 and s1.Value(v1["co_load"]) == 2160
    floors = floors_from(s1, v1, d)
    assert floors == {0: 100_000, 1: 20_000, 2: 100_000}
    m2, v2 = build_pass2(P, d, order_floors=floors)
    s2 = solve(m2)
    by2 = {r[0]: r for r in placed(s2, v2, d)}
    assert by2["X-W2"][3] <= 168 and by2["X-W2"][4] == 20_000
    assert s2.Value(v2["co_load"]) == 1080
    assert val(s2, v2["early_kg_weeks"]) == 20_000
    assert val(s2, v2["late_kg_weeks"]) == 0

    Pv1 = params(**V1_PRICES)
    d = pathology_case(Pv1, tail_open=True)
    mv, vv = build_pass2(Pv1, d, order_floors=floors)
    sv = solve(mv)
    byv = {r[0]: r for r in placed(sv, vv, d)}
    assert byv["X-W2"][2] >= 300
    assert sv.Value(vv["co_load"]) == 2160
    assert val(sv, vv["early_kg_weeks"]) == 0


# -- 6. Pass-2 makespan weight ---------------------------------------------

def test_pass2_makespan_weight_is_applied_in_pass2_only():
    """One line at 100 kg/h, one order banded 500..1,000 kg (target 1,000),
    pass-2 floor 500 kg (the shipped per-order floor). In pass 2 each hour
    of run adds 100 kg x 1,000 = 100,000 of fill and costs
    `pass2_makespan_weight` of makespan.
      default 1        : 100,000 >> 1 per hour -> the full 1,000 kg (10 h)
      weight 200,000   : 200,000 > 100,000 per hour -> shaved to the floor
                         500 kg (5 h): 5 x 200,000 - 500,000 = 500,000 beats
                         10 x 200,000 - 1,000,000 = 1,000,000 (without the
                         floor every hour is a loss and the order is dropped)
      weight 100,000   : exactly break-even per hour -> both 500 and 1,000
                         are optimal; the documented meaning "one hour of
                         shorter plan ~ 0.1 t of fill" at a 100 kg/h line.
    Pass 1 ignores the knob (fill 1e6 per kg vs makespan x 6): 1,000 kg."""
    o = [dict(order_id="X", sku="X", due_start=0, due_end=167,
              qty_min=500, qty_max=1000, qty_target=1000, priority=3)]
    P = params()
    d = mk_data(P, [0], ["X"], o, rate=100.0)
    m, v = build_pass2(P, d, order_floors={0: 500})
    s = solve(m)
    assert placed(s, v, d) == [("X", 0, 0, 10, 1000)]

    Pw = params(pass2_makespan_weight=200_000)
    d = mk_data(Pw, [0], ["X"], o, rate=100.0)
    m, v = build_pass2(Pw, d, order_floors={0: 500})
    s = solve(m)
    assert placed(s, v, d) == [("X", 0, 0, 5, 500)]

    m, v = build_pass1(Pw, mk_data(Pw, [0], ["X"], o, rate=100.0))
    s = solve(m)
    assert placed(s, v, d) == [("X", 0, 0, 10, 1000)]


# -- 7. Four-week look-ahead -----------------------------------------------

def test_four_week_horizon_prices_three_early_steps():
    """horizon_weeks = 4 -> horizon_h 672. An order due in week 3 [504,671]
    pinned to hour 0 is three whole steps early (thresholds 504, 336, 168;
    the fourth, 0, is at the gate and is not built), so 10 t x 3 =
    30,000 kg-weeks. The CIP grid follows the longer horizon too:
    ceil(672/120) + 1 = 7 slots."""
    P = params(horizon_h=4 * WEEK_STEP_H)
    d = mk_data(P, [0], ["X"], [order("X-W4", "X", 504, 671, 10_000)])
    d.cip_interval_map = {0: 120}
    m, v = build_pass1(P, d)
    m.Add(v["seg_a_start"][(0, 0)] == 0)
    s = solve(m)
    assert placed(s, v, d) == [("X-W4", 0, 0, 10, 10_000)]
    assert val(s, v["early_kg_weeks"]) == 30_000
    assert len(v["cip_vars"][0]) == 7
