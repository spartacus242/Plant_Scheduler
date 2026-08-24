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
from datetime import datetime
from pathlib import Path

CACHE_NAME = "report.cache.json"

# Everything the report reads besides the toggles: the six VIF exports
# (refresh_vif_snapshot's own mtime guard uses the same list), the board
# (schedule view), the demand plan (demand view) and the rates file (kg per
# block). A missing file signs 0.0 — appearing and disappearing both move
# the signature.
_VIF_FILES = ("ediact 3.csv", "ediact 4.csv", "jestkexp.csv",
              "jestkexp2.csv", "azapart.csv", "rmpkitems.csv")


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def current_signature(data_dir: Path, vif_folder: str,
                      toggles: dict | None) -> list:
    dd = Path(data_dir)
    vf = Path(vif_folder)
    sig: list = [str(vif_folder),
                 json.dumps(toggles or {}, sort_keys=True)]
    sig += [_mtime(vf / n) for n in _VIF_FILES]
    sig += [_mtime(dd / "calendar_blocks.csv"),
            _mtime(dd / "reference" / "demand_plan.csv"),
            _mtime(dd / "reference" / "capabilities_rates.csv")]
    return sig


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
