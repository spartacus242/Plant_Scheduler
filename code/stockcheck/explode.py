# code/stockcheck/explode.py — requirements from the two demand signals.
#
# Schedule signal: production blocks in data/calendar_blocks.csv. The board
#   row's own qty_kg is the quantity the plan says will be made (solver rows
#   and current_state rows all carry one); rate x hours from
#   capabilities_rates is only the fallback for a row without it — the same
#   rule the Gantt applies (supplyGlue.toTimelineBlock), so server and client
#   grade one quantity ([stock] use_board_qty_kg, audit stock-2).
# Demand signal: data/reference/demand_plan.csv (qty_target is kg).

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from .bom import BomGraph, ExplosionResult


def _kg_per_case(azapart: pd.DataFrame) -> dict[str, float]:
    return dict(zip(azapart["sku"], azapart["kg_per_case"]))


def load_rates(data_dir: str | Path) -> pd.DataFrame:
    p = Path(data_dir) / "reference" / "capabilities_rates.csv"
    from helpers.effective_rates import load_effective_capabilities
    return load_effective_capabilities(p, dd=data_dir)


def block_qty_kg(block: pd.Series, rates: pd.DataFrame) -> float:
    """hours * calc_rate_kgph for (line_id, sku); 0 if unrated."""
    m = rates[(rates["line_id"] == int(block["line_id"]))
              & (rates["sku"] == str(block["sku"]))]
    if m.empty:
        return 0.0
    hours = float(block["end_h"]) - float(block["start_h"])
    return hours * float(m.iloc[0]["calc_rate_kgph"])


def board_qty_kg(block: pd.Series) -> float | None:
    """The board row's own qty_kg as a positive finite float, else None
    (blank, NaN, 0 and garbage all read as 'unknown kg' — a 0 on the board
    is indistinguishable from unknown, calendar_io precedent)."""
    v = block.get("qty_kg") if hasattr(block, "get") else None
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def schedule_requirements(blocks: pd.DataFrame, bom: BomGraph,
                          azapart: pd.DataFrame,
                          rates: pd.DataFrame, *,
                          use_board_qty_kg: bool = True) -> list[dict]:
    """Explode every production block. Returns one dict per block with the
    ExplosionResult attached.

    Quantity: the board row's qty_kg when `use_board_qty_kg` and the row
    carries one, else rate x hours (`qty_source` says which). The default
    mirrors [stock] use_board_qty_kg = true; the caller passes the
    configured value."""
    kpc = _kg_per_case(azapart)
    out = []
    prods = blocks[blocks["block_type"] == "production"]
    for _, b in prods.iterrows():
        sku = str(b["sku"])
        kg_board = board_qty_kg(b) if use_board_qty_kg else None
        if kg_board is not None:
            kg, src = kg_board, "board"
        else:
            kg, src = block_qty_kg(b, rates), "rate"
        per_case = kpc.get(sku) or 0.0
        cases = (kg / per_case) if per_case > 0 else 0.0
        exp = bom.explode(sku, cases)
        out.append({
            "block_id": b["block_id"], "sku": sku,
            "line_id": int(b["line_id"]), "line_name": b["line_name"],
            "start_h": float(b["start_h"]), "end_h": float(b["end_h"]),
            "qty_kg": kg, "cases": cases, "explosion": exp,
            "qty_source": src,
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
