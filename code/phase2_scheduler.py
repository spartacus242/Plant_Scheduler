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
        help="Path to InitialStates CSV (default: data-dir/InitialStates.csv). Use Week-1_InitialStates.csv for next run.",
    )
    parser.add_argument(
        "--two-phase",
        action="store_true",
        help="Run Week-0 only, then Week-1 only with Week-1 InitialStates from Week-0; merge into one schedule (shows Week-1 even when qty_min=0).",
    )
    return parser.parse_args()


_ARGS = _parse_args()
DATA_DIR = _ARGS.data_dir.resolve() if _ARGS.data_dir is not None else BASE_DIR
ERR_FILE = DATA_DIR / "solver_error.txt"
KPI_FILE = DATA_DIR / "solver_kpis.txt"
TIME_LIMIT = _ARGS.time_limit
PHASE = _ARGS.phase
RELAX_DEMAND = _ARGS.relax_demand
IGNORE_CHANGEOVERS = _ARGS.ignore_changeovers
DIAGNOSE = _ARGS.diagnose
MAX_LINES_PER_ORDER = _ARGS.max_lines_per_order
MIN_RUN_HOURS_OVERRIDE = _ARGS.min_run_hours
NO_WEEK1_IN_WEEK0 = _ARGS.no_week1_in_week0
INITIAL_STATES_PATH = _ARGS.initial_states
TWO_PHASE = _ARGS.two_phase

# Week boundaries (must match model_builder)
WEEK0_END = 167
WEEK1_START = 168


def reset_err() -> None:
    try:
        if ERR_FILE.exists():
            ERR_FILE.unlink()
    except Exception:
        pass


def log(msg: str) -> None:
    try:
        with open(ERR_FILE, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


def write_kpi_lines(lines: List[str]) -> None:
    with open(KPI_FILE, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln.rstrip() + "\n")


def compute_cip_windows(
    schedule_rows: List[Dict[str, Any]],
    data: Data,
    P: Params,
) -> List[Dict[str, Any]]:
    """Place 6h CIP blocks in gaps between production only (no overlap with production).
    CIPs are due every 120 production hours (including carryover); each is placed in the
    first gap after that run-hour mark that has at least 6h free.
    """
    interval_h = P.cip_interval_h
    duration_h = P.cip_duration_h
    by_line: Dict[int, List[Tuple[int, int]]] = {}
    line_names: Dict[int, str] = {}
    for row in schedule_rows:
        l = row["line_id"]
        by_line.setdefault(l, []).append((row["start_hour"], row["end_hour"]))
        line_names[l] = row.get("line_name", f"L{l}")

    cip_rows: List[Dict[str, Any]] = []
    for l in sorted(by_line.keys()):
        segments = sorted(by_line[l], key=lambda x: x[0])
        carryover = int(data.init_map.get(l, {}).get("carryover_run_hours", 0))
        name = line_names[l]

        # Gaps between consecutive production segments: (end of seg i, start of seg i+1)
        gaps: List[Tuple[int, int]] = []
        for i in range(len(segments) - 1):
            g_start, g_end = segments[i][1], segments[i + 1][0]
            if g_end > g_start:
                gaps.append((g_start, g_end))

        # Run-hours completed before each gap (before gap 0 = after segment 0, etc.)
        run_before_gap: List[int] = []
        run_done = carryover
        for i, (s, e) in enumerate(segments):
            run_done += e - s
            run_before_gap.append(run_done)

        # How many CIPs are needed (every 120 run-hours from carryover)
        total_run = run_done - carryover if segments else 0
        total_run += carryover
        n_cip = 0
        r = carryover
        while r + interval_h <= total_run:
            n_cip += 1
            r += interval_h

        # Place each CIP in the first gap that (a) has run_before_gap >= required and (b) length >= 6
        used_gap = 0
        for cip_num in range(n_cip):
            required_run = carryover + (cip_num + 1) * interval_h
            placed = False
            for j in range(used_gap, len(gaps)):
                if run_before_gap[j] < required_run:
                    continue
                g_start, g_end = gaps[j]
                if g_end - g_start < duration_h:
                    continue
                cip_rows.append({
                    "line_id": l,
                    "line_name": name,
                    "start_hour": g_start,
                    "end_hour": g_start + duration_h,
                })
                used_gap = j + 1
                placed = True
                break
            if not placed:
                # No suitable gap (model should have ensured one); skip this CIP in output
                break
    return cip_rows


def write_week1_initial_states(
    schedule_rows: List[Dict[str, Any]],
    cip_rows: List[Dict[str, Any]],
    data: Data,
    P: Params,
    data_dir: Path,
) -> None:
    """Write Week-1_InitialStates.csv: state of each line at end of horizon for use as next run's InitialStates."""
    try:
        anchor = datetime.strptime(P.planning_start_date, "%Y-%m-%d %H:%M:%S")
    except Exception:
        anchor = datetime(2026, 2, 15, 0, 0, 0)

    # Per line: last CIP end hour (0 if no CIP)
    cip_by_line: Dict[int, List[Dict[str, Any]]] = {}
    for row in cip_rows:
        l = row["line_id"]
        cip_by_line.setdefault(l, []).append(row)
    last_cip_end_by_line: Dict[int, int] = {}
    for l, rows in cip_by_line.items():
        last_cip_end_by_line[l] = max(r["end_hour"] for r in rows) if rows else 0

    # Per line: production segments (start_hour, end_hour, run_hours, sku)
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
        else:
            # Last SKU = SKU of run that ends last
            last_run = max(prods, key=lambda x: x["end_hour"])
            initial_sku = str(last_run["sku"])
            # Carryover = run hours from last CIP end to end of horizon
            carryover = sum(
                p["run_hours"] for p in prods
                if p["start_hour"] >= last_cip_end
            )
            # Last CIP end datetime (optional)
            if last_cip_end > 0 and l in cip_by_line:
                last_cip_dt = (anchor + timedelta(hours=last_cip_end)).strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_cip_dt = ""

        out_rows.append({
            "line_id": l,
            "line_name": line_name,
            "initial_sku": initial_sku,
            "available_from_hour": 0,
            "long_shutdown_flag": 0,
            "long_shutdown_extra_setup_hours": 0,
            "carryover_run_hours_since_last_cip_at_t0": int(min(carryover, 119)),  # cap below 120
            "last_cip_end_datetime": last_cip_dt,
            "comment": "Auto from week-0 run",
        })

    df = pd.DataFrame(out_rows)
    df.to_csv(data_dir / "Week-1_InitialStates.csv", index=False)


def _solution_to_rows(
    solver: cp_model.CpSolver,
    data: Data,
    P: Params,
    present: Dict[Tuple[int, int], Any],
    run_h: Dict[Tuple[int, int], Any],
    start: Dict[Tuple[int, int], Any],
    end: Dict[Tuple[int, int], Any],
    produced: Dict[int, Any],
    hour_offset: int = 0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build schedule_rows and bounds_rows from solver; hour_offset is added to all hours (for Week-1 merge)."""
    orders = data.orders
    lines = data.lines
    try:
        anchor = datetime.strptime(P.planning_start_date, "%Y-%m-%d %H:%M:%S")
    except Exception:
        anchor = datetime(2026, 2, 15, 0, 0, 0)

    schedule_rows = []
    for l in lines:
        for o_idx, o in enumerate(orders):
            key = (l, o_idx)
            if not solver.BooleanValue(present[key]):
                continue
            s_val = solver.Value(start[key]) + hour_offset
            e_val = solver.Value(end[key]) + hour_offset
            rh_val = solver.Value(run_h[key])
            line_name = data.line_names.get(l, f"L{l}")
            start_dt = anchor + timedelta(hours=s_val)
            end_dt = anchor + timedelta(hours=e_val)
            schedule_rows.append({
                "line_id": l,
                "line_name": line_name,
                "order_id": o["order_id"],
                "sku": o["sku"],
                "start_hour": s_val,
                "end_hour": e_val,
                "run_hours": rh_val,
                "start_dt": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "end_dt": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
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
    present: Dict[Tuple[int, int], Any],
    run_h: Dict[Tuple[int, int], Any],
    start: Dict[Tuple[int, int], Any],
    end: Dict[Tuple[int, int], Any],
    produced: Dict[int, Any],
    hour_offset: int = 0,
) -> None:
    """Write schedule_phase2.csv and produced_vs_bounds.csv when solve is FEASIBLE/OPTIMAL."""
    schedule_rows, bounds_rows = _solution_to_rows(
        solver, data, P, present, run_h, start, end, produced, hour_offset
    )
    if schedule_rows:
        pd.DataFrame(schedule_rows).to_csv(data_dir / "schedule_phase2.csv", index=False)
        cip_rows = compute_cip_windows(schedule_rows, data, P)
        if cip_rows:
            pd.DataFrame(cip_rows).to_csv(data_dir / "cip_windows.csv", index=False)
        write_week1_initial_states(schedule_rows, cip_rows or [], data, P, data_dir)
    pd.DataFrame(bounds_rows).to_csv(data_dir / "produced_vs_bounds.csv", index=False)


def _run_two_phase(P: Params, F: Files, data_dir: Path) -> None:
    """Run Week-0 only, then Week-1 only with InitialStates from Week-0; merge schedule and write."""
    tl = float(TIME_LIMIT) if TIME_LIMIT is not None else 120.0
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
    )
    data0 = Data(P0, F)
    data0.load()
    orders_week0 = [o for o in data0.orders if int(o["due_end"]) <= WEEK0_END]
    data0.orders = orders_week0
    if not orders_week0:
        log("[two-phase] No Week-0 orders")
        write_kpi_lines(["Status: TWO_PHASE — no Week-0 orders"])
        return

    log(f"[two-phase] Phase 1: Week-0 only ({len(orders_week0)} orders), horizon=168")
    model0, vars0 = build_model(
        P0, data0, PHASE, RELAX_DEMAND, IGNORE_CHANGEOVERS,
        max_lines_per_order_override=MAX_LINES_PER_ORDER,
    )
    solver0 = cp_model.CpSolver()
    solver0.parameters.num_search_workers = 8
    solver0.parameters.max_time_in_seconds = tl
    status0 = solver0.Solve(model0)
    if status0 not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        write_kpi_lines([f"Status: TWO_PHASE — Week-0 {solver0.StatusName(status0)}"])
        log(f"[two-phase] Week-0 failed: {solver0.StatusName(status0)}")
        return

    schedule_rows_0, bounds_0 = _solution_to_rows(
        solver0, data0, P0, vars0["present"], vars0["run_h"], vars0["start"], vars0["end"], vars0["produced"], 0
    )
    cip_rows_0 = compute_cip_windows(schedule_rows_0, data0, P0)
    write_week1_initial_states(schedule_rows_0, cip_rows_0 or [], data0, P0, data_dir)

    # Phase 2: Week-1 with InitialStates from Week-0
    F_week1 = Files(data_dir)
    F_week1.init = str(data_dir / "Week-1_InitialStates.csv")
    P1 = Params(
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
        allow_week1_in_week0=False,
        objective_makespan_weight=P.objective_makespan_weight,
        objective_changeover_weight=P.objective_changeover_weight,
    )
    data1 = Data(P1, F_week1)
    data1.load()
    orders_week1 = [o for o in data1.orders if int(o["due_start"]) >= WEEK1_START]
    for o in orders_week1:
        o["due_start"] = int(o["due_start"]) - WEEK1_START
        o["due_end"] = int(o["due_end"]) - WEEK1_START
    data1.orders = orders_week1

    if not orders_week1:
        pd.DataFrame(schedule_rows_0).to_csv(data_dir / "schedule_phase2.csv", index=False)
        if cip_rows_0:
            pd.DataFrame(cip_rows_0).to_csv(data_dir / "cip_windows.csv", index=False)
        pd.DataFrame(bounds_0).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
        write_kpi_lines(["Status: TWO_PHASE — Week-0 FEASIBLE, no Week-1 orders"])
        return

    log(f"[two-phase] Phase 2: Week-1 only ({len(orders_week1)} orders), horizon=168, maximize production")
    model1, vars1 = build_model(
        P1, data1, PHASE, RELAX_DEMAND, IGNORE_CHANGEOVERS,
        max_lines_per_order_override=MAX_LINES_PER_ORDER,
        maximize_production=True,
    )
    solver1 = cp_model.CpSolver()
    solver1.parameters.num_search_workers = 8
    solver1.parameters.max_time_in_seconds = tl
    status1 = solver1.Solve(model1)

    if status1 not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        pd.DataFrame(schedule_rows_0).to_csv(data_dir / "schedule_phase2.csv", index=False)
        if cip_rows_0:
            pd.DataFrame(cip_rows_0).to_csv(data_dir / "cip_windows.csv", index=False)
        pd.DataFrame(bounds_0).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
        write_kpi_lines([f"Status: TWO_PHASE — Week-0 FEASIBLE, Week-1 {solver1.StatusName(status1)}"])
        log(f"[two-phase] Week-1 failed: {solver1.StatusName(status1)}")
        return

    schedule_rows_1, bounds_1 = _solution_to_rows(
        solver1, data1, P1, vars1["present"], vars1["run_h"], vars1["start"], vars1["end"], vars1["produced"], WEEK1_START
    )
    combined_schedule = schedule_rows_0 + schedule_rows_1
    combined_bounds = bounds_0 + bounds_1
    pd.DataFrame(combined_schedule).to_csv(data_dir / "schedule_phase2.csv", index=False)
    pd.DataFrame(combined_bounds).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
    cip_combined = compute_cip_windows(combined_schedule, data1, P1)
    if cip_combined:
        pd.DataFrame(cip_combined).to_csv(data_dir / "cip_windows.csv", index=False)
    write_week1_initial_states(combined_schedule, cip_combined or [], data1, P1, data_dir)
    write_kpi_lines(["Status: TWO_PHASE — Week-0 and Week-1 FEASIBLE"])
    log("[two-phase] Done: combined schedule written")


def main() -> None:
    P = Params()
    F = Files(DATA_DIR)
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
        )
    reset_err()
    log(
        f"[{datetime.now()}] START phase={PHASE} relax={RELAX_DEMAND} ignoreCO={IGNORE_CHANGEOVERS} "
        f"tl={TIME_LIMIT} mlpo={P.max_lines_per_order}"
    )
    try:
        if TWO_PHASE:
            _run_two_phase(P, F, DATA_DIR)
        else:
            data = Data(P, F)
            data.load()
            if DIAGNOSE:
                run_diagnostics(P, data, DATA_DIR)
                run_unique_line_load_diagnostic(P, data, DATA_DIR)
                run_blockages_diagnostic(P, data, DATA_DIR)
                write_kpi_lines([
                    "Status: DIAG COMPLETE (see diag_order_linecap.csv, diag_unique_line_load.csv, diag_blockages.csv / diag_blockages.txt)"
                ])
            else:
                model, vars_dict = build_model(
                    P,
                    data,
                    PHASE,
                    RELAX_DEMAND,
                    IGNORE_CHANGEOVERS,
                    max_lines_per_order_override=MAX_LINES_PER_ORDER,
                )
                solver = cp_model.CpSolver()
                solver.parameters.num_search_workers = 8
                solver.parameters.max_time_in_seconds = (
                    float(TIME_LIMIT) if TIME_LIMIT is not None else 120.0
                )
                status = solver.Solve(model)
                status_name = solver.StatusName(status)
                write_kpi_lines([f"Status: {status_name}"])
                log(f"[{datetime.now()}] SOLVER status={status_name}")
                if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                    write_solution(
                        solver,
                        data,
                        P,
                        DATA_DIR,
                        vars_dict["present"],
                        vars_dict["run_h"],
                        vars_dict["start"],
                        vars_dict["end"],
                        vars_dict["produced"],
                    )
    except Exception:
        log("\n=== FATAL ERROR ===\n" + traceback.format_exc())
        write_kpi_lines(["Status: ERROR — see solver_error.txt"])


if __name__ == "__main__":
    main()
