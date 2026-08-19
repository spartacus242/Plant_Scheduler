# phase2_scheduler.py — Flowstate Phase 2 factory scheduler (CLI + orchestration).
# Uses aggregate CIP accounting (v3). Phases: sanity1, sanity3, full.

from __future__ import annotations
import argparse
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

from data_loader import Params, Data, Files, available_hours_line
from diagnostics import run_diagnostics, run_unique_line_load_diagnostic, run_blockages_diagnostic
from model_builder import build_model
from validate_schedule import validate_all
from solver_progress import (
    init_progress,
    update_stage,
    set_data_summary,
    add_solution,
    update_solver_stats,
    STAGES_SINGLE,
    STAGES_TWO_PHASE,
)


class _ProgressCallback(cp_model.CpSolverSolutionCallback):
    """Reports each intermediate solution to solver_progress.json so the
    UI can display real-time objective improvements."""

    def __init__(self, data_dir: Path, label_prefix: str = ""):
        super().__init__()
        self._data_dir = data_dir
        self._prefix = label_prefix
        self._count = 0
        self._first_obj: float | None = None

    def on_solution_callback(self) -> None:
        self._count += 1
        obj = self.ObjectiveValue()
        wall = self.WallTime()
        bound = self.BestObjectiveBound()
        if self._first_obj is None:
            self._first_obj = obj
            label = f"{self._prefix}First feasible solution"
        else:
            pct = (obj - self._first_obj) / max(1, abs(self._first_obj)) * 100
            label = f"{self._prefix}Solution #{self._count} ({pct:+.1f}%)"
        add_solution(self._data_dir, wall, obj, label)
        gap = round(100 * abs(obj - bound) / max(1, abs(obj)), 1) if obj != 0 else 0
        update_solver_stats(
            self._data_dir,
            status="SOLVING",
            best_objective=round(obj, 1),
            best_bound=round(bound, 1),
            gap_pct=gap,
            elapsed_s=round(wall, 1),
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
            "CIP timing flexibility: scale down the cip_defer reward so a CIP "
            "can be pulled EARLIER to absorb a changeover. The line's max "
            "allowable CIP interval stays a HARD constraint (food safety)."
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
    return parser.parse_args()


_ARGS = _parse_args() if __name__ == "__main__" else argparse.Namespace(
    data_dir=None, phase="full", time_limit=None, relax_demand=False,
    relax_due=False, auto_relax=True, no_auto_relax=False,
    ignore_changeovers=False, diagnose=False, max_lines_per_order=None,
    min_run_hours=None, no_week1_in_week0=False, initial_states=None,
    two_phase=False,
    objective="balanced", validate=False, rolling=False, cross_week=False,
    cip_flex=False, no_warm_start=False, config=None,
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
    # Soft demand (Scenario F)
    P.soft_demand = bool(sched.get("soft_demand", False))
    if sched.get("shortfall_weight") is not None:
        P.objective_shortfall_weight = int(sched["shortfall_weight"])
    return P


DATA_DIR = _ARGS.data_dir.resolve() if _ARGS.data_dir is not None else BASE_DIR
_CFG = _load_config(_ARGS.config, DATA_DIR)
_CFG_SCHED = _CFG.get("scheduler", {})

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
    try:
        with open(ERR_FILE, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
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
    """Per-order total lateness (hours past due window) when relax_due was on."""
    lateness = vars_dict.get("lateness") or {}
    if not lateness:
        return []
    totals: Dict[int, int] = {}
    for (_l, o_idx), var in lateness.items():
        v = int(solver.Value(var))
        if v > 0:
            totals[o_idx] = totals.get(o_idx, 0) + v
    return [
        {
            "order_id": data.orders[o_idx]["order_id"],
            "sku": data.orders[o_idx]["sku"],
            "lateness_h": v,
        }
        for o_idx, v in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


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
    for (_l, o_idx), (early_v, late_v) in week_dev.items():
        e = int(solver.Value(early_v))
        lt = int(solver.Value(late_v))
        if e <= 0 and lt <= 0:
            continue
        rec = out.setdefault(o_idx, {
            "order_id": data.orders[o_idx]["order_id"],
            "sku": data.orders[o_idx]["sku"],
            "azap_week": 0 if int(data.orders[o_idx]["due_end"]) <= WEEK0_END else 1,
            "hours_earlier": 0,
            "hours_later": 0,
        })
        rec["hours_earlier"] += e
        rec["hours_later"] += lt
    return sorted(
        out.values(),
        key=lambda r: -(r["hours_earlier"] + r["hours_later"]),
    )


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

        if not prods:
            initial_sku = str(data.init_map.get(l, {}).get("initial_sku", "CLEAN"))
            carryover = orig_carry
            last_cip_dt = ""
            last_end_hour = 0
        else:
            last_run = max(prods, key=lambda x: x["end_hour"])
            initial_sku = str(last_run["sku"])
            carryover = sum(
                p["run_hours"] for p in prods
                if p["start_hour"] >= last_cip_end
            )
            last_end_hour = max(p["end_hour"] for p in prods)
            if last_cip_end > 0 and l in cip_by_line:
                last_cip_dt = (anchor + timedelta(hours=last_cip_end)).strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_cip_dt = ""

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
            "carryover_run_hours_since_last_cip_at_t0": int(min(carryover, 119)),
            "last_cip_end_datetime": last_cip_dt,
            "comment": "Auto from week-0 run",
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
    write_mo_changes(data_dir, data, schedule_rows, bounds_rows)
    return schedule_rows, bounds_rows


def write_mo_changes(
    data_dir: Path,
    data: Data,
    schedule_rows: List[Dict[str, Any]],
    bounds_rows: List[Dict[str, Any]],
) -> None:
    """Write mo_changes.csv comparing planned current-MO production vs manprg.

    Only current-state MOs (is_current_mo orders) are compared. For each MO
    we report the original remaining kg (qty_min), the solver's planned
    produced kg, the planned window (earliest start / latest end across its
    schedule blocks), the number of split blocks, and a reason:
      tonnage_trim   produced < orig
      tonnage_fill   produced > orig   (should not happen with qty_max=orig)
      split          more than one production block
      reordered      planned start moved from the manprg start_h
      unmoved        produced == orig and single block
    The file is the VIF write-back record (CSV download in the UI).
    """
    from collections import defaultdict

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
        orig_kg = int(o["qty_min"])
        new_kg = produced_by_order.get(oid, 0)
        blocks = sorted(
            blocks_by_order.get(oid, []), key=lambda r: r["start_hour"])
        if blocks:
            first_start = min(b["start_hour"] for b in blocks)
            last_end = max(b["end_hour"] for b in blocks)
            start_h = int(first_start)
            end_h = int(last_end)
            split_count = len(blocks)
        else:
            start_h, end_h, split_count = 0, 0, 0

        reasons: list[str] = []
        if new_kg < orig_kg:
            reasons.append("tonnage_trim")
        elif new_kg > orig_kg:
            reasons.append("tonnage_fill")
        if split_count > 1:
            reasons.append("split")
        if blocks and int(o["due_start"]) != start_h:
            reasons.append("reordered")
        if not reasons:
            reasons.append("unmoved")
        rows.append({
            "mo": str(o.get("mo_id", "")),
            "line_name": data.line_names.get(o.get("locked_line"), ""),
            "sku": o["sku"],
            "source": o.get("source", "manprg"),
            "orig_qty_kg": orig_kg,
            "new_qty_kg": new_kg,
            "delta_kg": new_kg - orig_kg,
            "orig_start_h": int(o["due_start"]),
            "new_start_h": start_h,
            "new_end_h": end_h,
            "split_count": split_count,
            "reason": "+".join(reasons),
        })
    if rows:
        pd.DataFrame(rows).to_csv(data_dir / "mo_changes.csv", index=False)
    else:
        # still write an empty file so callers can check existence/header
        pd.DataFrame(
            columns=["mo", "line_name", "sku", "source", "orig_qty_kg",
                     "new_qty_kg", "delta_kg", "orig_start_h", "new_start_h",
                     "new_end_h", "split_count", "reason"]
        ).to_csv(data_dir / "mo_changes.csv", index=False)


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
    P0 = Params(
        horizon_h=168,
        changeover_penalty=P.changeover_penalty,
        cip_interval_h=P.cip_interval_h,
        cip_duration_h=P.cip_duration_h,
        max_lines_per_order=P.max_lines_per_order,
        stale_threshold_days=P.stale_threshold_days,
        stale_setup_extra_h=P.stale_setup_extra_h,
        long_shutdown_default_h=P.long_shutdown_default_h,
        planning_start_date=P.planning_start_date,
        min_run_hours=MIN_RUN_HOURS_OVERRIDE if MIN_RUN_HOURS_OVERRIDE is not None else P.min_run_hours,
        min_run_pct_of_qty=P.min_run_pct_of_qty,
        allow_week1_in_week0=False,  # Week-0 only
        objective_makespan_weight=P.objective_makespan_weight,
        objective_changeover_weight=P.objective_changeover_weight,
        objective_cip_defer_weight=P.objective_cip_defer_weight,
        objective_idle_weight=P.objective_idle_weight,
        objective_late_weight=P.objective_late_weight,
        objective_week_deviation_weight=P.objective_week_deviation_weight,
        objective_cip_flex_weight=P.objective_cip_flex_weight,
        co_topload_weight=P.co_topload_weight,
        co_ttp_weight=P.co_ttp_weight,
        co_ffs_weight=P.co_ffs_weight,
        co_casepacker_weight=P.co_casepacker_weight,
        co_base_weight=P.co_base_weight,
        co_conv_org_weight=P.co_conv_org_weight,
        co_cinn_weight=P.co_cinn_weight,
        co_flavor_weight=P.co_flavor_weight,
    )
    data0 = Data(P0, F)
    data0.load()
    # Week-0 split: demand orders due in week 0, PLUS current-state MOs
    # (running/queued — they are committed work regardless of their nominal
    # due window; the solver places what fits in week 0 and the remainder
    # carries into week 1 via the MO's presence in both phases).
    orders_week0 = [
        o for o in data0.orders
        if int(o["due_end"]) <= WEEK0_END or o.get("is_current_mo")
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
        if not _ARGS.no_warm_start:
            try:
                from warm_start import apply_warm_start

                for _wsn in apply_warm_start(
                    model0, vars0, data0, P0.horizon_h, data_dir
                ):
                    log(_wsn)
            except Exception as _wsexc:  # noqa: BLE001
                log(f"[warm-start] W0 FAILED, solving cold: {_wsexc}")
        else:
            log("[warm-start] disabled by --no-warm-start")

        # ── Stage: Solving Week 0 ──
        update_stage(
            data_dir, "solving_week0", "active",
            f"{int(tl)}s limit, 8 workers (level {lvl}: {RELAX_LABELS[lvl]})",
        )
        update_solver_stats(data_dir, status="STARTING", time_limit_s=tl)
        solver0 = cp_model.CpSolver()
        solver0.parameters.num_search_workers = 8
        solver0.parameters.max_time_in_seconds = tl
        cb0 = _ProgressCallback(data_dir, label_prefix=f"W0 L{lvl}: ")
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
    P1 = Params(
        horizon_h=P.horizon_h,
        changeover_penalty=P.changeover_penalty,
        cip_interval_h=P.cip_interval_h,
        cip_duration_h=P.cip_duration_h,
        max_lines_per_order=P.max_lines_per_order,
        stale_threshold_days=P.stale_threshold_days,
        stale_setup_extra_h=P.stale_setup_extra_h,
        long_shutdown_default_h=P.long_shutdown_default_h,
        planning_start_date=P.planning_start_date,
        min_run_hours=MIN_RUN_HOURS_OVERRIDE if MIN_RUN_HOURS_OVERRIDE is not None else P.min_run_hours,
        min_run_pct_of_qty=P.min_run_pct_of_qty,
        allow_week1_in_week0=False,
        objective_makespan_weight=P.objective_makespan_weight,
        objective_changeover_weight=P.objective_changeover_weight,
        objective_cip_defer_weight=P.objective_cip_defer_weight,
        objective_idle_weight=P.objective_idle_weight,
        objective_late_weight=P.objective_late_weight,
        objective_week_deviation_weight=P.objective_week_deviation_weight,
        objective_cip_flex_weight=P.objective_cip_flex_weight,
        co_topload_weight=P.co_topload_weight,
        co_ttp_weight=P.co_ttp_weight,
        co_ffs_weight=P.co_ffs_weight,
        co_casepacker_weight=P.co_casepacker_weight,
        co_base_weight=P.co_base_weight,
        co_conv_org_weight=P.co_conv_org_weight,
        co_cinn_weight=P.co_cinn_weight,
        co_flavor_weight=P.co_flavor_weight,
    )
    data1 = Data(P1, F_week1)
    data1.load()
    orders_week1 = []
    for o in data1.orders:
        is_trial = o.get("is_trial", False)
        if is_trial:
            tsh = o.get("trial_start_hour", 0)
            teh = o.get("trial_end_hour", 0)
            if tsh < WEEK1_START and teh > WEEK0_END + 1:
                # Boundary-spanning trial: must go to Phase 2 (full 336h)
                log(
                    f"[two-phase] WARNING: trial {o['order_id']} spans "
                    f"Week-0/1 boundary (h{tsh}-h{teh}). "
                    f"Consider single-phase mode for best results."
                )
                orders_week1.append(o)
            elif tsh >= WEEK1_START:
                orders_week1.append(o)
            # else: trial fits entirely in Week-0 (handled by Phase 1)
        elif int(o["due_start"]) >= WEEK1_START:
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
        write_kpi_lines(["Status: TWO_PHASE — Week-0 FEASIBLE, no Week-1 orders"])
        write_feasibility_report(data_dir, {
            "relax_level": level0,
            "relax_mode": RELAX_LABELS[level0],
            "status": solver0.StatusName(status0),
            "solver_status": solver0.StatusName(status0),
            "week": "Week-0 (no Week-1 orders)",
            "orders_short_of_qmin": _orders_short_of_qmin(bounds_0),
            "late_orders": _late_orders(solver0, data0, vars0),
        })
        return

    # ── Stage: Building Model (Week 1) ──
    update_stage(data_dir, "building_model_w1", "active")
    log(f"[two-phase] Phase 2: Week-1 ({len(orders_week1)} orders), horizon=336 (full), maximize production")
    level1 = base_lvl
    solver1 = None
    status1 = None
    vars1 = None
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
        proto1 = model1.Proto()
        update_stage(
            data_dir, "building_model_w1", "done",
            f"{len(proto1.variables):,} vars, {len(proto1.constraints):,} constraints",
        )

        # ── Stage: Solving Week 1 ──
        update_stage(
            data_dir, "solving_week1", "active",
            f"{int(tl)}s limit, 8 workers (level {lvl}: {RELAX_LABELS[lvl]})",
        )
        solver1 = cp_model.CpSolver()
        solver1.parameters.num_search_workers = 8
        solver1.parameters.max_time_in_seconds = tl
        cb1 = _ProgressCallback(data_dir, label_prefix=f"W1 L{lvl}: ")
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
    # MO change table (current-state MOs vs planned) for VIF write-back
    write_mo_changes(data_dir, data1, combined_schedule, combined_bounds)

    # Final InitialStates for rolling (available_from=0 for next week)
    write_week1_initial_states(
        combined_schedule, combined_cips, data1, P1, data_dir,
        set_available_from_schedule=False,
    )
    update_stage(data_dir, "writing_output", "done", "Schedule and KPIs saved")

    # Idle-gap KPIs on combined schedule
    idle_kpi_lines = compute_idle_kpis(combined_schedule, combined_cips, data_dir)
    final_level = max(level0, level1)
    write_kpi_lines(
        ["Status: TWO_PHASE — Week-0 and Week-1 FEASIBLE",
         f"Relax level: {final_level} ({RELAX_LABELS[final_level]})"]
        + idle_kpi_lines
    )
    write_feasibility_report(data_dir, {
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
    if TWO_PASS_CO and P.soft_demand:
        log(f"[two-pass] enabled: pass 2 minimizes changeovers holding fill "
            f">= pass 1 - {TWO_PASS_EPS}%")
    reset_err()
    log(
        f"[{datetime.now()}] START phase={PHASE} relax={RELAX_DEMAND} relax_due={RELAX_DUE} "
        f"ignoreCO={IGNORE_CHANGEOVERS} auto_relax={AUTO_RELAX} "
        f"cross_week={CROSS_WEEK} cip_flex={CIP_FLEX} "
        f"tl={TIME_LIMIT} mlpo={P.max_lines_per_order}"
    )

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
                        f"{int(tl)}s time limit, 8 workers (level {lvl}: {RELAX_LABELS[lvl]})",
                    )
                    update_solver_stats(DATA_DIR, status="STARTING", time_limit_s=tl)

                    solver = cp_model.CpSolver()
                    solver.parameters.num_search_workers = 8
                    solver.parameters.max_time_in_seconds = tl
                    cb = _ProgressCallback(DATA_DIR, label_prefix=f"L{lvl}: ")
                    status = solver.Solve(model, cb)
                    status_name = solver.StatusName(status)
                    log(f"[{datetime.now()}] SOLVER level={lvl} status={status_name}")
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
                if (status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
                        and level == 0 and _SOFT_DEMAND_ACTIVE and TWO_PASS_CO
                        and vars_dict.get("prod_score") is not None):
                    try:
                        _score1 = solver.Value(vars_dict["prod_score"])
                        _floor = int(_score1 * (1.0 - TWO_PASS_EPS / 100.0))
                        log(f"[two-pass] pass 1 fill score {_score1:,} -> "
                            f"pass 2 floor {_floor:,} ({TWO_PASS_EPS}% give), "
                            "objective = weighted changeover load")
                        m2, v2 = build_model(
                            P, data, PHASE,
                            flags["relax_demand"], flags["ignore_co"],
                            max_lines_per_order_override=MAX_LINES_PER_ORDER,
                            maximize_production=True,
                            objective_mode=OBJECTIVE_MODE,
                            relax_due=flags["relax_due"],
                            cross_week=CROSS_WEEK, cip_flex=CIP_FLEX,
                            min_prod_score=_floor,
                        )
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
                            f"pass 2: min changeovers, fill floored "
                            f"({int(tl)}s)")
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
                        _s2a = cp_model.CpSolver()
                        _s2a.parameters.num_search_workers = 8
                        _s2a.parameters.max_time_in_seconds = min(120.0, tl)
                        _s2a.parameters.fix_variables_to_their_hinted_value = True
                        _sta = _s2a.Solve(m2)
                        if _sta in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                            _resp = _s2a.ResponseProto()
                            # ortools 9.15's proto wrapper has no ClearField;
                            # ClearHints() empties the hint, then the proto's
                            # repeated fields accept the full assignment.
                            m2.ClearHints()
                            _proto = m2.Proto()
                            _proto.solution_hint.vars.extend(
                                range(len(_resp.solution)))
                            _proto.solution_hint.values.extend(_resp.solution)
                            log(f"[two-pass] anchor {_s2a.StatusName(_sta)}: "
                                f"complete {len(_resp.solution):,}-var hint "
                                "installed (pass 1's plan as incumbent)")
                        else:
                            log(f"[two-pass] anchor {_s2a.StatusName(_sta)} — "
                                "falling back to the partial hint")
                        _s2 = cp_model.CpSolver()
                        _s2.parameters.num_search_workers = 8
                        _s2.parameters.max_time_in_seconds = tl
                        _s2.parameters.repair_hint = True
                        _cb2 = _ProgressCallback(DATA_DIR, label_prefix="P2co: ")
                        _st2 = _s2.Solve(m2, _cb2)
                        if _st2 in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                            log(f"[two-pass] pass 2 {_s2.StatusName(_st2)}: "
                                f"fill score {_s2.Value(v2['prod_score']):,} "
                                f"(floor {_floor:,}) — adopting pass 2")
                            solver, vars_dict = _s2, v2
                            status_name = _s2.StatusName(_st2)
                        else:
                            log(f"[two-pass] pass 2 {_s2.StatusName(_st2)} — "
                                "keeping pass 1 unchanged")
                    except Exception as _tp_exc:  # noqa: BLE001
                        log(f"[two-pass] FAILED, keeping pass 1: {_tp_exc}")

                if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                    update_stage(DATA_DIR, "solving", "done", status_name)

                    # ── Stage: Writing Output ──
                    update_stage(DATA_DIR, "writing_output", "active")
                    _sched_rows, bounds_rows = write_solution(
                        solver,
                        data,
                        P,
                        DATA_DIR,
                        vars_dict,
                    )
                    update_stage(DATA_DIR, "writing_output", "done", "Schedule and KPIs saved")

                    idle_kpi_path = DATA_DIR / "idle_kpis.csv"
                    idle_kpi_summary = []
                    if idle_kpi_path.exists():
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
                            idle_kpi_summary = [
                                f"Idle KPIs: {n} lines, total_idle={t_idle}h, median_idle={m_idle}h, utilization={util}%"
                            ]
                        except (OSError, KeyError, ValueError, TypeError):
                            pass
                    write_kpi_lines(
                        [f"Status: {status_name}",
                         f"Relax level: {level} ({RELAX_LABELS[level]})"]
                        + idle_kpi_summary
                    )
                    write_feasibility_report(DATA_DIR, {
                        "relax_level": level,
                        "relax_mode": RELAX_LABELS[level],
                        "status": status_name,
                        "solver_status": status_name,
                        "orders_short_of_qmin": _orders_short_of_qmin(bounds_rows),
                        "late_orders": _late_orders(solver, data, vars_dict),
                        "cross_week": CROSS_WEEK,
                        "cip_flex": CIP_FLEX,
                        "week_moved_orders": _week_moved_orders(
                            solver, data, vars_dict
                        ),
                    })
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
        log(f"[{datetime.now()}] Running post-solve validation")
        try:
            validate_all(DATA_DIR, verbose=True)
            update_stage(DATA_DIR, "validating", "done", "Validation complete")
        except (OSError, ValueError, KeyError) as exc:
            log(f"[validate] {type(exc).__name__}: {exc}\n" + traceback.format_exc())
            update_stage(DATA_DIR, "validating", "error", "Validation failed")

    # Record solver run timestamp
    if (DATA_DIR / "schedule_phase2.csv").exists():
        write_schedule_meta()


if __name__ == "__main__":
    main()
