# helpers/calendar_io.py - Unified calendar_blocks load/save + solver import.

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from helpers.safe_io import safe_write_csv

BLOCK_TYPES = (
    "production",
    "cip",
    "maintenance",
    "trial",
    "contractor",
    "line_down",
)

CALENDAR_COLUMNS = [
    "block_id",
    "block_type",
    "line_id",
    "line_name",
    "start_h",
    "end_h",
    "label",
    "order_id",
    "sku",
    "sku_description",
    "qty_kg",
    "locked",
    "attrs",
]


def empty_calendar() -> pd.DataFrame:
    return pd.DataFrame(columns=CALENDAR_COLUMNS)


def load_calendar(path: Path, *, warnings: list[str] | None = None,
                  strict: bool = False) -> pd.DataFrame:
    """Read calendar_blocks.csv. Rows whose start_h/end_h is blank or
    unparsable are DROPPED with a warning (fix quality-8, audit 2026-09-03):
    the old `.fillna(0)` silently relocated a corrupt row to hour 0 — on top
    of the running MO — and the next board save made the relocation
    permanent. A block's hours are its physical position; there is no safe
    default. Warnings are appended to `warnings` (when given) and stored on
    the frame as ``df.attrs["load_warnings"]``; ``strict=True`` raises
    ValueError instead of dropping.
    """
    if not path.exists():
        return empty_calendar()
    # Read all columns as strings; numeric columns are coerced explicitly below.
    # Code columns (sku, order_id, label, block_id) get an integer dtype in a
    # CSV with empty cells, producing "280351.0" labels. dtype=str prevents that.
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in CALENDAR_COLUMNS:
        if col not in df.columns:
            df[col] = None
    if "locked" in df.columns:
        df["locked"] = df["locked"].str.lower().isin({"true", "1", "yes"})
    else:
        df["locked"] = False
    df["start_h"] = pd.to_numeric(df["start_h"], errors="coerce").astype(float)
    df["end_h"] = pd.to_numeric(df["end_h"], errors="coerce").astype(float)
    notes: list[str] = []
    bad = df["start_h"].isna() | df["end_h"].isna()
    if bad.any():
        for _, r in df[bad].iterrows():
            notes.append(
                f"{path.name}: block {r.get('block_id') or '?'} "
                f"({r.get('line_name') or '?'} {r.get('block_type') or '?'} "
                f"{r.get('order_id') or r.get('sku') or ''}) has an unreadable "
                f"start_h/end_h ({r.get('start_h')!r}/{r.get('end_h')!r}) — "
                "row dropped, NOT moved to hour 0")
        if strict:
            raise ValueError("corrupt calendar rows:\n" + "\n".join(notes))
        df = df[~bad].copy()
    if warnings is not None:
        warnings.extend(notes)
    # qty_kg keeps NaN for "unknown kg" - do NOT fillna(0). A 0 here is
    # indistinguishable from "produced nothing", which made the scorecard's
    # Service excess-kg path read unknown production as "no excess" instead of
    # falling back to its estimate. Consumers treat NaN as missing; display
    # sites blank it out (never write 0 back into the data model).
    df["qty_kg"] = pd.to_numeric(df.get("qty_kg", np.nan), errors="coerce").astype(float)
    # Strip trailing ".0" from code columns that were written by a previous
    # pandas save with float dtype (e.g. "570468.0" in sku). dtype=str alone
    # doesn't fix this because the CSV literally stores the string "570468.0".
    for _col in ("sku", "order_id", "label"):
        if _col in df.columns:
            df[_col] = df[_col].str.replace(r"\.0$", "", regex=True)
    out = df[CALENDAR_COLUMNS]
    out.attrs["load_warnings"] = notes
    return out


def save_calendar(df: pd.DataFrame, path: Path, *,
                  backup_dir: Path | str | None = "auto",
                  expected_mtime: float | None = None,
                  lock_h: float | None = None,
                  lock_override: bool = False,
                  previous: pd.DataFrame | None = None,
                  allow_duplicate_ids: bool = False) -> dict[str, Any]:
    """Atomic calendar write (safe_write_csv) with the write-back guards the
    audit asked for (writeback-9 / writeback-13, 2026-09-03). Returns a
    result dict: {"path", "backup", "stale", "warnings", "lock_violations"}.
    Callers that ignore the return value behave exactly as before.

    backup_dir     "auto" (default): when the target exists and sits directly
                   in a ``data`` directory (the official board) copy it to
                   ``<dir>/_backups/calendar_blocks.<stamp>.csv`` first —
                   version and work-dir files are not backed up. A Path
                   forces a backup there; None disables.
    expected_mtime st_mtime of the file when the caller loaded it. When the
                   file changed since, the write still happens (last writer
                   wins) but result["stale"] is True and a warning names the
                   backup that holds the clobbered board.
    lock_h         locked-through hour (same frame as `df`). Any change to a
                   block that starts before it — moved, resized, added,
                   removed — raises LockViolation unless lock_override. The
                   comparison baseline is `previous` or the file on disk.
    """
    from helpers.safe_io import backup_file
    from helpers.week_lock import LockViolation, check_lock_violations

    path = Path(path)
    result: dict[str, Any] = {"path": str(path), "backup": None, "stale": False,
                              "warnings": [], "lock_violations": []}
    out = df.copy()
    for col in CALENDAR_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out[CALENDAR_COLUMNS]
    # Server-side twin of the Gantt's push guard (fix FE-1 / writeback-8;
    # INTEGRATE applying the FE->W handoff): a block_id shared by rows with a
    # DIFFERENT (order_id, sku, block_type) is two unrelated blocks under one
    # name — set_float_link would pin both. Split pieces of ONE MO share an
    # id legitimately and pass. Refused before any write or backup unless
    # allow_duplicate_ids.
    dup = duplicate_block_ids(out)
    if dup and not allow_duplicate_ids:
        raise ValueError(
            "calendar not saved: block_id used by unrelated blocks: "
            + ", ".join(dup[:8]) + (" ..." if len(dup) > 8 else ""))
    if dup:
        result["warnings"].append(
            f"{len(dup)} block_id(s) shared by unrelated blocks written on request")

    if lock_h is not None and path.exists():
        base = previous if previous is not None else load_calendar(path)
        viol = check_lock_violations(out, base, lock_h)
        result["lock_violations"] = viol
        if viol and not lock_override:
            raise LockViolation(viol, lock_h)
        if viol:
            result["warnings"].append(
                f"lock override: {len(viol)} change(s) written inside the "
                "locked window")

    if expected_mtime is not None and path.exists():
        try:
            cur = path.stat().st_mtime
        except OSError:
            cur = None
        if cur is not None and abs(cur - float(expected_mtime)) > 1e-6:
            result["stale"] = True

    bdir: Path | None
    if backup_dir == "auto":
        bdir = (path.parent / "_backups") if path.parent.name == "data" else None
    else:
        bdir = Path(backup_dir) if backup_dir is not None else None
    if bdir is not None and path.exists():
        b = backup_file(path, bdir)
        result["backup"] = str(b) if b else None

    if result["stale"]:
        result["warnings"].append(
            f"{path.name} changed on disk after this board was loaded (another "
            "tab, a promote or the bridge) — your save replaced it (last writer "
            f"wins); the overwritten board is at {result['backup'] or 'no backup'}")

    safe_write_csv(out, path)
    return result


def duplicate_block_ids(df: pd.DataFrame) -> list[str]:
    """block_ids shared by rows whose (order_id, sku, block_type) differ —
    unrelated blocks under one name (writeback-8). Split pieces of one MO
    (same order, same SKU, same type) are NOT reported. NaN/blank ids are
    ignored (they are minted on import)."""
    if df is None or df.empty or "block_id" not in df.columns:
        return []
    d = df.copy()
    d["block_id"] = d["block_id"].astype(str).str.strip()
    d = d[(d["block_id"] != "") & (d["block_id"].str.lower() != "nan")]
    if d.empty:
        return []
    for col in ("order_id", "sku", "block_type"):
        if col not in d.columns:
            d[col] = ""
        d[col] = d[col].fillna("").astype(str).str.strip()
    n = d.groupby("block_id")[["order_id", "sku", "block_type"]].nunique()
    bad = n[(n > 1).any(axis=1)]
    return sorted(bad.index.tolist())


def reconcile_block_identity(
    new_cal: pd.DataFrame,
    board: pd.DataFrame | None,
    *,
    board_shift_h: float = 0.0,
    tol_h: float = 0.5,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Give blocks that already exist on the board their board identity.

    Fix writeback-4 (audit 2026-09-03): import_solver_schedule minted a fresh
    uuid and locked=False for every row, so promoting a version cleared every
    planner lock, changed every block_id (dangling 'after:<id>' float links)
    and made version-to-version diffing by id meaningless — even for work the
    solver left exactly where it was. A new row MATCHES a board row when they
    share order_id (non-empty) and line and their starts agree within
    `tol_h`; CIP rows (no order) match by type + line + start. Each board row
    is used at most once (nearest start wins), so split pieces of one MO
    resolve piece-by-piece. A match carries over block_id, locked and the
    planner tokens (pinned, after:*) from the board; everything else keeps
    its fresh id (genuinely new work). `board_shift_h` is added to the board's
    hours before comparing (the board may live in another frame).
    Returns (calendar copy, {"matched": n, "new": n}).
    """
    stats = {"matched": 0, "new": 0}
    if new_cal is None or len(new_cal) == 0:
        return new_cal, stats
    cal = new_cal.copy().reset_index(drop=True)
    if board is None or len(board) == 0:
        stats["new"] = int(len(cal))
        return cal, stats

    def _code(v) -> str:
        s = "" if v is None else str(v).strip()
        if s.lower() in ("nan", "none", ""):
            return ""
        return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s

    def _line(r) -> str:
        ln = _code(r.get("line_name")).upper()
        return ln or f"#{_code(r.get('line_id'))}"

    def _f(v, default=None):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return default if f != f else f

    # board candidates keyed by (kind, line): kind = order_id for production/
    # trial, "cip" for CIP rows; downtimes never match (drawn, not stored).
    cands: dict[tuple[str, str], list[dict]] = {}
    for r in board.to_dict("records"):
        btype = _code(r.get("block_type"))
        oid = _code(r.get("order_id"))
        if btype in ("production", "trial") and oid:
            kind = oid
        elif btype == "cip":
            kind = "cip"
        else:
            continue
        s = _f(r.get("start_h"))
        if s is None:
            continue
        cands.setdefault((kind, _line(r)), []).append(
            {"start": s + float(board_shift_h), "row": r, "used": False})

    ids = cal["block_id"].astype(str).tolist()
    locked = cal["locked"].tolist() if "locked" in cal.columns else [False] * len(cal)
    attrs = cal["attrs"].tolist() if "attrs" in cal.columns else [""] * len(cal)
    # nearest-first across the whole frame, so the closest new row claims a
    # board row before a farther one does
    pairs: list[tuple[float, int, dict]] = []
    for i, r in enumerate(cal.to_dict("records")):
        btype = _code(r.get("block_type"))
        oid = _code(r.get("order_id"))
        if btype in ("production", "trial") and oid:
            kind = oid
        elif btype == "cip":
            kind = "cip"
        else:
            continue
        s = _f(r.get("start_h"))
        if s is None:
            continue
        for c in cands.get((kind, _line(r)), []):
            d = abs(c["start"] - s)
            if d <= tol_h:
                pairs.append((d, i, c))
    pairs.sort(key=lambda t: t[0])
    taken: set[int] = set()
    for _d, i, c in pairs:
        if i in taken or c["used"]:
            continue
        taken.add(i)
        c["used"] = True
        br = c["row"]
        ids[i] = str(br.get("block_id") or ids[i])
        locked[i] = bool(locked[i]) or bool(br.get("locked", False))
        keep = [t for t in str(br.get("attrs") or "").split(";")
                if t == "pinned" or t.startswith(FLOAT_PREFIX)]
        cur = [t for t in str(attrs[i] or "").split(";") if t]
        attrs[i] = ";".join(cur + [t for t in keep if t not in cur])
        stats["matched"] += 1
    stats["new"] = int(len(cal) - stats["matched"])
    cal["block_id"] = ids
    cal["locked"] = locked
    cal["attrs"] = attrs
    return cal, stats


def _new_id(prefix: str = "b") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _opt_kg(v: Any) -> float | None:
    """Optional produced kg: unknown stays None, never 0.0.

    Same rule as load_calendar's NaN: a 0 in qty_kg is indistinguishable from
    "produced nothing" and makes the scorecard read unknown production as "no
    excess". Blank, NaN, unparseable and the Gantt's 0 placeholder (see
    useScheduleState.addToHolding, which defaults qty_kg to 0) all collapse to
    None so the missing-kg path downstream can still fire.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(f) or f == 0.0:
        return None
    return f


def downtime_block_type(dtype, reason) -> str:
    """Board window type for a downtimes.csv row: the explicit `type` column
    (Downtime / Maintenance / Contractor, strip selector 2026-09-01) wins;
    older files without it fall back to the reason-keyword heuristic."""
    t = str(dtype or "").strip().lower()
    if t in ("line_down", "downtime", "down"):
        return "line_down"
    if t == "maintenance":
        return "maintenance"
    if t == "contractor":
        return "contractor"
    r = str(reason or "").lower()
    if "contractor" in r:
        return "contractor"
    return "line_down" if "down" in r else "maintenance"


def import_solver_schedule(
    schedule_path: Path,
    cip_path: Path | None = None,
    downtimes_path: Path | None = None,
    planning_anchor: str = "2026-02-15 00:00:00",
    *,
    board: pd.DataFrame | None = None,
    board_shift_h: float = 0.0,
) -> pd.DataFrame:
    """Convert solver schedule_phase2 + cip_windows (+ downtimes) → calendar_blocks.

    `board` (optional, fix writeback-4): the official calendar; rows that
    already exist there (same order/line, start within 0.5 h) keep their
    board block_id, locked flag and planner tokens instead of a fresh uuid —
    see reconcile_block_identity. `board_shift_h` converts the board's hours
    into this schedule's frame (board_anchor - schedule_anchor, in hours).
    Without `board` the output is exactly what it always was.
    """
    rows: list[dict[str, Any]] = []

    if schedule_path.exists():
        sched = pd.read_csv(schedule_path)
        for _, r in sched.iterrows():
            is_trial = bool(r.get("is_trial", False))
            btype = "trial" if is_trial else "production"
            start = float(r.get("start_hour", 0))
            end = float(r.get("end_hour", start))
            # Solver schedules carry the produced kg per row (qty_kg). Older
            # schedules have no such column - keep None then, exactly as before.
            qty = r.get("qty_kg")
            qty = None if qty is None or pd.isna(qty) else float(qty)
            # Trials are blocked production hours, never tonnage (user rule
            # 2026-08-14) - a trial's target_kgs sizes its window, nothing else.
            if is_trial:
                qty = None
            rows.append({
                "block_id": _new_id("prod" if not is_trial else "trial"),
                "block_type": btype,
                "line_id": int(r.get("line_id", 0)),
                "line_name": str(r.get("line_name", "")),
                "start_h": start,
                "end_h": end,
                "label": str(r.get("sku", "")),
                "order_id": str(r.get("order_id", "")),
                "sku": str(r.get("sku", "")),
                "sku_description": str(r.get("sku_description", "") or ""),
                "qty_kg": qty,
                "locked": False,
                "attrs": "",
            })

    if cip_path and cip_path.exists():
        cip = pd.read_csv(cip_path)
        for _, r in cip.iterrows():
            start = float(r.get("start_hour", 0))
            end = float(r.get("end_hour", start))
            rows.append({
                "block_id": _new_id("cip"),
                "block_type": "cip",
                "line_id": int(r.get("line_id", 0)),
                "line_name": str(r.get("line_name", "")),
                "start_h": start,
                "end_h": end,
                "label": "CIP",
                "order_id": "",
                "sku": "CIP",
                "sku_description": "",
                "qty_kg": None,
                "locked": False,
                "attrs": "",
            })

    if downtimes_path and downtimes_path.exists():
        # Two dialects arrive here. The REFERENCE file stores wall-clock
        # datetimes (helpers/downtime_store) — derive start_hour/end_hour
        # against this calendar's anchor frame. A solver WORK-DIR file
        # (work/downtimes.csv, real_downtimes.csv) already speaks hours in
        # the schedule's own frame — pass those through untouched.
        head = pd.read_csv(downtimes_path, nrows=0, encoding="utf-8-sig")
        if {"start_datetime", "end_datetime"}.issubset(head.columns):
            from helpers.downtime_store import load_downtimes_file
            dt = load_downtimes_file(downtimes_path, anchor=planning_anchor)
        else:
            dt = pd.read_csv(downtimes_path)
        for _, r in dt.iterrows():
            reason = str(r.get("reason", "Down") or "Down")
            btype = downtime_block_type(r.get("type"), reason)
            start = float(r.get("start_hour", 0))
            end = float(r.get("end_hour", start))
            if pd.isna(start) or pd.isna(end):
                continue
            rows.append({
                "block_id": _new_id("down" if btype == "line_down" else "maint"),
                "block_type": btype,
                "line_id": int(r.get("line_id", 0)),
                "line_name": str(r.get("line_name", "")),
                "start_h": start,
                "end_h": end,
                "label": reason,
                "order_id": "",
                "sku": "",
                "sku_description": "",
                "qty_kg": None,
                "locked": False,
                "attrs": reason,
            })

    if not rows:
        return empty_calendar()
    out = pd.DataFrame(rows)[CALENDAR_COLUMNS]
    if board is not None and len(board):
        out, _stats = reconcile_block_identity(
            out, board, board_shift_h=board_shift_h)
    return out


def calendar_to_gantt_payload(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Split calendar into schedule-like blocks and window blocks for the React Gantt.

    Production + trial → schedule; cip/maintenance/contractor/line_down → windows.
    """
    schedule: list[dict] = []
    windows: list[dict] = []
    for _, r in df.iterrows():
        btype = str(r["block_type"])
        start = float(r["start_h"])
        end = float(r["end_h"])

        def _txt(v) -> str:
            """Clean optional text: NaN/None/'nan' -> ''."""
            s = str(v or "").strip()
            return "" if s.lower() in ("nan", "none", "nat") else s

        def _code(v) -> str:
            """Item/MO codes: strip a trailing '.0' from float-typed CSV cells."""
            s = _txt(v)
            return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s

        block = {
            "id": str(r["block_id"]),
            "line_id": int(r["line_id"]) if pd.notna(r["line_id"]) else 0,
            "line_name": str(r["line_name"]),
            "order_id": _code(r.get("order_id")),
            "sku": _code(r.get("sku")) or _code(r.get("label")) or btype,
            "sku_description": _txt(r.get("sku_description")),
            "start_hour": start,
            "end_hour": end,
            "run_hours": round(max(0.0, end - start), 1),
            "is_trial": btype == "trial",
            "block_type": _to_gantt_type(btype),
            "label": _code(r.get("label")),
            "locked": bool(r.get("locked", False)),
            # Carried so a drag/drop round trip does not silently drop the
            # produced kg. Unknown kg travels as null, never 0.
            "qty_kg": _opt_kg(r.get("qty_kg")),
            # attrs round-trips so provenance flags (current_state:completed)
            # survive a save; `completed` drives the greyed render.
            "attrs": _txt(r.get("attrs")),
            "completed": "current_state:completed" in _txt(r.get("attrs")),
            # Planner-pinned: fixed for the solver (attrs token 'pinned',
            # toggled in the block popup). Exact-token check — never a
            # substring match, so future tokens like 'unpinned' can't lie.
            "pinned": "pinned" in _txt(r.get("attrs")).split(";"),
        }
        if btype in ("production", "trial"):
            schedule.append(block)
        else:
            windows.append(block)
    return schedule, windows


def _to_gantt_type(btype: str) -> str:
    mapping = {
        "production": "sku",
        "trial": "trial",
        "cip": "cip",
        "maintenance": "maintenance",
        "contractor": "contractor",
        "line_down": "line_down",
    }
    return mapping.get(btype, btype)


def gantt_payload_to_calendar(schedule: list[dict], windows: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for b in schedule:
        gtype = b.get("block_type", "sku")
        btype = "trial" if gtype == "trial" or b.get("is_trial") else "production"
        rows.append(_block_row(b, btype))
    for b in windows:
        gtype = str(b.get("block_type", "cip"))
        btype = {
            "cip": "cip",
            "maintenance": "maintenance",
            "contractor": "contractor",
            "line_down": "line_down",
        }.get(gtype, "cip")
        rows.append(_block_row(b, btype))
    if not rows:
        return empty_calendar()
    return pd.DataFrame(rows)[CALENDAR_COLUMNS]


def _block_row(b: dict, btype: str) -> dict:
    start = float(b.get("start_hour", 0))
    end = float(b.get("end_hour", start))
    # The pin toggle edits the payload BOOL; the CSV stores the attrs token.
    # Reconcile here so a toggle survives the save and an unpin removes it.
    tokens = [t for t in str(b.get("attrs") or "").split(";")
              if t and t != "pinned"]
    if b.get("pinned"):
        tokens.append("pinned")
    return {
        "block_id": str(b.get("id") or _new_id()),
        "block_type": btype,
        "line_id": int(b.get("line_id", 0)),
        "line_name": str(b.get("line_name", "")),
        "start_h": start,
        "end_h": end,
        "label": str(b.get("label") or b.get("sku") or btype),
        "order_id": str(b.get("order_id") or ""),
        "sku": str(b.get("sku") or ""),
        "sku_description": str(b.get("sku_description") or ""),
        "qty_kg": _opt_kg(b.get("qty_kg")),
        "locked": bool(b.get("locked", False)),
        "attrs": ";".join(tokens),
    }


# ── Floating blocks (2026-08-28) ────────────────────────────────────────────
# attrs token "after:<anchor_block_id>:<gap_h>": this block's start is tied
# to another block's end (use case: a SKU that must start when a running MO
# finishes — the MO's end re-forecasts from actual cases, and the floating
# block follows by the same amount). The gap is captured at LINK time so
# deliberate changeover/CIP spacing rides along. Linking also sets the
# 'pinned' token: a floating block is a planner commitment, and every
# pinned pathway (solver committed windows, demand credit, pins_match
# guard, read-only gating) then applies unchanged. Board never moves
# silently — apply_float_links runs behind the Calendar page's drift chip.

FLOAT_PREFIX = "after:"


def float_link_of(attrs) -> tuple[str, float] | None:
    """(anchor_block_id, gap_h) from an attrs string, or None."""
    for t in str(attrs or "").split(";"):
        if t.startswith(FLOAT_PREFIX):
            parts = t.split(":")
            if len(parts) >= 2 and parts[1]:
                try:
                    gap = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
                except ValueError:
                    gap = 0.0
                return parts[1], gap
    return None


def _with_tokens(attrs, drop_prefixes=(), add=()) -> str:
    tokens = [t for t in str(attrs or "").split(";")
              if t and not any(t.startswith(p) for p in drop_prefixes)]
    for t in add:
        if t not in tokens:
            tokens.append(t)
    return ";".join(tokens)


def set_float_link(calendar: pd.DataFrame, block_id: str,
                   anchor_id: str) -> pd.DataFrame:
    """Link block_id's start to anchor_id's end, gap = current spacing.
    Also pins the block (a float is a planner commitment). Returns a copy;
    raises ValueError on unknown ids, self-link, or a non-production block."""
    cal = calendar.copy()
    ids = cal["block_id"].astype(str)
    if block_id == anchor_id:
        raise ValueError("a block cannot float after itself")
    for bid in (block_id, anchor_id):
        if not (ids == bid).any():
            raise ValueError(f"unknown block: {bid}")
    b = cal.loc[ids == block_id].iloc[0]
    a = cal.loc[ids == anchor_id].iloc[0]
    if str(b.get("block_type")) != "production":
        raise ValueError("only production blocks can float")
    if "current_state:" in str(b.get("attrs") or ""):
        raise ValueError("a committed MO block cannot float")
    gap = round(float(b["start_h"]) - float(a["end_h"]), 2)
    tok = f"{FLOAT_PREFIX}{anchor_id}:{gap}"
    cal.loc[ids == block_id, "attrs"] = _with_tokens(
        b.get("attrs"), drop_prefixes=(FLOAT_PREFIX,), add=(tok, "pinned"))
    return cal


def clear_float_link(calendar: pd.DataFrame, block_id: str) -> pd.DataFrame:
    """Remove the float link. The 'pinned' token is left as-is — unpinning
    is the planner's separate, explicit choice (popup toggle)."""
    cal = calendar.copy()
    ids = cal["block_id"].astype(str)
    m = ids == block_id
    if m.any():
        cal.loc[m, "attrs"] = _with_tokens(
            cal.loc[m, "attrs"].iloc[0], drop_prefixes=(FLOAT_PREFIX,))
    return cal


def apply_float_links(calendar: pd.DataFrame,
                      tol: float = 1e-6) -> tuple[pd.DataFrame, list[str]]:
    """Recompute every floating block's position: start = anchor end + gap,
    duration preserved. Chains resolve by fixpoint iteration (bounded), so
    B-after-A and C-after-B both settle. Missing anchors and cycles degrade
    to notes, never exceptions. Returns (calendar copy, human notes)."""
    cal = calendar.copy()
    notes: list[str] = []
    if cal.empty or "attrs" not in cal.columns:
        return cal, notes
    links: dict[str, tuple[str, float]] = {}
    for _, r in cal.iterrows():
        ln = float_link_of(r.get("attrs"))
        if ln is not None:
            links[str(r["block_id"])] = ln
    if not links:
        return cal, notes
    ids = cal["block_id"].astype(str)
    known = set(ids)
    for bid, (anchor, _gap) in list(links.items()):
        if anchor not in known:
            notes.append(f"float link on {bid} points at a missing block "
                         f"({anchor}) — left where it is")
            links.pop(bid)
    before = {str(r["block_id"]): float(r["start_h"])
              for _, r in cal.iterrows() if str(r["block_id"]) in links}
    moved_any = True
    passes = 0
    while moved_any and passes <= len(links) + 1:
        moved_any = False
        passes += 1
        for bid, (anchor, gap) in links.items():
            a = cal.loc[ids == anchor].iloc[0]
            m = ids == bid
            b = cal.loc[m].iloc[0]
            target = float(a["end_h"]) + gap
            delta = target - float(b["start_h"])
            if abs(delta) > tol:
                cal.loc[m, "start_h"] = float(b["start_h"]) + delta
                cal.loc[m, "end_h"] = float(b["end_h"]) + delta
                moved_any = True
    for bid in links:
        m = ids == bid
        b = cal.loc[m].iloc[0]
        net = float(b["start_h"]) - before[bid]
        if abs(net) > tol:
            anchor, _ = links[bid]
            a = cal.loc[ids == anchor].iloc[0]
            notes.append(
                f"{b.get('label') or b.get('sku') or bid} moved {net:+.2f}h "
                f"to follow {a.get('label') or a.get('sku') or anchor}")
    if moved_any:
        notes.append("float links did not settle (cycle?) — check the links")
    return cal, notes


# Changeover-flag bitmask bit order (bit i = column i) — mirrored in the
# Gantt frontend (utils/skuPicker.ts CO_FLAG_BITS); change both or neither.
CO_FLAG_COLUMNS = (
    "ttp_change",
    "ffs_change",
    "topload_change",
    "casepacker_change",
    "conv_to_org_change",
    "cinn_to_non",
    # bit 64: the pair needs a clean between the runs (INTEGRATE, agent FE
    # handoff W-1; the client already labels this bit "CIP req").
    "cip_req_after",
)


def build_co_flags(
    changeovers_csv: Path,
    demand_skus: set[str],
    board_skus: set[str] | None = None,
) -> dict[str, int]:
    """{"FROM|TO": bitmask} of changeover-type flags for SKU pairs — data for
    the blank-space SKU picker.

    The pair universe is demand ∪ board SKUs: a picker candidate is always a
    demand SKU, but its NEIGHBOUR on the line can be any board block (a
    committed manprg MO of a rolled-off SKU, a trial) — dropping those pairs
    made unknown transitions render as clean (review 2026-08-19).

    Zero-mask pairs are kept EXPLICITLY: the client reads value 0 as "no
    machine touched" and a MISSING pair as "unknown — no changeover data".
    Setup hours are not duplicated here: the Gantt already receives the full
    setup matrix (`changeovers`).
    """
    if not changeovers_csv.exists() or not demand_skus:
        return {}
    from helpers.scorecard_engine import normalize_co_columns

    universe = demand_skus | (board_skus or set())
    df = normalize_co_columns(
        pd.read_csv(changeovers_csv, dtype={"from_sku": str, "to_sku": str}))
    df = df[df["from_sku"].isin(universe) & df["to_sku"].isin(universe)]
    for col in CO_FLAG_COLUMNS:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    out: dict[str, int] = {}
    for r in df.itertuples(index=False):
        mask = 0
        for i, col in enumerate(CO_FLAG_COLUMNS):
            if getattr(r, col):
                mask |= 1 << i
        out[f"{r.from_sku}|{r.to_sku}"] = mask
    return out


def build_line_capable_skus(
    caps_csv: Path, demand_skus: set[str]
) -> dict[str, list[dict]]:
    """{line_name: [{"sku", "rate"}]} of demand-plan SKUs each line can run
    (capable == 1) — candidate list for the blank-space SKU picker."""
    if not caps_csv.exists() or not demand_skus:
        return {}
    from helpers.effective_rates import load_effective_capabilities
    df = load_effective_capabilities(caps_csv)
    df["capable"] = pd.to_numeric(df.get("capable", 0), errors="coerce").fillna(0)
    df["calc_rate_kgph"] = pd.to_numeric(
        df.get("calc_rate_kgph", 0), errors="coerce").fillna(0)
    out: dict[str, list[dict]] = {}
    for r in df.itertuples(index=False):
        sku = str(r.sku)
        if int(r.capable) != 1 or sku not in demand_skus:
            continue
        out.setdefault(str(r.line_name), []).append(
            {"sku": sku, "rate": float(r.calc_rate_kgph)})
    return out


# ── Supply timeline payload (2026-09-01) ────────────────────────────────────
# The Gantt's per-block supply chips run stockcheck/timeline's TS port on
# the client; the page ships it the SAVED stock report's inputs (contract
# §5 StockArgs) and re-grades the pushed board server-side for the caption
# (§7). The report's hours live in ITS anchor frame — the toml anchor can
# roll between a compute and a page view — so every hour is shifted into
# the page frame here, in Python, never on the client.

_STOCK_RULE_KEYS = ("min_days_after_delivery", "lead_measured_from",
                    "dependent_frac_floor", "hard_block")


def _fnum(v, default=None):
    """float or default: None/NaN/bools/unparseable never leak into hours."""
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f   # NaN


def _code_str(v) -> str:
    """Item/MO code as a clean string: NaN/None -> '', float-typed
    '280351.0' -> '280351' (same rule as calendar_to_gantt_payload)."""
    s = "" if v is None else str(v).strip()
    if s.lower() in ("nan", "none", "nat"):
        return ""
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _short_stamp(v) -> str:
    """'2026-09-01 14:46:00' -> '9/1 14:46' for the as-of captions; an
    unparseable stamp shows as-is rather than vanishing."""
    from helpers.timefmt import parse_datetime
    d = parse_datetime(v)
    if d is None:
        return str(v or "").strip()
    return f"{d.month}/{d.day} {d.hour:02d}:{d.minute:02d}"


def build_stock_payload(report, cfg, page_anchor, *, cases_left=None
                        ) -> dict | None:
    """StockArgs for gantt_calendar(stock=...) from a saved stock report.

    None when the report predates the supply timeline (no `supply_meta`):
    the client then renders no supply UI at all. Only items some SKU recipe
    references travel — the report's meta spans the whole board+demand
    universe and the Gantt needs nothing outside `sku_needs`. `cases_left`
    is the manprg order_id -> cases-left map for running MOs.
    """
    if not isinstance(report, dict):
        return None
    meta = report.get("supply_meta")
    if not isinstance(meta, dict):
        return None
    from helpers.config import stock_config
    from helpers.timefmt import parse_anchor

    # No anchor on the report = same frame as the page (nothing to shift).
    shift = 0.0
    if report.get("anchor"):
        shift = (parse_anchor(report["anchor"]) - parse_anchor(page_anchor)
                 ).total_seconds() / 3600.0

    def _shift(v):
        f = _fnum(v)
        return None if f is None else f + shift

    sku_needs: dict = {}
    referenced: set[str] = set()
    for sku, need in (report.get("sku_needs") or {}).items():
        need = need if isinstance(need, dict) else {}
        ents = []
        for e in need.get("items") or []:
            item = str(e.get("item") or "")
            alts = [str(a) for a in (e.get("alts") or []) if str(a)]
            ents.append({"item": item, "per_case": _fnum(e.get("per_case"), 0.0),
                         "unit": str(e.get("unit") or ""), "alts": alts})
            referenced.add(item)
            referenced.update(alts)
        sku_needs[str(sku)] = {"kg_per_case": _fnum(need.get("kg_per_case"), 0.0),
                               "items": ents}

    def _pick(d, conv) -> dict:
        out = {}
        for k, v in (d or {}).items():
            if str(k) in referenced:
                c = conv(v)
                if c is not None:
                    out[str(k)] = c
        return out

    inbound = report.get("inbound")
    inbound = inbound if isinstance(inbound, dict) else {}
    receipts: dict = {}
    for item, rs in (inbound.get("receipts") or meta.get("receipts") or {}).items():
        if str(item) not in referenced:
            continue
        shifted = []
        for r in rs or []:
            rh, qty = _fnum(r.get("ready_h")), _fnum(r.get("qty"))
            if rh is None or qty is None:
                continue
            shifted.append({"ready_h": rh + shift, "qty": qty,
                            "po8": str(r.get("po8") or ""),
                            "tier": str(r.get("tier") or "erp"),
                            "receipt_date": str(r.get("receipt_date") or ""),
                            "label": str(r.get("label") or "")})
        shifted.sort(key=lambda r: (r["ready_h"], r["po8"]))
        receipts[str(item)] = shifted

    rules = stock_config(cfg)
    stamps = meta.get("snapshot_stamp") or {}
    left = {}
    for k, v in (cases_left or {}).items():
        f = _fnum(v)
        if f is not None:
            left[str(k)] = f
    return {
        "feed_state": str(meta.get("feed_state") or inbound.get("state")
                          or "missing"),
        # source_mtime carries a time of day; the header as-of is date-only.
        "as_of": {"stock_rm": _short_stamp(stamps.get("rm")),
                  "stock_pkg": _short_stamp(stamps.get("pkg")),
                  "po": _short_stamp(inbound.get("source_mtime")
                                     or inbound.get("as_of"))},
        "receipts_window_end_h": _fnum(meta.get("receipts_window_end_h"), 0.0)
                                 + shift,
        "rules": {k: rules[k] for k in _STOCK_RULE_KEYS},
        "opening": _pick(meta.get("opening"), lambda v: _fnum(v, 0.0)),
        "tracked": sorted(str(i) for i in (meta.get("tracked") or [])
                          if str(i) in referenced),
        "in_house": sorted(str(i) for i in (meta.get("in_house") or [])),
        "units": _pick(meta.get("units"), lambda v: str(v or "")),
        "designations": _pick(meta.get("designations"), lambda v: str(v or "")),
        "snapshot_h": _pick(meta.get("snapshot_h"), _shift),
        "receipts": receipts,
        "sku_needs": sku_needs,
        "cases_left": left,
    }


def board_supply_summary(records, payload, cfg, *, locked_through_h=None,
                         caps=None) -> dict:
    """Server-side re-grade of the pushed board for the Calendar caption:
    {"short", "dependent", "no_data", "verdicts": {key: verdict}}.

    Same engine and inputs as the client chips (stockcheck/timeline on the
    StockArgs payload), so the caption and the board cannot disagree. Rules
    aligned with the client's supplyGlue.toTimelineBlock and the stock
    report (fix K-1) — INTEGRATE applying the FE->W handoff (ui-11):
      * cases = the board's qty_kg / kg_per_case; unknown kg falls back to
        rate x hours when `caps` ({line: {sku: kg/h}}) knows the line's rate
        for the SKU, else `cases` is None (the engine grades it NO_DATA);
      * a SKU without a recipe in the payload is KEPT with cases None — the
        engine grades it NO_DATA (grey "?", never green), as the client does;
      * `locked` also for `current_state:completed` rows.
    Counts follow supply_rank: a minor DEPENDENT is a grey chip, not a
    delivery the planner must watch, so it is not in `dependent`.
    """
    out: dict = {"short": 0, "dependent": 0, "no_data": 0, "verdicts": {}}
    if not payload:
        return out
    from helpers.config import stock_config
    from stockcheck import timeline as tl

    needs = payload.get("sku_needs") or {}
    left = payload.get("cases_left") or {}
    caps = caps or {}
    blocks: list[dict] = []
    for r in records or []:
        if str(r.get("block_type") or "production") != "production":
            continue
        sku = _code_str(r.get("sku"))
        need = needs.get(sku)
        start = _fnum(r.get("start_h"), 0.0)
        end = _fnum(r.get("end_h"), start)
        kpc = _fnum(need.get("kg_per_case"), 0.0) if isinstance(need, dict) else 0.0
        kg = _opt_kg(r.get("qty_kg"))
        if kg is None:
            line_caps = caps.get(str(r.get("line_name") or "")) or {}
            rate = _fnum(line_caps.get(sku), 0.0) if isinstance(line_caps, dict) else 0.0
            if rate and rate > 0 and end > start:
                kg = float(rate) * (end - start)
        cases = kg / kpc if (kg is not None and kpc > 0) else None
        attrs = str(r.get("attrs") or "")
        _tokens = attrs.split(";")
        locked = (bool(r.get("locked")) or "pinned" in _tokens
                  or "current_state:completed" in _tokens
                  or (locked_through_h is not None
                      and start < float(locked_through_h)))
        blk = {"block_id": str(r.get("block_id") or ""), "sku": sku,
               "line_name": str(r.get("line_name") or ""),
               "start_h": start, "end_h": end, "cases": cases,
               "locked": locked,
               "running": "current_state:running" in attrs,
               "cases_left": _fnum(left.get(_code_str(r.get("order_id"))))}
        blk["key"] = tl.block_key(blk)
        blocks.append(blk)
    timelines = tl.build_timelines(
        blocks, needs, payload.get("opening") or {},
        payload.get("receipts") or {}, payload.get("snapshot_h") or {},
        tracked=payload.get("tracked") or [],
        in_house=payload.get("in_house") or [])
    board = tl.evaluate_board(
        blocks, timelines, stock_config(cfg),
        payload.get("feed_state") or "missing",
        _fnum(payload.get("receipts_window_end_h"), 0.0))
    for key, sup in board.items():
        out["verdicts"][key] = sup["verdict"]
        rank = tl.supply_rank(sup)
        if rank == 4:
            out["short"] += 1
        elif rank == 3:
            out["dependent"] += 1
        elif rank == 2:
            out["no_data"] += 1
    return out


# Ids the board draws but never stores. cipinfo_ = cip_info ScheduledCIP
# overlay; dt_ = downtimes.csv windows drawn on load; down_/maint_ = the
# legacy rows older imports minted from that same file. Downtimes are
# CONSTRAINTS, not schedule (user rule 2026-09-01): calendar_blocks.csv is
# the production export the ERP ingests, and the plant is simply not
# scheduled during a downtime — downtimes.csv is their single source.
DISPLAY_ONLY_PREFIXES = ("cipinfo_", "dt_", "down_", "maint_")


def drop_display_overlays(df: pd.DataFrame) -> pd.DataFrame:
    """Remove display-only overlay blocks before a save.

    The Plant Calendar draws cip_info's ScheduledCIP as overlay windows
    (block_id 'cipinfo_<line>') so the planner SEES the plant's cleaning
    plan — but they are a live-feed visualization, not calendar data. A save
    that keeps them writes the overlay into calendar_blocks.csv, where the
    next page load skips re-overlaying (line already has a CIP) and the
    scorecard counts a CIP the planner never placed. Strip them at every
    save boundary.
    """
    if df is None or df.empty or "block_id" not in df.columns:
        return df
    return df[~df["block_id"].astype(str).str.startswith(DISPLAY_ONLY_PREFIXES)]


def load_lines(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame(columns=["line_id", "line_name", "active"])


def ensure_lines_from_calendar(calendar: pd.DataFrame, lines_path: Path) -> pd.DataFrame:
    existing = load_lines(lines_path)
    if not existing.empty:
        return existing
    if calendar.empty:
        return pd.DataFrame(columns=["line_id", "line_name", "active"])
    lines = (
        calendar[["line_id", "line_name"]]
        .drop_duplicates()
        .sort_values("line_id")
        .reset_index(drop=True)
    )
    lines["active"] = True
    safe_write_csv(lines, lines_path)
    return lines
