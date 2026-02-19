# helpers/sandbox_engine.py — Validation and KPI engine for the sandbox.
#
# All functions accept plain Python dicts/lists (JSON-friendly) so they
# can be used both from the Streamlit page and from a future custom React
# component via setStateValue.

from __future__ import annotations
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd


# ── Data loading helpers ────────────────────────────────────────────────

def _get_planning_month(data_dir: Path) -> int:
    """Return the planning month integer from flowstate.toml, or current month."""
    import datetime as _dt
    toml_path = data_dir.parent / "flowstate.toml"
    if not toml_path.exists():
        toml_path = data_dir / "flowstate.toml"
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        with open(toml_path, "rb") as f:
            cfg = tomllib.load(f)
        date_str = cfg.get("scheduler", {}).get("planning_start_date", "")
        if date_str:
            return _dt.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").month
    except Exception:
        pass
    return _dt.datetime.now().month


def load_capabilities(data_dir: Path) -> Dict[Tuple[str, str], float]:
    """Return {(line_name, sku): rate_kgph} for capable pairs.

    Base rates come from capabilities_rates.csv (calc_rate_kgph).
    Monthly rates from line_rates.csv then override on a per-line basis,
    matching the month of planning_start_date from flowstate.toml.
    """
    path = data_dir / "capabilities_rates.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["line_id"] = pd.to_numeric(df["line_id"], errors="coerce").fillna(0).astype(int)
    rate_col = "calc_rate_kgph" if "calc_rate_kgph" in df.columns else "rate_uph"
    # Build base capability map and track line_id per line_name
    out: Dict[Tuple[str, str], float] = {}
    line_name_to_id: Dict[str, int] = {}
    for _, r in df.iterrows():
        if int(r.get("capable", 0)) == 1:
            rate = float(r.get(rate_col, 0))
            if rate > 0:
                ln = str(r["line_name"])
                out[(ln, str(r["sku"]))] = rate
                line_name_to_id[ln] = int(r["line_id"])

    # Apply monthly line-rate override from line_rates.csv
    lr_path = data_dir / "line_rates.csv"
    if lr_path.exists():
        plan_month = _get_planning_month(data_dir)
        lr = pd.read_csv(lr_path)
        lr["line_id"] = pd.to_numeric(lr["line_id"], errors="coerce").fillna(0).astype(int)
        lr["Month"] = pd.to_numeric(lr["Month"], errors="coerce").fillna(0).astype(int)
        lr["rate_kgph"] = pd.to_numeric(lr["rate_kgph"], errors="coerce").fillna(0.0)
        lr_month = lr[lr["Month"] == plan_month]
        line_rate_map: Dict[int, float] = {}
        for _, r in lr_month.iterrows():
            line_rate_map[int(r["line_id"])] = float(r["rate_kgph"])
        # Override rates for all capable (line_name, sku) pairs where the line has a monthly rate
        for (ln, sku) in list(out.keys()):
            lid = line_name_to_id.get(ln)
            if lid is not None and lid in line_rate_map and line_rate_map[lid] > 0:
                out[(ln, sku)] = line_rate_map[lid]

    return out


def load_changeovers(data_dir: Path) -> Dict[Tuple[str, str], int]:
    """Return {(from_sku, to_sku): setup_hours}."""
    path = data_dir / "changeovers.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    out: Dict[Tuple[str, str], int] = {}
    for _, r in df.iterrows():
        h = int(round(float(r.get("setup_hours", 0))))
        out[(str(r["from_sku"]), str(r["to_sku"]))] = h
    return out


def load_demand_targets(data_dir: Path) -> List[Dict[str, Any]]:
    """Return list of {order_id, sku, qty_min, qty_max}."""
    path = data_dir / "demand_plan.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    out = []
    for _, r in df.iterrows():
        qt = float(r.get("qty_target", 0))
        lp = r.get("lower_pct")
        up = r.get("upper_pct")
        qmin = r.get("qty_min")
        qmax = r.get("qty_max")
        if pd.notna(lp) and pd.notna(up):
            qmin = int(math.floor(qt * float(lp)))
            qmax = int(math.ceil(qt * float(up)))
        else:
            qmin = int(qmin) if pd.notna(qmin) else 0
            qmax = int(qmax) if pd.notna(qmax) else 0
        out.append({
            "order_id": str(r.get("order_id", "")),
            "sku": str(r.get("sku", "")),
            "qty_min": qmin,
            "qty_max": qmax,
        })
    return out


# ── Validation / KPI functions ──────────────────────────────────────────

def is_capable(line_name: str, sku: str, caps: Dict[Tuple[str, str], float]) -> bool:
    """Check if a line can produce the given SKU."""
    return (line_name, sku) in caps


def get_rate(line_name: str, sku: str, caps: Dict[Tuple[str, str], float]) -> float:
    """Return the production rate (UPH) for line+SKU, or 0 if not capable."""
    return caps.get((line_name, sku), 0.0)


def recalc_duration(
    block: Dict[str, Any],
    new_line: str,
    caps: Dict[Tuple[str, str], float],
) -> float | None:
    """Recalculate run hours when moving a block to a different line.

    Returns new run_hours or None if not capable.
    """
    sku = str(block["sku"])
    old_line = str(block["line_name"])
    old_rate = get_rate(old_line, sku, caps)
    new_rate = get_rate(new_line, sku, caps)
    if new_rate <= 0:
        return None  # not capable
    if old_rate <= 0:
        return float(block.get("run_hours", 0))
    qty = old_rate * float(block.get("run_hours", 0))
    return math.ceil(qty / new_rate)


def compute_adherence(
    schedule: List[Dict[str, Any]],
    demand: List[Dict[str, Any]],
    caps: Dict[Tuple[str, str], float],
) -> List[Dict[str, Any]]:
    """Compute per-order demand adherence.

    Returns a list of dicts sorted by SKU (A-Z):
      {order_id, sku, qty_min, qty_max, scheduled_qty, pct_adherence, status}
    """
    # Sum scheduled qty per order
    sched_by_order: Dict[str, float] = {}
    for blk in schedule:
        oid = str(blk.get("order_id", ""))
        ln = str(blk.get("line_name", ""))
        sku = str(blk.get("sku", ""))
        rh = float(blk.get("run_hours", 0))
        rate = get_rate(ln, sku, caps)
        sched_by_order[oid] = sched_by_order.get(oid, 0) + rate * rh

    rows = []
    for d in demand:
        oid = d["order_id"]
        qty_min = d["qty_min"]
        qty_max = d["qty_max"]
        sq = sched_by_order.get(oid, 0)
        if qty_min > 0:
            pct = min(100.0, sq / qty_min * 100)
        else:
            pct = 100.0
        if sq < qty_min:
            status = "UNDER"
        elif sq > qty_max:
            status = "OVER"
        else:
            status = "MET"
        rows.append({
            "order_id": oid,
            "sku": d["sku"],
            "qty_min": qty_min,
            "qty_max": qty_max,
            "scheduled_qty": int(round(sq)),
            "pct_adherence": round(pct, 1),
            "status": status,
        })
    rows.sort(key=lambda r: r["sku"])
    return rows


def overall_adherence(adherence_rows: List[Dict[str, Any]]) -> float:
    """Return overall % of orders that are MET."""
    if not adherence_rows:
        return 100.0
    met = sum(1 for r in adherence_rows if r["status"] == "MET")
    return round(met / len(adherence_rows) * 100, 1)


def count_changeovers(
    schedule: List[Dict[str, Any]],
) -> Tuple[int, Dict[str, int]]:
    """Count SKU changeovers per line and total.

    Returns (total, {line_name: count}).
    """
    by_line: Dict[str, List[Dict[str, Any]]] = {}
    for blk in schedule:
        ln = str(blk.get("line_name", ""))
        by_line.setdefault(ln, []).append(blk)

    per_line: Dict[str, int] = {}
    total = 0
    for ln, blocks in sorted(by_line.items()):
        blocks.sort(key=lambda b: float(b.get("start_hour", 0)))
        count = 0
        for i in range(1, len(blocks)):
            if str(blocks[i]["sku"]) != str(blocks[i - 1]["sku"]):
                count += 1
        per_line[ln] = count
        total += count
    return total, per_line


def check_overlaps(schedule: List[Dict[str, Any]]) -> List[str]:
    """Return list of overlap descriptions (empty = OK)."""
    by_line: Dict[str, List[Dict[str, Any]]] = {}
    for blk in schedule:
        ln = str(blk.get("line_name", ""))
        by_line.setdefault(ln, []).append(blk)

    issues = []
    for ln, blocks in by_line.items():
        blocks.sort(key=lambda b: float(b.get("start_hour", 0)))
        for i in range(1, len(blocks)):
            if float(blocks[i]["start_hour"]) < float(blocks[i - 1]["end_hour"]):
                issues.append(
                    f"{ln}: {blocks[i-1]['order_id']} (ends h{blocks[i-1]['end_hour']}) "
                    f"overlaps {blocks[i]['order_id']} (starts h{blocks[i]['start_hour']})"
                )
    return issues


def split_block(
    block: Dict[str, Any],
    split_hour: float,
    min_run: int = 4,
) -> Tuple[Dict[str, Any], Dict[str, Any]] | None:
    """Split a block at split_hour into seg_a and seg_b.

    Returns (seg_a, seg_b) or None if either segment < min_run.
    """
    start = float(block["start_hour"])
    end = float(block["end_hour"])
    if split_hour <= start + min_run or split_hour >= end - min_run:
        return None  # segments too short
    seg_a = {**block, "end_hour": split_hour, "run_hours": split_hour - start}
    seg_b = {**block, "start_hour": split_hour, "run_hours": end - split_hour}
    return seg_a, seg_b


def save_sandbox_to_files(
    schedule: List[Dict[str, Any]],
    cip_blocks: List[Dict[str, Any]],
    caps: Dict[Tuple[str, str], float],
    demand: List[Dict[str, Any]],
    data_dir: Path,
) -> None:
    """Write sandbox state to schedule_phase2.csv, cip_windows.csv, produced_vs_bounds.csv."""
    from datetime import datetime, timedelta

    anchor = datetime(2026, 2, 15, 0, 0, 0)

    # Schedule
    rows = []
    for blk in schedule:
        sh = float(blk["start_hour"])
        eh = float(blk["end_hour"])
        rh = float(blk["run_hours"])
        sdt = anchor + timedelta(hours=sh)
        edt = anchor + timedelta(hours=eh)
        rows.append({
            "line_id": blk.get("line_id", 0),
            "line_name": blk.get("line_name", ""),
            "order_id": blk.get("order_id", ""),
            "sku": blk.get("sku", ""),
            "sku_description": blk.get("sku_description", ""),
            "start_hour": sh,
            "end_hour": eh,
            "run_hours": rh,
            "start_dt": sdt.strftime("%Y-%m-%d %H:%M:%S"),
            "end_dt": edt.strftime("%Y-%m-%d %H:%M:%S"),
            "is_trial": blk.get("is_trial", False),
        })
    pd.DataFrame(rows).to_csv(data_dir / "schedule_phase2.csv", index=False)

    # CIP windows
    if cip_blocks:
        cip_rows = []
        for c in cip_blocks:
            cip_rows.append({
                "line_id": c.get("line_id", 0),
                "line_name": c.get("line_name", ""),
                "start_hour": c["start_hour"],
                "end_hour": c["end_hour"],
            })
        pd.DataFrame(cip_rows).to_csv(data_dir / "cip_windows.csv", index=False)

    # Produced vs bounds
    adherence = compute_adherence(schedule, demand, caps)
    bounds_rows = []
    for a in adherence:
        bounds_rows.append({
            "order_id": a["order_id"],
            "sku": a["sku"],
            "qty_min": a["qty_min"],
            "qty_max": a["qty_max"],
            "produced": a["scheduled_qty"],
            "in_bounds": a["status"] == "MET",
        })
    pd.DataFrame(bounds_rows).to_csv(data_dir / "produced_vs_bounds.csv", index=False)
