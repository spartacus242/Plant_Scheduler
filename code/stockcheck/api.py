# code/stockcheck/api.py — single entry point for the UI.
#
# stock_check_report() assembles the full JSON-serializable report the
# Stock Check page renders. The page calls THIS and nothing else.
#
# Two layers share one BOM/stock read (contract 2026-09-01 §4):
#   * the FLAT check — every block vs the whole on-hand pile (status,
#     demand_view, item_reverse) — unchanged since the WW33 goldens;
#   * the SUPPLY TIMELINE — consumption netted across blocks on one curve
#     per item, open-PO receipts gated in as steps (stockcheck.timeline),
#     one Supply verdict per schedule_view row plus the additive keys
#     anchor / supply_meta / sku_needs / inbound / quality.

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import coverage as cov
from . import timeline as tl
from .bom import BomGraph
from .explode import (demand_requirements, load_rates, schedule_requirements)
from .po_import import load_open_pos
from .receiving_import import parse_receiving_schedule
from .vif_import import (VifSnapshot, import_vif_folder, load_latest_snapshot,
                         save_snapshot)

# Dock appointment sheet: the bridge copy first, else the bundled dev sheet
# (same names pages/stock_check.py and helpers/data_health.py use).
RECEIVING_SCHEDULE_NAME = "Shipping Receiving Schedule NPA - 2024.xlsm"
DEV_RECEIVING_SCHEDULE_NAME = "dev_receiving_schedule.xlsm"

# Fates the Inbound tab always shows a counter for, even at zero.
_JOIN_KEYS = ("used", "received", "overdue", "landed", "unjoinable",
              "unit_mismatch", "offsite_no_transfer")


def _f(x: float):
    """JSON-safe float (inf -> None, rounded)."""
    if x is None or (isinstance(x, float) and (math.isinf(x) or math.isnan(x))):
        return None
    return round(float(x), 3)


def _finite(x, default: float = 0.0) -> float:
    """Plain finite Python float (numpy scalars, NaN and junk -> default)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


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


# --------------------------------------------------------------------------
# supply-section input resolution (shared with helpers/stock_report_cache)
# --------------------------------------------------------------------------

def receiving_schedule_path(data_dir: str | Path) -> Path | None:
    """Bridge-refreshed dock sheet in data/reference, else the bundled dev
    sheet, else None (no appointments: every receipt reads tier 'erp')."""
    dd = Path(data_dir)
    for p in (dd / "reference" / RECEIVING_SCHEDULE_NAME,
              dd / "stockcheck" / DEV_RECEIVING_SCHEDULE_NAME):
        if p.is_file():
            return p
    return None


def supply_input_paths(data_dir: str | Path, cfg: dict | None = None) -> list[Path]:
    """Every file the supply section reads besides the VIF exports, the
    board, the demand plan and the rates file: the two bridge PO names, the
    configured [datasources] po_report_path override (when set), both dock
    sheets and flowstate.toml (rules + anchor). The report cache signs
    their mtimes — resolved HERE so the cache and the report never drift.
    Absent files are listed too: appearing must move the signature."""
    from helpers import config as hcfg
    from helpers import paths as hpaths
    dd = Path(data_dir)
    ref = dd / "reference"
    cfg = cfg if cfg is not None else hcfg.load_toml()
    out = [ref / "open_pos.xlsx", ref / "open_pos.csv"]
    override = str(hcfg.datasources_config(cfg).get("po_report_path", "") or "").strip()
    if override:
        out.append(Path(override))
    out += [ref / RECEIVING_SCHEDULE_NAME,
            dd / "stockcheck" / DEV_RECEIVING_SCHEDULE_NAME,
            hpaths.toml_path()]
    return out


# --------------------------------------------------------------------------
# supply-section helpers
# --------------------------------------------------------------------------

def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v != v:          # None / NaN
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _block_flags(locked_cell, attrs_cell) -> tuple[bool, bool]:
    """(locked, running) for a board row. attrs is ';'-joined tokens
    ('current_state:running;pct=45.1'). running = the ERP says the MO is on
    the line now. locked = the board's locked flag, a 'pinned' token or ANY
    current_state token (manprg-derived blocks are frozen by the ERP queue):
    a supply problem there reads "chase the PO", never "move the block"."""
    tokens: list[str] = []
    if attrs_cell is not None and attrs_cell == attrs_cell:
        tokens = [t.strip() for t in str(attrs_cell).split(";") if t.strip()]
    running = "current_state:running" in tokens
    locked = (_truthy(locked_cell) or "pinned" in tokens
              or any(t.startswith("current_state:") for t in tokens))
    return locked, running


def _attr_tokens(attrs_cell) -> list[str]:
    if attrs_cell is None or attrs_cell != attrs_cell:
        return []
    return [t.strip() for t in str(attrs_cell).split(";") if t.strip()]


def remaining_cases(cases: float, attrs_cell) -> float:
    """Cases a board row still has to make (audit stock-3).

    A running MO must draw material only for what is LEFT, and the board
    row is the source. Two generations of rows exist:
      * rows written by current_state after fix C04 carry qty_kg =
        remaining kg plus an `fct_kg=` token -> `cases` is already the
        remainder, returned unchanged;
      * older rows carry the MO's full Fct share and only `pct=` (the ERP
        completion %) -> scale by (1 - pct/100); every split piece of the
        MO carries the same pct, so the pieces still sum to the remainder.
    Anything not running (queued, solver, planner rows) is returned as is.
    The caller puts the result on the timeline block's `cases_left`, never
    on `cases`: _block_draw draws cases_left from the stock-count hour for
    a block that spans it, so the remainder is not pro-rated twice."""
    tokens = _attr_tokens(attrs_cell)
    if "current_state:running" not in tokens:
        return cases
    if any(t.startswith("fct_kg=") for t in tokens):
        return cases
    pct = None
    for t in tokens:
        if t.startswith("pct="):
            try:
                pct = float(t[4:])
            except ValueError:
                pct = None
            break
    if pct is None or not math.isfinite(pct):
        return cases
    return cases * max(0.0, min(1.0, 1.0 - pct / 100.0))


def _load_appts(path: Path) -> dict:
    """{po8: [{date, time, category}]} from the dock sheet; blank po8 rows
    cannot join a PO line and are dropped."""
    raw, _errs = parse_receiving_schedule(path)
    appts: dict = {}
    for a in raw:
        k = str(a.get("po8") or "").strip()
        if not k:
            continue
        appts.setdefault(k, []).append({"date": a.get("date"),
                                        "time": a.get("time") or "",
                                        "category": a.get("category") or ""})
    return appts


def stock_check_report(data_dir: str | Path, vif_folder: str | Path,
                       toggles: dict | None = None,
                       week_index: int | None = None, *,
                       po_path=None, receiving_path=None, today=None) -> dict:
    """Full report. Keyword extras feed the supply timeline only:
    po_path None -> reconcile_engine.open_po_path (False = no PO feed);
    receiving_path None -> receiving_schedule_path (False = skip the dock
    sheet); today defaults to date.today() (the feed's stale rule)."""
    data_dir = Path(data_dir)
    snap = refresh_vif_snapshot(vif_folder, data_dir)
    frames = snap.frames
    ediact = frames.get("ediact 3.csv")
    azapart = frames.get("azapart.csv")
    if ediact is None or azapart is None:
        return {"error": "ediact 3.csv / azapart.csv not importable",
                "import_errors": snap.errors,
                "source_files": snap.source_files}

    bom = BomGraph(ediact, frames.get("ediact 4.csv"))
    rm = frames.get("jestkexp.csv")
    pkg = frames.get("jestkexp2.csv")
    avail = cov.available_stock(rm, pkg, toggles)
    tracked_items = set(avail.keys())
    # items with stock rows entirely toggled off still count as tracked
    for df in (rm, pkg):
        if df is not None and not df.empty:
            tracked_items |= set(df["item"]) - {""}

    blocks = pd.read_csv(data_dir / "calendar_blocks.csv",
                         dtype={"sku": str})
    demand = pd.read_csv(data_dir / "reference" / "demand_plan.csv",
                         dtype={"sku": str})
    rates = load_rates(data_dir)

    # ---- schedule view
    # [stock] use_board_qty_kg (audit stock-2): the board's own kg is the
    # quantity every surface must grade; rate x hours only fills a blank.
    from helpers import config as _hcfg
    use_board = bool(_hcfg.stock_config(_hcfg.load_toml()).get("use_board_qty_kg", True))
    schedule_view = []
    sreqs = schedule_requirements(blocks, bom, azapart, rates,
                                  use_board_qty_kg=use_board)
    for b in sreqs:
        exp = b["explosion"]
        items = [cov.coverage_for_requirement(g, avail, tracked_items)
                 for g in exp.requirements]
        item_statuses = [i["status"] for i in items if i["need"]]
        status = cov.worst_status(item_statuses)
        schedule_view.append({
            "block_id": b["block_id"], "sku": b["sku"],
            "line_id": b["line_id"], "line_name": b["line_name"],
            "start_h": b["start_h"], "end_h": b["end_h"],
            "qty_kg": _f(b["qty_kg"]), "cases": _f(b["cases"]),
            "qty_source": b.get("qty_source", "rate"),
            "status": status if exp.status == "OK" else exp.status,
            "items": [_serialize_group_cov(i) for i in items],
            "unk": exp.unk_items, "cycles": exp.cycles,
        })

    # ---- demand view (explode ONCE — the item-reverse view below reads
    # the same requirements; a second explosion doubled the report cost)
    dreqs = demand_requirements(demand, bom, azapart)
    demand_view = []
    for d in dreqs:
        if week_index is not None and d["week_index"] != week_index:
            continue
        exp = d["explosion"]
        items = [cov.coverage_for_requirement(g, avail, tracked_items)
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
    for d in dreqs:
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

    supply = _supply_section(
        data_dir, snap, bom, frames, avail, tracked_items, azapart, blocks,
        sreqs, dreqs, schedule_view,
        po_path=po_path, receiving_path=receiving_path, today=today)

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
        **supply,
    }


# --------------------------------------------------------------------------
# §4 supply timeline section
# --------------------------------------------------------------------------

def _supply_section(data_dir: Path, snap: VifSnapshot, bom: BomGraph,
                    frames: dict, avail: dict, tracked_items: set,
                    azapart: pd.DataFrame, blocks: pd.DataFrame,
                    sreqs: list, dreqs: list, schedule_view: list, *,
                    po_path, receiving_path, today) -> dict:
    """Additive report keys (anchor, supply_meta, sku_needs, inbound,
    quality) + schedule_view[i].key / .supply. Mutates schedule_view rows
    in place; the flat statuses are never touched."""
    from helpers import config as hcfg
    from helpers.timefmt import datetime_to_hour, parse_datetime, planning_anchor

    cfg = hcfg.load_toml()
    anchor = planning_anchor(cfg)
    rules = hcfg.stock_config(cfg)
    # `now` feeds the gate's file-age rule: the wall clock in production, a
    # pinned `today` (00:00, or the datetime itself) in tests/probes.
    now = None
    if today is None:
        now = datetime.now()
        today = now.date()
    elif isinstance(today, datetime):
        now = today
    rm = frames.get("jestkexp.csv")
    pkg = frames.get("jestkexp2.csv")

    # -- snapshot hour per stock frame: an item's on-hand is as old as the
    # export it came from (rm first). The gate's overdue rule uses the
    # EARLIER date — a receipt older than the youngest count could still be
    # missing from the other frame.
    stamps = {"rm": snap.source_files.get("jestkexp.csv", ""),
              "pkg": snap.source_files.get("jestkexp2.csv", "")}
    frame_h: dict = {}
    frame_dates = []
    for k, s in stamps.items():
        dt = parse_datetime(s) if s else None
        if dt is not None:
            frame_h[k] = float(datetime_to_hour(dt, anchor))
            frame_dates.append(dt.date())
    snapshot_date = min(frame_dates) if frame_dates else None

    # -- unit recipe per SKU (board + demand), memoised: explosion is linear
    # in cases, so one explode(sku, 1.0) serves every block of that SKU and
    # the client's drag previews alike. NO_BOM SKUs are left out -> the
    # engine grades their blocks NO_DATA ("no recipe").
    kpc = dict(zip(azapart["sku"], azapart["kg_per_case"]))
    sku_needs: dict = {}
    designations: dict = {}
    seen: set = set()

    def unit_recipe(sku: str) -> None:
        if sku in seen:
            return
        seen.add(sku)
        exp = bom.explode(sku, 1.0)
        if exp.status == "NO_BOM":
            return
        items = []
        for g in exp.requirements:
            alts = [str(a["item"]) for a in g.alternates]
            items.append({"item": str(g.primary_item),
                          "per_case": _finite(g.need_qty),
                          "unit": str(g.unit or ""), "alts": alts})
            designations.setdefault(str(g.primary_item), str(g.designation or ""))
            for a in g.alternates:
                designations.setdefault(str(a["item"]), str(a.get("designation") or ""))
        sku_needs[str(sku)] = {"kg_per_case": _finite(kpc.get(sku)),
                               "items": items}

    for b in sreqs:
        unit_recipe(str(b["sku"]))
    for d in dreqs:
        unit_recipe(str(d["sku"]))

    recipe_items: set = set()
    for need in sku_needs.values():
        for ent in need["items"]:
            recipe_items.add(ent["item"])
            recipe_items.update(ent["alts"])

    # -- units: BOM Input rows first (what the recipe consumes in), the
    # stock frames as fallback; designations likewise for the payload.
    unit_by_item: dict = {}
    bom_items: set = set()
    for df in (bom.df, frames.get("ediact 4.csv")):
        if df is None or df.empty or "item_type" not in df.columns:
            continue
        ins = df[df["item_type"] == "Input"]
        for item, unit in zip(ins["item"], ins["unit"]):
            item = str(item).strip()
            if not item:
                continue
            bom_items.add(item)
            if unit and item not in unit_by_item:
                unit_by_item[item] = str(unit).strip()
    stock_lots: dict = {}
    rm_items: set = set()
    for df, is_rm in ((rm, True), (pkg, False)):
        if df is None or df.empty:
            continue
        for item, unit, desig, batch, q in zip(df["item"], df["unit"],
                                               df["designation"], df["batch"],
                                               df["qty"]):
            item = str(item).strip()
            if not item:
                continue
            if is_rm:
                rm_items.add(item)
            if unit and item not in unit_by_item:
                unit_by_item[item] = str(unit).strip()
            if desig and item not in designations:
                designations[item] = str(desig).strip()
            # ALL lots regardless of toggles: the landed rule asks whether the
            # truck is physically here, not whether QC released it. Raw qty
            # (NaN as blank): the gate counts None/blank as 0 quietly and
            # NOTES garbage — pre-coercing here swallowed that diagnostic.
            stock_lots.setdefault(item, []).append(
                (batch, None if isinstance(q, float) and math.isnan(q) else q))
    rmpk = frames.get("rmpkitems.csv")
    if rmpk is not None and not rmpk.empty:
        for item, desig in zip(rmpk["item"], rmpk["designation"]):
            if item and desig:
                designations.setdefault(str(item), str(desig))
    bom_items |= tracked_items | recipe_items

    # -- open-PO feed
    inbound: dict = {"state": "missing", "source_path": "", "source_mtime": "",
                     "as_of": None, "max_receipt_date": None, "n_rows": 0,
                     "errors": [], "lines": [], "receipts": {}, "join": {},
                     "appt_join": {"matched": 0, "total": 0},
                     "receiving_path": ""}
    po_lines = None
    p = None
    if po_path is None:
        from helpers.reconcile_engine import open_po_path
        p = open_po_path(data_dir, cfg)
    elif po_path is not False:
        p = Path(po_path)
    if p is not None:
        inbound["source_path"] = str(p)
        if p.is_file():
            res = load_open_pos(p)
            po_lines = res.lines
            inbound.update(source_mtime=res.source_mtime, as_of=res.as_of,
                           max_receipt_date=res.max_receipt_date,
                           n_rows=res.n_rows, errors=list(res.errors))
        else:
            # a configured override that is absent (or a folder) is reported,
            # never silently replaced by the bridge copy (open_po_path contract)
            inbound["errors"].append(
                f"PO path is a directory, not a file: {p}" if p.is_dir()
                else f"PO file not found: {p}")

    # -- dock appointments (only worth the xlsm parse when there are lines
    # to join; the report is cached so the seconds are paid once)
    appts: dict = {}
    rp = None
    if receiving_path is None:
        rp = receiving_schedule_path(data_dir)
    elif receiving_path is not False:
        rp = Path(receiving_path)
    if rp is not None and po_lines:
        inbound["receiving_path"] = str(rp)
        try:
            appts = _load_appts(rp)
        except Exception as exc:  # noqa: BLE001 — the sheet is decaying
            inbound["errors"].append(f"receiving schedule {rp.name}: {exc}")

    receipts, fates, feed = tl.gate_receipts(
        po_lines, bom_items=bom_items, unit_by_item=unit_by_item,
        snapshot_date=snapshot_date, today=today, anchor=anchor, rules=rules,
        stock_lots=stock_lots, appts=appts or None,
        source_mtime=inbound.get("source_mtime") or None,
        as_of=inbound.get("as_of"), now=now)
    feed_state = "missing" if po_lines is None else str(feed["state"])
    # Window end is the gate's alone: it already takes max(latest counted
    # ready_h, 23:59 of the file's last receipt date) over the same lines
    # load_open_pos saw, so a fresh feed whose lines are all excluded still
    # vouches for "nothing else lands before then" (plan §2 NO DATA).
    window_end = _finite(feed.get("receipts_window_end_h"))
    # The gate's own notes (bad rows, malformed lots) join the file's import
    # errors: the Inbound tab shows ONE problem list.
    for e in feed.get("errors") or []:
        if str(e) not in inbound["errors"]:
            inbound["errors"].append(str(e))
    join = {k: 0 for k in _JOIN_KEYS}
    for k, n in (feed.get("fates") or {}).items():
        join[str(k)] = join.get(str(k), 0) + int(n)
    inbound.update(
        state=feed_state, lines=fates, receipts=receipts, join=join,
        appt_join={"matched": sum(1 for f in fates if f.get("tier") == "appt"),
                   "total": int(feed.get("n_used") or 0)},
        # which freshness rule fired (content / file_age), the extract's
        # age and what it was measured from (as_of cell or file mtime)
        stale_reason=list(feed.get("stale_reason") or []),
        source_age_h=_f(feed.get("source_age_h")),
        age_basis=feed.get("age_basis"))
    quality = {
        "unjoinable_items": sorted({str(f["item"]) for f in fates
                                    if f["fate"] == "unjoinable"}),
        "unit_mismatch": sorted({str(f.get("item_key") or f["item"]) for f in fates
                                 if f["fate"] == "unit_mismatch"}),
        "landed_unverifiable": sorted({str(f.get("item_key") or f["item"])
                                       for f in fates
                                       if f["fate"] == "landed_unverifiable"}),
    }

    # -- timeline blocks: one per schedule_view row, same order as
    # schedule_requirements (both walk the production rows in board order).
    prods = blocks[blocks["block_type"] == "production"]
    locked_col = prods["locked"].tolist() if "locked" in prods.columns else [None] * len(prods)
    attrs_col = prods["attrs"].tolist() if "attrs" in prods.columns else [None] * len(prods)
    tl_blocks = []
    for b, row, lk, at in zip(sreqs, schedule_view, locked_col, attrs_col):
        locked, running = _block_flags(lk, at)
        key = tl.block_key({"block_id": b["block_id"], "start_h": b["start_h"]})
        row["key"] = key
        cases = _finite(b["cases"])
        # 0 = unrated line/SKU or no kg_per_case: unknown quantity reads
        # NO_DATA, never a green chip (plan §8).
        known = cases > 0
        # Audit stock-3: the report path hard-coded cases_left=None, so a
        # running MO was charged the uniform TIME tail of its board quantity
        # (wrong whenever the re-forecast changed the rate). The remainder
        # rides on cases_left — the engine (_block_draw) draws exactly that
        # from the stock-count hour S for a block that spans S, and keeps the
        # row's own cases for a piece starting after S (the count predates
        # it, so all of its material is still in the pile). Putting the
        # remainder into `cases` instead would let the uniform tail pro-rate
        # it a second time.
        cases_left = remaining_cases(cases, at) if (known and running) else None
        tl_blocks.append({
            "key": key, "block_id": str(b["block_id"]), "sku": str(b["sku"]),
            "line_name": str(b["line_name"]),
            "start_h": float(b["start_h"]), "end_h": float(b["end_h"]),
            "cases": cases if known else None,
            "locked": locked, "running": running, "cases_left": cases_left,
        })

    tracked = sorted(recipe_items & tracked_items)
    opening = {i: _finite(avail.get(i, 0.0)) for i in tracked}
    snapshot_h: dict = {}
    for i in tracked:
        h = frame_h.get("rm" if i in rm_items else "pkg")
        if h is None:
            h = frame_h.get("pkg" if i in rm_items else "rm")
        if h is not None:
            snapshot_h[i] = h
    in_house = sorted(cov.IN_HOUSE_ITEMS)
    timelines = tl.build_timelines(tl_blocks, sku_needs, opening, receipts,
                                   snapshot_h, tracked=tracked, in_house=in_house)
    board = tl.evaluate_board(tl_blocks, timelines, rules, feed_state, window_end)
    for row in schedule_view:
        sup = board[row["key"]]
        # real stamps for the pages/reconcile; the engine's own 'h95.3' form
        # is only for the fixture parity
        sup["text"] = tl.verdict_text(sup, anchor)
        row["supply"] = sup

    recipe_sorted = sorted(recipe_items)
    supply_meta = {
        "rules": rules,
        "snapshot_h": snapshot_h,
        "snapshot_stamp": stamps,
        "receipts_window_end_h": window_end,
        "feed_state": feed_state,
        "opening": opening,
        "tracked": tracked,
        "in_house": in_house,
        "units": {i: unit_by_item[i] for i in recipe_sorted if unit_by_item.get(i)},
        "designations": {i: designations[i] for i in recipe_sorted
                         if designations.get(i)},
    }
    return {
        "anchor": anchor.isoformat(sep=" ", timespec="seconds"),
        "supply_meta": supply_meta,
        "sku_needs": sku_needs,
        "inbound": inbound,
        "quality": quality,
    }
