# helpers/version_manager.py — Named calendar versions with scorecard snapshots.

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from helpers.calendar_io import load_calendar, save_calendar
from helpers.paths import versions_dir
from helpers.safe_io import safe_write_json
from helpers.timefmt import planning_anchor, with_display_times

MAX_VERSIONS = 5
_SLUG_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid version slug {slug!r}: must be 1-64 lowercase alphanumeric/underscore."
        )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    # _validate_slug caps slugs at 64 chars; leave room for _unique_slug's
    # numeric suffix (timestamped scenario names got long, 2026-08-19).
    return slug[:60].rstrip("_") or "version"


def _unique_slug(name: str, data_dir: Path) -> str:
    base = _slugify(name)
    vd = versions_dir(data_dir)
    if not (vd / base).exists():
        return base
    for i in range(2, MAX_VERSIONS + 10):
        candidate = f"{base}_{i}"
        if not (vd / candidate).exists():
            return candidate
    return f"{base}_{int(datetime.now().timestamp())}"


def list_versions(data_dir: Path) -> list[dict[str, Any]]:
    """Every directory under data/versions is a version slot — ONE truth.

    Directories without a readable metadata.json (crash leftovers, hand
    copies, corrupt metadata) are surfaced as ``orphan`` entries instead of
    being skipped: hidden folders silently disagreeing with the '5/5 saved'
    counter is how 8 on-disk directories showed as 5 (walkthrough finding 5,
    2026-08-18). Orphans count toward MAX_VERSIONS and can be deleted from
    Version Compare like any other version.
    """
    vd = versions_dir(data_dir)
    versions: list[dict] = []
    for d in sorted(vd.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "metadata.json"
        meta: dict[str, Any] | None = None
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta = loaded
            except (json.JSONDecodeError, OSError):
                meta = None
        if meta is None:
            meta = {"name": d.name, "timestamp": "", "source": "",
                    "scorecard": {}, "orphan": True}
        meta["slug"] = d.name
        versions.append(meta)
    versions.sort(key=lambda v: str(v.get("timestamp") or ""), reverse=True)
    return versions


def _check_capacity(existing: list[dict[str, Any]]) -> None:
    if len(existing) < MAX_VERSIONS:
        return
    n_orph = sum(1 for v in existing if v.get("orphan"))
    msg = (f"Maximum of {MAX_VERSIONS} versions reached "
           f"({len(existing)} folder(s) under data/versions). "
           "Delete one in Version Compare before saving.")
    if n_orph:
        msg += (f" {n_orph} of them are orphaned folder(s) without metadata "
                "— safe to delete there.")
    raise ValueError(msg)


# Sources stamped by unattended save paths (scenario runner / agent). Only
# these are ever auto-evicted; anything a user named and saved by hand
# ("digital_twin", "manual", imports, the naive strawman) is never touched.
AUTO_SOURCE_PREFIXES = ("solver:", "solver-custom:", "agent:")


def is_auto_saved(meta: dict[str, Any]) -> bool:
    # A rename or a pros/cons edit stamps user_touched — curation protects a
    # version from eviction even though its source stays solver:/agent:.
    if meta.get("user_touched"):
        return False
    return str(meta.get("source") or "").startswith(AUTO_SOURCE_PREFIXES)


def _evict_oldest_auto(existing: list[dict[str, Any]], data_dir: Path) -> bool:
    """Delete the OLDEST auto-saved version. False when nothing is evictable
    (all slots hold user-named/curated versions or orphans)."""
    autos = [v for v in existing if is_auto_saved(v) and not v.get("orphan")]
    autos.sort(key=lambda v: str(v.get("timestamp") or ""))
    for cand in autos:
        try:
            delete_version(cand["slug"], data_dir)
            return True
        except ValueError:
            # Hand-copied dir whose name is not a valid slug: skip it rather
            # than wedge every future auto-save (review 2026-08-19).
            continue
    return False


def save_version(
    name: str,
    calendar: pd.DataFrame,
    scorecard: dict[str, Any],
    data_dir: Path,
    *,
    pros: str = "",
    cons: str = "",
    notes: str = "",
    source: str = "manual",
    extra_meta: dict[str, Any] | None = None,
    auto_evict: bool = False,
) -> str:
    """Save a NEW version. ``auto_evict`` (scenario-runner path only): when
    the slots are full, delete the oldest AUTO-SAVED version (source
    solver:/agent:) to make room — never a user-named one. With nothing
    evictable (or auto_evict off) the friendly capacity error raises."""
    existing = list_versions(data_dir)
    if auto_evict:
        while len(existing) >= MAX_VERSIONS and _evict_oldest_auto(existing, data_dir):
            existing = list_versions(data_dir)
    _check_capacity(existing)
    slug = _unique_slug(name, data_dir)
    dest = versions_dir(data_dir) / slug
    dest.mkdir(parents=True, exist_ok=True)
    save_calendar(calendar, dest / "calendar_blocks.csv")
    meta = {
        "name": name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "pros": pros,
        "cons": cons,
        "notes": notes,
        "scorecard": scorecard,
    }
    # Stamp the TIME FRAME the hours are in (audit 2026-08-17): with a
    # rolling anchor a version solved today and a board anchored last
    # Monday disagree by whole days — promoting hours verbatim rendered a
    # Monday solve one week in the past (fill "back-filled W33", committed
    # MOs "shifted"). Frame reconciliation happens at promote/load.
    try:
        from helpers import horizon as _hzm
        from helpers.config import load_toml as _ltm
        meta["planning_anchor"] = (
            f"{_hzm.resolve(_ltm()).anchor:%Y-%m-%d %H:%M:%S}")
    except Exception:  # noqa: BLE001
        pass
    # Structured extras (e.g. Scenario F fill_gates) ride alongside the core
    # keys; they must never shadow them.
    for k, v in (extra_meta or {}).items():
        meta.setdefault(k, v)
    safe_write_json(meta, dest / "metadata.json")
    return slug


def upsert_version(
    slug: str,
    name: str,
    calendar: pd.DataFrame,
    scorecard: dict[str, Any],
    data_dir: Path,
    *,
    pros: str = "",
    cons: str = "",
    notes: str = "",
    source: str = "manual",
    extra_meta: dict[str, Any] | None = None,
) -> str:
    """Create or overwrite a version at a fixed slug (does not count toward MAX when updating)."""
    _validate_slug(slug)
    dest = versions_dir(data_dir) / slug
    creating = not dest.exists()
    if creating:
        _check_capacity(list_versions(data_dir))
    dest.mkdir(parents=True, exist_ok=True)
    save_calendar(calendar, dest / "calendar_blocks.csv")
    meta = {
        "name": name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "pros": pros,
        "cons": cons,
        "notes": notes,
        "scorecard": scorecard,
    }
    # Stamp the TIME FRAME the hours are in (audit 2026-08-17): with a
    # rolling anchor a version solved today and a board anchored last
    # Monday disagree by whole days — promoting hours verbatim rendered a
    # Monday solve one week in the past (fill "back-filled W33", committed
    # MOs "shifted"). Frame reconciliation happens at promote/load.
    try:
        from helpers import horizon as _hzm
        from helpers.config import load_toml as _ltm
        meta["planning_anchor"] = (
            f"{_hzm.resolve(_ltm()).anchor:%Y-%m-%d %H:%M:%S}")
    except Exception:  # noqa: BLE001
        pass
    # Structured extras (e.g. fill_gates) ride alongside the core keys;
    # they must never shadow them — same rule as save_version.
    for k, v in (extra_meta or {}).items():
        meta.setdefault(k, v)
    safe_write_json(meta, dest / "metadata.json")
    return slug


def load_version(slug: str, data_dir: Path) -> dict[str, Any]:
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    result: dict[str, Any] = {"calendar": pd.DataFrame(), "metadata": {}}
    cal_path = vdir / "calendar_blocks.csv"
    if cal_path.exists():
        result["calendar"] = load_calendar(cal_path)
    meta_path = vdir / "metadata.json"
    if meta_path.exists():
        result["metadata"] = json.loads(meta_path.read_text(encoding="utf-8"))
    return result


def rename_version(slug: str, new_name: str, data_dir: Path) -> None:
    # Renaming is a curation act: the planner marked this version as theirs,
    # so auto-evict must never reclaim its slot (review 2026-08-19 — a
    # renamed solver run kept source=solver:* and was silently deleted).
    _validate_slug(slug)
    meta_path = versions_dir(data_dir) / slug / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["name"] = new_name
        meta["user_touched"] = True
        safe_write_json(meta, meta_path)


def update_notes(slug: str, data_dir: Path, *, pros: str | None = None, cons: str | None = None, notes: str | None = None) -> None:
    _validate_slug(slug)
    meta_path = versions_dir(data_dir) / slug / "metadata.json"
    if not meta_path.exists():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if pros is not None:
        meta["pros"] = pros
    if cons is not None:
        meta["cons"] = cons
    if notes is not None:
        meta["notes"] = notes
    # Annotating = curating, same protection as a rename.
    meta["user_touched"] = True
    safe_write_json(meta, meta_path)


def _rmtree_force(path: Path) -> None:
    # Windows: rmtree dies with WinError 5 on read-only entries (orphan
    # version dirs restored from backup carry the R attribute) — clear the
    # bit and retry.
    def _clear_ro(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    try:
        shutil.rmtree(path, onexc=_clear_ro)  # 3.12+
    except TypeError:
        shutil.rmtree(path, onerror=_clear_ro)


def delete_version(slug: str, data_dir: Path) -> None:
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    if vdir.exists() and vdir.is_dir():
        _rmtree_force(vdir)


def delete_all_versions(data_dir: Path) -> None:
    vd = versions_dir(data_dir)
    if vd.exists():
        _rmtree_force(vd)
        vd.mkdir(parents=True, exist_ok=True)


def _read_meta(vdir: Path) -> dict:
    import json
    p = vdir / "metadata.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def board_frame_shift_h(slug: str, data_dir: Path) -> float:
    """Hours to ADD to this version's block hours so they land in the
    official board's frame (toml planning_start_date). 0 when the frames
    match or the version predates frame stamping (assume board frame)."""
    from datetime import datetime as _dt

    meta = _read_meta(versions_dir(data_dir) / slug)
    va = str(meta.get("planning_anchor") or "").strip()
    if not va:
        return 0.0
    try:
        v_anchor = _dt.strptime(va, "%Y-%m-%d %H:%M:%S")
        b_anchor = planning_anchor()
        return (v_anchor - b_anchor).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 0.0


def calendar_in_board_frame(
    calendar: pd.DataFrame, slug: str, data_dir: Path
) -> tuple[pd.DataFrame, float]:
    """Re-base a version's calendar into the official board's frame.

    Returns (calendar, shift_h). A version solved on a newer rolling anchor
    than the (un-rolled) board shifts FORWARD by the gap so wall-clock
    positions are preserved; after the board rolls, older versions shift
    back the same way. Hours-only transform — nothing else changes.
    """
    shift = board_frame_shift_h(slug, data_dir)
    if abs(shift) < 1e-9:
        return calendar, 0.0
    cal = calendar.copy()
    for col in ("start_h", "end_h"):
        if col in cal.columns:
            cal[col] = pd.to_numeric(cal[col], errors="coerce") + shift
    return cal, shift


def promote_version(slug: str, data_dir: Path) -> None:
    """Version calendar becomes the official calendar_blocks.csv — hours
    re-based into the board's frame (see calendar_in_board_frame)."""
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    src = vdir / "calendar_blocks.csv"
    if not src.exists():
        return
    from helpers.calendar_io import load_calendar
    official = Path(data_dir) / "calendar_blocks.csv"
    if official.exists():
        bdir = Path(data_dir) / "_backups"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(official, bdir / (
            f"calendar_blocks.pre-promote."
            f"{datetime.now():%Y%m%d-%H%M%S}.csv"))
    cal = load_calendar(src)
    cal, _shift = calendar_in_board_frame(cal, slug, data_dir)
    save_calendar(cal, official)


def export_calendar_excel(cal_df: pd.DataFrame, scorecard: dict | None = None) -> bytes:
    """Excel bytes for any calendar frame — the live board or a version.

    Raw start_h/end_h stay (the solver round-trips on them); human start/end
    datetime columns are added alongside for readers.
    """
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        if cal_df is not None and len(cal_df):
            out = with_display_times(cal_df, planning_anchor(), iso=True)
            out.to_excel(writer, sheet_name="Calendar", index=False)
        flat = []
        for section in ("changeovers", "cip", "trials", "maintenance", "campaigns", "service"):
            for k, v in ((scorecard or {}).get(section) or {}).items():
                flat.append({"section": section, "metric": k, "value": v})
        if flat:
            pd.DataFrame(flat).to_excel(writer, sheet_name="Scorecard", index=False)
    return buf.getvalue()


def export_version_excel(slug: str, data_dir: Path) -> bytes:
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    cal = vdir / "calendar_blocks.csv"
    cal_df = pd.read_csv(cal) if cal.exists() else pd.DataFrame()
    meta_path = vdir / "metadata.json"
    sc: dict = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        sc = meta.get("scorecard", {}) or {}
    return export_calendar_excel(cal_df, sc)
