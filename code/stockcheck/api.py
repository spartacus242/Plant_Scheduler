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
from . import vif_import as _vi
from .vif_import import (VifSnapshot, import_vif_folder, load_latest_snapshot,
                         save_snapshot)

# --------------------------------------------------------------------------
# VIF loader contract (drop of 2026-09-15) — resolved through getattr so this
# module reads the new lot exports as soon as vif_import ships lot_frames /
# receipts_frame / VIF_FILES for them, and keeps working on the two legacy
# lot files (jestkexp / jestkexp2) with an older loader or a pickled
# snapshot from before the drop.
# --------------------------------------------------------------------------

# Lot frames in contract order: raw materials, packaging, off-site AMB,
# quality control, fresh apples, semi-finished. rm FIRST so an item present
# in several exports keeps the rm frame's snapshot hour (the old rule).
_LOT_FRAME_ORDER = ("jestkexp.csv", "jestkexp2.csv", "jestkamb.csv",
                    "jestkexq.csv", "jestksav.csv", "jestkexp5.csv")
# short tag per lot file for supply_meta.snapshot_stamp / snapshot_h ("rm"
# and "pkg" are the pre-drop keys every consumer already reads)
_FRAME_TAGS = {"jestkexp.csv": "rm", "jestkexp2.csv": "pkg",
               "jestkamb.csv": "amb", "jestkexq.csv": "qc",
               "jestksav.csv": "apples", "jestkexp5.csv": "semi"}
_VIF_FILES_FALLBACK = ("ediact.csv", "ediact 3.csv", "ediact 4.csv",
                       "jestkexp.csv", "jestkexp2.csv", "jestkamb.csv",
                       "jestkexq.csv", "jestksav.csv", "jestksa.csv",
                       "jestkexp5.csv", "jestkexp4.csv", "PKG-REC.csv",
                       "azapart.csv", "rmpkitems.csv")


def _vif_file_names() -> tuple:
    """vif_import.VIF_FILES (the loader owns the list) unioned with the
    names the drop introduced, so the mtime guard re-imports when a new
    export lands even before the loader lists it."""
    names = [str(n) for n in (getattr(_vi, "VIF_FILES", None) or ())]
    for n in _VIF_FILES_FALLBACK:
        if n not in names:
            names.append(n)
    return tuple(names)


def _lot_frames(snap) -> list:
    """[(file name, frame)] for every lot export in the snapshot, rm first:
    vif_import.lot_frames when the loader has it, else the frames dict
    walked in contract order (the two legacy files on an old snapshot)."""
    fn = getattr(_vi, "lot_frames", None)
    if fn is not None:
        return [(str(n), df) for n, df in fn(snap) if df is not None]
    frames = getattr(snap, "frames", None) or {}
    return [(n, frames[n]) for n in _LOT_FRAME_ORDER
            if frames.get(n) is not None]


def _receipts_frame(snap):
    """The packaging receipt-slip frame (PKG-REC.csv) or None."""
    fn = getattr(_vi, "receipts_frame", None)
    if fn is not None:
        return fn(snap)
    return (getattr(snap, "frames", None) or {}).get("PKG-REC.csv")


def _snapshot_notes(snap) -> list:
    """Import notes (empty exports, ignored duplicates, stale files): a
    snapshot pickled before the drop has no `notes` attribute."""
    return [str(n) for n in (getattr(snap, "notes", None) or [])]


def _frame_tag(name: str) -> str:
    return _FRAME_TAGS.get(name) or Path(name).stem


# (2026-09-16) The user rule of 2026-08-14 — a house-made HSM /
# semi-finished intermediate never gates a SKU on its own stock; the BOM
# explodes THROUGH it to its sub-components — applied to the drop's
# semi-finished lot export: jestkexp5.csv never feeds available_stock,
# tracked_items or the landed rule's stock_lots. With the recipes present
# the intermediate is no requirement anyway; with them missing ("ediact
# 4.csv" absent) it must read NOT_TRACKED, never AT_RISK on a few WIP lots
# — alternates included (BomGraph.explode flags the group no_recipe).
# Its lots still show in the lot-detail view (pages/stock_check._lot_rows).
_NEVER_COUNTED_FRAMES = frozenset({"jestkexp5.csv"})
# (2026-09-16) Fresh apples (jestksav.csv, SA1/SA2) arrive on daily trucks:
# a lot dated near a FUTURE apple PO line is another truck. The frame's
# lots go to the gate as daily_lots: they land only a line dated on/before
# the snapshot day, and only with lots of that same date — the five 730070
# lines of 2026-09-15 are in the 14:05 export (139,082 kg dated that day)
# and must not be counted again. The frame still counts on hand (avail /
# tracked) and shows in lot detail.
_DAILY_LOT_FRAMES = frozenset({"jestksav.csv"})
# (2026-09-16) The gate's snapshot date (overdue rule) comes from the raw
# material and packaging exports only — the pre-drop rule. A small side
# export (jestkexp5 / jestkexq / jestkamb / jestksav) that kept an old mtime
# must not pull the date back: that loosened the overdue rule and counted
# receipts already in on-hand a second time.
_GATE_SNAPSHOT_TAGS = ("rm", "pkg")
# alias -> canonical, mirrored from vif_import.LOT_FALLBACKS for a loader
# that predates it (the loader's own dict wins)
_LOT_FALLBACKS = {"jestkexp.csv": "jestkexp4.csv", "jestksav.csv": "jestksa.csv"}
# a lot export this much older than the newest one gets an import note
_STALE_FRAME_H = 24.0


def _counted_lot_frames(lots: list) -> list:
    """The lot frames that count toward on-hand / tracked (every one but the
    semi-finished export — see _NEVER_COUNTED_FRAMES)."""
    return [(n, df) for n, df in lots if n not in _NEVER_COUNTED_FRAMES]


def _frame_stamp(snap, name: str, df=None) -> str:
    """Export stamp of a lot frame (2026-09-16). source_files under the
    canonical name first; a frame the loader filled from its alias
    (jestkexp4.csv / jestksa.csv) may be stamped under the REAL file only,
    so fall back to the name in the frame's 'source' column, then to the
    LOT_FALLBACKS alias. '' when nothing is stamped."""
    sf = getattr(snap, "source_files", None) or {}
    s = sf.get(name)
    if s:
        return str(s)
    if df is None:
        return ""
    real = ""
    try:
        real = str(df.attrs.get("source") or "")
        if not real and "source" in df.columns and len(df):
            real = str(df["source"].iloc[0] or "")
    except Exception:  # noqa: BLE001 — a stamp is a courtesy, never a crash
        real = ""
    real = real.strip()
    if real and sf.get(real):
        return str(sf[real])
    alias = {**_LOT_FALLBACKS, **(getattr(_vi, "LOT_FALLBACKS", None) or {})}.get(name)
    if alias and sf.get(alias):
        return str(sf[alias])
    return ""


def _stale_frame_notes(snap, lots: list) -> list:
    """(2026-09-16) One import note per lot export stamped more than
    _STALE_FRAME_H older than the newest lot export: its lots (and, before
    the gate fix, the overdue rule) are that old."""
    from helpers.timefmt import parse_datetime
    dts = []
    for name, df in lots:
        s = _frame_stamp(snap, name, df)
        dt = parse_datetime(s) if s else None
        if dt is not None:
            dts.append((name, s, dt))
    if len(dts) < 2:
        return []
    newest = max(dts, key=lambda t: t[2])
    out = []
    for name, s, dt in dts:
        age_h = (newest[2] - dt).total_seconds() / 3600.0
        if age_h > _STALE_FRAME_H:
            out.append(f"{name}: exported {s}, {age_h / 24.0:.1f} d before the "
                       f"newest lot export ({newest[0]} {newest[1]}) — its "
                       f"lots may be out of date")
    return out


def _report_notes(snap) -> list:
    """The loader's notes plus the stale-lot-export notes (2026-09-16):
    one list for report['import_notes'] and inbound['notes']."""
    notes = _snapshot_notes(snap)
    try:
        extra = _stale_frame_notes(snap, _lot_frames(snap))
    except Exception:  # noqa: BLE001 — notes never break the report
        extra = []
    return notes + [n for n in extra if n not in notes]


def _missing_recipes(snap) -> list:
    """Semi-finished activities the BOM references but no recipe export
    carries (vif_import.missing_semi_recipes, recorded on the snapshot);
    [] for a loader / snapshot without the attribute."""
    return [str(x) for x in (getattr(snap, "missing_recipes", None) or [])]

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
    """Re-import only if any source mtime changed; keep last-good on failure.

    The gate signs every recognised export name (_vif_file_names) and
    remembers that signature ON the saved snapshot (`gate_mtimes`) rather
    than comparing against snap.source_files: what the loader records
    there is its own business (a loader mid-upgrade may skip a name the
    gate signs), and since the drop of 2026-09-15 the folder also holds
    aliases the loader ignores (jestkexp4.csv, jestksa.csv) — a compare
    that disagrees on one name would re-import and write a new snapshot
    pickle on EVERY report. A snapshot pickled before this change has no
    gate_mtimes and falls back to source_files (one extra import, then
    gated).

    A file the loader could not read (snap.errors, "name: exc") is left
    OUT of the signature so it is retried on the next call — a share
    hiccup must not hide a depot for a day — but an import whose outcome
    (frames, errors, signature) equals the saved one is returned without
    being saved again, so a persistently corrupt file costs an import per
    call, never a pickle per call."""
    snapshots_dir = Path(data_dir) / "stockcheck" / "snapshots"
    prev = load_latest_snapshot(snapshots_dir)
    folder = Path(vif_folder)
    current_mtimes = {}
    # every recognised export, old names and the 2026-09-15 drop's alike
    for name in _vif_file_names():
        p = folder / name
        if p.exists():
            import time
            current_mtimes[name] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
    if prev is not None:
        seen = getattr(prev, "gate_mtimes", None)
        if (seen if isinstance(seen, dict) else prev.source_files) == current_mtimes:
            return prev
    snap = import_vif_folder(folder)
    if snap.frames:
        failed = {str(e).split(":", 1)[0].strip() for e in (snap.errors or [])}
        snap.gate_mtimes = {k: v for k, v in current_mtimes.items()
                            if k not in failed}
        same_outcome = (
            prev is not None
            and list(prev.errors or []) == list(snap.errors or [])
            and set(prev.frames) == set(snap.frames)
            and getattr(prev, "gate_mtimes", None) == snap.gate_mtimes)
        if not same_outcome:
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
    board, the demand plan and the rates file: the bridge PO names (the ERP
    export and the legacy workbook), the configured [datasources]
    po_report_path override (when set), both dock sheets and flowstate.toml
    (rules + anchor). The report cache signs their mtimes — resolved HERE so
    the cache and the report never drift. Absent files are listed too:
    appearing must move the signature."""
    from helpers import config as hcfg
    from helpers import paths as hpaths
    from helpers.reconcile_engine import PO_FEED_NAMES
    dd = Path(data_dir)
    ref = dd / "reference"
    cfg = cfg if cfg is not None else hcfg.load_toml()
    out = [ref / name for name in PO_FEED_NAMES]
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
    # "ediact 3.csv" is the BOM frame whichever export it came from
    # (ediact.csv since the drop of 2026-09-15, "ediact 3.csv" before)
    ediact = frames.get("ediact 3.csv")
    azapart = frames.get("azapart.csv")
    if ediact is None or azapart is None:
        return {"error": "BOM export (ediact.csv / ediact 3.csv) or azapart.csv "
                         "not importable",
                "import_errors": snap.errors,
                "import_notes": _report_notes(snap),
                "missing_recipes": _missing_recipes(snap),
                "source_files": snap.source_files}

    bom = BomGraph(ediact, frames.get("ediact 4.csv"))
    # every lot export that COUNTS, rm first: AMB / QC / apple frames ride
    # in `extra` so the legacy two-frame call keeps its numbers. The
    # semi-finished export (jestkexp5) is left out of avail AND tracked
    # (rule of 2026-08-14 — an intermediate never gates a SKU on its own
    # stock; see _NEVER_COUNTED_FRAMES, 2026-09-16).
    counted = _counted_lot_frames(_lot_frames(snap))
    rm = frames.get("jestkexp.csv")
    pkg = frames.get("jestkexp2.csv")
    extra = [df for n, df in counted if n not in ("jestkexp.csv", "jestkexp2.csv")]
    avail = cov.available_stock(rm, pkg, toggles, extra=extra)
    tracked_items = set(avail.keys())
    # items with stock rows entirely toggled off still count as tracked
    for _n, df in counted:
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

    # Order priority rides into the projection's netting order (slice 3):
    # lower = more important, the loader's convention (blank -> 999).
    priorities: dict = {}
    if "priority" in demand.columns:
        for oid, pr in zip(demand["order_id"], demand["priority"]):
            priorities[str(oid)] = _finite(pr, 999.0)
    supply = _supply_section(
        data_dir, snap, bom, frames, avail, tracked_items, azapart, blocks,
        sreqs, dreqs, schedule_view,
        po_path=po_path, receiving_path=receiving_path, today=today,
        demand_view=demand_view, priorities=priorities)

    return {
        "generated_at": snap.imported_at,
        "source_files": snap.source_files,
        "import_errors": snap.errors,
        # loader notes (empty exports, dropped duplicates, ignored stale
        # files — drop of 2026-09-15) plus a note per lot export stamped
        # > 24 h before the newest one (2026-09-16); [] on an older snapshot
        "import_notes": _report_notes(snap),
        # semi-finished activities with no recipe in the VIF folder (the
        # loader's snap.missing_recipes, 2026-09-16); [] when not recorded
        "missing_recipes": _missing_recipes(snap),
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
                    po_path, receiving_path, today,
                    demand_view: list | None = None,
                    priorities: dict | None = None) -> dict:
    """Additive report keys (anchor, supply_meta, sku_needs, inbound,
    quality, projection) + schedule_view[i].key / .supply and
    demand_view[i].projected (slice 3). Mutates the view rows in place;
    the flat statuses are never touched."""
    from datetime import timedelta
    from helpers import config as hcfg
    from helpers.timefmt import (datetime_to_hour, hour_to_stamp,
                                 parse_datetime, planning_anchor)

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
    # every lot export in contract order (rm, pkg, amb, qc, apples, semi);
    # only the present ones, rm first (drop of 2026-09-15)
    lots = _lot_frames(snap)

    # -- snapshot hour per stock frame: an item's on-hand is as old as the
    # export it came from (rm first). "rm" and "pkg" are always present in
    # the stamps (blank when the file is missing) — the keys every consumer
    # reads; the other lot frames add their tag only when they exist. A
    # frame the loader filled from its alias is stamped through
    # _frame_stamp (2026-09-16: the canonical-name lookup read blank).
    by_name = dict(lots)
    stamps = {"rm": _frame_stamp(snap, "jestkexp.csv", by_name.get("jestkexp.csv")),
              "pkg": _frame_stamp(snap, "jestkexp2.csv", by_name.get("jestkexp2.csv"))}
    for name, df in lots:
        tag = _frame_tag(name)
        if tag not in stamps:
            stamps[tag] = _frame_stamp(snap, name, df)
    frame_h: dict = {}
    gate_dates = []
    side_dates = []
    for k, s in stamps.items():
        dt = parse_datetime(s) if s else None
        if dt is not None:
            frame_h[k] = float(datetime_to_hour(dt, anchor))
            (gate_dates if k in _GATE_SNAPSHOT_TAGS else side_dates).append(dt.date())
    # The gate's overdue rule uses the EARLIER of the rm / pkg dates — a
    # receipt older than the youngest of those counts could still be missing
    # from the other. Since 2026-09-16 the side exports (AMB, QC, apples,
    # semi-finished) no longer take part: one that kept an old mtime
    # loosened the rule and double counted receipts already on hand (see
    # _GATE_SNAPSHOT_TAGS). Only a snapshot with neither rm nor pkg stamped
    # falls back to the side exports rather than dropping the rule.
    snapshot_date = (min(gate_dates) if gate_dates
                     else (min(side_dates) if side_dates else None))

    # -- unit recipe per SKU (board + demand), memoised: explosion is linear
    # in cases, so one explode(sku, 1.0) serves every block of that SKU and
    # the client's drag previews alike. NO_BOM SKUs are left out -> the
    # engine grades their blocks NO_DATA ("no recipe").
    kpc = dict(zip(azapart["sku"], azapart["kg_per_case"]))
    sku_needs: dict = {}
    designations: dict = {}
    seen: set = set()
    no_recipe_items: set = set()

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
            if getattr(g, "no_recipe", False):
                # recipe-less semi-finished primary (2026-09-16): no
                # alternates and never tracked, so the timeline lists it
                # untracked instead of grading it on 750061-style
                # alternates (rule of 2026-08-14)
                alts = []
                no_recipe_items.add(str(g.primary_item))
            items.append({"item": str(g.primary_item),
                          "per_case": _finite(g.need_qty),
                          "unit": str(g.unit or ""), "alts": alts})
            designations.setdefault(str(g.primary_item), str(g.designation or ""))
            for a in (g.alternates if alts else ()):
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
    daily_lots: dict = {}
    # item -> tag of the FIRST lot frame it appears in (rm before pkg before
    # the drop's frames): that frame's export hour is the item's snapshot_h
    item_frame: dict = {}
    # the semi-finished export never counts (rule of 2026-08-14, see
    # _NEVER_COUNTED_FRAMES): no snapshot hour, no landed-rule lots
    for name, df in _counted_lot_frames(lots):
        if df is None or df.empty:
            continue
        tag = _frame_tag(name)
        # fresh apples arrive daily: their lots land only same-day lines of
        # the snapshot day (_DAILY_LOT_FRAMES, 2026-09-16)
        pool = daily_lots if name in _DAILY_LOT_FRAMES else stock_lots
        for item, unit, desig, batch, q in zip(df["item"], df["unit"],
                                               df["designation"], df["batch"],
                                               df["qty"]):
            item = str(item).strip()
            if not item:
                continue
            item_frame.setdefault(item, tag)
            if unit and item not in unit_by_item:
                unit_by_item[item] = str(unit).strip()
            if desig and item not in designations:
                designations[item] = str(desig).strip()
            # ALL lots regardless of toggles: the landed rule asks whether the
            # truck is physically here, not whether QC released it. Raw qty
            # (NaN as blank): the gate counts None/blank as 0 quietly and
            # NOTES garbage — pre-coercing here swallowed that diagnostic.
            pool.setdefault(item, []).append(
                (batch, None if isinstance(q, float) and math.isnan(q) else q))
    rmpk = frames.get("rmpkitems.csv")
    if rmpk is not None and not rmpk.empty:
        for item, desig in zip(rmpk["item"], rmpk["designation"]):
            if item and desig:
                designations.setdefault(str(item), str(desig))
    bom_items |= tracked_items | recipe_items

    # -- packaging receipt slips (PKG-REC.csv, drop of 2026-09-15): the
    # ERP's own booked receipts, {po8: [{item, qty, date, slip, batch}]}.
    # Raw-ish values (NaT/NaN -> None) so the gate notes garbage like it
    # does for lots; the gate applies the on/before-today and snapshot /
    # batch rules and consumes them — and, through the batch (2026-09-16),
    # the lots each landed slip became.
    receipt_slips: dict = {}
    slips_meta = {"n_slips": 0, "n_pos": 0, "date_min": None, "date_max": None,
                  "source_mtime": snap.source_files.get("PKG-REC.csv", "")}
    rf = _receipts_frame(snap)
    if rf is not None and not rf.empty:
        slip_dates = []
        batches = (rf["batch"].tolist() if "batch" in rf.columns
                   else [None] * len(rf))
        for po8, item, q, d, slip, batch in zip(
                rf.get("po8", []), rf.get("item", []), rf.get("qty", []),
                rf.get("receipt_date", []), rf.get("slip", []), batches):
            k = str(po8 or "").strip()
            if not k or k == "nan":
                continue
            if d is not None and d == d and hasattr(d, "date"):
                d = d.date()
            elif d != d:
                d = None
            if isinstance(d, date):
                slip_dates.append(d)
            receipt_slips.setdefault(k, []).append({
                "item": str(item or "").strip(),
                "qty": None if isinstance(q, float) and math.isnan(q) else q,
                "date": d, "slip": str(slip or "").strip(),
                "batch": ("" if batch is None or batch != batch
                          else str(batch).strip())})
        slips_meta.update(
            n_slips=int(sum(len(v) for v in receipt_slips.values())),
            n_pos=len(receipt_slips),
            date_min=min(slip_dates).isoformat() if slip_dates else None,
            date_max=max(slip_dates).isoformat() if slip_dates else None)

    # -- open-PO feed
    inbound: dict = {"state": "missing", "source_path": "", "source_mtime": "",
                     "as_of": None, "max_receipt_date": None, "n_rows": 0,
                     "errors": [], "lines": [], "receipts": {}, "join": {},
                     "appt_join": {"matched": 0, "total": 0},
                     "receiving_path": "",
                     # additive (2026-09-15): the receipt-slip feed and the
                     # loader's import notes, shown on the Inbound tab
                     "slips": slips_meta, "notes": _report_notes(snap)}
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
        as_of=inbound.get("as_of"), now=now,
        receipt_slips=receipt_slips or None, daily_lots=daily_lots or None)
    slips_meta["n_landed"] = int(feed.get("n_landed_by_slip") or 0)
    # lines counted as receipts because their slips were booked after the
    # stock export (2026-09-16)
    slips_meta["n_counted"] = int(feed.get("n_counted_by_slip") or 0)
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

    # a recipe-less semi-finished primary is never tracked (2026-09-16)
    tracked = sorted((recipe_items & tracked_items) - no_recipe_items)
    opening = {i: _finite(avail.get(i, 0.0)) for i in tracked}
    snapshot_h: dict = {}
    # hours of the exports that count on hand (the semi-finished stamp
    # never dates an opening, 2026-09-16)
    never_tags = {_frame_tag(n) for n in _NEVER_COUNTED_FRAMES}
    counted_h = [h for k, h in frame_h.items() if k not in never_tags]
    for i in tracked:
        # the export the item was counted in; an item whose own frame has
        # no stamp (or none at all) takes the EARLIEST counted hour — the
        # conservative end, matching the gate's overdue rule
        h = frame_h.get(item_frame.get(i, ""))
        if h is None and counted_h:
            h = min(counted_h)
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

    # -- slice 3: every demand order graded on the SAME curves, netted in
    # week order, receipts lifting what on-hand cannot support (projection
    # module). Demand weeks live in the demand anchor's frame; they are
    # converted to board hours HERE and nowhere else (time-frame invariant).
    from stockcheck.projection import project_demand, summarize
    from stockcheck.weeks import demand_anchor as _demand_anchor
    from stockcheck.weeks import week_index_label
    dem_anchor = _demand_anchor(data_dir)
    proj_orders = []
    for d in dreqs:
        wk = int(d["week_index"])
        ws = datetime_to_hour(dem_anchor + timedelta(weeks=wk), anchor)
        proj_orders.append({
            "order_id": str(d["order_id"]), "sku": str(d["sku"]),
            "week_index": wk, "target_kg": _finite(d["target_kg"]),
            "cases": _finite(d["cases"]),
            "priority": (priorities or {}).get(str(d["order_id"]), 999.0),
            "ws_h": float(ws), "we_h": float(ws) + 168.0,
        })
    projected = project_demand(
        proj_orders, timelines, rules=rules, feed_state=feed_state,
        kg_per_case={str(k): v for k, v in kpc.items()},
        dns_ratio=cov.DNS_RATIO,
        stamp=lambda h: hour_to_stamp(h, anchor),
        week_label=lambda w: week_index_label(w, dem_anchor))
    for row in (demand_view or []):
        row["projected"] = projected.get(str(row.get("order_id")))
    projection = {
        "demand_anchor": dem_anchor.isoformat(sep=" ", timespec="seconds"),
        "buffer_h": 24.0 * float(rules.get("min_days_after_delivery", 4)),
        "dns_ratio": cov.DNS_RATIO,
        "feed_state": feed_state,
        **summarize(projected),
    }

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
        "projection": projection,
    }
