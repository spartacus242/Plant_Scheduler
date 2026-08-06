# code/stockcheck/api.py — single entry point for the UI.
#
# stock_check_report() assembles the full JSON-serializable report the
# Stock Check page renders. The page calls THIS and nothing else.

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from . import coverage as cov
from .bom import BomGraph
from .explode import (demand_requirements, load_rates, schedule_requirements)
from .vif_import import (VifSnapshot, import_vif_folder, load_latest_snapshot,
                         save_snapshot)


def _f(x: float):
    """JSON-safe float (inf -> None, rounded)."""
    if x is None or (isinstance(x, float) and (math.isinf(x) or math.isnan(x))):
        return None
    return round(float(x), 3)


def refresh_vif_snapshot(vif_folder: str | Path, data_dir: str | Path
                         ) -> VifSnapshot:
    """Re-import only if any source mtime changed; keep last-good on failure."""
    snapshots_dir = Path(data_dir) / "stockcheck" / "snapshots"
    prev = load_latest_snapshot(snapshots_dir)
    folder = Path(vif_folder)
    current_mtimes = {}
    for name in ("ediact 3.csv", "ediact 4.csv", "jestkexp.csv",
                 "jestkexp2.csv", "azapart.csv", "rmpkitems.csv"):
        p = folder / name
        if p.exists():
            import time
            current_mtimes[name] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
    if prev is not None and prev.source_files == current_mtimes:
        return prev
    snap = import_vif_folder(folder)
    if snap.frames:
        save_snapshot(snap, snapshots_dir)
        return snap
    if prev is not None:
        return prev
    return snap  # nothing else to give; errors are in snap.errors


def _serialize_group_cov(c: dict) -> dict:
    return {**c, "need": _f(c["need"]), "available_primary": _f(c["available_primary"]),
            "available_total": _f(c["available_total"]), "ratio": _f(c["ratio"]),
            "alternates": [{**a, "available": _f(a["available"])}
                           for a in c["alternates"]]}


def stock_check_report(data_dir: str | Path, vif_folder: str | Path,
                       toggles: dict | None = None,
                       week_index: int | None = None) -> dict:
    data_dir = Path(data_dir)
    snap = refresh_vif_snapshot(vif_folder, data_dir)
    frames = snap.frames
    ediact = frames.get("ediact 3.csv")
    azapart = frames.get("azapart.csv")
    if ediact is None or azapart is None:
        return {"error": "ediact 3.csv / azapart.csv not importable",
                "import_errors": snap.errors,
                "source_files": snap.source_files}

    bom = BomGraph(ediact)
    rm = frames.get("jestkexp.csv")
    pkg = frames.get("jestkexp2.csv")
    avail = cov.available_stock(rm, pkg, toggles)

    blocks = pd.read_csv(data_dir / "calendar_blocks.csv",
                         dtype={"sku": str})
    demand = pd.read_csv(data_dir / "reference" / "demand_plan.csv",
                         dtype={"sku": str})
    rates = load_rates(data_dir)

    # ---- schedule view
    schedule_view = []
    for b in schedule_requirements(blocks, bom, azapart, rates):
        exp = b["explosion"]
        items = [cov.coverage_for_requirement(g, avail)
                 for g in exp.requirements]
        item_statuses = [i["status"] for i in items if i["need"]]
        status = cov.worst_status(item_statuses)
        schedule_view.append({
            "block_id": b["block_id"], "sku": b["sku"],
            "line_id": b["line_id"], "line_name": b["line_name"],
            "start_h": b["start_h"], "end_h": b["end_h"],
            "qty_kg": _f(b["qty_kg"]), "cases": _f(b["cases"]),
            "status": status if exp.status == "OK" else exp.status,
            "items": [_serialize_group_cov(i) for i in items],
            "unk": exp.unk_items, "cycles": exp.cycles,
        })

    # ---- demand view
    demand_view = []
    for d in demand_requirements(demand, bom, azapart):
        if week_index is not None and d["week_index"] != week_index:
            continue
        exp = d["explosion"]
        items = [cov.coverage_for_requirement(g, avail)
                 for g in exp.requirements]
        ratios = [i["ratio"] for i in items
                  if i["need"] and not math.isinf(i["ratio"])]
        achievable = min(ratios) if ratios else math.inf
        if exp.status == "NO_BOM":
            status = "NO_BOM"
        elif exp.status == "UNK_PARTIAL":
            status = "UNK"
        elif not math.isinf(achievable) and achievable < cov.DNS_RATIO:
            status = "DO_NOT_SCHEDULE"
        else:
            status = cov.worst_status([i["status"] for i in items])
        constraining = sorted(
            (i for i in items if i["need"]),
            key=lambda i: (math.inf if math.isinf(i["ratio"]) else i["ratio"]))[:5]
        demand_view.append({
            "order_id": d["order_id"], "sku": d["sku"],
            "week_index": d["week_index"],
            "target_kg": _f(d["target_kg"]), "cases": _f(d["cases"]),
            "achievable_ratio": _f(achievable), "status": status,
            "constraining": [_serialize_group_cov(i) for i in constraining],
            "unk": exp.unk_items,
        })

    # ---- item reverse view: item -> consuming skus (from demand universe)
    item_reverse: dict[str, list[dict]] = {}
    for d in demand_requirements(demand, bom, azapart):
        for g in d["explosion"].requirements:
            entry = {"sku": d["sku"], "week_index": d["week_index"],
                     "need": _f(g.need_qty), "unit": g.unit}
            item_reverse.setdefault(g.primary_item, []).append(entry)
            for a in g.alternates:
                item_reverse.setdefault(a["item"], []).append(
                    {**entry, "as_alternate_of": g.primary_item})

    no_bom = sorted({d["sku"] for d in demand_view if d["status"] == "NO_BOM"}
                    | {b["sku"] for b in schedule_view
                       if b["status"] == "NO_BOM"})
    unk = [{"sku": b["sku"], **u} for b in schedule_view for u in b["unk"]]
    unk += [{"sku": d["sku"], **u} for d in demand_view for u in d["unk"]]

    return {
        "generated_at": snap.imported_at,
        "source_files": snap.source_files,
        "import_errors": snap.errors,
        "availability_toggles": toggles or cov.default_toggles(),
        "schedule_view": schedule_view,
        "demand_view": demand_view,
        "item_reverse": item_reverse,
        "no_bom_skus": no_bom,
        "unk": unk,
    }
