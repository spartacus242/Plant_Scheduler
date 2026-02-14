# inventory_checker.py — Inventory validation for Flowstate scheduler.
# Checks on-hand + inbound vs. material consumption from schedule.
# PLAN: sufficient inventory; FLAG: insufficient and no inbound before stockout.

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd


PLANNING_ANCHOR = "2026-02-15 00:00:00"


@dataclass
class InventoryCheckResult:
    """Result of inventory validation for one order."""
    order_id: str
    sku: str
    produced: int
    start_hour: int
    status: str  # "PLAN" | "FLAG"
    shortfall_materials: List[str] = field(default_factory=list)
    shortfall_qty: Dict[str, float] = field(default_factory=dict)
    message: str = ""


def load_bom(data_dir: Path) -> Dict[str, Dict[str, float]]:
    """Load BOM_by_SKU.csv: {sku: {material_id: qty_per_unit}}."""
    path = data_dir / "BOM_by_SKU.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["sku"] = df["sku"].astype(str)
    df["material_id"] = df["material_id"].astype(str)
    df["qty_per_unit"] = pd.to_numeric(df.get("qty_per_unit", 1), errors="coerce").fillna(1.0)
    bom: Dict[str, Dict[str, float]] = {}
    for _, r in df.iterrows():
        sku = str(r["sku"])
        mat = str(r["material_id"])
        qty = float(r["qty_per_unit"])
        bom.setdefault(sku, {})[mat] = bom.get(sku, {}).get(mat, 0) + qty
    return bom


def load_on_hand(data_dir: Path) -> Dict[str, float]:
    """Load OnHand_Inventory.csv: {material_id: quantity}."""
    path = data_dir / "OnHand_Inventory.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["material_id"] = df["material_id"].astype(str)
    df["quantity"] = pd.to_numeric(df.get("quantity", 0), errors="coerce").fillna(0.0)
    return df.groupby("material_id", as_index=False)["quantity"].sum().set_index("material_id")["quantity"].to_dict()


def load_inbound(data_dir: Path, anchor_hour: int = 0) -> List[tuple]:
    """Load Inbound_Inventory.csv. Returns list of (material_id, quantity, arrival_hour)."""
    path = data_dir / "Inbound_Inventory.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    df["material_id"] = df["material_id"].astype(str)
    df["quantity"] = pd.to_numeric(df.get("quantity", 0), errors="coerce").fillna(0.0)
    # arrival_hour or arrival_date -> hour offset from anchor
    if "arrival_hour" in df.columns:
        df["arrival_hour"] = pd.to_numeric(df["arrival_hour"], errors="coerce").fillna(0).astype(int)
    elif "arrival_date" in df.columns:
        anchor = pd.Timestamp(PLANNING_ANCHOR)
        df["arrival_date"] = pd.to_datetime(df["arrival_date"], errors="coerce")
        df["arrival_hour"] = ((df["arrival_date"] - anchor).dt.total_seconds() / 3600).fillna(0).astype(int)
    else:
        df["arrival_hour"] = 0
    out = []
    for _, r in df.iterrows():
        out.append((str(r["material_id"]), float(r["quantity"]), int(r["arrival_hour"]) + anchor_hour))
    return out


def load_schedule_produced(
    data_dir: Path,
    schedule_path: Optional[Path] = None,
    produced_path: Optional[Path] = None,
) -> pd.DataFrame:
    """Build order-level consumption timing: order_id, sku, produced, start_hour (earliest)."""
    sched = schedule_path or data_dir / "schedule_phase2.csv"
    prod = produced_path or data_dir / "produced_vs_bounds.csv"
    if not sched.exists() or not prod.exists():
        return pd.DataFrame()

    sdf = pd.read_csv(sched)
    pdf = pd.read_csv(prod)
    # Earliest start per order
    order_starts = sdf.groupby("order_id").agg({"start_hour": "min", "sku": "first"}).reset_index()
    merged = pdf.merge(order_starts, on="order_id", how="inner", suffixes=("", "_dup"))
    merged = merged[["order_id", "sku", "produced", "start_hour"]].drop_duplicates()
    merged = merged.sort_values("start_hour")
    return merged


def run_inventory_check(
    data_dir: Path,
    schedule_path: Optional[Path] = None,
    produced_path: Optional[Path] = None,
) -> List[InventoryCheckResult]:
    """
    Run inventory validation. For each order:
    - Compute material consumption from BOM × produced qty
    - Simulate inventory: on-hand + inbound arrivals vs consumption over time
    - PLAN if sufficient; FLAG if insufficient and no inbound arrives before stockout
    """
    bom = load_bom(data_dir)
    on_hand = load_on_hand(data_dir)
    inbound_list = load_inbound(data_dir)
    orders_df = load_schedule_produced(data_dir, schedule_path, produced_path)

    if orders_df.empty:
        return []

    orders_df["order_id"] = orders_df["order_id"].astype(str)
    orders_df["sku"] = orders_df["sku"].astype(str)

    # Build events: (hour, type, material, qty, order_id, sku)
    # type: 'inbound' or 'consume'
    events: List[tuple] = []
    for mat, qty, hour in inbound_list:
        events.append((max(0, hour), "inbound", mat, qty, None, None))
    for _, row in orders_df.iterrows():
        sku = str(row["sku"])
        produced = int(row["produced"])
        start_hour = int(row["start_hour"])
        order_id = str(row["order_id"])
        if sku not in bom:
            continue
        for mat, qty_per in bom[sku].items():
            consumed = produced * qty_per
            if consumed > 0:
                events.append((start_hour, "consume", mat, consumed, order_id, sku))
    events.sort(key=lambda x: (x[0], 0 if x[1] == "inbound" else 1, x[2]))  # inbound before consume at same hour

    # Initialize balance
    all_mats = set(on_hand) | {e[2] for e in events}
    balance: Dict[str, float] = {m: float(on_hand.get(m, 0)) for m in all_mats}

    # Order results: (order_id, sku) -> result
    order_results: Dict[tuple, InventoryCheckResult] = {}
    # Pending results for orders we haven't finalized
    pending: Dict[tuple, dict] = {}

    for hour, etype, mat, qty, order_id, sku in events:
        if etype == "inbound":
            balance[mat] = balance.get(mat, 0) + qty
            continue
        # consume
        key = (order_id, sku)
        available = balance.get(mat, 0)
        shortfall = max(0, qty - available)
        balance[mat] = max(0, available - qty)

        if key not in pending:
            row = orders_df[(orders_df["order_id"] == order_id) & (orders_df["sku"] == sku)].iloc[0]
            pending[key] = {
                "order_id": order_id,
                "sku": sku,
                "produced": int(row["produced"]),
                "start_hour": int(row["start_hour"]),
                "shortfall_materials": [],
                "shortfall_qty": {},
            }
        if shortfall > 0:
            pending[key]["shortfall_materials"].append(mat)
            pending[key]["shortfall_qty"][mat] = shortfall

    # Add orders with no BOM
    for _, row in orders_df.iterrows():
        key = (str(row["order_id"]), str(row["sku"]))
        if key not in pending and str(row["sku"]) not in bom:
            order_results[key] = InventoryCheckResult(
                order_id=str(row["order_id"]),
                sku=str(row["sku"]),
                produced=int(row["produced"]),
                start_hour=int(row["start_hour"]),
                status="PLAN",
                message="No BOM defined; material check skipped",
            )

    for key, p in pending.items():
        flag = bool(p["shortfall_materials"])
        status = "FLAG" if flag else "PLAN"
        msg = (
            "Insufficient material(s): " + ", ".join(
                f"{m} short {p['shortfall_qty'][m]:.0f}" for m in p["shortfall_materials"]
            )
        ) if flag else "Sufficient inventory"
        order_results[key] = InventoryCheckResult(
            order_id=p["order_id"],
            sku=p["sku"],
            produced=p["produced"],
            start_hour=p["start_hour"],
            status=status,
            shortfall_materials=p["shortfall_materials"],
            shortfall_qty=p["shortfall_qty"],
            message=msg,
        )

    return list(order_results.values())


def results_to_dataframe(results: List[InventoryCheckResult]) -> pd.DataFrame:
    """Convert check results to DataFrame for display."""
    rows = []
    for r in results:
        rows.append({
            "order_id": r.order_id,
            "sku": r.sku,
            "produced": r.produced,
            "start_hour": r.start_hour,
            "status": r.status,
            "shortfall_materials": ", ".join(r.shortfall_materials) if r.shortfall_materials else "",
            "shortfall_detail": ", ".join(f"{m}: {r.shortfall_qty[m]:.0f}" for m in r.shortfall_materials) if r.shortfall_materials else "",
            "message": r.message,
        })
    return pd.DataFrame(rows)
