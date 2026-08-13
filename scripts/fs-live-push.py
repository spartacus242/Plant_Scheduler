#!/usr/bin/env python3
"""Flowstate live-data PUSH script — run on the WORK computer.

Watches the plant data files listed in fs-live-data.conf.json and pushes any
changes to the private `flowstate-live-data` GitHub repo. Run once per
scheduled task / every N minutes (or leave running in the background).

This is the ONLY file that ever touches the plant network. It never runs AI —
it just copies files and git-pushes. One-way (plant -> GitHub). Nothing is
pulled back into the plant.

Usage:
    python fs-live-push.py                 # one pass: push if anything changed
    python fs-live-push.py --watch 300     # loop, checking every 300s

One-time setup (run once from the work computer):
    1. Install Git for Windows (https://git-scm.com).
    2. Create the private repo flowstate-live-data on GitHub (account below).
    3. Configure the source dir below + your GitHub identity/token.
    4. Run this script once; it clones the repo and starts pushing.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONF = Path(__file__).resolve().parent / "fs-live-data.conf.json"
# ── EDIT THESE on the work computer ──────────────────────────────────────
SRC_DIR = Path(r"C:\PlantData")  # where the plant system drops the raw files
GIT_BIN = shutil.which("git") or r"C:\Program Files\Git\bin\git.exe"
# ─────────────────────────────────────────────────────────────────────────


def load_conf():
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [GIT_BIN, "-C", str(repo), *args],
        capture_output=True, text=True,
    )


def ensure_clone(conf: dict) -> Path:
    clone = Path(conf["clone_dir"])
    if (clone / ".git").exists():
        return clone
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([GIT_BIN, "clone", conf["repo_url"], str(clone)],
                   check=True, text=True)
    return clone


def push_once(conf: dict) -> list[str]:
    """Copy changed files into the clone and push. Returns pushed filenames."""
    clone = ensure_clone(conf)
    pushed: list[str] = []
    for fname in conf["files"]:
        src = SRC_DIR / fname
        dst = clone / fname
        if not src.exists():
            continue
        if dst.exists() and src.read_bytes() == dst.read_bytes():
            continue  # unchanged — skip
        shutil.copy2(src, dst)
        pushed.append(fname)
    if not pushed:
        return []
    # Commit + push whatever changed
    git(clone, "add", "-A")
    msg = f"plant data update: {', '.join(pushed)}"
    git(clone, "commit", "-m", msg)
    r = git(clone, "push", conf["remote"], conf["branch"])
    if r.returncode != 0:
        raise RuntimeError(f"push failed: {r.stderr}")
    return pushed


def main() -> None:
    conf = load_conf()
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0,
                    help="if set, loop forever checking every N seconds")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    args = ap.parse_args()

    while True:
        try:
            pushed = push_once(conf)
            if pushed:
                print(f"[{time.strftime('%H:%M:%S')}] pushed: {', '.join(pushed)}", flush=True)
            else:
                print(f"[{time.strftime('%H:%M:%S')}] nothing changed", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{time.strftime('%H:%M:%S')}] ERROR: {e}", flush=True)
        if args.watch <= 0 or args.once:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
