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
from helpers.timefmt import parse_anchor, planning_anchor, with_display_times

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


def _frame_stamp(planning_anchor: "datetime | str | None" = None) -> str:
    """'YYYY-mm-dd HH:MM:SS' for the frame a version's hours live in.

    None -> the rolling horizon anchor (helpers.horizon.resolve on the live
    toml). Any failure raises ValueError: a version without a frame stamp
    promotes into the wrong week silently (quality-12), so it is not
    saveable. Never swallowed."""
    from helpers.timefmt import parse_datetime as _pdt

    try:
        if planning_anchor is None:
            from helpers import horizon as _hzm
            from helpers.config import load_toml as _ltm
            anchor = _hzm.resolve(_ltm()).anchor
        elif isinstance(planning_anchor, datetime):
            anchor = planning_anchor
        else:
            # parse_datetime, not parse_anchor: the latter falls back to the
            # 2026-02-15 default on garbage, which would stamp a wrong frame
            anchor = _pdt(planning_anchor)
            if anchor is None:
                raise ValueError(f"unparseable anchor {planning_anchor!r}")
    except Exception as exc:  # noqa: BLE001 — re-raised with context
        raise ValueError(
            f"cannot stamp the version's time frame (planning_anchor): {exc}"
        ) from exc
    return f"{anchor:%Y-%m-%d %H:%M:%S}"


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
    planning_anchor: "datetime | str | None" = None,
) -> str:
    """Save a NEW version. ``auto_evict`` (scenario-runner path only): when
    the slots are full, delete the oldest AUTO-SAVED version (source
    solver:/agent:) to make room — never a user-named one. With nothing
    evictable (or auto_evict off) the friendly capacity error raises.

    ``planning_anchor``: the frame the calendar's hours are measured from.
    Default = the rolling horizon anchor (what a scenario solve uses). The
    Plant Calendar passes its STORAGE anchor: its board is in the toml frame,
    and stamping the rolling anchor on it shifted every block by the un-
    rolled gap at promote (writeback-1 family, audit 2026-09-03)."""
    existing = list_versions(data_dir)
    if auto_evict:
        while len(existing) >= MAX_VERSIONS and _evict_oldest_auto(existing, data_dir):
            existing = list_versions(data_dir)
    _check_capacity(existing)
    # Frame stamp FIRST: a stamp failure must leave no half-written version
    # (an orphan dir would hold a slot and mislead list_versions).
    frame = _frame_stamp(planning_anchor)
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
    # MOs "shifted"). Frame reconciliation happens at promote/load. The
    # stamp is load-bearing, so it RAISES when it cannot be produced
    # (quality-12): an unstamped version used to promote with shift 0.
    meta["planning_anchor"] = frame
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
    planning_anchor: "datetime | str | None" = None,
) -> str:
    """Create or overwrite a version at a fixed slug (does not count toward
    MAX when updating). ``planning_anchor`` as in save_version."""
    _validate_slug(slug)
    dest = versions_dir(data_dir) / slug
    creating = not dest.exists()
    if creating:
        _check_capacity(list_versions(data_dir))
    frame = _frame_stamp(planning_anchor)   # before any disk write
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
    # MOs "shifted"). Frame reconciliation happens at promote/load. The
    # stamp is load-bearing, so it RAISES when it cannot be produced
    # (quality-12): an unstamped version used to promote with shift 0.
    meta["planning_anchor"] = frame
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


def board_frame_shift_h(slug: str, data_dir: Path) -> float | None:
    """Hours to ADD to this version's block hours so they land in the
    official board's frame (toml planning_start_date). 0 when the frames
    match. None when the version carries NO readable planning_anchor stamp:
    the caller must refuse to promote (quality-12) — "assume board frame"
    silently re-created the very bug the stamp was introduced for."""
    from datetime import datetime as _dt

    meta = _read_meta(versions_dir(data_dir) / slug)
    va = str(meta.get("planning_anchor") or "").strip()
    if not va:
        return None
    try:
        v_anchor = _dt.strptime(va, "%Y-%m-%d %H:%M:%S")
        b_anchor = planning_anchor()
        return (v_anchor - b_anchor).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def version_anchor(slug: str, data_dir: Path) -> datetime | None:
    """The version's own planning_anchor stamp as a datetime, or None."""
    meta = _read_meta(versions_dir(data_dir) / slug)
    va = str(meta.get("planning_anchor") or "").strip()
    if not va:
        return None
    try:
        return datetime.strptime(va, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _verbatim_board_rows(calendar: pd.DataFrame, board: pd.DataFrame | None
                         ) -> pd.Series:
    """Mask of version rows that are VERBATIM copies of board rows: same
    block_id, same start_h/end_h, and no current_state:* token.

    Fix writeback-1 (audit 2026-09-03): Scenario F's committed layer mixes
    two frames — build_current_state rows (rolling anchor) and the planner's
    pinned/CIP rows copied straight out of calendar_blocks.csv (board
    anchor). One planning_anchor is stamped for the whole file, so the
    frame shift landed those board-frame rows a full day late on promote.
    A row that still has the exact id AND hours of a board row is by
    construction already in the board frame — the shift is applied to it
    ZERO times, everything else exactly once. current_state rows are
    always re-staged in the version's own frame, so they never qualify
    (their ids are deterministic and could collide). The test is robust to
    the staging-side fix (rebasing pinned rows before staging): rebased
    rows no longer share the board's hours and are shifted normally.
    """
    mask = pd.Series(False, index=calendar.index)
    if board is None or len(board) == 0 or len(calendar) == 0:
        return mask
    need = {"block_id", "start_h", "end_h"}
    if not need.issubset(calendar.columns) or not need.issubset(board.columns):
        return mask
    pos: dict[str, list[tuple[float, float]]] = {}
    for r in board.to_dict("records"):
        try:
            pos.setdefault(str(r.get("block_id")), []).append(
                (float(r.get("start_h")), float(r.get("end_h"))))
        except (TypeError, ValueError):
            continue
    for idx, r in calendar.iterrows():
        if "current_state:" in str(r.get("attrs") or ""):
            continue
        hits = pos.get(str(r.get("block_id")))
        if not hits:
            continue
        try:
            s, e = float(r.get("start_h")), float(r.get("end_h"))
        except (TypeError, ValueError):
            continue
        if any(abs(s - bs) < 1e-6 and abs(e - be) < 1e-6 for bs, be in hits):
            mask.at[idx] = True
    return mask


def calendar_in_board_frame(
    calendar: pd.DataFrame, slug: str, data_dir: Path, *,
    assume_board_frame: bool = False,
    board: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, float]:
    """Re-base a version's calendar into the official board's frame.

    Returns (calendar, shift_h). A version solved on a newer rolling anchor
    than the (un-rolled) board shifts FORWARD by the gap so wall-clock
    positions are preserved; after the board rolls, older versions shift
    back the same way. Hours-only transform — nothing else changes.

    Refuses (ValueError) when the version has no readable planning_anchor
    stamp unless ``assume_board_frame`` is passed explicitly (quality-12).
    With ``board`` given, rows that are verbatim copies of board rows are
    left where they are (see _verbatim_board_rows, writeback-1); the count
    is reported in ``calendar.attrs["verbatim_rows"]``.
    """
    shift = board_frame_shift_h(slug, data_dir)
    if shift is None:
        if not assume_board_frame:
            raise ValueError(
                f"version {slug!r} has no readable planning_anchor stamp, so "
                "its hours cannot be placed on the board's time line. "
                "Re-save the version (or promote with assume_board_frame=True "
                "if you are certain it was saved in the board's own frame).")
        shift = 0.0
    cal = calendar.copy()
    if abs(shift) < 1e-9:
        cal.attrs["verbatim_rows"] = 0
        return cal, 0.0
    verbatim = _verbatim_board_rows(cal, board)
    for col in ("start_h", "end_h"):
        if col in cal.columns:
            vals = pd.to_numeric(cal[col], errors="coerce")
            cal[col] = vals.where(verbatim, vals + shift)
    cal.attrs["verbatim_rows"] = int(verbatim.sum())
    return cal, shift


def promote_version(slug: str, data_dir: Path, *,
                    lock_override: bool = False,
                    assume_board_frame: bool = False) -> dict[str, Any]:
    """Version calendar becomes the official calendar_blocks.csv — hours
    re-based into the board's frame (see calendar_in_board_frame), board
    identity carried over (reconcile_block_identity), and the 2-week lock
    enforced (LockViolation unless lock_override). Returns a result dict:
    {"promoted", "shift_h", "verbatim_rows", "matched", "new", "backup",
    "lock_h", "warnings"}; {"promoted": False} when the version has no
    calendar file. compare.py may ignore the return value (unchanged)."""
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    src = vdir / "calendar_blocks.csv"
    result: dict[str, Any] = {"promoted": False, "shift_h": 0.0,
                              "verbatim_rows": 0, "matched": 0, "new": 0,
                              "backup": None, "lock_h": None, "warnings": []}
    if not src.exists():
        return result
    from helpers.calendar_io import reconcile_block_identity
    from helpers.week_lock import locked_through_h, read_lock

    official = Path(data_dir) / "calendar_blocks.csv"
    board = load_calendar(official) if official.exists() else None
    cal = load_calendar(src, warnings=result["warnings"])
    cal, shift = calendar_in_board_frame(
        cal, slug, data_dir, assume_board_frame=assume_board_frame, board=board)
    result["shift_h"] = float(shift)
    result["verbatim_rows"] = int(cal.attrs.get("verbatim_rows", 0))
    if board is not None and len(board):
        # both frames are the board's now -> no extra shift while matching
        cal, stats = reconcile_block_identity(cal, board, board_shift_h=0.0)
        result.update(stats)
    # 2-week lock in the BOARD frame (lock_state.json is wall clock).
    lock_h = locked_through_h(read_lock(Path(data_dir)), planning_anchor())
    result["lock_h"] = lock_h
    # The lock check runs BEFORE the backup so a refused promote leaves no
    # trace; the pre-promote backup keeps its historical name and
    # save_calendar's own auto backup is off, so one promote = one copy.
    if official.exists():
        from helpers.week_lock import LockViolation, check_lock_violations
        viol = check_lock_violations(cal, board, lock_h)
        if viol and not lock_override:
            raise LockViolation(viol, lock_h)
        bdir = Path(data_dir) / "_backups"
        bdir.mkdir(parents=True, exist_ok=True)
        bpath = bdir / (f"calendar_blocks.pre-promote."
                        f"{datetime.now():%Y%m%d-%H%M%S}.csv")
        n = 1
        while bpath.exists():
            n += 1
            bpath = bdir / (f"calendar_blocks.pre-promote."
                            f"{datetime.now():%Y%m%d-%H%M%S}-{n}.csv")
        shutil.copy2(official, bpath)
        result["backup"] = str(bpath)
    saved = save_calendar(cal, official, backup_dir=None, lock_h=lock_h,
                          lock_override=lock_override, previous=board)
    result["warnings"].extend(saved.get("warnings") or [])
    result["promoted"] = True
    return result


def export_calendar_excel(cal_df: pd.DataFrame, scorecard: dict | None = None,
                          *, anchor: "datetime | str | None" = None,
                          now_h: float | None = None) -> bytes:
    """Excel bytes for any calendar frame — the live board or a version.

    Raw start_h/end_h stay (the solver round-trips on them); human start/end
    datetime columns are added alongside for readers, rendered from
    ``anchor`` — the frame the hours are measured from. Default = the
    board's toml anchor (right for the Plant Calendar); export_version_excel
    passes the version's own stamp (fix writeback-2: the workbook and the
    promoted schedule used to name different wall-clock times).

    ``now_h`` (hours, same frame) adds a ``status`` column: 'completed' for
    blocks that ended at or before now, 'planned' otherwise (fix
    writeback-12: finished blocks are INCLUDED and flagged, never omitted).
    A 'Frame' sheet records the anchor so the file is self-describing.
    """
    a = parse_anchor(anchor) if anchor is not None else planning_anchor()
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        if cal_df is not None and len(cal_df):
            out = with_display_times(cal_df, a, iso=True)
            if now_h is not None and "end_h" in out.columns:
                ends = pd.to_numeric(out["end_h"], errors="coerce")
                out["status"] = [
                    "completed" if (e == e and e <= float(now_h)) else "planned"
                    for e in ends]
            out.to_excel(writer, sheet_name="Calendar", index=False)
        flat = []
        for section in ("changeovers", "cip", "trials", "maintenance", "campaigns", "service"):
            for k, v in ((scorecard or {}).get(section) or {}).items():
                flat.append({"section": section, "metric": k, "value": v})
        if flat:
            pd.DataFrame(flat).to_excel(writer, sheet_name="Scorecard", index=False)
        pd.DataFrame([
            {"key": "planning_anchor", "value": f"{a:%Y-%m-%d %H:%M:%S}"},
            {"key": "note", "value": "start_h/end_h are hour offsets from "
                                     "planning_anchor; start/end are the same "
                                     "moments as wall-clock datetimes"},
        ]).to_excel(writer, sheet_name="Frame", index=False)
    return buf.getvalue()


def export_version_excel(slug: str, data_dir: Path, *,
                         now: datetime | None = None) -> bytes:
    """Workbook for a saved version, rendered in the VERSION'S OWN frame
    (its planning_anchor stamp; the board anchor only when a legacy version
    carries none). ``now`` (default wall clock) drives the 'completed'
    status flag in the version's frame."""
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    cal = vdir / "calendar_blocks.csv"
    cal_df = pd.read_csv(cal) if cal.exists() else pd.DataFrame()
    meta_path = vdir / "metadata.json"
    sc: dict = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        sc = meta.get("scorecard", {}) or {}
    v_anchor = version_anchor(slug, data_dir) or planning_anchor()
    now_dt = now or datetime.now()
    now_h = (now_dt - v_anchor).total_seconds() / 3600.0
    return export_calendar_excel(cal_df, sc, anchor=v_anchor, now_h=now_h)
