#!/usr/bin/env python3
"""Flowstate live-data PUSH script — run on the WORK computer.

Watches the plant data files listed in fs-live-data.conf.json and pushes any
changes to the private `flowstate-live-data` GitHub repo. Run once per
scheduled task / every N minutes (or leave running with --watch).

Several source folders (2026-09-15 revision): the drop is split into the
ERP's own exports (fs_vif) and the files people maintain (fs_manual), so
"source_dirs_work" in the conf lists every folder to watch, in priority order
(see docs/vif_exports.md for the layout). A file present in more than one
folder is taken from wherever it is newest. When the list is empty or absent
the single "source_dir_work" (or SRC_DIR below) is watched as before. A listed
folder that holds fs_vif and/or fs_manual stands for those subfolders
(2026-09-17, expand_drop_roots), so the drop ROOT fs_data is a valid single
entry; a flat folder is watched as it is.

AZAP demand (2026-09-16): when this script runs from a repo checkout, a
watched folder holding "New Export AZAP MMDDYY.xlsx" first gets its
demand_plan_summary.csv rebuilt from it (rebuild_azap_summaries); a
hand-copied script prints "AZAP rebuild skipped: ..." and pushes as before.

This is the ONLY file that ever touches the plant network. It never runs AI —
it just copies files and git-pushes. One-way (plant -> GitHub). Nothing is
pulled back into the plant.

Behaviour (2026-09-04 revision):
  * the clone is brought up to date (fetch + pull --rebase) BEFORE new files
    are copied in, so a push never fights the personal laptop's history;
  * leftovers from a crashed earlier pass are committed first, otherwise the
    rebase would refuse to run on a dirty tree;
  * every git command that must succeed raises with its stdout/stderr - the
    watch loop prints the error and tries again on the next tick;
  * a non-fast-forward push is retried once after a fresh rebase;
  * the commit is verified (HEAD moved) before the push is attempted.

Usage:
    python fs-live-push.py                 # one pass: push if anything changed
    python fs-live-push.py --once          # same, explicit
    python fs-live-push.py --watch 300     # loop, checking every 300 s

One-time setup (run once from the work computer):
    1. Install Git for Windows (https://git-scm.com).
    2. Create the private repo flowstate-live-data on GitHub.
    3. Configure "source_dirs_work" (or SRC_DIR below) + your GitHub identity/
       token (git credential manager); set "clone_dir_work" in
       fs-live-data.conf.json.
    4. Run this script once; it clones the repo and starts pushing.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONF = Path(__file__).resolve().parent / "fs-live-data.conf.json"
# ── EDIT THESE on the work computer ──────────────────────────────────────
SRC_DIR = Path(r"C:\FlowstateLive\fs_live_data")  # where the plant system drops the raw files
# or set "source_dir_work" in fs-live-data.conf.json (e.g. the ERP drop folder on the
# share, 2026-09-14) and leave the constant alone; the conf value wins when present.
# "source_dirs_work" (a list, 2026-09-15) wins over both: every folder in it is
# watched, e.g. the ERP export folder plus the people-maintained folder.
GIT_BIN = shutil.which("git") or r"C:\Program Files\Git\bin\git.exe"
# ─────────────────────────────────────────────────────────────────────────


def load_conf() -> dict:
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


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


# ── several source folders (2026-09-15) ─────────────────────────────────────
# The drop of 2026-09-15 split the plant files over two folders (the ERP's
# exports and the files people maintain), so the work PC watches a LIST of
# folders. resolve_entries stays single-folder and byte-identical to the copy
# in fs-live-pull.py (a test pins it); the merge lives in these local helpers.

def source_dirs_work(conf: dict) -> list[Path]:
    """Folders to watch, in conf order: "source_dirs_work" when it is a
    non-empty list, else the single "source_dir_work" (or SRC_DIR)."""
    raw = conf.get("source_dirs_work")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        dirs = [Path(p.strip()) for p in raw if isinstance(p, str) and p.strip()]
        if dirs:
            return dirs
    return [Path(conf.get("source_dir_work") or SRC_DIR)]


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
    per destination name, in conf order. A folder that is not reachable
    contributes nothing (the others still push); a stat that fails mid-way
    drops only that candidate."""
    picked: dict[str, Path] = {}
    for d in src_dirs:
        d = Path(d)
        try:
            if not d.is_dir():
                print(f"Source folder not reachable, skipped: {d}", flush=True)
                continue
        except OSError as e:
            print(f"Source folder not reachable, skipped: {d} ({e})", flush=True)
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


# ── AZAP workbook -> demand_plan_summary.csv (2026-09-16) ───────────────────
# The planners drop the weekly "New Export AZAP MMDDYY.xlsx" into fs_manual
# instead of cutting demand_plan_summary.csv by hand. Folder-mode sync
# (fs-live-pull.py) rebuilds the csv on the planner's PC, but GitHub mode
# never did: this script pushed whatever csv sat in fs_manual. So before
# resolving the file list, each watched folder that holds a workbook gets
# the same rebuild — best-effort, only when this script runs from a repo
# checkout (the helper is code/helpers/azap_demand.py next to scripts/). A
# hand-copied single script cannot import it: it prints one line and pushes
# as before. Never raises.
AZAP_GLOB = "New Export AZAP*.xls[xm]"
AZAP_SETTLE_SECONDS_DEFAULT = 60   # a workbook modified this recently may still be copying in


def rebuild_azap_summaries(conf: dict, src_dirs) -> list[str]:
    """Rebuild demand_plan_summary.csv in every folder of `src_dirs` holding
    an AZAP workbook (helpers.azap_demand.refresh_demand_summary: unless the
    csv is up to date with it; a 0-row or much smaller build is refused and
    the previous csv kept). Prints and returns one line per thing that
    happened. Never raises.

    2026-09-16: every pass also says when the csv in use comes from a
    workbook with no MMDDYY in its name, and names every workbook the
    ranking passed over although it was saved after the one in use
    (helpers.azap_demand.passed_over_workbooks) — "AZAP rebuild WARNING:"
    for a doubtful name date (a year typo), "AZAP rebuild note:" otherwise."""
    lines: list[str] = []

    def say(msg: str) -> None:
        print(msg, flush=True)
        lines.append(msg)

    try:
        holders: list[Path] = []
        for d in src_dirs:
            d = Path(d)
            try:
                if d.is_dir() and any(p.is_file() for p in d.glob(AZAP_GLOB)):
                    holders.append(d)
            except OSError:
                continue                    # an unreachable folder is reported by the copy step
        if not holders:
            return lines
        try:
            code_dir = Path(__file__).resolve().parent.parent / "code"
            if not (code_dir / "helpers" / "azap_demand.py").is_file():
                raise ImportError(f"no repo checkout around this script "
                                  f"({code_dir} has no helpers/azap_demand.py)")
            if str(code_dir) not in sys.path:
                sys.path.insert(0, str(code_dir))
            from helpers.azap_demand import (export_date_warning, find_azap_workbook,
                                             passed_over_workbooks, refresh_demand_summary,
                                             workbook_date_warning)
        except Exception as exc:  # noqa: BLE001 — no helper / pandas / openpyxl: push as before
            say(f"AZAP rebuild skipped: {exc} - run scripts/azap_demand_summary.py")
            return lines
        try:
            settle = float(conf.get("settle_seconds", AZAP_SETTLE_SECONDS_DEFAULT) or 0)
        except (TypeError, ValueError):
            settle = float(AZAP_SETTLE_SECONDS_DEFAULT)
        for d in holders:
            passed: list[dict] = []
            try:
                wb = find_azap_workbook(d)
                if wb is None:
                    continue
                passed = passed_over_workbooks(d, wb, settle_seconds=settle)   # never raises
                age = time.time() - wb.stat().st_mtime
                if settle and abs(age) < settle:
                    say(f"AZAP rebuild: {wb.name} modified {age:.0f}s ago, settling - next pass")
                else:
                    out = refresh_demand_summary(d)
                    if out.get("ran"):
                        meta = out.get("meta") or {}
                        weeks = meta.get("weeks") or [None]
                        say(f"AZAP rebuild: demand_plan_summary.csv built from {wb.name} in {d}: "
                            f"{out.get('rows')} rows (W{weeks[0]}..W{weeks[-1]})")
                        warning = export_date_warning(meta)
                    else:
                        say(f"AZAP rebuild: demand_plan_summary.csv in {d} {out.get('reason')}")
                        warning = workbook_date_warning(wb) if out.get("from_workbook") else None
                    if warning:
                        say(f"AZAP rebuild WARNING: {warning}")
            except Exception as exc:  # noqa: BLE001 — a bad workbook costs only the rebuild
                say(f"AZAP rebuild failed in {d}: {exc} - the existing "
                    f"demand_plan_summary.csv is pushed unchanged")
            for item in passed:
                say(f"AZAP rebuild {'WARNING' if item['problem'] else 'note'}: {item['message']}")
    except Exception as exc:  # noqa: BLE001 — never end the push over the rebuild
        say(f"AZAP rebuild skipped: {exc} - run scripts/azap_demand_summary.py")
    return lines


# ── git helpers ─────────────────────────────────────────────────────────────

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in `repo`. With check=True (default) a non-zero exit raises a
    RuntimeError carrying the command and both output streams, so nothing
    fails silently. Use check=False for commands whose non-zero exit is a
    normal answer (e.g. `commit` with nothing to commit)."""
    result = subprocess.run(
        [GIT_BIN, "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode})\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def log_git(label: str, result: subprocess.CompletedProcess) -> None:
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if out:
        print(f"[git {label}] {out}", flush=True)
    if err:
        print(f"[git {label}] {err}", flush=True)


def ensure_clone(conf: dict) -> Path:
    # Work side uses its own clone location (differs from the personal laptop's).
    clone = Path(conf.get("clone_dir_work") or conf["clone_dir"])
    if (clone / ".git").exists():
        return clone
    clone.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {conf['repo_url']} into {clone} ...", flush=True)
    subprocess.run([GIT_BIN, "clone", conf["repo_url"], str(clone)],
                   check=True, text=True)
    return clone


def head_sha(clone: Path) -> str:
    r = git(clone, "rev-parse", "--verify", "HEAD", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def commits_ahead(clone: Path, remote: str, branch: str) -> int:
    """Local commits not yet on <remote>/<branch> (0 when in sync or unknown)."""
    r = git(clone, "rev-list", "--count", f"{remote}/{branch}..HEAD", check=False)
    try:
        return int(r.stdout.strip()) if r.returncode == 0 else 0
    except ValueError:
        return 0


def commit_leftovers(clone: Path) -> bool:
    """A crashed earlier pass can leave copied-but-uncommitted files; a rebase
    refuses to run on a dirty tree. Commit them under an honest message so
    nothing is lost and the sync can proceed. Returns True when it did."""
    status = git(clone, "status", "--porcelain")
    if not status.stdout.strip():
        return False
    print("Uncommitted leftovers from an earlier pass — committing them first:",
          flush=True)
    print(status.stdout, flush=True)
    git(clone, "add", "-A")
    r = git(clone, "commit", "-m", "plant data update (recovered leftovers)",
            check=False)
    log_git("commit", r)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr).lower():
        raise RuntimeError(f"recovery commit failed:\n{r.stdout}\n{r.stderr}")
    return True


def sync_with_remote(clone: Path, remote: str, branch: str) -> None:
    """Bring the local branch up to date before adding new commits."""
    commit_leftovers(clone)
    print("Fetching remote changes...", flush=True)
    log_git("fetch", git(clone, "fetch", remote))
    print("Rebasing onto the latest remote branch...", flush=True)
    log_git("pull --rebase", git(clone, "pull", "--rebase", remote, branch))


def push_with_retry(clone: Path, remote: str, branch: str) -> None:
    """Push; on a non-fast-forward rejection (someone pushed meanwhile),
    rebase once more and retry once. Anything else raises."""
    r = git(clone, "push", remote, branch, check=False)
    log_git("push", r)
    if r.returncode == 0:
        return
    text = (r.stdout + r.stderr).lower()
    if "non-fast-forward" in text or "fetch first" in text or "rejected" in text:
        print("Push rejected (remote moved) — rebasing and retrying once...",
              flush=True)
        log_git("pull --rebase", git(clone, "pull", "--rebase", remote, branch))
        log_git("push", git(clone, "push", remote, branch))
        return
    raise RuntimeError(f"push failed (exit {r.returncode})\n{r.stdout}\n{r.stderr}")


# ── one pass ────────────────────────────────────────────────────────────────

def push_once(conf: dict) -> list[str]:
    """Sync, copy changed files into the clone, commit, push.
    Returns the list of pushed filenames ([] when nothing changed)."""
    clone = ensure_clone(conf)
    remote, branch = conf["remote"], conf["branch"]

    sync_with_remote(clone, remote, branch)

    # a conf naming the fs_data root stands for its fs_vif + fs_manual (2026-09-17),
    # then rebuild demand_plan_summary.csv from the AZAP workbook first
    # (best-effort, 2026-09-16) so this pass pushes the fresh csv
    src_dirs = expand_drop_roots(source_dirs_work(conf))
    rebuild_azap_summaries(conf, src_dirs)

    changed: list[str] = []
    for src, fname in resolve_entries_multi(conf["files"], src_dirs):
        dst = clone / fname
        if dst.exists():
            try:
                if src.read_bytes() == dst.read_bytes():
                    continue  # unchanged — skip
            except OSError as e:
                print(f"Comparison failed for {fname}: {e} — copying anyway",
                      flush=True)
        shutil.copy2(src, dst)
        changed.append(fname)

    if not changed:
        # Nothing new from the plant - but a recovered-leftovers commit (or an
        # earlier pass whose push failed) may still be waiting locally. Push it
        # now; otherwise it sits unpushed until the next real change.
        ahead = commits_ahead(clone, remote, branch)
        if ahead:
            print(f"No file changes, but {ahead} local commit(s) not yet on "
                  f"{remote}/{branch} - pushing them.", flush=True)
            push_with_retry(clone, remote, branch)
        else:
            print("No file changes detected.", flush=True)
        return []

    print(f"Changed files: {', '.join(changed)}", flush=True)
    git(clone, "add", "-A")
    log_git("status", git(clone, "status", "--short"))

    before = head_sha(clone)
    msg = f"plant data update: {', '.join(changed)}"
    r = git(clone, "commit", "-m", msg, check=False)
    log_git("commit", r)
    if r.returncode != 0:
        if "nothing to commit" in (r.stdout + r.stderr).lower():
            print("Nothing new to commit.", flush=True)
            return []
        raise RuntimeError(f"commit failed (exit {r.returncode})\n{r.stdout}\n{r.stderr}")
    after = head_sha(clone)
    if not after or after == before:
        raise RuntimeError("commit reported success but HEAD did not move")

    push_with_retry(clone, remote, branch)
    return changed


def main() -> None:
    conf = load_conf()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--watch", type=int, default=0,
                    help="if set, loop forever checking every N seconds")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    args = ap.parse_args()

    while True:
        try:
            pushed = push_once(conf)
            stamp = time.strftime("%H:%M:%S")
            if pushed:
                print(f"[{stamp}] pushed: {', '.join(pushed)}", flush=True)
            else:
                print(f"[{stamp}] nothing changed", flush=True)
        except Exception as e:  # noqa: BLE001 — the loop must survive a bad tick
            print(f"[{time.strftime('%H:%M:%S')}] ERROR:\n{e}", flush=True)
        if args.watch <= 0 or args.once:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
