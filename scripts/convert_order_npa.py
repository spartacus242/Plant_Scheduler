#!/usr/bin/env python3
"""Convert the ERP's PO-line export (order_npa.csv: Sage X3, pipe-delimited,
Windows-1252, 13-digit fixed-point numbers, DD/MM/YYYY dates, no quoting)
into files Excel opens cleanly — all 219 columns, decoded, nothing dropped.

Excel mangles the raw file (it assumes commas, so "SOLON, OH" splits and the
one literal inch mark opens a quote that never closes). The app reads the
raw file directly (stockcheck/x3_po_export); this script is for people.

Usage:
    python scripts/convert_order_npa.py
        data/reference/order_npa.csv -> order_npa.clean.xlsx + .clean.csv next to it
    python scripts/convert_order_npa.py path/to/order_npa.csv --xlsx out.xlsx --csv out.csv
    python scripts/convert_order_npa.py --french          # French headers on the csv
    python scripts/convert_order_npa.py --summary         # structure/health report only

The xlsx has two sheets: "PO lines" (English keys, typed cells: real dates
and numbers, codes as text so '000048' keeps its zeros) and "Columns" (key
<-> French header <-> kind). The csv is UTF-8 with BOM, comma, quoted.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

from stockcheck.x3_po_export import (  # noqa: E402
    LAYOUT_VERSION, read_x3_po_export, to_po_lines, write_clean_csv,
    write_clean_xlsx)

DEFAULT_SRC = ROOT / "data" / "reference" / "order_npa.csv"


def summary(ex) -> str:
    lines = to_po_lines(ex)
    c = collections.Counter
    dates = sorted(ln["receipt_date"] for ln in lines if ln["receipt_date"])
    odates = sorted(ln["order_date"] for ln in lines if ln["order_date"])
    out = [
        f"file        : {ex.source_path}",
        f"modified    : {ex.source_mtime}",
        f"layout      : {LAYOUT_VERSION} — header {'OK' if ex.header_ok else 'DRIFTED'}, "
        f"{len(ex.keys)} columns",
        f"rows        : {ex.n_rows} PO lines, {len({ln['po8'] for ln in lines})} POs, "
        f"{len({ln['item'] for ln in lines})} items, {len({ln['supplier'] for ln in lines})} suppliers",
        f"open lines  : {sum(1 for ln in lines if not ln['received'])} still inbound "
        f"(qty remaining > 0); {sum(1 for ln in lines if ln['received'])} nothing left to receive",
        f"status codes: {dict(sorted(c(ln['status'] for ln in lines).items(), key=str))}",
        f"receipt site: {dict(c(ln['arrival_area'] for ln in lines).most_common())}",
        f"order units : {dict(c(ln['unit'] for ln in lines).most_common())}",
        f"order dates : {odates[0] if odates else '—'} .. {odates[-1] if odates else '—'}",
        f"receipt date: {dates[0] if dates else '—'} .. {dates[-1] if dates else '—'}",
        f"problems    : {len(ex.errors)}",
    ]
    out += [f"  - {e}" for e in ex.errors[:15]]
    if len(ex.errors) > 15:
        out.append(f"  … {len(ex.errors) - 15} more")
    if ex.unknown_columns:
        out.append(f"unknown columns (kept as extra_<n>): {ex.unknown_columns}")
    if ex.missing_columns:
        out.append(f"missing layout columns: {ex.missing_columns}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src", nargs="?", default=str(DEFAULT_SRC),
                    help=f"the raw export (default: {DEFAULT_SRC})")
    ap.add_argument("--xlsx", help="output workbook (default: <src>.clean.xlsx)")
    ap.add_argument("--csv", help="output clean csv (default: <src>.clean.csv)")
    ap.add_argument("--french", action="store_true",
                    help="French header names on the csv (default: English keys)")
    ap.add_argument("--summary", action="store_true",
                    help="print the structure/health summary, write nothing")
    args = ap.parse_args(argv)

    src = Path(args.src)
    ex = read_x3_po_export(src)
    print(summary(ex))
    if not ex.rows:
        return 1
    if args.summary:
        return 0
    stem = src.with_suffix("")
    xlsx = Path(args.xlsx) if args.xlsx else stem.with_name(stem.name + ".clean.xlsx")
    csv_ = Path(args.csv) if args.csv else stem.with_name(stem.name + ".clean.csv")
    write_clean_xlsx(ex, xlsx)
    write_clean_csv(ex, csv_, french_names=args.french)
    print(f"wrote       : {xlsx}\n              {csv_}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
