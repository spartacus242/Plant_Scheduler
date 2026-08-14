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

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

_FILENAME = "lock_state.json"
_FMT = "%Y-%m-%d %H:%M:%S"


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
    path.write_text(
        json.dumps({"locked_through": locked_through.strftime(_FMT)},
                   indent=2),
        encoding="utf-8")


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
