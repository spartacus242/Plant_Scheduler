#!/usr/bin/env python3
"""Flowstate live-data PULL script — run on the PERSONAL computer (Hermes).

Pulls the latest plant data from the private `flowstate-live-data` GitHub repo
into the local clone, then copies any changed files into the tool's
data/reference/ directory. Read-only with respect to GitHub (pull only, never
pushes). Safe to run on a cron cadence: skips cleanly when nothing changed.

Usage:
    python fs-live-pull.py            # one pass
    python fs-live-pull.py --watch 900   # loop every 15 min
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONF = Path(__file__).resolve().parent / "fs-live-data.conf.json"
GIT_BIN = shutil.which("git") or r"C:\Program Files\Git\bin\git.exe"


def load_conf():
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([GIT_BIN, "-C", str(repo), *args],
                          capture_output=True, text=True)


def ensure_clone(conf: dict) -> Path:
    clone = Path(conf["clone_dir"])
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


def pull_once(conf: dict) -> list[str]:
    """git pull, then copy changed files into data/reference. Returns names."""
    clone = ensure_clone(conf)
    git(clone, "fetch", conf["remote"], conf["branch"])
    before = git(clone, "rev-parse", "HEAD").stdout.strip()
    git(clone, "pull", "--ff-only", conf["remote"], conf["branch"])
    after = git(clone, "rev-parse", "HEAD").stdout.strip()

    ref_dir = Path(conf["data_reference_dir"])
    ref_dir.mkdir(parents=True, exist_ok=True)
    updated: list[str] = []
    for fname in conf["files"]:
        src = clone / fname
        dst = ref_dir / fname
        if not src.exists():
            continue
        if dst.exists() and src.read_bytes() == dst.read_bytes():
            continue
        shutil.copy2(src, dst)
        updated.append(fname)
    # Report new HEAD only if we actually advanced
    changed = before != after
    return (updated, after[:7]) if changed else (updated, None)


def main() -> None:
    conf = load_conf()
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    while True:
        try:
            updated, head = pull_once(conf)
            ts = time.strftime("%H:%M:%S")
            if head:
                print(f"[{ts}] pulled -> {head}: updated {', '.join(updated) or 'nothing'}", flush=True)
            elif updated:
                print(f"[{ts}] data changed in place: {', '.join(updated)}", flush=True)
            else:
                print(f"[{ts}] up to date", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{ts if 'ts' in dir() else ''}] ERROR: {e}", flush=True)
        if args.watch <= 0 or args.once:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
