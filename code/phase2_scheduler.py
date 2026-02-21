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
from helpers.solver_progress import (
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
        "--config",
        type=Path,
        default=None,
        help="Path to flowstate.toml config file for defaults.",
    )
    return parser.parse_args()


_ARGS = _parse_args()

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


DATA_DIR = _ARGS.data_dir.resolve() if _ARGS.data_dir is not None else BASE_DIR
_CFG = _load_config(_ARGS.config, DATA_DIR)
_CFG_SCHED = _CFG.get("scheduler", {})

ERR_FILE = DATA_DIR / "solver_error.txt"
KPI_FILE = DATA_DIR / "solver_kpis.txt"
TIME_LIMIT = _ARGS.time_limit or _CFG_SCHED.get("time_limit")
PHASE = _ARGS.phase
RELAX_DEMAND = _ARGS.relax_demand
IGNORE_CHANGEOVERS = _ARGS.ignore_changeovers
DIAGNOSE = _ARGS.diagnose
MAX_LINES_PER_ORDER = _ARGS.max_lines_per_order or _CFG_SCHED.get("max_lines_per_order")
MIN_RUN_HOURS_OVERRIDE = _ARGS.min_run_hours or _CFG_SCHED.get("min_run_hours")
NO_WEEK1_IN_WEEK0 = _ARGS.no_week1_in_week0
INITIAL_STATES_PATH = _ARGS.initial_states
TWO_PHASE = _ARGS.two_phase or _ARGS.rolling
VALIDATE = _ARGS.validate or _CFG_SCHED.get("validate", False)
ROLLING = _ARGS.rolling
OBJECTIVE_MODE = _ARGS.objective or _CFG_SCHED.get("objective", "balanced")

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

        if not prods:
            initial_sku = str(data.init_map.get(l, {}).get("initial_sku", "CLEAN"))
            carryover = 0
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
            avail_from = max(last_end_hour, last_cip_end)
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
                        "start_dt": sb_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_dt": sb_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "is_trial": is_trial,
                    })

    bounds_rows = []
    for o_idx, o in enumerate(orders):
        prod_val = solver.Value(produced[o_idx])
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
    return schedule_rows, bounds_rows


def write_solution(
    solver: cp_model.CpSolver,
    data: Data,
    P: Params,
    data_dir: Path,
    vars_dict: Dict[str, Any],
    hour_offset: int = 0,
) -> None:
    """Write schedule_phase2.csv and produced_vs_bounds.csv when solve is FEASIBLE/OPTIMAL."""
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
    orders_week0 = [o for o in data0.orders if int(o["due_end"]) <= WEEK0_END]
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
        return

    # ── Stage: Building Model (Week 0) ──
    update_stage(data_dir, "building_model_w0", "active")
    log(f"[two-phase] Phase 1: Week-0 only ({len(orders_week0)} orders), horizon=168")
    model0, vars0 = build_model(
        P0, data0, PHASE, RELAX_DEMAND, IGNORE_CHANGEOVERS,
        max_lines_per_order_override=MAX_LINES_PER_ORDER,
        objective_mode=OBJECTIVE_MODE,
    )
    proto0 = model0.Proto()
    update_stage(
        data_dir, "building_model_w0", "done",
        f"{len(proto0.variables):,} vars, {len(proto0.constraints):,} constraints",
    )

    # ── Stage: Solving Week 0 ──
    update_stage(data_dir, "solving_week0", "active", f"{int(tl)}s limit, 8 workers")
    update_solver_stats(data_dir, status="STARTING", time_limit_s=tl)
    solver0 = cp_model.CpSolver()
    solver0.parameters.num_search_workers = 8
    solver0.parameters.max_time_in_seconds = tl
    cb0 = _ProgressCallback(data_dir, label_prefix="W0: ")
    status0 = solver0.Solve(model0, cb0)
    if status0 not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        update_stage(data_dir, "solving_week0", "error", solver0.StatusName(status0))
        write_kpi_lines([f"Status: TWO_PHASE — Week-0 {solver0.StatusName(status0)}"])
        log(f"[two-phase] Week-0 failed: {solver0.StatusName(status0)}")
        return
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

    # Phase 2: Week-1 on FULL 336h horizon (lines available from Week-0 end)
    F_week1 = Files(data_dir)
    F_week1.init = str(data_dir / "week1_initial_states.csv")
    P1 = Params(
        horizon_h=336,  # Full 2-week horizon (lines blocked until Week-0 end via available_from)
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
        return

    # ── Stage: Building Model (Week 1) ──
    update_stage(data_dir, "building_model_w1", "active")
    log(f"[two-phase] Phase 2: Week-1 ({len(orders_week1)} orders), horizon=336 (full), maximize production")
    model1, vars1 = build_model(
        P1, data1, PHASE, RELAX_DEMAND, IGNORE_CHANGEOVERS,
        max_lines_per_order_override=MAX_LINES_PER_ORDER,
        maximize_production=True,
        objective_mode=OBJECTIVE_MODE,
    )
    proto1 = model1.Proto()
    update_stage(
        data_dir, "building_model_w1", "done",
        f"{len(proto1.variables):,} vars, {len(proto1.constraints):,} constraints",
    )

    # ── Stage: Solving Week 1 ──
    update_stage(data_dir, "solving_week1", "active", f"{int(tl)}s limit, 8 workers")
    solver1 = cp_model.CpSolver()
    solver1.parameters.num_search_workers = 8
    solver1.parameters.max_time_in_seconds = tl
    cb1 = _ProgressCallback(data_dir, label_prefix="W1: ")
    status1 = solver1.Solve(model1, cb1)

    if status1 not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        update_stage(data_dir, "solving_week1", "error", solver1.StatusName(status1))
        pd.DataFrame(schedule_rows_0).to_csv(data_dir / "schedule_phase2.csv", index=False)
        if cip_rows_0:
            pd.DataFrame(cip_rows_0).to_csv(data_dir / "cip_windows.csv", index=False)
        pd.DataFrame(bounds_0).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
        write_kpi_lines([f"Status: TWO_PHASE — Week-0 FEASIBLE, Week-1 {solver1.StatusName(status1)}"])
        log(f"[two-phase] Week-1 failed: {solver1.StatusName(status1)}")
        return
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

    # Final InitialStates for rolling (available_from=0 for next week)
    write_week1_initial_states(
        combined_schedule, combined_cips, data1, P1, data_dir,
        set_available_from_schedule=False,
    )
    update_stage(data_dir, "writing_output", "done", "Schedule and KPIs saved")

    # Idle-gap KPIs on combined schedule
    idle_kpi_lines = compute_idle_kpis(combined_schedule, combined_cips, data_dir)
    write_kpi_lines(["Status: TWO_PHASE — Week-0 and Week-1 FEASIBLE"] + idle_kpi_lines)
    log("[two-phase] Done: combined schedule written")
    for ln in idle_kpi_lines:
        log(ln)


def main() -> None:
    P = Params()
    # Apply config overrides from flowstate.toml
    if _CFG_SCHED.get("planning_start_date"):
        P.planning_start_date = _CFG_SCHED["planning_start_date"]
    _cfg_cip = _CFG.get("cip", {})
    if _cfg_cip.get("interval_h") is not None:
        P.cip_interval_h = int(_cfg_cip["interval_h"])
    if _cfg_cip.get("duration_h") is not None:
        P.cip_duration_h = int(_cfg_cip["duration_h"])
    _cfg_obj = _CFG.get("objective", {})
    if _cfg_obj.get("makespan_weight") is not None:
        P.objective_makespan_weight = int(_cfg_obj["makespan_weight"])
    if _cfg_obj.get("changeover_weight") is not None:
        P.objective_changeover_weight = int(_cfg_obj["changeover_weight"])
    if _cfg_obj.get("cip_defer_weight") is not None:
        P.objective_cip_defer_weight = int(_cfg_obj["cip_defer_weight"])
    if _cfg_obj.get("idle_weight") is not None:
        P.objective_idle_weight = int(_cfg_obj["idle_weight"])
    # Changeover type penalty weights — saved by settings.py into [objective]
    if _cfg_obj.get("co_conv_org_weight") is not None:
        P.co_conv_org_weight = int(_cfg_obj["co_conv_org_weight"])
    if _cfg_obj.get("co_cinn_weight") is not None:
        P.co_cinn_weight = int(_cfg_obj["co_cinn_weight"])
    if _cfg_obj.get("co_flavor_weight") is not None:
        P.co_flavor_weight = int(_cfg_obj["co_flavor_weight"])
    _cfg_co = _CFG.get("changeover", {})
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
    if _CFG_SCHED.get("use_sku_rates") is not None:
        P.use_sku_rates = bool(_CFG_SCHED["use_sku_rates"])
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
    if MAX_LINES_PER_ORDER is not None or MIN_RUN_HOURS_OVERRIDE is not None or NO_WEEK1_IN_WEEK0:
        P = Params(
            horizon_h=P.horizon_h,
            changeover_penalty=P.changeover_penalty,
            cip_interval_h=P.cip_interval_h,
            cip_duration_h=P.cip_duration_h,
            max_lines_per_order=MAX_LINES_PER_ORDER if MAX_LINES_PER_ORDER is not None else P.max_lines_per_order,
            stale_threshold_days=P.stale_threshold_days,
            stale_setup_extra_h=P.stale_setup_extra_h,
            long_shutdown_default_h=P.long_shutdown_default_h,
            planning_start_date=P.planning_start_date,
            min_run_hours=MIN_RUN_HOURS_OVERRIDE if MIN_RUN_HOURS_OVERRIDE is not None else P.min_run_hours,
            min_run_pct_of_qty=P.min_run_pct_of_qty,
            allow_week1_in_week0=not NO_WEEK1_IN_WEEK0,
            objective_makespan_weight=P.objective_makespan_weight,
            objective_changeover_weight=P.objective_changeover_weight,
            objective_cip_defer_weight=P.objective_cip_defer_weight,
            objective_idle_weight=P.objective_idle_weight,
            co_topload_weight=P.co_topload_weight,
            co_ttp_weight=P.co_ttp_weight,
            co_ffs_weight=P.co_ffs_weight,
            co_casepacker_weight=P.co_casepacker_weight,
            co_base_weight=P.co_base_weight,
            co_conv_org_weight=P.co_conv_org_weight,
            co_cinn_weight=P.co_cinn_weight,
            co_flavor_weight=P.co_flavor_weight,
            use_sku_rates=P.use_sku_rates,
        )
    reset_err()
    log(
        f"[{datetime.now()}] START phase={PHASE} relax={RELAX_DEMAND} ignoreCO={IGNORE_CHANGEOVERS} "
        f"tl={TIME_LIMIT} mlpo={P.max_lines_per_order}"
    )

    # Initialise structured progress
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
                model, vars_dict = build_model(
                    P,
                    data,
                    PHASE,
                    RELAX_DEMAND,
                    IGNORE_CHANGEOVERS,
                    max_lines_per_order_override=MAX_LINES_PER_ORDER,
                    objective_mode=OBJECTIVE_MODE,
                )
                proto = model.Proto()
                n_vars = len(proto.variables)
                n_cons = len(proto.constraints)
                update_stage(
                    DATA_DIR, "building_model", "done",
                    f"{n_vars:,} variables, {n_cons:,} constraints",
                )
                log(f"[model] {n_vars} vars, {n_cons} constraints")

                # ── Stage: Solving ──
                tl = float(TIME_LIMIT) if TIME_LIMIT is not None else 120.0
                update_stage(DATA_DIR, "solving", "active", f"{int(tl)}s time limit, 8 workers")
                update_solver_stats(DATA_DIR, status="STARTING", time_limit_s=tl)

                solver = cp_model.CpSolver()
                solver.parameters.num_search_workers = 8
                solver.parameters.max_time_in_seconds = tl
                cb = _ProgressCallback(DATA_DIR)
                status = solver.Solve(model, cb)
                status_name = solver.StatusName(status)
                log(f"[{datetime.now()}] SOLVER status={status_name}")
                update_solver_stats(
                    DATA_DIR,
                    status=status_name,
                    elapsed_s=round(solver.WallTime(), 1),
                )

                if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                    update_stage(DATA_DIR, "solving", "done", status_name)

                    # ── Stage: Writing Output ──
                    update_stage(DATA_DIR, "writing_output", "active")
                    write_solution(
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
                    write_kpi_lines([f"Status: {status_name}"] + idle_kpi_summary)
                else:
                    update_stage(DATA_DIR, "solving", "error", status_name)
                    write_kpi_lines([f"Status: {status_name}"])
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


if __name__ == "__main__":
    main()
