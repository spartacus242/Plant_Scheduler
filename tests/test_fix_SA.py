# tests/test_fix_SA.py — regression tests for the SA fix set (solver model:
# objective, quantities, changeover pricing), forensic audit 2026-09-03.
#
# Every expected value below is derived BY HAND in the comment above the
# assertion. Models are tiny (<= 3 lines, <= 5 orders) and solve in well
# under a second; CP-SAT runs with 2 workers and a 10 s cap (CPU discipline).
#
# Fix index (ids: scratchpad/own/clusters.json, bench/FINDINGS.md):
#   SA-1  C72/F9   same-SKU adjacency costs 0
#   SA-2  C31/F1   initial changeover (initial_sku -> first order) is priced
#   SA-3  C73/F4   pass-2 per-order floors + fill exchange rate
#   SA-4  C36      setup hours rounded UP to whole model hours
#   SA-5  C61      current-MO quantity bracketed at whole hours of the rate
#   SA-6  C63      relax level 3 (ignore_co) keeps setup TIME
#   SA-7  C33      co_cost_l domain bound covers the flavor term
#   SA-8  C65/adv-1/adv-3  producible guards need capable==1, union downtimes
#   SA-9  C62      min_run_pct_of_qty on the clamped qmin; soft demand -> hours only
#   SA-10 F3       idle measured from the availability gate
#   SA-11 adv-3    producible bound respects max_lines_per_order
#   SA-12 adv-4/quality-10/adv-10  demand + capabilities validation, duplicate MOs

from __future__ import annotations

import math
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
    _producible_kg_in_window,
    _usable_hours_on_line,
    build_model,
    default_fill_exchange_rate,
)
from changeover_cache import (  # noqa: E402
    load_changeover_dicts,
    round_half_up,
    round_setup_hours,
)


# ── Harness ───────────────────────────────────────────────────────────────

def f_params(**kw) -> Params:
    """The live Scenario F weights (snapshot F_workdir/flowstate.toml)."""
    base = dict(
        horizon_h=336, min_run_hours=4, min_run_pct_of_qty=0.5,
        max_lines_per_order=2, allow_week1_in_week0=True,
        objective_makespan_weight=6, objective_changeover_weight=300,
        objective_cip_defer_weight=5, objective_idle_weight=3,
        objective_late_weight=200, objective_week_deviation_weight=40,
        co_topload_weight=450, co_ttp_weight=5, co_ffs_weight=600,
        co_casepacker_weight=20, co_base_weight=5, co_conv_org_weight=30,
        co_cinn_weight=20, co_flavor_weight=5, co_cip_req_weight=150,
        cip_interval_h=120, cip_duration_h=6,
    )
    base.update(kw)
    return Params(**base)


# Machine-change rows: TTP-only (cheap) and FULL (every machine).
TTP_ONLY = {"ttp": 1, "ffs": 0, "topload": 0, "casepacker": 0,
            "conv_to_org": 0, "cinn_to_non": 0, "added_flavors": 0,
            "cip_req_after": 0}
FULL = {"ttp": 1, "ffs": 1, "topload": 1, "casepacker": 1,
        "conv_to_org": 0, "cinn_to_non": 0, "added_flavors": 0,
        "cip_req_after": 0}
# Hand values under F weights:
#   FULL     = base 5 + topload 450 + ttp 5 + ffs 600 + casepacker 20 = 1080
#   TTP_ONLY = base 5 + ttp 5                                          =   10
COST_FULL = 1080
COST_TTP = 10


def mk_data(P, lines, skus, orders, init=None, setup=None, mc=None,
            downtimes=None, rate=1000.0, capable=None, avail=None):
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = list(lines)
    d.line_names = {l: f"L{l}" for l in lines}
    for l in lines:
        for s in skus:
            d.capable[(l, s)] = 1
            d.rate[(l, s)] = rate
    if capable:
        d.capable.update(capable)
    init = init or {}
    avail = avail or {}
    d.init_map = {l: {"available_from": avail.get(l, 0),
                      "initial_sku": init.get(l, "CLEAN"),
                      "carryover_run_hours": 0, "long_shutdown_flag": 0,
                      "long_shutdown_extra": 0} for l in lines}
    d.downtimes = list(downtimes or [])
    d.cip_interval_map = {l: 100000 for l in lines}  # F stand-down
    d.setup = dict(setup or {})
    d.machine_changes = dict(mc or {})
    d.orders = [dict(o) for o in orders]
    return d


def order(oid, sku, ds, de, target, lo=0.9, hi=1.1):
    return dict(order_id=oid, sku=sku, due_start=ds, due_end=de,
                qty_min=int(math.floor(target * lo)),
                qty_max=int(math.ceil(target * hi)),
                qty_target=int(target), priority=3)


def exact(oid, sku, ds, de, kg):
    """Hard-demand order with qty_min == qty_max (a fixed run length)."""
    return dict(order_id=oid, sku=sku, due_start=ds, due_end=de,
                qty_min=kg, qty_max=kg, qty_target=kg, priority=3)


def solve(model, tl=10):
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 2
    s.parameters.max_time_in_seconds = tl
    st = s.Solve(model)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    return s


def status_of(model, tl=10):
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 2
    s.parameters.max_time_in_seconds = tl
    return s, s.Solve(model)


def val(s, expr):
    return expr if isinstance(expr, int) else s.Value(expr)


def blocks(s, v, data):
    """[(line, order_id, start, end)] for present orders, by start."""
    out = []
    for (l, o_idx), pv in v["present"].items():
        if s.Value(pv):
            out.append((l, data.orders[o_idx]["order_id"],
                        s.Value(v["seg_a_start"][(l, o_idx)]),
                        s.Value(v["eff_end"][(l, o_idx)])))
    return sorted(out, key=lambda t: (t[0], t[2]))


# ══════════════════════════════════════════════════════════════════════════
# SA-1  C72 / F9 — same-SKU adjacency is not a changeover
# ══════════════════════════════════════════════════════════════════════════

def test_same_sku_adjacency_costs_zero_and_campaign_is_kept():
    """One line, init CLEAN, hard demand. A-W0 (111, 40 t, W0), A-W1 (111,
    40 t, W1 -> may not start before h168), B-W0 (222, 40 t, W0). 111<->222
    is TTP-only (cost 10, setup 1 h). The pair (111,111) is ABSENT from the
    matrix — exactly the live situation that made the old default (all-1
    flags = 1080) charge a phantom changeover between two runs of one SKU.

    Hand: sequence B, A-W0, A-W1  -> B->A priced 10, A->A 0      = co 10
          sequence A-W0, B, A-W1  -> A->B 10 + B->A 10           = co 20
    Makespan (208) and idle (208 - 120 h of production = 88 h from the gate)
    are identical for both, so the changeover term decides: B first, co 10.
    (Old model: A->A cost 1080, so A,B,A (20) beat B,A,A (1090).)"""
    P = f_params(allow_week1_in_week0=False)
    setup = {("111", "222"): 1, ("222", "111"): 1}
    mc = {("111", "222"): TTP_ONLY, ("222", "111"): TTP_ONLY}
    orders = [exact("A-W0", "111", 0, 167, 40_000),
              exact("A-W1", "111", 168, 335, 40_000),
              exact("B-W0", "222", 0, 167, 40_000)]
    data = mk_data(P, [0], ["111", "222"], orders, setup=setup, mc=mc)
    assert ("111", "111") not in data.machine_changes
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s = solve(m)
    assert val(s, v["co_load"]) == COST_TTP
    seq = [b[1] for b in blocks(s, v, data)]
    assert seq == ["B-W0", "A-W0", "A-W1"]
    # B-W0 [0,40), 1 h setup, A-W0 [41,81) at the earliest — the two 111
    # runs are separated only by the W1 window (>= 168), never by B.
    bl = {b[1]: b for b in blocks(s, v, data)}
    assert bl["A-W1"][2] >= 168
    assert bl["B-W0"][3] + 1 <= bl["A-W0"][2]


# ══════════════════════════════════════════════════════════════════════════
# SA-2  C31 / F1 — the initial changeover is priced
# ══════════════════════════════════════════════════════════════════════════

def test_initial_changeover_is_priced_single_order():
    """E7 from the audit: init 333, one order A (111), 333->111 FULL with a
    2 h setup. co_load was 0 (the time offset existed, the price did not).
    Hand: pair cost 333->111 = 1080; the first order pays it -> co_load 1080,
    co_init_load 1080, A starts at >= 2 (setup time unchanged)."""
    P = f_params(min_run_pct_of_qty=0.0)
    orders = [exact("A-W0", "111", 0, 335, 50_000)]
    data = mk_data(P, [0], ["111", "333"], orders, init={0: "333"},
                   setup={("333", "111"): 2}, mc={("333", "111"): FULL})
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s = solve(m)
    assert val(s, v["co_load"]) == COST_FULL
    assert val(s, v["co_init_load"]) == COST_FULL
    assert blocks(s, v, data)[0][2] >= 2


def test_initial_changeover_pricing_picks_the_free_start():
    """init 333; orders A (111, 30 t) and B (333, 30 t), every transition
    between 111 and 333 FULL (setup 2 h), hard demand, one line.
    Hand: B first -> init->B same SKU 0, B->A 1080          = co 1080
          A first -> init->A 1080 (now priced) + A->B 1080  = co 2160
    Old model: both sequences cost 1080 (the initial one was free) and the
    tie went to whichever CP-SAT found first. Now B must start first and
    co_init_load must be 0."""
    P = f_params(min_run_pct_of_qty=0.0)
    setup = {("111", "333"): 2, ("333", "111"): 2}
    mc = {("111", "333"): FULL, ("333", "111"): FULL}
    orders = [exact("A", "111", 0, 335, 30_000), exact("B", "333", 0, 335, 30_000)]
    data = mk_data(P, [0], ["111", "333"], orders, init={0: "333"},
                   setup=setup, mc=mc)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s = solve(m)
    seq = [b[1] for b in blocks(s, v, data)]
    assert seq == ["B", "A"]
    assert val(s, v["co_load"]) == COST_FULL
    assert val(s, v["co_init_load"]) == 0


def test_initial_changeover_same_sku_or_clean_costs_nothing():
    """CLEAN line, or init SKU == first SKU: no priced initial changeover
    (mirrors the time offset, which is 0 in both cases)."""
    P = f_params(min_run_pct_of_qty=0.0)
    orders = [exact("A", "111", 0, 335, 30_000)]
    for init in ("CLEAN", "111"):
        data = mk_data(P, [0], ["111"], orders, init={0: init},
                       mc={("333", "111"): FULL})
        m, v = build_model(P, data, "full", False, False,
                           objective_mode="balanced")
        s = solve(m)
        assert val(s, v["co_load"]) == 0, init
        assert val(s, v["co_init_load"]) == 0, init


def test_initial_cip_req_changeover_waived_by_committed_cip_window():
    """Scenario F shape: solver CIPs stand down, a committed CIP arrives as a
    downtime row with a CIP reason. init P (protein), first order X with
    P->X cip_req_after=1 (cost 1080 + 150 = 1230, setup floored to 6 h by
    the loader — here given directly). Committed CIP window [10,16) on the
    line, gate 10. Hand: X starts >= 16 (after the window) and the whole
    1230 is refunded -> co_load 0. Without a window the cost would stand."""
    P = f_params(min_run_pct_of_qty=0.0)
    mc_req = dict(FULL, cip_req_after=1)
    orders = [exact("X", "X", 0, 335, 30_000)]
    data = mk_data(P, [0], ["X", "P"], orders, init={0: "P"},
                   setup={("P", "X"): 6}, mc={("P", "X"): mc_req},
                   downtimes=[dict(line_id=0, start=10, end=16, reason="Committed CIP")],
                   avail={0: 10})
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s = solve(m)
    assert val(s, v["co_load"]) == 0
    assert blocks(s, v, data)[0][2] >= 16
    # control: same model, window reason is NOT a CIP -> full price stands
    data2 = mk_data(P, [0], ["X", "P"], orders, init={0: "P"},
                    setup={("P", "X"): 6}, mc={("P", "X"): mc_req},
                    downtimes=[dict(line_id=0, start=10, end=16, reason="Maintenance")],
                    avail={0: 10})
    m2, v2 = build_model(P, data2, "full", False, False, objective_mode="balanced")
    s2 = solve(m2)
    assert val(s2, v2["co_load"]) == COST_FULL + 150


# ══════════════════════════════════════════════════════════════════════════
# SA-3  C73 / F4 — pass-2 per-order floors and the fill exchange rate
# ══════════════════════════════════════════════════════════════════════════

def _soft(**kw) -> Params:
    P = f_params(**kw)
    P.soft_demand = True
    return P


def _two_pass(P, data, floors=True, K=None, eps_pct=1.0):
    m1, v1 = build_model(P, data, "full", False, False,
                         maximize_production=True, objective_mode="balanced")
    s1 = solve(m1)
    score1 = s1.Value(v1["prod_score"])
    kwargs = dict(min_prod_score=int(score1 * (1 - eps_pct / 100)))
    if floors:
        kwargs["order_floors"] = {
            o_idx: min(s1.Value(v1["produced"][o_idx]),
                       int(o.get("qty_target") or 0))
            for o_idx, o in enumerate(data.orders)}
    if K is not None:
        kwargs["fill_exchange_rate"] = K
    m2, v2 = build_model(P, data, "full", False, False,
                         maximize_production=True, objective_mode="balanced",
                         **kwargs)
    s2 = solve(m2)
    return (s1, v1), (s2, v2)


def test_default_fill_exchange_rate_arithmetic():
    """K makes ONE FFS change (co base + ffs, x100 in pass 2) worth 2 h of a
    1,000 kg/h line in fill units (kg x ~1010 tier weight).
    F weights: (5 + 600) x 100 = 60,500 per FFS change;
               2 h x 1,000 kg/h x 1,010 = 2,020,000 fill units;
               2,020,000 / 60,500 = 33.39 -> 33.
    Params defaults: (5 + 10) x 100 = 1,500 -> 2,020,000 / 1,500 = 1,346.7 -> 1,347."""
    assert default_fill_exchange_rate(f_params()) == 33
    assert default_fill_exchange_rate(Params()) == 1347


def test_pass2_floor_keeps_100t_from_being_trimmed_to_99t():
    """E2: one line, one order 100 t (target), nothing to save on
    changeovers. Old pass 2 gave back the whole 1 % epsilon to end 1 h
    earlier (99 t). Hand: floor = min(pass-1 produced 100,000, target
    100,000) = 100,000 -> produced stays 100,000, run 100 h, makespan 100."""
    P = _soft()
    orders = [order("A-W0", "111", 0, 335, 100_000)]
    data = mk_data(P, [0], ["111"], orders, init={0: "111"})
    (s1, v1), (s2, v2) = _two_pass(P, data, floors=True)
    assert s1.Value(v1["produced"][0]) == 100_000
    assert s2.Value(v2["produced"][0]) == 100_000
    assert s2.Value(v2["run_h"][(0, 0)]) == 100


def test_pass2_exchange_rate_alone_does_not_shave_for_makespan():
    """Same E2 with only the exchange rate (no per-order floors): one hour
    of tail = 1,000 kg x 1,000 tier weight = 1,000,000 fill units lost vs
    1 makespan unit + 0 idle saved -> the tail stays (old pass 2 shaved it
    because fill was not in the objective at all)."""
    P = _soft()
    orders = [order("A-W0", "111", 0, 335, 100_000)]
    data = mk_data(P, [0], ["111"], orders, init={0: "111"})
    (_, _), (s2, v2) = _two_pass(P, data, floors=False, K=33)
    assert s2.Value(v2["produced"][0]) == 100_000


def test_pass2_floors_keep_the_2t_sole_line_order():
    """E3b: L0 runs A (111, 200 t). L1 (init 333) runs X (333, 100 t) and B
    (222, 2 t) which ONLY L1 can make; 333<->222 FULL (1080, setup 1 h).
    Old pass 2 deleted B (2 t < 1 % of 302 t) to save the 1080 changeover
    and left L1 idle. With per-order floors B is kept: produced B = 2,000.
    Sequence on L1 by hand: X then B -> init 333->X 0 + X->B 1080 = 1080;
    B then X -> 333->B 1080 (priced initial) + B->X 1080 = 2160 -> X first."""
    P = _soft(min_run_hours=2, min_run_pct_of_qty=0.0, max_lines_per_order=1)
    orders = [order("A-W0", "111", 0, 335, 200_000),
              order("X-W0", "333", 0, 335, 100_000),
              order("B-W0", "222", 0, 335, 2_000)]
    data = mk_data(P, [0, 1], ["111", "222", "333"], orders,
                   init={0: "111", 1: "333"},
                   setup={("333", "222"): 1, ("222", "333"): 1},
                   mc={("333", "222"): FULL, ("222", "333"): FULL},
                   capable={(0, "222"): 0, (0, "333"): 0, (1, "111"): 0})
    (s1, v1), (s2, v2) = _two_pass(P, data, floors=True, K=33)
    assert s1.Value(v1["produced"][2]) == 2_000
    assert s2.Value(v2["produced"][2]) == 2_000
    assert s2.Value(v2["produced"][0]) == 200_000
    assert s2.Value(v2["produced"][1]) == 100_000
    l1 = [b for b in blocks(s2, v2, data) if b[0] == 1]
    assert [b[1] for b in l1] == ["X-W0", "B-W0"]
    assert val(s2, v2["co_load"]) == COST_FULL


def test_pass2_backward_compatible_without_new_args():
    """min_prod_score alone (the pre-fix contract) still builds and floors."""
    P = _soft()
    orders = [order("A-W0", "111", 0, 335, 100_000)]
    data = mk_data(P, [0], ["111"], orders, init={0: "111"})
    (s1, v1), (s2, v2) = _two_pass(P, data, floors=False)
    floor = int(s1.Value(v1["prod_score"]) * 0.99)
    assert s2.Value(v2["prod_score"]) >= floor


# ══════════════════════════════════════════════════════════════════════════
# SA-4  C36 — setup hours are rounded UP
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("hours,expected", [
    (0.0, 0), (0.25, 1), (0.5, 1), (0.75, 1), (1.0, 1), (1.25, 2),
    (2.5, 3), (2.75, 3), (3.0, 3), (2.0000000001, 2), (-1.0, 0),
    (float("nan"), 0),
])
def test_round_setup_hours_never_under_provisions(hours, expected):
    assert round_setup_hours(hours) == expected


def test_round_half_up_still_available_for_other_callers():
    assert round_half_up(0.25) == 0 and round_half_up(0.5) == 1
    assert round_half_up(1.25) == 1 and round_half_up(2.5) == 3


def test_setup_time_1_25h_reserves_2h_in_the_solved_schedule(tmp_path):
    """Loader + model: a 1.25 h standard must reserve 2 h, not 1. A (111)
    then B (222) on one line, hard demand, 10 h each; makespan weight
    forces the runs together, so the gap IS the reserved setup: hand
    B.start - A.end == 2 and makespan == 10 + 2 + 10 = 22."""
    p = tmp_path / "changeovers.csv"
    p.write_text(
        "from_sku,to_sku,setup_hours,ttp_change,ffs_change,topload_change,"
        "casepacker_change,conv_to_org_change,cinn_to_non,added_flavors\n"
        "111,222,1.25,1,0,0,0,0,0,0\n222,111,1.25,1,0,0,0,0,0,0\n",
        encoding="utf-8")
    setup, mc, _ = load_changeover_dicts(p)
    assert setup[("111", "222")] == 2
    P = f_params(min_run_pct_of_qty=0.0)
    orders = [exact("A", "111", 0, 335, 10_000), exact("B", "222", 0, 335, 10_000)]
    data = mk_data(P, [0], ["111", "222"], orders, init={0: "111"},
                   setup=setup, mc=mc)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s = solve(m)
    bl = {b[1]: b for b in blocks(s, v, data)}
    assert bl["B"][2] - bl["A"][3] == 2
    assert bl["B"][3] == 22


def test_live_matrix_only_quarter_hour_rows_change():
    """On the live matrix the only rows whose integer hours change are those
    with a .25 fraction (0.5/0.75 already rounded up under half-up). The
    count is reported in CHANGES.md (13,998 of 54,988 at the time of the
    fix) — asserted structurally here, not as a magic number."""
    src = ROOT / "data" / "reference" / "changeovers.csv"
    if not src.exists():
        pytest.skip("live changeovers.csv not present")
    h = pd.to_numeric(pd.read_csv(src)["setup_hours"], errors="coerce").fillna(0.0)
    changed = (h.apply(round_half_up) != h.apply(round_setup_hours))
    quarter = h.apply(lambda x: abs((x - math.floor(x)) - 0.25) < 1e-9)
    assert changed.sum() == quarter.sum()
    assert changed.sum() > 0


# ══════════════════════════════════════════════════════════════════════════
# SA-5  C61 — current-MO quantity bracket
# ══════════════════════════════════════════════════════════════════════════

def test_current_mo_bounds_bracket_remaining_at_whole_hours_and_level0_is_feasible():
    """remaining 23,000 kg on a 737 kg/h line. Hand: 23,000 / 737 = 31.21 h
    -> qty_min = 31 x 737 = 22,847, qty_max = 32 x 737 = 23,584. The old
    exact bounds (23,000 == 23,000) were unsatisfiable by round(rate) x
    integer hours -> level 0 INFEASIBLE. Level 0 (hard) must now solve with
    produced in {22,847, 23,584}."""
    P = f_params(min_run_pct_of_qty=0.0, cip_interval_h=100000)
    data = mk_data(P, [0], ["S"], [], init={0: "S"}, rate=737.0)
    cmo = pd.DataFrame([{"mo": "30176", "line_name": "L0", "sku": "S",
                         "remaining_kg": 23000, "due_start_h": 0,
                         "due_end_h": 335, "locked_line": 1,
                         "source": "manprg"}])
    data.orders = data._parse_current_mo(cmo)
    o = data.orders[0]
    assert (o["qty_min"], o["qty_max"]) == (22_847, 23_584)
    assert o["qty_remaining"] == 23_000
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    assert s.Value(v["produced"][0]) in (22_847, 23_584)


def test_current_mo_bounds_exact_multiple_and_unknown_rate():
    """22,110 = 30 x 737 -> qmin == qmax == 22,110 (unchanged behaviour);
    a sub-hour remainder (500 kg @ 737) takes at least one hour: 737..737;
    unknown rate (no rate row) keeps the old exact bounds."""
    P = f_params()
    data = mk_data(P, [0], ["S"], [], init={0: "S"}, rate=737.0)
    rows = [{"mo": "1", "line_name": "L0", "sku": "S", "remaining_kg": 22110,
             "due_start_h": 0, "due_end_h": 335, "locked_line": 1},
            {"mo": "2", "line_name": "L0", "sku": "S", "remaining_kg": 500,
             "due_start_h": 0, "due_end_h": 335, "locked_line": 1},
            {"mo": "3", "line_name": "L0", "sku": "NORATE", "remaining_kg": 1234,
             "due_start_h": 0, "due_end_h": 335, "locked_line": 1}]
    out = data._parse_current_mo(pd.DataFrame(rows))
    assert (out[0]["qty_min"], out[0]["qty_max"]) == (22_110, 22_110)
    assert (out[1]["qty_min"], out[1]["qty_max"]) == (737, 737)
    assert (out[2]["qty_min"], out[2]["qty_max"]) == (1_234, 1_234)


# ══════════════════════════════════════════════════════════════════════════
# SA-6  C63 — ignore_co keeps setup TIME
# ══════════════════════════════════════════════════════════════════════════

def test_ignore_co_still_enforces_setup_time_between_different_skus():
    """Relax level 3 (ignore_co=True): A (111, 10 h) and B (222, 10 h) on
    one line, setup 3 h both ways, hard demand, balanced objective (makespan
    x6 pulls the runs together). Hand: the gap between the runs must be
    exactly the 3 h setup and makespan = 10 + 3 + 10 = 23. Old level 3
    dropped the setup entirely (gap 0, makespan 20) and such schedules were
    written and scored as FEASIBLE plans."""
    P = f_params(min_run_pct_of_qty=0.0)
    setup = {("111", "222"): 3, ("222", "111"): 3}
    mc = {("111", "222"): FULL, ("222", "111"): FULL}
    orders = [exact("A", "111", 0, 335, 10_000), exact("B", "222", 0, 335, 10_000)]
    data = mk_data(P, [0], ["111", "222"], orders, setup=setup, mc=mc)
    m, v = build_model(P, data, "full", False, True, objective_mode="balanced")
    s = solve(m)
    bl = sorted(blocks(s, v, data), key=lambda b: b[2])
    assert bl[1][2] - bl[0][3] == 3
    assert max(b[3] for b in bl) == 23
    # and the price is indeed off: no weighted changeover load at level 3
    assert isinstance(v["co_init_load"], int) and v["co_init_load"] == 0


def test_ignore_co_still_enforces_initial_setup_time():
    """Level 3, init 333, order A (111), setup 333->111 = 2 h, gate 5:
    A must start at >= 5 + 2 = 7 (old level 3: at the gate, 5)."""
    P = f_params(min_run_pct_of_qty=0.0)
    orders = [exact("A", "111", 0, 335, 10_000)]
    data = mk_data(P, [0], ["111", "333"], orders, init={0: "333"},
                   setup={("333", "111"): 2}, mc={("333", "111"): FULL},
                   avail={0: 5})
    m, v = build_model(P, data, "full", False, True, objective_mode="balanced")
    s = solve(m)
    assert blocks(s, v, data)[0][2] == 7


# ══════════════════════════════════════════════════════════════════════════
# SA-7  C33 — co_cost_l domain bound
# ══════════════════════════════════════════════════════════════════════════

def test_flavor_heavy_sequence_is_not_declared_infeasible():
    """Four distinct SKUs on ONE line (mlpo 1, hard demand), every pair
    TTP-only with added_flavors 3 and W_flavor 5,000: pair cost = base 5 +
    ttp 5 + 3 x 5,000 = 15,010; three adjacencies = 45,030. The old closed-
    form bound len(elig) x (sum of non-flavor weights) = 4 x 1,280 = 5,120
    made co_cost_l's domain too small -> INFEASIBLE. Now: solves with
    co_load 45,030."""
    P = f_params(min_run_pct_of_qty=0.0, max_lines_per_order=1,
                 co_flavor_weight=5000)
    skus = ["1", "2", "3", "4"]
    mc = {(a, b): dict(TTP_ONLY, added_flavors=3)
          for a in skus for b in skus if a != b}
    orders = [exact(f"O{s}", s, 0, 335, 10_000) for s in skus]
    data = mk_data(P, [0], skus, orders, mc=mc)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    assert val(s, v["co_load"]) == 3 * (5 + 5 + 3 * 5000)


# ══════════════════════════════════════════════════════════════════════════
# SA-8  C65 / adversarial-1 / adversarial-3 — producible guards
# ══════════════════════════════════════════════════════════════════════════

def test_overlapping_downtime_rows_are_unioned():
    """Two identical downtime rows [0,200) on a 336 h horizon. Union = 200
    blocked -> 136 usable hours, 136,000 kg at 1,000 kg/h. The old per-row
    subtraction gave 336 - 200 - 200 < 0 -> dead pair, bound 0, order
    unmakeable. The model must place the 100 h order after h200."""
    P = f_params(min_run_pct_of_qty=0.0)
    dts = [dict(line_id=0, start=0, end=200, reason="down"),
           dict(line_id=0, start=0, end=200, reason="down (duplicate)")]
    orders = [exact("A", "111", 0, 335, 100_000)]
    data = mk_data(P, [0], ["111"], orders, downtimes=dts)
    assert _usable_hours_on_line(P, data, 0, 0, 335) == 136
    assert _producible_kg_in_window(P, data, orders[0], [0]) == 136_000
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s = solve(m)
    b = blocks(s, v, data)[0]
    assert b[2] >= 200 and s.Value(v["produced"][0]) == 100_000


def test_partially_overlapping_downtimes_union():
    """[0,100) and [50,150) -> union [0,150) = 150 blocked, 186 usable
    (old: 336 - 100 - 100 = 136)."""
    P = f_params()
    dts = [dict(line_id=0, start=0, end=100, reason="a"),
           dict(line_id=0, start=50, end=150, reason="b")]
    data = mk_data(P, [0], ["111"], [], downtimes=dts)
    assert _usable_hours_on_line(P, data, 0, 0, 335) == 186


def test_non_capable_line_with_flat_rate_is_excluded_from_the_bound():
    """line_rates.csv writes the flat line rate onto capable == 0 pairs too.
    L0: capable 0, rate 1,000 (whole horizon free). L1: capable 1, rate
    1,000, free only [0,50). Order qmin 100,000. Honest bound = L1 only =
    50 h x 1,000 = 50,000 (old: 336,000 + 50,000 = 386,000, so qmin was not
    clamped and the hard model was INFEASIBLE). Now qmin clamps to 50,000
    and level 0 solves with produced 50,000 on L1."""
    P = f_params(min_run_pct_of_qty=0.0)
    orders = [order("A", "111", 0, 335, 100_000, lo=1.0, hi=1.1)]
    data = mk_data(P, [0, 1], ["111"], orders,
                   capable={(0, "111"): 0},
                   downtimes=[dict(line_id=1, start=50, end=336, reason="down")])
    assert _producible_kg_in_window(P, data, orders[0], [0, 1]) == 50_000
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    assert s.Value(v["produced"][0]) == 50_000
    assert [b[0] for b in blocks(s, v, data)] == [1]


# ══════════════════════════════════════════════════════════════════════════
# SA-9  C62 — min_run_pct_of_qty on the clamped qmin; soft demand -> hours
# ══════════════════════════════════════════════════════════════════════════

def test_min_run_pct_uses_the_window_clamped_qmin():
    """Hard demand, order qmin 200 t (target 200 t, lower_pct 1.0), two
    lines each free only [0,60), pct 0.5, mlpo 2. Bound = 2 x 60 h x 1,000 =
    120,000 -> qmin clamps to 120,000; per-line floor = ceil(0.5 x 120,000 /
    1,000) = 60 h <= 60 free -> both lines run 60 h, produced 120,000.
    Old: floor = ceil(0.5 x 200,000 / 1,000) = 100 h > 60 free on either
    line -> neither line could host it -> INFEASIBLE."""
    P = f_params(min_run_pct_of_qty=0.5, max_lines_per_order=2)
    orders = [order("A", "111", 0, 335, 200_000, lo=1.0, hi=1.1)]
    dts = [dict(line_id=0, start=60, end=336, reason="down"),
           dict(line_id=1, start=60, end=336, reason="down")]
    data = mk_data(P, [0, 1], ["111"], orders, downtimes=dts)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    assert s.Value(v["produced"][0]) == 120_000
    assert sorted(s.Value(v["run_h"][(l, 0)]) for l in (0, 1)) == [60, 60]


def test_soft_demand_applies_only_min_run_hours_so_partial_fills_split():
    """Scenario F (soft demand, maximize fill): order target 100 t, qmin
    90 t. L0 free [0,60), L1 free [0,30). pct 0.5, min_run 4.
    New: only the 4 h floor applies -> 60 + 30 = 90 h -> 90,000 kg.
    Old: per-line floor ceil(0.5 x 90,000 / 1,000) = 45 h > L1's 30 free
    -> L1 unusable -> 60,000 kg."""
    P = _soft(min_run_pct_of_qty=0.5, max_lines_per_order=2)
    orders = [order("A", "111", 0, 335, 100_000)]
    dts = [dict(line_id=0, start=60, end=336, reason="down"),
           dict(line_id=1, start=30, end=336, reason="down")]
    data = mk_data(P, [0, 1], ["111"], orders, downtimes=dts)
    m, v = build_model(P, data, "full", False, False,
                       maximize_production=True, objective_mode="balanced")
    s = solve(m)
    assert s.Value(v["produced"][0]) == 90_000


# ══════════════════════════════════════════════════════════════════════════
# SA-10  F3 — idle measured from the availability gate
# ══════════════════════════════════════════════════════════════════════════

def test_idle_counts_hours_between_the_gate_and_the_first_run():
    """Line gate 10, one 40 h order whose window is [50, 89] -> the run is
    pinned to [50, 90). Idle by hand = span from the gate (90 - 10 = 80)
    minus 40 h production = 40. Old definition (span from the FIRST block)
    gave 0: leading idle was free."""
    P = f_params(min_run_pct_of_qty=0.0)
    orders = [exact("A", "111", 50, 89, 40_000)]
    data = mk_data(P, [0], ["111"], orders, avail={0: 10})
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s = solve(m)
    assert blocks(s, v, data)[0][2:] == (50, 90)
    assert s.Value(v["line_idle"][0]) == 40


def test_starting_late_is_no_longer_cheaper_than_starting_at_the_gate():
    """Two same-SKU 40 h orders on one line, B pinned to [100,140) by its
    window. A's window either pins it to [0,40) or to [20,60). Balanced
    objective = makespan x6 + co x300 + idle x3. New: idle = 140 - 80 = 60
    in both cases -> objective 140 x 6 + 0 + 60 x 3 = 1,020 for both.
    Old: variant 2 had idle 40 (leading 20 h free) -> 960 < 1,020, i.e.
    'start late' was strictly optimal for nothing."""
    P = f_params(min_run_pct_of_qty=0.0)
    objs = []
    for a_ds in (0, 20):
        orders = [exact("A", "111", a_ds, a_ds + 39, 40_000),
                  exact("B", "111", 100, 139, 40_000)]
        data = mk_data(P, [0], ["111"], orders)
        m, v = build_model(P, data, "full", False, False,
                           objective_mode="balanced")
        s = solve(m)
        assert s.Value(v["line_idle"][0]) == 60
        objs.append(int(round(s.ObjectiveValue())))
    assert objs == [1020, 1020]


# ══════════════════════════════════════════════════════════════════════════
# SA-11  adversarial-3 — bound respects max_lines_per_order
# ══════════════════════════════════════════════════════════════════════════

def test_producible_bound_keeps_only_mlpo_largest_lines():
    """Three capable lines free [0,50), [0,40), [0,30) at 1,000 kg/h:
    per-line 50,000 / 40,000 / 30,000. mlpo None -> 120,000; mlpo 2 -> the
    two largest = 90,000; mlpo 1 -> 50,000."""
    P = f_params()
    dts = [dict(line_id=0, start=50, end=336, reason="d"),
           dict(line_id=1, start=40, end=336, reason="d"),
           dict(line_id=2, start=30, end=336, reason="d")]
    o = order("A", "111", 0, 335, 500_000)
    data = mk_data(P, [0, 1, 2], ["111"], [o], downtimes=dts)
    assert _producible_kg_in_window(P, data, o, [0, 1, 2]) == 120_000
    assert _producible_kg_in_window(P, data, o, [0, 1, 2], mlpo=2) == 90_000
    assert _producible_kg_in_window(P, data, o, [0, 1, 2], mlpo=1) == 50_000


def test_impossible_demand_under_mlpo_is_clamped_not_infeasible():
    """Same three lines, mlpo 1, hard order qmin 80,000: the honest bound is
    50,000 (one line), so qmin clamps to 50,000 and level 0 solves with
    produced 50,000 on L0. Old bound 120,000 left qmin at 80,000 ->
    INFEASIBLE (reported as a solver failure, not impossible demand)."""
    P = f_params(min_run_pct_of_qty=0.0, max_lines_per_order=1)
    dts = [dict(line_id=0, start=50, end=336, reason="d"),
           dict(line_id=1, start=40, end=336, reason="d"),
           dict(line_id=2, start=30, end=336, reason="d")]
    o = order("A", "111", 0, 335, 80_000, lo=1.0, hi=1.1)
    data = mk_data(P, [0, 1, 2], ["111"], [o], downtimes=dts)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = status_of(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE), s.StatusName(st)
    assert s.Value(v["produced"][0]) == 50_000
    assert [b[0] for b in blocks(s, v, data)] == [0]


# ══════════════════════════════════════════════════════════════════════════
# SA-12  data_loader validation
# ══════════════════════════════════════════════════════════════════════════

def _demand_df(rows):
    return pd.DataFrame(rows, columns=["order_id", "sku", "week_index",
                                       "qty_target", "lower_pct", "upper_pct",
                                       "due_start_hour", "due_end_hour",
                                       "priority"])


def _row(oid="X-W0", sku="X", target=1000.0, lo=0.9, hi=1.1):
    return [oid, sku, 0, target, lo, hi, 0, 167, 3]


def test_demand_negative_target_raises_naming_the_row():
    d = mk_data(f_params(), [0], ["X"], [])
    with pytest.raises(ValueError, match=r"X-W0.*negative"):
        d._parse_demand(_demand_df([_row(target=-5000.0)]))


def test_demand_inverted_pct_raises_naming_the_row():
    d = mk_data(f_params(), [0], ["X"], [])
    with pytest.raises(ValueError, match=r"X-W0.*lower_pct=1.1 > upper_pct=0.9"):
        d._parse_demand(_demand_df([_row(lo=1.1, hi=0.9)]))


def test_demand_non_numeric_target_raises_naming_the_row():
    d = mk_data(f_params(), [0], ["X"], [])
    with pytest.raises(ValueError, match=r"X-W0.*qty_target='abc' is not a number"):
        d._parse_demand(_demand_df([_row(target="abc")]))


def test_demand_zero_target_is_legitimate():
    """Scenario F netting writes fully-covered weeks as qty_target 0.0 (8 such
    rows in the frozen F work dir): qmin = qmax = 0, never an error."""
    d = mk_data(f_params(), [0], ["X"], [])
    out = d._parse_demand(_demand_df([_row(target=0.0)]))
    assert (out[0]["qty_min"], out[0]["qty_max"]) == (0, 0)
    # and the good row still parses to floor(1000 x 0.9) = 900, ceil(1100)
    out = d._parse_demand(_demand_df([_row()]))
    assert (out[0]["qty_min"], out[0]["qty_max"]) == (900, 1100)


def test_capabilities_unparsable_line_id_raises(tmp_path):
    """A line NAME in the line_id column used to become line 0 (P09) and
    declare that SKU capable on P09 at the foreign line's rate."""
    pd.DataFrame([{"line_id": 0, "sku": "A", "line_name": "P09", "capable": 1,
                   "calc_rate_kgph": 500.0},
                  {"line_id": "P17B", "sku": "B", "line_name": "P17B",
                   "capable": 1, "calc_rate_kgph": 700.0}]
                 ).to_csv(tmp_path / "capabilities_rates.csv", index=False)
    for name, cols in (("changeovers.csv", ["from_sku", "to_sku", "setup_hours"]),
                       ("initial_states.csv", ["line_id", "initial_sku"]),
                       ("demand_plan.csv", ["order_id", "sku", "qty_target",
                                            "lower_pct", "upper_pct"])):
        pd.DataFrame(columns=cols).to_csv(tmp_path / name, index=False)
    d = Data(Params(), Files(tmp_path))
    with pytest.raises(ValueError, match=r"P17B"):
        d.load()


def test_duplicate_mo_numbers_get_distinct_order_ids(capsys):
    """The same MO number on two lines: two orders, two ids, the '|CUR'
    suffix consumers key on preserved, mo_id unchanged, a warning printed."""
    P = f_params()
    data = mk_data(P, [0, 1], ["S"], [], rate=1000.0)
    rows = [{"mo": "77777", "line_name": "L0", "sku": "S", "remaining_kg": 3600,
             "due_start_h": 0, "due_end_h": 335, "locked_line": 1},
            {"mo": "77777", "line_name": "L1", "sku": "S", "remaining_kg": 3600,
             "due_start_h": 0, "due_end_h": 335, "locked_line": 1}]
    out = data._parse_current_mo(pd.DataFrame(rows))
    ids = [o["order_id"] for o in out]
    assert len(set(ids)) == 2
    assert ids[0] == "77777|CUR" and ids[1] == "77777#2|CUR"
    assert all(i.endswith("|CUR") for i in ids)
    assert [o["mo_id"] for o in out] == ["77777", "77777"]
    assert [o["locked_line"] for o in out] == [0, 1]
    assert "WARNING" in capsys.readouterr().out
