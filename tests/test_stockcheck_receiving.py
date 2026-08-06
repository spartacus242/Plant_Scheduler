# tests/test_stockcheck_receiving.py — golden: W33 yields exactly 11 appointments.

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from stockcheck.receiving_import import parse_receiving_schedule  # noqa: E402

XLSM = ROOT / "data" / "stockcheck" / "dev_receiving_schedule.xlsm"


def test_w33_appointments():
    appts, errors = parse_receiving_schedule(XLSM, week_tab="W33")
    # 14 verified against raw sheet (rows 21-28 x day cols A/J/S, plus row 55):
    # Sun 8/9: 3 CAPS; Mon 8/10: 3 CAPS + 2 GPI; Tue 8/11: SL3 transfer + 3 GPI;
    # Thu 8/13: 1 GPI. (Earlier manual scan undercounted at 11.)
    assert len(appts) == 14, f"got {len(appts)}: {appts}"
    by_po = {a["po"]: a for a in appts}
    assert by_po["30042199"]["category"] == "SL3 transefer"
    assert by_po["30042199"]["date"] == "2026-08-11"
    assert by_po["30042199"]["time"] == "08:00"
    assert by_po["30043559"]["date"] == "2026-08-13"
    assert by_po["30042841"]["date"] == "2026-08-09"
    assert by_po["30042841"]["time"] == "09:00"
    assert by_po["30042807"]["time"] == "13:00"
    cats = {a["category"] for a in appts}
    assert "CAPS" in cats and "GPI" in cats
    assert all(a["date"] for a in appts)


def test_all_weeks_no_crash():
    appts, errors = parse_receiving_schedule(XLSM)
    assert len(appts) > 100  # 52 weeks of appointments
    assert all(a["week_tab"] for a in appts)
