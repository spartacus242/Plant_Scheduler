"""QA helper: verify CIP max-interval compliance and cross-week movement.

Usage:
    python scripts/check_cip_and_weeks.py <work_dir> [<work_dir> ...]

For each solver work dir it reports:
  * changeover count and average campaign (run) length from schedule_phase2.csv
  * per-line CIP gaps from cip_windows.csv vs that line's max_cip_hrs in
    line_cip_hrs.csv -- ANY gap greater than max_cip_hrs is a HARD violation
  * how many orders finished outside AZAP's requested week

Read-only; touches nothing outside the given directories.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

WEEK0_END = 167


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def report(work: Path) -> None:
    print("=" * 68)
    print(work)
    print("=" * 68)

    sched = _rows(work / "schedule_phase2.csv")
    if not sched:
        print("  no schedule_phase2.csv")
        return
    cols = sched[0].keys()

    def pick(*names):
        for n in names:
            if n in cols:
                return n
        return None

    c_line = pick("line_id", "line")
    c_sku = pick("sku", "sku_id")
    c_start = pick("start_hour", "start_h", "start")
    c_end = pick("end_hour", "end_h", "end")
    c_order = pick("order_id")

    # Campaigns: consecutive same-SKU blocks per line, in start order.
    per_line: dict[str, list[dict]] = {}
    for r in sched:
        per_line.setdefault(str(r[c_line]), []).append(r)
    changeovers = 0
    campaigns: list[float] = []
    for line, rows in per_line.items():
        rows.sort(key=lambda r: _num(r[c_start]))
        prev_sku = None
        cur = 0.0
        for r in rows:
            sku = str(r[c_sku])
            dur = _num(r[c_end]) - _num(r[c_start])
            if prev_sku is None:
                cur = dur
            elif sku == prev_sku:
                cur += dur
            else:
                changeovers += 1
                campaigns.append(cur)
                cur = dur
            prev_sku = sku
        if prev_sku is not None:
            campaigns.append(cur)
    avg_camp = sum(campaigns) / len(campaigns) if campaigns else 0.0
    print(f"  production blocks : {len(sched)}")
    print(f"  changeovers       : {changeovers}")
    print(f"  campaigns         : {len(campaigns)}")
    print(f"  avg campaign len  : {avg_camp:.1f} h")

    # AZAP week compliance from the schedule itself.
    if c_order:
        moved = []
        for r in sched:
            oid = str(r[c_order])
            azap_w0 = oid.endswith("-W0")
            azap_w1 = oid.endswith("-W1")
            if not (azap_w0 or azap_w1):
                continue
            s, e = _num(r[c_start]), _num(r[c_end])
            if azap_w0 and e > WEEK0_END + 1:
                moved.append((oid, "ran into week 2", s, e))
            if azap_w1 and s < WEEK0_END + 1:
                moved.append((oid, "pulled into week 1", s, e))
        uniq = sorted({m[0] for m in moved})
        print(f"  orders outside AZAP week: {len(uniq)}")
        for oid, why, s, e in moved[:10]:
            print(f"      {oid}: {why} ({s:.0f}h-{e:.0f}h)")

    # ── CIP max-interval compliance (HARD, food safety) ──────────────
    maxima: dict[str, float] = {}
    for r in _rows(work / "line_cip_hrs.csv"):
        key = str(
            r.get("line_id") or r.get("line") or r.get("Line") or ""
        ).strip()
        val = r.get("max_cip_hrs") or r.get("max_cip_h")
        if key and val:
            maxima[key] = _num(val, 120.0)

    cips = _rows(work / "cip_windows.csv")
    if not cips:
        print("  no cip_windows.csv (no CIPs in this schedule)")
        return
    ccols = cips[0].keys()

    def cpick(*names):
        for n in names:
            if n in ccols:
                return n
        return None

    k_line = cpick("line_id", "line")
    k_start = cpick("start_hour", "start_h", "cip_start_hour", "start")
    k_end = cpick("end_hour", "end_h", "cip_end_hour", "end")

    by_line: dict[str, list[tuple[float, float]]] = {}
    for r in cips:
        by_line.setdefault(str(r[k_line]), []).append(
            (_num(r[k_start]), _num(r[k_end]))
        )

    violations = 0
    print("  CIP max-interval check (gap between consecutive CIPs):")
    for line in sorted(by_line):
        wins = sorted(by_line[line])
        limit = maxima.get(line, 120.0)
        prev_end = 0.0
        gaps = []
        for s, e in wins:
            gap = s - prev_end
            gaps.append(gap)
            if gap > limit:
                violations += 1
                print(
                    f"    !! VIOLATION line {line}: CIP at {s:.0f}h is "
                    f"{gap:.0f}h after previous end (max {limit:.0f}h)"
                )
            prev_end = e
        gtxt = ", ".join(f"{g:.0f}" for g in gaps)
        print(
            f"    line {line} max={limit:.0f}h  n_cip={len(wins)}  "
            f"gaps=[{gtxt}]  worst={max(gaps):.0f}h"
        )
    print(
        f"  RESULT: {violations} max-interval violation(s)"
        if violations
        else "  RESULT: 0 max-interval violations - CIP limit HELD"
    )


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    for a in args:
        report(Path(a))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
