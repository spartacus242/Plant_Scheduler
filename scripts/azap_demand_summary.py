#!/usr/bin/env python3
"""Rebuild demand_plan_summary.csv from the planners' AZAP workbook (2026-09-15).

The weekly "New Export AZAP MMDDYY.xlsx" (sometimes .xlsm) lands in the
fs_manual folder; this cuts the app's small demand summary from it the way it
used to be done by hand (recipe: helpers/azap_demand.py). The live-data sync
(scripts/fs-live-pull.py, folder mode) does the same thing on every pass; run
this by hand to force a rebuild, to preview the rows, or to write elsewhere.

Folder (2026-09-16): --folder, else the first of the live-data sync's source
folders ("source_dirs", or the single "source_dir", from
scripts/fs-live-data.conf.json merged with the machine's
fs-live-data.local.json that the installer writes) that holds an AZAP
workbook. No folder is hard-coded: a machine without a folder-mode conf
(the dev laptop, the work PC) passes --folder.

Write guards (helpers/azap_demand.write_refusal): a build with 0 rows is never
written, and one with fewer than half the rows of the csv it would replace
is written only with --force; both exit 1 and keep the previous csv. A
window taken from the workbook's modified date (no MMDDYY in the name) is
printed as a WARNING, and so is (2026-09-16) a workbook saved after the one
in use whose name date is doubtful (a year typo such as 091825); another
workbook saved later but ranked older (an AutoSave) is printed as a note.

Up to date (2026-09-16): the csv carries the selected workbook's modified
time (it was built from it), or it is newer than every AZAP workbook in the
folder (a hand edit); anything else is rebuilt.

Usage:
    python azap_demand_summary.py                       # the configured folder, rebuild when stale
    python azap_demand_summary.py --dry-run             # print the rows, write nothing
    python azap_demand_summary.py --force --weeks 8     # rebuild even when up to date / much smaller
    python azap_demand_summary.py --folder D:/drop --out C:/tmp/demand_plan_summary.csv

Exit code 1 on any error (no workbook, no configured folder with one, missing
sheet/column, unreadable file, a refused write).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# the helper lives in the repo's code/ dir (same idiom as fs-live-pull.py)
_CODE_DIR = Path(__file__).resolve().parent.parent / "code"
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

# the live-data sync's conf pair (same files and merge order as fs-live-pull.py)
CONF = Path(__file__).resolve().parent / "fs-live-data.conf.json"
LOCAL_CONF = CONF.with_name("fs-live-data.local.json")
NO_FOLDER_MSG = "no AZAP workbook in the configured source folders; pass --folder"


def load_conf() -> tuple[dict, list[str]]:
    """The tracked conf updated with the per-machine local conf (both read as
    utf-8-sig: PowerShell 5.1 writes a BOM). Returns (conf, problems); an
    unreadable file is a problem entry, never a crash."""
    conf: dict = {}
    problems: list[str] = []
    for p in (CONF, LOCAL_CONF):
        try:
            if not p.is_file():
                continue
            with open(p, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            problems.append(f"{p.name} unreadable: {exc}")
            continue
        if isinstance(data, dict):
            conf.update(data)
        else:
            problems.append(f"{p.name} is not a JSON object")
    return conf, problems


def configured_source_dirs(conf: dict) -> list[Path]:
    """"source_dirs" (list or string), else "source_dir" — the folder-mode
    keys of fs-live-pull.py's source_dirs()."""
    raw = conf.get("source_dirs")
    if not raw:
        raw = conf.get("source_dir") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [Path(p.strip()) for p in raw if isinstance(p, str) and p.strip()]


def default_folder(find_workbook) -> tuple[Path | None, str]:
    """(first configured source folder holding an AZAP workbook, detail for
    the error message when there is none)."""
    conf, problems = load_conf()
    dirs = configured_source_dirs(conf)
    for d in dirs:
        if find_workbook(d) is not None:
            return d, ""
    detail = ("checked: " + "; ".join(str(d) for d in dirs)) if dirs else \
        f"no source_dirs / source_dir in {CONF.name} or {LOCAL_CONF.name}"
    if problems:
        detail += "; " + "; ".join(problems)
    return None, detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild demand_plan_summary.csv from the newest 'New Export AZAP MMDDYY.xlsx'.")
    ap.add_argument("--folder", default=None, metavar="DIR",
                    help="folder holding the workbook (default: the first folder in the live-data "
                         "conf's source_dirs / source_dir that holds one)")
    ap.add_argument("--weeks", type=int, default=7, metavar="N",
                    help="ISO weeks in the window, from the first Monday after the export date (default 7)")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even when the csv is up to date, and even when the "
                         "build has under half the existing csv's rows (a 0-row build is never written)")
    ap.add_argument("--out", default=None, metavar="CSV",
                    help="write here instead of <folder>/demand_plan_summary.csv")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the rows that would be written; write nothing")
    args = ap.parse_args(argv)

    try:
        from helpers.azap_demand import (CSV_NAME, build_demand_summary, export_date_warning,
                                         find_azap_workbook, passed_over_workbooks,
                                         refresh_demand_summary, summary_csv_text,
                                         workbook_date_warning, write_refusal)

        if args.folder:
            folder = Path(args.folder)
        else:
            found, detail = default_folder(find_azap_workbook)
            if found is None:
                print(f"ERROR: {NO_FOLDER_MSG} ({detail})", file=sys.stderr)
                return 1
            folder = found
            print(f"folder: {folder} (first source folder in the live-data conf holding an AZAP workbook)")
        # (2026-09-16) a workbook passed over although saved after the one in
        # use is never silent: a doubtful name date (a year typo) or an AutoSave
        for item in passed_over_workbooks(folder):
            print(f"{'WARNING' if item['problem'] else 'note'}: {item['message']}", file=sys.stderr)
        if args.dry_run:
            wb = find_azap_workbook(folder)
            if wb is None:
                print(f"ERROR: no 'New Export AZAP*.xlsx|xlsm' in {folder}", file=sys.stderr)
                return 1
            df, meta = build_demand_summary(wb, weeks=args.weeks)
            print(f"dry run: {wb.name} (export date {meta['export_date']} from "
                  f"{meta['export_date_source']}) -> window W{meta['weeks'][0]}..W{meta['weeks'][-1]} "
                  f"({meta['first_monday']} .. {meta['last_monday']}), {meta['rows_in']} rows read, "
                  f"{meta['rows_kept']} kept, {len(df)} summary rows; dropped products: "
                  f"{meta['dropped_products'] or 'none'}; nothing written")
            warning = export_date_warning(meta)
            if warning:
                print(f"WARNING: {warning}", file=sys.stderr)
            # the same write guards a real run applies (2026-09-16), as a preview
            refusal = write_refusal(len(df), meta, Path(args.out) if args.out else folder / CSV_NAME,
                                    force=args.force)
            if refusal:
                print(f"WARNING: a real run would refuse to write: {refusal}", file=sys.stderr)
            sys.stdout.write(summary_csv_text(df).replace("\r\n", "\n"))
            return 0

        # refused writes (0 rows / under half the existing rows) raise ValueError -> exit 1
        res = refresh_demand_summary(folder, weeks=args.weeks, force=args.force, out_path=args.out)
        if res["ran"]:
            meta = res["meta"]
            print(f"{Path(res['workbook']).name} -> {res['csv']}: {res['rows']} rows "
                  f"(W{meta['weeks'][0]}..W{meta['weeks'][-1]}, export date {meta['export_date']}; "
                  f"{res['reason']})")
            warning = export_date_warning(meta)
            if warning:
                print(f"WARNING: {warning}", file=sys.stderr)
            return 0
        if res["reason"] == "up to date":
            why = (f"built from {Path(res['workbook']).name}" if res.get("from_workbook")
                   else "newer than every AZAP workbook in the folder: a hand edit")
            print(f"{res['csv']} is up to date ({why}); use --force to rebuild")
            warning = workbook_date_warning(res["workbook"], weeks=args.weeks) \
                if res.get("from_workbook") else None
            if warning:
                print(f"WARNING: {warning}", file=sys.stderr)
            return 0
        print(f"ERROR: {res['reason']}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — one line, exit 1, for the scheduler / shell
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
