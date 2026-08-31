# helpers/scenario_runner.py - Phase 2: generate solver scenarios A-D vs the current schedule.
#
# Wraps the CP-SAT solver (code/solver/) with different objective modes, imports
# results into calendar_blocks shape, and scores with the same scorecard engine.

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from helpers.calendar_io import import_solver_schedule
from helpers.scorecard_engine import score_calendar
from helpers.version_manager import list_versions, save_version

# ---------------------------------------------------------------------------
# Solver knob documentation
#
# Every knob below is a real input to the CP-SAT objective built in
# code/solver/model_builder.py (the objective branches live around
# lines 1005-1127).  Knobs sourced from flowstate.toml are declared with a
# "config" key of the form "<section>.<key>"; their live value is resolved by
# scenario_knobs() against the loaded toml.  Knobs with a literal "value" are
# hard-coded multipliers inside the objective branch and are NOT tunable.
# ---------------------------------------------------------------------------

# Weighted changeover cost is assembled from the per-machine weights below; it
# feeds every objective mode, so it is shown for all scenarios.
_CO_WEIGHT_KNOBS = [
    {
        "param": "changeover.topload_weight",
        "config": "changeover.topload_weight",
        "effect": "Cost of a topload (carton/format) change on a line. High = campaign formats together.",
    },
    {
        "param": "changeover.ffs_weight",
        "config": "changeover.ffs_weight",
        "effect": "Cost of a form-fill-seal film change. High = avoid film swaps.",
    },
    {
        "param": "changeover.ttp_weight",
        "config": "changeover.ttp_weight",
        "effect": "Cost of a tray/thermoform (TTP) change.",
    },
    {
        "param": "changeover.casepacker_weight",
        "config": "changeover.casepacker_weight",
        "effect": "Cost of a case-packer pattern change.",
    },
    {
        "param": "changeover.base_changeover_weight",
        "config": "changeover.base_changeover_weight",
        "effect": "Flat cost charged for any SKU switch, on top of the per-machine costs.",
    },
    {
        "param": "changeover.conv_org_weight",
        "config": "changeover.conv_org_weight",
        "effect": "Extra cost for a conventional <-> organic switch (allergen/clean-down risk).",
    },
    {
        "param": "changeover.cinn_weight",
        "config": "changeover.cinn_weight",
        "effect": "Extra cost for switching in/out of cinnamon (carry-over risk).",
    },
    {
        "param": "changeover.flavor_weight",
        "config": "changeover.flavor_weight",
        "effect": "Extra cost for a plain flavour-to-flavour switch.",
    },
    {
        "param": "changeover.cip_req_weight",
        "config": "changeover.cip_req_weight",
        "effect": ("Penalty for running a cip_req_after SKU pair without a "
                   "CIP between the runs (fully waived at a CIP window)."),
    },
]

# Terms present in every objective branch.
_COMMON_KNOBS = [
    {
        "param": "objective.idle_weight",
        "config": "objective.idle_weight",
        "effect": "Penalty per idle hour on a line. Raise to pack runs tighter; 0 disables idle tracking.",
    },
    {
        "param": "objective.cip_defer_weight",
        "config": "objective.cip_defer_weight",
        "effect": "Reward per hour a CIP STARTS LATER — CIPs drift toward their legal deadline instead of being dumped at hour 0. The line's max CIP interval stays hard.",
    },
    {
        "param": "objective.late_weight",
        "config": "objective.late_weight",
        "effect": "Penalty per hour an order finishes past its due date (active from relax level 2).",
    },
    {
        "param": "objective.week_deviation_weight",
        "config": "objective.week_deviation_weight",
        "effect": "Cross-week mode only: penalty per hour an order runs outside AZAP's requested week. Lower = more willing to move a SKU between week 1 and week 2 to build a longer run.",
    },
    # Hard-rule overrides (custom builder 2026-08-19) — shown here so the
    # "What this scenario tunes" table matches every knob the UI exposes.
    {
        "param": "scheduler.min_run_hours",
        "config": "scheduler.min_run_hours",
        "effect": "HARD floor: no new production run shorter than this many hours (committed MOs exempt).",
    },
    {
        "param": "scheduler.min_run_pct_of_qty",
        "config": "scheduler.min_run_pct_of_qty",
        "effect": "HARD floor on multi-line splits: each line's share of an order runs at least this fraction of the order's minimum quantity.",
    },
    {
        "param": "scheduler.max_lines_per_order",
        "config": "scheduler.max_lines_per_order",
        "effect": "HARD cap: how many lines may run the same ORDER (SKU-week row) simultaneously. Per order, not per SKU.",
    },
    {
        "param": "objective.cip_flex_weight",
        "config": "objective.cip_flex_weight",
        "effect": "CIP flexibility mode only: percent the cip_defer reward is scaled to, so a CIP can be pulled EARLIER to absorb a changeover. The line's max allowable CIP interval stays HARD.",
    },
    {
        "param": "objective.over_target_reward_pct",
        "config": "objective.over_target_reward_pct",
        "effect": "Soft-demand (F) only: reward for kg ABOVE an order's target (toward its max), as a % of the per-kg value of real demand. 0 = never overproduce on purpose. NOT a fulfillment gain — fulfillment is capped at target; the extra kg build inventory.",
    },
]

_FORMULA_MIN_CO = (
    "minimize  100 * weighted_changeover_cost"
    " + makespan"
    " + idle_h * idle_weight"
    " - cip_deferred_h * cip_defer_weight"
    " + late_h * late_weight"
    "\n(no per-machine weights available -> 10000 * changeover_count replaces the first term)"
)

_FORMULA_SPREAD = (
    "minimize  1000 * max_line_run_h"
    " + weighted_changeover_cost"
    " + makespan"
    " + idle_h * idle_weight"
    " - cip_deferred_h * cip_defer_weight"
    " + late_h * late_weight"
    "\n(no per-machine weights available -> 10 * changeover_count replaces the changeover term)"
)

_FORMULA_BALANCED = (
    "minimize  makespan * makespan_weight"
    " + weighted_changeover_cost * changeover_weight"
    " + idle_h * idle_weight"
    " - cip_deferred_h * cip_defer_weight"
    " + late_h * late_weight"
)

_KNOBS_MIN_CO = [
    {
        "param": "weighted changeover multiplier",
        "value": 100,
        "effect": "Hard-coded in min-changeovers mode: changeover cost dominates everything else.",
    },
    {
        "param": "makespan coefficient",
        "value": 1,
        "effect": "Fixed at 1 in this mode (makespan_weight is IGNORED) - schedule length is only a tiebreaker.",
    },
    {
        "param": "objective.changeover_weight",
        "value": "not used",
        "effect": "Ignored in min-changeovers mode; the fixed 100x multiplier is used instead.",
    },
] + _COMMON_KNOBS + _CO_WEIGHT_KNOBS

_KNOBS_SPREAD = [
    {
        "param": "max_line_run multiplier",
        "value": 1000,
        "effect": "Hard-coded: minimizes the busiest line's total run hours, i.e. levels load across lines.",
    },
    {
        "param": "weighted changeover multiplier",
        "value": 1,
        "effect": "Changeovers are only a light tiebreaker here, so lines stay loaded.",
    },
    {
        "param": "objective.makespan_weight / changeover_weight",
        "value": "not used",
        "effect": "Both are IGNORED in spread-load mode; coefficients are fixed by the branch.",
    },
] + _COMMON_KNOBS + _CO_WEIGHT_KNOBS

_KNOBS_BALANCED = [
    {
        "param": "objective.makespan_weight",
        "config": "objective.makespan_weight",
        "effect": "Weight on total schedule length. Raise to finish the week earlier.",
    },
    {
        "param": "objective.changeover_weight",
        "config": "objective.changeover_weight",
        "effect": "Multiplier on the weighted changeover cost. Raise to trade makespan for fewer switches.",
    },
] + _COMMON_KNOBS + _CO_WEIGHT_KNOBS

# Flat override keys the UI/caller may supply, mapped to their toml section.
OVERRIDE_SECTIONS: dict[str, str] = {
    "makespan_weight": "objective",
    "changeover_weight": "objective",
    "cip_defer_weight": "objective",
    "idle_weight": "objective",
    "late_weight": "objective",
    "week_deviation_weight": "objective",
    "cip_flex_weight": "objective",
    "over_target_reward_pct": "objective",
    "base_changeover_weight": "changeover",
    "topload_weight": "changeover",
    "ttp_weight": "changeover",
    "ffs_weight": "changeover",
    "casepacker_weight": "changeover",
    "conv_org_weight": "changeover",
    "cinn_weight": "changeover",
    "flavor_weight": "changeover",
    "cip_req_weight": "changeover",
    # Solve rules (hard constraints), not objective weights. They ride the
    # same toml patch: [scheduler] -> phase2_scheduler.params_from_config -> P.
    "min_run_hours": "scheduler",
    "min_run_pct_of_qty": "scheduler",
    "max_lines_per_order": "scheduler",
    # CP-SAT random seed (search diversity, not a weight): same toml ride,
    # applied to every solve pass via phase2_scheduler.apply_solver_seed.
    # Absent = CP-SAT's own default seed.
    "solver_random_seed": "scheduler",
}

# Overrides that are fractions, not integer weights — normalize_overrides
# must not truncate them (int("0.5") -> 0 would silently erase the floor;
# int(2.5) -> 2 would silently move the over-target reward).
FLOAT_OVERRIDE_KEYS = {"min_run_pct_of_qty", "over_target_reward_pct"}

OBJECTIVE_MODES = ("balanced", "min-changeovers", "spread-load")

# Fallbacks matching Params in code/solver/data_loader.py, used when a
# key is absent from flowstate.toml so the knob table never shows a blank.
SOLVER_DEFAULTS: dict[str, float] = {
    "objective.makespan_weight": 1,
    "objective.changeover_weight": 100,
    "objective.cip_defer_weight": 10,
    "objective.idle_weight": 0,
    "objective.late_weight": 200,
    "objective.week_deviation_weight": 40,
    "objective.cip_flex_weight": 20,
    "objective.over_target_reward_pct": 0.0,
    "changeover.topload_weight": 50,
    "changeover.ttp_weight": 10,
    "changeover.ffs_weight": 10,
    "changeover.casepacker_weight": 10,
    "changeover.base_changeover_weight": 5,
    "changeover.conv_org_weight": 30,
    "changeover.cinn_weight": 20,
    "changeover.flavor_weight": 5,
    "changeover.cip_req_weight": 2000,
}

CUSTOM_SCENARIO_ID = "X"

# Scenario definitions — mapped to legacy --objective modes (+ notes).
SCENARIOS = [
    {
        "id": "A",
        "name": "Scenario A — Minimum changeovers",
        "objective": "min-changeovers",
        "two_phase": True,
        "intent": "Minimize SKU switches above all else.",
        "objective_formula": _FORMULA_MIN_CO,
        "knobs": _KNOBS_MIN_CO,
    },
    {
        "id": "B",
        "name": "Scenario B — Maximum throughput",
        "objective": "spread-load",
        "two_phase": True,
        "intent": "Spread load / keep lines productive (proxy for throughput).",
        "objective_formula": _FORMULA_SPREAD,
        "knobs": _KNOBS_SPREAD,
    },
    {
        "id": "C",
        "name": "Scenario C — Maintenance friendly",
        "objective": "balanced",
        "two_phase": True,
        "intent": (
            "Balanced solve; planner should align maintenance with CIP in the twin. "
            "v0 uses balanced weights — tune CIP defer / idle in legacy config for stronger maint bias."
        ),
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
    {
        "id": "D",
        "name": "Scenario D — Balanced",
        "objective": "balanced",
        "two_phase": True,
        "intent": "Minimize makespan + weighted changeovers (recommended default).",
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
    {
        "id": CUSTOM_SCENARIO_ID,
        "name": "Custom scenario",
        "objective": "balanced",
        "two_phase": True,
        "intent": "Planner-defined objective mode and weight overrides.",
        "custom": True,
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
    {
        "id": "F",
        "name": "Scenario F — Fill the tail (committed plan fixed)",
        "objective": "balanced",
        # Single-phase: the committed layer spans arbitrary hours; fill
        # placement must see the whole horizon at once.
        "two_phase": False,
        "fill_mode": True,
        # Soft demand: every kg short of qty_min is penalized, so filling
        # W34/W35 always pays — no all-or-nothing relax ladder needed.
        "soft_demand": True,
        # Two-pass: after the fill-maximizing solve, a second full-budget
        # pass minimizes the weighted changeover load holding fill >= pass1
        # minus 1% (measured: price pressure alone never cut topload count).
        "two_pass_co": True,
        # Pack tails: idle time between fill blocks is the enemy of the
        # user's "never leave large blocks of idle time" rule.
        # Changeover pressure (user, 2026-08-14): FFS & topload swaps are
        # the expensive ones — push the solver hard on those specifically.
        # At weight 300 a topload swap costs ~46 kg of tier-1 production
        # equivalent (FFS ~61 kg, TTP ~3 kg): strong resequencing pressure
        # that still never trades away meaningful target tonnage.
        "overrides": {"idle_weight": 3, "changeover_weight": 300,
                      "topload_weight": 450, "ffs_weight": 600},
        "intent": (
            "The user's process (2026-08-14): manprg + cip_info lay out the "
            "committed plan as FIXED line-time (running + queued MOs, trials, "
            "CIPs projected at each line's max interval through the horizon). "
            "The solver only fills the remaining time with demand_plan "
            "orders — right tonnage on the right ISO week, minimum "
            "changeovers, tails packed. The plant's own plan is never "
            "rewritten: no current_mo.csv, no mo_changes."
        ),
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
    {
        "id": "E",
        "name": "Scenario E — Current state + demand",
        "objective": "balanced",
        # Single-phase (full 336h in one model). The two-phase driver splits
        # week 0 (168h) from week 1, which structurally cannot place current
        # MOs whose remaining work spans the whole horizon. Single-phase
        # keeps them on their locked line and lets mo_changes.csv record the
        # split/trim/reorder for VIF write-back.
        "two_phase": False,
        "intent": (
            "Re-optimize the current plant state: manprg running/queued MOs "
            "are locked to their line (committed work), tonnage adjustable; "
            "new demand meshes into the tail. mo_changes.csv records every "
            "change for VIF write-back."
        ),
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
]

# Formula / knob table per objective mode — used to re-describe a custom
# scenario when the planner picks a different mode.
MODE_DOCS: dict[str, dict[str, Any]] = {
    "min-changeovers": {"objective_formula": _FORMULA_MIN_CO, "knobs": _KNOBS_MIN_CO},
    "spread-load": {"objective_formula": _FORMULA_SPREAD, "knobs": _KNOBS_SPREAD},
    "balanced": {"objective_formula": _FORMULA_BALANCED, "knobs": _KNOBS_BALANCED},
}


def make_custom_scenario(
    name: str,
    objective: str,
    overrides: dict[str, Any] | None = None,
    *,
    two_phase: bool = True,
    cross_week: bool = False,
    cip_flex: bool = False,
) -> dict[str, Any]:
    """Build a runnable CUSTOM scenario dict from planner input."""
    mode = objective if objective in OBJECTIVE_MODES else "balanced"
    docs = MODE_DOCS[mode]
    return {
        "id": CUSTOM_SCENARIO_ID,
        "name": (name or "Custom scenario").strip() or "Custom scenario",
        "objective": mode,
        "two_phase": two_phase,
        "cross_week": bool(cross_week),
        "cip_flex": bool(cip_flex),
        "custom": True,
        "intent": f"Custom solve ({mode}) with planner weight overrides.",
        "objective_formula": docs["objective_formula"],
        "knobs": docs["knobs"],
        "overrides": dict(overrides or {}),
    }


def config_value(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Look up '<section>.<key>' in a loaded flowstate.toml dict."""
    section, _, key = dotted.partition(".")
    return (cfg.get(section) or {}).get(key, default)


def scenario_knobs(
    scenario: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Resolve a scenario's knob table into concrete (param, value, effect) rows."""
    cfg = cfg or {}
    overrides = overrides or {}
    rows: list[dict[str, Any]] = []
    for knob in scenario.get("knobs", []):
        dotted = knob.get("config")
        if dotted:
            key = dotted.partition(".")[2]
            if key in overrides and overrides[key] is not None:
                value = overrides[key]
            else:
                section = dotted.partition(".")[0]
                if key in (cfg.get(section) or {}):
                    value = cfg[section][key]
                else:
                    fallback = SOLVER_DEFAULTS.get(dotted)
                    value = (
                        f"{fallback} (solver default)" if fallback is not None else "(solver default)"
                    )
        else:
            value = knob.get("value", "")
        rows.append(
            {
                "Parameter": knob["param"],
                # Stringified: the column mixes ints and notes like "not used",
                # and a mixed-type column fails Arrow serialization in Streamlit.
                "Value": str(value),
                "What it does": knob["effect"],
            }
        )
    return rows


def _prepare_work_dir(data_dir: Path, work: Path) -> None:
    """Copy the solver's data inputs into a work directory.

    De-legacy (2026-08-10): the solver reads ONLY from its work dir, and the
    work dir is sourced exclusively from data/reference/ — no more copies out
    of Flowstate-legacy/data. All solver inputs are already in reference/.
    """
    if work.exists():
        # Warm start (item 10): the previous run's schedule is the best
        # available hint for this one, but the wipe below would destroy it.
        # Carry it across as prev_schedule.csv; the solver hints from it and
        # CP-SAT repairs anything stale, so a wrong carry-over can only cost
        # search time, never correctness.
        _prev = work / "schedule_phase2.csv"
        _carry: bytes | None = None
        if _prev.exists():
            try:
                _carry = _prev.read_bytes()
            except OSError:
                _carry = None
        # The previous run's relax level travels WITH the schedule: the solver
        # skips warm-start hints when the previous plan was solved at a MORE
        # relaxed level than the current attempt (hints from a changeover-
        # ignoring schedule starve a changeover-enforcing search — measured
        # 2026-08-14). Without this carry the wipe below deleted the report
        # and the gate silently never fired.
        _prev_feas = work / "feasibility_report.json"
        _carry_feas: bytes | None = None
        if _prev_feas.exists():
            try:
                _carry_feas = _prev_feas.read_bytes()
            except OSError:
                _carry_feas = None
        shutil.rmtree(work)
        work.mkdir(parents=True)
        if _carry is not None:
            try:
                (work / "prev_schedule.csv").write_bytes(_carry)
            except OSError:
                pass
        if _carry_feas is not None:
            try:
                (work / "prev_feasibility.json").write_bytes(_carry_feas)
            except OSError:
                pass
    else:
        work.mkdir(parents=True)

    ref = data_dir / "reference"
    root_toml = data_dir.parent / "flowstate.toml"

    mapping = {
        "changeovers.csv": "changeovers.csv",
        "downtimes.csv": "downtimes.csv",
        "demand_plan.csv": "demand_plan.csv",
        "capabilities_rates.csv": "capabilities_rates.csv",
        "line_cip_hrs.csv": "line_cip_hrs.csv",
        "line_rates.csv": "line_rates.csv",
        "sku_info.csv": "sku_info.csv",
        "initial_states.csv": "initial_states.csv",
    }
    missing: list[str] = []
    for src_name, dst_name in mapping.items():
        src = ref / src_name
        if not src.exists():
            missing.append(src_name)
        elif src_name == "downtimes.csv":
            # The reference file stores wall-clock datetimes; the solver
            # speaks hours. Derive the work-dir copy against the root toml
            # anchor (the work frame until _stage_time_frame moves it).
            from helpers.config import load_toml as _lt
            from helpers.downtime_store import stage_solver_downtimes
            from helpers.timefmt import planning_anchor as _pa

            stage_solver_downtimes(
                src, work / dst_name,
                _pa(_lt(root_toml) if root_toml.exists() else None))
        else:
            shutil.copy2(src, work / dst_name)

    if root_toml.exists():
        shutil.copy2(root_toml, work / "flowstate.toml")

    if missing:
        # The solver's data_loader raises on missing files; the caller turns
        # that into a visible "solver failed" — but be loud about it here too.
        import warnings

        warnings.warn(
            f"Solver work dir missing reference inputs: {', '.join(missing)}"
        )


def _stage_time_frame(work: Path, dd: Path, hz) -> list[str]:
    """Put every staged input into ONE time frame (audit 2026-08-15).

    Three anchors coexist: the demand file's self-anchor
    (demand_plan.source.json), the root toml's planning_start_date, and the
    resolved horizon anchor (anchor_mode="today"). Staging without
    reconciling them put gates in the today-frame while demand due windows
    sat in the demand frame — up to 264h apart. This helper (called by BOTH
    overlays, so scenarios A-E get it too, not just F):
      1. rewrites the work toml's planning_start_date to hz.anchor,
      2. re-bases demand_plan.csv due windows into the hz.anchor frame,
         dropping weeks that ended before the horizon (misses), and
      3. re-derives the work downtimes.csv from the wall-clock reference
         file so its hours are in the hz.anchor frame too (the copy staged
         by _prepare_work_dir spoke the root-toml frame).
    """
    import json as _json
    from datetime import datetime as _dt

    import pandas as _pd

    from helpers.plan_fill import rebase_demand

    notes: list[str] = []
    try:
        import tomllib as _tl
        import tomli_w as _tw
        _tp = work / "flowstate.toml"
        with open(_tp, "rb") as _fh:
            _tcfg = _tl.load(_fh)
        _tcfg["planning_start_date"] = hz.anchor.strftime("%Y-%m-%d %H:%M:%S")
        with open(_tp, "wb") as _fh:
            _tw.dump(_tcfg, _fh)
    except Exception:  # noqa: BLE001 — stamp cosmetics must not kill a solve
        pass

    dem_path = work / "demand_plan.csv"
    src_meta = dd / "reference" / "demand_plan.source.json"
    if dem_path.exists() and src_meta.exists():
        try:
            _da = str(_json.loads(
                src_meta.read_text(encoding="utf-8")).get("anchor") or "")
            shift_h = ((hz.anchor - _dt.strptime(
                _da, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600.0
                if _da else 0.0)
        except (OSError, ValueError):
            shift_h = 0.0
        if shift_h:
            dem = _pd.read_csv(dem_path, dtype={"sku": str})
            dem, rb_notes = rebase_demand(dem, shift_h, float(hz.hours))
            dem.to_csv(dem_path, index=False)
            notes.append(f"demand due windows re-based {shift_h:+.0f}h "
                         "(demand anchor -> staging anchor)")
            notes.extend(rb_notes)

    # Downtimes are wall-clock truth; only their HOUR VIEW depends on the
    # frame. Re-derive the staged copy so hour 0 = hz.anchor. This must run
    # BEFORE the overlays append trial/committed windows (they already speak
    # the hz frame) — both overlays call this helper first, so it does.
    dt_src = dd / "reference" / "downtimes.csv"
    if dt_src.exists():
        from helpers.downtime_store import stage_solver_downtimes

        n_dt = stage_solver_downtimes(dt_src, work / "downtimes.csv", hz.anchor)
        notes.append(f"downtimes derived from wall-clock file ({n_dt} row(s), "
                     f"hour 0 = {hz.anchor:%Y-%m-%d %H:%M})")
    return notes


def _audit_work_downtimes(work: Path) -> list[str]:
    """Report downtime windows that went stale when the horizon grew.

    Reads the work-dir downtimes.csv and flowstate.toml (both already staged)
    so the audit sees exactly what the solver will see. Returns note strings
    that the caller prepends to the run log -- always a list, never silent.
    """
    from helpers.downtime_horizon import audit_downtime_horizon

    import pandas as pd

    dt_path = work / "downtimes.csv"
    if not dt_path.exists():
        return []
    horizon = 0.0
    toml_path = work / "flowstate.toml"
    if toml_path.exists():
        try:
            import tomllib

            cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            sch = cfg.get("scheduler", {}) or {}
            horizon = float(sch.get("horizon_hours")
                            or (float(sch.get("horizon_weeks", 0) or 0) * 168.0))
        except Exception:  # noqa: BLE001
            horizon = 0.0
    if horizon <= 0:
        return []
    rows = pd.read_csv(dt_path, encoding="utf-8-sig", dtype=str,
                       keep_default_na=False).to_dict("records")
    return audit_downtime_horizon(rows, horizon)


def _overlay_current_state(
    work: Path, data_dir: Path, lock_current_mo: bool | None = None
) -> list[str]:
    """Patch the work-dir initial_states.csv with the real plant state.

    Handoff WW32 item 3: the solver must start from ground truth, not from the
    seeded fixture. From `helpers.current_state` we take, per line:
      * `available_from_hour` <- end of the RUNNING MO (the starting position:
        the line is busy until the current run finishes, then demand fills
        forward) — the solver may not place work before it;
      * `initial_sku`         <- the SKU actually running (so the changeover
        matrix charges the real changeover, not a CLEAN start);
      * `carryover_run_hours_since_last_cip_at_t0` <- hours since the line's
        last CIP, so the first CIP is due at the right time.

    `lock_current_mo` (default: from config `scheduler.use_current_mo`) decides
    whether the running+queued manprg MOs are ALSO emitted as locked solver
    orders (current_mo.csv). This is ONLY correct for scenario E ("current
    state + demand", re-optimize the committed tail). Fresh-generation
    scenarios (A-D) take manprg purely as a starting state and re-place the
    demand plan, so they must NOT lock the queued MOs — locking them on top of
    the demand plan double-counts the tonnage and makes the solve INFEASIBLE.

    Returns a list of human-readable notes. Best-effort: if the live feeds are
    missing the initial_states file is left alone, but the reason is REPORTED
    rather than silently swallowed (a bare `except ImportError` here previously
    made the whole overlay a no-op because of a wrong import path).
    """
    notes: list[str] = []
    import pandas as _pd

    from helpers.calendar_io import load_lines as _ll  # NOT lines_model
    from helpers.config import datasources_config as _ds
    from helpers.config import load_toml as _lt
    from helpers.current_state import build_current_state as _bcs
    from helpers.horizon import resolve as _hr
    from helpers.paths import data_dir as _dd

    dd = data_dir if Path(data_dir).name == "data" else _dd()
    dd = Path(dd)
    cfg = _lt()
    hz = _hr(cfg)
    ds = _ds(cfg)
    mp_paths = [p.strip() for p in str(ds.get("manprg_files", "")).split(";")
                if p.strip()] or [
                    str(dd / "reference" / "manprg.txt"),
                    str(dd / "reference" / "manprg2.txt")]
    cip_path = str(ds.get("cip_info_csv", "")).strip() or str(
        dd / "reference" / "cip_info.csv")
    notes.extend(_stage_time_frame(work, dd, hz))
    try:
        cs = _bcs(hz, manprg_paths=mp_paths, cip_path=cip_path,
                  lines=_ll(dd / "lines.csv"), cfg=cfg,
                  caps_path=dd / "reference" / "capabilities_rates.csv")
    except Exception as exc:  # noqa: BLE001
        return notes + [f"current-state overlay skipped: {exc}"]

    init_path = Path(work) / "initial_states.csv"
    if not init_path.exists():
        return ["current-state overlay skipped: no initial_states.csv in work dir"]
    init = _pd.read_csv(init_path)
    if init.empty:
        return ["current-state overlay skipped: initial_states.csv is empty"]

    # The line is busy until the RUNNING MO finishes — that is the starting
    # position. (line_free_h is the end of the running MO PLUS every queued MO
    # behind it; it is NOT used for the gate because queued MOs are re-placed
    # from the demand plan, not locked.)
    running_free_map = {
        ln.upper(): max(0, int(h)) for ln, h in cs.line_running_free_h.items()
    }
    _use_cmo = str(
        cfg.get("scheduler", {}).get("use_current_mo", "")).strip().lower()
    if lock_current_mo is None:
        lock_current_mo = _use_cmo in ("1", "true", "yes")
    sku_map = {r["line"].upper(): r["item"] for r in cs.running}
    # hours since the last CIP -> the first CIP is due MaxHoursBetweenCIP later
    from helpers.cip_import import read_cip_info as _rci
    carry_map: dict[str, int] = {}
    for line, info in _rci(cip_path).by_line.items():
        if info.previous_cip is not None:
            hrs = (hz.anchor - info.previous_cip.to_pydatetime()).total_seconds() / 3600.0
            if hrs > 0:
                # A carryover >= the line's max CIP interval says "a CIP was
                # already overdue before hour 0", which the CIP constraints
                # cannot satisfy and turns the whole solve INFEASIBLE. Clamp
                # just below the limit so the solver schedules the CIP
                # immediately instead of failing. phase2_scheduler does the
                # same (min(carryover, 119)) when it writes week-1 states.
                limit = int(info.max_hours_between or 120)
                carry_map[line.upper()] = int(min(hrs, max(0, limit - 1)))

    changed = 0
    # The line is busy until the RUNNING MO finishes — that is the starting
    # position the planner described. queued MOs are re-placed from the demand
    # plan, so they must NOT widen the gate (that would double-block).
    gate_map = running_free_map
    for idx, row in init.iterrows():
        ln = str(row.get("line_name", "")).strip().upper()
        if ln in gate_map and gate_map[ln] > 0:
            init.at[idx, "available_from_hour"] = gate_map[ln]
            changed += 1
        if ln in sku_map and sku_map[ln]:
            init.at[idx, "initial_sku"] = sku_map[ln]
        if ln in carry_map and "carryover_run_hours_since_last_cip_at_t0" in init.columns:
            init.at[idx, "carryover_run_hours_since_last_cip_at_t0"] = carry_map[ln]

    init.to_csv(init_path, index=False)
    notes.append(
        f"current state overlaid: {changed} line(s) gated to running-MO end, "
        f"{len(sku_map)} initial SKU(s), {len(carry_map)} CIP carryover(s)")

    # ── current_mo.csv: running + queued MOs as solver input ─────────────
    # Each row is an MO locked to its manprg line; the solver may split /
    # reorder / trim it, and mo_changes.csv records the delta for VIF.
    #
    # The RUNNING MO's remaining work is expressed as the availability gate
    # above (available_from = running-MO end) — the line is busy until it
    # finishes, and the gate stops the solver placing work over it. It must NOT
    # also be emitted here as a locked demand order, or its remaining tonnage
    # is double-counted on top of the demand plan (that made a "minimum
    # changeovers" solve demand ~150h of P10's running MO PLUS the same SKUs
    # from the plan -> INFEASIBLE).
    #
    # QUEUED MOs are emitted ONLY for scenario E (current state + demand),
    # where the manprg queue is committed work to re-optimize. Fresh-generation
    # scenarios (A-D) take manprg as a STARTING STATE and re-place the demand
    # plan; locking queued MOs on top of demand double-counts tonnage and made
    # them INFEASIBLE.
    if not lock_current_mo:
        return notes

    def _is_trial(r: dict) -> bool:
        return str(r.get("item", "")).strip().upper() == "TRIALS"

    # manprg TRIALS pseudo-MOs are the plant's trial reservations (user rule
    # 2026-08-14; trials.csv is NOT a source). As solver ORDERS they were
    # impossible by construction (no rate -> prod == 0 vs qty_min > 0, one
    # root of the level-0 infeasibility) — they are BLOCKED LINE TIME, so
    # they go into the work-dir downtimes instead.
    trial_windows: list[dict] = []
    for r in cs.running:
        if _is_trial(r):
            s_h = max(0, _hours_h(r.get("shown_start"), hz.anchor))
            e_h = max(s_h + 1, _hours_h(r.get("est_end"), hz.anchor))
            trial_windows.append({"line": str(r["line"]).upper(),
                                  "start": s_h, "end": e_h})
    for r in cs.queued:
        if _is_trial(r):
            s_h = max(0, _hours_h(r.get("placed_start"), hz.anchor))
            e_h = max(s_h + 1, _hours_h(r.get("placed_end"), hz.anchor))
            trial_windows.append({"line": str(r["line"]).upper(),
                                  "start": s_h, "end": e_h})
    if trial_windows:
        dt_path = Path(work) / "downtimes.csv"
        dt = _pd.read_csv(dt_path) if dt_path.exists() else _pd.DataFrame(
            columns=["line_id", "line_name", "start_hour", "end_hour", "reason"])
        from helpers.calendar_io import load_lines as _ll2
        lines_df = _ll2(Path(dd) / "lines.csv")
        lid_map = {str(r["line_name"]).upper(): int(r["line_id"])
                   for _, r in lines_df.iterrows()} if len(lines_df) else {}
        add = _pd.DataFrame([{
            "line_id": lid_map.get(w["line"], 0), "line_name": w["line"],
            "start_hour": int(w["start"]), "end_hour": int(round(w["end"])),
            "reason": "Trial (manprg)",
        } for w in trial_windows])
        _pd.concat([dt, add], ignore_index=True).to_csv(dt_path, index=False)
        notes.append(
            f"{len(trial_windows)} manprg TRIALS window(s) blocked as downtime "
            "(trials are blocked hours, never orders)")

    cmo_rows = []
    for r in cs.running:
        if _is_trial(r):
            continue
        left_cas = float(r.get("left_cas", 0) or 0)
        fct_cas = float(r.get("fct_cas", 0) or 0)
        qty_kg = float(r.get("qty_kg", 0) or 0)
        remaining = round(qty_kg * (left_cas / fct_cas), 3) \
            if fct_cas > 0 else qty_kg
        cmo_rows.append({
            "mo": r["mo"], "line_name": str(r["line"]).upper(),
            "sku": r["item"], "remaining_kg": remaining,
            # full horizon, not the pre-rolling 336h window (audit
            # 2026-08-15): a running MO finishing after hour 335 was
            # forced late/infeasible by construction
            "due_start_h": 0, "due_end_h": int(hz.hours) - 1,
            "locked_line": 1, "source": "manprg",
        })
    for r in cs.queued:
        if _is_trial(r):
            continue
        remaining = round(float(r.get("qty_kg", 0) or 0), 3)
        start_h = max(0, int(_hours_h(r.get("placed_start"), hz.anchor)))
        end_h = max(start_h + 1, int(_hours_h(r.get("placed_end"), hz.anchor)))
        cmo_rows.append({
            "mo": r["mo"], "line_name": str(r["line"]).upper(),
            "sku": r["item"], "remaining_kg": remaining,
            "due_start_h": start_h, "due_end_h": end_h,
            "locked_line": 1, "source": "manprg",
        })
    if cmo_rows:
        _pd.DataFrame(cmo_rows).to_csv(Path(work) / "current_mo.csv", index=False)
        notes.append(
            f"current_mo.csv: {len(cmo_rows)} MO(s) locked to their manprg line")
    return notes


def _hours_h(when, anchor) -> float:
    """Hours from the horizon anchor to `when` (datetime/pandas Timestamp)."""
    import pandas as _pd
    ts = _pd.Timestamp(when).to_pydatetime()
    return (ts - anchor).total_seconds() / 3600.0


def _overlay_fill(work: Path, data_dir: Path) -> list[str]:
    """Scenario F staging: the committed plan becomes FIXED line-time.

    manprg + cip_info (via build_current_state) are laid out as blocked
    windows; per line the fill region starts where committed work ends, with
    the last committed SKU as the changeover base. Demand targets are reduced
    by what the committed plan already produces (surplus carries forward).
    The solver's own CIP generation stands down — layer-1 CIPs are projected
    at the real per-line interval through the whole horizon, so fill dirty-
    time between cleans is bounded by construction. No current_mo.csv: the
    plant's plan is never a solver order in F.
    """
    import math as _math

    import pandas as _pd

    from helpers.calendar_io import load_lines as _ll
    from helpers.config import datasources_config as _ds
    from helpers.config import load_toml as _lt
    from helpers.current_state import build_current_state as _bcs
    from helpers.horizon import resolve as _hr
    from helpers.paths import data_dir as _dd
    from helpers.plan_fill import (SOLVER_CIP_INTERVAL_STANDDOWN_H,
                                   coalesce_windows, committed_windows,
                                   last_sku_per_line, line_free_from,
                                   pinned_blocks)

    notes: list[str] = []
    dd = Path(data_dir) if Path(data_dir).name == "data" else Path(_dd())
    cfg = _lt()
    hz = _hr(cfg)
    H = float(hz.hours)
    ds = _ds(cfg)
    mp_paths = [p.strip() for p in str(ds.get("manprg_files", "")).split(";")
                if p.strip()] or [str(dd / "reference" / "manprg.txt"),
                                  str(dd / "reference" / "manprg2.txt")]
    cip_path = str(ds.get("cip_info_csv", "")).strip() or str(
        dd / "reference" / "cip_info.csv")
    notes.extend(_stage_time_frame(work, dd, hz))
    cs = _bcs(hz, manprg_paths=mp_paths, cip_path=cip_path,
              lines=_ll(dd / "lines.csv"), cfg=cfg,
              caps_path=dd / "reference" / "capabilities_rates.csv")
    blocks = cs.blocks

    # Planner-pinned demand blocks (Plant Calendar popup: "Fix for solver").
    # A pinned block is committed line-time exactly like a manprg MO — a
    # blocked window the solver plans around, its kg crediting the demand
    # targets, and part of the proposal calendar. It deliberately does NOT
    # join `blocks` for line_free_from / last_sku_per_line: a block pinned
    # deep in the fill region must not gate the whole line before it, and
    # the changeover base belongs to the committed TAIL, not to a mid-
    # horizon pin.
    from helpers.calendar_io import load_calendar as _lcal
    cal_path = dd / "calendar_blocks.csv"
    pinned = pinned_blocks(_lcal(cal_path)) if cal_path.exists() else None
    if pinned is not None and len(pinned):
        blocks_all = _pd.concat([blocks, pinned], ignore_index=True)
        notes.append(
            f"{len(pinned)} planner-pinned block(s) held FIXED "
            "(blocked windows + demand credit); the solver fills around them")
    else:
        blocks_all = blocks

    # 1. committed windows -> downtimes (existing line-downs kept). The
    # TRUE committed blocks are stashed alongside so the proposal calendar
    # can show them as production/trial/CIP instead of fake maintenance.
    windows = committed_windows(blocks_all, H)
    blocks_all.to_csv(work / "committed_blocks.csv", index=False)
    dt_path = work / "downtimes.csv"
    dt = _pd.read_csv(dt_path) if dt_path.exists() else _pd.DataFrame(
        columns=["line_id", "line_name", "start_hour", "end_hour", "reason"])
    dt.to_csv(work / "real_downtimes.csv", index=False)
    combined = dt.to_dict("records") + windows
    merged = coalesce_windows([
        {"line_id": r.get("line_id", 0), "line_name": r["line_name"],
         "start_hour": r["start_hour"], "end_hour": r["end_hour"],
         "reason": r.get("reason", "blocked")}
        for r in combined])
    _pd.DataFrame(merged, columns=["line_id", "line_name", "start_hour",
                                   "end_hour", "reason"]).to_csv(
        dt_path, index=False)
    notes.append(f"{len(windows)} committed window(s) fixed as blocked time "
                 "(running+queued MOs, trials, projected CIPs)")

    # 2. initial states: fill starts at the committed tail OR NOW, whichever
    #    is later; changeover base = last committed SKU, dirty-clock
    #    carryover 0 (layer-1 CIPs rule). The now-floor exists because a
    #    line whose manprg queue ran dry yesterday has a committed tail in
    #    the past — without the floor the solver placed fill blocks 28h ago
    #    (user report 2026-08-14). Only applied when "now" falls inside the
    #    horizon, so replays/tests on stale snapshots stay untouched.
    from datetime import datetime as _dtm
    now_h = (_dtm.now() - hz.anchor).total_seconds() / 3600.0
    now_floor = now_h if 0.0 <= now_h < H else 0.0
    if now_floor:
        notes.append(f"fill floor at now (t+{now_floor:.0f}h): no new block "
                     "may start in the past")
    free = line_free_from(blocks, H)
    last_sku = last_sku_per_line(blocks)
    init_path = work / "initial_states.csv"
    if init_path.exists():
        init = _pd.read_csv(init_path)
        n_gated = 0
        gates: dict[str, float] = {}
        for idx, row in init.iterrows():
            ln = str(row.get("line_name", "")).strip().upper()
            gates[ln] = float(_math.ceil(max(free.get(ln, 0.0), now_floor)))
            if ln in free or now_floor:
                gate = max(free.get(ln, 0.0), now_floor)
                init.at[idx, "available_from_hour"] = int(_math.ceil(gate))
                n_gated += 1
            if ln in last_sku:
                init.at[idx, "initial_sku"] = last_sku[ln]
            if "carryover_run_hours_since_last_cip_at_t0" in init.columns:
                init.at[idx, "carryover_run_hours_since_last_cip_at_t0"] = 0
        init.to_csv(init_path, index=False)
        # The per-line fill boundary, persisted for fill-window scoring:
        # the scorecard needs the SAME gates the solver was staged with to
        # window a proposal (and the official board) to the fill region.
        import json as _json
        (work / "fill_gates.json").write_text(
            _json.dumps({"gates": gates}, indent=1), encoding="utf-8")
        notes.append(f"{n_gated} line(s) gated to their committed tail; "
                     f"{len(last_sku)} changeover base SKU(s) set")

    # 3. solver CIP standdown (projected CIPs carry the cleans)
    cip_hrs_path = work / "line_cip_hrs.csv"
    if cip_hrs_path.exists():
        ch = _pd.read_csv(cip_hrs_path)
        if "max_cip_hrs" in ch.columns:
            ch["max_cip_hrs"] = SOLVER_CIP_INTERVAL_STANDDOWN_H
            ch.to_csv(cip_hrs_path, index=False)
            notes.append("solver CIP generation stood down "
                         "(layer-1 projected CIPs carry the cleans)")

    # 4. demand minus committed production (carry-forward per SKU). The due
    #    windows were already re-based into the staging frame by
    #    _stage_time_frame (called at the top of this overlay). The ledger
    #    keys by TRUE ISO weeks (anchor=hz.anchor) — week_index keying
    #    silently no-opped when the demand file anchored at an older week
    #    than the staging frame (live: W32 file vs W33 anchor, 2026-08-16).
    dem_path = work / "demand_plan.csv"
    if dem_path.exists():
        from helpers.demand_coverage import (apply_ledger, build_ledger,
                                             demand_source_anchor,
                                             demand_week_grid)

        dem = _pd.read_csv(dem_path, dtype={"sku": str})
        # History: past demand weeks were dropped from the staged frame as
        # misses, but completed production from those weeks must settle
        # against ITS OWN week first — otherwise a finished pre-build MO
        # double-credits the surviving weeks. The reference demand file
        # still carries the past weeks; colliding live weeks are skipped
        # inside build_ledger.
        hist: dict = {}
        _ref_dem = dd / "reference" / "demand_plan.csv"
        _dem_anchor = demand_source_anchor(dd)
        if _ref_dem.exists() and _dem_anchor is not None:
            hist = demand_week_grid(
                _pd.read_csv(_ref_dem, dtype={"sku": str}), _dem_anchor)
        ledger = build_ledger(dem, blocks_all, completed=cs.completed,
                              anchor=hz.anchor, history_demand=hist)
        dem2, sub_notes = apply_ledger(dem, ledger)
        dem2.to_csv(dem_path, index=False)
        ledger.to_frame().to_csv(work / "coverage_ledger.csv", index=False)
        notes.append(f"demand reduced by committed production on "
                     f"{len(sub_notes)} order(s) "
                     "(SKU-week ledger: coverage_ledger.csv)")
        notes.extend(ledger.notes[:2])
        notes.extend(sub_notes[:5])
        if ledger.overcommitted:
            _oc = sorted(ledger.overcommitted.items(),
                         key=lambda kv: -kv[1]["surplus_kg"])[:3]
            notes.append(
                "committed MOs outrun the demand plan for "
                + ", ".join(f"{s} (+{d['surplus_kg']:,.0f} kg through "
                            f"{d['last_week_label']})" for s, d in _oc)
                + " — surplus no demand week absorbs; check whether the plan "
                  "was already netted or the MOs pre-build beyond the file")

    # 5. never a committed order in F
    cmo = work / "current_mo.csv"
    if cmo.exists():
        cmo.unlink()
    for w in cs.warnings[:4]:
        notes.append(f"current-state warning: {w}")
    return notes


def _work_input_signature(work: Path) -> str:
    """MUST mirror phase2_scheduler.input_signature exactly — the solver
    only trusts warm-start hints whose signature matches its own inputs."""
    import hashlib
    h = hashlib.md5()
    for name in ("capabilities_rates.csv", "changeovers.csv",
                 "demand_plan.csv", "downtimes.csv", "initial_states.csv",
                 "line_cip_hrs.csv", "line_rates.csv", "sku_info.csv",
                 "current_mo.csv"):
        p = Path(work) / name
        if p.exists():
            try:
                h.update(name.encode())
                h.update(p.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def _greedy_seed(work: Path) -> list[str]:
    """Scenario F: construct a dense fill greedily and write it as the
    solver's warm start (prev_schedule.csv + a matching prev_feasibility.json
    so the hint gates accept it). Runs AFTER every input patch — the seed
    sees exactly what the solver will see."""
    import json as _json

    import pandas as _pd

    from helpers.greedy_fill import build_greedy_fill, free_segments

    notes: list[str] = []
    dem = _pd.read_csv(work / "demand_plan.csv", dtype={"sku": str})
    dt = _pd.read_csv(work / "downtimes.csv") if (work / "downtimes.csv").exists()         else _pd.DataFrame(columns=["line_name", "start_hour", "end_hour"])
    init = _pd.read_csv(work / "initial_states.csv")
    import tomllib as _toml
    cfg = _toml.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    sched = cfg.get("scheduler", {})
    H = float(sched.get("horizon_hours")
              or (float(sched.get("horizon_weeks", 3) or 3) * 168.0))
    min_run = int(sched.get("min_run_hours", 4))

    # flat line rates (F runs with use_sku_rates off)
    rates: dict[tuple[str, str], float] = {}
    lr = _pd.read_csv(work / "line_rates.csv")
    # bridge exports name this column inconsistently (Line vs line_name)
    lr_name_col = next(c for c in ("line_name", "Line", "line") if c in lr.columns)
    flat = {str(r[lr_name_col]).upper(): float(r.get("rate_kgph") or r.get("calc_rate_kgph") or 0)
            for _, r in lr.iterrows()}
    caps = _pd.read_csv(work / "capabilities_rates.csv", dtype={"sku": str})
    for _, r in caps.iterrows():
        if int(r.get("capable", 0) or 0) == 1:
            ln = str(r["line_name"]).upper()
            if flat.get(ln, 0) > 0:
                rates[(ln, str(r["sku"]))] = flat[ln]

    from solver.changeover_cache import load_changeover_setup_nested
    setups = load_changeover_setup_nested(work / "changeovers.csv")

    # Format-aware transition costs: the same weighted machine economics the
    # solver optimizes ([changeover] in the work toml, AFTER scenario
    # overrides — the seed runs post-_patch_work_toml). FFS/topload swaps
    # rank expensive, TTP cheap, so the greedy builds format-grouped
    # campaigns instead of baking 70 topload changes into the warm start.
    cw = cfg.get("changeover", {})
    _w = {
        "topload_change": float(cw.get("topload_weight", 50)),
        "ffs_change": float(cw.get("ffs_weight", 75)),
        "casepacker_change": float(cw.get("casepacker_weight", 20)),
        "ttp_change": float(cw.get("ttp_weight", 5)),
        "conv_to_org_change": float(cw.get("conv_org_weight", 30)),
        "cinn_to_non": float(cw.get("cinn_weight", 30)),
        # required-CIP pairs: steer the greedy seed away from unclean
        # protein transitions just like the model does
        "cip_req_after": float(cw.get("cip_req_weight", 2000)),
    }
    _base_w = float(cw.get("base_changeover_weight", 5))
    _flavor_w = float(cw.get("flavor_weight", 5))
    co_cost: dict[str, dict[str, float]] = {}
    from helpers.scorecard_engine import normalize_co_columns

    co_df = normalize_co_columns(
        _pd.read_csv(work / "changeovers.csv",
                     dtype={"from_sku": str, "to_sku": str}))
    for _r in co_df.itertuples(index=False):
        _c = _base_w + sum(
            w for col, w in _w.items()
            if int(getattr(_r, col, 0) or 0) == 1)
        # added_flavors can be negative (removing flavors is a reward);
        # clamp at 0 exactly like the model's pair_cost (model_builder).
        _c += _flavor_w * int(getattr(_r, "added_flavors", 0) or 0)
        co_cost.setdefault(str(_r.from_sku), {})[str(_r.to_sku)] = max(0.0, _c)

    blocked: dict[str, list[tuple[float, float]]] = {}
    for _, r in dt.iterrows():
        blocked.setdefault(str(r["line_name"]).upper(), []).append(
            (float(r["start_hour"]), float(r["end_hour"])))
    line_segments: dict[str, list[tuple[float, float]]] = {}
    line_ids: dict[str, int] = {}
    init_sku: dict[str, str] = {}
    for _, r in init.iterrows():
        ln = str(r["line_name"]).upper()
        gate = float(r.get("available_from_hour", 0) or 0)
        line_segments[ln] = free_segments(gate, blocked.get(ln, []), H)
        line_ids[ln] = int(r.get("line_id", 0) or 0)
        init_sku[ln] = str(r.get("initial_sku", "") or "")

    rows, summary = build_greedy_fill(
        dem.to_dict("records"), rates, setups, line_segments, line_ids,
        init_sku, co_cost=co_cost, min_run_hours=min_run, horizon_h=H)
    if not rows:
        return ["greedy seed: nothing to place"]
    _pd.DataFrame(rows).to_csv(work / "prev_schedule.csv", index=False)
    (work / "prev_feasibility.json").write_text(_json.dumps({
        "relax_level": 0, "soft_demand": True,
        "input_sig": _work_input_signature(work),
        "note": "greedy construction seed (helpers/greedy_fill.py)",
    }, indent=2), encoding="utf-8")
    notes.append(
        f"greedy seed: {summary['rows']} fill block(s), kg/week "
        f"{summary['kg_by_week']} - handed to CP-SAT as a full warm start")
    if summary["orders_short"]:
        notes.append(f"greedy seed left {len(summary['orders_short'])} "
                     "order(s) short (solver may still improve)")
    return notes


def _stage_warm_start(work: Path, scenario: dict[str, Any]) -> list[str]:
    """Fill-mode warm-start staging, per the scenario's ``warm_start`` key.

    * "greedy" (default / absent): build the greedy construction seed —
      the historical behaviour, byte-identical for every existing caller.
    * "none" (cold arm): NO seed at all. The greedy seed is skipped AND any
      prev_schedule.csv / prev_feasibility.json carried across the work-dir
      wipe by _prepare_work_dir is removed, so the solver has nothing to
      hint from and logs a true cold start.
    * "prev" (chained arm): the caller's work_dir_patch planted a donor
      schedule as prev_schedule.csv (+ prev_feasibility.json carrying the
      donor's input_sig and relax level). The greedy seed is skipped so it
      cannot overwrite the donor; the solver's own trust gates (input-
      signature md5 + relax-level) then decide whether to hint from it.
    """
    mode = str(scenario.get("warm_start") or "greedy")
    if mode == "none":
        removed = []
        for name in ("prev_schedule.csv", "prev_feasibility.json"):
            p = work / name
            if p.exists():
                try:
                    p.unlink()
                    removed.append(name)
                except OSError:
                    pass
        note = "warm start disabled (cold start): greedy seed skipped"
        if removed:
            note += f"; removed carried {', '.join(removed)}"
        return [note]
    if mode == "prev":
        if (work / "prev_schedule.csv").exists():
            return ["warm start from planted prev_schedule.csv (chained): "
                    "greedy seed skipped; the solver's trust gates decide"]
        return ["warm start mode 'prev' but no prev_schedule.csv planted — "
                "solving cold (greedy seed skipped)"]
    return _greedy_seed(work)


def normalize_overrides(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only known weight keys with a real value; coerce to int.

    Accepts flat keys ('makespan_weight') or dotted ('objective.makespan_weight').
    """
    clean: dict[str, Any] = {}
    for raw_key, value in (overrides or {}).items():
        key = raw_key.partition(".")[2] if "." in raw_key else raw_key
        if key not in OVERRIDE_SECTIONS or value is None:
            continue
        try:
            clean[key] = (
                float(value) if key in FLOAT_OVERRIDE_KEYS else int(value)
            )
        except (TypeError, ValueError):
            continue
    return clean


def _patch_work_toml(
    toml: Path,
    time_limit: int | None,
    overrides: dict[str, Any] | None = None,
) -> None:
    """Write time limit + weight overrides into the work-dir flowstate.toml.

    With no overrides this keeps the historical in-place regex patch so the
    copied config (comments and all) is byte-identical apart from time_limit.
    With overrides it round-trips the file through tomllib / tomli_w.
    """
    overrides = normalize_overrides(overrides)
    if not overrides:
        if time_limit is None:
            return
        try:
            import re

            text = toml.read_text(encoding="utf-8")
            text = re.sub(r"time_limit\s*=\s*\d+", f"time_limit = {int(time_limit)}", text)
            toml.write_text(text, encoding="utf-8")
        except Exception:
            pass
        return

    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - py<3.11
            import tomli as tomllib  # type: ignore
        import tomli_w

        with open(toml, "rb") as fh:
            cfg = tomllib.load(fh)
        if time_limit is not None:
            cfg.setdefault("scheduler", {})["time_limit"] = int(time_limit)
        for key, value in overrides.items():
            section = OVERRIDE_SECTIONS[key]
            cfg.setdefault(section, {})[key] = value
            # [objective] carries duplicate co_* keys that phase2_scheduler
            # applies before [changeover]; keep them in step so the override wins
            # regardless of which section the solver reads last.
            if section == "changeover" and key in ("conv_org_weight", "cinn_weight", "flavor_weight"):
                cfg.setdefault("objective", {})[f"co_{key}"] = value
        with open(toml, "wb") as fh:
            tomli_w.dump(cfg, fh)
    except Exception:
        # Never let config rewriting kill a solve; fall back to time-limit only.
        if time_limit is not None:
            try:
                import re

                text = toml.read_text(encoding="utf-8")
                text = re.sub(r"time_limit\s*=\s*\d+", f"time_limit = {int(time_limit)}", text)
                toml.write_text(text, encoding="utf-8")
            except Exception:
                pass


def _set_work_scheduler_flag(toml: Path, key: str, value) -> None:
    """Set one [scheduler] key in a work-dir flowstate.toml (best-effort)."""
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - py<3.11
            import tomli as tomllib  # type: ignore
        import tomli_w
        with open(toml, "rb") as fh:
            cfg = tomllib.load(fh)
        cfg.setdefault("scheduler", {})[key] = value
        with open(toml, "wb") as fh:
            tomli_w.dump(cfg, fh)
    except Exception:  # noqa: BLE001
        pass


def _set_work_use_current_mo(toml: Path, value: bool) -> None:
    """Set scheduler.use_current_mo in a work-dir flowstate.toml.

    Keeps the solver's relax-ladder skip (USE_CURRENT_MO -> jump to
    ignore_co) consistent with the overlay's per-scenario decision. A fresh-
    generation scenario must not skip to ignore_co — that was how a
    "minimum changeovers" solve silently ignored changeovers. Best-effort:
    a failure to write here must not kill a solve, so swallow it.
    """
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - py<3.11
            import tomli as tomllib  # type: ignore
        import tomli_w
        with open(toml, "rb") as fh:
            cfg = tomllib.load(fh)
        cfg.setdefault("scheduler", {})["use_current_mo"] = bool(value)
        with open(toml, "wb") as fh:
            tomli_w.dump(cfg, fh)
    except Exception:  # noqa: BLE001
        pass


def _read_feasibility(work: Path) -> dict[str, Any] | None:
    """Read feasibility_report.json from a solver work dir (written every run)."""
    import json
    path = work / "feasibility_report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_diag_blockages(work: Path) -> str:
    """Human-readable blockages summary from the solver work dir (if any)."""
    path = work / "diag_blockages.txt"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ── Mid-solve reattach (walkthrough finding 6, 2026-08-18) ─────────────────
# Streamlit kills the page script when the user navigates away mid-solve; the
# solver subprocess keeps running but nobody is left to save its result. The
# live-progress launch drops run_pending.json next to the solver outputs so
# generate.py can reattach on the next load: still running → resume polling;
# finished but never saved → collect + save now. The manifest is cleared the
# moment a save is ATTEMPTED (or the run fails) — it marks "result at risk of
# being lost", not a durable queue.

PENDING_MANIFEST = "run_pending.json"
PENDING_STALE_H = 24.0


def pending_manifest_path(work: Path) -> Path:
    return Path(work) / PENDING_MANIFEST


def _write_pending_manifest(
    work: Path,
    scenario: dict[str, Any],
    *,
    time_limit: int | None,
    timeout_s: float,
    pid: int,
    cs_notes: list[str] | None,
    started_at: str | None = None,
) -> None:
    from datetime import datetime as _dt

    from helpers.safe_io import safe_write_json
    safe_write_json({
        # started_at also names the saved version (scenario_version_name):
        # a reattached run must reproduce the SAME name the attended save
        # would have produced.
        "started_at": started_at or _dt.now().isoformat(timespec="seconds"),
        "scenario": scenario,
        "time_limit": time_limit,
        "timeout_s": timeout_s,
        "pid": int(pid),
        "work_dir": str(work),
        "slug_hint": f"scenario_{str(scenario.get('id', '')).lower()}",
        # Pre-solve staging notes live only in run_scenario's locals — carry
        # them so a reattached run's log is as complete as an attended one.
        "cs_notes": [str(n) for n in (cs_notes or [])],
    }, pending_manifest_path(work))


def read_pending_manifest(work: Path) -> dict[str, Any] | None:
    """None = no manifest; {} = present but unreadable (offer discard)."""
    import json as _j
    p = pending_manifest_path(work)
    if not p.exists():
        return None
    try:
        m = _j.loads(p.read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def clear_pending_manifest(work: Path) -> None:
    try:
        pending_manifest_path(work).unlink(missing_ok=True)
    except OSError:
        pass


def pending_is_stale(manifest: dict[str, Any], *, now: Any | None = None) -> bool:
    """Corrupt, or started more than PENDING_STALE_H ago."""
    from datetime import datetime as _dt
    if not manifest or not isinstance(manifest.get("scenario"), dict):
        return True
    try:
        started = _dt.fromisoformat(str(manifest.get("started_at")))
    except (TypeError, ValueError):
        return True
    return ((now or _dt.now()) - started).total_seconds() > PENDING_STALE_H * 3600


def list_pending_runs(data_dir: Path) -> list[dict[str, Any]]:
    """All pending-run manifests under data/_scenario_work, work_dir attached."""
    root = Path(data_dir) / "_scenario_work"
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    for p in sorted(root.glob(f"*/{PENDING_MANIFEST}")):
        m = read_pending_manifest(p.parent)
        if m is None:  # pragma: no cover — raced with a concurrent clear
            continue
        m = dict(m)
        m["work_dir"] = str(p.parent)
        out.append(m)
    return out


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            # An exited process whose handle someone still holds opens fine —
            # only exit code STILL_ACTIVE means it is really running.
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    import os
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def resume_scenario(
    scenario: dict[str, Any],
    data_dir: Path,
    *,
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    """Reattach to a solve whose page run was killed mid-poll.

    Same contract as run_scenario: polls solver_progress.json while the
    recorded pid is alive, then collects the work-dir outputs. The exit code
    is unknowable after a detach, so outputs are the truth: a schedule on
    disk counts as success.
    """
    import json as _pj
    import time as _time
    from datetime import datetime as _dt

    work = (Path(data_dir) / "_scenario_work" / str(scenario["id"])).resolve()
    m = read_pending_manifest(work) or {}
    started_at = m.get("started_at")
    try:
        t0 = _dt.fromisoformat(str(started_at))
    except (TypeError, ValueError):
        t0 = None
    timeout_s = float(m.get("timeout_s") or 0) or max(
        120, float(m.get("time_limit") or 60) * 4 + 60)
    pid = m.get("pid")
    while _pid_alive(pid):
        elapsed = (_dt.now() - t0).total_seconds() if t0 else 0.0
        prog = None
        try:
            pp = work / "solver_progress.json"
            if pp.exists():
                prog = _pj.loads(pp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prog = None
        if progress_cb is not None:
            try:
                progress_cb(prog, elapsed)
            except Exception:  # noqa: BLE001 — UI must not kill a solve
                pass
        if t0 is not None and elapsed > timeout_s:
            break  # over budget: collect what exists instead of spinning
        _time.sleep(2.0)
    so_p = work / "_solver_stdout.txt"
    se_p = work / "_solver_stderr.txt"
    stdout = so_p.read_text(encoding="utf-8", errors="replace") if so_p.exists() else ""
    stderr = se_p.read_text(encoding="utf-8", errors="replace") if se_p.exists() else ""
    rc = 0 if (work / "schedule_phase2.csv").exists() else 1
    result = _collect_result(
        scenario, work, data_dir, rc, stdout, stderr,
        [str(n) for n in (m.get("cs_notes") or [])])
    # The save path names the version from the run's start time — carry the
    # manifest's so a reattached save reproduces the attended run's name.
    result["started_at"] = started_at
    if not result["ok"]:
        clear_pending_manifest(work)  # nothing to save — stop reattaching
    return result


def _collect_result(
    scenario: dict[str, Any],
    work: Path,
    data_dir: Path,
    returncode: int,
    stdout: str,
    stderr: str,
    cs_notes: list[str],
) -> dict[str, Any]:
    """Turn a finished solver work dir into run_scenario's result contract.

    Shared by the attended path (run_scenario) and the reattach path
    (resume_scenario) so both save and score identically.
    """
    log = (stdout or "") + "\n" + (stderr or "")
    err_file = work / "solver_error.txt"
    if err_file.exists():
        log += "\n" + err_file.read_text(encoding="utf-8")
    if cs_notes:
        log = "[current state] " + "; ".join(cs_notes) + "\n" + log

    fill_mode = bool(scenario.get("fill_mode"))
    sched = work / "schedule_phase2.csv"
    cip = work / "cip_windows.csv"
    feas = _read_feasibility(work)
    relax_level = feas.get("relax_level") if feas else None
    diag_blockages = _read_diag_blockages(work)
    if returncode != 0 or not sched.exists():
        return {
            "ok": False,
            "returncode": returncode,
            "log": log,
            "calendar": None,
            "scorecard": None,
            "feasibility": feas,
            "relax_level": relax_level,
            "diag_blockages": diag_blockages,
        }

    if fill_mode:
        # Committed layer (true block types) + the solver's fill blocks +
        # only the REAL line-downs — the committed windows in downtimes.csv
        # were solver plumbing, not calendar content.
        import pandas as _pd2

        from helpers.calendar_io import load_calendar as _lc
        committed_path = work / "committed_blocks.csv"
        committed = _lc(committed_path) if committed_path.exists() else None
        real_dt = work / "real_downtimes.csv"
        fill_part = import_solver_schedule(
            sched,
            cip if cip.exists() else None,
            real_dt if real_dt.exists() else None,
        )
        calendar = (_pd2.concat([committed, fill_part], ignore_index=True)
                    if committed is not None and len(committed) else fill_part)
    else:
        calendar = import_solver_schedule(
            sched,
            cip if cip.exists() else None,
            work / "downtimes.csv" if (work / "downtimes.csv").exists() else None,
        )
    score = score_calendar(calendar, week_label=scenario["name"], data_dir=data_dir)
    fill_gates = None
    if fill_mode:
        gates_path = work / "fill_gates.json"
        if gates_path.exists():
            try:
                import json as _json

                fill_gates = _json.loads(
                    gates_path.read_text(encoding="utf-8")).get("gates") or None
            except Exception:  # noqa: BLE001 — gates are an enhancement, not a gate
                fill_gates = None
    return {
        "ok": True,
        "returncode": returncode,
        "log": log,
        "calendar": calendar,
        "scorecard": score,
        "feasibility": feas,
        "relax_level": relax_level,
        "diag_blockages": diag_blockages,
        "fill_gates": fill_gates,
    }


def run_scenario(
    scenario: dict[str, Any],
    data_dir: Path,
    *,
    time_limit: int | None = None,
    python_exe: str | None = None,
    overrides: dict[str, Any] | None = None,
    work_dir_patch: Any | None = None,
    progress_cb: Any | None = None,
    timeout_s: float | None = None,
    work_root: str = "_scenario_work",
) -> dict[str, Any]:
    """Run one scenario. Returns {ok, calendar, scorecard, log, returncode,
    feasibility, relax_level}.

    ``overrides`` is a flat weight mapping (see OVERRIDE_SECTIONS) written into
    the work-dir flowstate.toml before the solver runs. Scenarios may also carry
    their own 'overrides' key (custom scenarios); the argument wins.

    ``timeout_s`` overrides the subprocess kill ceiling. The default
    (4 x time_limit + 60) assumes one solve budget; a two-pass run whose
    pass 2 carries its own budget (scheduler.time_limit_pass2, overnight
    batch) must pass an explicit ceiling covering both passes.

    ``work_dir_patch`` is the agent seam: a callable ``(work: Path) ->
    list[str]`` invoked AFTER staging + current-state overlay and BEFORE the
    solve. It may rewrite the solver's own input copies (never the real
    data/reference files) — e.g. the agent trimming component-blocked demand —
    and returns human-readable notes that are prepended to the run log so
    every input mutation is visible in the record.

    ``work_root`` names the directory under data/ that holds the run's work
    dir. The default is the shared "_scenario_work" that the UI and the
    solved-dir probes scan; a background producer (the overnight batch) passes
    its own root so its dirs never surface as "the run that solved last" in
    Compare's MO-changes picker or the constraint probes.
    """
    from datetime import datetime as _dt_run
    _started_at = _dt_run.now().isoformat(timespec="seconds")
    work = (Path(data_dir) / work_root / scenario["id"]).resolve()
    _prepare_work_dir(Path(data_dir).resolve(), work)

    # Only scenario E ("current state + demand") treats the manprg running /
    # queued MOs as committed work locked to their lines. Fresh-generation
    # scenarios (A-D) take manprg purely as a STARTING STATE (running-MO end,
    # initial SKU, next CIP) and re-place the demand plan — locking the queued
    # MOs on top of demand double-counts tonnage and makes the solve
    # INFEASIBLE. So the lock flag is per-scenario, and we also write it into
    # the work toml so the solver's relax-ladder skip (USE_CURRENT_MO ->
    # {0:3} jump to ignore_co) matches the same intent.
    fill_mode = bool(scenario.get("fill_mode"))
    lock_current_mo = bool(scenario.get("lock_current_mo", scenario["id"] == "E"))
    if fill_mode:
        lock_current_mo = False
    _set_work_use_current_mo(work / "flowstate.toml", lock_current_mo)
    if scenario.get("soft_demand"):
        _set_work_scheduler_flag(work / "flowstate.toml", "soft_demand", True)
    if scenario.get("two_pass_co"):
        _set_work_scheduler_flag(work / "flowstate.toml", "two_pass_co", True)

    # Inject the current plant state. Scenario F fixes the committed plan as
    # blocked line-time (fill mode); every other scenario uses the classic
    # overlay (gates + optional locked current-MO orders). Best-effort, but
    # ALWAYS reported — a silent no-op here means the solver quietly plans
    # from the stale fixture.
    try:
        if fill_mode:
            _cs_notes = _overlay_fill(work, data_dir)
        else:
            _cs_notes = _overlay_current_state(
                work, data_dir, lock_current_mo=lock_current_mo)
    except Exception as _exc:  # noqa: BLE001
        if fill_mode:
            # Fill staging IS the scenario — a half-staged work dir would
            # solve the wrong problem convincingly (measured: a dtype crash
            # in demand subtraction left UNSUBTRACTED demand and the run
            # 'succeeded'). Fail loudly instead.
            import traceback as _tb
            return {
                "ok": False, "returncode": -1,
                "log": "fill staging FAILED:\n" + _tb.format_exc(),
                "calendar": None, "scorecard": None,
                "feasibility": None, "relax_level": None,
                "diag_blockages": "",
            }
        _cs_notes = [f"current-state overlay FAILED: {_exc}"]

    # The reference file stores wall-clock datetimes now, but the WORK-DIR
    # copy still speaks hours — a window ending on a legacy horizon boundary
    # (a stale pre-migration artifact, or a replayed old work dir) silently
    # frees the line mid-plan. Detect and report; never rewrite.
    try:
        _cs_notes.extend(_audit_work_downtimes(work))
    except Exception as _exc:  # noqa: BLE001
        _cs_notes.append(f"downtime horizon audit FAILED: {_exc}")

    # Agent seam: let the caller rewrite the WORK-DIR inputs (never reference/)
    # with every mutation reported into the log. A failing patch aborts the
    # solve — silently running on half-patched inputs would be dishonest.
    if work_dir_patch is not None:
        _patch_notes = work_dir_patch(work)
        _cs_notes.extend(f"[input patch] {n}" for n in (_patch_notes or []))

    scheduler = (Path(data_dir).resolve().parent / "code" / "solver" / "phase2_scheduler.py").resolve()
    toml = work / "flowstate.toml"
    cmd = [
        python_exe or sys.executable,
        str(scheduler),
        "--data-dir", str(work),
        "--objective", scenario["objective"],
        "--config", str(toml.resolve()),
    ]
    if scenario.get("two_phase"):
        cmd.append("--two-phase")
    # Cross-week mode makes the solver ignore --two-phase and solve the whole
    # 336h horizon at once (see phase2_scheduler). Both default to off, so a
    # scenario that does not set them behaves exactly as before.
    if scenario.get("cross_week"):
        cmd.append("--cross-week")
    if scenario.get("cip_flex"):
        cmd.append("--cip-flex")
    # legacy reads time_limit + all weights from the toml; patch the work copy.
    eff_overrides = overrides if overrides is not None else scenario.get("overrides")
    _patch_work_toml(toml, time_limit, eff_overrides)

    # Scenario F: stage the warm start AFTER all input patching — including
    # the toml weight overrides above, which the greedy seed's format-aware
    # candidate ranking reads, and any donor schedule the patch planted.
    # Hours-hashed inputs are untouched by the toml patch, so a seed's input
    # signature still matches the solver's. Default is the greedy
    # construction seed; scenario["warm_start"] = "none"/"prev" gives the
    # overnight batch cold and chained arms (see _stage_warm_start).
    # Best-effort but loud.
    if fill_mode:
        try:
            _cs_notes.extend(_stage_warm_start(work, scenario))
        except Exception as _exc:  # noqa: BLE001
            _cs_notes.append(f"greedy seed FAILED (solving cold): {_exc}")

    _solver_cwd = str(
        (Path(data_dir).resolve().parent / "code" / "solver").resolve())
    _timeout_s = (float(timeout_s) if timeout_s
                  else max(120, (time_limit or 60) * 4 + 60))
    if progress_cb is None:
        proc = subprocess.run(
            cmd, cwd=_solver_cwd, capture_output=True, text=True,
            timeout=_timeout_s,
        )
        _stdout, _stderr = proc.stdout, proc.stderr
    else:
        # Live-progress launch: the solver streams telemetry into the work
        # dir (solver_progress.json — stages, incumbents, gap); poll it
        # every couple of seconds and hand it to the caller's callback so a
        # UI can show a real progress bar instead of a frozen spinner.
        # stdout/stderr go to files (PIPE would deadlock on the big logs).
        import json as _pj
        import time as _time

        _so_p = work / "_solver_stdout.txt"
        _se_p = work / "_solver_stderr.txt"
        with open(_so_p, "w", encoding="utf-8") as _so, \
                open(_se_p, "w", encoding="utf-8") as _se:
            _p = subprocess.Popen(cmd, cwd=_solver_cwd, stdout=_so,
                                  stderr=_se, text=True)
            # Reattach manifest: if the user navigates away Streamlit kills
            # THIS loop but not the solver — generate.py finds the manifest
            # on its next load and resumes (resume_scenario).
            try:
                _write_pending_manifest(
                    work, scenario, time_limit=time_limit,
                    timeout_s=_timeout_s, pid=_p.pid, cs_notes=_cs_notes,
                    started_at=_started_at)
            except Exception:  # noqa: BLE001 — bookkeeping must not kill a solve
                pass
            _t0 = _time.monotonic()
            while True:
                _rc = _p.poll()
                _elapsed = _time.monotonic() - _t0
                _prog = None
                try:
                    _pp = work / "solver_progress.json"
                    if _pp.exists():
                        _prog = _pj.loads(_pp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    _prog = None
                try:
                    progress_cb(_prog, _elapsed)
                except Exception:  # noqa: BLE001 — UI must not kill a solve
                    pass
                if _rc is not None:
                    break
                if _elapsed > _timeout_s:
                    _p.kill()
                    _p.wait(timeout=30)
                    break
                _time.sleep(2.0)

        class _P:  # duck-typed result matching subprocess.run's fields
            returncode = _p.returncode if _p.returncode is not None else -9

        proc = _P()
        _stdout = _so_p.read_text(encoding="utf-8", errors="replace")
        _stderr = _se_p.read_text(encoding="utf-8", errors="replace")
    result = _collect_result(scenario, work, data_dir, proc.returncode,
                             _stdout, _stderr, _cs_notes)
    result["started_at"] = _started_at
    if progress_cb is not None and not result["ok"]:
        clear_pending_manifest(work)  # nothing to save — no reattach needed
    return result


def scenario_version_name(
    scenario: dict[str, Any], started_at: Any = None
) -> str:
    """Honest display name for a scenario run: what the solver actually did
    plus the run's start timestamp (user request 2026-08-19), e.g. a two-pass
    F run -> 'Scenario: Pass 1 - Seed & Fill, Pass 2 - CO Optimized
    26-08-19 10:36'. Every run gets a distinct name (and thus slug) instead
    of silently overwriting the previous run's fixed slot."""
    from datetime import datetime as _dt

    if isinstance(started_at, str):
        try:
            started_at = _dt.fromisoformat(started_at)
        except ValueError:
            started_at = None
    ts = (started_at or _dt.now()).strftime("%y-%m-%d %H:%M")
    if scenario.get("fill_mode"):
        core = ("Pass 1 - Seed & Fill, Pass 2 - CO Optimized"
                if scenario.get("two_pass_co")
                else "Max Fill (committed plan fixed)")
    else:
        raw = str(scenario.get("name") or "Scenario").strip()
        sid = str(scenario.get("id") or "").strip()
        # "Scenario A — Minimum changeovers" -> "Minimum changeovers (A)";
        # custom/planner names pass through untouched (plus their id).
        desc = raw
        head, _, tail = raw.partition("—")
        if tail and head.strip().lower().startswith("scenario"):
            desc = tail.strip()
        core = f"{desc} ({sid})" if sid else desc
    return f"Scenario: {core} {ts}"


def save_scenario_version(
    scenario: dict[str, Any],
    result: dict[str, Any],
    data_dir: Path,
) -> dict[str, Any]:
    """Persist a scenario run as a NEW timestamped version.

    Every run gets its own slot — the name (and slug) carry the run's start
    time, so runs never silently overwrite each other. At capacity the oldest
    AUTO-SAVED version (metadata source solver:/agent:) is evicted; a user-
    named version is never touched, and with nothing evictable the capacity
    error raises. Returns {"slug", "name", "evicted": [evicted slugs]}.
    """
    # A save ATTEMPT retires the reattach manifest either way: the result has
    # reached a screen, so it is no longer silently at risk (a failed save is
    # reported to the user, not retried on every page load).
    clear_pending_manifest(Path(data_dir) / "_scenario_work" / str(scenario.get("id", "")))
    if not result.get("ok") or result.get("calendar") is None:
        raise ValueError("Scenario did not produce a calendar")
    is_custom = bool(scenario.get("custom"))
    source = (
        f"solver-custom:{scenario['objective']}" if is_custom else f"solver:{scenario['objective']}"
    )
    name = scenario_version_name(scenario, started_at=result.get("started_at"))

    sc = result["scorecard"]
    extra = {}
    if result.get("fill_gates"):
        # Fill-window scoring (Compare page) needs the staging gates saved
        # WITH the proposal — they cannot be re-derived from the calendar.
        extra["fill_gates"] = result["fill_gates"]
    before = {v["slug"] for v in list_versions(data_dir)}
    slug = save_version(
        name,
        result["calendar"],
        sc.to_dict() if hasattr(sc, "to_dict") else sc,
        data_dir,
        notes=scenario.get("intent", ""),
        source=source,
        pros="",
        cons="",
        extra_meta=extra or None,
        auto_evict=True,
    )
    after = {v["slug"] for v in list_versions(data_dir)}
    return {"slug": slug, "name": name,
            "evicted": sorted(before - after)}
