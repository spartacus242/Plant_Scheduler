# tests/test_solver_weights.py — every adjustable solver weight, verified
# end-to-end.
#
# Three layers of proof, each catching a different failure mode:
#   1. Model structure: building a tiny model with one weight doubled must
#      change the CP-SAT proto (a knob that patches the toml but leaves the
#      model byte-identical is a dead knob). The inverse is asserted too:
#      weights the UI documents as mode-gated ("Balanced mode only",
#      "cross-week only", "relax level 2+") must NOT move the model outside
#      their mode — that is what makes the help text honest.
#   2. Solve behavior: min_run_hours / min_run_pct_of_qty /
#      max_lines_per_order are hard rules; a real (tiny) solve must honor
#      them, not just carry them.
#   3. Config plumbing: _patch_work_toml -> flowstate.toml -> _load_config
#      -> params_from_config must land EVERY override key on its Params
#      field, including the floats min_run_pct_of_qty and
#      over_target_reward_pct.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "code" / "solver"))

from ortools.sat.python import cp_model  # noqa: E402

import phase2_scheduler  # noqa: E402
from solver.data_loader import Data, Files, Params  # noqa: E402
from solver.model_builder import build_model  # noqa: E402

from helpers.scenario_runner import (  # noqa: E402
    OVERRIDE_SECTIONS,
    _patch_work_toml,
    normalize_overrides,
)


# ── Tiny model harness ─────────────────────────────────────────────────────

def _tiny_data(P: Params, n_lines: int = 2) -> Data:
    """n lines, two orders (different SKUs), full changeover flags set so
    every per-machine weight has a term to land in."""
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = list(range(n_lines))
    for l in d.lines:
        d.line_names[l] = f"P{l:02d}"
        d.init_map[l] = {"available_from": 0, "initial_sku": "CLEAN",
                         "carryover_run_hours": 0,
                         "long_shutdown_flag": 0, "long_shutdown_extra": 0}
        for sku in ("111", "222"):
            d.capable[(l, sku)] = 1
            d.rate[(l, sku)] = 10.0
    d.downtimes = []
    d.cip_interval_map = {}
    d.setup = {("111", "222"): 2, ("222", "111"): 2}
    full_mc = {"ttp": 1, "ffs": 1, "topload": 1, "casepacker": 1,
               "conv_to_org": 1, "cinn_to_non": 1, "added_flavors": 1}
    d.machine_changes = {("111", "222"): dict(full_mc),
                         ("222", "111"): dict(full_mc)}
    d.orders = [
        dict(order_id="O1", sku="111", due_start=0, due_end=167,
             qty_min=100, qty_max=110, qty_target=100, priority=1),
        dict(order_id="O2", sku="222", due_start=0, due_end=167,
             qty_min=100, qty_max=110, qty_target=100, priority=1),
    ]
    return d


def _base_params(**kw) -> Params:
    P = Params(
        horizon_h=168,
        min_run_hours=1,
        min_run_pct_of_qty=0.0,
        max_lines_per_order=2,
        allow_week1_in_week0=False,
        objective_makespan_weight=6,
        objective_changeover_weight=120,
        objective_cip_defer_weight=10,
        objective_idle_weight=3,
        objective_late_weight=200,
        objective_week_deviation_weight=40,
        objective_cip_flex_weight=20,
        co_topload_weight=50,
        co_ttp_weight=5,
        co_ffs_weight=75,
        co_casepacker_weight=20,
        co_base_weight=5,
        co_conv_org_weight=30,
        co_cinn_weight=20,
        co_flavor_weight=5,
    )
    for k, v in kw.items():
        setattr(P, k, v)
    return P


def _proto_str(P: Params, *, objective_mode: str = "balanced",
               relax_due: bool = False, cross_week: bool = False,
               cip_flex: bool = False) -> str:
    data = _tiny_data(P)
    model, _ = build_model(
        P, data, "full", False, False,
        objective_mode=objective_mode, relax_due=relax_due,
        cross_week=cross_week, cip_flex=cip_flex,
    )
    return str(model.Proto())


def test_model_build_is_deterministic():
    """Control for every proto-diff assertion below: identical Params must
    give a byte-identical model."""
    assert _proto_str(_base_params()) == _proto_str(_base_params())


# (attr, build flags needed for the weight's term to exist at all)
WEIGHT_CASES = [
    ("objective_makespan_weight", {}),
    ("objective_changeover_weight", {}),
    ("objective_idle_weight", {}),
    ("objective_cip_defer_weight", {}),
    ("objective_late_weight", {"relax_due": True}),
    ("objective_week_deviation_weight", {"cross_week": True}),
    ("objective_cip_flex_weight", {"cip_flex": True}),
    ("co_topload_weight", {}),
    ("co_ttp_weight", {}),
    ("co_ffs_weight", {}),
    ("co_casepacker_weight", {}),
    ("co_base_weight", {}),
    ("co_conv_org_weight", {}),
    ("co_cinn_weight", {}),
    ("co_flavor_weight", {}),
    ("co_cip_req_weight", {}),
]


@pytest.mark.parametrize("attr,flags", WEIGHT_CASES,
                         ids=[c[0] for c in WEIGHT_CASES])
def test_doubling_weight_changes_the_model(attr, flags):
    """Every knob must actually reach an objective/constraint coefficient."""
    base = _proto_str(_base_params(), **flags)
    doubled = _proto_str(
        _base_params(**{attr: getattr(_base_params(), attr) * 2}), **flags)
    assert base != doubled, f"{attr} is a dead knob: model unchanged"


@pytest.mark.parametrize("mode", ["min-changeovers", "spread-load"])
@pytest.mark.parametrize("attr", ["objective_makespan_weight",
                                  "objective_changeover_weight"])
def test_balanced_only_weights_ignored_in_fixed_modes(mode, attr):
    """The UI says makespan_weight / changeover_weight are 'Balanced mode
    only' — prove the fixed-multiplier modes really ignore them."""
    base = _proto_str(_base_params(), objective_mode=mode)
    doubled = _proto_str(
        _base_params(**{attr: getattr(_base_params(), attr) * 2}),
        objective_mode=mode)
    assert base == doubled, f"{attr} unexpectedly moves {mode} mode"


@pytest.mark.parametrize("attr,flags", [
    ("objective_late_weight", {}),            # inert below relax level 2
    ("objective_week_deviation_weight", {}),  # inert without cross-week
    ("objective_cip_flex_weight", {}),        # inert without cip-flex
], ids=["late_without_relax_due", "week_dev_without_cross_week",
        "cip_flex_without_mode"])
def test_mode_gated_weights_inert_outside_their_mode(attr, flags):
    base = _proto_str(_base_params(), **flags)
    doubled = _proto_str(
        _base_params(**{attr: getattr(_base_params(), attr) * 2}), **flags)
    assert base == doubled, f"{attr} moves the model outside its mode"


# ── Over-target reward: an honest percentage (2026-08-19) ─────────────────
#
# In the soft-demand fill objective the marginal kg has two prices: a kg
# AT/BELOW target moves both `prodcap_*` (min(produced, target)) and
# `produced_*`, worth _w1 * 1000 = 1_000_000 in the single-week tiny model
# (tier-1 base, week gradient zero); a kg ABOVE target moves only
# `produced_*`, worth exactly the over-target coefficient. pct = X must make
# that coefficient X% of the base — X * 10_000.

def _soft_objective_coeffs(P: Params) -> dict[str, int]:
    """Objective coefficient per variable name for a soft-demand fill model
    (Maximize semantics: positive = reward)."""
    data = _tiny_data(P)
    model, _ = build_model(
        P, data, "full", False, False,
        maximize_production=True, objective_mode="balanced",
    )
    proto = model.Proto()
    coeffs: dict[str, int] = {}
    for i, c in zip(proto.objective.vars, proto.objective.coeffs):
        if i < 0:  # negated variable reference
            i, c = -i - 1, -c
        name = proto.variables[i].name
        coeffs[name] = coeffs.get(name, 0) + c
    if proto.objective.scaling_factor < 0:  # Maximize stored as Minimize(-e)
        coeffs = {k: -v for k, v in coeffs.items()}
    return coeffs


def test_over_target_reward_pct_scales_the_objective_linearly():
    tier1_base = 1_000_000  # _w1 = 1000 (single week) x the x1000 scaling
    c5 = _soft_objective_coeffs(
        _base_params(soft_demand=True, over_target_reward_pct=5.0))
    c25 = _soft_objective_coeffs(
        _base_params(soft_demand=True, over_target_reward_pct=2.5))
    c0 = _soft_objective_coeffs(
        _base_params(soft_demand=True, over_target_reward_pct=0.0))
    # Marginal reward of a kg ABOVE target = the coefficient on produced.
    assert c5["produced_O1"] == 50_000, "5.0 must be exactly 5% of tier-1"
    assert c25["produced_O1"] == 25_000, "2.5 must be exactly half of 5.0"
    assert "produced_O1" not in c0, "0 must remove the term, not just zero it"
    # Marginal reward of a kg at/below target (prodcap + produced move
    # together) stays the full tier-1 base — the pct never touches tier 1.
    for c in (c5, c25, c0):
        assert c.get("prodcap_O1", 0) + c.get("produced_O1", 0) == tier1_base


def test_over_target_reward_default_is_off():
    """Params() must default to 0.0 — every tuned run so far effectively ran
    at ~0.005% (the old x50 vs the x1000 scaling), i.e. no over-fill
    incentive; the honest default preserves that."""
    assert Params().over_target_reward_pct == 0.0


def test_over_target_reward_changes_a_real_solve():
    """Spare line-time sanity: one line, one order, 10h of work in a 168h
    horizon. pct=0 stops at target; pct=5 tops the order up to qty_max."""
    orders = [dict(order_id="O1", sku="111", due_start=0, due_end=167,
                   qty_min=100, qty_max=200, qty_target=100, priority=1)]
    common = dict(soft_demand=True, max_lines_per_order=1,
                  objective_makespan_weight=1, objective_changeover_weight=0,
                  objective_idle_weight=0)

    def _solve(P: Params) -> int:
        data = _tiny_data(P, n_lines=1)
        data.orders = orders
        model, v = build_model(P, data, "sanity1", False, True,
                               maximize_production=True,
                               objective_mode="balanced")
        s = cp_model.CpSolver()
        s.parameters.max_time_in_seconds = 10
        s.parameters.num_search_workers = 4
        status = s.Solve(model)
        assert status == cp_model.OPTIMAL, s.StatusName(status)
        return s.Value(v["produced"][0])

    assert _solve(_base_params(over_target_reward_pct=0.0, **common)) == 100
    assert _solve(_base_params(over_target_reward_pct=5.0, **common)) == 200


# ── Hard solve rules honored by a real solve ──────────────────────────────

def _solve_tiny(P: Params, orders=None, n_lines: int = 2):
    data = _tiny_data(P, n_lines=n_lines)
    if orders is not None:
        data.orders = orders
    # sanity1: no changeover/CIP machinery — isolates the run-bound rules.
    model, v = build_model(P, data, "sanity1", False, True,
                           objective_mode="balanced")
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = 10
    s.parameters.num_search_workers = 4
    status = s.Solve(model)
    assert status == cp_model.OPTIMAL, s.StatusName(status)
    present = {k for k, var in v["present"].items() if s.BooleanValue(var)}
    runs = {k: s.Value(v["run_h"][k]) for k in present}
    return present, runs


def _mk_order(qmin, qmax):
    return [dict(order_id="O1", sku="111", due_start=0, due_end=167,
                 qty_min=qmin, qty_max=qmax, qty_target=qmin, priority=1)]


def test_min_run_hours_is_honored_by_the_solver():
    # 20 kg needs 2h at 10 kg/h — the 8h floor must stretch the run anyway.
    P = _base_params(min_run_hours=8, max_lines_per_order=1,
                     objective_makespan_weight=1,
                     objective_changeover_weight=0, objective_idle_weight=0)
    present, runs = _solve_tiny(P, _mk_order(20, 200), n_lines=1)
    assert list(runs.values()) == [8]
    # Same model, floor 2: makespan pressure shrinks the run to exactly 2h.
    P2 = _base_params(min_run_hours=2, max_lines_per_order=1,
                      objective_makespan_weight=1,
                      objective_changeover_weight=0, objective_idle_weight=0)
    _, runs2 = _solve_tiny(P2, _mk_order(20, 200), n_lines=1)
    assert list(runs2.values()) == [2]


def test_min_run_pct_of_qty_forces_meaningful_split_shares():
    common = dict(objective_makespan_weight=1, objective_changeover_weight=0,
                  objective_idle_weight=0, min_run_hours=1,
                  max_lines_per_order=2)
    # pct=0.1: splitting 100 kg over both lines (5h each) wins on makespan.
    present, runs = _solve_tiny(
        _base_params(min_run_pct_of_qty=0.1, **common), _mk_order(100, 110))
    assert len(present) == 2
    assert sorted(runs.values()) == [5, 5]
    # pct=0.9: each helping line would owe >= 9h (>= 90 kg); two lines would
    # make 180 kg > qty_max 110 — the rule forces a single full run.
    present2, runs2 = _solve_tiny(
        _base_params(min_run_pct_of_qty=0.9, **common), _mk_order(100, 110))
    assert len(present2) == 1
    assert list(runs2.values()) == [10]


def test_max_lines_per_order_caps_parallelism():
    common = dict(objective_makespan_weight=1, objective_changeover_weight=0,
                  objective_idle_weight=0, min_run_hours=1,
                  min_run_pct_of_qty=0.0)
    # mlpo=2: split wins (5h makespan).
    present, _ = _solve_tiny(
        _base_params(max_lines_per_order=2, **common), _mk_order(100, 110))
    assert len(present) == 2
    # mlpo=1: the cap forbids the split even though it is faster.
    present1, runs1 = _solve_tiny(
        _base_params(max_lines_per_order=1, **common), _mk_order(100, 110))
    assert len(present1) == 1
    assert list(runs1.values()) == [10]


# ── Toml plumbing: every UI override key reaches its Params field ─────────

# UI override key -> (toml section, Params attribute, distinctive test value)
KNOB_TO_PARAM = {
    "makespan_weight": ("objective", "objective_makespan_weight", 7),
    "changeover_weight": ("objective", "objective_changeover_weight", 121),
    "cip_defer_weight": ("objective", "objective_cip_defer_weight", 11),
    "idle_weight": ("objective", "objective_idle_weight", 5),
    "late_weight": ("objective", "objective_late_weight", 201),
    "week_deviation_weight": (
        "objective", "objective_week_deviation_weight", 41),
    "cip_flex_weight": ("objective", "objective_cip_flex_weight", 21),
    "over_target_reward_pct": ("objective", "over_target_reward_pct", 2.5),
    "topload_weight": ("changeover", "co_topload_weight", 51),
    "ttp_weight": ("changeover", "co_ttp_weight", 13),
    "ffs_weight": ("changeover", "co_ffs_weight", 76),
    "casepacker_weight": ("changeover", "co_casepacker_weight", 22),
    "base_changeover_weight": ("changeover", "co_base_weight", 6),
    "conv_org_weight": ("changeover", "co_conv_org_weight", 31),
    "cinn_weight": ("changeover", "co_cinn_weight", 23),
    "flavor_weight": ("changeover", "co_flavor_weight", 8),
    "cip_req_weight": ("changeover", "co_cip_req_weight", 1999),
    "min_run_hours": ("scheduler", "min_run_hours", 5),
    "min_run_pct_of_qty": ("scheduler", "min_run_pct_of_qty", 0.35),
    "max_lines_per_order": ("scheduler", "max_lines_per_order", 4),
    "solver_random_seed": ("scheduler", "solver_random_seed", 7),
}


def test_knob_map_covers_every_override_key():
    """A new OVERRIDE_SECTIONS key without a Params mapping here is exactly
    the silent-no-op trap this file exists to catch."""
    assert set(KNOB_TO_PARAM) == set(OVERRIDE_SECTIONS)
    for key, (section, _, _) in KNOB_TO_PARAM.items():
        assert OVERRIDE_SECTIONS[key] == section


def test_patched_toml_reaches_params_for_every_knob(tmp_path):
    toml = tmp_path / "flowstate.toml"
    toml.write_text("[scheduler]\ntime_limit = 60\n", encoding="utf-8")
    overrides = {k: v for k, (_, _, v) in KNOB_TO_PARAM.items()}
    _patch_work_toml(toml, 90, overrides)

    cfg = phase2_scheduler._load_config(toml, tmp_path)
    assert cfg["scheduler"]["time_limit"] == 90
    for key, (section, _, value) in KNOB_TO_PARAM.items():
        assert cfg[section][key] == value, f"{key} missing from patched toml"

    P = phase2_scheduler.params_from_config(cfg)
    for key, (_, attr, value) in KNOB_TO_PARAM.items():
        got = getattr(P, attr)
        assert got == pytest.approx(value), (
            f"{key}: patched toml value {value} never reached Params.{attr} "
            f"(got {got})")


def test_cli_overrides_beat_toml_in_params_from_config():
    cfg = {"scheduler": {"min_run_hours": 4, "max_lines_per_order": 2}}
    P = phase2_scheduler.params_from_config(
        cfg, max_lines_override=5, min_run_override=7, allow_week1=False)
    assert P.max_lines_per_order == 5
    assert P.min_run_hours == 7
    assert P.allow_week1_in_week0 is False


# ── CP-SAT random seed: toml -> Params -> solver.parameters ───────────────
#
# random_seed is a SEARCH parameter, not a model coefficient — it can never
# appear in the model proto, so the WEIGHT_CASES proto-diff harness cannot
# see it. The honest assertion is on the CpSolver's parameters proto:
# apply_solver_seed is the one choke point every solve pass calls.

def test_solver_seed_reaches_cpsat_parameters():
    P = Params(solver_random_seed=7)
    s = cp_model.CpSolver()
    phase2_scheduler.apply_solver_seed(s, P)
    assert s.parameters.random_seed == 7


def test_solver_seed_absent_leaves_cpsat_default_untouched():
    """No seed in Params must mean NO write at all — CP-SAT's own default
    stays in force, so every existing config behaves byte-identically.
    (ortools 9.15's parameters wrapper has no HasField, so the assertion is
    against a fresh solver's default value.)"""
    default = cp_model.CpSolver().parameters.random_seed
    s = cp_model.CpSolver()
    phase2_scheduler.apply_solver_seed(s, Params())
    assert s.parameters.random_seed == default
    assert Params().solver_random_seed is None


def test_solver_seed_toml_to_params():
    cfg = {"scheduler": {"solver_random_seed": 3}}
    assert phase2_scheduler.params_from_config(cfg).solver_random_seed == 3
    assert phase2_scheduler.params_from_config({}).solver_random_seed is None


def test_normalize_overrides_keeps_float_pct_and_drops_junk():
    clean = normalize_overrides({
        "min_run_pct_of_qty": 0.35,      # must stay a float, not int-> 0
        "over_target_reward_pct": 2.5,   # must stay a float, not int-> 2
        "min_run_hours": "6",
        "max_lines_per_order": 3.0,
        "makespan_weight": 9,
        "unknown_key": 5,                # silently dropped
        "late_weight": None,             # dropped
    })
    assert clean == {
        "min_run_pct_of_qty": pytest.approx(0.35),
        "over_target_reward_pct": pytest.approx(2.5),
        "min_run_hours": 6,
        "max_lines_per_order": 3,
        "makespan_weight": 9,
    }
    assert isinstance(clean["min_run_pct_of_qty"], float)
    assert isinstance(clean["over_target_reward_pct"], float)
