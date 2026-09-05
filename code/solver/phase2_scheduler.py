# phase2_scheduler.py — Flowstate Phase 2 factory scheduler (CLI + orchestration).
# Uses aggregate CIP accounting (v3). Phases: sanity1, sanity3, full.

from __future__ import annotations
import argparse
import copy
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from typing import Any, Dict, List, Tuple

import pandas as pd
from ortools.sat.python import cp_model

from data_loader import (Params, Data, Files, available_hours_line, parse_early_fill_hours,
                         parse_due_week_policy, parse_kg_week_weight)
from diagnostics import run_diagnostics, run_unique_line_load_diagnostic, run_blockages_diagnostic
from model_builder import build_model, default_fill_exchange_rate
from validate_schedule import validate_all
from solver_progress import (
    init_progress,
    update_stage,
    set_data_summary,
    add_solution,
    update_solver_stats,
    reset_solver_stats,
    fill_solution_label,
    co_solution_label,
    generic_solution_label,
    STAGES_SINGLE,
    STAGES_TWO_PHASE,
)


class _ProgressCallback(cp_model.CpSolverSolutionCallback):
    """Reports each intermediate solution to solver_progress.json so the
    UI can display real-time objective improvements.

    Pass-aware labels (user request 2026-08-19): the caller says which pass
    this is and hands over the model expressions that carry the planner
    metric — placed kg for a fill pass, weighted changeover load for the CO
    pass — so labels report REAL numbers evaluated from the solution, never
    percentages against a meaningless baseline.
    """

    def __init__(
        self,
        data_dir: Path,
        label_prefix: str = "",
        *,
        direction: str = "min",
        pass_id: str = "",
        pass_name: str = "fill",
        placed_expr: Any = None,
        demand_kg: int = 0,
        co_expr: Any = None,
    ):
        super().__init__()
        self._data_dir = data_dir
        self._prefix = label_prefix
        self._direction = direction
        self._pass_id = pass_id
        self._pass_name = pass_name
        self._placed_expr = placed_expr
        self._demand_kg = int(demand_kg or 0)
        self._co_expr = co_expr
        self._count = 0
        self._first_obj: float | None = None
        self._prev_placed: int | None = None
        self._first_co: int | None = None

    def on_solution_callback(self) -> None:
        self._count += 1
        obj = self.ObjectiveValue()
        wall = self.WallTime()
        bound = self.BestObjectiveBound()
        # Watched planner metrics: evaluated from the incumbent itself. A
        # failed evaluation falls back to the generic label — never invent.
        placed: int | None = None
        co_load: int | None = None
        if self._placed_expr is not None:
            try:
                placed = int(self.Value(self._placed_expr))
            except Exception:  # noqa: BLE001
                placed = None
        if self._co_expr is not None:
            try:
                co_load = int(self.Value(self._co_expr))
            except Exception:  # noqa: BLE001
                co_load = None
        if self._first_obj is None:
            self._first_obj = obj
        if co_load is not None and self._first_co is None:
            self._first_co = co_load
        if self._pass_id == "fill" and placed is not None:
            label = fill_solution_label(
                self._count, placed, self._demand_kg, self._prev_placed,
                pass_name=self._pass_name, prefix=self._prefix)
        elif self._pass_id == "co":
            label = co_solution_label(
                self._count, obj, self._first_obj, co_load, self._first_co,
                prefix=self._prefix)
        else:
            label = generic_solution_label(
                self._count, obj, self._first_obj,
                direction=self._direction, prefix=self._prefix)
        if placed is not None:
            self._prev_placed = placed
        add_solution(
            self._data_dir, wall, obj, label,
            pass_id=self._pass_id or None,
            placed_kg=placed,
            co_load=co_load,
        )
        gap = round(100 * abs(obj - bound) / max(1, abs(obj)), 1) if obj != 0 else 0
        update_solver_stats(
            self._data_dir,
            status="SOLVING",
            best_objective=round(obj, 1),
            best_bound=round(bound, 1),
            gap_pct=gap,
            elapsed_s=round(wall, 1),
            direction=self._direction,
            pass_id=self._pass_id,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flowstate Phase 2 factory scheduler (OR-Tools CP-SAT)."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Directory for input CSVs and output files (default: script directory)",
    )
    parser.add_argument(
        "--phase",
        choices=("sanity1", "sanity3", "full"),
        default="full",
        help="Model phase: sanity1 (no changeover/CIP), sanity3 (changeovers only), full (CIP + changeovers)",
    )
    parser.add_argument(
        "--time-limit",
        type=float,
        default=None,
        help="Solver time limit in seconds (default: 120)",
    )
    parser.add_argument(
        "--relax-demand",
        action="store_true",
        help="Set qty_min=0 for all orders (feasibility check)",
    )
    parser.add_argument(
        "--relax-due",
        action="store_true",
        help="Soft due dates: orders may finish past due_end with a lateness penalty",
    )
    parser.add_argument(
        "--auto-relax",
        action="store_true",
        help="Auto-escalate relax levels on INFEASIBLE (default: on)",
    )
    parser.add_argument(
        "--no-auto-relax",
        action="store_true",
        help="Disable auto-relaxation ladder (single attempt, legacy behavior)",
    )
    parser.add_argument(
        "--ignore-changeovers",
        action="store_true",
        help="Do not enforce changeover setup times",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Run diagnostics only (diag_order_linecap.csv, diag_unique_line_load.csv)",
    )
    parser.add_argument(
        "--max-lines-per-order",
        type=int,
        default=None,
        help="Max lines per order (default: 3)",
    )
    parser.add_argument(
        "--min-run-hours",
        type=int,
        default=None,
        help="Min run hours per (line, order) assignment (default: 4); use 2 or 1 to relax",
    )
    parser.add_argument(
        "--no-week1-in-week0",
        action="store_true",
        help="Disallow Week-1 orders in Week-0 window (default: allow)",
    )
    parser.add_argument(
        "--initial-states",
        type=Path,
        default=None,
        help="Path to InitialStates CSV (default: data-dir/initial_states.csv). Use week1_initial_states.csv for next run.",
    )
    parser.add_argument(
        "--two-phase",
        action="store_true",
        help="Run Week-0 only, then Week-1 only with Week-1 InitialStates from Week-0; merge into one schedule (shows Week-1 even when qty_min=0).",
    )
    parser.add_argument(
        "--objective",
        choices=("balanced", "min-changeovers", "spread-load"),
        default=None,
        help="Objective mode: balanced (default), min-changeovers, spread-load.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run post-solve validation (bounds, overlaps, CIP, changeovers) after scheduling.",
    )
    parser.add_argument(
        "--rolling",
        action="store_true",
        help="Rolling weekly run: auto-load week1_initial_states.csv if it exists, then run --two-phase.",
    )
    parser.add_argument(
        "--cross-week",
        action="store_true",
        help=(
            "Cross-week mode: AZAP's week becomes a weighted soft preference "
            "over the full 336h horizon instead of a hard wall, and the solve "
            "runs single-phase so week-0 and week-1 runs of the same SKU can "
            "merge. Demand quantities stay hard."
        ),
    )
    parser.add_argument(
        "--cip-flex",
        action="store_true",
        help=(
            "RETIRED (no effect since 2026-09-03, fix SB-4): the cip_defer "
            "reward this mode scaled was removed from the objective; a CIP "
            "now costs dur x median line rate kg and may be pulled earlier "
            "freely for changeover absorption. The line's max allowable CIP "
            "interval stays a HARD constraint (food safety). Flag accepted "
            "for compatibility."
        ),
    )
    parser.add_argument(
        "--no-warm-start",
        action="store_true",
        help=(
            "Disable CP-SAT solution hinting from the previous run's schedule "
            "(prev_schedule.csv in the work dir). Hints only steer the search "
            "- they cannot change the feasible set - so this flag exists for "
            "A/B measurement, not for correctness."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to flowstate.toml config file for defaults.",
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help=(
            "Reproducible search: num_search_workers=1 plus the configured "
            "solver_random_seed (0 when unset). The default 8-worker portfolio "
            "is NOT reproducible even with a fixed seed (C80: same inputs + "
            "same seed gave different plans) - use this for A/B evidence, "
            "not for production speed."
        ),
    )
    return parser.parse_args()


_ARGS = _parse_args() if __name__ == "__main__" else argparse.Namespace(
    data_dir=None, phase="full", time_limit=None, relax_demand=False,
    relax_due=False, auto_relax=True, no_auto_relax=False,
    ignore_changeovers=False, diagnose=False, max_lines_per_order=None,
    min_run_hours=None, no_week1_in_week0=False, initial_states=None,
    two_phase=False,
    objective="balanced", validate=False, rolling=False, cross_week=False,
    cip_flex=False, no_warm_start=False, config=None, deterministic=False,
)

# --- Config file loading (Phase 2.2) ---
def _load_config(config_path: Path | None, data_dir: Path) -> dict:
    """Load flowstate.toml if it exists. Returns dict of settings."""
    cfg: dict = {}
    candidates = [config_path] if config_path else [data_dir / "flowstate.toml", data_dir.parent / "flowstate.toml"]
    for p in candidates:
        if p and p.exists():
            try:
                import tomllib  # Python 3.11+
            except ImportError:
                try:
                    import tomli as tomllib  # type: ignore
                except ImportError:
                    break
            with open(p, "rb") as f:
                cfg = tomllib.load(f)
            break
    return cfg


def params_from_config(
    cfg: dict,
    *,
    max_lines_override: int | None = None,
    min_run_override: int | None = None,
    allow_week1: bool = True,
) -> Params:
    """Resolve Params from a loaded flowstate.toml dict.

    The ONE place every tunable reaches P. The custom-scenario override path
    (helpers/scenario_runner._patch_work_toml) writes into the same sections
    this reads, and tests/test_solver_weights.py asserts the mapping stays
    complete — a knob that patches the toml but never lands in P is a silent
    no-op, which is exactly the failure mode this function exists to prevent.
    ``max_lines_override`` / ``min_run_override`` carry CLI flags (they beat
    the toml); ``allow_week1`` carries --no-week1-in-week0.
    """
    P = Params()
    sched = cfg.get("scheduler", {}) or {}
    if sched.get("planning_start_date"):
        P.planning_start_date = sched["planning_start_date"]
    # Horizon length: horizon_hours wins; horizon_weeks * 168 is the fallback
    # so the two keys cannot silently disagree.
    _hz_h = sched.get("horizon_hours")
    if _hz_h is None and sched.get("horizon_weeks") is not None:
        _hz_h = int(sched["horizon_weeks"]) * 168
    if _hz_h:
        P.horizon_h = int(_hz_h)
    _cfg_cip = cfg.get("cip", {}) or {}
    if _cfg_cip.get("interval_h") is not None:
        P.cip_interval_h = int(_cfg_cip["interval_h"])
    if _cfg_cip.get("duration_h") is not None:
        P.cip_duration_h = int(_cfg_cip["duration_h"])
    _cfg_obj = cfg.get("objective", {}) or {}
    if _cfg_obj.get("makespan_weight") is not None:
        P.objective_makespan_weight = int(_cfg_obj["makespan_weight"])
    if _cfg_obj.get("changeover_weight") is not None:
        P.objective_changeover_weight = int(_cfg_obj["changeover_weight"])
    if _cfg_obj.get("cip_defer_weight") is not None:
        P.objective_cip_defer_weight = int(_cfg_obj["cip_defer_weight"])
    if _cfg_obj.get("idle_weight") is not None:
        P.objective_idle_weight = int(_cfg_obj["idle_weight"])
    if _cfg_obj.get("late_weight") is not None:
        P.objective_late_weight = int(_cfg_obj["late_weight"])
    if _cfg_obj.get("week_deviation_weight") is not None:
        P.objective_week_deviation_weight = int(
            _cfg_obj["week_deviation_weight"]
        )
    if _cfg_obj.get("cip_flex_weight") is not None:
        P.objective_cip_flex_weight = int(_cfg_obj["cip_flex_weight"])
    # Percentage, not an integer weight — float() so 2.5 survives.
    if _cfg_obj.get("over_target_reward_pct") is not None:
        P.over_target_reward_pct = float(_cfg_obj["over_target_reward_pct"])
    # Changeover type penalty weights — saved by settings.py into [objective]
    if _cfg_obj.get("co_conv_org_weight") is not None:
        P.co_conv_org_weight = int(_cfg_obj["co_conv_org_weight"])
    if _cfg_obj.get("co_cinn_weight") is not None:
        P.co_cinn_weight = int(_cfg_obj["co_cinn_weight"])
    if _cfg_obj.get("co_flavor_weight") is not None:
        P.co_flavor_weight = int(_cfg_obj["co_flavor_weight"])
    _cfg_co = cfg.get("changeover", {}) or {}
    if _cfg_co.get("topload_weight") is not None:
        P.co_topload_weight = int(_cfg_co["topload_weight"])
    if _cfg_co.get("ttp_weight") is not None:
        P.co_ttp_weight = int(_cfg_co["ttp_weight"])
    if _cfg_co.get("ffs_weight") is not None:
        P.co_ffs_weight = int(_cfg_co["ffs_weight"])
    if _cfg_co.get("casepacker_weight") is not None:
        P.co_casepacker_weight = int(_cfg_co["casepacker_weight"])
    if _cfg_co.get("base_changeover_weight") is not None:
        P.co_base_weight = int(_cfg_co["base_changeover_weight"])
    if _cfg_co.get("conv_org_weight") is not None:
        P.co_conv_org_weight = int(_cfg_co["conv_org_weight"])
    if _cfg_co.get("cinn_weight") is not None:
        P.co_cinn_weight = int(_cfg_co["cinn_weight"])
    if _cfg_co.get("flavor_weight") is not None:
        P.co_flavor_weight = int(_cfg_co["flavor_weight"])
    if _cfg_co.get("cip_req_weight") is not None:
        P.co_cip_req_weight = int(_cfg_co["cip_req_weight"])
    if sched.get("use_sku_rates") is not None:
        P.use_sku_rates = bool(sched["use_sku_rates"])
    # Solve rules ([scheduler]): CLI override beats toml beats Params default.
    if max_lines_override is None:
        max_lines_override = sched.get("max_lines_per_order")
    if max_lines_override is not None:
        P.max_lines_per_order = int(max_lines_override)
    if min_run_override is None:
        min_run_override = sched.get("min_run_hours")
    if min_run_override is not None:
        P.min_run_hours = int(min_run_override)
    if sched.get("min_run_pct_of_qty") is not None:
        P.min_run_pct_of_qty = float(sched["min_run_pct_of_qty"])
    P.allow_week1_in_week0 = bool(allow_week1)
    # Early-fill policy (plant decision 2026-09-04): [scheduler]
    # early_fill_hours = "unbounded" | "none" | <hours>. Absent = None =
    # unbounded — next-week demand may be pre-built as early as free capacity
    # allows, never before an already scheduled MO (model_builder). An
    # integer restores a bounded allowance (48 = the pre-decision value,
    # now applied to EVERY later week, not only the second).
    P.early_fill_hours = parse_early_fill_hours(sched.get("early_fill_hours"))
    # Due-week policy: [scheduler] due_week_policy = "hard" (shipped default,
    # 2026-09-04 night: due_end + 1 is a wall at relax levels 0-1) | "soft"
    # (opt-in, plant decision 2026-09-04 #2: the demand week is a preference
    # in both directions, lateness inside the horizon is priced per kg-week;
    # benchmarked -38 % on-time kg at 600 s, see data_loader.Params). The
    # prices are late_kg_week_weight / early_kg_week_weight in fill units per
    # tonne per week-step (v2 defaults 200,000 / 50,000 = 20 % / 5 % of a
    # tonne's fill value per week; 10 t one week late ~ one FFS change --
    # data_loader.Params and model_builder.dev_kg_coefficients for the
    # one-currency invariant). The late price is soft-only; the early price
    # grades pre-building under both policies.
    P.due_week_policy = parse_due_week_policy(sched.get("due_week_policy"))
    P.late_kg_week_weight = parse_kg_week_weight(
        sched.get("late_kg_week_weight"), "late_kg_week_weight",
        Params.late_kg_week_weight)
    P.early_kg_week_weight = parse_kg_week_weight(
        sched.get("early_kg_week_weight"), "early_kg_week_weight",
        Params.early_kg_week_weight)
    # [scheduler] pass2_makespan_weight (v2 pricing, 2026-09-04): makespan
    # coefficient in the pass-2 fill-exchange objective only. Default 1 (a
    # tiebreaker); 1 kg-eq ~ 1,000 pass-2 units, so 100,000 makes one hour
    # of shorter plan worth 0.1 t of fill. Whole integer >= 0.
    if sched.get("pass2_makespan_weight") is not None:
        P.pass2_makespan_weight = max(0, int(sched["pass2_makespan_weight"]))
    # Opt-in legacy week-0/week-1 gap stitch (fix SB-1 retired the hard
    # form; default off). Read with getattr by model_builder.
    P.legacy_week_stitch = bool(sched.get("legacy_week_stitch", False))
    # Soft demand (Scenario F)
    P.soft_demand = bool(sched.get("soft_demand", False))
    if sched.get("shortfall_weight") is not None:
        P.objective_shortfall_weight = int(sched["shortfall_weight"])
    # CP-SAT random seed: absent = never touch the parameter (CP-SAT keeps
    # its own default), so existing configs behave byte-identically.
    if sched.get("solver_random_seed") is not None:
        P.solver_random_seed = int(sched["solver_random_seed"])
    return P


def apply_solver_seed(solver: "cp_model.CpSolver", P: Params) -> None:
    """[scheduler] solver_random_seed -> CP-SAT parameters, EVERY solve pass.

    The one place the seed reaches the solver — two-phase W0/W1, the
    single-phase ladder, and both pass-2 solves (anchor + real) call this,
    so an arm pinned to seed N really searches with seed N everywhere.
    None (the default) leaves solver.parameters untouched: CP-SAT's own
    default seed stays in force and existing runs are unchanged.
    """
    seed = getattr(P, "solver_random_seed", None)
    if seed is None and DETERMINISTIC:
        # --deterministic without a configured seed: pin CP-SAT's seed so
        # two runs of the same inputs really are the same search.
        seed = 0
    if seed is not None:
        solver.parameters.random_seed = int(seed)


DATA_DIR = _ARGS.data_dir.resolve() if _ARGS.data_dir is not None else BASE_DIR
_CFG = _load_config(_ARGS.config, DATA_DIR)
_CFG_SCHED = _CFG.get("scheduler", {})
# C80 (fix V-5): CP-SAT's 8-worker portfolio is nondeterministic under a
# wall-clock limit even with random_seed pinned (measured: same inputs +
# seed 7 -> three different plans). --deterministic (or [scheduler]
# deterministic = true) runs every solve with ONE worker so a seed is
# reproducible; the anchor solve of the two-pass block is always 1 worker
# (it is a fixed-assignment check, workers add nothing).
DETERMINISTIC = bool(getattr(_ARGS, "deterministic", False)
                     or _CFG_SCHED.get("deterministic", False))
SEARCH_WORKERS = 1 if DETERMINISTIC else 8
ANCHOR_WORKERS = 1

ERR_FILE = DATA_DIR / "solver_error.txt"
KPI_FILE = DATA_DIR / "solver_kpis.txt"
TIME_LIMIT = _ARGS.time_limit or _CFG_SCHED.get("time_limit")
PHASE = _ARGS.phase
RELAX_DEMAND = _ARGS.relax_demand
RELAX_DUE = _ARGS.relax_due
IGNORE_CHANGEOVERS = _ARGS.ignore_changeovers
# Auto-relaxation ladder is ON by default; --no-auto-relax preserves old behavior.
AUTO_RELAX = not _ARGS.no_auto_relax
DIAGNOSE = _ARGS.diagnose
MAX_LINES_PER_ORDER = _ARGS.max_lines_per_order or _CFG_SCHED.get("max_lines_per_order")
MIN_RUN_HOURS_OVERRIDE = _ARGS.min_run_hours or _CFG_SCHED.get("min_run_hours")
NO_WEEK1_IN_WEEK0 = _ARGS.no_week1_in_week0
INITIAL_STATES_PATH = _ARGS.initial_states
TWO_PHASE = _ARGS.two_phase or _ARGS.rolling
VALIDATE = _ARGS.validate or _CFG_SCHED.get("validate", False)
ROLLING = _ARGS.rolling
OBJECTIVE_MODE = _ARGS.objective or _CFG_SCHED.get("objective", "balanced")
# Cross-week / CIP-flex modes. Default OFF everywhere -> current behavior.
CROSS_WEEK = bool(_ARGS.cross_week or _CFG_SCHED.get("cross_week", False))
CIP_FLEX = bool(_ARGS.cip_flex or _CFG_SCHED.get("cip_flex", False))
# Cross-week routing: the two-phase driver solves Week-0 and then Week-1 as
# two separate models, which structurally prevents a week-0 SKU from merging
# with a week-1 run of the same SKU. When cross-week mode is on we must see
# the whole 336h horizon in ONE model, so we force the single-phase path.
# When cross-week is OFF, TWO_PHASE is exactly what it was before.
if CROSS_WEEK and TWO_PHASE:
    TWO_PHASE = False

# Current-state MOs (scenario E): running/queued manprg MOs are solver orders
# locked to their line. When active, the auto-relax ladder skips levels 1-2:
# those keep changeovers but relax demand, so the solver finds a fast
# FEASIBLE that drops the committed MOs. Jumping hard(0) -> level 3
# (ignore_co) keeps the model small and the MOs present (presence is forced
# at level 0; at level 3 demand + due relax but the MO stays locked-line).
USE_CURRENT_MO = bool(_CFG_SCHED.get("use_current_mo", False))
# Historically {0: 3} in current-MO mode: the ladder jumped straight to
# ignore_co because levels 1-2 always proved INFEASIBLE with committed MOs.
# 2026-08-14: the true causes were three presence-gating bugs in the
# changeover block (first-flag and pairwise ordering compared against ABSENT
# orders' collapsed-to-0 intervals; the week-gap stitch classified committed
# MOs), invisible until live gates/initial SKUs arrived. With those fixed,
# level 2 is FEASIBLE on the live dataset with changeovers ENFORCED — the
# skip would now only rob the ladder of its changeover-preserving rungs.
_RELAX_SKIP: dict[int, int] = {}


def _ladder_levels(base_lvl: int, max_lvl: int) -> list[int]:
    """Relax levels to try in order, with current-MO skip applied.

    For ``min-changeovers`` the whole point of the mode is to honour
    changeovers, so the ladder must NEVER escalate to level 3 (``ignore_co``),
    which disables the changeover constraints AND the changeover objective
    term (model_builder gates both behind ``not ignore_co``). A min-changeovers
    solve that relaxes changeovers is a contradiction and silently emits a
    schedule with MORE changeovers than a plain draft. Cap at level 2
    (relax demand + soft due, changeovers still enforced); if even that is
    infeasible, report it honestly rather than drop the objective.
    """
    if OBJECTIVE_MODE == "min-changeovers":
        base_lvl = min(base_lvl, 2)
        max_lvl = min(max_lvl, 2)
    if not _RELAX_SKIP:
        return list(range(base_lvl, max_lvl + 1))
    levels: list[int] = []
    lvl = base_lvl
    while lvl <= max_lvl:
        levels.append(lvl)
        nxt = _RELAX_SKIP.get(lvl)
        if nxt is not None and nxt > lvl:
            # Clamp the skip to max_lvl so a mode cap (e.g. min-changeovers at
            # level 2) is never overshot — otherwise {0:3} + max 2 would jump
            # straight past max and only level 0 would ever be tried.
            lvl = min(nxt, max_lvl)
        else:
            lvl += 1
    return levels


if CROSS_WEEK and TWO_PHASE:
    _CROSS_WEEK_FORCED_SINGLE = True
else:
    _CROSS_WEEK_FORCED_SINGLE = False

# Week boundaries (must match model_builder)
WEEK0_END = 167
WEEK1_START = 168


def reset_err() -> None:
    try:
        if ERR_FILE.exists():
            ERR_FILE.unlink()
    except OSError:
        pass


def log(msg: str) -> None:
    # Every line gets a wall-clock stamp so the UI's solver-journal tail can
    # show WHEN the machinery moved (warm-start, ladder, two-pass, ...).
    try:
        with open(ERR_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%H:%M:%S}] " + msg.rstrip() + "\n")
    except OSError:
        pass


def write_kpi_lines(lines: List[str]) -> None:
    with open(KPI_FILE, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln.rstrip() + "\n")


def write_schedule_meta() -> None:
    """Write schedule_meta.json recording when the solver ran."""
    import json as _json
    meta = {
        "solver_ran_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "edited": False,
    }
    try:
        with open(DATA_DIR / "schedule_meta.json", "w", encoding="utf-8") as f:
            _json.dump(meta, f, indent=2)
    except OSError:
        pass


# ── Auto-relaxation ladder (graceful degradation) ───────────────────────
# Level 0: hard. 1: qty_min relaxed. 2: + soft due dates. 3: + no changeovers.
RELAX_LADDER = [
    {"relax_demand": False, "relax_due": False, "ignore_co": False},
    {"relax_demand": True, "relax_due": False, "ignore_co": False},
    {"relax_demand": True, "relax_due": True, "ignore_co": False},
    {"relax_demand": True, "relax_due": True, "ignore_co": True},
]
RELAX_LABELS = [
    "hard",
    "relax_demand",
    "relax_demand+soft_due",
    "relax_demand+soft_due+ignore_co",
]


def _base_relax_level() -> int:
    """Starting ladder level from explicit CLI flags."""
    if IGNORE_CHANGEOVERS:
        return 3
    if RELAX_DUE:
        return 2
    if RELAX_DEMAND:
        return 1
    return 0


def _read_blocking_lines(data_dir: Path, max_rows: int = 8) -> List[str]:
    """Short summary strings from diag_blockages.csv (if present)."""
    path = data_dir / "diag_blockages.csv"
    if not path.exists():
        return []
    try:
        import csv as _csv
        with open(path, encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
    except (OSError, ValueError):
        return []
    out = []
    for r in rows[:max_rows]:
        out.append(
            f"Line {r.get('line_id')} ({r.get('line_name', '')}) week {r.get('week')}: "
            f"required {r.get('required_run_hours')}h vs available {r.get('available_hours')}h "
            f"(overflow {r.get('overflow_hours')}h)"
        )
    return out


def _orders_short_of_qmin(bounds_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in bounds_rows:
        produced = int(r.get("produced", 0))
        qmin = int(r.get("qty_min", 0))
        if produced < qmin:
            out.append({
                "order_id": r.get("order_id"),
                "sku": r.get("sku"),
                "produced": produced,
                "qty_min": qmin,
            })
    return out


def _late_orders(
    solver: cp_model.CpSolver,
    data: Data,
    vars_dict: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Per-order lateness: hours past the due window (relax_due, or the soft
    due-week policy at every level — plant decision 2026-09-04 #2) plus, when
    the model priced it, the kg that landed after due_end + 1 (`late_kg`) and
    the kg x whole-weeks-late (`late_kg_weeks`, the priced quantity). Under
    the soft policy these are a TRADE-OFF the solver chose (late beats short;
    a small order joins a later campaign when that saves a changeover), not a
    violation — the independent validator reports them as WARN."""
    lateness = vars_dict.get("lateness") or {}
    late_kg = vars_dict.get("late_kg") or {}
    late_steps = vars_dict.get("late_kg_steps") or {}
    if not lateness and not late_kg:
        return []
    hours: Dict[int, int] = {}
    kg: Dict[int, int] = {}
    kgw: Dict[int, int] = {}
    for (_l, o_idx), var in lateness.items():
        v = int(solver.Value(var))
        if v > 0:
            hours[o_idx] = hours.get(o_idx, 0) + v
    for (_l, o_idx), expr in late_kg.items():
        v = int(solver.Value(expr))
        if v > 0:
            kg[o_idx] = kg.get(o_idx, 0) + v
    for (_l, o_idx), exprs in late_steps.items():
        v = sum(int(solver.Value(e)) for e in exprs)
        if v > 0:
            kgw[o_idx] = kgw.get(o_idx, 0) + v
    idxs = set(hours) | set(kg)
    rows = [
        {
            "order_id": data.orders[o_idx]["order_id"],
            "sku": data.orders[o_idx]["sku"],
            "lateness_h": hours.get(o_idx, 0),
            "late_kg": kg.get(o_idx, 0),
            "late_kg_weeks": kgw.get(o_idx, 0),
        }
        for o_idx in idxs
    ]
    return sorted(rows, key=lambda r: (-r["late_kg_weeks"], -r["lateness_h"]))


def _early_orders(
    solver: cp_model.CpSolver,
    data: Data,
    vars_dict: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Per-order kg produced BEFORE due_start (`early_kg`) and kg x whole
    weeks early (`early_kg_weeks`) — the priced pre-building of the soft
    due-week policy (decision #1 allows it, #2 prices it). Empty when the
    model priced nothing."""
    early_kg = vars_dict.get("early_kg") or {}
    early_steps = vars_dict.get("early_kg_steps") or {}
    if not early_kg:
        return []
    kg: Dict[int, int] = {}
    kgw: Dict[int, int] = {}
    for (_l, o_idx), expr in early_kg.items():
        v = int(solver.Value(expr))
        if v > 0:
            kg[o_idx] = kg.get(o_idx, 0) + v
    for (_l, o_idx), exprs in early_steps.items():
        v = sum(int(solver.Value(e)) for e in exprs)
        if v > 0:
            kgw[o_idx] = kgw.get(o_idx, 0) + v
    rows = [
        {
            "order_id": data.orders[o_idx]["order_id"],
            "sku": data.orders[o_idx]["sku"],
            "early_kg": kg[o_idx],
            "early_kg_weeks": kgw.get(o_idx, 0),
        }
        for o_idx in kg
    ]
    return sorted(rows, key=lambda r: -r["early_kg_weeks"])


def _dev_kg_week_totals(solver: cp_model.CpSolver, vars_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Report fields for the soft due-week policy: the policy in force and the
    solved kg-week totals (0 when the model built no deviation terms)."""
    def _val(expr) -> int:
        if isinstance(expr, (int, float)):
            return int(expr)
        try:
            return int(solver.Value(expr))
        except Exception:  # noqa: BLE001 - telemetry, never a gate
            return 0
    return {
        "due_week_policy": str(vars_dict.get("due_week_policy") or "hard"),
        "late_kg_weeks": _val(vars_dict.get("late_kg_weeks", 0)),
        "early_kg_weeks": _val(vars_dict.get("early_kg_weeks", 0)),
    }


def _week_moved_orders(
    solver: cp_model.CpSolver,
    data: Data,
    vars_dict: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Orders that ran outside their AZAP week (cross_week mode only).

    Empty list when cross_week was off, so the report shape is unchanged.
    """
    week_dev = vars_dict.get("week_dev") or {}
    if not week_dev:
        return []
    out: Dict[int, Dict[str, Any]] = {}
    _wk_boundary = _two_phase_boundary(data.orders)
    for (_l, o_idx), (early_v, late_v) in week_dev.items():
        e = int(solver.Value(early_v))
        lt = int(solver.Value(late_v))
        if e <= 0 and lt <= 0:
            continue
        rec = out.setdefault(o_idx, {
            "order_id": data.orders[o_idx]["order_id"],
            "sku": data.orders[o_idx]["sku"],
            # demand-derived frame (fix SB-1): first week = 0, later = 1
            "azap_week": 0 if int(data.orders[o_idx]["due_start"]) < _wk_boundary else 1,
            "hours_earlier": 0,
            "hours_later": 0,
        })
        rec["hours_earlier"] += e
        rec["hours_later"] += lt
    return sorted(
        out.values(),
        key=lambda r: -(r["hours_earlier"] + r["hours_later"]),
    )


# ── Fix V helpers (audit 2026-09-03: C81/C83/C84/C85/C63/C80) ──────────────
# Validator ERROR codes that mean the plan cannot be run on the floor. A
# schedule carrying any of these must not be promoted without an explicit
# planner override (pages/generate.py, pages/compare.py read the count from
# feasibility_report.json["validation"]["physical_errors"]).
PHYSICAL_ERROR_CODES = (
    "OVERLAP", "IN_DOWNTIME", "CIP_INTERVAL", "CHANGEOVER_GAP", "BEFORE_GATE",
)

# mo_changes.csv (VIF write-back) columns. The first twelve are the historical
# record; the rest were added 2026-09-03 (writeback-5/6/7 + agent W handoff):
# one row per produced PIECE (piece = 1..split_count), where orig_start_h came
# from, wall-clock start/end of the piece, the anchor datetime hour 0 refers to
# (so the hours are usable in VIF), the work dir / scenario and a timestamp.
MO_CHANGES_COLUMNS = [
    "mo", "line_name", "sku", "source", "orig_qty_kg", "new_qty_kg",
    "delta_kg", "orig_start_h", "new_start_h", "new_end_h", "split_count",
    "reason", "piece", "orig_start_src", "new_start_dt", "new_end_dt",
    "planning_anchor", "scenario_id", "generated_at",
]


def _model_warnings(vars_dict: Dict[str, Any] | None) -> List[str]:
    """Model-build warnings (fix SB-3: lines already past their CIP interval
    at the availability gate get the clean pinned at the gate). Surfaced in
    the run log, feasibility_report.json["model_warnings"] and Generate
    (INTEGRATE, agent SB handoff V-1)."""
    return [str(w) for w in ((vars_dict or {}).get("warnings") or [])]


def _log_model_warnings(vars_dict: Dict[str, Any] | None, label: str = "") -> List[str]:
    ws = _model_warnings(vars_dict)
    for w in ws:
        log(f"[model] WARNING{label}: {w}")
    return ws


def _two_phase_boundary(orders: List[dict]) -> int:
    """Hour at which the two-phase driver cuts week 0 from week 1.

    = the SECOND distinct demand due_start (model_builder.week_frame, fix
    SB-1), falling back to the legacy Monday constant when the demand file
    holds a single week. The old test `due_end <= 167` / `due_start >= 168`
    was right only on a Monday-anchored frame: on the live Wednesday-anchored
    Scenario F frame (W1 = 120-287) a W1 order fell in NEITHER class and was
    silently dropped from the two-phase solve (INTEGRATE, agent SB handoff
    V-3).
    """
    try:
        from model_builder import week_frame
        second = week_frame(orders).get("second_start")
    except Exception:  # noqa: BLE001 - never lose a solve over telemetry
        second = None
    return int(second) if second is not None else WEEK1_START


def _sub_phase_params(P: Params, *, horizon_h: int,
                      min_run_override: int | None = None) -> Params:
    """Params for a two-phase sub-solve (C84 / config-5 / quality-3).

    dataclasses.replace copies EVERY field of the parent solve and overrides
    only the three the sub-phase deliberately changes: the horizon, the
    week-1-in-week-0 rule (off inside a single-week model) and the CLI
    min-run override. The previous hand-written constructor silently dropped
    use_sku_rates, soft_demand, objective_shortfall_weight,
    over_target_reward_pct and solver_random_seed - and would have dropped
    every future field too.
    """
    import dataclasses
    return dataclasses.replace(
        P,
        horizon_h=int(horizon_h),
        allow_week1_in_week0=False,
        min_run_hours=(int(min_run_override) if min_run_override is not None
                       else P.min_run_hours),
    )


def _level_report_fields(level: int, vars_dict: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Honest labels for a relax level (C63 / orchestration-1).

    Level 3 (ignore_co) drops the changeover ordering + setup-time block in
    model_builder, so its plans can put two SKUs back-to-back with 0 h of
    setup. ``setup_times_enforced`` says so; a model that keeps setup TIME at
    level 3 (model-side fix S12) can announce it by putting
    ``vars_dict["setup_times_enforced"] = True`` and this report follows.
    """
    ignore_co = bool(RELAX_LADDER[level]["ignore_co"])
    kept_by_model = bool((vars_dict or {}).get("setup_times_enforced", False))
    return {
        "ignore_co": ignore_co,
        "setup_times_enforced": (not ignore_co) or kept_by_model,
    }


def _keep_committed_mo_bounds(model: Any, vars_dict: Dict[str, Any], data: Data,
                              level: int) -> Tuple[str, int]:
    """C85 / orchestration-9: relax levels must not zero a committed MO's qty_min.

    model_builder applies ``relax_demand`` (levels >= 1) to EVERY order, so a
    running/queued manprg MO could be trimmed to a 4 h stub and reported as
    'tonnage_trim' - the plant's committed contract silently broken. Here the
    orchestrator re-imposes produced >= qty_min for is_current_mo orders:
      level 0  - bounds are hard already ("n/a")
      level 1  - relax_demand for DEMAND orders only; MO bounds kept
      level 2  - + soft due dates: an MO may now finish past its manprg
                 window (lateness is priced), but its tonnage is still kept
      level 3  - last resort (ignore_co): MO bounds are RELEASED so the ladder
                 can still end FEASIBLE; the report/log say so loudly and
                 mo_changes.csv records every trim.
    Returns (state, n_orders) with state in {"n/a", "kept", "released"}.
    """
    cur = [(i, o) for i, o in enumerate(data.orders) if o.get("is_current_mo")]
    if level <= 0 or not cur:
        return ("n/a", 0)
    if level >= 3:
        return ("released", len(cur))
    produced = vars_dict.get("produced")
    if produced is None:
        return ("n/a", 0)
    n = 0
    for i, o in cur:
        try:
            var = produced[i]
        except (KeyError, IndexError, TypeError):
            continue
        model.Add(var >= int(o["qty_min"]))
        n += 1
    return ("kept", n)


def _co_load_value(solver: cp_model.CpSolver, vars_dict: Dict[str, Any]) -> int | None:
    """Weighted changeover load of a solved model (int when no CO term exists)."""
    co = vars_dict.get("co_load")
    try:
        if co is None:
            return None
        if isinstance(co, int):
            return int(co)
        return int(solver.Value(co))
    except Exception:  # noqa: BLE001
        return None


def _adopt_pass2(co1: int | None, co2: int | None,
                 err1: int | None, err2: int | None, *,
                 obj_anchor: float | None = None,
                 obj2: float | None = None) -> Tuple[bool, str]:
    """C83 / orchestration-7: pass 2 replaces pass 1 only when it is not worse.

    A FEASIBLE-but-unimproved pass 2 (anchor failed, time ran out) used to be
    adopted blindly. "Not worse" is measured on pass 2's OWN objective when
    both sides are known (INTEGRATE, 2026-09-03): since fix SA-3 pass 2
    minimizes `co_load x 100 x K - prod_score + ...`, i.e. it prices fill at
    the exchange rate K and may legitimately ACCEPT a higher changeover load
    for materially more fill (live_F t60: +870 t of fill for +13 % co_load
    was discarded by the co_load-only rule). `obj_anchor` is the pass-2
    objective of pass 1's own plan (the anchor solve), `obj2` the pass-2
    result: obj2 <= obj_anchor means pass 2 improved on pass 1 by pass 2's
    yardstick. Without the objective pair the legacy rule applies:
    co_load_2 <= co_load_1. In both cases the validator's PHYSICAL error
    count must not grow (err1/err2; unknown counts cannot veto — they are
    logged as such).
    """
    if obj_anchor is not None and obj2 is not None:
        if obj2 > obj_anchor + 1e-6:
            return False, (f"pass-2 objective {obj2:,.0f} > pass 1's plan {obj_anchor:,.0f}"
                           " (hint not honoured)")
        note = f"pass-2 objective {obj2:,.0f} <= pass 1's plan {obj_anchor:,.0f}"
        if co1 is not None and co2 is not None:
            note += f" (co_load {co2:,} vs {co1:,})"
    else:
        if co1 is None or co2 is None:
            return False, "changeover load could not be evaluated on both passes"
        if co2 > co1:
            return False, f"co_load {co2:,} > pass 1's {co1:,}"
        note = f"co_load {co2:,} <= {co1:,}"
    if err1 is not None and err2 is not None and err2 > err1:
        return False, note + f"; but physical validator errors {err2} > pass 1's {err1}"
    if err1 is None or err2 is None:
        note += " (validator error counts unknown - not compared)"
    else:
        note += f", physical validator errors {err2} <= {err1}"
    return True, note


def _validation_summary(rep_dict: Dict[str, Any], top_n: int = 12) -> Dict[str, Any]:
    """Compact validation record for feasibility_report.json (C81 / V-1)."""
    counts = rep_dict.get("counts") or {}
    physical = sum(int((counts.get(c) or {}).get("ERROR", 0)) for c in PHYSICAL_ERROR_CODES)
    errs = [v for v in (rep_dict.get("violations") or []) if v.get("severity") == "ERROR"]
    top = [
        f"{v.get('code')} line={v.get('line') or '-'} order={v.get('order') or '-'}"
        + (f" [{v.get('hours'):g}h]" if v.get("hours") is not None else "")
        + f" {v.get('detail', '')}"
        for v in errs[:top_n]
    ]
    return {
        "ok": bool(rep_dict.get("ok")),
        "n_errors": int(rep_dict.get("n_errors") or 0),
        "n_warnings": int(rep_dict.get("n_warnings") or 0),
        "physical_errors": int(physical),
        "physical_error_codes": list(PHYSICAL_ERROR_CODES),
        "counts": counts,
        "top_violations": top,
        "checks_run": len(rep_dict.get("checks_run") or []),
    }


def _run_independent_validation(data_dir: Path, *, schedule: Any = None,
                                cips: Any = None, write_txt: bool = True,
                                label: str = "") -> Dict[str, Any]:
    """Run code/solver/independent_validator.validate_work_dir (C81 / V-1).

    Runs after EVERY solve that produced a schedule. Returns the compact
    summary that goes into feasibility_report.json["validation"]; the full
    text report is written to validation_independent.txt when the on-disk
    schedule is validated. Never raises - a validator failure is recorded as
    ok=None so the UI can say "not validated" instead of "clean".
    """
    try:
        from independent_validator import validate_work_dir

        rep = validate_work_dir(
            Path(data_dir), schedule=schedule, cips=cips,
            config=(_ARGS.config if getattr(_ARGS, "config", None) else None),
        )
        summ = _validation_summary(rep.to_dict())
        if write_txt:
            try:
                (Path(data_dir) / "validation_independent.txt").write_text(
                    rep.summary(), encoding="utf-8")
            except OSError:
                pass
        log(f"[validate] independent validator{label}: errors={summ['n_errors']} "
            f"(physical {summ['physical_errors']}), warnings={summ['n_warnings']}, "
            f"checks={summ['checks_run']}")
        for t in summ["top_violations"][:8]:
            log("[validate]   " + t)
        return summ
    except Exception as exc:  # noqa: BLE001
        log(f"[validate] independent validator FAILED{label}: {type(exc).__name__}: {exc}")
        return {"ok": None, "n_errors": None, "n_warnings": None,
                "physical_errors": None, "error": f"{type(exc).__name__}: {exc}"}


def _prev_solve_trust(data_dir: Path) -> Tuple[int | None, str | None]:
    """(relax_level, input_sig) of the previous schedule, for the warm-start gates."""
    import json as _json
    for _name in ("prev_feasibility.json", "feasibility_report.json"):
        _fr = Path(data_dir) / _name
        if _fr.exists():
            try:
                _prev = _json.loads(_fr.read_text(encoding="utf-8"))
                return _prev.get("relax_level"), _prev.get("input_sig")
            except Exception:  # noqa: BLE001
                return None, None
    return None, None


def _idle_kpi_summary(data_dir: Path) -> List[str]:
    idle_kpi_path = Path(data_dir) / "idle_kpis.csv"
    if not idle_kpi_path.exists():
        return []
    try:
        import csv
        with open(idle_kpi_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        n = len(rows)
        t_idle = sum(int(r.get("idle_h", 0)) for r in rows)
        t_prod = sum(int(r.get("production_h", 0)) for r in rows)
        t_span = sum(int(r.get("span_h", 0)) for r in rows)
        m_idle = sorted(int(r.get("idle_h", 0)) for r in rows)[n // 2] if n else 0
        util = round(100 * t_prod / t_span, 1) if t_span > 0 else 0.0
        return [f"Idle KPIs: {n} lines, total_idle={t_idle}h, median_idle={m_idle}h, utilization={util}%"]
    except (OSError, KeyError, ValueError, TypeError):
        return []


def _write_single_phase_outputs(solver: cp_model.CpSolver, data: Data, P: Params,
                                data_dir: Path, vars_dict: Dict[str, Any], *,
                                level: int, status_name: str,
                                extra: Dict[str, Any] | None = None,
                                announce_stage: bool = True) -> Dict[str, Any]:
    """Write EVERY single-phase output for one solved model and return the report.

    schedule_phase2.csv, cip_windows.csv, produced_vs_bounds.csv,
    mo_changes.csv, week1_initial_states.csv, idle_kpis.csv, solver_kpis.txt,
    feasibility_report.json (with the independent validation record).
    Called for pass 1 BEFORE pass 2 starts (C80: a pass-2 abort can no
    longer lose the schedule) and again when pass 2 is adopted.
    """
    if announce_stage:
        update_stage(data_dir, "writing_output", "active")
    _sched_rows, bounds_rows = write_solution(solver, data, P, data_dir, vars_dict)
    if announce_stage:
        update_stage(data_dir, "writing_output", "done", "Schedule and KPIs saved")
    fields = _level_report_fields(level, vars_dict)
    kpi = [f"Status: {status_name}", f"Relax level: {level} ({RELAX_LABELS[level]})"]
    if not fields["setup_times_enforced"]:
        kpi.append("UNSAFE: changeover times not enforced (relax level 3)")
    write_kpi_lines(kpi + _idle_kpi_summary(data_dir))
    report: Dict[str, Any] = {
        "relax_level": level,
        "relax_mode": RELAX_LABELS[level],
        "status": status_name,
        "solver_status": status_name,
        "orders_short_of_qmin": _orders_short_of_qmin(bounds_rows),
        "late_orders": _late_orders(solver, data, vars_dict),
        # Soft due weeks (2026-09-04 #2): pre-built kg and the kg-week totals
        # the objective priced; late_orders above carries late_kg per order.
        "early_orders": _early_orders(solver, data, vars_dict),
        "cross_week": CROSS_WEEK,
        "cip_flex": CIP_FLEX,
        "week_moved_orders": _week_moved_orders(solver, data, vars_dict),
        "deterministic": DETERMINISTIC,
        "search_workers": SEARCH_WORKERS,
    }
    report.update(_dev_kg_week_totals(solver, vars_dict))
    report.update(fields)
    report["model_warnings"] = _model_warnings(vars_dict)
    if extra:
        report.update(extra)
    if not fields["setup_times_enforced"]:
        log("UNSAFE: changeover times not enforced (relax level 3 / ignore_co) - "
            "this plan may put two SKUs back-to-back with no setup time")
    if _sched_rows:
        report["validation"] = _run_independent_validation(data_dir)
    else:
        report["validation"] = {"ok": None, "n_errors": None, "n_warnings": None,
                                "physical_errors": None, "note": "no schedule rows"}
    write_feasibility_report(data_dir, report)
    return report


# Set once P is resolved; write_feasibility_report stamps it into every
# report so the probes know the demand contract of the artifact they read.
_SOFT_DEMAND_ACTIVE = False


def input_signature(data_dir: Path) -> str:
    """Fingerprint of the solve inputs (md5 over the staged input files).

    Warm-start hints from a previous schedule are only trustworthy when the
    inputs that shaped it are the SAME inputs being solved now. Measured
    2026-08-14 (dry-run 6): new trial-downtime windows landed exactly where
    the previous schedule had placed blocks, the (level-legitimate) hints
    steered the search into walls, and level 2 starved to UNKNOWN.
    """
    import hashlib
    h = hashlib.md5()
    for name in ("capabilities_rates.csv", "changeovers.csv",
                 "demand_plan.csv", "downtimes.csv", "initial_states.csv",
                 "line_cip_hrs.csv", "line_rates.csv", "sku_info.csv",
                 "current_mo.csv"):
        p = Path(data_dir) / name
        if p.exists():
            try:
                h.update(name.encode())
                h.update(p.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def write_feasibility_report(data_dir: Path, report: Dict[str, Any]) -> None:
    """Write feasibility_report.json — always, on any solve outcome."""
    import json as _json
    report.setdefault("generated_at", datetime.now().isoformat(timespec="seconds"))
    report.setdefault("orders_short_of_qmin", [])
    report.setdefault("late_orders", [])
    report.setdefault("early_orders", [])
    report.setdefault("late_kg_weeks", 0)
    report.setdefault("early_kg_weeks", 0)
    report.setdefault("blocking_lines", _read_blocking_lines(data_dir))
    report.setdefault("input_sig", input_signature(data_dir))
    report.setdefault("soft_demand", _SOFT_DEMAND_ACTIVE)
    try:
        with open(data_dir / "feasibility_report.json", "w", encoding="utf-8") as f:
            _json.dump(report, f, indent=2)
    except OSError:
        pass


def _handle_infeasible(
    P: Params,
    data: Data,
    data_dir: Path,
    *,
    level: int,
    solver_status: str,
    week_label: str,
    two_phase: bool,
    extra: Dict[str, Any] | None = None,
) -> None:
    """Terminal INFEASIBLE at max relax level: report + diagnose + exit nonzero."""
    log(
        f"[auto-relax] {week_label} INFEASIBLE at max relax level "
        f"{level} ({RELAX_LABELS[level]})"
    )
    try:
        run_blockages_diagnostic(P, data, data_dir, two_phase=two_phase)
    except Exception:
        log("[auto-relax] blockages diagnostic failed:\n" + traceback.format_exc())
    report: Dict[str, Any] = {
        "relax_level": level,
        "relax_mode": RELAX_LABELS[level],
        "status": "INFEASIBLE",
        "solver_status": solver_status,
        "week": week_label,
        "blocking_lines": _read_blocking_lines(data_dir),
    }
    if extra:
        report.update(extra)
    write_feasibility_report(data_dir, report)
    write_kpi_lines([
        f"Status: INFEASIBLE at max relax level ({week_label}, {RELAX_LABELS[level]})"
    ])
    raise SystemExit(2)


def compute_idle_kpis(
    schedule_rows: List[Dict[str, Any]],
    cip_rows: List[Dict[str, Any]],
    data_dir: Path,
) -> List[str]:
    """Compute per-line idle-gap KPIs from the schedule and CIP windows.

    Returns KPI summary lines and writes ``idle_kpis.csv`` to *data_dir*.
    Idle time = span − production − CIP hours − changeover dead-time
    (changeover time is not tracked separately, so it is included in idle here).
    """
    # Group production segments by line
    by_line: Dict[int, List[Tuple[int, int, int]]] = {}
    line_names: Dict[int, str] = {}
    for row in schedule_rows:
        l = row["line_id"]
        by_line.setdefault(l, []).append(
            (int(row["start_hour"]), int(row["end_hour"]), int(row["run_hours"]))
        )
        line_names[l] = row.get("line_name", f"L{l}")

    # Group CIP blocks by line
    cip_by_line: Dict[int, int] = {}
    for row in cip_rows:
        l = row["line_id"]
        cip_by_line[l] = cip_by_line.get(l, 0) + (
            int(row["end_hour"]) - int(row["start_hour"])
        )

    kpi_rows: List[Dict[str, Any]] = []
    total_idle = 0
    total_prod = 0
    for l in sorted(by_line.keys()):
        segs = sorted(by_line[l], key=lambda x: x[0])
        first_start = segs[0][0]
        last_end = max(s[1] for s in segs)
        span = last_end - first_start
        prod_hours = sum(s[2] for s in segs)
        cip_hours = cip_by_line.get(l, 0)
        idle = max(0, span - prod_hours - cip_hours)
        util_pct = round(100 * prod_hours / span, 1) if span > 0 else 0.0
        total_idle += idle
        total_prod += prod_hours
        kpi_rows.append({
            "line_id": l,
            "line_name": line_names.get(l, f"L{l}"),
            "span_h": span,
            "production_h": prod_hours,
            "cip_h": cip_hours,
            "idle_h": idle,
            "utilization_pct": util_pct,
        })

    # Write CSV
    if kpi_rows:
        pd.DataFrame(kpi_rows).to_csv(data_dir / "idle_kpis.csv", index=False)

    # Summary lines for solver_kpis.txt
    n_lines = len(kpi_rows)
    median_idle = sorted(r["idle_h"] for r in kpi_rows)[n_lines // 2] if n_lines else 0
    total_span = sum(r["span_h"] for r in kpi_rows)
    overall_util = round(100 * total_prod / total_span, 1) if total_span > 0 else 0.0
    return [
        f"Idle KPIs: {n_lines} lines, total_idle={total_idle}h, median_idle={median_idle}h, utilization={overall_util}%",
    ]


def compute_cip_windows(
    schedule_rows: List[Dict[str, Any]],
    data: Data,
    P: Params,
) -> List[Dict[str, Any]]:
    """Place 6h CIP blocks in gaps between production only (no overlap with production).
    CIPs are due every 120 production hours (including carryover); each is placed in the
    first gap after that run-hour mark that has at least 6h free.

    CIP absorption: when a gap already includes changeover dead-time (SKU switch),
    the CIP absorbs up to cip_duration_h of that changeover. If the gap includes
    both a changeover and enough room for CIP, the CIP start is offset to fill the
    gap from the beginning (changeover + CIP overlap).
    """
    interval_h = P.cip_interval_h
    duration_h = P.cip_duration_h
    by_line: Dict[int, List[Tuple[int, int, str]]] = {}  # (start, end, sku)
    line_names: Dict[int, str] = {}
    for row in schedule_rows:
        l = row["line_id"]
        by_line.setdefault(l, []).append((row["start_hour"], row["end_hour"], str(row["sku"])))
        line_names[l] = row.get("line_name", f"L{l}")

    cip_rows: List[Dict[str, Any]] = []
    for l in sorted(by_line.keys()):
        segments = sorted(by_line[l], key=lambda x: x[0])
        carryover = int(data.init_map.get(l, {}).get("carryover_run_hours", 0))
        name = line_names[l]

        # Gaps between consecutive production segments
        gaps: List[Tuple[int, int, int]] = []  # (g_start, g_end, changeover_hours)
        for i in range(len(segments) - 1):
            g_start = segments[i][1]
            g_end = segments[i + 1][0]
            sku_from = segments[i][2]
            sku_to = segments[i + 1][2]
            co_h = data.setup.get((sku_from, sku_to), 0) if sku_from != sku_to else 0
            if g_end > g_start:
                gaps.append((g_start, g_end, co_h))

        # Run-hours completed before each gap
        run_before_gap: List[int] = []
        run_done = carryover
        for i, (s, e, _sku) in enumerate(segments):
            run_done += e - s
            run_before_gap.append(run_done)

        # How many CIPs are needed
        total_run = run_done
        n_cip = 0
        r = carryover
        while r + interval_h <= total_run:
            n_cip += 1
            r += interval_h

        # Place each CIP; absorb changeover when possible
        used_gap = 0
        for cip_num in range(n_cip):
            required_run = carryover + (cip_num + 1) * interval_h
            placed = False
            for j in range(used_gap, len(gaps)):
                if run_before_gap[j] < required_run:
                    continue
                g_start, g_end, co_h = gaps[j]
                gap_len = g_end - g_start
                # CIP absorbs changeover: effective CIP time = max(duration_h, co_h)
                # (CIP includes the changeover if co_h <= duration_h)
                effective_cip = max(duration_h, co_h)
                if gap_len < effective_cip:
                    continue
                # Place CIP at gap start (absorbs changeover)
                cip_end = g_start + effective_cip
                cip_rows.append({
                    "line_id": l,
                    "line_name": name,
                    "start_hour": g_start,
                    "end_hour": cip_end,
                    "absorbed_changeover_h": min(co_h, duration_h),
                })
                used_gap = j + 1
                placed = True
                break
            if not placed:
                break
    return cip_rows


def extract_cip_windows(
    solver: cp_model.CpSolver,
    data: Data,
    cip_vars: Dict,
    hour_offset: int = 0,
) -> List[Dict[str, Any]]:
    """Extract CIP positions from solver solution (explicit CIP interval variables)."""
    cip_rows: List[Dict[str, Any]] = []
    for l in sorted(cip_vars.keys()):
        for cip_idx, (s_var, e_var, is_present) in enumerate(cip_vars[l]):
            if solver.BooleanValue(is_present):
                s = solver.Value(s_var) + hour_offset
                e = solver.Value(e_var) + hour_offset
                cip_rows.append({
                    "line_id": l,
                    "line_name": data.line_names.get(l, f"L{l}"),
                    "start_hour": s,
                    "end_hour": e,
                })
    return cip_rows


def write_week1_initial_states(
    schedule_rows: List[Dict[str, Any]],
    cip_rows: List[Dict[str, Any]],
    data: Data,
    P: Params,
    data_dir: Path,
    set_available_from_schedule: bool = False,
) -> None:
    """Write week1_initial_states.csv: state of each line at end of schedule.
    set_available_from_schedule=True: set available_from_hour to last production end (for two-phase Phase 2).
    set_available_from_schedule=False: set available_from_hour=0 (for rolling/final states).
    """
    try:
        anchor = datetime.strptime(P.planning_start_date, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        anchor = datetime(2026, 2, 15, 0, 0, 0)

    # Per line: last CIP end hour (0 if no CIP)
    cip_by_line: Dict[int, List[Dict[str, Any]]] = {}
    for row in cip_rows:
        l = row["line_id"]
        cip_by_line.setdefault(l, []).append(row)
    last_cip_end_by_line: Dict[int, int] = {}
    for l, rows in cip_by_line.items():
        last_cip_end_by_line[l] = max(r["end_hour"] for r in rows) if rows else 0

    # Per line: production segments
    prod_by_line: Dict[int, List[Dict[str, Any]]] = {}
    for row in schedule_rows:
        l = row["line_id"]
        prod_by_line.setdefault(l, []).append({
            "start_hour": row["start_hour"],
            "end_hour": row["end_hour"],
            "run_hours": row["run_hours"],
            "sku": row["sku"],
        })

    out_rows: List[Dict[str, Any]] = []
    for l in sorted(data.lines):
        line_name = data.line_names.get(l, f"L{l}")
        prods = prod_by_line.get(l, [])
        last_cip_end = last_cip_end_by_line.get(l, 0)
        # Original running-MO gate (available_from in the CURRENT schedule's
        # initial_states). A line may have had NO week-0 production because its
        # running MO occupies it past hour 168 (e.g. P10 gate=170h). That gate
        # must survive into Phase 2 — otherwise week-1 demand is placed at
        # hour ~0, overlapping the still-running MO (contract C1).
        orig_gate = int(data.init_map.get(l, {}).get("available_from", 0) or 0)
        # Original CIP carryover (clock hours since the line's last CIP before
        # the CURRENT horizon). A gated line that produced nothing in week-0
        # still carries its pre-horizon CIP clock into week-1 — if we reset it
        # to 0 here, Phase-2 thinks the line is freshly cleaned and schedules
        # the next CIP far too late (e.g. P10 carryover 133h -> 0, CIP pushed
        # to hour 314 instead of ~170), violating the mandatory CIP interval.
        orig_carry = int(
            data.init_map.get(l, {}).get("carryover_run_hours", 0) or 0
        )

        # Fix SB-6 (audit C50 / cip-11, 2026-09-03). The carry is the WALL-
        # CLOCK age of the line's last clean at hour 0 of the frame the
        # week-1 model runs in — and that frame is the SAME anchor as week 0
        # (Phase 2 keeps P.horizon_h and gates each line at its week-0 end;
        # week-1 orders get due_start 0). model_builder's trigger is
        # `last_end + carry >= k x interval` on absolute hours, so:
        #   * no CIP in week 0  -> the pre-horizon carry is still the truth
        #     (the old code REPLACED it with the week-0 run hours, dropping
        #     the pre-horizon dirty time, and double-counting week-0 hours
        #     that the absolute clock already sees);
        #   * a CIP in week 0 ending at hour c -> the clean is c hours AFTER
        #     t0, i.e. carry = -c (negative is a valid input: the model's
        #     deadline becomes interval - carry = c + interval, the same
        #     absolute deadline the last_cip_end_datetime column carries,
        #     and the validator's cip_ref_hour = -carry = c);
        #   * clamp to the LINE's interval - 1 (line_cip_hrs), not a
        #     hard-coded 119 — a 144 h line with 130 h of carry was reset to
        #     119 and given 25 h of clock it did not have.
        line_interval = int(data.cip_interval_map.get(l, P.cip_interval_h) or 0)
        carry_cap = line_interval - 1 if line_interval > 1 else orig_carry
        if not prods:
            initial_sku = str(data.init_map.get(l, {}).get("initial_sku", "CLEAN"))
            carryover = orig_carry
            last_cip_dt = ""
            last_end_hour = 0
        else:
            last_run = max(prods, key=lambda x: x["end_hour"])
            initial_sku = str(last_run["sku"])
            if last_cip_end > 0 and l in cip_by_line:
                carryover = -int(last_cip_end)
            else:
                carryover = orig_carry
            last_end_hour = max(p["end_hour"] for p in prods)
            if last_cip_end > 0 and l in cip_by_line:
                last_cip_dt = (anchor + timedelta(hours=last_cip_end)).strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_cip_dt = ""
        carryover = int(min(carryover, carry_cap))

        # Use the later of last production end or last CIP end.
        # A CIP may extend past the last production block (e.g. CIP at
        # end of Week-0); Phase 2 must not schedule production that
        # overlaps with that CIP.
        if set_available_from_schedule:
            # A line with no week-0 production is either idle or still busy
            # with its running MO. If the original schedule gated it (running
            # MO extends past hour 168), carry that gate into Phase 2 so the
            # week-1 model does not place work over the still-running MO.
            avail_from = max(last_end_hour, last_cip_end)
            if not prods and orig_gate > avail_from:
                avail_from = orig_gate
        else:
            avail_from = 0

        out_rows.append({
            "line_id": l,
            "line_name": line_name,
            "initial_sku": initial_sku,
            "available_from_hour": avail_from,
            "long_shutdown_flag": 0,
            "long_shutdown_extra_setup_hours": 0,
            "carryover_run_hours_since_last_cip_at_t0": carryover,
            "last_cip_end_datetime": last_cip_dt,
            "comment": ("Auto from week-0 run (wall-clock carry at t0; negative"
                        " = clean after t0)"),
        })

    df = pd.DataFrame(out_rows)
    df.to_csv(data_dir / "week1_initial_states.csv", index=False)


def _reconcile_row_qty_kg(
    schedule_rows: List[Dict[str, Any]],
    produced_by_order: Dict[str, float],
) -> None:
    """Make each order's qty_kg rows sum to the solver's produced value.

    Rows arrive carrying rate x run_hours (see _solution_to_rows). That is
    already the model's own decomposition of produced[o_idx], so normally
    nothing moves; this pass is the guard for the two edge cases:
      * the rate was unreachable (all rows 0 kg) -> split produced by run_hours
      * rounding drift -> scale the rows, then push the remainder onto the
        last row so the order total matches produced exactly.
    Mutates schedule_rows in place. Output only - no solver behaviour.
    """
    if not schedule_rows or not produced_by_order:
        return
    by_order: Dict[str, List[Dict[str, Any]]] = {}
    for row in schedule_rows:
        by_order.setdefault(str(row.get("order_id", "")), []).append(row)
    for oid, rows in by_order.items():
        total = produced_by_order.get(oid)
        if total is None:
            continue
        total = float(total)
        raw = sum(float(r.get("qty_kg", 0) or 0) for r in rows)
        if raw <= 0:
            run_total = sum(float(r.get("run_hours", 0) or 0) for r in rows)
            if run_total <= 0:
                continue
            for r in rows:
                r["qty_kg"] = round(total * float(r.get("run_hours", 0) or 0) / run_total, 1)
        elif abs(raw - total) > 0.5:
            for r in rows:
                r["qty_kg"] = round(total * float(r["qty_kg"]) / raw, 1)
        drift = round(total - sum(float(r["qty_kg"]) for r in rows), 1)
        if drift:
            rows[-1]["qty_kg"] = round(float(rows[-1]["qty_kg"]) + drift, 1)


def _solution_to_rows(
    solver: cp_model.CpSolver,
    data: Data,
    P: Params,
    vars_dict: Dict[str, Any],
    hour_offset: int = 0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build schedule_rows and bounds_rows from solver.

    hour_offset is added to all hours (for Week-1 merge).
    Orders split by a CIP produce two schedule rows (same order_id/sku)
    for seg_a and seg_b respectively.
    """
    orders = data.orders
    lines = data.lines
    present = vars_dict["present"]
    seg_a_start = vars_dict["seg_a_start"]
    seg_a_end = vars_dict["seg_a_end"]
    seg_a_run = vars_dict["seg_a_run"]
    seg_b_present = vars_dict["seg_b_present"]
    seg_b_start = vars_dict["seg_b_start"]
    seg_b_end = vars_dict["seg_b_end"]
    seg_b_run = vars_dict["seg_b_run"]
    produced = vars_dict["produced"]

    try:
        anchor = datetime.strptime(P.planning_start_date, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        anchor = datetime(2026, 2, 15, 0, 0, 0)

    schedule_rows = []
    for l in lines:
        for o_idx, o in enumerate(orders):
            key = (l, o_idx)
            if not solver.BooleanValue(present[key]):
                continue
            line_name = data.line_names.get(l, f"L{l}")

            is_trial = bool(o.get("is_trial", False))
            sku_desc = data.sku_desc.get(o["sku"], "")
            # Produced kg per row. The model defines produced[o_idx] as
            # sum over lines of int(round(rate(l, sku))) * run_h(l, o), and
            # run_h = seg_a_run + seg_b_run, so rate * row run_hours is the
            # exact per-row share of the solver's produced value. A missing
            # rate leaves 0 here and the reconcile pass below splits the
            # solver total by run_hours instead. Output only - no constraint
            # or objective change.
            row_rate = int(round(float(data.rate.get((l, o["sku"])) or 0)))

            # seg_a (always present when order is assigned)
            sa_s = solver.Value(seg_a_start[key]) + hour_offset
            sa_e = solver.Value(seg_a_end[key]) + hour_offset
            sa_r = solver.Value(seg_a_run[key])
            if sa_r > 0:
                sa_start_dt = anchor + timedelta(hours=sa_s)
                sa_end_dt = anchor + timedelta(hours=sa_e)
                schedule_rows.append({
                    "line_id": l,
                    "line_name": line_name,
                    "order_id": o["order_id"],
                    "sku": o["sku"],
                    "sku_description": sku_desc,
                    "start_hour": sa_s,
                    "end_hour": sa_e,
                    "run_hours": sa_r,
                    "qty_kg": row_rate * sa_r,
                    "start_dt": sa_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_dt": sa_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "is_trial": is_trial,
                })

            # seg_b (only when split by CIP — same SKU continues)
            if solver.BooleanValue(seg_b_present[key]):
                sb_s = solver.Value(seg_b_start[key]) + hour_offset
                sb_e = solver.Value(seg_b_end[key]) + hour_offset
                sb_r = solver.Value(seg_b_run[key])
                if sb_r > 0:
                    sb_start_dt = anchor + timedelta(hours=sb_s)
                    sb_end_dt = anchor + timedelta(hours=sb_e)
                    schedule_rows.append({
                        "line_id": l,
                        "line_name": line_name,
                        "order_id": o["order_id"],
                        "sku": o["sku"],
                        "sku_description": sku_desc,
                        "start_hour": sb_s,
                        "end_hour": sb_e,
                        "run_hours": sb_r,
                        "qty_kg": row_rate * sb_r,
                        "start_dt": sb_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_dt": sb_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "is_trial": is_trial,
                    })

    bounds_rows = []
    produced_by_order: Dict[str, float] = {}
    for o_idx, o in enumerate(orders):
        prod_val = solver.Value(produced[o_idx])
        produced_by_order[str(o["order_id"])] = float(prod_val)
        qmin, qmax = int(o["qty_min"]), int(o["qty_max"])
        in_bounds = qmin <= prod_val <= qmax
        bounds_rows.append({
            "order_id": o["order_id"],
            "sku": o["sku"],
            "qty_min": qmin,
            "qty_max": qmax,
            "produced": prod_val,
            "in_bounds": in_bounds,
        })
    _reconcile_row_qty_kg(schedule_rows, produced_by_order)
    return schedule_rows, bounds_rows


def write_solution(
    solver: cp_model.CpSolver,
    data: Data,
    P: Params,
    data_dir: Path,
    vars_dict: Dict[str, Any],
    hour_offset: int = 0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Write schedule_phase2.csv and produced_vs_bounds.csv when solve is FEASIBLE/OPTIMAL.

    Returns (schedule_rows, bounds_rows) for feasibility reporting.
    """
    schedule_rows, bounds_rows = _solution_to_rows(
        solver, data, P, vars_dict, hour_offset
    )
    if schedule_rows:
        pd.DataFrame(schedule_rows).to_csv(data_dir / "schedule_phase2.csv", index=False)
        # Prefer model-extracted CIPs; fall back to post-solve placement
        cip_vars = vars_dict.get("cip_vars")
        if cip_vars:
            cip_rows = extract_cip_windows(solver, data, cip_vars, hour_offset)
        else:
            cip_rows = compute_cip_windows(schedule_rows, data, P)
        if cip_rows:
            pd.DataFrame(cip_rows).to_csv(data_dir / "cip_windows.csv", index=False)
        write_week1_initial_states(schedule_rows, cip_rows or [], data, P, data_dir)
        # Idle-gap KPIs
        idle_kpi_lines = compute_idle_kpis(schedule_rows, cip_rows or [], data_dir)
        for ln in idle_kpi_lines:
            log(ln)
    pd.DataFrame(bounds_rows).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
    write_mo_changes(data_dir, data, schedule_rows, bounds_rows, P=P)
    return schedule_rows, bounds_rows


def write_mo_changes(
    data_dir: Path,
    data: Data,
    schedule_rows: List[Dict[str, Any]],
    bounds_rows: List[Dict[str, Any]],
    P: Params | None = None,
) -> None:
    """Write mo_changes.csv - the VIF write-back record for committed MOs.

    Only current-state MOs (is_current_mo orders) are compared. Columns (the
    first twelve are the historical record, see MO_CHANGES_COLUMNS):
      orig_qty_kg   remaining kg the plant committed (qty_remaining, else qty_min)
      new_qty_kg    the solver's planned produced kg
      orig_start_h  the MO's manprg start when current_mo.csv carries
                    ``manprg_start_h`` (orig_start_src = "manprg"), else the
                    solver bound due_start (orig_start_src = "due_start",
                    a Flowstate projection - writeback-6)
      piece         1..split_count: ONE ROW PER PRODUCED BLOCK, each with its
                    own new_start_h/new_end_h (writeback-7: a split MO is no
                    longer one window spanning the CIP gap)
      reason        tonnage_trim / tonnage_fill / split / reordered / unmoved,
                    or ``dropped`` when the solver placed NO block for the MO
                    (writeback-5: new_start_h/new_end_h are then EMPTY, never 0)
      new_start_dt / new_end_dt / planning_anchor  wall-clock equivalents; the
                    anchor is the datetime hour 0 refers to, so the hour
                    columns are usable in VIF (P.planning_start_date)
      scenario_id   the work-dir name (the runner names it after the scenario)
      generated_at  when this record was written
    """
    from collections import defaultdict

    anchor: datetime | None = None
    if P is not None:
        try:
            anchor = datetime.strptime(str(P.planning_start_date), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            anchor = None
    anchor_s = anchor.strftime("%Y-%m-%d %H:%M:%S") if anchor else ""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scenario_id = Path(data_dir).name

    def _dt(h: Any) -> str:
        if h is None or h == "" or anchor is None:
            return ""
        return (anchor + timedelta(hours=float(h))).strftime("%Y-%m-%d %H:%M:%S")

    # produced per order id (mo|CUR) from bounds_rows
    produced_by_order: dict[str, int] = {}
    for b in bounds_rows:
        if str(b.get("order_id", "")).endswith("|CUR"):
            produced_by_order[str(b["order_id"])] = int(b.get("produced", 0))

    # schedule blocks per order id
    blocks_by_order: dict[str, list[dict]] = defaultdict(list)
    for row in schedule_rows:
        oid = str(row.get("order_id", ""))
        if oid.endswith("|CUR"):
            blocks_by_order[oid].append(row)

    rows: list[dict] = []
    for o in data.orders:
        if not o.get("is_current_mo"):
            continue
        oid = o["order_id"]
        orig_kg = int(o.get("qty_remaining") or o["qty_min"])
        new_kg = produced_by_order.get(oid, 0)
        blocks = sorted(
            blocks_by_order.get(oid, []), key=lambda r: r["start_hour"])
        manprg_start = o.get("manprg_start_h")
        if manprg_start is not None and str(manprg_start) != "":
            orig_start_h: Any = int(manprg_start)
            orig_src = "manprg"
        else:
            orig_start_h = int(o["due_start"])
            orig_src = "due_start"
        base = {
            "mo": str(o.get("mo_id", "")),
            "line_name": data.line_names.get(o.get("locked_line"), ""),
            "sku": o["sku"],
            "source": o.get("source", "manprg"),
            "orig_qty_kg": orig_kg,
            "new_qty_kg": new_kg,
            "delta_kg": new_kg - orig_kg,
            "orig_start_h": orig_start_h,
            "orig_start_src": orig_src,
            "planning_anchor": anchor_s,
            "scenario_id": scenario_id,
            "generated_at": generated_at,
        }
        if not blocks:
            # writeback-5: a committed MO the solver did not place at all.
            # Hours are EMPTY (not 0 - hour 0 is a real position) and the
            # reason is the single token the UI/export can refuse on.
            rows.append({**base, "new_start_h": "", "new_end_h": "",
                         "split_count": 0, "reason": "dropped", "piece": "",
                         "new_start_dt": "", "new_end_dt": ""})
            continue
        reasons: list[str] = []
        if new_kg < orig_kg:
            reasons.append("tonnage_trim")
        elif new_kg > orig_kg:
            reasons.append("tonnage_fill")
        if len(blocks) > 1:
            reasons.append("split")
        if int(blocks[0]["start_hour"]) != int(orig_start_h):
            reasons.append("reordered")
        if not reasons:
            reasons.append("unmoved")
        for k, b in enumerate(blocks, start=1):
            s_h, e_h = int(b["start_hour"]), int(b["end_hour"])
            rows.append({**base, "new_start_h": s_h, "new_end_h": e_h,
                         "split_count": len(blocks), "reason": "+".join(reasons),
                         "piece": k, "new_start_dt": _dt(s_h), "new_end_dt": _dt(e_h)})
    pd.DataFrame(rows, columns=MO_CHANGES_COLUMNS).to_csv(
        data_dir / "mo_changes.csv", index=False)


def _run_two_phase(P: Params, F: Files, data_dir: Path) -> None:
    """Two-phase solve:
    Phase 1: Week-0 orders (168h horizon).
    Phase 2: Week-1 orders on FULL 336h horizon, with line availability from Week-0 end.
    Week-1 orders can start as soon as each line finishes Week-0 (not waiting until hour 168).
    CIPs extracted from solver (explicit intervals in model).
    """
    tl = float(TIME_LIMIT) if TIME_LIMIT is not None else 120.0

    # ── Stage: Loading Data ──
    update_stage(data_dir, "loading_data", "active")
    # C84 / config-5: dataclasses.replace keeps EVERY Params field (the
    # hand-written constructor dropped use_sku_rates, soft_demand,
    # objective_shortfall_weight, over_target_reward_pct, solver_random_seed).
    P0 = _sub_phase_params(P, horizon_h=168, min_run_override=MIN_RUN_HOURS_OVERRIDE)
    log(f"[two-phase] sub-phase params: use_sku_rates={P0.use_sku_rates} "
        f"soft_demand={P0.soft_demand} seed={P0.solver_random_seed} "
        f"min_run_hours={P0.min_run_hours} (all other fields inherited from P)")
    n_levels_tp = len(_ladder_levels(_base_relax_level(), 3 if AUTO_RELAX else _base_relax_level()))
    log(f"[budget] two-phase: {tl:.0f}s per week solve, {SEARCH_WORKERS} workers; "
        f"typical wall ~{2 * tl:.0f}s (W0 + W1), ceiling ~{2 * n_levels_tp * tl:.0f}s "
        f"if both relax ladders escalate through {n_levels_tp} level(s)")
    data0 = Data(P0, F)
    data0.load()
    # Week-0 split: demand orders due in week 0, PLUS current-state MOs
    # (running/queued — they are committed work regardless of their nominal
    # due window; the solver places what fits in week 0 and the remainder
    # carries into week 1 via the MO's presence in both phases).
    # The week boundary is the SECOND distinct demand due_start (fix SB-1
    # frame; INTEGRATE): a first-week order is one whose due_start lies
    # before it, whatever weekday the horizon is anchored on. Trials keep the
    # old end-based test against the same boundary.
    _wk_boundary = _two_phase_boundary(data0.orders)
    log(f"[two-phase] week boundary at hour {_wk_boundary} (second distinct demand due_start)")
    orders_week0 = [
        o for o in data0.orders
        if o.get("is_current_mo")
        or (o.get("is_trial", False) and int(o["due_end"]) < _wk_boundary)
        or (not o.get("is_trial", False) and int(o["due_start"]) < _wk_boundary)
    ]
    data0.orders = orders_week0

    n_skus = len({o["sku"] for o in data0.orders})
    total_demand = sum(o.get("qty_min", 0) for o in data0.orders)
    update_stage(
        data_dir, "loading_data", "done",
        f"{len(data0.lines)} lines, {len(orders_week0)} W0 orders, {n_skus} SKUs",
    )
    set_data_summary(
        data_dir,
        lines=len(data0.lines),
        orders=len(orders_week0),
        skus=n_skus,
        total_demand_kg=total_demand,
        changeover_pairs=len(data0.setup),
        horizon_h=P.horizon_h,
    )

    if not orders_week0:
        log("[two-phase] No Week-0 orders")
        write_kpi_lines(["Status: TWO_PHASE — no Week-0 orders"])
        write_feasibility_report(data_dir, {
            "relax_level": _base_relax_level(),
            "relax_mode": RELAX_LABELS[_base_relax_level()],
            "status": "NO_WEEK0_ORDERS",
            "solver_status": "N/A",
        })
        return

    # ── Stage: Building Model (Week 0) ──
    update_stage(data_dir, "building_model_w0", "active")
    log(f"[two-phase] Phase 1: Week-0 only ({len(orders_week0)} orders), horizon=168")
    base_lvl = _base_relax_level()
    max_lvl = 3 if AUTO_RELAX else base_lvl
    level0 = base_lvl
    solver0 = None
    status0 = None
    vars0 = None
    mo_state0 = "n/a"
    # orchestration-12: the SAME trust gates as the single-phase path -
    # hints from a schedule solved on different inputs, or at a MORE relaxed
    # level than this attempt, starve the search instead of seeding it.
    _prev_relax_tp, _prev_sig_tp = _prev_solve_trust(data_dir)
    _inputs_changed_tp = _prev_sig_tp != input_signature(Path(data_dir))
    for lvl in _ladder_levels(base_lvl, max_lvl):
        flags = RELAX_LADDER[lvl]
        if lvl > base_lvl:
            log(f"[auto-relax] Week-0 escalating to level {lvl} ({RELAX_LABELS[lvl]})")
            update_stage(data_dir, "building_model_w0", "active", f"relax level {lvl}")
            update_stage(data_dir, "solving_week0", "pending")
        model0, vars0 = build_model(
            P0, data0, PHASE, flags["relax_demand"], flags["ignore_co"],
            max_lines_per_order_override=MAX_LINES_PER_ORDER,
            objective_mode=OBJECTIVE_MODE,
            relax_due=flags["relax_due"],
            cross_week=CROSS_WEEK,
            cip_flex=CIP_FLEX,
        )
        _log_model_warnings(vars0, " (Week-0)")
        mo_state0, _mo_n0 = _keep_committed_mo_bounds(model0, vars0, data0, lvl)
        if mo_state0 != "n/a":
            log(f"[auto-relax] Week-0 level {lvl}: committed MO bounds {mo_state0} "
                f"for {_mo_n0} MO(s)")
        proto0 = model0.Proto()
        update_stage(
            data_dir, "building_model_w0", "done",
            f"{len(proto0.variables):,} vars, {len(proto0.constraints):,} constraints",
        )

        # ── Warm start (item 30) ──
        # Seed the Week-0 search with the previous run's schedule. Week-0 is
        # the daily re-run Carsten re-solves every morning, so it benefits
        # most from yesterday's Week-0 plan. The previous schedule lives in
        # prev_schedule.csv (the full-horizon combined schedule from the last
        # run). build_hint_plan range-checks every prev row against this
        # model's 168h horizon and DROPS week 1-2 rows (absolute hours 168+),
        # and rebuilds order_index from the CURRENT Week-0 order list so every
        # (line, order) key aligns with vars0["present"]. A hint only steers
        # the search (CP-SAT repairs it), so it is safe at every relax level.
        # ALWAYS logs, including the no-op / failure paths (pitfall 15).
        if _ARGS.no_warm_start:
            log("[warm-start] disabled by --no-warm-start")
        elif _inputs_changed_tp:
            log("[warm-start] W0 skipped: solve inputs changed since the "
                "previous schedule (input_sig mismatch) - stale hints steer "
                "into the new constraints")
        elif _prev_relax_tp is not None and _prev_relax_tp > lvl:
            log(f"[warm-start] W0 skipped: previous schedule is relax level "
                f"{_prev_relax_tp}, attempting level {lvl} - hints from a "
                "more-relaxed plan starve the search")
        else:
            try:
                from warm_start import apply_warm_start

                for _wsn in apply_warm_start(
                    model0, vars0, data0, P0.horizon_h, data_dir
                ):
                    log(_wsn)
            except Exception as _wsexc:  # noqa: BLE001
                log(f"[warm-start] W0 FAILED, solving cold: {_wsexc}")

        # ── Stage: Solving Week 0 ──
        update_stage(
            data_dir, "solving_week0", "active",
            f"{int(tl)}s limit, {SEARCH_WORKERS} workers (level {lvl}: {RELAX_LABELS[lvl]})",
        )
        reset_solver_stats(data_dir, status="STARTING", time_limit_s=tl,
                           direction="min")
        solver0 = cp_model.CpSolver()
        solver0.parameters.num_search_workers = SEARCH_WORKERS
        solver0.parameters.max_time_in_seconds = tl
        apply_solver_seed(solver0, P)
        cb0 = _ProgressCallback(data_dir, label_prefix=f"W0 L{lvl}: ",
                                direction="min")
        status0 = solver0.Solve(model0, cb0)
        if status0 in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            level0 = lvl
            break
        log(f"[auto-relax] Week-0 {solver0.StatusName(status0)} at level {lvl}")
    if status0 not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        update_stage(data_dir, "solving_week0", "error", solver0.StatusName(status0))
        log(f"[two-phase] Week-0 failed: {solver0.StatusName(status0)}")
        _handle_infeasible(
            P0, data0, data_dir,
            level=level0, solver_status=solver0.StatusName(status0),
            week_label="Week-0", two_phase=True,
        )
    update_stage(data_dir, "solving_week0", "done", solver0.StatusName(status0))

    schedule_rows_0, bounds_0 = _solution_to_rows(
        solver0, data0, P0, vars0, 0
    )
    # Extract CIP positions from Week-0 solver
    cip_rows_0 = extract_cip_windows(solver0, data0, vars0.get("cip_vars", {}), 0)

    # Write intermediate InitialStates: available_from = last Week-0 end per line
    write_week1_initial_states(
        schedule_rows_0, cip_rows_0, data0, P0, data_dir,
        set_available_from_schedule=True,  # line availability from Week-0 end
    )

    # Phase 2: Week-1 on the FULL horizon (lines available from Week-0 end).
    # Horizon comes from P (flowstate.toml [scheduler] horizon_hours) rather
    # than a hard-coded 336: the app moved to a 3-week (504h) rolling horizon
    # and demand_plan.csv now carries week-2 orders with due windows out to
    # hour 503. With H pinned at 336 their max run length computes to zero
    # (model_builder line 77) and a third of the demand is silently
    # unschedulable.
    F_week1 = Files(data_dir)
    F_week1.init = str(data_dir / "week1_initial_states.csv")
    P1 = _sub_phase_params(P, horizon_h=P.horizon_h, min_run_override=MIN_RUN_HOURS_OVERRIDE)
    data1 = Data(P1, F_week1)
    data1.load()
    orders_week1 = []
    _wk_boundary1 = _two_phase_boundary(data1.orders)
    for o in data1.orders:
        is_trial = o.get("is_trial", False)
        if is_trial:
            tsh = o.get("trial_start_hour", 0)
            teh = o.get("trial_end_hour", 0)
            if tsh < _wk_boundary1 and teh > _wk_boundary1:
                # Boundary-spanning trial: must go to Phase 2 (full 336h)
                log(
                    f"[two-phase] WARNING: trial {o['order_id']} spans "
                    f"Week-0/1 boundary (h{tsh}-h{teh}). "
                    f"Consider single-phase mode for best results."
                )
                orders_week1.append(o)
            elif tsh >= _wk_boundary1:
                orders_week1.append(o)
            # else: trial fits entirely in Week-0 (handled by Phase 1)
        elif int(o["due_start"]) >= _wk_boundary1:
            orders_week1.append(o)
    # Allow Week-1 orders to start as soon as line is available (due_start=0)
    # but do NOT override due_start for trial orders (timing is fixed)
    for o in orders_week1:
        if not o.get("is_trial", False):
            o["due_start"] = 0   # line availability constraint handles actual start
            # due_end stays at 335 (end of Week-1)
    data1.orders = orders_week1

    if not orders_week1:
        pd.DataFrame(schedule_rows_0).to_csv(data_dir / "schedule_phase2.csv", index=False)
        if cip_rows_0:
            pd.DataFrame(cip_rows_0).to_csv(data_dir / "cip_windows.csv", index=False)
        pd.DataFrame(bounds_0).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
        write_mo_changes(data_dir, data0, schedule_rows_0, bounds_0, P=P0)
        _fields0 = _level_report_fields(level0, vars0)
        _kpi0 = ["Status: TWO_PHASE — Week-0 FEASIBLE, no Week-1 orders"]
        if not _fields0["setup_times_enforced"]:
            _kpi0.append("UNSAFE: changeover times not enforced (relax level 3)")
            log("UNSAFE: changeover times not enforced (relax level 3 / ignore_co)")
        write_kpi_lines(_kpi0)
        _rep0: Dict[str, Any] = {
            "relax_level": level0,
            "relax_mode": RELAX_LABELS[level0],
            "status": solver0.StatusName(status0),
            "solver_status": solver0.StatusName(status0),
            "week": "Week-0 (no Week-1 orders)",
            "orders_short_of_qmin": _orders_short_of_qmin(bounds_0),
            "late_orders": _late_orders(solver0, data0, vars0),
            "committed_mo_bounds": mo_state0,
            "deterministic": DETERMINISTIC,
            "search_workers": SEARCH_WORKERS,
            "model_warnings": _model_warnings(vars0),
        }
        _rep0.update(_fields0)
        _rep0["validation"] = _run_independent_validation(data_dir, label=" (two-phase, W0 only)")
        write_feasibility_report(data_dir, _rep0)
        return

    # ── Stage: Building Model (Week 1) ──
    update_stage(data_dir, "building_model_w1", "active")
    log(f"[two-phase] Phase 2: Week-1 ({len(orders_week1)} orders), horizon=336 (full), maximize production")
    level1 = base_lvl
    solver1 = None
    status1 = None
    vars1 = None
    mo_state1 = "n/a"
    for lvl in _ladder_levels(base_lvl, max_lvl):
        flags = RELAX_LADDER[lvl]
        if lvl > base_lvl:
            log(f"[auto-relax] Week-1 escalating to level {lvl} ({RELAX_LABELS[lvl]})")
            update_stage(data_dir, "building_model_w1", "active", f"relax level {lvl}")
            update_stage(data_dir, "solving_week1", "pending")
        model1, vars1 = build_model(
            P1, data1, PHASE, flags["relax_demand"], flags["ignore_co"],
            max_lines_per_order_override=MAX_LINES_PER_ORDER,
            maximize_production=True,
            objective_mode=OBJECTIVE_MODE,
            relax_due=flags["relax_due"],
            cross_week=CROSS_WEEK,
            cip_flex=CIP_FLEX,
        )
        _log_model_warnings(vars1, " (Week-1)")
        mo_state1, _mo_n1 = _keep_committed_mo_bounds(model1, vars1, data1, lvl)
        if mo_state1 != "n/a":
            log(f"[auto-relax] Week-1 level {lvl}: committed MO bounds {mo_state1} "
                f"for {_mo_n1} MO(s)")
        proto1 = model1.Proto()
        update_stage(
            data_dir, "building_model_w1", "done",
            f"{len(proto1.variables):,} vars, {len(proto1.constraints):,} constraints",
        )

        # ── Stage: Solving Week 1 ──
        update_stage(
            data_dir, "solving_week1", "active",
            f"{int(tl)}s limit, {SEARCH_WORKERS} workers (level {lvl}: {RELAX_LABELS[lvl]})",
        )
        reset_solver_stats(data_dir, status="STARTING", time_limit_s=tl,
                           direction="min")
        solver1 = cp_model.CpSolver()
        solver1.parameters.num_search_workers = SEARCH_WORKERS
        solver1.parameters.max_time_in_seconds = tl
        apply_solver_seed(solver1, P)
        cb1 = _ProgressCallback(data_dir, label_prefix=f"W1 L{lvl}: ",
                                direction="min")
        status1 = solver1.Solve(model1, cb1)
        if status1 in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            level1 = lvl
            break
        log(f"[auto-relax] Week-1 {solver1.StatusName(status1)} at level {lvl}")

    if status1 not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        update_stage(data_dir, "solving_week1", "error", solver1.StatusName(status1))
        pd.DataFrame(schedule_rows_0).to_csv(data_dir / "schedule_phase2.csv", index=False)
        if cip_rows_0:
            pd.DataFrame(cip_rows_0).to_csv(data_dir / "cip_windows.csv", index=False)
        pd.DataFrame(bounds_0).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
        log(f"[two-phase] Week-1 failed: {solver1.StatusName(status1)}")
        _handle_infeasible(
            P1, data1, data_dir,
            level=level1, solver_status=solver1.StatusName(status1),
            week_label="Week-1 (Week-0 feasible)", two_phase=True,
            extra={
                "week0": {
                    "relax_level": level0,
                    "relax_mode": RELAX_LABELS[level0],
                    "status": solver0.StatusName(status0),
                },
                "orders_short_of_qmin": _orders_short_of_qmin(bounds_0),
                "late_orders": _late_orders(solver0, data0, vars0),
            },
        )
    update_stage(data_dir, "solving_week1", "done", solver1.StatusName(status1))

    # No hour offset: Phase 2 uses absolute hours (0-335)
    schedule_rows_1, bounds_1 = _solution_to_rows(
        solver1, data1, P1, vars1, 0
    )
    # Extract CIPs from Phase 2 solver (covers CIPs for Week-1 portion)
    cip_rows_1 = extract_cip_windows(solver1, data1, vars1.get("cip_vars", {}), 0)

    # ── Stage: Writing Output ──
    update_stage(data_dir, "writing_output", "active")

    combined_schedule = schedule_rows_0 + schedule_rows_1
    combined_bounds = bounds_0 + bounds_1
    combined_cips = cip_rows_0 + cip_rows_1

    pd.DataFrame(combined_schedule).to_csv(data_dir / "schedule_phase2.csv", index=False)
    pd.DataFrame(combined_bounds).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
    if combined_cips:
        pd.DataFrame(combined_cips).to_csv(data_dir / "cip_windows.csv", index=False)
    # MO change table (current-state MOs vs planned) for VIF write-back.
    # (orchestration-10, not fixed here: data1 holds the Week-1 orders, so
    # committed MOs - which live in the Week-0 model - are not listed.)
    write_mo_changes(data_dir, data1, combined_schedule, combined_bounds, P=P1)

    # Final InitialStates for rolling (available_from=0 for next week)
    write_week1_initial_states(
        combined_schedule, combined_cips, data1, P1, data_dir,
        set_available_from_schedule=False,
    )
    update_stage(data_dir, "writing_output", "done", "Schedule and KPIs saved")

    # Idle-gap KPIs on combined schedule
    idle_kpi_lines = compute_idle_kpis(combined_schedule, combined_cips, data_dir)
    final_level = max(level0, level1)
    _fields_tp = _level_report_fields(final_level, vars1)
    _kpi_tp = ["Status: TWO_PHASE — Week-0 and Week-1 FEASIBLE",
               f"Relax level: {final_level} ({RELAX_LABELS[final_level]})"]
    if not _fields_tp["setup_times_enforced"]:
        _kpi_tp.append("UNSAFE: changeover times not enforced (relax level 3)")
        log("UNSAFE: changeover times not enforced (relax level 3 / ignore_co) - "
            "this plan may put two SKUs back-to-back with no setup time")
    write_kpi_lines(_kpi_tp + idle_kpi_lines)
    write_feasibility_report(data_dir, {
        **_fields_tp,
        "committed_mo_bounds": {"week0": mo_state0, "week1": mo_state1},
        "deterministic": DETERMINISTIC,
        "search_workers": SEARCH_WORKERS,
        "model_warnings": _model_warnings(vars0) + _model_warnings(vars1),
        "validation": _run_independent_validation(data_dir, label=" (two-phase)"),
        "relax_level": final_level,
        "relax_mode": RELAX_LABELS[final_level],
        "status": "FEASIBLE",
        "solver_status": f"Week-0 {solver0.StatusName(status0)}, Week-1 {solver1.StatusName(status1)}",
        "week0": {
            "relax_level": level0,
            "relax_mode": RELAX_LABELS[level0],
            "status": solver0.StatusName(status0),
        },
        "week1": {
            "relax_level": level1,
            "relax_mode": RELAX_LABELS[level1],
            "status": solver1.StatusName(status1),
        },
        "orders_short_of_qmin": _orders_short_of_qmin(combined_bounds),
        "late_orders": (
            _late_orders(solver0, data0, vars0) + _late_orders(solver1, data1, vars1)
        ),
    })
    log("[two-phase] Done: combined schedule written")
    for ln in idle_kpi_lines:
        log(ln)


def main() -> None:
    # orchestration-11: the run log is reset FIRST. Every banner below
    # ([soft-demand], [two-pass], [rolling], ...) used to be written and then
    # wiped by a later reset_err(), so the journal never showed the mode.
    reset_err()
    # Apply config overrides from flowstate.toml + CLI flags (CLI wins).
    # The full toml -> Params mapping lives in params_from_config so the
    # override plumbing is testable without a subprocess.
    P = params_from_config(
        _CFG,
        max_lines_override=MAX_LINES_PER_ORDER,
        min_run_override=MIN_RUN_HOURS_OVERRIDE,
        allow_week1=not NO_WEEK1_IN_WEEK0,
    )
    F = Files(DATA_DIR)
    # Rolling mode: auto-load week1_initial_states.csv if it exists
    if ROLLING:
        w1_init = DATA_DIR / "week1_initial_states.csv"
        if w1_init.exists():
            F.init = str(w1_init)
            log(f"[rolling] Using {w1_init} as InitialStates")
    if INITIAL_STATES_PATH is not None:
        p = Path(INITIAL_STATES_PATH)
        F.init = str(p.resolve() if p.is_absolute() else (DATA_DIR / p))

    # Soft demand (Scenario F): resolved inside params_from_config.
    global _SOFT_DEMAND_ACTIVE
    _SOFT_DEMAND_ACTIVE = P.soft_demand
    if P.soft_demand:
        log(f"[soft-demand] every kg short of qty_min costs "
            f"{P.objective_shortfall_weight} in the objective (Scenario F)")
        if P.over_target_reward_pct > 0:
            log(f"[soft-demand] over-target kg pay "
                f"{P.over_target_reward_pct}% of the base per-kg reward "
                f"(toward qty_max; fulfillment stays capped at target)")
    # Two-pass CO minimization (Scenario F): pass 1 maximizes fill, pass 2
    # re-solves with that fill score as a hard floor (minus epsilon) and
    # MINIMIZES the weighted changeover load. Measured 2026-08-14: price
    # pressure alone couldn't cut topload count at equal tonnage — a
    # dedicated minimization pass is the lever.
    TWO_PASS_CO = bool(_CFG_SCHED.get("two_pass_co", False))
    TWO_PASS_EPS = float(_CFG_SCHED.get("two_pass_epsilon_pct", 1.0))
    # Per-order pass-2 floors (fix SA-3, model_builder order_floors): opt-in,
    # default OFF — see the INTEGRATE decision note at the pass-2 build.
    PASS2_ORDER_FLOORS = bool(_CFG_SCHED.get("pass2_order_floors", False))
    # Separate pass-2 budget (overnight batch): scheduler.time_limit_pass2
    # in the work toml. Unset/0 keeps the historical behaviour — pass 2
    # reuses pass 1's time limit.
    TWO_PASS_TL = float(_CFG_SCHED.get("time_limit_pass2", 0) or 0)
    if TWO_PASS_CO and P.soft_demand:
        log(f"[two-pass] enabled: pass 2 minimizes changeovers holding fill "
            f">= pass 1 - {TWO_PASS_EPS}%"
            + (f" (pass 2 budget {TWO_PASS_TL:.0f}s)" if TWO_PASS_TL else ""))
    log(
        f"START {datetime.now():%Y-%m-%d} phase={PHASE} relax={RELAX_DEMAND} relax_due={RELAX_DUE} "
        f"ignoreCO={IGNORE_CHANGEOVERS} auto_relax={AUTO_RELAX} "
        f"cross_week={CROSS_WEEK} cip_flex={CIP_FLEX} "
        f"tl={TIME_LIMIT} mlpo={P.max_lines_per_order}"
    )
    if P.solver_random_seed is not None:
        log(f"[seed] CP-SAT random_seed {P.solver_random_seed} "
            "(applied to every solve pass)")
    if DETERMINISTIC:
        log("[seed] --deterministic: every solve runs with num_search_workers=1 "
            f"and random_seed {P.solver_random_seed if P.solver_random_seed is not None else 0}; "
            "the default 8-worker portfolio is NOT reproducible under a "
            "wall-clock limit even with a fixed seed (C80)")
    # Time-limit honesty (fix V-9): the per-solve budget is NOT the wall time.
    if not TWO_PHASE:
        _tl_b = float(TIME_LIMIT) if TIME_LIMIT is not None else 120.0
        _nl_b = len(_ladder_levels(_base_relax_level(), 3 if AUTO_RELAX else _base_relax_level()))
        _msg = (f"[budget] single-phase: {_tl_b:.0f}s per solve, {SEARCH_WORKERS} workers; "
                f"ceiling ~{_nl_b * _tl_b:.0f}s if the relax ladder runs through "
                f"{_nl_b} level(s)")
        if TWO_PASS_CO and P.soft_demand:
            _tl2_b = TWO_PASS_TL or _tl_b
            _msg += (f"; two-pass adds anchor <= {min(120.0, _tl2_b):.0f}s + pass 2 "
                     f"{_tl2_b:.0f}s -> typical wall ~{_tl_b + min(120.0, _tl2_b) + _tl2_b:.0f}s")
        log(_msg)

    # Initialise structured progress
    if _CROSS_WEEK_FORCED_SINGLE:
        log(
            "[cross-week] --two-phase overridden: solving single-phase over "
            "the full horizon so orders can move between AZAP weeks"
        )
    if TWO_PHASE:
        init_progress(DATA_DIR, STAGES_TWO_PHASE)
    else:
        init_progress(DATA_DIR, STAGES_SINGLE)

    try:
        if TWO_PHASE:
            _run_two_phase(P, F, DATA_DIR)
        else:
            # ── Stage: Loading Data ──
            update_stage(DATA_DIR, "loading_data", "active")
            data = Data(P, F)
            data.load()
            n_skus = len({o["sku"] for o in data.orders})
            total_demand = sum(o.get("qty_min", 0) for o in data.orders)
            update_stage(
                DATA_DIR, "loading_data", "done",
                f"{len(data.lines)} lines, {len(data.orders)} orders, {n_skus} SKUs",
            )
            set_data_summary(
                DATA_DIR,
                lines=len(data.lines),
                orders=len(data.orders),
                skus=n_skus,
                total_demand_kg=total_demand,
                changeover_pairs=len(data.setup),
                horizon_h=P.horizon_h,
            )
            log(f"[data] {len(data.lines)} lines, {len(data.orders)} orders, {n_skus} SKUs")

            if DIAGNOSE:
                run_diagnostics(P, data, DATA_DIR)
                run_unique_line_load_diagnostic(P, data, DATA_DIR)
                run_blockages_diagnostic(P, data, DATA_DIR, two_phase=TWO_PHASE)
                write_kpi_lines([
                    "Status: DIAG COMPLETE (see diag_order_linecap.csv, diag_unique_line_load.csv, diag_blockages.csv / diag_blockages.txt)"
                ])
            else:
                # ── Stage: Building Model ──
                update_stage(DATA_DIR, "building_model", "active")
                tl = float(TIME_LIMIT) if TIME_LIMIT is not None else 120.0
                base_lvl = _base_relax_level()
                max_lvl = 3 if AUTO_RELAX else base_lvl
                level = base_lvl
                solver = None
                status = None
                vars_dict = None
                _mo_state = "n/a"
                # Warm-start hints are only useful when the previous schedule
                # honoured AT LEAST this attempt's constraints. Hinting a
                # changeover-ENFORCING level with a changeover-IGNORING
                # (level-3) schedule starves the search instead of seeding it
                # — measured 2026-08-14: level 2 cold found FEASIBLE in 300s,
                # level 2 warm-hinted from a level-3 run timed out UNKNOWN and
                # the ladder silently escalated back to ignore_co.
                _prev_relax: int | None = None
                _prev_sig: str | None = None
                try:
                    import json as _json
                    # prev_feasibility.json is the copy the scenario runner
                    # carries across its work-dir wipe; a direct CLI rerun in
                    # the same dir still has its own feasibility_report.json.
                    for _name in ("prev_feasibility.json",
                                  "feasibility_report.json"):
                        _fr = Path(DATA_DIR) / _name
                        if _fr.exists():
                            _prev = _json.loads(_fr.read_text(encoding="utf-8"))
                            _prev_relax = _prev.get("relax_level")
                            _prev_sig = _prev.get("input_sig")
                            break
                except Exception:  # noqa: BLE001
                    _prev_relax = None
                # Hints from a schedule solved on DIFFERENT inputs steer into
                # walls (see input_signature docstring). Unknown previous sig
                # (older report) counts as changed — cold is the safe default.
                _cur_sig = input_signature(Path(DATA_DIR))
                _inputs_changed = _prev_sig != _cur_sig
                for lvl in _ladder_levels(base_lvl, max_lvl):
                    flags = RELAX_LADDER[lvl]
                    if lvl > base_lvl:
                        log(f"[auto-relax] escalating to level {lvl} ({RELAX_LABELS[lvl]})")
                        update_stage(DATA_DIR, "building_model", "active", f"relax level {lvl}")
                        update_stage(DATA_DIR, "solving", "pending")
                    model, vars_dict = build_model(
                        P,
                        data,
                        PHASE,
                        flags["relax_demand"],
                        flags["ignore_co"],
                        max_lines_per_order_override=MAX_LINES_PER_ORDER,
                        # Single-phase (forced by cross-week) must also push
                        # production, else a relaxed demand floor lets the
                        # changeover/makespan objective abandon orders.
                        maximize_production=True,
                        objective_mode=OBJECTIVE_MODE,
                        relax_due=flags["relax_due"],
                        cross_week=CROSS_WEEK,
                        cip_flex=CIP_FLEX,
                    )
                    # C85 / orchestration-9: relax levels 1-2 relax DEMAND
                    # orders only; committed MOs keep their tonnage bounds.
                    _mo_state, _mo_n = _keep_committed_mo_bounds(
                        model, vars_dict, data, lvl)
                    if _mo_state == "kept":
                        log(f"[auto-relax] level {lvl}: committed MO bounds kept "
                            f"for {_mo_n} MO(s) (relax_demand applies to demand "
                            "orders only)")
                    elif _mo_state == "released":
                        log(f"[auto-relax] level {lvl}: committed MO bounds "
                            f"RELEASED for {_mo_n} MO(s) - last resort; "
                            "mo_changes.csv records every trim")
                    proto = model.Proto()
                    n_vars = len(proto.variables)
                    n_cons = len(proto.constraints)
                    update_stage(
                        DATA_DIR, "building_model", "done",
                        f"{n_vars:,} variables, {n_cons:,} constraints",
                    )
                    log(f"[model] level {lvl} ({RELAX_LABELS[lvl]}): {n_vars} vars, {n_cons} constraints")

                    # ── Warm start (item 10) ──
                    # Seed the search with the previous run's schedule. A hint
                    # cannot change the feasible set (CP-SAT repairs it), so it
                    # is safe at every relax level; it only gives the search a
                    # foothold on the single-phase model, which is the hard
                    # case (pitfall 9). ALWAYS logs, including the no-op paths.
                    if _ARGS.no_warm_start:
                        log("[warm-start] disabled by --no-warm-start")
                    elif _inputs_changed:
                        log("[warm-start] skipped: solve inputs changed since "
                            "the previous schedule (input_sig mismatch) — "
                            "stale hints steer into the new constraints")
                    elif _prev_relax is not None and _prev_relax > lvl:
                        log(f"[warm-start] skipped: previous schedule is relax "
                            f"level {_prev_relax}, attempting level {lvl} — "
                            "hints from a more-relaxed plan starve the search")
                    else:
                        try:
                            from warm_start import apply_warm_start

                            for _wsn in apply_warm_start(
                                model, vars_dict, data, P.horizon_h, DATA_DIR
                            ):
                                log(_wsn)
                        except Exception as _wsexc:  # noqa: BLE001
                            log(f"[warm-start] FAILED, solving cold: {_wsexc}")

                    # ── Stage: Solving ──
                    update_stage(
                        DATA_DIR, "solving", "active",
                        f"{int(tl)}s time limit, {SEARCH_WORKERS} workers (level {lvl}: {RELAX_LABELS[lvl]})",
                    )
                    # Single-phase always maximizes production (see
                    # build_model call above); a soft-demand run additionally
                    # watches REAL placed kg so solution labels talk demand,
                    # not raw objective units.
                    _cb_kwargs: Dict[str, Any] = {"direction": "max"}
                    if _SOFT_DEMAND_ACTIVE:
                        _dem_idx = [i for i, o in enumerate(data.orders)
                                    if not o.get("is_current_mo")]
                        if _dem_idx:
                            _cb_kwargs.update(
                                pass_id="fill",
                                pass_name=("pass 1" if TWO_PASS_CO and lvl == 0
                                           else "fill"),
                                placed_expr=sum(vars_dict["produced"][i]
                                                for i in _dem_idx),
                                demand_kg=sum(
                                    int(data.orders[i].get("qty_min") or 0)
                                    for i in _dem_idx),
                            )
                    reset_solver_stats(
                        DATA_DIR, status="STARTING", time_limit_s=tl,
                        direction="max",
                        pass_id=_cb_kwargs.get("pass_id", ""))

                    solver = cp_model.CpSolver()
                    solver.parameters.num_search_workers = SEARCH_WORKERS
                    solver.parameters.max_time_in_seconds = tl
                    apply_solver_seed(solver, P)
                    # Fill labels carry their own pass wording; the ladder
                    # level only matters once it escalates past hard rules.
                    _lvl_prefix = ("" if _cb_kwargs.get("pass_id") and lvl == 0
                                   else f"L{lvl}: ")
                    cb = _ProgressCallback(
                        DATA_DIR, label_prefix=_lvl_prefix, **_cb_kwargs)
                    status = solver.Solve(model, cb)
                    status_name = solver.StatusName(status)
                    log(f"SOLVER level={lvl} status={status_name}")
                    update_solver_stats(
                        DATA_DIR,
                        status=status_name,
                        elapsed_s=round(solver.WallTime(), 1),
                    )
                    if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                        level = lvl
                        break

                status_name = solver.StatusName(status)

                # ── Two-pass CO minimization (Scenario F, level 0 only) ──
                # Level 0 = changeovers enforced; at level 3 (ignore_co) a
                # CO-minimizing pass is meaningless. Pass 2 gets its own full
                # time budget (the runner's subprocess ceiling is 4x tl).
                #
                # Fix V (2026-09-03):
                #  * C80  pass 1's outputs are written BEFORE pass 2 starts, so
                #         a pass-2 process abort (CP-SAT CHECK failure seen on
                #         bench B7_free) can never lose the schedule; the
                #         anchor solve runs 1 worker with the model's search
                #         strategy cleared (every var is fixed, an empty
                #         strategy is what tripped 'heuristics.fixed_search !=
                #         nullptr'); pass 2 runs without repair_hint (the
                #         complete hint is adopted as the incumbent anyway)
                #         and without the 'fixed' subsolver.
                #  * C83  pass 2 is adopted only when its co_load <= pass 1's
                #         and its validator error count is not worse.
                #  * SA-3 per-order floors min(pass-1 kg, target) + fill
                #         exchange rate K go into the pass-2 build; the
                #         min_prod_score floor stays as the secondary floor.
                _outputs_done = False
                _two_pass_extra: Dict[str, Any] | None = None
                _pass1_report: Dict[str, Any] | None = None
                if (status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
                        and level == 0 and _SOFT_DEMAND_ACTIVE and TWO_PASS_CO
                        and vars_dict.get("prod_score") is not None):
                    try:
                        _tl2 = TWO_PASS_TL or tl
                        _score1 = solver.Value(vars_dict["prod_score"])
                        _floor = int(_score1 * (1.0 - TWO_PASS_EPS / 100.0))
                        _co1 = _co_load_value(solver, vars_dict)
                        log(f"[two-pass] pass 1 fill score {_score1:,} -> "
                            f"pass 2 floor {_floor:,} ({TWO_PASS_EPS}% give), "
                            f"pass 1 co_load {_co1}, objective = weighted "
                            "changeover load")
                        log(f"[budget] pass 2: anchor <= {min(120.0, _tl2):.0f}s "
                            f"({ANCHOR_WORKERS} worker) + solve {_tl2:.0f}s "
                            f"({SEARCH_WORKERS} workers)")
                        # (a) C80: the schedule is safe on disk before pass 2.
                        update_stage(DATA_DIR, "solving", "active",
                                     "pass 1 done: saving its schedule before pass 2")
                        _pass1_report = _write_single_phase_outputs(
                            solver, data, P, DATA_DIR, vars_dict,
                            level=level, status_name=status_name,
                            extra={"two_pass": {"adopted": "pass 1",
                                                "decision": "pass 2 pending",
                                                "co_load_pass1": _co1},
                                   "committed_mo_bounds": _mo_state},
                            announce_stage=False)
                        _outputs_done = True
                        _two_pass_extra = {"two_pass": _pass1_report["two_pass"]}
                        log("[two-pass] pass 1 outputs written (schedule safe "
                            "before pass 2 starts)")
                        # PHYSICAL errors only (INTEGRATE): the pass-2
                        # candidate is validated in memory against the
                        # pass-1 produced_vs_bounds.csv on disk, so its
                        # DEMAND_SUM rows are an artefact, never a veto.
                        _val1 = _pass1_report.get("validation") or {}
                        _err1 = _val1.get("physical_errors")
                        _nerr1 = _val1.get("n_errors")
                        # Per-order floors (fix 4 / SA-3): pass 2 may
                        # resequence but never delete an order or shave its
                        # tail below min(pass-1 kg, target).
                        # INTEGRATE decision (bench B8/B8_eps2 vs SA E3b,
                        # 2026-09-03): the per-order floors are OFF by
                        # default. With the fill exchange rate K in pass 2's
                        # objective (fix SA-3) a kg of fill is already priced
                        # against a changeover (one full change ~ 3.5 t), so
                        # trimming a tail for cosmetics can never pay (E2);
                        # the floors additionally forbade DROPPING any order
                        # pass 1 made, which is exactly the trade the plant
                        # asked pass 2 to make (bench B8: an 8 h pair of
                        # format changes for a 4 t stub is a losing trade;
                        # audit F4's own recommendation). Opt in with
                        # [scheduler] pass2_order_floors = true to protect
                        # every pass-1 order's kg unconditionally.
                        _floors: Dict[int, int] = {}
                        if PASS2_ORDER_FLOORS:
                            for _i, _o in enumerate(data.orders):
                                if _o.get("is_current_mo") or _o.get("is_trial"):
                                    continue
                                _made = int(solver.Value(vars_dict["produced"][_i]))
                                _tgt = int(_o.get("qty_target") or 0)
                                _fl = min(_made, _tgt) if _tgt > 0 else 0
                                if _fl > 0:
                                    _floors[_i] = _fl
                        _K = int(default_fill_exchange_rate(P))
                        log(f"[two-pass] {len(_floors)} per-order floors "
                            + ("(min(pass-1 kg, target))" if PASS2_ORDER_FLOORS
                               else "(disabled: [scheduler] pass2_order_floors = false)")
                            + f" + fill exchange rate K={_K} (one full changeover ~ "
                            f"{1080 * 100 * _K / 1010 / 1000:.1f} t of fill); "
                            "prod_score floor kept as secondary")
                        m2, v2 = build_model(
                            P, data, PHASE,
                            flags["relax_demand"], flags["ignore_co"],
                            max_lines_per_order_override=MAX_LINES_PER_ORDER,
                            maximize_production=True,
                            objective_mode=OBJECTIVE_MODE,
                            relax_due=flags["relax_due"],
                            cross_week=CROSS_WEEK, cip_flex=CIP_FLEX,
                            min_prod_score=_floor,
                            order_floors=_floors,
                            fill_exchange_rate=_K,
                        )
                        _log_model_warnings(v2, " (pass 2)")
                        _hinted = 0
                        for _grp in ("present", "seg_b_present", "run_h",
                                     "seg_a_start", "seg_a_run", "seg_a_end",
                                     "seg_b_start", "seg_b_run", "seg_b_end",
                                     "eff_end"):
                            for _key, _var in vars_dict[_grp].items():
                                m2.AddHint(v2[_grp][_key],
                                           solver.Value(_var))
                                _hinted += 1
                        log(f"[two-pass] hinted {_hinted} vars from pass 1")
                        update_stage(
                            DATA_DIR, "solving", "active",
                            "pass 2 anchor: locking pass 1's plan in as the "
                            "starting point")
                        reset_solver_stats(
                            DATA_DIR, status="ANCHORING", time_limit_s=_tl2,
                            direction="min", pass_id="co")
                        # The floor excludes ~99% of the feasible space, so
                        # pass 2 lives or dies on starting FROM pass 1's
                        # solution. A partial hint is not enough: CP-SAT's
                        # hint repair abandoned completing the ~60k derived
                        # vars and went UNKNOWN in 600s (run 20), yet pinning
                        # the hinted vars solves OPTIMAL in seconds. So:
                        # anchor-solve with the hinted vars FIXED to
                        # materialize a complete solution, then install that
                        # full assignment as the hint for the real solve —
                        # complete hints are adopted as the incumbent.
                        _proto2 = m2.Proto()
                        # C80: with every strategy variable fixed the search
                        # strategy presolves to EMPTY and CP-SAT aborts the
                        # process (CHECK heuristics.fixed_search != nullptr).
                        # The anchor is a fixed-assignment check: no strategy,
                        # one worker. The strategy is restored for pass 2.
                        _strategy_backup = [copy.deepcopy(_s) for _s in _proto2.search_strategy]
                        _proto2.search_strategy.clear()
                        _s2a = cp_model.CpSolver()
                        _s2a.parameters.num_search_workers = ANCHOR_WORKERS
                        _s2a.parameters.max_time_in_seconds = min(120.0, _tl2)
                        apply_solver_seed(_s2a, P)
                        _s2a.parameters.fix_variables_to_their_hinted_value = True
                        _sta = _s2a.Solve(m2)
                        _anchor_ok = _sta in (cp_model.FEASIBLE, cp_model.OPTIMAL)
                        if _anchor_ok:
                            _resp = _s2a.ResponseProto()
                            # ortools 9.15's proto wrapper has no ClearField;
                            # ClearHints() empties the hint, then the proto's
                            # repeated fields accept the full assignment.
                            m2.ClearHints()
                            _proto2.solution_hint.vars.extend(
                                range(len(_resp.solution)))
                            _proto2.solution_hint.values.extend(_resp.solution)
                            log(f"[two-pass] anchor {_s2a.StatusName(_sta)}: "
                                f"complete {len(_resp.solution):,}-var hint "
                                "installed (pass 1's plan as incumbent)")
                        else:
                            log(f"[two-pass] anchor {_s2a.StatusName(_sta)} — "
                                "falling back to the partial hint")
                        _proto2.search_strategy.extend(_strategy_backup)
                        update_stage(
                            DATA_DIR, "solving", "active",
                            f"pass 2: min changeovers, fill floored at "
                            f"{100 - TWO_PASS_EPS:g}% ({int(_tl2)}s)")
                        reset_solver_stats(
                            DATA_DIR, status="STARTING", time_limit_s=_tl2,
                            direction="min", pass_id="co")
                        _s2 = cp_model.CpSolver()
                        _s2.parameters.num_search_workers = SEARCH_WORKERS
                        _s2.parameters.max_time_in_seconds = _tl2
                        apply_solver_seed(_s2, P)
                        # C80: repair_hint=True was the crashing configuration
                        # (bench B7_free: 2/12 aborts; 0/24 without it). A
                        # complete hint is adopted as the incumbent without
                        # repair; a partial one was measured useless with it
                        # (run 20). The 'fixed' subsolver only replays the
                        # fill strategy, which the incumbent already embodies.
                        _s2.parameters.repair_hint = False
                        _s2.parameters.ignore_subsolvers.append("fixed")
                        # Watch the true weighted changeover load so pass-2
                        # labels report it directly instead of the composite
                        # objective (an int co_load means no CO term exists).
                        _co_expr = v2.get("co_load")
                        _cb2 = _ProgressCallback(
                            DATA_DIR, direction="min", pass_id="co",
                            co_expr=(None if isinstance(_co_expr, int)
                                     else _co_expr))
                        _st2 = _s2.Solve(m2, _cb2)
                        # Pass 1's plan measured on pass 2's objective (the
                        # anchor solve fixes every variable to the hint).
                        _obj_anchor = float(_s2a.ObjectiveValue()) if _anchor_ok else None
                        _tp_rec: Dict[str, Any] = {
                            "co_load_pass1": _co1, "anchor": _s2a.StatusName(_sta),
                            "pass2_status": _s2.StatusName(_st2),
                            "validator_errors_pass1": _nerr1,
                            "validator_physical_pass1": _err1,
                            "objective_pass1_plan": _obj_anchor,
                            "floors": len(_floors), "fill_exchange_rate": _K,
                        }
                        if _st2 in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                            _co2 = _co_load_value(_s2, v2)
                            _fill2 = int(_s2.Value(v2["prod_score"]))
                            # Validate the pass-2 CANDIDATE in memory before
                            # it may replace the pass-1 files on disk.
                            _rows2, _b2 = _solution_to_rows(_s2, data, P, v2)
                            _cips2 = extract_cip_windows(
                                _s2, data, v2.get("cip_vars", {}) or {})
                            if _rows2:
                                _val2 = _run_independent_validation(
                                    DATA_DIR, schedule=pd.DataFrame(_rows2),
                                    cips=(pd.DataFrame(_cips2) if _cips2 else None),
                                    write_txt=False, label=" (pass 2 candidate)")
                                _err2 = _val2.get("physical_errors")
                                _nerr2 = _val2.get("n_errors")
                            else:
                                _err2 = _nerr2 = None
                            _obj2 = float(_s2.ObjectiveValue())
                            _adopt, _why = _adopt_pass2(_co1, _co2, _err1, _err2,
                                                        obj_anchor=_obj_anchor, obj2=_obj2)
                            _tp_rec.update({
                                "co_load_pass2": _co2, "fill_score_pass2": _fill2,
                                "fill_floor": _floor, "validator_errors_pass2": _nerr2,
                                "validator_physical_pass2": _err2,
                                "objective_pass2": _obj2,
                                "adopted": "pass 2" if _adopt else "pass 1",
                                "decision": _why,
                            })
                            log(f"[two-pass] pass 2 {_s2.StatusName(_st2)}: "
                                f"fill score {_fill2:,} (floor {_floor:,}), "
                                f"co_load {_co2} vs pass 1 {_co1} - "
                                f"{'ADOPTING pass 2' if _adopt else 'KEEPING pass 1'} "
                                f"({_why})")
                            if _adopt:
                                solver, vars_dict = _s2, v2
                                status_name = _s2.StatusName(_st2)
                                update_solver_stats(
                                    DATA_DIR, status=status_name,
                                    elapsed_s=round(_s2.WallTime(), 1))
                                _outputs_done = False
                        else:
                            _tp_rec.update({"adopted": "pass 1",
                                            "decision": f"pass 2 {_s2.StatusName(_st2)}"})
                            log(f"[two-pass] pass 2 {_s2.StatusName(_st2)} — "
                                "keeping pass 1 unchanged")
                        _two_pass_extra = {"two_pass": _tp_rec}
                        if _outputs_done and _pass1_report is not None:
                            # pass 1 stays: stamp the decision into its report
                            _pass1_report["two_pass"] = _tp_rec
                            write_feasibility_report(DATA_DIR, _pass1_report)
                    except Exception as _tp_exc:  # noqa: BLE001
                        log(f"[two-pass] FAILED, keeping pass 1: {_tp_exc}")
                        if _outputs_done and _pass1_report is not None:
                            _pass1_report["two_pass"] = {
                                "adopted": "pass 1",
                                "decision": f"pass 2 failed: {type(_tp_exc).__name__}: {_tp_exc}"}
                            write_feasibility_report(DATA_DIR, _pass1_report)

                if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                    update_stage(DATA_DIR, "solving", "done", status_name)
                    if not _outputs_done:
                        _extra_out: Dict[str, Any] = {"committed_mo_bounds": _mo_state}
                        if _two_pass_extra:
                            _extra_out.update(_two_pass_extra)
                        _write_single_phase_outputs(
                            solver, data, P, DATA_DIR, vars_dict,
                            level=level, status_name=status_name,
                            extra=_extra_out)
                    else:
                        update_stage(DATA_DIR, "writing_output", "done",
                                     "Schedule and KPIs saved (pass 1 kept)")
                else:
                    update_stage(DATA_DIR, "solving", "error", status_name)
                    _handle_infeasible(
                        P, data, DATA_DIR,
                        level=level, solver_status=status_name,
                        week_label="single-phase", two_phase=False,
                    )
    except Exception as exc:
        log("\n=== FATAL ERROR ===\n" + traceback.format_exc())
        write_kpi_lines([f"Status: ERROR — {type(exc).__name__}: see solver_error.txt"])

    # Post-solve validation
    if VALIDATE and not DIAGNOSE:
        update_stage(DATA_DIR, "validating", "active")
        log("[validate] running post-solve validation")
        try:
            validate_all(DATA_DIR, verbose=True, cfg=_CFG)
            update_stage(DATA_DIR, "validating", "done", "Validation complete")
        except (OSError, ValueError, KeyError) as exc:
            log(f"[validate] {type(exc).__name__}: {exc}\n" + traceback.format_exc())
            update_stage(DATA_DIR, "validating", "error", "Validation failed")

    # Record solver run timestamp
    if (DATA_DIR / "schedule_phase2.csv").exists():
        write_schedule_meta()


if __name__ == "__main__":
    main()
