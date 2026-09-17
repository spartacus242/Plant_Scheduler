# tests/test_min_run_8h.py — the 8-hour minimum run (plant decision
# 2026-09-16: "4 hrs is too short. Make it 8 hrs").
#
# Rule under test (model_builder.min_run_dead_reason + run-bound block,
# independent_validator MIN_RUN, [scorecard] short_run_h):
#   * every DEMAND run the solver creates, and each CIP-split segment, is
#     >= [scheduler] min_run_hours — no max_len clamp any more;
#   * a demand (line, order) pair that cannot host ONE minimum run (window
#     too short, or one run over qty_max) is a dead pair;
#   * an order no capable line can host has its floor clamped to 0 (hard
#     demand stays FEASIBLE) and is listed (vars_dict["min_run_too_small"] +
#     one model warning);
#   * committed work (current-state MOs, pinned trials) keeps the legacy
#     floor min(min_run_hours, 4).
#
# Every expected value is derived by hand in the docstring. Models are tiny
# and solve in well under a second (2 workers, 10 s cap).

from __future__ import annotations

import math
import sys
import tomllib
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "code", ROOT / "code" / "solver", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ortools.sat.python import cp_model  # noqa: E402

from data_loader import Data, Files, Params  # noqa: E402
import model_builder as mb  # noqa: E402
from model_builder import build_model  # noqa: E402
import phase2_scheduler as p2  # noqa: E402
from solver import independent_validator as iv  # noqa: E402
from helpers.config import scorecard_config  # noqa: E402
from helpers.calendar_io import CALENDAR_COLUMNS  # noqa: E402
from helpers.scorecard_engine import score_campaigns  # noqa: E402
from helpers.solver_rules import solver_rules  # noqa: E402

import test_independent_validator as tiv  # noqa: E402  (world builder)

ROOT_TOML = ROOT / "flowstate.toml"


# ── Harness ───────────────────────────────────────────────────────────────

def _root_cfg() -> dict:
    return tomllib.loads(ROOT_TOML.read_text(encoding="utf-8"))


def params(**kw) -> Params:
    base = dict(
        horizon_h=168, min_run_hours=8, min_run_pct_of_qty=0.0,
        max_lines_per_order=2, allow_week1_in_week0=True,
        objective_makespan_weight=1, objective_changeover_weight=1,
        objective_idle_weight=0, co_base_weight=5, co_ffs_weight=10,
        cip_interval_h=100000, cip_duration_h=6,
    )
    base.update(kw)
    return Params(**base)


def soft(**kw) -> Params:
    P = params(**kw)
    P.soft_demand = True
    return P


def mk_data(P, lines, skus, orders, downtimes=None, rate=1000.0, rates=None,
            avail=None):
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = list(lines)
    d.line_names = {l: f"L{l}" for l in lines}
    for l in lines:
        for s in skus:
            d.capable[(l, s)] = 1
            d.rate[(l, s)] = float((rates or {}).get(l, rate))
    avail = avail or {}
    d.init_map = {l: {"available_from": avail.get(l, 0), "initial_sku": "CLEAN",
                      "carryover_run_hours": 0, "long_shutdown_flag": 0,
                      "long_shutdown_extra": 0} for l in lines}
    d.downtimes = list(downtimes or [])
    d.cip_interval_map = {l: 100000 for l in lines}   # F stand-down
    d.setup, d.machine_changes = {}, {}
    d.orders = [dict(o) for o in orders]
    return d


def order(oid, sku, ds, de, target, lo=0.9, hi=1.1):
    return dict(order_id=oid, sku=sku, due_start=ds, due_end=de,
                qty_min=int(math.floor(target * lo)),
                qty_max=int(math.ceil(target * hi)),
                qty_target=int(target), priority=3)


def current_mo(oid, sku, ds, de, kg, line):
    return dict(order_id=oid, sku=sku, due_start=ds, due_end=de, qty_min=kg,
                qty_max=kg, qty_target=kg, qty_remaining=kg, priority=0,
                is_current_mo=True, mo_id=oid.split("|")[0], locked_line=line)


def solve(model, tl=10):
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 2
    s.parameters.max_time_in_seconds = tl
    return s, s.Solve(model)


def ok(st):
    return st in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def fill_model(P, data, **kw):
    return build_model(P, data, "full", False, False, maximize_production=True,
                       objective_mode="balanced", **kw)


def var_names(model) -> set:
    return {v.name for v in model.Proto().variables}


# ══════════════════════════════════════════════════════════════════════════
# flowstate.toml carries the decision
# ══════════════════════════════════════════════════════════════════════════

def test_repo_toml_carries_the_2026_09_16_decisions():
    """[scheduler] min_run_hours 8 (was 4), [scorecard] short_run_h 8.0 (was
    4.0), both commented with the decision; [scheduler] partial_week_demand =
    "prebuild" for the pre-build agent. Every reader resolves 8."""
    cfg = _root_cfg()
    assert cfg["scheduler"]["min_run_hours"] == 8
    assert cfg["scorecard"]["short_run_h"] == 8.0
    assert cfg["scheduler"]["partial_week_demand"] == "prebuild"
    text = ROOT_TOML.read_text(encoding="utf-8")
    assert text.count("4 hrs is too short. Make it 8 hrs") >= 2
    assert '"prorate"' in text and "2026-09-16" in text
    assert p2.params_from_config(cfg).min_run_hours == 8
    assert iv.load_cfg(cfg).min_run_hours == 8.0
    assert scorecard_config(cfg)["short_run_h"] == 8.0
    assert {r["id"]: r for r in solver_rules(cfg)}["min_run_hours"]["value"] == 8


def test_a_6h_demand_run_is_rejected_and_an_8h_run_allowed_under_the_repo_toml():
    """One line at 1,000 kg/h, soft demand, order target 50 t (qty_max
    55 t), window [0,168). Params from the repo toml's min_run_hours (8):
    forcing run_h == 6 is INFEASIBLE, run_h == 8 is FEASIBLE (8,000 kg).
    Under the old toml (4) the 6 h run was legal."""
    mrh = p2.params_from_config(_root_cfg()).min_run_hours
    for hours, feasible in ((6, False), (8, True)):
        P = soft(min_run_hours=mrh)
        data = mk_data(P, [0], ["A"], [order("A", "A", 0, 167, 50_000)])
        m, v = fill_model(P, data)
        m.Add(v["present"][(0, 0)] == 1)
        m.Add(v["run_h"][(0, 0)] == hours)
        s, st = solve(m)
        assert ok(st) is feasible, (hours, s.StatusName(st))
        if feasible:
            assert s.Value(v["produced"][0]) == 8_000


def test_cip_split_segments_each_run_the_repo_minimum():
    """Line free [0,6), committed CIP [6,12), free [12,40), down [40,168).
    Soft demand, 100 t target, repo-toml floor (8). The 6 h stretch before the
    clean cannot host a segment (6 < 8), so the only run is [12,40) = 28 h =
    28,000 kg and no segment is shorter than 8 h. Under the old 4 h floor
    seg_a [0,6) + seg_b [12,40) made 34,000 kg."""
    mrh = p2.params_from_config(_root_cfg()).min_run_hours
    P = soft(min_run_hours=mrh)
    dts = [dict(line_id=0, start=6, end=12, reason="Committed CIP"),
           dict(line_id=0, start=40, end=168, reason="down")]
    data = mk_data(P, [0], ["A"], [order("A", "A", 0, 167, 100_000)], downtimes=dts)
    m, v = fill_model(P, data)
    s, st = solve(m)
    assert ok(st), s.StatusName(st)
    assert s.Value(v["produced"][0]) == 28_000
    seg_a = s.Value(v["seg_a_run"][(0, 0)])
    assert seg_a >= 8
    if s.Value(v["seg_b_present"][(0, 0)]):
        assert s.Value(v["seg_b_run"][(0, 0)]) >= 8
    # and a forced 6 h seg_a with a seg_b is infeasible outright
    m2, v2 = fill_model(P, data)
    m2.Add(v2["seg_b_present"][(0, 0)] == 1)
    m2.Add(v2["seg_a_run"][(0, 0)] == 6)
    _, st2 = solve(m2)
    assert not ok(st2)


# ══════════════════════════════════════════════════════════════════════════
# Dead pairs: window too short, qty_max below one minimum run
# ══════════════════════════════════════════════════════════════════════════

def test_due_window_shorter_than_the_minimum_makes_the_pair_dead():
    """early_fill_hours 0 pins the order to its own week: due window
    [100,106) = 6 h (max_len 6). Soft demand, 20 t target, floor 8.
    Old: min_run = min(max_len 6, 8) = 6 -> a 6 h run, 6,000 kg.
    New: the pair is dead ("window"), nothing is produced, the order is
    listed with longest_free_h 6 and the warning names it."""
    P = soft(early_fill_hours=0)
    data = mk_data(P, [0], ["A"], [order("W", "A", 100, 105, 20_000)])
    m, v = fill_model(P, data)
    s, st = solve(m)
    assert ok(st)
    assert s.Value(v["produced"][0]) == 0
    assert v["min_run_dead_pairs"] == {(0, 0): "window"}
    assert (0, 0) in v["dead_pairs"]
    (rec,) = v["min_run_too_small"]
    assert rec["order_id"] == "W" and rec["reason"] == "window"
    assert rec["longest_free_h"] == 6 and rec["min_run_hours"] == 8
    assert any("cannot host one 8 h minimum run" in w and "W (" in w
               for w in v["warnings"])


def test_longest_contiguous_stretch_decides_window_death():
    """Window [0,20) on one line (horizon 20). Downtime [7,13) leaves free
    stretches 7 h and 7 h -> 14 usable hours but no 8 h stretch -> dead.
    Downtime [6,12) leaves 6 h and 8 h -> the [12,20) stretch hosts one
    minimum run -> live."""
    P = soft(horizon_h=20)
    o = order("A", "A", 0, 19, 50_000)
    dead = mk_data(P, [0], ["A"], [o], downtimes=[dict(line_id=0, start=7, end=13, reason="d")])
    live = mk_data(P, [0], ["A"], [o], downtimes=[dict(line_id=0, start=6, end=12, reason="d")])
    assert mb._longest_free_stretch_h(dead, 0, 0, 20) == 7
    assert mb._longest_free_stretch_h(live, 0, 0, 20) == 8
    assert mb.min_run_dead_reason(P, dead, dead.orders[0], 0) == "window"
    assert mb.min_run_dead_reason(P, live, live.orders[0], 0) is None
    _, v = fill_model(P, dead)
    assert v["min_run_dead_pairs"] == {(0, 0): "window"}


def test_qty_max_below_one_minimum_run_makes_the_pair_dead_not_shorter():
    """Order target 4,500 kg (qty_max ceil(4,500 x 1.1) = 4,950). L0 runs
    1,000 kg/h: one 8 h run = 8,000 > 4,950 -> dead ("qty_max"). L1 runs
    500 kg/h: 8 h = 4,000 <= 4,950 -> live; the best fill is 9 h = 4,500 kg
    (10 h = 5,000 > qty_max). produced == rate x run_h is an equality, so
    the model can never run 8 h on L0 and book fewer kg. The dead pair gets
    no changeover arc (first_ flag), CIP-link or idle variables."""
    P = soft()
    data = mk_data(P, [0, 1], ["A"], [order("A", "A", 0, 167, 4_500)],
                   rates={0: 1000.0, 1: 500.0})
    m, v = fill_model(P, data)
    assert v["min_run_dead_pairs"] == {(0, 0): "qty_max"}
    assert v["min_run_too_small"] == []          # L1 can still host it
    names = var_names(m)
    assert "first_l0_oA" not in names and "first_l1_oA" in names
    assert "cipEo0_l0_o0" not in names and "cipEo0_l1_o0" in names
    s, st = solve(m)
    assert ok(st)
    assert s.Value(v["present"][(0, 0)]) == 0
    assert s.Value(v["run_h"][(1, 0)]) == 9
    assert s.Value(v["produced"][0]) == 4_500


def test_idle_bookkeeping_skips_dead_pairs():
    """objective_idle_weight > 0 builds per-pair first-start / last-end
    vars; a dead pair (qty_max) gets none, a live one does, and the idle of
    a line with ONLY dead pairs is still a well-defined 0."""
    P = soft(objective_idle_weight=3)
    data = mk_data(P, [0, 1], ["A"], [order("A", "A", 0, 167, 4_500)],
                   rates={0: 1000.0, 1: 500.0})
    m, v = fill_model(P, data)
    names = var_names(m)
    assert "cS_l0_o0" not in names and "cE_l0_o0" not in names
    assert "cS_l1_o0" in names and "cE_l1_o0" in names
    s, st = solve(m)
    assert ok(st)
    assert s.Value(v["line_idle"][0]) == 0


# ══════════════════════════════════════════════════════════════════════════
# Orders too small everywhere: soft stays short, hard stays FEASIBLE
# ══════════════════════════════════════════════════════════════════════════

def _too_small_world(P):
    """Two lines at 1,000 kg/h; order S: target 3,000 (qmin 2,700, qmax
    3,301) — one 8 h run makes 8,000 > 3,301 on BOTH lines. Order B: target
    40,000 (qmin 36,000) is an ordinary order."""
    orders = [order("S", "A", 0, 167, 3_000), order("B", "A", 0, 167, 40_000)]
    return mk_data(P, [0, 1], ["A"], orders)


def test_order_too_small_everywhere_soft_demand_stays_short_and_is_listed():
    """Soft demand: S produces 0 and is listed (reason qty_max, best line
    L0 at 8,000 kg per minimum run); B is filled to its 40,000 target (over-
    target kg earn nothing at over_target_reward_pct 0); the
    model warning carries the count, the kg of target and the order."""
    P = soft()
    data = _too_small_world(P)
    m, v = fill_model(P, data)
    assert v["min_run_dead_pairs"] == {(0, 0): "qty_max", (1, 0): "qty_max"}
    (rec,) = v["min_run_too_small"]
    # qty_max = ceil(3,000 x 1.1) = 3,301 (float 3,300.0000000000005)
    assert (rec["order_id"], rec["qty_min"], rec["qty_target"], rec["qty_max"]) == ("S", 2_700, 3_000, 3_301)
    assert rec["reason"] == "qty_max" and rec["best_line_min_run_kg"] == 8_000
    assert rec["best_line"] == "L0"
    assert "8,000 kg" in rec["why"] and "qty_max 3,301" in rec["why"]
    (w,) = [w for w in v["warnings"] if "minimum run" in w]
    assert w.startswith("1 demand order(s) cannot host one 8 h minimum run")
    assert "3,000 kg of target" in w and "S (target 3,000 kg" in w
    s, st = solve(m)
    assert ok(st)
    assert s.Value(v["produced"][0]) == 0
    assert 40_000 <= s.Value(v["produced"][1]) <= 44_000


def test_order_too_small_everywhere_hard_demand_is_feasible_with_the_floor_clamped():
    """Hard demand (balanced objective, relax level 0). Old: S's qty_min
    2,700 > 0 and every present pair needs >= 8 h = 8,000 kg > qty_max 3,300
    -> INFEASIBLE, the relax ladder would escalate. New: S's floor is clamped
    to 0 (listed), B keeps its hard floor 36,000 -> FEASIBLE."""
    P = params()
    data = _too_small_world(P)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = solve(m)
    assert ok(st), s.StatusName(st)
    assert s.Value(v["produced"][0]) == 0
    assert 36_000 <= s.Value(v["produced"][1]) <= 44_000
    assert [t["order_id"] for t in v["min_run_too_small"]] == ["S"]


def test_window_bound_skips_lines_that_cannot_host_a_minimum_run():
    """Hard demand, order qmin 30,000 (target 30,000, lo 1.0, hi 1.1 ->
    qmax 33,000). L0 at 1,000 kg/h is free only [0,20) -> 20,000 kg max. L1
    at 5,000 kg/h: one 8 h run = 40,000 > 33,000 -> dead. Old bound counted
    L1 (20,000 + 168 x 5,000) so qmin stayed 30,000 -> INFEASIBLE. New:
    the bound skips L1 -> 20,000, qmin clamps to 20,000 -> FEASIBLE with
    L0 running 20 h."""
    P = params()
    dts = [dict(line_id=0, start=20, end=168, reason="down")]
    data = mk_data(P, [0, 1], ["A"], [order("A", "A", 0, 167, 30_000, lo=1.0)],
                   downtimes=dts, rates={0: 1000.0, 1: 5000.0})
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = solve(m)
    assert ok(st), s.StatusName(st)
    assert s.Value(v["produced"][0]) == 20_000
    assert s.Value(v["run_h"][(0, 0)]) == 20
    assert v["min_run_dead_pairs"] == {(1, 0): "qty_max"}
    o = data.orders[0]
    # Without skip_lines the qty_max line-count cap (review fix SOLVER-1 (3))
    # already keeps ONE line: 8,000 + 40,000 > 33,000, so the order can never
    # run on both -> the larger single line, 168 x 5,000 (still an over-
    # estimate; the dead-pair skip is what brings it down to 20,000).
    assert mb._producible_kg_in_window(P, data, o, [0, 1]) == 168 * 5_000
    assert mb._producible_kg_in_window(P, data, o, [0, 1], skip_lines=[1]) == 20_000


# ══════════════════════════════════════════════════════════════════════════
# Committed work is unaffected
# ══════════════════════════════════════════════════════════════════════════

def test_pinned_6h_trial_stays_feasible_under_the_8h_demand_floor():
    """A trial pinned to L0 [10,16) with trial_run_hours 6. Old: seg_a_run >=
    min_run_hours 8 but run_h == 6 -> INFEASIBLE at every relax level. New:
    trials keep the legacy committed floor min(8, 4) = 4 -> FEASIBLE, the
    trial runs its 6 h."""
    P = params()
    trial = dict(order("T", "A", 10, 15, 6_000, lo=1.0, hi=1.0), is_trial=True,
                 trial_line=0, trial_start_hour=10, trial_end_hour=16,
                 trial_run_hours=6)
    data = mk_data(P, [0], ["A"], [trial])
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    assert v["min_run_hours"] == {"demand": 8, "committed": 4}
    s, st = solve(m)
    assert ok(st), s.StatusName(st)
    assert s.Value(v["run_h"][(0, 0)]) == 6
    assert v["min_run_dead_pairs"] == {}


def test_current_mo_short_remainder_and_split_leg_stay_feasible():
    """Current-state MOs keep the legacy floor: a 6,000 kg remainder at
    1,000 kg/h (6 h, < 8) runs its 6 h on L0. M2 (5,000 kg = 5 h) on L1 must
    finish by h12 while L1 is free only [0,2), then a committed clean [2,8),
    then [8,12): it can only run as a CIP split whose first leg is <= 2 h
    (no per-segment floor for committed MOs; total 5 >= the committed floor
    4). Both FEASIBLE at their remaining kg; no current MO is ever a min-run
    dead pair."""
    P = params()
    mos = [current_mo("M1|CUR", "A", 0, 167, 6_000, 0),
           current_mo("M2|CUR", "A", 0, 11, 5_000, 1)]
    dts = [dict(line_id=1, start=2, end=8, reason="Committed CIP")]
    data = mk_data(P, [0, 1], ["A"], mos, downtimes=dts)
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = solve(m)
    assert ok(st), s.StatusName(st)
    assert s.Value(v["produced"][0]) == 6_000 and s.Value(v["run_h"][(0, 0)]) == 6
    assert s.Value(v["produced"][1]) == 5_000
    assert s.Value(v["seg_b_present"][(1, 1)]) == 1
    assert s.Value(v["seg_a_run"][(1, 1)]) <= 2
    assert s.Value(v["seg_a_run"][(1, 1)]) + s.Value(v["seg_b_run"][(1, 1)]) == 5
    assert v["min_run_dead_pairs"] == {} and v["min_run_too_small"] == []


def test_committed_floor_matches_the_old_rule_at_4h_and_below():
    """min(min_run_hours, 4): 1 -> 1, 3 -> 3, 4 -> 4, 8 -> 4 (the legacy
    behaviour at every value the old code was run with); the demand floor is
    the knob itself, at least 1."""
    for k, want in ((0, 0), (1, 1), (3, 3), (4, 4), (8, 4), (12, 4)):
        assert mb.committed_min_run_hours(params(min_run_hours=k)) == want
    assert [mb.demand_min_run_hours(params(min_run_hours=k)) for k in (0, 4, 8)] == [1, 4, 8]


# ══════════════════════════════════════════════════════════════════════════
# Independent validator parity
# ══════════════════════════════════════════════════════════════════════════

TOML_8H = tiv.TOML.replace("min_run_hours = 4", "min_run_hours = 8")


def test_validator_flags_a_6h_demand_block_but_not_8h_or_committed_blocks(tmp_path):
    """World of test_independent_validator with min_run_hours 8 (every
    valid block is >= 10 h). O3 shortened to [157,163) = 6 h -> ONE MIN_RUN
    ERROR. O3 at [157,165) = 8 h -> no MIN_RUN. An E-style committed MO row
    'M1|CUR' of 6 h and a trial row of 6 h -> no MIN_RUN at all (committed
    work keeps the legacy 4 h floor; the old validator reported the |CUR row
    as an ERROR). A 3 h |CUR row -> a WARN only (below the committed floor)."""
    d = tiv.build_world(tmp_path / "w", toml=TOML_8H)
    pd.DataFrame([("M1", "L1", "A", 6000, 0, 119, 1, "manprg"),
                  ("M2", "L2", "B", 1500, 0, 119, 1, "manprg")],
                 columns=["mo", "line_name", "sku", "remaining_kg", "due_start_h",
                          "due_end_h", "locked_line", "source"]
                 ).to_csv(d / "current_mo.csv", index=False)

    def run(o3_end, extra):
        s = tiv.valid_schedule()
        s.loc[3, ["end_hour", "run_hours", "qty_kg"]] = [o3_end, o3_end - 157, (o3_end - 157) * 1000]
        s = pd.concat([s, pd.DataFrame(extra, columns=tiv.SCHED_COLS)], ignore_index=True)
        tiv.write_outputs(d, sched=s)
        return iv.validate_work_dir(d)

    committed = [tiv._row(0, "L1", "M1|CUR", "A", 60, 66, 6000),
                 tiv._row(1, "L2", "T1", "A", 60, 66, 3000, trial=True)]
    rep6 = run(163, committed)
    mr = rep6.by_code("MIN_RUN")
    assert [(v.severity, v.line, v.order, v.hours) for v in mr] == [(iv.ERROR, "L1", "O3", 6.0)]
    assert "min_run_hours 8" in mr[0].detail
    assert any(c.startswith("MIN_RUN (min_run_hours=8 on demand blocks") for c in rep6.checks_run)

    rep8 = run(165, committed)
    assert rep8.by_code("MIN_RUN") == []

    rep3 = run(165, [tiv._row(1, "L2", "M2|CUR", "B", 60, 63, 1500)])
    (w,) = rep3.by_code("MIN_RUN")
    assert (w.severity, w.order, w.hours) == (iv.WARN, "M2|CUR", 3.0)
    assert "committed floor 4h" in w.detail and "[current-state MO]" in w.detail


def test_validator_on_the_repo_toml_uses_8h(tmp_path):
    """The validator reads min_run_hours from the toml as before: with the
    repo's [scheduler] block a 7 h demand block is an ERROR."""
    cfg = _root_cfg()
    d = tiv.build_world(tmp_path / "w")
    s = tiv.valid_schedule()
    s.loc[3, ["end_hour", "run_hours", "qty_kg"]] = [164, 7, 7000]
    tiv.write_outputs(d, sched=s)
    conf = tomllib.loads(tiv.TOML)
    conf["scheduler"]["min_run_hours"] = cfg["scheduler"]["min_run_hours"]
    rep = iv.validate_work_dir(d, config=conf)
    assert [(v.severity, v.order, v.hours) for v in rep.by_code("MIN_RUN")] == [(iv.ERROR, "O3", 7.0)]


# ══════════════════════════════════════════════════════════════════════════
# Scorecard: short_run_h 8.0, strict '<'
# ══════════════════════════════════════════════════════════════════════════

def _blk(bid, start, end, sku, line_id=12):
    return {"block_id": bid, "block_type": "production", "line_id": line_id,
            "line_name": f"P{line_id}", "start_h": float(start), "end_h": float(end),
            "label": sku, "order_id": f"O-{sku}", "sku": sku, "sku_description": "",
            "qty_kg": 1000.0, "locked": False, "attrs": ""}


def test_scorecard_counts_a_7_5h_block_as_short_and_an_8h_block_as_not():
    """Repo toml short_run_h 8.0. Line P12: A [0,7.5), B [10,18) (8.0 h),
    C [20,44). Blocks < 8: only A -> short_run_count 1; campaigns (maximal
    same-SKU runs) are the same three -> short_campaign_count 1. Under the
    old 4.0 both counts were 0."""
    cfg = scorecard_config(_root_cfg())
    cal = pd.DataFrame([_blk("a", 0, 7.5, "A"), _blk("b", 10, 18, "B"),
                        _blk("c", 20, 44, "C")], columns=CALENDAR_COLUMNS)
    cal["qty_kg"] = pd.to_numeric(cal["qty_kg"], errors="coerce").astype(float)
    c = score_campaigns(cal, cfg)
    assert c["short_run_count"] == 1
    assert c["short_campaign_count"] == 1


# ══════════════════════════════════════════════════════════════════════════
# Review fixes SOLVER-1 / TESTS-1 (2026-09-16): hard demand stays FEASIBLE
# when hours the model certainly loses (initial changeover, long-shutdown
# extra, queued locked MOs, sub-floor fragments, a second line's minimum run
# over qty_max) leave no room for the 8 h floor. Every world below was
# INFEASIBLE at relax level 0 before the fixes (the ladder then drops the
# qty_min floor of EVERY order); the controls must stay live and produce.
# ══════════════════════════════════════════════════════════════════════════

def _world_gate_setup(mrh, setup=3, long_extra=0, gate=110):
    """One line gated at `gate` holding SKU X; W0 order Y due [0,119], target
    3,700 at 500 kg/h (qmin 3,330, qmax 4,070). Free [gate, 120)."""
    P = params(min_run_hours=mrh, min_run_pct_of_qty=0.5)
    o = order("Y-W0", "Y", 0, 119, 3_700)
    d = mk_data(P, [0], ["X", "Y"], [o], rate=500.0, avail={0: gate})
    d.init_map[0]["initial_sku"] = "X"
    d.init_map[0]["long_shutdown_flag"] = 1 if long_extra else 0
    d.init_map[0]["long_shutdown_extra"] = long_extra
    d.setup = {("X", "Y"): setup}
    return P, d


def _world_queued_mo(mrh, mo_kg=56_500):
    """Scenario E: a current MO locked to L0 (500 kg/h, window [0,167]) and a
    W0 demand order on the same line due [0,119], target 3,700."""
    P = params(min_run_hours=mrh, min_run_pct_of_qty=0.5)
    mo = current_mo("MO1|Q", "X", 0, 167, mo_kg, 0)
    o = order("X-W0", "X", 0, 119, 3_700)
    return P, mk_data(P, [0], ["X"], [mo, o], rate=500.0)


def _world_two_lines(mrh, gate=110):
    """Two 1,000 kg/h lines gated at `gate`; W0 order due [0,119], target
    12,500 (qmin 11,250, qmax 13,750)."""
    P = params(min_run_hours=mrh, min_run_pct_of_qty=0.0)
    o = order("Z-W0", "Z", 0, 119, 12_500)
    return P, mk_data(P, [0, 1], ["Z"], [o], rate=1000.0,
                      avail={0: gate, 1: gate})


def _hard(P, data):
    m, v = build_model(P, data, "full", False, False, objective_mode="balanced")
    s, st = solve(m)
    assert ok(st), s.StatusName(st)
    return s, v


@pytest.mark.parametrize("setup,long_extra", [(3, 0), (0, 3)])
def test_initial_changeover_hours_count_against_the_minimum_run(setup, long_extra):
    """Gate 110, window ends 120 -> 10 free hours, but the first order on the
    line pays setup(X->Y) 3 h (or a 3 h long-shutdown extra) first:
    seg_a_start >= 113, so only 7 h are left < 8. The pair is 'window'-dead,
    the order is listed and its qty_min is clamped to 0 -> FEASIBLE, nothing
    made. Before: the pair counted as live (10 h >= 8), dead={}, INFEASIBLE."""
    P, d = _world_gate_setup(8, setup=setup, long_extra=long_extra)
    assert mb.demand_line_start_floors(P, d, "full") == {0: 113}
    s, v = _hard(P, d)
    assert v["min_run_dead_pairs"] == {(0, 0): "window"}
    assert [t["order_id"] for t in v["min_run_too_small"]] == ["Y-W0"]
    assert v["min_run_too_small"][0]["longest_free_h"] == 7
    assert s.Value(v["produced"][0]) == 0
    # At the old 4 h floor the same 7 h still host one run: 3,500 kg.
    P4, d4 = _world_gate_setup(4, setup=setup, long_extra=long_extra)
    s4, v4 = _hard(P4, d4)
    assert v4["min_run_dead_pairs"] == {}
    assert s4.Value(v4["produced"][0]) == 3_500


def test_initial_changeover_control_pair_with_room_stays_live():
    """Control: gate 108 + 2 h setup -> [110, 120) = 10 h >= 8 -> live, one
    8 h run of 4,000 kg (qmin 3,330 <= 4,000 <= qmax 4,070)."""
    P, d = _world_gate_setup(8, setup=2, gate=108)
    s, v = _hard(P, d)
    assert v["min_run_dead_pairs"] == {} and v["min_run_too_small"] == []
    assert s.Value(v["produced"][0]) == 4_000


def test_queued_locked_mo_hours_count_against_the_minimum_run():
    """Scenario E: the locked MO must make 56,500 kg at 500 kg/h = 113 h and
    every demand order on the line starts after it -> the demand window is
    [113, 120) = 7 h < 8: 'window'-dead, listed, clamped -> FEASIBLE with
    the MO whole. Before: INFEASIBLE at level 0."""
    P, d = _world_queued_mo(8)
    assert mb.demand_line_start_floors(P, d, "full") == {0: 113}
    # Above level 0 build_model zeroes MO floors: no MO hours are certain.
    assert mb.demand_line_start_floors(P, d, "full", relax_demand=True) == {}
    s, v = _hard(P, d)
    assert v["min_run_dead_pairs"] == {(0, 1): "window"}
    assert [t["order_id"] for t in v["min_run_too_small"]] == ["X-W0"]
    assert [s.Value(v["produced"][i]) for i in (0, 1)] == [56_500, 0]


def test_queued_locked_mo_control_leaves_room_for_one_run():
    """Control: a 50,000 kg MO (100 h) leaves [100, 120) = 20 h -> live:
    MO 50,000 + one 8 h demand run of 4,000."""
    P, d = _world_queued_mo(8, mo_kg=50_000)
    s, v = _hard(P, d)
    assert v["min_run_dead_pairs"] == {}
    assert [s.Value(v["produced"][i]) for i in (0, 1)] == [50_000, 4_000]


def test_mo_floor_uses_due_start_except_in_cross_week_and_skips_unlocked_mos():
    """Floor = max(gate + sum of MO hours, max(start_i + h_i)); start_i =
    max(gate, due_start) except under cross_week (the MO's due start is not
    hard there). An MO with locked_line None is not forced onto the line."""
    P = params(min_run_hours=8)
    mo = current_mo("MO1|Q", "X", 20, 167, 10_000, 0)       # 20 h at 500
    free = dict(current_mo("MO2|Q", "X", 0, 167, 10_000, 0), locked_line=None)
    o = order("X-W0", "X", 0, 119, 3_700)
    d = mk_data(P, [0], ["X"], [mo, free, o], rate=500.0, avail={0: 5})
    assert mb.demand_line_start_floors(P, d, "full") == {0: 40}
    assert mb.demand_line_start_floors(P, d, "full", cross_week=True) == {0: 25}
    # The setup floor needs the changeover phases; sanity1 has none.
    Ps, ds = _world_gate_setup(8)
    assert mb.demand_line_start_floors(Ps, ds, "sanity1") == {}
    assert mb.demand_line_start_floors(Ps, ds, "full", relax_demand=True) == {0: 113}


def test_two_lines_whose_minimum_runs_overshoot_qty_max_clamp_to_one_line():
    """Two lines each free [110, 120) = 10 h at 1,000 kg/h: 10,000 per line,
    20,000 over two. But two 8 h runs make 16,000 > qmax 13,750, so the order
    can use ONE line: the window bound is 10,000 and qmin 11,250 clamps to it
    -> FEASIBLE with 10,000. Before: bound 20,000, no clamp, INFEASIBLE."""
    P, d = _world_two_lines(8)
    o = d.orders[0]
    assert mb._producible_kg_in_window(P, d, o, d.lines, mlpo=2) == 10_000
    s, v = _hard(P, d)
    assert v["min_run_dead_pairs"] == {} and v["min_run_too_small"] == []
    assert s.Value(v["produced"][0]) == 10_000


def test_two_lines_control_with_room_on_one_line():
    """Control: gates at 100 -> 20 h per line; one line holds qmin 11,250."""
    P, d = _world_two_lines(8, gate=100)
    assert mb._producible_kg_in_window(P, d, d.orders[0], d.lines, mlpo=2) == 20_000
    s, v = _hard(P, d)
    assert 11_250 <= s.Value(v["produced"][0]) <= 13_750


def _world_fragments(mrh):
    """One line at 1,000 kg/h, window [0,168): committed CIPs [6,12) and
    [30,36), down [42,168) -> free stretches 6 h, 18 h, 6 h. Hard order
    qmin 24,000, qmax 26,401."""
    P = params(min_run_hours=mrh)
    dts = [dict(line_id=0, start=6, end=12, reason="CIP"),
           dict(line_id=0, start=30, end=36, reason="CIP"),
           dict(line_id=0, start=42, end=168, reason="down")]
    d = mk_data(P, [0], ["F"], [order("F", "F", 0, 167, 24_000, lo=1.0)],
                downtimes=dts)
    return P, d


def test_window_bound_counts_only_stretches_that_can_host_a_minimum_run():
    """TESTS-1: at 8 h only the 18 h stretch can carry a run (the 6 h
    fragments cannot), so the bound is 18,000 and qmin 24,000 clamps to it
    -> FEASIBLE with 18,000. Before: 30 usable hours -> 30,000 >= qmin, no
    clamp, INFEASIBLE. At 4 h seg_a + seg_b reach 6 + 18 = 24 h -> 24,000
    (the two-stretch cap; all three stretches would be 30,000). Committed
    MOs keep the plain usable-hours bound (30,000)."""
    P8, d8 = _world_fragments(8)
    o = d8.orders[0]
    assert mb._free_stretches_h(d8, 0, 0, 168) == [6, 18, 6]
    assert mb._producible_kg_in_window(P8, d8, o, [0]) == 18_000
    mo_like = dict(o, is_current_mo=True, locked_line=0)
    assert mb._producible_kg_in_window(P8, d8, mo_like, [0]) == 30_000
    s8, v8 = _hard(P8, d8)
    assert s8.Value(v8["produced"][0]) == 18_000

    P4, d4 = _world_fragments(4)
    assert mb._producible_kg_in_window(P4, d4, d4.orders[0], [0]) == 24_000
    s4, v4 = _hard(P4, d4)
    assert s4.Value(v4["produced"][0]) == 24_000
