# tests/test_early_fill_policy.py — the plant's early-fill policy, decided by
# the user on 2026-09-04:
#
#   "early fill can go as far into the previous weeks as needed, except
#    cannot be placed before an already scheduled MO."
#
# Implemented as:
#   * Params.early_fill_hours (data_loader): None = unbounded (the default and
#     the plant decision), int = hours before its own due_start ANY demand
#     order may start; [scheduler] early_fill_hours = "unbounded" | "none" |
#     <int> (phase2_scheduler.params_from_config, parse_early_fill_hours).
#   * model_builder.effective_due_start: unbounded -> due-window start floor 0
#     for every demand order; int -> max(0, due_start - h) for every demand
#     order (fix SB-1's "second week only, 48 h" is gone; EARLY_FILL_HOURS = 48
#     stays exported as the documented legacy value).
#   * "never before an already scheduled MO": Scenario F = the line
#     availability gate + committed downtime windows (both already hard);
#     Scenario E = a NEW constraint: every demand order on a line starts at or
#     after the eff_end of every committed current-state MO present there.
#   * The soft "right week first" preference is unchanged: week_dev prices
#     every hour before due_start at objective_week_deviation_weight, and the
#     fill week gradient rewards the nearest due week.
#   * independent_validator: DUE_WINDOW early start -> WARN under the
#     unbounded policy, ERROR beyond a bounded allowance; new check
#     EARLY_BEFORE_COMMITTED for E-style work dirs (tests in
#     tests/test_independent_validator.py and tests/test_fix_INTEGRATE.py).
#
# Every expected value is derived by hand in the docstrings. Models are tiny
# (1-2 lines, <= 3 orders), CP-SAT runs with 2 workers and a <= 15 s cap.

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "code", ROOT / "code" / "solver"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ortools.sat.python import cp_model  # noqa: E402

from data_loader import Data, Files, Params, parse_early_fill_hours  # noqa: E402
import model_builder as mb  # noqa: E402
import phase2_scheduler as p2  # noqa: E402
from model_builder import build_model, effective_due_start  # noqa: E402
from helpers.solver_rules import solver_rules  # noqa: E402


# ── Harness (same shape as tests/test_fix_SB.py) ──────────────────────────

def params(**kw) -> Params:
    """Balanced hard-demand model, live objective weights (makespan 6,
    changeover 120, week deviation 40), idle 0, early fill allowed."""
    base = dict(
        horizon_h=504, min_run_hours=1, min_run_pct_of_qty=0.0,
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


def mk_data(P, lines, skus, orders, *, avail=None, downtimes=None, rate=1000.0):
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = list(lines)
    d.line_names = {l: f"L{l}" for l in lines}
    for l in lines:
        for s in skus:
            d.capable[(l, s)] = 1
            d.rate[(l, s)] = rate
    avail = avail or {}
    d.init_map = {l: {"available_from": avail.get(l, 0), "initial_sku": "CLEAN",
                      "carryover_run_hours": 0, "long_shutdown_flag": 0,
                      "long_shutdown_extra": 0, "last_cip_end_datetime": ""}
                  for l in lines}
    d.downtimes = list(downtimes or [])
    d.cip_interval_map = {l: 100000 for l in lines}   # CIP stand-down
    d.setup = {}
    d.machine_changes = {}
    d.orders = [dict(o) for o in orders]
    return d


def exact(oid, sku, ds, de, kg, **kw):
    return dict(order_id=oid, sku=sku, due_start=ds, due_end=de,
                qty_min=kg, qty_max=kg, qty_target=kg, priority=3, **kw)


def current_mo(oid, sku, ds, de, kg, line):
    """A committed current-state MO locked to `line` (data_loader shape)."""
    return dict(order_id=oid, sku=sku, due_start=ds, due_end=de, qty_min=kg,
                qty_max=kg, qty_target=kg, qty_remaining=kg, priority=0,
                is_current_mo=True, mo_id=oid.split("|")[0], locked_line=line)


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
    return s.Solve(model)


def block(s, v, l, o_idx):
    """(seg_a_start, seg_a_end, seg_b_present, run_h)."""
    k = (l, o_idx)
    return (s.Value(v["seg_a_start"][k]), s.Value(v["seg_a_end"][k]),
            bool(s.Value(v["seg_b_present"][k])), s.Value(v["run_h"][k]))


WED = [("A-W0", 0, 119), ("A-W1", 120, 287), ("A-W2", 288, 455), ("A-W3", 456, 503)]


# ═══════════════════════════════════════════════════════════════════════════
# 1. The knob: Params default, toml parse, config plumbing
# ═══════════════════════════════════════════════════════════════════════════

def test_params_default_is_unbounded():
    """Plant decision 2026-09-04: no key -> None -> unbounded."""
    assert Params().early_fill_hours is None
    assert p2.params_from_config({}).early_fill_hours is None
    assert p2.params_from_config({"scheduler": {}}).early_fill_hours is None


@pytest.mark.parametrize("raw,want", [
    (None, None), ("unbounded", None), ("UNBOUNDED", None), ("none", None),
    ("", None), (48, 48), ("48", 48), (48.0, 48), (0, 0), (24, 24), (47.6, 48),
])
def test_parse_early_fill_hours(raw, want):
    assert parse_early_fill_hours(raw) == want
    assert p2.params_from_config(
        {"scheduler": {"early_fill_hours": raw}}).early_fill_hours == want


@pytest.mark.parametrize("bad", ["soon", -1, True, float("nan"), "48h"])
def test_parse_early_fill_hours_refuses_junk(bad):
    """A typo must not silently become a policy."""
    with pytest.raises(ValueError):
        parse_early_fill_hours(bad)


def test_repo_toml_declares_the_plant_decision():
    """flowstate.toml carries the documented key; the rulebook row resolves
    it and tells the planner about the decision."""
    cfg = tomllib.loads((ROOT / "flowstate.toml").read_text(encoding="utf-8"))
    assert cfg["scheduler"]["early_fill_hours"] == "unbounded"
    assert p2.params_from_config(cfg).early_fill_hours is None
    rows = {r["id"]: r for r in solver_rules(cfg)}
    row = rows["week0_fill_start"]
    assert row["value"] == "unbounded"
    assert "2026-09-04" in row["planner"] and "already" in row["planner"]
    assert "early_fill_hours" in row["where"]
    # absent key -> the stated default, marked as such
    assert {r["id"]: r for r in solver_rules({})}["week0_fill_start"]["value"] == "unbounded (default)"


def test_sub_phase_params_carry_the_knob():
    """Two-phase sub-solves copy every Params field (T-3 C84); the new one too."""
    P = params(early_fill_hours=24)
    sub = p2._sub_phase_params(P, horizon_h=168)
    assert sub.early_fill_hours == 24 and sub.allow_week1_in_week0 is False


# ═══════════════════════════════════════════════════════════════════════════
# 2. The due-window start floor
# ═══════════════════════════════════════════════════════════════════════════

def test_effective_due_start_unbounded_and_bounded():
    """Wednesday frame [0, 120, 288, 456]. Unbounded -> 0 for every demand
    order; 48 -> [0, 72, 240, 408] (every later week, not only the second);
    0 -> own due_start; flag off -> own due_start; a current MO and a trial
    keep their own due_start whatever the knob."""
    orders = [exact(o, "A", ds, de, 1000) for o, ds, de in WED]
    assert [effective_due_start(params(), o) for o in orders] == [0, 0, 0, 0]
    assert [effective_due_start(params(early_fill_hours=48), o) for o in orders] == [0, 72, 240, 408]
    assert [effective_due_start(params(early_fill_hours=0), o) for o in orders] == [0, 120, 288, 456]
    assert [effective_due_start(params(allow_week1_in_week0=False), o) for o in orders] == [0, 120, 288, 456]
    mo = current_mo("MO|CUR", "A", 130, 200, 5000, 0)
    trial = dict(exact("T", "A", 300, 320, 1000), is_trial=True, trial_line=0)
    for P in (params(), params(early_fill_hours=48)):
        assert effective_due_start(P, mo) == 130
        assert effective_due_start(P, trial) == 300


def test_week_dev_exists_for_every_order_that_may_start_early():
    """Default mode: an early IntVar is created exactly for the (line,
    order) pairs whose floor lies before due_start — unbounded: W1, W2, W3
    (W0 opens at 0 anyway); bounded 0: none."""
    orders = [exact(o, "A", ds, de, 1000) for o, ds, de in WED]
    _, v = build_model(params(), mk_data(params(), [0], ["A"], orders), "full", False, False)
    assert sorted(v["week_dev"]) == [(0, 1), (0, 2), (0, 3)]
    P0 = params(early_fill_hours=0)
    _, v = build_model(P0, mk_data(P0, [0], ["A"], orders), "full", False, False)
    assert v["week_dev"] == {}


# ═══════════════════════════════════════════════════════════════════════════
# 3. Scenario F: never before the line's gate or a committed window
# ═══════════════════════════════════════════════════════════════════════════

def test_fill_order_never_starts_before_the_line_gate_even_when_due_later():
    """1 line, gate (available_from) 200, H 504, unbounded early fill. A W2
    order due [288, 455] of 250,000 kg = 250 h must start by 456 - 250 = 206
    and, because the gate is hard, not before 200: start in [200, 206].
    Objective 6 x makespan + 40 x early hours = 6(s + 250) + 40(288 - s) is
    decreasing in s -> start 206, end 456, early 82 h (priced, not banned).
    Pinning the start at 199 (1 h before the gate) is INFEASIBLE.
    Under the OPT-IN soft due weeks (2026-09-04 #2) the producible bound's
    window ends at the horizon for a demand order ([200, 504) = 304,000);
    the hard-wall value 256,000 holds under due_week_policy = "hard", the
    shipped default (asserted both spelled out and left at the default).
    The placement is the same under soft: finishing late instead (start 254
    -> 48 h late) would cost 48,000 kg x 73 per kg-week (2,000 rescaled to
    this balanced branch) on top of the early price, far more than the 82
    early hours."""
    P = params(due_week_policy="soft")
    orders = [exact("A-W2", "A", 288, 455, 250_000)]
    d = mk_data(P, [0], ["A"], orders, avail={0: 200})
    assert mb._producible_kg_in_window(P, d, orders[0], d.lines) == 304_000  # [200, 504)
    for Ph in (params(due_week_policy="hard"), params()):
        assert Ph.due_week_policy == "hard"
        assert mb._producible_kg_in_window(Ph, mk_data(Ph, [0], ["A"], orders, avail={0: 200}),
                                           orders[0], [0]) == 256_000  # [200, 456)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    assert block(s, v, 0, 0) == (206, 456, False, 250)
    assert s.Value(v["week_dev"][(0, 0)][0]) == 82
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["seg_a_start"][(0, 0)] == 199)
    assert status_of(m) == cp_model.INFEASIBLE


def test_fill_order_never_overlaps_a_committed_window_even_when_due_later():
    """1 line, gate 0, committed window [100, 130) (a downtime row: in
    Scenario F the running/queued MOs arrive as such rows), H 504, unbounded.
    A W2 order [288, 455] of 300,000 kg = 300 h needs more than its own
    168 h window: it may reach back, but only to the end of the committed
    window: start in [130, 156] (end <= 456). The price picks the latest:
    start 156, end 456, unsplit. Pinning the start at 129 puts production
    over the committed window -> INFEASIBLE (NoOverlap)."""
    P = params()
    orders = [exact("A-W2", "A", 288, 455, 300_000)]
    dt = [dict(line_id=0, start=100, end=130, reason="Committed MO 4711")]
    d = mk_data(P, [0], ["A"], orders, downtimes=dt)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    assert block(s, v, 0, 0) == (156, 456, False, 300)
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["seg_a_start"][(0, 0)] == 129)
    assert status_of(m) == cp_model.INFEASIBLE
    # ... and the price alone still serves the right week first: with a
    # window that fits (100 h) nothing starts before due_start.
    d = mk_data(P, [0], ["A"], [exact("A-W2", "A", 288, 455, 100_000)], downtimes=dt)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    assert block(s, v, 0, 0)[0] == 288


# ═══════════════════════════════════════════════════════════════════════════
# 4. Scenario E: never before a committed current-state MO on the line
# ═══════════════════════════════════════════════════════════════════════════

def _e_world(P, mo_window=(0, 100)):
    """1 line at 1,000 kg/h; MO 20,000 kg (20 h) locked to line 0 with the
    given due window; a W1 demand order [168, 335] of 10,000 kg (10 h)."""
    ds, de = mo_window
    orders = [current_mo("4711|CUR", "A", ds, de, 20_000, 0),
              exact("B-W1", "B", 168, 335, 10_000)]
    return orders, mk_data(P, [0], ["A", "B"], orders)


def test_demand_never_starts_before_a_committed_mo_on_its_line():
    """Unbounded early fill, MO due [0, 100]. NoOverlap alone would let the
    demand order run [10, 20) with the MO at [20, 40) — the plant forbids it:
    the demand order must start at or after the MO's end (>= 20 whatever the
    MO's own placement, since it runs 20 h from hour 0 at the earliest).
      pin demand start 0  -> INFEASIBLE
      pin demand start 10 -> INFEASIBLE
      pin demand start 20 -> FEASIBLE (MO exactly [0, 20))
    Free solve: the price keeps the demand order in its week (start 168;
    early hours cost 40, makespan 6), the MO runs [0, 20)."""
    P = params(horizon_h=336)
    orders, d = _e_world(P)
    for pin, want in ((0, cp_model.INFEASIBLE), (10, cp_model.INFEASIBLE)):
        m, v = build_model(P, d, "full", False, False)
        m.Add(v["seg_a_start"][(0, 1)] == pin)
        assert status_of(m) == want, pin
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["seg_a_start"][(0, 1)] == 20)
    assert status_of(m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    m, v = build_model(P, d, "full", False, False)
    s = solve(m)
    assert block(s, v, 0, 0) == (0, 20, False, 20)
    assert block(s, v, 0, 1)[0] == 168


def test_committed_mo_floor_holds_for_a_queued_mo_due_later():
    """MO due [50, 100] (a QUEUED manprg MO): the demand order may not slip
    in front of it even though hours 0-50 are free — pin start 0 (would end
    at 10, before the MO's earliest start 50) -> INFEASIBLE; pin start 70 with
    the MO at [50, 70) -> FEASIBLE."""
    P = params(horizon_h=336)
    orders, d = _e_world(P, mo_window=(50, 100))
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["seg_a_start"][(0, 1)] == 0)
    assert status_of(m) == cp_model.INFEASIBLE
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["seg_a_start"][(0, 0)] == 50)
    m.Add(v["seg_a_start"][(0, 1)] == 70)
    assert status_of(m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def test_committed_mo_floor_is_per_line_and_never_binds_absent_pairs():
    """2 lines, MO locked to line 0. The demand order on line 1 is free to
    start at 0 (FEASIBLE) — the floor is per line and gated on both
    presences, so the MO's absence from line 1 does not collapse anything."""
    P = params(horizon_h=336, max_lines_per_order=1)
    orders = [current_mo("4711|CUR", "A", 0, 100, 20_000, 0),
              exact("B-W1", "B", 168, 335, 10_000)]
    d = mk_data(P, [0, 1], ["A", "B"], orders)
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["present"][(1, 1)] == 1)
    m.Add(v["seg_a_start"][(1, 1)] == 0)
    assert status_of(m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    m, v = build_model(P, d, "full", False, False)
    m.Add(v["present"][(0, 1)] == 1)
    m.Add(v["seg_a_start"][(0, 1)] == 0)
    assert status_of(m) == cp_model.INFEASIBLE
