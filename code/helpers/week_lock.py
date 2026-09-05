# helpers/week_lock.py — the 2-week lock window (charter §2.2).
#
# "2 weeks locked and ready, week 3 flexible." The lock is OPERATIONAL STATE,
# not configuration — it advances every weekly roll — so it lives in a sidecar
# JSON (data/lock_state.json), never in flowstate.toml (whose comments a toml
# round-trip would destroy).
#
# Semantics: blocks that START before `locked_through` are committed to the
# plant. The Gantt refuses drag/resize/edit inside the window; the solver
# already honors per-block locks. Clearing the lock (None) unfreezes.
#
# Server-side enforcement (fix writeback-9, audit 2026-09-03): until then the
# lock was a browser-only convention — no save or promote path read
# lock_state.json, so a promoted Scenario E/F version, a stale Gantt mount or
# a drop INTO the window re-planned the two committed weeks silently.
# `check_lock_violations` compares the locked window of the board being
# written against the board on disk; save_calendar / promote_version refuse
# (LockViolation) unless the caller passes an explicit override.

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

_FILENAME = "lock_state.json"
_FMT = "%Y-%m-%d %H:%M:%S"


class LockViolation(ValueError):
    """A write would move, alter, add or remove work inside the locked
    window. `.violations` carries one human line per offending block."""

    def __init__(self, violations: list[str], locked_through_h: float):
        self.violations = list(violations)
        self.locked_through_h = float(locked_through_h)
        n = len(self.violations)
        head = (f"{n} change(s) inside the locked window (blocks starting "
                f"before hour {self.locked_through_h:g} are committed to the "
                "plant). Unlock first, or save with the lock override.")
        super().__init__(head + "".join(f"\n  - {v}" for v in self.violations[:12]))


def read_lock(data_dir: Path) -> datetime | None:
    """The locked-through moment, or None when no lock is set."""
    path = Path(data_dir) / _FILENAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        val = str(raw.get("locked_through", "")).strip()
        return datetime.strptime(val, _FMT) if val else None
    except (OSError, ValueError, AttributeError):
        return None


def write_lock(data_dir: Path, locked_through: datetime | None) -> None:
    """Set or clear the lock. None clears."""
    path = Path(data_dir) / _FILENAME
    if locked_through is None:
        if path.exists():
            path.unlink()
        return
    # Atomic (writeback-15 aside): a truncated lock_state.json reads as
    # "no lock" (read_lock swallows it), which would silently unfreeze.
    from helpers.safe_io import safe_write_json
    safe_write_json({"locked_through": locked_through.strftime(_FMT)}, path)


def locked_through_h(locked_through: datetime | None, anchor: datetime
                     ) -> float | None:
    """Lock boundary as an hour offset from the calendar anchor.

    None when no lock; clamped at 0 so a lock behind the anchor (e.g. after
    a roll) simply locks nothing instead of going negative.
    """
    if locked_through is None:
        return None
    return max(0.0, (locked_through - anchor).total_seconds() / 3600.0)


def default_lock_through(anchor: datetime, weeks: int = 2) -> datetime:
    """The charter default: anchor + 2 whole weeks."""
    return anchor + timedelta(hours=weeks * 168)


# Display-only ids never count: they are drawn from downtimes.csv / cip_info
# on every load and stripped on every save (calendar_io.DISPLAY_ONLY_PREFIXES).
_DISPLAY_PREFIXES = ("cipinfo_", "dt_", "down_", "maint_")


def _locked_rows(cal, lock_h: float, tol: float) -> dict[tuple, int]:
    """Multiset {(identity tuple): count} of the rows starting inside the
    locked window. Identity = what the plant sees: id, type, line, start,
    end, order, sku, kg (rounded to 1e-3 h / 1 kg). Split pieces share an
    id, so the tuple (not the id) is the key."""
    import pandas as pd

    out: dict[tuple, int] = {}
    if cal is None or len(cal) == 0 or "start_h" not in cal.columns:
        return out
    for r in cal.to_dict("records"):
        bid = str(r.get("block_id") or "")
        if bid.startswith(_DISPLAY_PREFIXES):
            continue
        try:
            s = float(r.get("start_h"))
        except (TypeError, ValueError):
            continue
        if not (s < lock_h - tol):
            continue
        try:
            e = float(r.get("end_h"))
        except (TypeError, ValueError):
            e = s
        # unknown kg (None/NaN/blank) and the Gantt's 0 placeholder are the
        # same "no tonnage" — calendar_io._opt_kg collapses them on the
        # payload round trip, so they must not read as a change here
        kg = r.get("qty_kg")
        try:
            kg = None if kg is None or pd.isna(kg) else round(float(kg))
        except (TypeError, ValueError):
            kg = None
        if kg == 0:
            kg = None

        def _c(v) -> str:
            t = "" if v is None else str(v).strip()
            return "" if t.lower() in ("nan", "none") else t

        key = (bid, _c(r.get("block_type")), _c(r.get("line_name")).upper(),
               round(s, 3), round(e, 3), _c(r.get("order_id")),
               _c(r.get("sku")), kg)
        out[key] = out.get(key, 0) + 1
    return out


def check_lock_violations(new_cal, old_cal, lock_h: float | None,
                          *, tol: float = 1e-6) -> list[str]:
    """Human lines for every block that differs inside the locked window
    between the board on disk (`old_cal`) and the board about to be written
    (`new_cal`). Empty list = the locked weeks are untouched.

    A block counts as inside the window when it STARTS before `lock_h`
    (charter rule; same test as the Gantt's immovable flag). Removed or
    moved-out rows read "moved/removed"; new or moved-in rows read "added".
    Rows are compared as full identity tuples, so a resize, a tonnage edit,
    a line change or a re-labelled SKU inside the window is a violation too.
    """
    if lock_h is None:
        return []
    lock_h = float(lock_h)
    if lock_h <= 0:
        return []
    old = _locked_rows(old_cal, lock_h, tol)
    new = _locked_rows(new_cal, lock_h, tol)

    def _fmt(k: tuple) -> str:
        bid, btype, line, s, e, oid, sku, kg = k
        what = oid or sku or btype
        return f"{line} {btype} {what} @ {s:g}-{e:g}h (id {bid})"

    out: list[str] = []
    for k, n in old.items():
        missing = n - new.get(k, 0)
        for _ in range(max(0, missing)):
            out.append(f"moved/removed inside the locked window: {_fmt(k)}")
    for k, n in new.items():
        added = n - old.get(k, 0)
        for _ in range(max(0, added)):
            out.append(f"added inside the locked window: {_fmt(k)}")
    return out
