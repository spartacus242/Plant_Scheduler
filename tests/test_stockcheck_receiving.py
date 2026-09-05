# tests/test_stockcheck_receiving.py — golden: W33 yields exactly 11 appointments.

from __future__ import annotations

import re
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


def test_po8_on_appointments():
    # po8 is the join key to open-PO lines: the dock cell already holds the
    # 8-digit short form, so it round-trips; multi-PO cells keep the first.
    appts, _ = parse_receiving_schedule(XLSM, week_tab="W33")
    by_po = {a["po"]: a for a in appts}
    assert by_po["30042199"]["po8"] == "30042199"
    assert all("po8" in a for a in appts)
    assert all(a["po8"] == "" or re.fullmatch(r"\d{8}", a["po8"]) for a in appts)


def test_norm_time_midnight_datetime_is_blank():
    """A date-only Plan APT cell arrives from openpyxl as a midnight datetime.
    '00:00' would join as a real slot (ready 02:00 after the appt offset);
    blank hands the line to the ERP ready-hour rule. Real times survive."""
    from datetime import datetime
    from stockcheck.receiving_import import _norm_time
    assert _norm_time(datetime(2026, 9, 1)) == ""
    assert _norm_time(datetime(2026, 9, 1, 0, 0, 0)) == ""
    assert _norm_time(datetime(2026, 9, 1, 0, 30)) == "00:30"
    assert _norm_time(datetime(2026, 9, 1, 9, 0)) == "09:00"
    assert _norm_time(datetime(2026, 9, 1, 13, 15)) == "13:15"
    assert _norm_time("9AM") == "09:00" and _norm_time("1pm") == "13:00"
    assert _norm_time("11:30") == "11:30" and _norm_time(None) == ""


def test_date_only_appointment_cell_parses_blank_time(tmp_path):
    """Through the sheet parser: a datetime Plan APT cell with no time part
    yields time '' (ERP rule); a timed one keeps its HH:MM."""
    from datetime import datetime
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "W36"
    ws.append([datetime(2026, 9, 1)])
    ws.append(["RECEIVING", "30043600", "GPI", None, "XPO", datetime(2026, 9, 1)])
    ws.append(["RECEIVING", "30043601", "GPI", None, "XPO",
               datetime(2026, 9, 1, 9, 0)])
    p = tmp_path / "dock.xlsx"
    wb.save(p)
    wb.close()
    appts, _ = parse_receiving_schedule(p)
    by_po = {a["po"]: a for a in appts}
    assert by_po["30043600"]["time"] == ""
    assert by_po["30043600"]["date"] == "2026-09-01"
    assert by_po["30043601"]["time"] == "09:00"
