# validate_schedule.py -- Post-solve validation for Flowstate schedules.
# Checks: produced vs bounds, CIP spacing, changeover timing, no overlaps.
# Run standalone or import validate_all() from other modules.

from __future__ import annotations
import argparse
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pandas as pd

from data_loader import Params, Data, Files

# A per-line max_cip_hrs at or above this is the fill-mode STAND-DOWN sentinel
# (scenario_runner writes 100000 into the work dir so the model draws no CIP;
# the committed layer carries the real cleans). Such a line is reported as
# NOT CHECKED, never as OK (fix V / C47 / config-7). Same threshold as
# independent_validator.CIP_STANDDOWN_MIN.
CIP_STANDDOWN_MIN = 10000


# ---------------------------------------------------------------------------
# Check 1: produced vs demand bounds
# ---------------------------------------------------------------------------
def check_produced_vs_bounds(
    produced_path: Path,
    demand_path: Path,
) -> List[str]:
    """Compare produced_vs_bounds.csv against DemandPlan.csv. Returns list of issues."""
    issues: List[str] = []
    if not produced_path.exists():
        issues.append(f"MISSING: {produced_path}")
        return issues
    if not demand_path.exists():
        issues.append(f"MISSING: {demand_path}")
        return issues

    prod = pd.read_csv(produced_path)
    dem = pd.read_csv(demand_path)

    # Every demand order should appear in produced
    demand_orders = set(dem["order_id"].astype(str))
    produced_orders = set(prod["order_id"].astype(str))
    missing = demand_orders - produced_orders
    if missing:
        issues.append(f"BOUNDS: {len(missing)} order(s) in DemandPlan but missing from produced: {sorted(missing)[:10]}")

    # Check in_bounds for each produced order
    for _, r in prod.iterrows():
        oid = str(r["order_id"])
        produced_qty = int(r["produced"])
        qmin = int(r["qty_min"])
        qmax = int(r["qty_max"])
        in_bounds = bool(r["in_bounds"]) if "in_bounds" in r.index else (qmin <= produced_qty <= qmax)
        if not in_bounds:
            if produced_qty < qmin:
                issues.append(f"UNDER: {oid} produced={produced_qty} < qty_min={qmin} (short {qmin - produced_qty})")
            elif produced_qty > qmax:
                issues.append(f"OVER: {oid} produced={produced_qty} > qty_max={qmax} (excess {produced_qty - qmax})")

    if not issues:
        issues.append("BOUNDS: All orders within qty_min/qty_max. OK.")
    return issues


# ---------------------------------------------------------------------------
# Check 2: no schedule overlaps on same line
# ---------------------------------------------------------------------------
def check_no_overlaps(schedule_path: Path) -> List[str]:
    """Ensure no two production blocks overlap on the same line."""
    issues: List[str] = []
    if not schedule_path.exists():
        return ["MISSING: schedule_phase2.csv"]

    df = pd.read_csv(schedule_path)
    df = df.sort_values(["line_id", "start_hour"])

    for line_id, grp in df.groupby("line_id"):
        rows = grp.to_dict("records")
        for i in range(len(rows) - 1):
            a = rows[i]
            b = rows[i + 1]
            if b["start_hour"] < a["end_hour"]:
                issues.append(
                    f"OVERLAP: Line {a.get('line_name', line_id)} -- "
                    f"{a['order_id']} ends at h{a['end_hour']} but {b['order_id']} starts at h{b['start_hour']}"
                )
    if not issues:
        issues.append("OVERLAPS: No overlaps detected. OK.")
    return issues


# ---------------------------------------------------------------------------
# Check 3: CIP spacing (every 120 production hours)
# ---------------------------------------------------------------------------
def _read_line_cip_map(path: Path | None, default_h: int) -> Dict[int, int]:
    out: Dict[int, int] = {}
    if path and Path(path).exists():
        lc = pd.read_csv(path)
        lc["line_id"] = pd.to_numeric(lc["line_id"], errors="coerce").fillna(0).astype(int)
        lc["max_cip_hrs"] = pd.to_numeric(lc["max_cip_hrs"], errors="coerce").fillna(default_h).astype(int)
        for _, r in lc.iterrows():
            out[int(r["line_id"])] = int(r["max_cip_hrs"])
    return out


def check_cip_spacing(
    schedule_path: Path,
    cip_path: Path,
    init_path: Path,
    interval_h: int = 120,
    line_cip_hrs_path: Path | None = None,
    tolerance_h: float = 0.0,
    standdown_path: Path | None = None,
) -> List[str]:
    """Verify CIP occurs within the allowed clock-hours per line.

    Uses per-line intervals from *line_cip_hrs_path* when available,
    falling back to *interval_h* for lines not listed.

    Fix V (C47 / config-7, 2026-09-03):
      * ``tolerance_h`` replaces the hidden ``+ 12`` hours: the default is 0
        (the model has no tolerance either); ``[validator] cip_tolerance_h``
        in flowstate.toml sets it explicitly.
      * a line whose interval is the stand-down sentinel (>= CIP_STANDDOWN_MIN,
        read from *standdown_path* - normally the WORK-dir line_cip_hrs.csv -
        or from the limits file itself) is reported as ``NOT CHECKED``, never
        as OK: in fill mode the model is told 100000 h so it draws no CIP and
        the check could not fire. The limits themselves should come from the
        ORIGINAL reference file (validate_all resolves that).
    """
    issues: List[str] = []
    if not schedule_path.exists():
        return ["MISSING: schedule_phase2.csv"]

    sched = pd.read_csv(schedule_path)

    # Per-line CIP interval overrides (the limits) + stand-down flags
    cip_interval_map: Dict[int, int] = _read_line_cip_map(line_cip_hrs_path, interval_h)
    standdown_map: Dict[int, int] = (
        _read_line_cip_map(standdown_path, interval_h) if standdown_path else {})
    not_checked: List[str] = []

    # Load initial carryover (clock hours since last CIP before horizon)
    init_carry: Dict[int, int] = {}
    if init_path.exists():
        init_df = pd.read_csv(init_path)
        for _, r in init_df.iterrows():
            lid = int(r["line_id"])
            carry = int(pd.to_numeric(r.get("carryover_run_hours_since_last_cip_at_t0", 0), errors="coerce") or 0)
            init_carry[lid] = carry

    # CIP windows per line
    cip_by_line: Dict[int, List[Tuple[int, int]]] = {}
    if cip_path.exists():
        cip_df = pd.read_csv(cip_path)
        for _, r in cip_df.iterrows():
            lid = int(r["line_id"])
            cip_by_line.setdefault(lid, []).append((int(r["start_hour"]), int(r["end_hour"])))

    for line_id, grp in sched.groupby("line_id"):
        line_id = int(line_id)
        line_interval = cip_interval_map.get(line_id, interval_h)
        segs = sorted(
            [(int(r["start_hour"]), int(r["end_hour"])) for _, r in grp.iterrows()],
            key=lambda x: x[0],
        )
        cips = sorted(cip_by_line.get(line_id, []), key=lambda x: x[0])
        carry = init_carry.get(line_id, 0)
        line_name = grp.iloc[0].get("line_name", f"L{line_id}")

        if not segs:
            continue
        stood_down = standdown_map.get(line_id, line_interval)
        if line_interval >= CIP_STANDDOWN_MIN or stood_down >= CIP_STANDDOWN_MIN:
            limit_note = (f"reference limit {line_interval}h"
                          if line_interval < CIP_STANDDOWN_MIN else "no reference limit")
            not_checked.append(f"{line_name} ({limit_note})")
            continue

        first_start = segs[0][0]
        clock_since_cip = carry
        last_reference = first_start - carry
        cip_idx = 0

        for s, e in segs:
            while cip_idx < len(cips) and cips[cip_idx][0] <= s:
                last_reference = cips[cip_idx][1]
                clock_since_cip = 0
                cip_idx += 1
            clock_at_end = e - last_reference
            if clock_at_end > line_interval + tolerance_h:
                issues.append(
                    f"CIP: Line {line_name} -- {clock_at_end}h clock since last CIP "
                    f"(limit {line_interval}h, tolerance {tolerance_h:g}h) at h{e}"
                )

    n_lines = len(sched["line_id"].unique())
    if not issues and len(not_checked) < n_lines:
        issues.append("CIP: All checked lines within allowed clock-hours between CIPs. OK.")
    if not_checked:
        issues.append(
            f"CIP: NOT CHECKED on {len(not_checked)} of {n_lines} line(s) -- stand-down "
            f"sentinel max_cip_hrs >= {CIP_STANDDOWN_MIN} in the work dir (fill mode: the "
            "model draws no CIP, the committed layer carries the cleans; verify on the "
            "calendar): " + ", ".join(not_checked)
        )
    return issues


# ---------------------------------------------------------------------------
# Check 4: changeover timing between consecutive runs on same line
# ---------------------------------------------------------------------------
def check_changeover_timing(
    schedule_path: Path,
    changeovers_path: Path,
) -> List[str]:
    """Verify gaps between consecutive runs respect changeover setup times."""
    issues: List[str] = []
    if not schedule_path.exists():
        return ["MISSING: schedule_phase2.csv"]
    if not changeovers_path.exists():
        return ["MISSING: changeovers.csv"]

    sched = pd.read_csv(schedule_path)
    chg = pd.read_csv(changeovers_path)
    setup_map: Dict[Tuple[str, str], int] = {}
    for _, r in chg.iterrows():
        f = str(r["from_sku"])
        t = str(r["to_sku"])
        h = int(round(float(r["setup_hours"])))
        setup_map[(f, t)] = h

    for line_id, grp in sched.groupby("line_id"):
        rows = grp.sort_values("start_hour").to_dict("records")
        for i in range(len(rows) - 1):
            a = rows[i]
            b = rows[i + 1]
            sku_a = str(a["sku"])
            sku_b = str(b["sku"])
            if sku_a == sku_b:
                continue  # same SKU, no changeover needed
            required = setup_map.get((sku_a, sku_b), 0)
            gap = int(b["start_hour"]) - int(a["end_hour"])
            if gap < required:
                line_name = a.get("line_name", f"L{line_id}")
                issues.append(
                    f"CHANGEOVER: Line {line_name} -- {sku_a}->{sku_b} needs {required}h setup but gap is {gap}h "
                    f"(h{a['end_hour']}->h{b['start_hour']})"
                )

    if not issues:
        issues.append("CHANGEOVERS: All gaps respect setup times. OK.")
    return issues


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------
def _load_cfg_dict(data_dir: Path, cfg: Any) -> dict:
    if isinstance(cfg, dict):
        return cfg
    p = Path(cfg) if cfg else data_dir / "flowstate.toml"
    if not p.exists():
        return {}
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve_cip_check_inputs(
    data_dir: Path,
    cfg: Any = None,
    *,
    interval_h: int | None = None,
    line_cip_hrs_path: Path | None = None,
    tolerance_h: float | None = None,
) -> Dict[str, Any]:
    """Where the CIP-spacing check takes its limits from (fix V / config-7).

    interval_h: explicit > ``[cip] interval_h`` of the config (the work-dir
    flowstate.toml unless ``cfg`` is given) > 120.
    line_cip_hrs_path: explicit > the ORIGINAL ``data/reference/line_cip_hrs.csv``
    (the work dir sits in ``data/_scenario_work/<id>``) > the work-dir copy.
    The work-dir copy is always read for the stand-down flags.
    tolerance_h: explicit > ``[validator] cip_tolerance_h`` > 0.
    """
    data_dir = Path(data_dir)
    raw = _load_cfg_dict(data_dir, cfg)
    cip = raw.get("cip", {}) or {}
    src_int = "explicit"
    if interval_h is None:
        if cip.get("interval_h") is not None:
            interval_h, src_int = int(cip["interval_h"]), "[cip] interval_h"
        else:
            interval_h, src_int = 120, "default 120"
    work_lc = data_dir / "line_cip_hrs.csv"
    src_lc = "explicit"
    if line_cip_hrs_path is None:
        ref_lc = data_dir.parent.parent / "reference" / "line_cip_hrs.csv"
        if ref_lc.exists():
            line_cip_hrs_path, src_lc = ref_lc, "reference (original)"
        else:
            line_cip_hrs_path, src_lc = work_lc, "work dir"
    if tolerance_h is None:
        val = raw.get("validator", {}) or {}
        tolerance_h = float(val.get("cip_tolerance_h", 0.0) or 0.0)
    return {
        "interval_h": int(interval_h), "interval_src": src_int,
        "line_cip_hrs_path": Path(line_cip_hrs_path), "line_cip_hrs_src": src_lc,
        "standdown_path": work_lc if work_lc.exists() else None,
        "tolerance_h": float(tolerance_h),
    }


def validate_all(
    data_dir: Path,
    verbose: bool = True,
    *,
    cfg: Any = None,
    interval_h: int | None = None,
    line_cip_hrs_path: Path | None = None,
    tolerance_h: float | None = None,
) -> List[str]:
    """Run all validation checks and return combined report lines.

    ``cfg`` (dict or toml path) supplies ``[cip] interval_h`` and
    ``[validator] cip_tolerance_h``; see resolve_cip_check_inputs for the
    precedence. Positional use ``validate_all(data_dir, verbose)`` is unchanged.
    """
    data_dir = Path(data_dir)
    report: List[str] = ["=" * 60, "Flowstate Schedule Validation Report", "=" * 60, ""]

    # Bounds
    report.append("--- Produced vs Bounds ---")
    bounds_issues = check_produced_vs_bounds(
        data_dir / "produced_vs_bounds.csv",
        data_dir / "demand_plan.csv",
    )
    report.extend(bounds_issues)
    report.append("")

    # Overlaps
    report.append("--- Schedule Overlaps ---")
    overlap_issues = check_no_overlaps(data_dir / "schedule_phase2.csv")
    report.extend(overlap_issues)
    report.append("")

    # CIP
    report.append("--- CIP Spacing ---")
    _cip_in = resolve_cip_check_inputs(
        data_dir, cfg, interval_h=interval_h,
        line_cip_hrs_path=line_cip_hrs_path, tolerance_h=tolerance_h)
    report.append(
        f"CIP check inputs: interval_h={_cip_in['interval_h']} ({_cip_in['interval_src']}); "
        f"per-line limits from {_cip_in['line_cip_hrs_path']} ({_cip_in['line_cip_hrs_src']}); "
        f"tolerance {_cip_in['tolerance_h']:g}h"
    )
    cip_issues = check_cip_spacing(
        data_dir / "schedule_phase2.csv",
        data_dir / "cip_windows.csv",
        data_dir / "initial_states.csv",
        interval_h=_cip_in["interval_h"],
        line_cip_hrs_path=_cip_in["line_cip_hrs_path"],
        tolerance_h=_cip_in["tolerance_h"],
        standdown_path=_cip_in["standdown_path"],
    )
    report.extend(cip_issues)
    report.append("")

    # Changeovers
    report.append("--- Changeover Timing ---")
    co_issues = check_changeover_timing(
        data_dir / "schedule_phase2.csv",
        data_dir / "changeovers.csv",
    )
    report.extend(co_issues)
    report.append("")

    # Summary
    all_issues = bounds_issues + overlap_issues + cip_issues + co_issues
    n_ok = sum(1 for i in all_issues if i.endswith("OK."))
    n_problems = sum(1 for i in all_issues
                     if not i.endswith("OK.") and not i.startswith("MISSING")
                     and not i.startswith("CIP check inputs:"))
    report.append("=" * 60)
    report.append(f"Checks passed: {n_ok}/4    Issues found: {n_problems}")
    report.append("=" * 60)

    # Write to file
    with open(data_dir / "validation_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    if verbose:
        for line in report:
            print(line)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Flowstate schedule outputs.")
    parser.add_argument("--data-dir", type=Path, default=None, help="Data directory")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve() if args.data_dir else BASE_DIR
    validate_all(data_dir)


if __name__ == "__main__":
    main()
