# helpers/live_sync.py — run / read the live-data sync from inside the app.
#
# scripts/fs-live-pull.py is the ONE routine that brings plant files into
# data/reference (a clone of the private GitHub repo on the dev PC, the ERP's
# shared drop folder on the planner's PC — docs/live-data-bridge-setup.md).
# The scheduled task runs it every few minutes; this module lets the app run
# the same pass on demand (the calendar's "Rebuild from plant state",
# 2026-09-14: a rebuild must never trail the schedule) and read the heartbeat
# it leaves (data/reference/live_sync.json) for the health page.
#
# The script runs as a subprocess, exactly as the scheduled task runs it, so
# the app never imports the bridge and a hung share cannot hang the page: the
# run is bounded by a timeout and every failure comes back as an outcome,
# never as an exception.

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from helpers.paths import reference_dir, repo_root

STATE_NAME = "live_sync.json"
SCRIPT_REL = ("scripts", "fs-live-pull.py")
CONF_REL = ("scripts", "fs-live-data.conf.json")
LOCAL_CONF_REL = ("scripts", "fs-live-data.local.json")


def script_path(root: Path | None = None) -> Path:
    return Path(root or repo_root()).joinpath(*SCRIPT_REL)


def load_bridge_conf(root: Path | None = None) -> dict:
    """Tracked conf with the git-ignored per-machine override merged over it
    (the same merge the script does). {} when neither file exists."""
    root = Path(root or repo_root())
    conf: dict = {}
    tracked = root.joinpath(*CONF_REL)
    if tracked.is_file():
        conf.update(json.loads(tracked.read_text(encoding="utf-8")))
    local = root.joinpath(*LOCAL_CONF_REL)
    if local.is_file():
        # utf-8-sig: PowerShell 5.1 writes a BOM with -Encoding utf8
        conf.update(json.loads(local.read_text(encoding="utf-8-sig")))
    return conf


def configured_sources(conf: dict) -> list[str]:
    """Folder-mode source folders named by the conf ([] in GitHub mode)."""
    raw = conf.get("source_dirs") or conf.get("source_dir") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(p).strip() for p in raw if isinstance(p, str) and str(p).strip()]


# ── the fs_data drop root (2026-09-17) ───────────────────────────────────────
# The sync scripts replace a configured folder that holds fs_vif and/or
# fs_manual by those subfolders (scripts/fs-live-pull.py expand_drop_roots);
# the app must read the conf the same way or the Data Files page looks for
# the AZAP workbook in the root and finds nothing. code/ cannot import the
# scripts (they import from code/), so this is a verbatim copy pinned by
# tests/test_live_bridge_glob.py::test_helper_copies_are_identical.
DROP_SUBFOLDERS = ("fs_vif", "fs_manual")


def expand_drop_roots(dirs) -> list[Path]:
    """Replace every folder in `dirs` that holds an fs_vif and/or fs_manual
    subfolder by those subfolders (fs_vif first, then fs_manual), keep every
    other folder as it is, and drop duplicates keeping the first occurrence:
    an explicit list of the two subfolders comes back unchanged, and a root
    listed next to its own fs_vif does not list fs_vif twice. A root that
    expands is NOT kept itself (a workbook left in the root must not get the
    demand_plan_summary.csv rebuild). A subfolder probe that fails (OSError)
    counts as absent, so an unreachable folder comes back unchanged and the
    caller reports it.
    """
    out: list[Path] = []
    for d in dirs:
        d = Path(d)
        subs: list[Path] = []
        for name in DROP_SUBFOLDERS:
            sub = d / name
            try:
                if sub.is_dir():
                    subs.append(sub)
            except OSError:
                continue
        for p in (subs or [d]):
            if p not in out:
                out.append(p)
    return out


def source_folders(conf: dict) -> list[str]:
    """The folders the sync actually reads for this conf: the configured
    sources with every fs_data root expanded to its subfolders (2026-09-17),
    as strings, in the sync's order. [] in GitHub mode."""
    return [str(p) for p in expand_drop_roots(configured_sources(conf))]


def is_configured(root: Path | None = None) -> bool:
    """True when a sync pass makes sense on this machine: the script exists
    and the conf names a source folder or a clone that exists. On a box
    where the files are copied in by hand this is False and nothing runs."""
    if not script_path(root).is_file():
        return False
    try:
        conf = load_bridge_conf(root)
    except (OSError, ValueError):
        return False
    if configured_sources(conf):
        return True
    clone = conf.get("clone_dir_personal") or conf.get("clone_dir")
    try:
        return bool(clone) and (Path(str(clone)) / ".git").exists()
    except OSError:
        return False


def read_state(dd: Path | None = None) -> dict | None:
    """The heartbeat the script writes after every pass; None when the sync
    never ran on this machine or the file is unreadable."""
    p = reference_dir(dd) / STATE_NAME
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@dataclass
class SyncOutcome:
    ran: bool                 # False: not configured here, nothing happened
    ok: bool
    summary: str              # one line for a toast / caption
    updated: list[str] = field(default_factory=list)   # files actually copied
    problems: list[str] = field(default_factory=list)
    seconds: float = 0.0


def run_sync(dd: Path | None = None, *, timeout_s: float = 90.0,
             python: str | None = None, root: Path | None = None) -> SyncOutcome:
    """Run one sync pass now (scripts/fs-live-pull.py --once). Never raises.

    The outcome is read from the heartbeat the pass wrote, with the exit
    code as the fallback truth: a pass that could not even write the
    heartbeat still reports as failed."""
    if not is_configured(root):
        return SyncOutcome(ran=False, ok=True,
                           summary="Live-data sync is not set up on this machine.")
    t0 = time.monotonic()
    started = datetime.now().replace(microsecond=0)
    cmd = [python or sys.executable, str(script_path(root)), "--once"]
    kwargs: dict = {}
    if sys.platform == "win32":  # no console flash under the hidden launcher
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                              cwd=str(root or repo_root()), **kwargs)
    except subprocess.TimeoutExpired:
        return SyncOutcome(
            ran=True, ok=False, seconds=time.monotonic() - t0,
            problems=[f"timed out after {timeout_s:.0f} s"],
            summary=f"Live-data sync timed out after {timeout_s:.0f} s (is the source folder reachable?).")
    except OSError as exc:
        return SyncOutcome(ran=True, ok=False, seconds=time.monotonic() - t0,
                           problems=[str(exc)],
                           summary=f"Live-data sync could not start: {exc}")
    secs = time.monotonic() - t0
    state = read_state(dd) or {}
    try:
        fresh = datetime.fromisoformat(str(state.get("finished"))) >= started
    except (TypeError, ValueError):
        fresh = False
    notes = [str(u) for u in state.get("updated", [])] if fresh else []
    problems = [str(x) for x in state.get("problems", [])] if fresh else []
    files = [u for u in notes if not u.endswith(")")]   # drop "(derived)" / "(stamped)" notes
    ok = proc.returncode == 0 and not problems
    if not fresh and proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        problems = [tail[-1]] if tail else ["the sync exited with an error and left no report"]
        ok = False
    if ok:
        summary = ("Live data synced: updated " + ", ".join(files) + ".") if files \
            else "Live data synced: nothing new."
    else:
        summary = "Live-data sync reported problems: " + "; ".join(problems[:2])
    return SyncOutcome(ran=True, ok=ok, summary=summary, updated=files,
                       problems=problems, seconds=secs)
