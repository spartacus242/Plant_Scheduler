# code/helpers/holding_builder.py — turn under-target demand into holding blocks.
#
# After a scenario solve, every demand-plan order that is under its qmin OR
# has zero scheduled qty should be available in the Gantt holding area so the
# planner can place it manually. This module builds those blocks from the
# solver's produced_vs_bounds.csv + demand_plan.csv (pure, no Streamlit).

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pandas as pd


@dataclass
class HoldingBlock:
    id: str
    order_id: str
    sku: str
    qty_kg: float
    run_hours: float
    line_name: str = ""
    line_id: int = 0
    # FRONTEND vocabulary ("sku", not "production"): this dataclass exists
    # solely to build Gantt payloads. A "production"-typed card dragged onto
    # a line dodged the pin button's block_type === "sku" gate and the KPI
    # counters' isProduction check (user report 2026-08-17: no pin option on
    # a holding-placed 280478-W35). The save path maps sku -> production.
    block_type: str = "sku"
    sku_description: str = ""
    start_hour: float = 0.0
    end_hour: float = 0.0
    reason: str = ""

    def to_payload(self) -> dict:
        """Gantt ScheduleBlock-shaped dict (holding cards need id/order_id/sku/run_hours)."""
        return {
            "id": self.id,
            "line_id": self.line_id,
            "line_name": self.line_name,
            "order_id": self.order_id,
            "sku": self.sku,
            "sku_description": self.sku_description,
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
            "run_hours": round(self.run_hours, 1),
            "block_type": self.block_type,
            "qty_kg": round(self.qty_kg, 1),
        }


def load_demand(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"sku": str})
    df["sku"] = df["sku"].astype(str).str.strip()
    return df


def load_produced(path: str | Path) -> pd.DataFrame:
    """produced_vs_bounds.csv — order_id, sku, qty_min, qty_max, produced."""
    df = pd.read_csv(path, dtype={"sku": str})
    df["order_id"] = df["order_id"].astype(str)
    return df


def build_holding(
    demand: pd.DataFrame,
    produced: pd.DataFrame,
    *,
    rates: dict[str, float] | None = None,
    sku_desc: dict[str, str] | None = None,
    committed_by_order: dict[str, float] | None = None,
) -> list[HoldingBlock]:
    """Build holding blocks for demand orders under qmin or with zero qty.

    Rules (user, 2026-08-10):
      * order is under-qmin  (produced < qty_min)  OR
      * order has zero scheduled qty (produced == 0 and qty_min > 0)
      -> one holding block per order, qty = qty_min − produced, run_hours
         derived from the average capable-line rate for that SKU.
    Current-state MO rows (order_id ending in '|CUR') are never holding.

    `committed_by_order` (coverage ledger, order_id -> covered kg) credits
    the plant's committed MOs + already-made kg alongside the solver's fill
    — without it every gross demand card sat in holding even when the MOs
    for that week were fully planned (the 280480-W34 screenshot,
    2026-08-16). No double count: fill targets were netted before solving,
    so fill + covered ≤ gross by construction.
    """
    produced_by = produced.set_index("order_id")["produced"].to_dict()
    min_by = produced.set_index("order_id")["qty_min"].to_dict()
    max_by = produced.set_index("order_id")["qty_max"].to_dict()

    blocks: list[HoldingBlock] = []
    seen: set[str] = set()
    for _, r in demand.iterrows():
        oid = str(r.get("order_id", ""))
        sku = str(r.get("sku", "")).strip()
        if not oid or not sku or oid.endswith("|CUR"):
            continue
        if oid in seen:
            continue
        seen.add(oid)
        qty_min = float(r.get("qty_target", 0) or 0)
        if pd.notna(r.get("lower_pct")) and pd.notna(r.get("qty_target")):
            qty_min = float(r["qty_target"]) * float(r["lower_pct"])
        elif pd.notna(r.get("qty_min")):
            qty_min = float(r["qty_min"])
        if qty_min <= 0:
            continue
        prod = float(produced_by.get(oid, 0.0) or 0.0)
        prod += float((committed_by_order or {}).get(oid, 0.0) or 0.0)
        if prod >= qty_min:
            continue  # met or exceeded — nothing to hold
        missing = qty_min - prod
        rate = (rates or {}).get(sku, 0.0)
        run_hours = missing / rate if rate and rate > 0 else 0.0
        blocks.append(HoldingBlock(
            id=f"hold_{oid}",
            order_id=oid,
            sku=sku,
            qty_kg=missing,
            run_hours=round(run_hours, 2),
            sku_description=(sku_desc or {}).get(sku, ""),
            reason="under_qmin" if prod > 0 else "zero_qty",
        ))
    return blocks


def average_rate_per_sku(capabilities: pd.DataFrame) -> dict[str, float]:
    """Mean calc_rate_kgph across capable lines, per SKU.

    "Capable" means capable == 1 AND a positive rate (INTEGRATE, agent FE
    handoff / ui-4): a capable row carrying a 0 rate used to pull the mean
    down while the client's meanCapableRate ignored it, so the server card
    and the client re-price disagreed on the first edit.
    """
    df = capabilities.copy()
    df["sku"] = df["sku"].astype(str).str.strip()
    df["capable"] = pd.to_numeric(df.get("capable", 0), errors="coerce").fillna(0)
    df["calc_rate_kgph"] = pd.to_numeric(
        df.get("calc_rate_kgph", 0), errors="coerce").fillna(0.0)
    ok = df[(df["capable"] == 1) & (df["calc_rate_kgph"] > 0)]
    return ok.groupby("sku")["calc_rate_kgph"].mean().to_dict()


def load_capabilities(path: str | Path) -> pd.DataFrame:
    """Capabilities with the measured-rate overlay (helpers.effective_rates)."""
    from helpers.effective_rates import load_effective_capabilities
    return load_effective_capabilities(path)
