#!/usr/bin/env python3
"""Flowstate live-data PULL / SYNC script — the side that feeds data/reference.

Two sources, one behaviour (copy changed files into data/reference/, re-derive
demand_plan.csv when the summary changed, stamp the manprg as-of, write the
live_sync.json heartbeat the app's health page reads):

  GitHub mode  (dev PC, off the work network): pull the private
               `flowstate-live-data` repo into a local clone, copy from there.
  Folder mode  (planner's PC on the work network, 2026-09-14): copy straight
               from the folder(s) the ERP drops its exports into — set
               "source_dirs" in the git-ignored fs-live-data.local.json
               (install_flowstate.ps1 -FeedDir writes it) or pass --source-dir.
               The recommended value is the plant drop ROOT, fs_data
               (2026-09-17): a listed folder holding fs_vif and/or fs_manual
               stands for those subfolders (expand_drop_roots); an explicit
               subfolder list and a flat folder work as before. A reachable
               folder holding none of the listed files is a problem, not a
               quiet "nothing new".
               The folders are only ever READ: no lock, marker, rename or
               delete, so other people and the ERP keep using them untouched.
  drop mirror  (2026-09-18, the dev PC) "mirror_github_to": "<path>\\fs_data" in
               the local conf makes the pass FIRST pull the GitHub clone and
               drop its files into that folder's fs_vif / fs_manual (routing:
               "drop_layout" in the conf), the way OneDrive would sync the
               SharePoint library on a planner's PC, then run folder mode
               from it. GitHub's copy lands only when it is NEWER than what
               sits there (commit time vs file time; the file time becomes
               the commit time), nothing is ever deleted, so the dev PC runs
               the planner's exact setup while the work PC still pushes.
               ONE exception (2026-09-15): the folder that holds the planners'
               weekly "New Export AZAP MMDDYY.xlsx" (fs_manual) gets ONE file
               written into it — demand_plan_summary.csv, rebuilt from that
               workbook by helpers/azap_demand.refresh_demand_summary unless
               the csv is up to date with it. Nothing else is
               ever written to a source folder.

Read-only towards every source (bar that one csv). Safe on a schedule: a pass
with nothing new changes nothing. Exit code 1 when a pass failed or reported problems (a share
that is not reachable, a file that could not be read), so the Task Scheduler
"Last Run Result" and the installer's first sync both see it.

Usage:
    python fs-live-pull.py --once                 # one pass, conf decides the mode
    python fs-live-pull.py --watch 900            # loop every 15 min
    python fs-live-pull.py --once --source-dir "\\\\server\\share\\erp_out"   # folder mode, ad hoc
    python fs-live-pull.py --once --source-dir "C:\\Users\\<planner>\\Flowstate\\fs_data"   # the drop root
"""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

CONF = Path(__file__).resolve().parent / "fs-live-data.conf.json"
# Per-machine overrides (git-ignored): clone_dir_personal / data_reference_dir
# on a planner's PC, written by scripts/install_flowstate.ps1 -LiveData. The
# tracked conf keeps the shared keys (repo, branch, file list).
LOCAL_CONF = CONF.with_name("fs-live-data.local.json")
# FS_LIVE_DATA_LOCAL_CONF (2026-09-18): another per-machine conf to merge, or
# "" for none. Tests that run this script as a subprocess set it to "" so the
# box's own local conf (its drop mirror!) never reaches a test pass — one such
# run re-timed the real drop's manprg files while the suite ran.
LOCAL_CONF_ENV = "FS_LIVE_DATA_LOCAL_CONF"
GIT_BIN = shutil.which("git") or r"C:\Program Files\Git\bin\git.exe"


def load_conf():
    with open(CONF, encoding="utf-8") as f:
        conf = json.load(f)
    local = LOCAL_CONF
    if LOCAL_CONF_ENV in os.environ:
        override = os.environ[LOCAL_CONF_ENV].strip()
        local = Path(override) if override else None
    if local is not None and local.is_file():
        # utf-8-sig: PowerShell 5.1 writes a BOM with -Encoding utf8
        with open(local, encoding="utf-8-sig") as f:
            conf.update(json.load(f))
    return conf


# ── source folders (folder mode) ─────────────────────────────────────────────
# "source_dirs" (list) or "source_dir" (string) in the conf switches the pass
# to folder mode: files are copied from those folders instead of from a clone
# of the GitHub repo. Several folders may be listed (the ERP drop folder plus
# a planning folder, say); a file present in more than one is taken from
# wherever it is newest. A folder that is not reachable right now costs the
# pass a problem entry, never a crash: the others still sync.
SETTLE_SECONDS_DEFAULT = 60   # a file modified this recently may still be being written
SYNC_STATE_NAME = "live_sync.json"   # heartbeat written into data/reference after every pass


def source_dirs(conf: dict) -> list[Path]:
    raw = conf.get("source_dirs")
    if not raw:
        raw = conf.get("source_dir") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [Path(p.strip()) for p in raw if isinstance(p, str) and p.strip()]

# Glob entries in conf["files"]: "<glob> -> <dest name>". IT's open-PO export
# carries a date in its name ("NPA Open POs -8.24.xlsx"), so a fixed conf
# name would silently never match; the newest match is copied under <dest>.
# The helper is duplicated VERBATIM in fs-live-push.py / fs-live-pull.py: the
# work PC runs a hand-copied single script, so neither may import the other.
GLOB_SEP = " -> "


def resolve_entries(entries, src_dir):
    """Expand conf["files"] into copyable (src_path, dest_name) pairs.

    Plain entry: <src_dir>/<name> under the same name, only when it exists
    (unchanged behaviour). Glob entry: the newest file matching <glob> in
    src_dir (by mtime), under <dest>; no match = dropped. When several
    entries resolve to the same dest, the newest source wins so a fixed-name
    file and a dated re-export never race each other.
    """
    src_dir = Path(src_dir)
    picked: dict[str, Path] = {}
    order: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue  # a non-string conf entry costs itself, not the pass
        if GLOB_SEP in entry:
            pattern, dest = (s.strip() for s in entry.split(GLOB_SEP, 1))
            if not pattern or not dest:
                continue  # malformed entry: skip, never crash the bridge
            try:
                matches = [p for p in src_dir.glob(pattern) if p.is_file()]
            except (ValueError, NotImplementedError, TypeError):
                continue  # e.g. an absolute pattern: pathlib refuses it
            if not matches:
                continue
            src = max(matches, key=lambda p: p.stat().st_mtime)
        else:
            dest, src = entry, src_dir / entry
            if not src.is_file():
                continue
        prev = picked.get(dest)
        if prev is None:
            order.append(dest)
        if prev is None or src.stat().st_mtime > prev.stat().st_mtime:
            picked[dest] = src
    return [(picked[d], d) for d in order]


# ── the fs_data drop root (2026-09-17) ───────────────────────────────────────
# The plant drop is ONE synced folder, fs_data, holding fs_vif (the ERP's own
# exports) and fs_manual (the files people maintain). A conf that named the
# root — install_flowstate.ps1 -FeedDir "<path>\fs_data", the intended single
# value — synced NOTHING and still reported ok / nothing new: the file list
# resolves direct children only. expand_drop_roots turns every listed folder
# that holds those subfolders into the subfolders themselves; the pure conf
# readers (source_dirs / source_dirs_work) stay free of filesystem I/O and the
# expansion runs once, at the call site. Duplicated VERBATIM like
# resolve_entries (a test pins the copies).
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


def _entry_dest(entry) -> str | None:
    """Destination name a conf entry copies under (None for a bad entry)."""
    if not isinstance(entry, str):
        return None
    if GLOB_SEP in entry:
        dest = entry.split(GLOB_SEP, 1)[1].strip()
        return dest or None
    return entry


def resolve_entries_multi(entries, src_dirs) -> list[tuple[Path, str]]:
    """resolve_entries over several source folders, merged newest-source-wins
    per destination name, in conf order. An unreachable folder contributes
    nothing (the caller reports it); a stat that fails mid-way drops only
    that candidate."""
    picked: dict[str, Path] = {}
    for d in src_dirs:
        d = Path(d)
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        for src, dest in resolve_entries(entries, d):
            try:
                prev = picked.get(dest)
                if prev is None or src.stat().st_mtime > prev.stat().st_mtime:
                    picked[dest] = src
            except OSError:
                continue
    order: list[str] = []
    for e in entries:
        dest = _entry_dest(e)
        if dest in picked and dest not in order:
            order.append(dest)
    return [(picked[d], d) for d in order]


# The silent-green failure (2026-09-17): a conf pointing at a folder that
# holds none of the plant files synced nothing and the pass still said ok /
# nothing new, so Home's row stayed green. Such a folder is a problem now.
NO_PLANT_FILES_MSG = ("no plant files found in {folder} — expected the fs_data layout "
                      "(fs_vif, fs_manual) or the files on the conf list")


def empty_source_folders(entries, src_dirs, holding_workbook=()) -> list[Path]:
    """Folders in `src_dirs` that ARE reachable yet hold NONE of the files on
    the conf list, in conf order. A folder in `holding_workbook` (the ones
    refresh_demand_summaries found an AZAP workbook in) counts as holding
    plant files even while its demand_plan_summary.csv is not there yet: a
    settling or refused build is reported on its own. An unreachable folder
    is not listed (the caller already reports it), and neither is one whose
    probe fails mid-way (OSError): no proof, no problem."""
    held = [Path(h) for h in holding_workbook]
    out: list[Path] = []
    for d in src_dirs:
        d = Path(d)
        try:
            if not d.is_dir() or d in held or resolve_entries(entries, d):
                continue
        except OSError:
            continue
        out.append(d)
    return out


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([GIT_BIN, "-C", str(repo), *args],
                          capture_output=True, text=True)


# ── the drop mirror (2026-09-18): GitHub clone -> a local fs_data ───────────
# A planner's PC reads the SharePoint library that OneDrive syncs into
# fs_data\fs_vif + fs_data\fs_manual. The dev PC has no SharePoint; it has the
# private GitHub repo the work PC pushes the same files to. To run the dev PC
# EXACTLY like a planner's PC, the pass first drops the clone's files into a
# local fs_data by the conf's "drop_layout" (dest name -> fs_vif | fs_manual),
# then folder mode takes over from that root as on any planner PC. Rules that
# keep it an honest stand-in for the sync: GitHub's copy lands only when it
# is newer than the file already there (the commit's author time against the
# file's mtime — so a csv the pass itself rebuilt from the AZAP workbook, or a
# hand-dropped newer export, is never clobbered and the two never ping-pong),
# the landed file's mtime becomes that commit time (OneDrive keeps the
# SharePoint modified time the same way, and folder mode's manprg as-of reads
# it), and nothing is ever deleted.
MIRROR_KEY = "mirror_github_to"
LAYOUT_KEY = "drop_layout"


def layout_folder(dest: str, layout: dict) -> str | None:
    """The drop subfolder a conf file (by its DESTINATION name) lands in."""
    if not isinstance(layout, dict):
        return None
    for sub, names in layout.items():
        if isinstance(names, (list, tuple)) and dest in names:
            return str(sub)
    return None


def clone_asof(clone: Path, name: str) -> float | None:
    """Epoch seconds at which the clone's CURRENT content of `name` FIRST
    appeared in the repo: the author time of the earliest commit that
    introduced today's blob for that path (git log --find-object). The work
    PC commits right after the export, so for a fresh export that is the
    last commit's time; for a file the work PC pushed BACK to an older export
    (2026-09-18 10:08: manprg.txt returned to the 09-15 blob eight minutes
    after a fresh one) it is when that content was really first seen, so a
    revert never masquerades as a fresh observation and never outranks a
    fresher file already in the drop. Falls back to the last commit's time
    when the blob cannot be resolved; None when git cannot tell at all."""
    last = git(clone, "log", "-1", "--format=%at", "--", name)
    try:
        last_at = float(last.stdout.strip()) if last.returncode == 0 and last.stdout.strip() else None
    except ValueError:
        last_at = None
    head = git(clone, "rev-parse", f"HEAD:{name}")
    blob = head.stdout.strip() if head.returncode == 0 else ""
    if not blob:
        return last_at
    r = git(clone, "log", "--format=%at", f"--find-object={blob}", "--", name)
    if r.returncode != 0:
        return last_at
    times = []
    for tok in r.stdout.split():
        try:
            times.append(float(tok))
        except ValueError:
            continue
    return min(times) if times else last_at


def mirror_clone_into_drop(clone: Path, drop_root: Path, entries, layout: dict,
                           asof=None) -> tuple[list[str], list[str], list[str]]:
    """Drop the clone's conf files into drop_root/<fs_vif|fs_manual> by
    `layout`. Returns (mirrored, kept, problems): `mirrored` the names
    written (or re-timed: an identical copy whose time was later than the
    content's first appearance), `kept` the names left alone because the
    copy in the drop is newer than GitHub's (with the reason), `problems`
    files the layout does not route or that could not be written.
    `asof(name) -> epoch | None` is the clone's observation time per file
    (default clone_asof: when the current content FIRST appeared in the
    repo; the clone file's mtime when git cannot tell). A file lands under
    its own name (the dated "NPA Open POs*.xlsx" keeps its date; folder
    mode's glob renames it as it does on a planner PC)."""
    clone, drop_root = Path(clone), Path(drop_root)
    asof = asof or (lambda name: clone_asof(clone, name))
    mirrored: list[str] = []
    kept: list[str] = []
    problems: list[str] = []
    for src, dest in resolve_entries(entries, clone):
        sub = layout_folder(dest, layout)
        if sub is None:
            problems.append(f"{dest}: no {LAYOUT_KEY} entry (fs_vif or fs_manual?) — not mirrored")
            continue
        try:
            target_dir = drop_root / sub
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / src.name
            data = src.read_bytes()
            when = asof(src.name)
            if when is None:
                when = src.stat().st_mtime
            if target.exists() and target.read_bytes() == data:
                # same bytes: nothing to write — but a copy this mirror stamped
                # with a LATER time than the content's first appearance (a
                # re-push of old content carried a fresh commit time before
                # 2026-09-18) is put back to that time, so the drop never
                # shows a fresh date on old data
                if target.stat().st_mtime > when + 1.0:
                    os.utime(target, (when, when))
                    mirrored.append(f"{sub}/{src.name} (time set to "
                                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(when))})")
                continue
            if target.exists():
                have = target.stat().st_mtime
                if have > when + 1.0:
                    kept.append(f"{src.name} (drop copy newer: "
                                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(have))} vs GitHub "
                                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(when))})")
                    continue
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            os.utime(tmp, (when, when))
            if target.exists():
                try:
                    os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
            _replace_with_retry(tmp, target)
            mirrored.append(f"{sub}/{src.name}")
        except OSError as exc:
            problems.append(f"{dest}: mirror into {sub} failed: {exc}")
    return mirrored, kept, problems


# ── manprg as-of stamp (audit C01, 2026-09-03) ───────────────────────────────
# manprg.txt carries no observation timestamp and its counters are batch-
# posted, so the app must know WHEN the content it reads was observed. The
# pull stamps data/reference/manprg.asof.json whenever the manprg content
# changes (and once when the stamp is missing): {"as_of", "sha256", ...}.
# helpers/manprg_import.read_asof_stamp trusts it only while the sha matches.
MANPRG_FILES = ("manprg.txt", "manprg2.txt")
ASOF_STAMP_NAME = "manprg.asof.json"


def content_sha256(paths) -> str:
    """Mirrors helpers.manprg_import.content_sha256 VERBATIM (no import: the
    bridge may run without the repo on sys.path)."""
    h = hashlib.sha256()
    for p in paths:
        p = Path(p)
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def _local_iso(ts: str | None) -> str | None:
    """'2026-09-03T14:16:10-06:00' (git %aI) -> local naive '2026-09-03T14:16:10'.

    Every timestamp in the app is local wall-clock; a tz-aware stamp would be
    mis-read as UTC by a naive comparison."""
    if not ts:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts.strip())
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def manprg_observation_time(clone: Path) -> str | None:
    """Author time of the last commit touching a manprg file in the clone —
    the work PC commits right after the export, so it is the closest thing
    to the plant's observation time. None when git cannot tell."""
    best = None
    for name in MANPRG_FILES:
        r = git(clone, "log", "-1", "--format=%aI", "--", name)
        iso = _local_iso(r.stdout.strip()) if r.returncode == 0 else None
        if iso and (best is None or iso > best):
            best = iso
    return best


def manprg_source_asof(src_dirs) -> str | None:
    """Folder mode: newest manprg file mtime across the source folders as
    local naive ISO. The ERP wrote the file at that moment — the closest
    thing to the observation time (git author time plays that role in
    GitHub mode). None when no manprg file is reachable."""
    best: float | None = None
    for d in src_dirs:
        for name in MANPRG_FILES:
            p = Path(d) / name
            try:
                if p.is_file():
                    m = p.stat().st_mtime
                    if best is None or m > best:
                        best = m
            except OSError:
                continue
    if best is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(best))


def stamp_manprg_asof(ref_dir: Path, updated: list[str], *,
                      as_of: str | None = None, head: str | None = None,
                      ) -> bool:
    """Write the as-of stamp when manprg content changed or no stamp exists.

    `as_of` defaults to now (the moment the change was observed by the pull —
    at most one pull cadence late). Returns True when a stamp was written."""
    files = [ref_dir / n for n in MANPRG_FILES]
    if not any(f.is_file() for f in files):
        return False
    stamp = ref_dir / ASOF_STAMP_NAME
    changed = any(n in updated for n in MANPRG_FILES)
    if not changed and stamp.exists():
        return False
    stamp.write_text(json.dumps({
        "as_of": as_of or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sha256": content_sha256(files),
        "files": [f.name for f in files if f.is_file()],
        "stamped": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pull_head": head,
        "note": "time the manprg CONTENT was observed; helpers/manprg_import "
                "reads it (falls back to file mtime when the sha mismatches)",
    }, indent=2), encoding="utf-8")
    return True


def ensure_clone(conf: dict) -> Path:
    # Personal side uses its own clone location (differs from the work computer's).
    clone = Path(conf.get("clone_dir_personal") or conf["clone_dir"])
    if (clone / ".git").exists():
        return clone
    clone.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([GIT_BIN, "clone", conf["repo_url"], str(clone)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"clone failed — is the repo created + reachable?\n"
            f"  repo: {conf['repo_url']}\n"
            f"  {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}")
    return clone


@dataclass
class SyncResult:
    """What one pass did. `problems` are failures the planner should see
    (unreachable folder, unreadable file, derive failure); `skipped` are
    files left for the next pass on purpose (still being written)."""
    mode: str                                   # "folder" | "github"
    sources: list[str]
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    head: str | None = None                     # github mode: new HEAD when it advanced
    mirror: dict | None = None                  # drop mirror (2026-09-18): where GitHub landed

    @property
    def ok(self) -> bool:
        return not self.problems


def _replace_with_retry(tmp: Path, dst: Path, attempts: int = 5, pause_s: float = 0.2) -> None:
    """os.replace, retried briefly: on Windows the swap is refused while the
    app happens to hold the old copy open for reading (milliseconds), and a
    refused swap must not leave the stale copy in place for a whole pass."""
    for i in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(pause_s)


def copy_into_reference(pairs, ref_dir: Path, *, settle_seconds: float = 0.0,
                        settle_exempt=(),
                        ) -> tuple[list[str], list[str], list[str]]:
    """Copy each (src, dest_name) into ref_dir when its bytes differ.

    Returns (updated, skipped, problems). One busy or unreadable file costs
    only itself — a sharing violation on one export used to end the whole
    pass. With settle_seconds > 0 a source modified that recently is left
    for the next pass (an export may still be being written), and so is a
    file whose size or mtime moves while it is read. The local copy is
    written to a temp name and swapped in, so the app never reads a half
    copied file; the source mtime is kept because the health page ages
    files by it. `settle_exempt` (2026-09-18) names source paths the drop
    mirror wrote whole this very pass: their mtime is a commit time, not a
    write in progress, so the settle rule must not hold them back a pass
    (the fresh 10:00 manprg of 2026-09-18 waited exactly that way and was
    overwritten by a stale re-push before the next pass came)."""
    ref_dir = Path(ref_dir)
    exempt = {Path(x).resolve() for x in settle_exempt}
    updated: list[str] = []
    skipped: list[str] = []
    problems: list[str] = []
    for src, fname in pairs:
        dst = ref_dir / fname
        try:
            st0 = src.stat()
            age = time.time() - st0.st_mtime
            if settle_seconds and abs(age) < settle_seconds and src.resolve() not in exempt:
                skipped.append(f"{fname} (modified {age:.0f}s ago, settling)")
                continue
            data = src.read_bytes()
            st1 = src.stat()
            if (st1.st_mtime, st1.st_size) != (st0.st_mtime, st0.st_size):
                skipped.append(f"{fname} (changed while reading)")
                continue
            if dst.exists() and data == dst.read_bytes():
                continue
            tmp = dst.with_name(dst.name + ".tmp")
            tmp.write_bytes(data)
            os.utime(tmp, ns=(st0.st_atime_ns, st0.st_mtime_ns))
            if dst.exists():
                try:
                    os.chmod(dst, stat.S_IWRITE | stat.S_IREAD)  # a read-only copy blocks the swap on Windows
                except OSError:
                    pass
            _replace_with_retry(tmp, dst)
            updated.append(fname)
        except OSError as exc:
            problems.append(f"{fname}: {exc}")
    return updated, skipped, problems


def derive_demand_plan(ref_dir: Path) -> None:
    """demand_plan.csv is DERIVED, never copied: it is the summary exploded
    into per-week orders with due windows in the file's own anchor frame
    (staging re-bases at solve time). It used to be a manual Data-page
    import, which is how it sat 4 days stale while the summary refreshed
    underneath it (found 2026-08-15). Re-derive whenever the summary lands."""
    code_dir = Path(__file__).resolve().parent.parent / "code"
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))
    from helpers.demand_summary_import import import_summary

    dem, meta = import_summary(ref_dir / "demand_plan_summary.csv")
    dem.to_csv(ref_dir / "demand_plan.csv", index=False)
    (ref_dir / "demand_plan.source.json").write_text(json.dumps({
        "source": "demand_plan_summary.csv",
        "imported": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": meta.rows,
        "weeks": [int(w) for w in meta.weeks],
        "skus": len(meta.skus),
        "anchor": meta.anchor.strftime("%Y-%m-%d %H:%M:%S")
        if meta.anchor else None,
        "anchor_iso_week": meta.anchor_iso_week,
    }, indent=2), encoding="utf-8")


def refresh_demand_summaries(src_dirs, res: SyncResult, *,
                             settle_seconds: float = 0.0) -> list[Path]:
    """Folder mode (2026-09-15): the planners drop the weekly AZAP workbook
    ("New Export AZAP MMDDYY.xlsx", ~14 MB) into fs_manual instead of cutting
    demand_plan_summary.csv from it by hand. For every source folder that
    holds such a workbook, rebuild the folder's demand_plan_summary.csv
    unless it is up to date with it (helpers/azap_demand), BEFORE
    the copy step so the same pass carries it into data/reference and
    re-derives demand_plan.csv. This is the ONE file the sync ever writes
    into a source folder. A workbook modified less than settle_seconds ago
    may still be being copied in: it waits a pass, like any source file.
    Never raises: a failure is a problem entry, the rest of the pass runs.
    Returns the folders a workbook was found in (2026-09-17: they count as
    holding plant files for empty_source_folders, whatever the build did).

    2026-09-16: a build the helper refuses to write (0 rows, or under half
    the rows of the csv it would replace) raises ValueError, so it lands in
    `problems` on EVERY pass while the previous csv survives. A window taken
    from the workbook's mtime (no MMDDYY in the name) is not a problem but a
    warning: it rides on the "(built from ...)" note, which the heartbeat
    readers treat as a note and Home's sync row shows, and goes to stderr —
    and on EVERY later pass that keeps the csv built from that workbook it
    rides on an "(unchanged, from ...)" note, so it never drops off Home
    after one pass. A workbook the ranking passes over although it was
    saved after the one in use (helpers/azap_demand.passed_over_workbooks)
    is reported on every pass too: a doubtful name date (a year typo) under
    `problems`, anything else (an AutoSave on last week's export) as a
    "(NOTE: ...)" entry."""
    code_dir = Path(__file__).resolve().parent.parent / "code"
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))
    holders: list[Path] = []
    try:
        from helpers.azap_demand import (export_date_warning, find_azap_workbook,
                                         passed_over_workbooks, refresh_demand_summary,
                                         workbook_date_warning)
    except Exception as exc:  # noqa: BLE001 — a missing helper/openpyxl must not end the pass
        res.problems.append(f"demand_plan_summary.csv from AZAP workbook: helper unavailable ({exc})")
        return holders
    for d in src_dirs:
        wb = find_azap_workbook(d)
        if wb is None:
            continue
        holders.append(Path(d))
        for item in passed_over_workbooks(d, wb, settle_seconds=settle_seconds):
            if item["problem"]:
                print(f"[warn] {item['message']}", file=sys.stderr)
                res.problems.append(f"demand_plan_summary.csv: {item['message']}")
            else:
                res.updated.append(f"demand_plan_summary.csv (NOTE: {item['message']})")
        try:
            age = time.time() - wb.stat().st_mtime
            if settle_seconds and abs(age) < settle_seconds:
                res.skipped.append(f"{wb.name} (modified {age:.0f}s ago, settling)")
                continue
            out = refresh_demand_summary(d)
        except Exception as exc:  # noqa: BLE001 — the pass must not die on this
            print(f"[warn] demand_plan_summary.csv build from {wb.name} failed: {exc}",
                  file=sys.stderr)
            res.problems.append(f"demand_plan_summary.csv from {wb.name}: {exc}")
            continue
        if out.get("ran"):
            warning = export_date_warning(out.get("meta"))
            if warning:
                print(f"[warn] {warning}", file=sys.stderr)
                res.updated.append(f"demand_plan_summary.csv (built from {wb.name}; "
                                   f"WARNING: {warning})")
            else:
                res.updated.append(f"demand_plan_summary.csv (built from {wb.name})")
        elif out.get("from_workbook"):
            # the csv in use still comes from an undated workbook: say so every pass
            warning = workbook_date_warning(wb)
            if warning:
                res.updated.append(f"demand_plan_summary.csv (unchanged, from {wb.name}; "
                                   f"WARNING: {warning})")
    return holders


def write_sync_state(ref_dir: Path, result: SyncResult | None, *,
                     error: str | None = None) -> Path:
    """Heartbeat for the app (helpers/data_health row "live_sync"): what ran,
    from where, when it finished, what changed, what went wrong. Written
    after EVERY pass, including a failed one, so a silent scheduled task
    and a broken share both show up as age or as problems."""
    ref_dir = Path(ref_dir)
    ref_dir.mkdir(parents=True, exist_ok=True)
    problems = list(result.problems) if result else []
    if error:
        problems.append(error)
    body = {
        "mode": result.mode if result else None,
        "sources": list(result.sources) if result else [],
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ok": not problems,
        "updated": list(result.updated) if result else [],
        "skipped": list(result.skipped) if result else [],
        "problems": problems,
        "head": result.head if result else None,
        # the drop mirror (2026-09-18): {"from", "clone", "to", "head", "mirrored",
        # "kept"} when this machine feeds its own fs_data from the GitHub clone
        "mirror": result.mirror if result else None,
    }
    out = ref_dir / SYNC_STATE_NAME
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    return out


def pull_once(conf: dict) -> SyncResult:
    """One pass. Folder mode when the conf names source folders, else the
    GitHub clone. Copies changed files into data/reference, re-derives
    demand_plan.csv when the summary changed, stamps the manprg as-of and
    writes the live_sync.json heartbeat."""
    ref_dir = Path(conf["data_reference_dir"])
    ref_dir.mkdir(parents=True, exist_ok=True)
    # the drop mirror (2026-09-18): GitHub -> this machine's fs_data first, so the
    # folder-mode pass below reads exactly what a planner's PC would
    mirror_to = str(conf.get(MIRROR_KEY) or "").strip()
    mirror_info: dict | None = None
    mirror_problems: list[str] = []
    mirror_paths: list[Path] = []               # what the mirror wrote this pass
    if mirror_to:
        drop_root = Path(mirror_to)
        if not source_dirs(conf):
            conf = {**conf, "source_dirs": [str(drop_root)]}
        try:
            clone = ensure_clone(conf)
            git(clone, "fetch", conf["remote"], conf["branch"])
            before = git(clone, "rev-parse", "HEAD").stdout.strip()
            git(clone, "pull", "--ff-only", conf["remote"], conf["branch"])
            after = git(clone, "rev-parse", "HEAD").stdout.strip()
            mirrored, kept, mprb = mirror_clone_into_drop(
                clone, drop_root, conf["files"], conf.get(LAYOUT_KEY) or {})
            mirror_problems.extend(mprb)
            mirror_paths = [drop_root / m.split(" (", 1)[0] for m in mirrored]
            mirror_info = {"from": str(conf.get("repo_url", "")), "clone": str(clone),
                           "to": str(drop_root),
                           "head": after[:7] if after and after != before else None,
                           "mirrored": mirrored, "kept": kept}
        except Exception as exc:  # noqa: BLE001 — the drop keeps serving what it has
            mirror_problems.append(f"drop mirror from GitHub failed: {exc}")
            mirror_info = {"from": str(conf.get("repo_url", "")), "to": str(drop_root),
                           "head": None, "mirrored": [], "kept": []}
    # a conf naming the fs_data root stands for its fs_vif + fs_manual (2026-09-17):
    # the heartbeat, the reachability check, the AZAP rebuild, the copy and the
    # manprg as-of all see the subfolders
    dirs = expand_drop_roots(source_dirs(conf))
    if dirs:
        res = SyncResult(mode="folder", sources=[str(d) for d in dirs], mirror=mirror_info)
        res.problems.extend(mirror_problems)
        for d in dirs:
            try:
                reachable = d.is_dir()
            except OSError:
                reachable = False
            if not reachable:
                res.problems.append(f"source folder not reachable: {d}")
        settle = conf.get("settle_seconds", SETTLE_SECONDS_DEFAULT)
        try:
            settle = float(settle or 0)
        except (TypeError, ValueError):
            settle = float(SETTLE_SECONDS_DEFAULT)
        # the AZAP workbook -> demand_plan_summary.csv in its own folder,
        # before the copy so this pass picks the rebuilt csv up
        holders = refresh_demand_summaries(dirs, res, settle_seconds=settle)
        pairs = resolve_entries_multi(conf["files"], dirs)
        upd, skp, prb = copy_into_reference(pairs, ref_dir, settle_seconds=settle,
                                            settle_exempt=mirror_paths)
        # a reachable folder with none of the plant files is a problem, not a
        # quiet "nothing new" (the pass exits 1, Home's sync row goes STALE)
        for d in empty_source_folders(conf["files"], dirs, holders):
            res.problems.append(NO_PLANT_FILES_MSG.format(folder=d))
        as_of, stamp_head = manprg_source_asof(dirs), None
    else:
        clone = ensure_clone(conf)
        git(clone, "fetch", conf["remote"], conf["branch"])
        before = git(clone, "rev-parse", "HEAD").stdout.strip()
        git(clone, "pull", "--ff-only", conf["remote"], conf["branch"])
        after = git(clone, "rev-parse", "HEAD").stdout.strip()
        res = SyncResult(mode="github", sources=[str(conf.get("repo_url", "")), str(clone)],
                         head=after[:7] if after and after != before else None)
        upd, skp, prb = copy_into_reference(resolve_entries(conf["files"], clone), ref_dir)
        as_of, stamp_head = manprg_observation_time(clone), after[:7]
    # downtimes.csv is deliberately NOT in conf["files"] (removed 2026-08-21):
    # it is planner-owned wall-clock data edited ONLY in the app's Start-of-day
    # downtime strip (helpers/downtime_ui.py). Pulling it from the live repo
    # clobbered the planner's edits with the plant feed's stale copy.
    res.updated.extend(upd)
    res.skipped.extend(skp)
    res.problems.extend(prb)

    if "demand_plan_summary.csv" in res.updated:
        try:
            derive_demand_plan(ref_dir)
            res.updated.append("demand_plan.csv (derived)")
        except Exception as exc:  # noqa: BLE001 — the pass must not die on this
            print(f"[warn] demand_plan derive failed: {exc}", file=sys.stderr)
            res.problems.append(f"demand_plan derive failed: {exc}")

    # manprg content changed (or never stamped): record WHEN it was observed
    # so the running-MO re-forecast measures its rate up to that moment, not
    # the page's render clock (audit C01).
    try:
        if stamp_manprg_asof(ref_dir, res.updated, as_of=as_of, head=stamp_head):
            res.updated.append(f"{ASOF_STAMP_NAME} (stamped)")
    except Exception as exc:  # noqa: BLE001 — the pass must not die on this
        print(f"[warn] manprg as-of stamp failed: {exc}", file=sys.stderr)

    write_sync_state(ref_dir, res)
    return res


def _describe(res: SyncResult) -> str:
    if res.mode == "folder":
        where = "synced from " + "; ".join(res.sources)
        if res.mirror:
            m = res.mirror
            n = len(m.get("mirrored") or [])
            where = (f"GitHub -> {m.get('to')}: {n} file(s) dropped"
                     + (f" (pulled -> {m['head']})" if m.get("head") else "")
                     + (f", {len(m['kept'])} kept (drop copy newer)" if m.get("kept") else "")
                     + "; " + where)
    else:
        where = f"pulled -> {res.head}" if res.head else "github up to date"
    what = f"updated {', '.join(res.updated)}" if res.updated else "nothing new"
    msg = f"{where}: {what}"
    if res.skipped:
        msg += " | left for next pass: " + "; ".join(res.skipped)
    if res.problems:
        msg += " | PROBLEMS: " + "; ".join(res.problems)
    return msg


def main() -> int:
    conf = load_conf()
    ap = argparse.ArgumentParser(
        description="Flowstate live-data sync (GitHub clone or shared folder -> data/reference).")
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                    help="loop with this many seconds between passes (default: one pass)")
    ap.add_argument("--once", action="store_true",
                    help="one pass (the default; kept for the scheduled task)")
    ap.add_argument("--source-dir", action="append", default=None, metavar="FOLDER",
                    help="folder mode: copy from this folder (repeatable; overrides the conf)")
    ap.add_argument("--reference-dir", default=None, metavar="FOLDER",
                    help="where to copy to (default: data_reference_dir from the conf)")
    ap.add_argument("--mirror-github-to", default=None, metavar="FS_DATA",
                    help="drop mirror: pull the GitHub clone and drop its files into this "
                         "fs_data root's fs_vif / fs_manual first, then sync from it "
                         "(the dev PC's stand-in for the synced SharePoint folder)")
    args = ap.parse_args()
    if args.mirror_github_to:
        conf[MIRROR_KEY] = args.mirror_github_to
    if args.source_dir:
        conf["source_dirs"] = list(args.source_dir)
        # an ad-hoc source folder means "sync from THAT" — the machine's own
        # drop mirror stays out of it unless asked on the same command line
        if not args.mirror_github_to:
            conf.pop(MIRROR_KEY, None)
    if args.reference_dir:
        conf["data_reference_dir"] = args.reference_dir

    ok = True
    while True:
        ts = time.strftime("%H:%M:%S")
        try:
            res = pull_once(conf)
            ok = res.ok
            print(f"[{ts}] {_describe(res)}", flush=True)
        except Exception as e:  # noqa: BLE001 — report, keep the loop alive
            ok = False
            print(f"[{ts}] ERROR: {e}", flush=True)
            try:
                write_sync_state(Path(conf["data_reference_dir"]), None, error=str(e))
            except Exception:  # noqa: BLE001 — the heartbeat is best effort
                pass
        if args.watch <= 0 or args.once:
            break
        time.sleep(args.watch)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
