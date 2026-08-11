# code/stockcheck/explode.py — requirements from the two demand signals.
#
# Schedule signal: production blocks in data/calendar_blocks.csv (qty_kg is
#   NULL there; qty_kg = hours * calc_rate_kgph for (line_id, sku)).
# Demand signal: data/reference/demand_plan.csv (qty_target is kg).

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .bom import BomGraph, ExplosionResult


def _kg_per_case(azapart: pd.DataFrame) -> dict[str, float]:
    return dict(zip(azapart["sku"], azapart["kg_per_case"]))


def load_rates(data_dir: str | Path) -> pd.DataFrame:
    p = Path(data_dir) / "reference" / "capabilities_rates.csv"
    df = pd.read_csv(p, dtype={"sku": str})
    if "calc_rate_kgph" not in df.columns and "rate_kgph" in df.columns:
        df = df.rename(columns={"rate_kgph": "calc_rate_kgph"})
    return df


def block_qty_kg(block: pd.Series, rates: pd.DataFrame) -> float:
    """hours * calc_rate_kgph for (line_id, sku); 0 if unrated."""
    m = rates[(rates["line_id"] == int(block["line_id"]))
              & (rates["sku"] == str(block["sku"]))]
    if m.empty:
        return 0.0
    hours = float(block["end_h"]) - float(block["start_h"])
    return hours * float(m.iloc[0]["calc_rate_kgph"])


def schedule_requirements(blocks: pd.DataFrame, bom: BomGraph,
                          azapart: pd.DataFrame,
                          rates: pd.DataFrame) -> list[dict]:
    """Explode every production block. Returns one dict per block with the
    ExplosionResult attached."""
    kpc = _kg_per_case(azapart)
    out = []
    prods = blocks[blocks["block_type"] == "production"]
    for _, b in prods.iterrows():
        sku = str(b["sku"])
        kg = block_qty_kg(b, rates)
        per_case = kpc.get(sku) or 0.0
        cases = (kg / per_case) if per_case > 0 else 0.0
        exp = bom.explode(sku, cases)
        out.append({
            "block_id": b["block_id"], "sku": sku,
            "line_id": int(b["line_id"]), "line_name": b["line_name"],
            "start_h": float(b["start_h"]), "end_h": float(b["end_h"]),
            "qty_kg": kg, "cases": cases, "explosion": exp,
        })
    return out


def demand_requirements(demand: pd.DataFrame, bom: BomGraph,
                        azapart: pd.DataFrame) -> list[dict]:
    kpc = _kg_per_case(azapart)
    out = []
    for _, d in demand.iterrows():
        sku = str(d["sku"])
        kg = float(d["qty_target"])
        per_case = kpc.get(sku) or 0.0
        cases = (kg / per_case) if per_case > 0 else 0.0
        exp = bom.explode(sku, cases)
        out.append({
            "order_id": d["order_id"], "sku": sku,
            "week_index": int(d["week_index"]),
            "target_kg": kg, "cases": cases, "explosion": exp,
        })
    return out
