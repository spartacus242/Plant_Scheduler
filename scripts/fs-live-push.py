#!/usr/bin/env python3
"""Flowstate live-data PUSH script — run on the WORK computer.

Watches the plant data files listed in fs-live-data.conf.json and pushes any
changes to the private `flowstate-live-data` GitHub repo. Run once per
scheduled task / every N minutes (or leave running with --watch).

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
    3. Configure SRC_DIR below + your GitHub identity/token (git credential
       manager); set "clone_dir_work" in fs-live-data.conf.json.
    4. Run this script once; it clones the repo and starts pushing.
"""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

CONF = Path(__file__).resolve().parent / "fs-live-data.conf.json"
# ── EDIT THESE on the work computer ──────────────────────────────────────
SRC_DIR = Path(r"C:\FlowstateLive\fs_live_data")  # where the plant system drops the raw files
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

    changed: list[str] = []
    for src, fname in resolve_entries(conf["files"], SRC_DIR):
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
