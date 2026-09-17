# helpers/data_health.py — data-health engine: present? readable? fresh? complete?
#
# Command Center (2026-08-10): the app's answer to "is my data OK right now?".
# Pure module — no Streamlit imports — so every rule is unit-testable and the
# Home page / global strip can both call it.
#
# State vocabulary (fixed):
#   OK                — present, readable, fresh, complete
#   STALE             — present but older than its expected refresh cadence,
#                       or semantically out of date (wrong demand week, anchor)
#   MISSING           — file does not exist
#   ERROR             — file exists but cannot be read / has no usable rows
#   NOT_APPLICABLE    — nothing to check (e.g. no lines yet)
#
# Colors: OK=green, STALE=amber, MISSING/ERROR=red, NOT_APPLICABLE=gray.

from __future__ import annotations

import json
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from helpers.config import load_toml
from helpers.data_catalog import CATALOG, status
from helpers.horizon import Horizon, resolve as resolve_horizon
from helpers.paths import data_dir as default_data_dir
from helpers.paths import reference_dir

OK = "OK"
STALE = "STALE"
MISSING = "MISSING"
ERROR = "ERROR"
NOT_APPLICABLE = "NOT_APPLICABLE"

# Severity: 0 = fine, 1 = warn, 2 = blocking.
_SEVERITY = {OK: 0, STALE: 1, MISSING: 2, ERROR: 2, NOT_APPLICABLE: 0}

# Expected refresh cadence (hours) for the live feeds and derived inputs.
# Manprg refreshes ~every 15 min; the VIF exports and cip_info are daily;
# demand_plan is weekly (AZAP Monday). Defaults live here; a [health] section
# in flowstate.toml can override any of them.
DEFAULT_CADENCE_H: dict[str, float] = {
    "manprg": 0.5,          # 30 min — manprg.txt / manprg2.txt
    "cip_info": 26.0,       # daily, allow some drift
    "vif": 26.0,            # daily VIF exports (stock check)
    "demand_plan": 192.0,   # ~weekly + margin
    "demand_summary": 168.0,  # weekly AZAP baseline (demand_plan_summary.csv)
    "calendar": 24.0,       # the schedule of record should be touched daily
    "open_pos": 26.0,       # IT's open-PO extract, daily with the VIF job
    "receiving": 168.0,     # weekly dock-appointment xlsm (packaging only)
    "live_sync": 1.0,       # heartbeat of scripts/fs-live-pull.py (folder or GitHub mode)
}


@dataclass(frozen=True)
class HealthStatus:
    key: str                    # "demand_plan", "manprg", ...
    name: str                   # human label
    state: str                  # OK | STALE | MISSING | ERROR | NOT_APPLICABLE
    detail: str                 # one-line human explanation
    actions: tuple[str, ...] = ()   # what the user should do, in order
    cadence_h: float | None = None
    age_h: float | None = None
    source: str = "catalog"     # catalog | live_feed | semantic

    @property
    def severity(self) -> int:
        return _SEVERITY[self.state]

    @property
    def is_ok(self) -> bool:
        return self.state == OK


def _age_h(path: Path) -> float | None:
    """Age of a file in hours, or None when missing."""
    if not path.exists():
        return None
    try:
        return (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 3600.0
    except OSError:
        return None


def _fmt_age(age_h: float | None) -> str:
    if age_h is None:
        return "—"
    if age_h < 1:
        return f"{int(age_h * 60)} min"
    if age_h < 24:
        return f"{age_h:.1f} h"
    return f"{age_h / 24:.1f} days"


# (path, mtime_ns, size) of the BOM export -> missing semi-finished recipes;
# the BOM parse costs about a second, so it is paid once per file version.
_MISSING_RECIPES_MEMO: dict[tuple, list[str]] = {}


def _vif_missing_recipes(vif_anchor: Path) -> list[str]:
    """Semi-finished activities the VIF folder's BOM export references
    without a recipe (2026-09-16). Cheap by construction: nothing is read
    when "ediact 4.csv" (the semi-finished recipes) sits beside the BOM
    export; otherwise the BOM frame is loaded once per file version and
    handed to vif_import.missing_semi_recipes. Never raises: a loader
    without the helper, an unreadable export or any error reads as [] —
    health must not crash on the stock check's data."""
    try:
        from stockcheck import vif_import as vi
        fn = getattr(vi, "missing_semi_recipes", None)
        if fn is None:
            return []
        bom = vi.find_bom_file(Path(vif_anchor).parent)
        if bom is None:
            return []
        if (bom.parent / getattr(vi, "HSM_FILE", "ediact 4.csv")).is_file():
            return []
        st = bom.stat()
        key = (str(bom.resolve()), st.st_mtime_ns, st.st_size)
        if key not in _MISSING_RECIPES_MEMO:
            frames = {"ediact 3.csv": vi.load_ediact(bom)}
            _MISSING_RECIPES_MEMO.clear()
            _MISSING_RECIPES_MEMO[key] = sorted(str(x) for x in (fn(frames) or []))
        return list(_MISSING_RECIPES_MEMO[key])
    except Exception:  # noqa: BLE001 — a warn row, never a crash
        return []


# ---------------------------------------------------------------------------
# Catalog rules
# ---------------------------------------------------------------------------

def _catalog_statuses(dd: Path) -> list[HealthStatus]:
    out: list[HealthStatus] = []
    for spec in CATALOG:
        # Entries a dedicated live-feed rule judges (manprg, cip_info, the
        # demand baseline, the VIF exports, the PO feed, the receiving
        # workbook) or a semantic rule covers (line_rates ↔ rate_mode: the
        # generic MISSING row would cry wolf in per-SKU mode) carry
        # generic_health=False — a row here would duplicate them. The Data
        # Files page lists every file through file_statuses() below.
        if not spec.generic_health:
            continue
        info = status(spec, dd)
        if not info["exists"]:
            if spec.optional:
                continue  # an absent optional reference file is not a finding
            out.append(HealthStatus(
                key=spec.key, name=spec.name, state=MISSING,
                detail=f"{spec.rel()} does not exist.",
                actions=(f"Upload {spec.filename} on the Data Files page",),
                source="catalog",
            ))
            continue
        if info["error"]:
            out.append(HealthStatus(
                key=spec.key, name=spec.name, state=ERROR,
                detail=f"{spec.rel()} cannot be read: {info['error']}",
                actions=("Fix or re-upload the file on the Data Files page",),
                source="catalog",
            ))
            continue
        if info["rows"] == 0:
            out.append(HealthStatus(
                key=spec.key, name=spec.name, state=ERROR,
                detail=f"{spec.rel()} has 0 data rows.",
                actions=("Add rows or re-upload the file",),
                source="catalog",
            ))
            continue
        age = _age_h(spec.path(dd))
        out.append(HealthStatus(
            key=spec.key, name=spec.name, state=OK,
            detail=f"{spec.rel()} present ({info['rows']} rows, {_fmt_age(age)} old).",
            age_h=age,
            source="catalog",
        ))
    return out


# ---------------------------------------------------------------------------
# Per-file statuses — the Data Files page (2026-09-17)
# ---------------------------------------------------------------------------
# One row per catalog entry, on the file actually in use (the configured
# path, else the first existing fallback), graded present → readable → fresh
# against the SAME cadence table the feed rules above use, so the Data Files
# page and the Command Center never disagree about what "stale" means. An
# optional file that is absent reads NOT_APPLICABLE (grey), never MISSING.

@dataclass(frozen=True)
class FileStatus:
    key: str
    name: str
    group: str
    filename: str               # the file in use (fallback / override) or the expected name
    path: Path
    state: str                  # OK | STALE | MISSING | ERROR | NOT_APPLICABLE
    detail: str                 # one-line note: fallback in use, dev fixture, stamp age …
    rows: int | None = None
    age_h: float | None = None
    modified: str | None = None
    cadence_h: float | None = None
    size: int | None = None
    optional: bool = False
    source: str = ""
    managed_by: str = "user"

    @property
    def severity(self) -> int:
        return _SEVERITY[self.state]


def _manprg_content_age(dd: Path, cfg: dict) -> tuple[float, float] | None:
    """(content_age_h, file_age_h) for the manprg pair from the as-of stamp
    when it is trusted (its sha matches the files), else None — the same
    rule the manprg feed row applies (fix CA-1)."""
    try:
        from helpers.data_catalog import by_key
        from helpers.manprg_import import read_asof_stamp
        present = [p for p in (by_key(k).resolve(dd, cfg) for k in ("manprg", "manprg2"))
                   if p.is_file()]
        if not present:
            return None
        as_of, src, _w = read_asof_stamp(present)
        if as_of is None or src != "stamp":
            return None
        content_age = (datetime.now() - as_of.to_pydatetime()).total_seconds() / 3600.0
        file_age = min(_age_h(p) or 0.0 for p in present)
        return content_age, file_age
    except Exception:  # noqa: BLE001 — a detail on a row, never a crash
        return None


def file_statuses(data_dir: Path | None = None, cfg: dict | None = None) -> list[FileStatus]:
    """Every catalog entry graded for the Data Files page. Never raises: a
    resolver or reader failure becomes an ERROR row."""
    dd = Path(data_dir) if data_dir is not None else default_data_dir()
    cfg = cfg if cfg is not None else load_toml()
    cadence = {**DEFAULT_CADENCE_H, **cfg.get("health", {}).get("cadence_h", {})}
    manprg_ages = _manprg_content_age(dd, cfg)
    out: list[FileStatus] = []
    for spec in CATALOG:
        common = dict(key=spec.key, name=spec.name, group=spec.group,
                      optional=spec.optional, source=spec.source,
                      managed_by=spec.managed_by)
        cad = cadence.get(spec.cadence_key) if spec.cadence_key else None
        cad = float(cad) if cad is not None else None
        try:
            info = status(spec, dd, cfg)
        except Exception as exc:  # noqa: BLE001 — e.g. the VIF folder resolver
            out.append(FileStatus(filename=spec.filename, path=Path(spec.filename),
                                  state=ERROR, detail=f"could not resolve the file: {exc}",
                                  cadence_h=cad, **common))
            continue
        p: Path = info["path"]
        notes: list[str] = []
        if spec.folder == "vif" and p.parent.name == "dev_vif":
            notes.append("bundled dev fixture — not live plant data")
        if info["configured"]:
            notes.append(f"configured path (Settings): {p}")
        if not info["exists"]:
            if spec.optional:
                state, lead = NOT_APPLICABLE, "optional file, not present"
            else:
                state, lead = MISSING, "missing"
            looked = [n for n in spec.all_names() if n != p.name]
            if looked and not info["configured"]:
                notes.append("also looked for " + ", ".join(looked))
            out.append(FileStatus(filename=p.name, path=p, state=state,
                                  detail="; ".join([lead] + notes), cadence_h=cad, **common))
            continue
        if info["error"]:
            out.append(FileStatus(filename=p.name, path=p, state=ERROR,
                                  detail="; ".join([f"cannot be read: {info['error']}"] + notes),
                                  age_h=info["age_h"], modified=info["modified"],
                                  cadence_h=cad, size=info["size"], **common))
            continue
        if info["rows"] == 0 and spec.kind == "csv":
            out.append(FileStatus(filename=p.name, path=p, state=ERROR,
                                  detail="; ".join(["0 data rows"] + notes),
                                  rows=0, age_h=info["age_h"], modified=info["modified"],
                                  cadence_h=cad, size=info["size"], **common))
            continue
        age = info["age_h"]
        if spec.cadence_key == "manprg" and manprg_ages is not None:
            # CONTENT age: a file re-written with identical content is still
            # stale telemetry (audit C01)
            age = manprg_ages[0]
            notes.append(f"content observed {_fmt_age(age)} ago (as-of stamp); "
                         f"file written {_fmt_age(manprg_ages[1])} ago")
        if info["alternate_in_use"]:
            notes.append(f"fallback in use — {spec.filename} not present")
        stale = cad is not None and age is not None and age > cad
        if stale:
            notes.insert(0, f"older than its expected refresh (≤ {cad:g} h)")
        out.append(FileStatus(filename=p.name, path=p, state=STALE if stale else OK,
                              detail="; ".join(notes), rows=info["rows"], age_h=age,
                              modified=info["modified"], cadence_h=cad,
                              size=info["size"], **common))
    return out


# ---------------------------------------------------------------------------
# Live feed freshness
# ---------------------------------------------------------------------------

def _live_sync_status(dd: Path, cadence: dict) -> HealthStatus:
    """The bridge's heartbeat. scripts/fs-live-pull.py writes data/reference/
    live_sync.json after EVERY pass (folder mode on the planner's PC, GitHub
    mode on the dev PC). No file = the sync has never run on this machine
    (files copied in by hand): not a finding. A pass that reported problems,
    or a heartbeat older than the cadence, is a warning — the feed rows below
    say which files are actually old, so this row never blocks on its own."""
    p = reference_dir(dd) / "live_sync.json"
    if not p.is_file():
        return HealthStatus(
            key="live_sync", name="Live data sync", state=NOT_APPLICABLE,
            detail="No sync has run on this machine (scripts/fs-live-pull.py); "
                   "the feed files are whatever was copied in by hand.",
            source="live_feed")
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
        finished = datetime.fromisoformat(str(meta.get("finished")))
    except (OSError, ValueError, TypeError):
        return HealthStatus(
            key="live_sync", name="Live data sync", state=STALE,
            detail="live_sync.json is unreadable.",
            actions=("Run scripts/fs-live-pull.py --once and read its output",),
            source="live_feed")
    age = (datetime.now() - finished).total_seconds() / 3600.0
    mode = str(meta.get("mode") or "?")
    sources = [str(x) for x in (meta.get("sources") or [])]
    where = ("folder " + "; ".join(sources)) if mode == "folder" else "GitHub bridge"
    problems = [str(x) for x in (meta.get("problems") or [])]
    updated = [str(x) for x in (meta.get("updated") or [])]
    limit = float(cadence.get("live_sync", 1.0))
    if problems or not meta.get("ok", True):
        state = STALE
        detail = (f"Last sync ({where}) {_fmt_age(age)} ago reported problems: "
                  + "; ".join(problems[:3]) + ".")
        actions = ("Check that the source folder is reachable and readable, "
                   "then run scripts/fs-live-pull.py --once",)
    elif age > limit:
        state = STALE
        detail = (f"Last sync ({where}) finished {_fmt_age(age)} ago; expected every "
                  f"{limit:g} h or less. Is the scheduled task 'Flowstate Live Data Pull' running?")
        actions = ("Open Task Scheduler and run 'Flowstate Live Data Pull', "
                   "or run scripts/fs-live-pull.py --once",)
    else:
        state = OK
        what = ((f"updated {', '.join(updated[:4])}" + ("…" if len(updated) > 4 else ""))
                if updated else "nothing new")
        detail = f"Last sync ({where}) finished {_fmt_age(age)} ago: {what}."
        actions = ()
    return HealthStatus(key="live_sync", name="Live data sync", state=state, detail=detail,
                        actions=actions, cadence_h=limit, age_h=age, source="live_feed")


def _live_feed_statuses(dd: Path, cfg: dict) -> list[HealthStatus]:
    ds = cfg.get("datasources", {})
    cadence = {**DEFAULT_CADENCE_H, **cfg.get("health", {}).get("cadence_h", {})}
    out: list[HealthStatus] = [_live_sync_status(dd, cadence)]

    manprg_paths = [p.strip() for p in str(ds.get("manprg_files", "")).split(";")
                    if p.strip()] or [
        str(reference_dir(dd) / "manprg.txt"),
        str(reference_dir(dd) / "manprg2.txt"),
    ]
    present = [p for p in manprg_paths if Path(p).exists()]
    if not present:
        out.append(HealthStatus(
            key="manprg", name="Live MO progress (manprg)", state=MISSING,
            detail="No manprg.txt / manprg2.txt found (checked configured paths + data/reference/).",
            actions=("Refresh the manprg export (or enable SQL live refresh in Settings)",),
            source="live_feed",
        ))
    else:
        # CONTENT age, not just file age (fix CA-1 / audit C01): the bridge
        # stamps manprg.asof.json when the manprg content changes; a file
        # re-written with identical content is still stale telemetry. The
        # stamp is trusted only while its sha matches the files (else mtime).
        file_age = min(_age_h(Path(p)) for p in present)
        age, src = file_age, "file mtime"
        try:
            from helpers.manprg_import import read_asof_stamp
            as_of, as_of_src, _w = read_asof_stamp([Path(p) for p in present])
            if as_of is not None and as_of_src == "stamp":
                age = (datetime.now() - as_of.to_pydatetime()).total_seconds() / 3600.0
                src = "as-of stamp"
        except Exception:  # noqa: BLE001 — health must never crash on this
            pass
        stale = age is not None and age > cadence.get("manprg", 0.5)
        detail = (f"manprg content observed {_fmt_age(age)} ago ({src})."
                  if age is not None else "manprg present (age unknown).")
        if src == "as-of stamp" and file_age is not None:
            detail += f" File written {_fmt_age(file_age)} ago."
        out.append(HealthStatus(
            key="manprg", name="Live MO progress (manprg)", state=STALE if stale else OK,
            detail=detail
            + (f" Expected refresh ≤ {cadence.get('manprg', 0.5):g} h." if stale else ""),
            actions=("Refresh the manprg export (or enable SQL live refresh in Settings)",) if stale else (),
            cadence_h=cadence.get("manprg"), age_h=age,
            source="live_feed",
        ))

    cip_path = str(ds.get("cip_info_csv", "")).strip() or str(reference_dir(dd) / "cip_info.csv")
    cip_p = Path(cip_path)
    if not cip_p.exists():
        out.append(HealthStatus(
            key="cip_info", name="CIP schedule (cip_info)", state=MISSING,
            detail="cip_info.csv not found.",
            actions=("Drop cip_info.csv in data/reference/ or set the path in Settings",),
            source="live_feed",
        ))
    else:
        age = _age_h(cip_p)
        stale = age is not None and age > cadence.get("cip_info", 26.0)
        out.append(HealthStatus(
            key="cip_info", name="CIP schedule (cip_info)", state=STALE if stale else OK,
            detail=(f"cip_info.csv last refreshed {_fmt_age(age)} ago."
                    if age is not None else "cip_info.csv present (age unknown).")
            + (f" Expected refresh ≤ {cadence.get('cip_info', 26.0):g} h." if stale else ""),
            actions=("Refresh cip_info.csv (or enable SQL live refresh in Settings)",) if stale else (),
            cadence_h=cadence.get("cip_info"), age_h=age,
            source="live_feed",
        ))

    dem_sum_path = str(ds.get("demand_summary_csv", "")).strip() or str(
        reference_dir(dd) / "demand_plan_summary.csv")
    dem_sum_p = Path(dem_sum_path)
    if not dem_sum_p.exists():
        out.append(HealthStatus(
            key="demand_summary", name="Demand plan summary (baseline)", state=MISSING,
            detail="demand_plan_summary.csv not found.",
            actions=("Drop demand_plan_summary.csv in data/reference/, set the "
                     "path in Settings, or upload it on the Data Files page",),
            source="live_feed",
        ))
    else:
        age = _age_h(dem_sum_p)
        stale = age is not None and age > cadence.get("demand_summary", 168.0)
        out.append(HealthStatus(
            key="demand_summary", name="Demand plan summary (baseline)", state=STALE if stale else OK,
            detail=(f"demand_plan_summary.csv last refreshed {_fmt_age(age)} ago."
                    if age is not None else "demand_plan_summary.csv present (age unknown).")
            + (f" Expected refresh ≤ {cadence.get('demand_summary', 168.0):g} h." if stale else ""),
            actions=("Import a fresh demand_plan_summary.csv on the Data Files page",) if stale else (),
            cadence_h=cadence.get("demand_summary"), age_h=age,
            source="live_feed",
        ))

    # VIF exports for the stock check (P1 live link, landed 2026-08-14 via the
    # bridge). The BOM export anchors the set — ediact.csv since the ERP drop
    # of 2026-09-15, the legacy "ediact 3.csv" before it (one rule:
    # stock_report_cache.find_bom_file) — the engine cannot run without it,
    # so its age speaks for every VIF file + the receiving xlsm. Dev
    # fixtures under data/stockcheck/dev_vif keep the page usable without the
    # live link, so their presence downgrades MISSING to STALE (warn).
    from helpers.stock_report_cache import find_bom_file
    vif_anchor = find_bom_file(reference_dir(dd))
    if vif_anchor is None:
        dev_ok = find_bom_file(dd / "stockcheck" / "dev_vif") is not None
        out.append(HealthStatus(
            key="vif_stock",
            name="VIF exports (stock check)",
            state=STALE if dev_ok else MISSING,
            detail=("Live VIF link not landed — stock check is running on the "
                    "bundled dev fixtures." if dev_ok else
                    "No VIF exports found (live link or dev fixtures)."),
            actions=("Push the VIF exports from the work PC "
                     "(fs-live-push) and pull the bridge",),
            source="live_feed",
        ))
    else:
        age = _age_h(vif_anchor)
        stale = age is not None and age > cadence.get("vif", 26.0)
        # (2026-09-16) the ediact.csv export carries no semi-finished recipes
        # (24 of the 27 "ediact 4.csv" activities absent): without that file
        # the stock check grades intermediates as unstocked leaves. A warn,
        # never blocking — the report still runs.
        missing = _vif_missing_recipes(vif_anchor)
        detail = (f"VIF exports last refreshed {_fmt_age(age)} ago."
                  if age is not None else "VIF exports present (age unknown).")
        detail += (f" Expected refresh ≤ {cadence.get('vif', 26.0):g} h." if stale else "")
        actions: tuple[str, ...] = (
            ("Refresh the VIF export push from the work PC",) if stale else ())
        if missing:
            detail += (f" {len(missing)} semi-finished recipe(s) missing from "
                       f"{vif_anchor.name} ({', '.join(missing[:3])}"
                       f"{', …' if len(missing) > 3 else ''}) — add ediact 4.csv "
                       "(semi-finished recipes) to the VIF folder.")
            actions += ("Add ediact 4.csv (semi-finished recipes) to the VIF folder",)
        out.append(HealthStatus(
            key="vif_stock", name="VIF exports (stock check)",
            state=STALE if (stale or missing) else OK,
            detail=detail,
            actions=actions,
            cadence_h=cadence.get("vif"), age_h=age,
            source="live_feed",
        ))

    # Open-PO report (supply timeline, 2026-09-01). Same resolver as the
    # stock report so Home and the Inbound tab agree on the source. mtime is
    # the bridge pull time, so this row is a health-only staleness signal;
    # the engine's own gate is content-based (max receipt date vs today).
    # No dev fixture exists for this feed, so absence is MISSING, not STALE.
    from helpers.reconcile_engine import open_po_path
    po_p = open_po_path(dd, cfg)
    # is_file: an override pointing at IT's drop FOLDER exists but is no
    # report — MISSING (naming it), never OK on the bridge copy's presence
    if po_p is None or not po_p.is_file():
        out.append(HealthStatus(
            key="open_pos", name="Open PO report (inbound)", state=MISSING,
            detail=("ERP export order_npa.csv and the legacy "
                    "open_pos.xlsx / open_pos.csv not found in data/reference/."
                    if po_p is None else
                    f"Configured po_report_path is a directory, not a file: {po_p}"
                    if po_p.is_dir() else
                    f"Configured po_report_path not found: {po_p}"),
            actions=("Push the ERP PO export order_npa.csv (or the legacy "
                     "open_pos.xlsx) via the bridge (fs-live-push) and pull, "
                     "or set po_report_path in Settings",),
            source="live_feed",
        ))
    else:
        age = _age_h(po_p)
        stale = age is not None and age > cadence.get("open_pos", 26.0)
        out.append(HealthStatus(
            key="open_pos", name="Open PO report (inbound)", state=STALE if stale else OK,
            detail=(f"{po_p.name} last refreshed {_fmt_age(age)} ago."
                    if age is not None else f"{po_p.name} present (age unknown).")
            + (f" Expected refresh ≤ {cadence.get('open_pos', 26.0):g} h." if stale else ""),
            actions=(f"Refresh the PO export push from the work PC ({po_p.name})",)
            if stale else (),
            cadence_h=cadence.get("open_pos"), age_h=age,
            source="live_feed",
        ))

    # Receiving schedule (dock appointments). Its own row: it rode along
    # under vif_stock's age before, which hid a decayed xlsm behind fresh
    # VIF exports. The bundled dev fixture keeps the Receiving tab usable, so
    # its presence downgrades MISSING to STALE like the VIF rule.
    recv_p = reference_dir(dd) / "Shipping Receiving Schedule NPA - 2024.xlsm"
    if not recv_p.exists():
        dev_ok = (dd / "stockcheck" / "dev_receiving_schedule.xlsm").exists()
        out.append(HealthStatus(
            key="receiving", name="Receiving schedule (dock appointments)",
            state=STALE if dev_ok else MISSING,
            detail=("Live receiving xlsm not landed — Stock Check is showing the "
                    "bundled dev fixture." if dev_ok else
                    "Shipping Receiving Schedule NPA - 2024.xlsm not found in data/reference/."),
            actions=("Push the weekly receiving xlsm from the work PC "
                     "(fs-live-push) and pull the bridge",),
            source="live_feed",
        ))
    else:
        age = _age_h(recv_p)
        stale = age is not None and age > cadence.get("receiving", 168.0)
        out.append(HealthStatus(
            key="receiving", name="Receiving schedule (dock appointments)",
            state=STALE if stale else OK,
            detail=(f"Receiving xlsm last refreshed {_fmt_age(age)} ago."
                    if age is not None else "Receiving xlsm present (age unknown).")
            + (f" Expected refresh ≤ {cadence.get('receiving', 168.0):g} h." if stale else ""),
            actions=("Refresh the weekly receiving xlsm push from the work PC",)
            if stale else (),
            cadence_h=cadence.get("receiving"), age_h=age,
            source="live_feed",
        ))
    return out


# ---------------------------------------------------------------------------
# Semantic rules
# ---------------------------------------------------------------------------

def _demand_week_semantics(dd: Path, cfg: dict) -> list[HealthStatus]:
    """demand_plan.csv week grid vs the resolved horizon's ISO week."""
    out: list[HealthStatus] = []
    dem_path = reference_dir(dd) / "demand_plan.csv"
    if not dem_path.exists():
        return out  # catalog already flags MISSING
    try:
        df = pd.read_csv(dem_path)
    except Exception:
        return out  # catalog already flags ERROR
    if df.empty or "week_index" not in df.columns:
        return out
    try:
        hz: Horizon = resolve_horizon(cfg)
        iso_now = hz.anchor.isocalendar()[1]
        # week_index 0 == the ISO week of the DEMAND FILE'S OWN anchor
        # (demand_plan.source.json), NOT the planning anchor — the file is
        # self-anchored to the Monday of its earliest week (audit
        # 2026-08-15: mapping via the planning anchor reported the plan a
        # week fresher than it was, so the staleness gate under-fired).
        import json as _json
        base_iso = None
        _meta = reference_dir(dd) / "demand_plan.source.json"
        if _meta.exists():
            try:
                base_iso = int(_json.loads(
                    _meta.read_text(encoding="utf-8"))["anchor_iso_week"])
            except Exception:
                base_iso = None
        if base_iso is None:
            from helpers.timefmt import week_index_to_iso
            stored_weeks = {int(w): week_index_to_iso(int(w), hz.anchor)
                            for w in df["week_index"].unique()}
        else:
            stored_weeks = {int(w): base_iso + int(w)
                            for w in df["week_index"].unique()}
        latest_iso = max(stored_weeks.values())
        stale = latest_iso < iso_now - 1  # more than one week behind today
        out.append(HealthStatus(
            key="demand_week", name="Demand plan week", state=STALE if stale else OK,
            detail=(f"demand_plan covers through ISO week {latest_iso} "
                    f"(today is ISO week {iso_now}).")
            + ("" if not stale else " Refresh demand_plan_summary.csv — the "
               "pull script derives demand_plan.csv from it automatically."),
            actions=("Run scripts/fs-live-pull.py (or re-upload "
                     "demand_plan_summary.csv on the Data Files page)",)
            if stale else (),
            source="semantic",
        ))
    except Exception as exc:  # pragma: no cover - defensive
        out.append(HealthStatus(
            key="demand_week", name="Demand plan week", state=ERROR,
            detail=f"Could not evaluate demand week: {exc}", source="semantic",
        ))
    return out


def _calendar_anchor_semantics(dd: Path, cfg: dict) -> list[HealthStatus]:
    """Stored calendar anchor vs the resolved horizon anchor."""
    out: list[HealthStatus] = []
    cal_path = dd / "calendar_blocks.csv"
    if not cal_path.exists():
        return out
    try:
        hz = resolve_horizon(cfg)
        if hz.mode == "today" and hz.stale:
            out.append(HealthStatus(
                key="calendar_anchor", name="Calendar anchor", state=STALE,
                detail=(f"Calendar is anchored to {hz.config_anchor:%a %Y-%m-%d}, "
                        f"{hz.shift_h / 24:.1f} day(s) before today. Blocks are shown "
                        "against the old anchor."),
                actions=("Roll calendar to today on the Plant Calendar page",),
                source="semantic",
            ))
    except Exception as exc:  # pragma: no cover
        out.append(HealthStatus(
            key="calendar_anchor", name="Calendar anchor", state=ERROR,
            detail=f"Could not evaluate calendar anchor: {exc}", source="semantic",
        ))
    return out


def _calendar_completeness(dd: Path, cfg: dict) -> list[HealthStatus]:
    """Zero-CIP / zero-maintenance calendars are suspicious, not good."""
    out: list[HealthStatus] = []
    cal_path = dd / "calendar_blocks.csv"
    if not cal_path.exists():
        return out
    try:
        cal = pd.read_csv(cal_path)
    except Exception:
        return out
    if cal.empty or "block_type" not in cal.columns:
        return out
    counts = cal["block_type"].value_counts().to_dict()
    n_cip = int(counts.get("cip", 0))
    n_maint = int(counts.get("maintenance", 0))
    n_prod = int(counts.get("production", 0))
    if n_prod > 0 and n_cip == 0:
        out.append(HealthStatus(
            key="calendar_cip", name="CIP blocks in calendar", state=STALE,
            detail=(f"{n_prod} production block(s) but 0 CIP blocks. The scorecard's "
                    "CIP category will read 100 without any cleaning scheduled."),
            actions=("Add CIP blocks, or rebuild from plant state (cip_info) on the Plant Calendar",),
            source="semantic",
        ))
    if n_prod > 0 and n_maint == 0:
        out.append(HealthStatus(
            key="calendar_maint", name="Maintenance blocks in calendar", state=STALE,
            detail=("0 maintenance blocks. The scorecard's maintenance category "
                    "will read 100 — that is no data, not good performance."),
            actions=("Enter planned maintenance in the Plant Calendar (STEP 1 downtime editor)",),
            source="semantic",
        ))
    return out


def _changeover_quality(dd: Path, cfg: dict) -> list[HealthStatus]:
    """changeovers.csv full of setup_hours == 0 makes the solver plan free changeovers."""
    out: list[HealthStatus] = []
    path = reference_dir(dd) / "changeovers.csv"
    if not path.exists():
        return out
    try:
        df = pd.read_csv(path)
    except Exception:
        return out
    if df.empty or "setup_hours" not in df.columns:
        return out
    try:
        zeros = int((df["setup_hours"].astype(float) <= 0).sum())
    except (TypeError, ValueError):
        return out
    if zeros == len(df):
        out.append(HealthStatus(
            key="changeover_hours", name="Changeover setup hours", state=STALE,
            detail=(f"All {zeros} changeover rows have setup_hours ≤ 0. The scorecard "
                    "falls back to default CO hours, but the SOLVER plans every "
                    "changeover as free (~0h)."),
            actions=("Populate setup_hours in data/reference/changeovers.csv",),
            source="semantic",
        ))
    elif zeros > 0:
        out.append(HealthStatus(
            key="changeover_hours", name="Changeover setup hours", state=OK,
            detail=f"{zeros} of {len(df)} changeover rows have setup_hours ≤ 0 (fallback applies).",
            source="semantic",
        ))
    # Duplicate (from,to) rows: benign when identical (loaders dedupe
    # last-wins), dangerous when they conflict — the surviving row is
    # arbitrary. Seen first in the 2026-08-26 plant export (466 exact dupes).
    if {"from_sku", "to_sku"}.issubset(df.columns):
        dup_mask = df.duplicated(subset=["from_sku", "to_sku"], keep=False)
        if dup_mask.any():
            n_dup_rows = int(df.duplicated(
                subset=["from_sku", "to_sku"]).sum())
            n_conflict = int((df[dup_mask].groupby(
                ["from_sku", "to_sku"]).nunique() > 1).any(axis=1).sum())
            out.append(HealthStatus(
                key="changeover_dupes", name="Changeover duplicate pairs",
                state=STALE if n_conflict else OK,
                detail=(f"{n_dup_rows} duplicate from/to row(s)"
                        + (f", {n_conflict} pair(s) with CONFLICTING values "
                           "— the loaded row is arbitrary (last wins)."
                           if n_conflict else " (exact copies — harmless).")),
                actions=("De-duplicate data/reference/changeovers.csv in the "
                         "source export",) if n_conflict else (),
                source="semantic",
            ))
    return out


def _rate_mode_semantics(dd: Path, cfg: dict) -> list[HealthStatus]:
    """Flat line rates (use_sku_rates=false + line_rates.csv present) is the
    intended mode; per-SKU rates (use_sku_rates=true) remain OK as before."""
    out: list[HealthStatus] = []
    sched = cfg.get("scheduler", {})
    use_sku = bool(sched.get("use_sku_rates", False))
    has_flat_file = (reference_dir(dd) / "line_rates.csv").exists()
    if use_sku:
        out.append(HealthStatus(
            key="rate_mode", name="Solver rate mode", state=OK,
            detail=("use_sku_rates = true: solver and UI both use per-SKU "
                    "calc_rate_kgph, with measured kg/h from "
                    "reference/historical/rates_by_line_sku.csv overlaid where "
                    "the run log has enough evidence (helpers.effective_rates)."),
            source="semantic",
        ))
    elif has_flat_file:
        out.append(HealthStatus(
            key="rate_mode", name="Solver rate mode", state=OK,
            detail=("flat line rates active: solver and scorecard both use "
                    "line_rates.csv."),
            source="semantic",
        ))
    else:
        out.append(HealthStatus(
            key="rate_mode", name="Solver rate mode", state=STALE,
            detail=("use_sku_rates = false but data/reference/line_rates.csv is "
                    "missing: the solver falls back to per-SKU calc_rate_kgph, so "
                    "solver run durations disagree with the calendar/scorecard."),
            actions=("Place line_rates.csv in data/reference/",),
            source="semantic",
        ))
    return out


def _scorecard_semantics(dd: Path, cfg: dict) -> list[HealthStatus]:
    """No scorecard saved this ISO week -> the weekly snapshot is missing."""
    out: list[HealthStatus] = []
    sc_dir = dd / "scorecards"
    if not sc_dir.exists():
        return out
    try:
        hz = resolve_horizon(cfg)
        # (year, week) — a bare week number matches the same week of any year.
        anchor_yw = tuple(hz.anchor.isocalendar())[:2]
        iso_now = anchor_yw[1]
        from helpers.scorecard_engine import list_scorecards
        items = list_scorecards(dd)
        for item in items:
            scored_at = item.get("scored_at", "")
            if not scored_at:
                continue
            try:
                dt = datetime.fromisoformat(scored_at)
            except ValueError:
                continue
            if tuple(dt.isocalendar())[:2] == anchor_yw:
                # Scored this week — say so explicitly. An ABSENT entry is
                # ambiguous: the Home Track step read "no entry" as "no
                # scorecard history yet" even with history on disk
                # (walkthrough finding, 2026-08-18).
                comp = item.get("composite")
                out.append(HealthStatus(
                    key="scorecard", name="Weekly scorecard", state=OK,
                    detail=(f"Scored in ISO week {iso_now} "
                            f"({item.get('week_label', '?')}"
                            + (f", composite {comp}" if comp is not None else "")
                            + f") — {len(items)} scorecard(s) in history."),
                    source="semantic",
                ))
                return out
        out.append(HealthStatus(
            key="scorecard", name="Weekly scorecard", state=STALE,
            detail=f"No scorecard saved in ISO week {iso_now}. History is how the "
                   "planner shows a week got better or worse.",
            actions=("Open Schedule Scorecard and click 'Score this week'",),
            source="semantic",
        ))
    except Exception:  # pragma: no cover
        pass
    return out


def _version_slots(dd: Path, cfg: dict) -> list[HealthStatus]:
    """Version capacity pressure."""
    from helpers.version_manager import MAX_VERSIONS, list_versions
    try:
        n = len(list_versions(dd))
        if n >= MAX_VERSIONS:
            return [HealthStatus(
                key="versions", name="Version slots", state=STALE,
                detail=f"{n} / {MAX_VERSIONS} version slots used. A scenario run "
                       "will auto-evict the oldest auto-saved scenario version "
                       "(never a user-named one); manual saves need a free slot.",
                actions=("Delete an old version in Version Compare to free a slot",),
                source="semantic",
            )]
    except Exception:  # pragma: no cover
        pass
    return []


def _capability_semantics(dd: Path, cfg: dict) -> list[HealthStatus]:
    """manprg SKU/line pairs + demand SKU coverage vs capabilities table.

    manprg is ground truth: if the plant ran an SKU on a line, the
    capabilities table must allow it. Demand SKUs with NO capable line
    anywhere silently drop their demand — also flag those.
    """
    caps_path = dd / "reference" / "capabilities_rates.csv"
    if not caps_path.exists():
        return []
    try:
        from helpers.capability_check import (
            check_capabilities,
            check_demand_capabilities,
            load_capabilities,
            load_manprg_mos,
        )
        ds = cfg.get("datasources", {})
        mp_paths = [p.strip() for p in str(ds.get("manprg_files", "")).split(";")
                    if p.strip()] or [
                        str(dd / "reference" / "manprg.txt"),
                        str(dd / "reference" / "manprg2.txt")]
        mos = load_manprg_mos(mp_paths)
        caps = load_capabilities(caps_path)
        res = check_capabilities(caps, mos)
        dem_path = dd / "reference" / "demand_plan.csv"
        dem_res = None
        if dem_path.exists():
            dem_res = check_demand_capabilities(
                caps, pd.read_csv(dem_path, dtype={"sku": str}))
        total = res.count + (dem_res.count if dem_res else 0)
        if total == 0:
            return [HealthStatus(
                key="capability_check", name="Line/SKU capability check",
                state=OK, detail=f"{len(mos)} manprg MO(s) all match the "
                "capabilities table; every demand SKU has a capable line.",
                source="semantic")]
        parts = [f"{c.sku}@{c.line_name}" for c in res.conflicts[:6]]
        if dem_res:
            parts += [f"{c.sku} (no line)" for c in dem_res.conflicts[:4]]
        more = f" (+{total - len(parts)} more)" if total > len(parts) else ""
        return [HealthStatus(
            key="capability_check", name="Line/SKU capability check",
            state=STALE, detail=f"{total} capability issue(s): {', '.join(parts)}{more}",
            actions=("Data Files → 'Add these proven SKU/line pairs to "
                     "capabilities (one-click fix)'",),
            source="semantic")]
    except Exception:  # pragma: no cover
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assess(data_dir: Path | None = None, cfg: dict | None = None) -> list[HealthStatus]:
    """Full health assessment: catalog + live feeds + semantic rules."""
    dd = Path(data_dir) if data_dir is not None else default_data_dir()
    cfg = cfg if cfg is not None else load_toml()
    results: list[HealthStatus] = []
    results.extend(_catalog_statuses(dd))
    results.extend(_live_feed_statuses(dd, cfg))
    results.extend(_demand_week_semantics(dd, cfg))
    results.extend(_calendar_anchor_semantics(dd, cfg))
    results.extend(_calendar_completeness(dd, cfg))
    results.extend(_changeover_quality(dd, cfg))
    results.extend(_rate_mode_semantics(dd, cfg))
    results.extend(_scorecard_semantics(dd, cfg))
    results.extend(_version_slots(dd, cfg))
    results.extend(_capability_semantics(dd, cfg))
    return results


def next_actions(health: Iterable[HealthStatus], limit: int = 3) -> list[str]:
    """Ranked, de-duplicated 'what you need to do next' actions.

    Blocking (MISSING/ERROR) first, then stale, then informational. Each
    status contributes at most its first action; duplicates are removed.
    """
    statuses = list(health)
    statuses.sort(key=lambda s: (-s.severity, s.name))
    seen: set[str] = set()
    actions: list[str] = []
    for s in statuses:
        if not s.actions:
            continue
        for a in s.actions:
            if a not in seen:
                seen.add(a)
                actions.append(a)
                break
        if len(actions) >= limit:
            break
    return actions


def summary(health: Iterable[HealthStatus]) -> dict[str, int]:
    """Counts per state for banners."""
    counts = {OK: 0, STALE: 0, MISSING: 0, ERROR: 0, NOT_APPLICABLE: 0}
    for s in health:
        counts[s.state] = counts.get(s.state, 0) + 1
    return counts
