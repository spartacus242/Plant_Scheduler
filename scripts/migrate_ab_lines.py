#!/usr/bin/env python
"""Migrate a Flowstate data/ directory to genuine A/B double lines (P17-P22).

The Bossar lines P17-P22 are physically two parallel sides (A and B). This
script expands them into real per-side rows across every line-keyed reference
file, halving the per-side production rates.

What it rewrites (each file is backed up to data/_backups/<stem>.<ts>.csv):
  lines.csv                        P17 -> P17A + P17B rows, plus the new
                                   line_group / side / is_double columns.
  reference/capabilities_rates.csv each P17-P22 row -> A and B rows with
                                   calc_rate_kgph HALVED (nominal column dropped).
  reference/line_cip_hrs.csv       duplicated per side (same max_cip_hrs).
  reference/initial_states.csv     duplicated per side (same state).
  reference/downtimes.csv          a whole-line downtime becomes one row per
                                   side (both sides down = line down).

P09-P16 are single "Volpak" lines and are left completely untouched.
calendar_blocks.csv and reference/trials.csv are NOT rewritten: a block whose
line_name is the group (e.g. "P17") means "both sides", which stays valid.

The script is IDEMPOTENT -- running it twice changes nothing the second time.

Usage:
    python scripts/migrate_ab_lines.py [--data DIR] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "code"))

from helpers.lines_model import (  # noqa: E402
    DOUBLE_GROUPS,
    SIDES,
    group_of,
    is_double,
    side_line_id,
    side_of,
    sides_of,
)

RATE_COLUMNS = ("calc_rate_kgph",)  # nominal_rate_kgph dropped 2026-08-10


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _backup(path: Path, data_dir: Path, stamp: str) -> Path:
    bdir = data_dir / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest


def _write(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8", lineterminator="\r\n")
    tmp.replace(path)


def _already_split(df: pd.DataFrame) -> bool:
    """True when the frame already carries per-side rows for every group."""
    if "line_name" not in df.columns:
        return True
    names = {str(v).strip().upper() for v in df["line_name"]}
    targets = [g for g in DOUBLE_GROUPS if any(group_of(n) == g for n in names)]
    if not targets:
        return True
    return all(all(s in names for s in sides_of(g)) for g in targets)


def _halve(value: str) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    half = num / 2.0
    if abs(half - round(half)) < 1e-9:
        return str(int(round(half)))
    return f"{half:g}"


def _expand_rows(df: pd.DataFrame, *, halve_rates: bool = False) -> tuple[pd.DataFrame, int]:
    """Duplicate every double-line row into A and B side rows."""
    out: list[dict] = []
    made = 0
    for rec in df.to_dict("records"):
        name = str(rec.get("line_name", "") or "").strip().upper()
        group = group_of(name)
        if not is_double(name) or side_of(name) is not None:
            out.append(rec)
            continue
        for s in SIDES:
            new = dict(rec)
            new["line_name"] = f"{group}{s}"
            if "line_id" in new:
                new["line_id"] = str(side_line_id(group, s))
            if halve_rates:
                for col in RATE_COLUMNS:
                    if col in new:
                        new[col] = _halve(new[col])
            out.append(new)
            made += 1
    return pd.DataFrame(out, columns=list(df.columns)), made


# ---------------------------------------------------------------------------
# per-file migrations
# ---------------------------------------------------------------------------

def migrate_lines(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    notes: list[str] = []
    rows: list[dict] = []
    for rec in df.to_dict("records"):
        name = str(rec.get("line_name", "") or "").strip().upper()
        group = group_of(name)
        if not is_double(name):
            new = dict(rec)
            new["line_group"] = group
            new["side"] = ""
            new["is_double"] = "False"
            rows.append(new)
            continue
        if side_of(name) is not None:
            new = dict(rec)
            new["line_group"] = group
            new["side"] = side_of(name)
            new["is_double"] = "True"
            new["line_id"] = str(side_line_id(group, side_of(name)))
            rows.append(new)
            continue
        for s in SIDES:
            new = dict(rec)
            new["line_id"] = str(side_line_id(group, s))
            new["line_name"] = f"{group}{s}"
            new["line_group"] = group
            new["side"] = s
            new["is_double"] = "True"
            rows.append(new)
        notes.append(f"  {group} -> {group}A (id {side_line_id(group, 'A')}) + "
                     f"{group}B (id {side_line_id(group, 'B')})")
    cols = list(df.columns)
    for extra in ("line_group", "side", "is_double"):
        if extra not in cols:
            cols.append(extra)
    return pd.DataFrame(rows, columns=cols), notes


def migrate_caps(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    before = df[df["line_name"].astype(str).str.upper().isin(DOUBLE_GROUPS)]
    sample = ""
    if not before.empty:
        r = before.iloc[0]
        sample = (f"  e.g. {r['line_name']} sku {r['sku']}: "
                  f"calc {r.get('calc_rate_kgph')} -> {_halve(str(r.get('calc_rate_kgph')))}")
    out, made = _expand_rows(df, halve_rates=True)
    notes = [f"  {len(before)} double-line rows -> {made} per-side rows (rates halved)"]
    if sample:
        notes.append(sample)
    return out, notes


def migrate_simple(df: pd.DataFrame, label: str) -> tuple[pd.DataFrame, list[str]]:
    out, made = _expand_rows(df)
    return out, [f"  duplicated {made // 2} {label} row(s) into {made} per-side rows"]


FILES = [
    ("lines.csv", migrate_lines),
    ("reference/capabilities_rates.csv", migrate_caps),
    ("reference/line_cip_hrs.csv", lambda d: migrate_simple(d, "CIP")),
    ("reference/initial_states.csv", lambda d: migrate_simple(d, "initial-state")),
    ("reference/downtimes.csv", lambda d: migrate_simple(d, "downtime")),
]


def run(data_dir: Path, dry_run: bool) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"A/B double-line migration  (data: {data_dir})")
    print(f"mode: {'DRY RUN - nothing will be written' if dry_run else 'APPLY'}")
    print("")
    changed = 0
    for rel, fn in FILES:
        path = data_dir / rel
        if not path.exists():
            print(f"[skip]    {rel}  (missing)")
            continue
        df = _read(path)
        if "line_name" not in df.columns:
            print(f"[skip]    {rel}  (no line_name column)")
            continue
        if _already_split(df):
            print(f"[ok]      {rel}  (already migrated, {len(df)} rows)")
            continue
        out, notes = fn(df)
        changed += 1
        verb = "would rewrite" if dry_run else "rewrote"
        print(f"[change]  {rel}  {len(df)} -> {len(out)} rows  ({verb})")
        for n in notes:
            print(n)
        if not dry_run:
            b = _backup(path, data_dir, stamp)
            _write(out, path)
            print(f"  backup: _backups/{b.name}")
    print("")
    if changed == 0:
        print("Nothing to do -- data/ is already on the A/B model.")
    elif dry_run:
        print(f"{changed} file(s) would change. Re-run without --dry-run to apply.")
    else:
        print(f"{changed} file(s) migrated. Restore from data/_backups/*.{stamp}.csv to revert.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=str(_REPO_ROOT / "data"),
                    help="data directory to migrate (default: repo data/)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change without writing anything")
    args = ap.parse_args()
    data_dir = Path(args.data).resolve()
    if not data_dir.exists():
        print(f"ERROR: data directory not found: {data_dir}")
        return 2
    return run(data_dir, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
