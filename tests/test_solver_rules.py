# tests/test_solver_rules.py — the solver rulebook (helpers/solver_rules.py)
# stays complete, well-formed, and in step with the override plumbing.

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.scenario_runner import OVERRIDE_SECTIONS  # noqa: E402
from helpers.solver_rules import (  # noqa: E402
    GROUP_FIXED,
    GROUP_KNOB,
    solver_rules,
)

_UI_LEVELS = {"exposed", "easy", "needs care", "don't expose"}

# Rules the task explicitly demanded be inventoried — losing one of these
# rows means the rulebook stopped telling planners the truth.
_REQUIRED_IDS = {
    # knobs
    "makespan_weight", "changeover_weight", "idle_weight",
    "cip_defer_weight", "late_weight", "week_deviation_weight",
    "cip_flex_weight", "topload_weight", "ffs_weight", "ttp_weight",
    "casepacker_weight", "base_changeover_weight", "conv_org_weight",
    "cinn_weight", "flavor_weight",
    "min_run_hours", "min_run_pct_of_qty", "max_lines_per_order",
    # fixed rules
    "week_grid", "week0_fill_start", "week_stitch", "relax_ladder",
    "producible_zeroing", "availability_gate", "cip_max_interval",
    "cip_duration", "cip_max_count", "cip_absorb", "co_fallback",
    "min_co_multipliers", "spread_multipliers", "soft_demand_tiers",
    "week_gradient", "cur_mo_priority", "two_pass_epsilon", "horizon",
    "warm_start_gates", "f_staging", "now_floor", "cip_standdown",
    "demand_netting", "trials_blocked", "greedy_seed", "line_rates",
    "scorecard_co_fallbacks",
}


def test_rulebook_is_well_formed():
    rules = solver_rules()
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids)), "duplicate rule ids"
    for r in rules:
        for key in ("id", "group", "name", "planner", "where", "ui"):
            assert r.get(key), f"rule {r.get('id')} missing {key}"
        assert r["group"] in (GROUP_KNOB, GROUP_FIXED)
        assert r["ui"] in _UI_LEVELS
        assert "value" in r, f"rule {r['id']} resolved without a value"


def test_required_rules_present():
    ids = {r["id"] for r in solver_rules()}
    missing = _REQUIRED_IDS - ids
    assert not missing, f"rulebook lost required rows: {sorted(missing)}"


def test_every_override_key_is_documented_as_a_knob():
    """Every key the custom-scenario UI can patch into the work toml must
    appear in the rulebook's adjustable-knob group — an undocumented knob
    and a documented non-knob are both lies."""
    knob_ids = {r["id"] for r in solver_rules() if r["group"] == GROUP_KNOB}
    missing = set(OVERRIDE_SECTIONS) - knob_ids
    assert not missing, f"override keys missing from rulebook: {missing}"


def test_config_backed_rules_resolve_live_values():
    cfg = {
        "objective": {"makespan_weight": 6},
        "scheduler": {"min_run_hours": 4},
        "cip": {"duration_h": 8},
    }
    rows = {r["id"]: r for r in solver_rules(cfg)}
    assert rows["makespan_weight"]["value"] == 6
    assert rows["min_run_hours"]["value"] == 4
    assert rows["cip_duration"]["value"] == 8
    # absent key -> stated default, marked as such
    assert rows["changeover_weight"]["value"] == "100 (default)"


def test_config_dotted_keys_match_override_sections():
    """A knob whose config path disagrees with the override plumbing would
    render a value the solve never uses."""
    rows = {r["id"]: r for r in solver_rules()}
    for key, section in OVERRIDE_SECTIONS.items():
        assert rows[key].get("config") == f"{section}.{key}", (
            f"rulebook config path for {key} out of step with "
            f"OVERRIDE_SECTIONS ({section})")
