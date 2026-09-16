# helpers/stock_report_cache.py — the ONE persisted stock report every page
# shares.
#
# stock_check_report() costs ~35s (BOM explosion of the whole board + the
# demand plan); Home, Stock Check and Reconcile each rebuilt it on a ttl, so
# nearly every visit paid the full recompute. The pages now share ONE report
# persisted at data/stockcheck/report.cache.json (covered by the
# data/stockcheck/*.cache.json gitignore), keyed by an input signature (the
# six VIF exports, the board, the demand plan, the rates file, the resolved
# folder and the availability toggles):
#   * Home is the AUTO surface — a moved signature recomputes + saves right
#     there; unchanged inputs load the saved report in milliseconds;
#   * Stock Check and Reconcile NEVER recompute on their own — they render
#     the saved report instantly, a stale signature only raises a banner,
#     and their Refresh button calls refresh_report().
# Folder + toggles always come from reconcile_engine.stock_report_inputs,
# the shared resolver, so every surface keeps counting the SAME report
# (walkthrough 2026-08-17: Home and Reconcile once disagreed).
#
# The overnight batch keeps calling stock_check_report directly — a fresh
# component check before every solve round is a user requirement, never
# served from a cache.

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

CACHE_NAME = "report.cache.json"

# Everything the report reads besides the toggles: the VIF exports
# (refresh_vif_snapshot's own mtime guard uses the same list), the board
# (schedule view), the demand plan (demand view), the rates file (kg per
# block) and the supply-timeline inputs (open-PO file incl. a configured
# override, dock sheet, flowstate.toml for rules + anchor — the list lives in
# stockcheck.api.supply_input_paths so cache and report cannot drift). A
# missing file signs 0.0 — appearing and disappearing both move the
# signature.
#
# The ERP drop of 2026-09-15 replaced "ediact 3.csv" + "ediact 4.csv" with
# one headerless ediact.csv and added the AMB / QC / apple / semi-finished
# lot exports plus the packaging receipt slips (PKG-REC.csv). The names come
# from stockcheck.vif_import.VIF_FILES (the loader owns the list) unioned
# with the local fallback below, so the signature covers every file whether
# the loader on this box already knows the new names or not.
_BOM_FILES_FALLBACK = ("ediact.csv", "ediact 3.csv")
_VIF_FILES_FALLBACK = (
    "ediact.csv", "ediact 3.csv", "ediact 4.csv",
    "jestkexp.csv", "jestkexp2.csv", "jestkamb.csv", "jestkexq.csv",
    "jestksav.csv", "jestksa.csv", "jestkexp5.csv", "jestkexp4.csv",
    "PKG-REC.csv", "azapart.csv", "rmpkitems.csv",
)


def vif_file_names() -> tuple[str, ...]:
    """Every recognised VIF export name, old and new, in a stable order:
    the loader's VIF_FILES first (when importable), then any fallback name
    it does not list."""
    names: list[str] = []
    try:
        from stockcheck.vif_import import VIF_FILES
        names = [str(n) for n in VIF_FILES]
    except Exception:  # noqa: BLE001 — the loader may be mid-upgrade
        names = []
    for n in _VIF_FILES_FALLBACK:
        if n not in names:
            names.append(n)
    return tuple(names)


def find_bom_file(folder) -> Path | None:
    """The BOM export present in `folder`: ediact.csv (the drop of
    2026-09-15) or the legacy "ediact 3.csv", whichever exists first —
    vif_import.find_bom_file when the loader exports it, the same rule
    locally otherwise. None when neither exists. This is the presence
    anchor every "is the VIF folder live?" check keys on (data_health,
    reconcile_engine.stock_report_inputs, the Stock Check page)."""
    try:
        from stockcheck.vif_import import find_bom_file as _find
    except Exception:  # noqa: BLE001
        _find = None
    if _find is not None:
        try:
            hit = _find(folder)
            return Path(hit) if hit is not None else None
        except Exception:  # noqa: BLE001 — presence is a yes/no, never a crash
            pass
    for name in _BOM_FILES_FALLBACK:
        p = Path(folder) / name
        if p.is_file():
            return p
    return None


_VIF_FILES = vif_file_names()


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def current_signature(data_dir: Path, vif_folder: str,
                      toggles: dict | None, *, today=None) -> list:
    """Input signature of the report. Besides the file mtimes it carries
    the compute DATE (audit stock-12): the feed's stale verdict and the
    overdue fate both depend on today's date, so a report computed
    yesterday must read stale after midnight even when no file moved.
    `today` (date) is for tests; None = date.today()."""
    from stockcheck.api import supply_input_paths
    dd = Path(data_dir)
    vf = Path(vif_folder)
    # Paths are signed RESOLVED: the app hands an absolute data_dir while
    # scripts (overnight batch, probes) pass a relative one, and a spelling
    # difference alone read as "inputs moved" (2026-09-01: a report computed
    # by a script showed stale on the calendar seconds later).
    sig: list = [_abs(vf),
                 json.dumps(toggles or {}, sort_keys=True)]
    sig += [_mtime(vf / n) for n in _VIF_FILES]
    sig += [_mtime(dd / "calendar_blocks.csv"),
            _mtime(dd / "reference" / "demand_plan.csv"),
            _mtime(dd / "reference" / "capabilities_rates.csv")]
    # paths, not just mtimes: a changed po_report_path must re-sign even
    # when both files happen to share an mtime
    for p in supply_input_paths(dd):
        sig += [_abs(p), _mtime(p)]
    # the clock term: one recompute per day, never a day-old staleness
    sig.append((today or date.today()).isoformat())
    return sig


def _abs(p: Path | str) -> str:
    try:
        return str(Path(p).resolve())
    except OSError:
        return str(p)


@dataclass(frozen=True)
class CachedReport:
    report: dict
    computed_at: str        # ISO stamp of the compute this report came from
    elapsed_s: float | None
    stale: bool             # the inputs moved since that compute
    source: str             # "cache" | "computed"


def _cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "stockcheck" / CACHE_NAME


def load_cached(data_dir: Path) -> CachedReport | None:
    """The saved report, stale-flagged against the LIVE inputs; None when
    absent or corrupt (a half-written cache reads as no cache)."""
    try:
        raw = json.loads(_cache_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    report = raw.get("report") if isinstance(raw, dict) else None
    if not isinstance(report, dict):
        return None
    from helpers.reconcile_engine import stock_report_inputs
    vif, toggles = stock_report_inputs(data_dir)
    stale = raw.get("signature") != current_signature(data_dir, vif, toggles)
    elapsed = raw.get("elapsed_s")
    return CachedReport(
        report=report, computed_at=str(raw.get("computed_at") or ""),
        elapsed_s=float(elapsed) if isinstance(elapsed, (int, float)) else None,
        stale=stale, source="cache")


def refresh_report(data_dir: Path) -> CachedReport:
    """Compute the report through the shared resolver and persist it.
    Error reports (unreachable share, unimportable exports) are returned
    but never saved — the last good report stays on disk. The signature is
    taken BEFORE the compute, so an input moving mid-crunch reads stale on
    the next visit instead of silently passing as fresh."""
    from helpers.reconcile_engine import stock_report_inputs
    from helpers.safe_io import safe_write_json
    import stockcheck.api as _api
    dd = Path(data_dir)
    vif, toggles = stock_report_inputs(dd)
    sig = current_signature(dd, vif, toggles)
    t0 = time.perf_counter()
    try:
        report = _api.stock_check_report(dd, vif, toggles=toggles or None)
    except Exception as exc:  # noqa: BLE001 — the VIF share may be unreachable
        report = {"error": str(exc)}
    elapsed = round(time.perf_counter() - t0, 1)
    computed_at = datetime.now().isoformat(timespec="seconds")
    if isinstance(report, dict) and not report.get("error"):
        safe_write_json({"signature": sig, "computed_at": computed_at,
                         "elapsed_s": elapsed, "report": report},
                        _cache_path(dd))
    return CachedReport(report=report, computed_at=computed_at,
                        elapsed_s=elapsed, stale=False, source="computed")


def get_report(data_dir: Path, *, auto: bool) -> CachedReport:
    """The page entry point. auto=True (Home): recompute whenever the saved
    report is stale — the Command Center always shows current numbers.
    auto=False (Stock Check / Reconcile): serve whatever is saved, however
    stale — only refresh_report() (their Refresh button) recomputes.
    Either way, nothing saved yet computes once: there is no saved report
    to show instead."""
    cached = load_cached(data_dir)
    if cached is None or (auto and cached.stale):
        return refresh_report(data_dir)
    return cached
