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
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONF = Path(__file__).resolve().parent / "fs-live-data.conf.json"
# Per-machine overrides (git-ignored): clone_dir_personal / data_reference_dir
# on a planner's PC, written by scripts/install_flowstate.ps1 -LiveData. The
# tracked conf keeps the shared keys (repo, branch, file list).
LOCAL_CONF = CONF.with_name("fs-live-data.local.json")
GIT_BIN = shutil.which("git") or r"C:\Program Files\Git\bin\git.exe"


def load_conf():
    with open(CONF, encoding="utf-8") as f:
        conf = json.load(f)
    if LOCAL_CONF.is_file():
        # utf-8-sig: PowerShell 5.1 writes a BOM with -Encoding utf8
        with open(LOCAL_CONF, encoding="utf-8-sig") as f:
            conf.update(json.load(f))
    return conf

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


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([GIT_BIN, "-C", str(repo), *args],
                          capture_output=True, text=True)


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
    # downtimes.csv is deliberately NOT in conf["files"] (removed 2026-08-21):
    # it is planner-owned wall-clock data edited ONLY in the app's Start-of-day
    # downtime strip (helpers/downtime_ui.py). Pulling it from the live repo
    # clobbered the planner's edits with the plant feed's stale copy.
    for src, fname in resolve_entries(conf["files"], clone):
        dst = ref_dir / fname
        if dst.exists() and src.read_bytes() == dst.read_bytes():
            continue
        shutil.copy2(src, dst)
        updated.append(fname)

    # demand_plan.csv is DERIVED, never pulled: it is the summary exploded
    # into per-week orders with due windows in the file's own anchor frame
    # (staging re-bases at solve time). It used to be a manual Data-page
    # import, which is how it sat 4 days stale while the summary refreshed
    # underneath it (found 2026-08-15). Re-derive whenever the summary lands.
    if "demand_plan_summary.csv" in updated:
        try:
            code_dir = Path(__file__).resolve().parent.parent / "code"
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
            updated.append("demand_plan.csv (derived)")
        except Exception as exc:  # noqa: BLE001 — pull must not die on this
            print(f"[warn] demand_plan derive failed: {exc}", file=sys.stderr)

    # manprg content changed (or never stamped): record WHEN it was observed
    # so the running-MO re-forecast measures its rate up to that moment, not
    # the page's render clock (audit C01).
    try:
        if stamp_manprg_asof(ref_dir, updated,
                             as_of=manprg_observation_time(clone),
                             head=after[:7]):
            updated.append(f"{ASOF_STAMP_NAME} (stamped)")
    except Exception as exc:  # noqa: BLE001 — pull must not die on this
        print(f"[warn] manprg as-of stamp failed: {exc}", file=sys.stderr)

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
