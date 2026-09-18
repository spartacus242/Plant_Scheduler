# tests/test_azap_demand.py — AZAP workbook -> demand_plan_summary.csv (2026-09-15).
#
# The planners drop "New Export AZAP MMDDYY.xlsx" into fs_manual; the app
# needs the small Week/Product/Tons summary that used to be cut from it by
# hand. These tests pin the recipe (helpers/azap_demand.py) against a small
# workbook built here, the csv format byte for byte, the window read from the
# file name, the mtime freshness rule, the CLI's --dry-run, the live-data
# sync's folder-mode hook, the importer's ISO-week -> year rule, and — when
# the real drop is on this box — that the current hand-made csv reproduces.

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from helpers import azap_demand as az
from helpers import demand_summary_import as dsi

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REAL_DROP = Path("C:/Users/jbdil/Flowstate/fs_data/fs_manual")
REAL_WB = REAL_DROP / "New Export AZAP 091126.xlsx"
REAL_CSV = REAL_DROP / "demand_plan_summary.csv"
BOM = b"\xef\xbb\xbf"

# the 33-column header of the real sheet (note the trailing space on 'APCE ')
HEADER = ["Year Start", "Month Start", "Day Start", "Machine", "Product", "Tons",
          "gen_idtypemaille", "Year End", "Month End", "Day End", "Hours", "Start Date",
          "End Date", "Factory", "Blank for Spacing", "Blank for Space", "Blank for Spacing",
          "Week", "Month", "Quarter", "To Export", "Multi", "Bio", "Name", "Format",
          "Blank for Spacing", "Market", "Product Type", "Blank for Spacing", "Multiple",
          "Blank for Spacing", "Cases", "APCE "]
assert len(HEADER) == 33


def _row(start, machine, product, tons, factory="NPA", to_export="Yes"):
    """One sheet row in the real column order; `start` is what the Start Date
    cell holds (datetime, date or string)."""
    sd = start
    d = az._as_date(start)
    r = [None] * 33
    r[0], r[1], r[2] = d.year, d.month, d.day
    r[3], r[4], r[5] = machine, product, tons
    r[6] = 4
    r[10] = 10.0
    r[11] = sd
    r[13] = factory
    r[17] = d.isocalendar()[1]
    r[20] = to_export
    r[23] = "GGS VP APL 2x20x3.2oz"
    return r


ROWS = [
    _row(datetime(2026, 9, 14), "P01", 280480, 400.001),          # W38
    _row(datetime(2026, 9, 14), "P02", 280480, 12.0),             # W38, 2nd machine -> summed
    _row(datetime(2026, 9, 14), "P01", 120448, 50.0),             # W38 -> "50"
    _row(datetime(2026, 9, 14), "P01", 280351, 8.999),            # W38 -> "8.999"
    _row(datetime(2026, 9, 14), "P01", 280105, 100.0, factory="TVC"),   # other factory
    _row(datetime(2026, 9, 14), "P01", 280480, 999.0, factory=None),    # blank factory
    _row(datetime(2026, 9, 14), "P01", "Trial", 5.0),             # non-numeric product
    _row(datetime(2026, 9, 7), "P01", 280480, 30.0),              # W37: before the window
    _row(datetime(2026, 11, 2), "P01", 280480, 30.0),             # W45: after the window
    _row(datetime(2026, 10, 26), "P01", 280351, 20.5, to_export="No"),  # W44, To Export=No: KEPT
    _row(datetime(2026, 9, 21), "P01", 280480, None),             # W39, blank Tons: skipped
    _row("2026-09-21", "P01", "280590", 17.999),                  # W39, string date + str product
    _row(date(2026, 9, 28), "P01", 280480.0, 7.25),               # W40, date + float product
]

EXPECTED_CSV = (BOM + b"Week,Product,Tons\r\n"
                b"38,120448,50\r\n"
                b"38,280351,8.999\r\n"
                b"38,280480,412.001\r\n"
                b"39,280590,17.999\r\n"
                b"40,280480,7.25\r\n"
                b"44,280351,20.5\r\n")


def _workbook(path: Path, rows=ROWS, sheet=az.SHEET_NAME, header=HEADER,
              age_s: float = 600.0) -> Path:
    """Write a small workbook (one filler sheet before the data sheet, like
    the real file) with a controlled mtime (default: settled 10 min ago)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.active.title = "ImportExport Instructions"
    ws = wb.create_sheet(sheet)
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(str(path))
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def _set_mtime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _pull():
    spec = importlib.util.spec_from_file_location("fs_live_pull", SCRIPTS / "fs-live-pull.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# file name, window, discovery
# ---------------------------------------------------------------------------

def test_export_date_from_name_reads_mmddyy():
    """The six digits in the name are the export date as MMDDYY; anything
    else (no digits, an impossible date, another extension) is None."""
    assert az.export_date_from_name("New Export AZAP 091126.xlsx") == date(2026, 9, 11)
    assert az.export_date_from_name(Path("x/New Export AZAP 091126.xlsm")) == date(2026, 9, 11)
    assert az.export_date_from_name("new export azap 010127.XLSX") == date(2027, 1, 1)
    assert az.export_date_from_name("New Export AZAP.xlsx") is None
    assert az.export_date_from_name("New Export AZAP 139926.xlsx") is None
    assert az.export_date_from_name("New Export AZAP 091126.csv") is None
    assert az.export_date_from_name("demand_plan_summary.csv") is None


def test_export_date_survives_copy_and_conflict_suffixes():
    """Rule of 2026-09-16: the six digits right after the prefix are the date
    whatever follows them — an Explorer copy "(1)" or a OneDrive conflict
    copy "-DESKTOP-X" used to fall back to the mtime and shift the window
    silently. A seventh digit means it is not a MMDDYY date."""
    assert az.export_date_from_name("New Export AZAP 091126 (1).xlsx") == date(2026, 9, 11)
    assert az.export_date_from_name("New Export AZAP 091126-DESKTOP-X.xlsx") == date(2026, 9, 11)
    assert az.export_date_from_name("New Export AZAP091126 - Copy.xlsm") == date(2026, 9, 11)
    assert az.export_date_from_name("New Export AZAP 0911267.xlsx") is None
    assert az.export_date_from_name("New Export AZAP - Copy.xlsx") is None
    assert az.export_date_from_name("New Export AZAP 091126 (1).csv") is None


def test_first_monday_strictly_after_the_export_date():
    assert az.first_monday_after(date(2026, 9, 11)) == date(2026, 9, 14)   # Friday
    assert az.first_monday_after(date(2026, 9, 13)) == date(2026, 9, 14)   # Sunday
    assert az.first_monday_after(date(2026, 9, 14)) == date(2026, 9, 21)   # a Monday -> the next one


def test_find_azap_workbook_newest_wins_xlsm_included(tmp_path):
    """Newest export wins between an .xlsx and an .xlsm; Excel's ~$ lock file
    and unrelated names never match; no folder / no file -> None.

    Rule of 2026-09-16: newest = the export date (the name's MMDDYY, else
    the mtime date) first and the mtime only second, so an AutoSave that
    bumps last week's workbook no longer beats this week's export."""
    assert az.find_azap_workbook(tmp_path) is None
    assert az.find_azap_workbook(tmp_path / "missing") is None
    old = _workbook(tmp_path / "New Export AZAP 090426.xlsx", age_s=7200)
    new = _workbook(tmp_path / "New Export AZAP 091126.xlsm", age_s=600)
    (tmp_path / "~$New Export AZAP 091126.xlsm").write_bytes(b"lock")
    (tmp_path / "Shipping Receiving Schedule NPA - 2024.xlsm").write_bytes(b"x")
    assert az.find_azap_workbook(tmp_path) == new
    # last week's workbook AutoSaved a minute ago: this week's export still wins
    _set_mtime(old, time.time() - 60)
    assert az.find_azap_workbook(str(tmp_path)) == new
    # same export date (a "(1)" copy): the later mtime decides
    copy = _workbook(tmp_path / "New Export AZAP 091126 (1).xlsx", age_s=30)
    assert az.find_azap_workbook(tmp_path) == copy
    # a name without a date competes with its mtime date
    undated = _workbook(tmp_path / "New Export AZAP.xlsx")
    _set_mtime(undated, datetime(2026, 9, 10, 12, 0).timestamp())
    assert az.find_azap_workbook(tmp_path) == copy
    _set_mtime(undated, datetime(2026, 9, 12, 12, 0).timestamp())
    assert az.find_azap_workbook(tmp_path) == undated


def _ts(*a) -> float:
    return datetime(*a).timestamp()


# next week's export (091826 -> window W39..W45): 6 rows, as many as ROWS builds
NEXT_ROWS = [
    _row(datetime(2026, 9, 21), "P01", 280480, 111.0),             # W39
    _row(datetime(2026, 9, 21), "P01", 280351, 22.0),              # W39
    _row(datetime(2026, 9, 28), "P01", 280480, 33.0),              # W40
    _row(datetime(2026, 10, 5), "P01", 120448, 44.0),              # W41
    _row(datetime(2026, 10, 12), "P01", 280590, 55.0),             # W42
    _row(datetime(2026, 11, 2), "P01", 280351, 66.0),              # W45
]


def test_a_workbook_saved_later_but_ranked_older_is_never_silent(tmp_path):
    """Rules of 2026-09-16: the export date ranks, but a name date after the
    file's own save date (+1 day) cannot be one, so that file ranks by its
    save date. A workbook the ranking passes over although it was saved
    after the export in use is reported: a PROBLEM when its name date is more
    than four weeks before its save date (a year typo, 091825 for 091826 or
    the January slip 010126 for 010127, which used to lose the ranking
    silently), a note otherwise (an AutoSave on last week's export).
    Untouched older exports, a "(1)" copy and a file still settling are not
    reported."""
    typo = tmp_path / "typo"
    _set_mtime(_workbook(typo / "New Export AZAP 091126.xlsx"), _ts(2026, 9, 11, 19))
    bad = _workbook(typo / "New Export AZAP 091825.xlsx", rows=NEXT_ROWS)
    _set_mtime(bad, _ts(2026, 9, 18, 19))
    assert az.find_azap_workbook(typo).name == "New Export AZAP 091126.xlsx"
    items = az.passed_over_workbooks(typo)
    assert len(items) == 1 and items[0]["problem"] is True and items[0]["workbook"] == bad
    msg = items[0]["message"]
    assert msg.startswith("New Export AZAP 091825.xlsx was saved 2026-09-18 19:00")
    assert "New Export AZAP 091126.xlsx" in msg and "365 days" in msg and "rename it" in msg
    # still being copied in: waits a pass
    assert az.passed_over_workbooks(typo, settle_seconds=60, now=_ts(2026, 9, 18, 19, 0, 5)) == []

    jan = tmp_path / "jan"
    _set_mtime(_workbook(jan / "New Export AZAP 122526.xlsx"), _ts(2026, 12, 25, 19))
    _set_mtime(_workbook(jan / "New Export AZAP 010126.xlsx"), _ts(2027, 1, 1, 19))
    assert az.find_azap_workbook(jan).name == "New Export AZAP 122526.xlsx"
    items = az.passed_over_workbooks(jan, az.find_azap_workbook(jan))
    assert [(i["workbook"].name, i["problem"]) for i in items] == [("New Export AZAP 010126.xlsx", True)]

    autosave = tmp_path / "autosave"
    old = _workbook(autosave / "New Export AZAP 091126.xlsx")
    _set_mtime(old, _ts(2026, 9, 21, 9))
    new = _workbook(autosave / "New Export AZAP 091826.xlsx", rows=NEXT_ROWS)
    _set_mtime(new, _ts(2026, 9, 18, 8))
    assert az.find_azap_workbook(autosave) == new
    items = az.passed_over_workbooks(autosave)
    assert len(items) == 1 and items[0]["problem"] is False and items[0]["workbook"] == old
    assert "built from New Export AZAP 091826.xlsx" in items[0]["message"]

    quiet = tmp_path / "quiet"
    _set_mtime(_workbook(quiet / "New Export AZAP 090426.xlsx"), _ts(2026, 9, 4, 19))
    _set_mtime(_workbook(quiet / "New Export AZAP 091126.xlsx"), _ts(2026, 9, 11, 19))
    _set_mtime(_workbook(quiet / "New Export AZAP 091126 (1).xlsx"), _ts(2026, 9, 11, 18))
    assert az.find_azap_workbook(quiet).name == "New Export AZAP 091126.xlsx"
    assert az.passed_over_workbooks(quiet) == []
    assert az.passed_over_workbooks(tmp_path / "missing") == []

    ahead = tmp_path / "ahead"              # 091827: a name dated after its save date
    _set_mtime(_workbook(ahead / "New Export AZAP 091126.xlsx"), _ts(2026, 9, 11, 19))
    future = _workbook(ahead / "New Export AZAP 091827.xlsx")
    _set_mtime(future, _ts(2026, 9, 18, 19))
    assert az.rank_date(future, future.stat().st_mtime) == date(2026, 9, 18)
    assert az.find_azap_workbook(ahead) == future
    later = _workbook(ahead / "New Export AZAP 092526.xlsx")
    _set_mtime(later, _ts(2026, 9, 25, 19))
    assert az.find_azap_workbook(ahead) == later and az.passed_over_workbooks(ahead) == []

    assert az.name_date_doubt("New Export AZAP 091126.xlsx", _ts(2026, 10, 9, 12)) is None   # 28 days
    assert "29 days before" in az.name_date_doubt("New Export AZAP 091126.xlsx", _ts(2026, 10, 10, 12))
    assert "after the file was saved" in az.name_date_doubt("New Export AZAP 091326.xlsx",
                                                            _ts(2026, 9, 11, 12))
    assert az.name_date_doubt("New Export AZAP - Copy.xlsx", _ts(2026, 9, 11, 12)) is None


# ---------------------------------------------------------------------------
# the recipe and the csv format
# ---------------------------------------------------------------------------

def test_build_reproduces_the_hand_made_recipe(tmp_path):
    """Factory NPA only, no 'To Export' filter, non-numeric products dropped,
    blank Tons skipped, machines summed per (ISO week, product), 7-week window
    from the first Monday after the name's date, Product zero-padded to 6,
    rows in window order then Product."""
    wb = _workbook(tmp_path / "New Export AZAP 091126.xlsx")
    df, meta = az.build_demand_summary(wb)
    assert list(df.columns) == ["Week", "Product", "Tons"]
    assert df.values.tolist() == [
        [38, "120448", 50.0], [38, "280351", 8.999], [38, "280480", 412.001],
        [39, "280590", 17.999], [40, "280480", 7.25], [44, "280351", 20.5]]
    assert str(df["Week"].dtype).startswith("int") and str(df["Tons"].dtype) == "float64"
    assert meta["export_date"] == date(2026, 9, 11) and meta["export_date_source"] == "name"
    assert meta["first_monday"] == date(2026, 9, 14) and meta["last_monday"] == date(2026, 10, 26)
    assert meta["weeks"] == [38, 39, 40, 41, 42, 43, 44]
    assert meta["rows_in"] == len(ROWS) and meta["rows_kept"] == 7 and meta["rows_out"] == 6
    assert meta["dropped_products"] == {"Trial": 1}
    assert meta["blank_tons"] == 1 and meta["bad_dates"] == 0
    assert meta["sheet"] == az.SHEET_NAME and meta["workbook"] == str(wb)


def test_csv_is_utf8_bom_crlf_with_trailing_zeros_stripped(tmp_path):
    """Byte-exact: BOM, CRLF, header Week,Product,Tons, Tons with up to 3
    decimals and trailing zeros stripped (50 not 50.0, 20.5 not 20.500)."""
    wb = _workbook(tmp_path / "New Export AZAP 091126.xlsx")
    df, _ = az.build_demand_summary(wb)
    out = az.write_demand_summary(df, tmp_path / "out" / "demand_plan_summary.csv")
    assert out.read_bytes() == EXPECTED_CSV
    assert not list((tmp_path / "out").glob("*.tmp"))
    assert az.format_tons(39.003) == "39.003" and az.format_tons(50.0) == "50"
    assert az.format_tons(8.999) == "8.999" and az.format_tons(0.0004) == "0"
    assert az.format_tons(412.0009) == "412.001"


def test_product_coercion_and_zero_padding():
    """int / whole float / digit string -> 6-digit code; short codes are
    zero-padded; labels, fractions, blanks and booleans are dropped."""
    assert az._product_code(280480) == "280480"
    assert az._product_code(280480.0) == "280480"
    assert az._product_code(" 280480 ") == "280480"
    assert az._product_code(1234) == "001234"
    assert az._product_code("Trial") is None
    assert az._product_code(280480.5) is None
    assert az._product_code(None) is None and az._product_code("") is None
    assert az._product_code(True) is None


def test_window_from_the_file_name_else_mtime_or_override(tmp_path):
    """No date in the name -> the file's mtime date sets the window; an
    explicit first_monday overrides both; `weeks` sizes the window."""
    wb = _workbook(tmp_path / "New Export AZAP.xlsx")
    _set_mtime(wb, datetime(2026, 10, 1, 9, 0).timestamp())   # a Thursday
    df, meta = az.build_demand_summary(wb)
    assert meta["export_date"] == date(2026, 10, 1) and meta["export_date_source"] == "mtime"
    assert meta["first_monday"] == date(2026, 10, 5) and meta["weeks"][0] == 41
    # W38-40 fell out of the window (W41..W47); W44 stays and W45 comes in
    assert df.values.tolist() == [[44, "280351", 20.5], [45, "280480", 30.0]]
    # explicit window start
    df, meta = az.build_demand_summary(wb, first_monday=date(2026, 9, 7), weeks=2)
    assert meta["export_date_source"] == "given" and meta["weeks"] == [37, 38]
    assert df["Week"].tolist() == [37, 38, 38, 38] and df.iloc[0].tolist() == [37, "280480", 30.0]
    with pytest.raises(ValueError, match="not a Monday"):
        az.build_demand_summary(wb, first_monday=date(2026, 9, 8))
    with pytest.raises(ValueError, match="weeks"):
        az.build_demand_summary(wb, weeks=0)


def test_window_crossing_new_year_keeps_chronological_order(tmp_path):
    """Week numbers alone would sort W1 before W52: the csv keeps window
    order so the importer's year inference sees 52, 53, 1 (2026 is a
    53-week ISO year: W53 = Monday 2026-12-28, W1 of 2027 = 2027-01-04)."""
    rows = [_row(datetime(2026, 12, 21), "P01", 280480, 10.0),   # W52
            _row(datetime(2026, 12, 28), "P01", 280480, 15.0),   # W53
            _row(datetime(2027, 1, 4), "P01", 280480, 20.0),     # W1 of 2027
            _row(datetime(2025, 12, 29), "P01", 280480, 99.0)]   # W1 of 2026: outside
    wb = _workbook(tmp_path / "New Export AZAP 121826.xlsx", rows=rows)
    df, meta = az.build_demand_summary(wb, weeks=3)
    assert meta["weeks"] == [52, 53, 1]
    assert df.values.tolist() == [[52, "280480", 10.0], [53, "280480", 15.0], [1, "280480", 20.0]]


def test_missing_sheet_or_column_raise_a_clear_error(tmp_path):
    wb = _workbook(tmp_path / "New Export AZAP 091126.xlsx", sheet="pdp export AZAP n21")
    with pytest.raises(ValueError, match="pdp export AZAP n20"):
        az.build_demand_summary(wb)
    hdr = [h if h != "Tons" else "Tonnes" for h in HEADER]
    wb = _workbook(tmp_path / "b" / "New Export AZAP 091126.xlsx", header=hdr)
    with pytest.raises(ValueError, match="'Tons'"):
        az.build_demand_summary(wb)
    with pytest.raises(FileNotFoundError):
        az.build_demand_summary(tmp_path / "nope.xlsx")


# ---------------------------------------------------------------------------
# freshness rule
# ---------------------------------------------------------------------------

def test_refresh_rebuilds_only_when_the_csv_is_older_than_the_workbook(tmp_path):
    """csv mtime >= workbook mtime -> "up to date"; older or missing -> built;
    --force always builds; the built csv takes the workbook's mtime; a folder
    without a workbook is ran=False, not an error."""
    assert az.refresh_demand_summary(tmp_path)["ran"] is False
    assert "no AZAP workbook" in az.refresh_demand_summary(tmp_path)["reason"]
    wb = _workbook(tmp_path / "New Export AZAP 091126.xlsx", age_s=3600)
    csv = tmp_path / "demand_plan_summary.csv"

    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is True and res["reason"] == "built" and res["rows"] == 6
    assert res["workbook"] == str(wb) and res["csv"] == str(csv)
    assert res["meta"]["weeks"] == [38, 39, 40, 41, 42, 43, 44]
    assert csv.read_bytes() == EXPECTED_CSV
    assert csv.stat().st_mtime_ns == wb.stat().st_mtime_ns     # carries the export's time

    # up to date: nothing rewritten, hand edits survive
    csv.write_bytes(b"hand edited")
    _set_mtime(csv, wb.stat().st_mtime + 30)
    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is False and res["reason"] == "up to date" and res["rows"] == 0
    assert csv.read_bytes() == b"hand edited"

    # a newer workbook (same name, re-exported) beats the csv
    _set_mtime(wb, csv.stat().st_mtime + 30)
    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is True and csv.read_bytes() == EXPECTED_CSV

    # force
    csv.write_bytes(b"hand edited")
    _set_mtime(csv, wb.stat().st_mtime + 30)
    res = az.refresh_demand_summary(tmp_path, force=True)
    assert res["ran"] is True and res["reason"] == "forced" and csv.read_bytes() == EXPECTED_CSV

    # out_path elsewhere, weeks override
    other = tmp_path / "elsewhere" / "summary.csv"
    res = az.refresh_demand_summary(tmp_path, weeks=1, out_path=other)
    assert res["ran"] is True and res["rows"] == 3 and other.read_bytes().count(b"\r\n") == 4


def test_refresh_is_up_to_date_only_for_the_selected_workbooks_build(tmp_path):
    """Rule of 2026-09-16 (revised): the csv is up to date only when its
    stamp is the SELECTED workbook's (it was built from it) or it is newer
    than EVERY AZAP workbook in the folder (a hand edit). Comparing it with
    the selected workbook alone missed this week's export: an AutoSave on
    last week's workbook rebuilt the csv with a later stamp, the new export
    was copied in keeping its older mtime, and every pass said "up to date"
    with last week's W38 rows in place. A hand edit wins until any AZAP
    workbook in the folder is written after it."""
    csv = tmp_path / "demand_plan_summary.csv"
    old = _workbook(tmp_path / "New Export AZAP 091126.xlsx")
    _set_mtime(old, _ts(2026, 9, 11, 19))
    assert az.refresh_demand_summary(tmp_path)["ran"] is True
    # Monday 09-21 09:00: last week's export is opened and AutoSaved -> rebuilt, same rows
    _set_mtime(old, _ts(2026, 9, 21, 9))
    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is True and csv.stat().st_mtime_ns == old.stat().st_mtime_ns
    # 14:00: this week's export is copied in, keeping its 09-18 08:00:00.4567 mtime
    new = _workbook(tmp_path / "New Export AZAP 091826.xlsx", rows=NEXT_ROWS)
    new_ns = int(_ts(2026, 9, 18, 8)) * 1_000_000_000 + 456_700_000
    os.utime(new, ns=(new_ns, new_ns))
    assert az.find_azap_workbook(tmp_path) == new
    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is True and res["workbook"] == str(new), res
    assert res["meta"]["weeks"] == [39, 40, 41, 42, 43, 44, 45] and res["from_workbook"] is True
    assert csv.read_bytes().splitlines()[1] == b"39,280351,22"
    assert csv.stat().st_mtime_ns == new.stat().st_mtime_ns
    # built from the selected workbook: up to date although last week's is newer on disk
    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is False and res["reason"] == "up to date" and res["from_workbook"] is True
    # a stamp a sync rounded to whole seconds still counts as built from it ...
    for whole_s in (new_ns // 10**9, new_ns // 10**9 + 1):
        os.utime(csv, ns=(whole_s * 10**9, whole_s * 10**9))
        res = az.refresh_demand_summary(tmp_path)
        assert res["ran"] is False and res["from_workbook"] is True
    # ... but a csv 0.2 s off with its own sub-second time (copied in the same
    # batch by a tool that does not keep file times) is not: rebuilt
    os.utime(csv, ns=(new_ns - 200_000_000, new_ns - 200_000_000))
    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is True and csv.stat().st_mtime_ns == new_ns
    assert az._same_stamp(new_ns, new_ns) and not az._same_stamp(new_ns + 1, new_ns)
    # a hand edit after every workbook survives ...
    csv.write_bytes(b"hand edited")
    _set_mtime(csv, _ts(2026, 9, 21, 10))
    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is False and res["from_workbook"] is False
    assert csv.read_bytes() == b"hand edited"
    # ... until any AZAP workbook in the folder is written after it
    _set_mtime(old, _ts(2026, 9, 21, 11))
    res = az.refresh_demand_summary(tmp_path)
    assert res["ran"] is True and res["workbook"] == str(new)
    assert csv.read_bytes().splitlines()[1] == b"39,280351,22"


# ---------------------------------------------------------------------------
# write guards (2026-09-16): a bad build never replaces the shared csv
# ---------------------------------------------------------------------------

WRONG_FACTORY_ROWS = [_row(datetime(2026, 9, 14), "P01", 280480, 400.0, factory="NPA-Nampa"),
                      _row(datetime(2026, 9, 21), "P01", 280351, 20.0, factory="NPA-Nampa")]


def _csv_rows(n: int) -> bytes:
    return BOM + ("Week,Product,Tons\r\n" + "".join(
        f"38,{280000 + i:06d},10\r\n" for i in range(n))).encode("utf-8")


def test_refresh_refuses_a_zero_row_build_and_keeps_the_csv(tmp_path):
    """Rule of 2026-09-16: a build with 0 rows (here the Factory column reads
    "NPA-Nampa") raises ValueError naming the workbook, the window and the
    factory filter, writes nothing (no header-only csv, no .tmp) and does so
    again on the next call — the csv stays older than the workbook — even
    with force=True."""
    good = tmp_path / "demand_plan_summary.csv"
    good.write_bytes(EXPECTED_CSV)
    _set_mtime(good, time.time() - 7200)
    _workbook(tmp_path / "New Export AZAP 091126.xlsx", rows=WRONG_FACTORY_ROWS, age_s=600)
    for _ in range(2):
        with pytest.raises(ValueError) as ei:
            az.refresh_demand_summary(tmp_path)
        msg = str(ei.value)
        assert "New Export AZAP 091126.xlsx" in msg and "0 summary rows" in msg
        assert "W38..W44" in msg and "Factory == 'NPA'" in msg
        assert good.read_bytes() == EXPECTED_CSV and not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError, match="0 summary rows"):
        az.refresh_demand_summary(tmp_path, force=True)
    # no csv yet: still refused, nothing created
    good.unlink()
    with pytest.raises(ValueError, match="0 summary rows"):
        az.refresh_demand_summary(tmp_path)
    assert not good.exists()


def test_refresh_refuses_a_build_under_half_the_existing_rows_unless_forced(tmp_path):
    """Rule of 2026-09-16: a build with fewer than half the data rows of the
    csv it would replace raises ValueError naming both counts and keeps the
    csv; force=True writes it; exactly half is written."""
    csv = tmp_path / "demand_plan_summary.csv"
    csv.write_bytes(_csv_rows(13))
    _set_mtime(csv, time.time() - 7200)
    _workbook(tmp_path / "New Export AZAP 091126.xlsx", age_s=600)   # builds 6 rows
    assert az.existing_csv_rows(csv) == 13
    with pytest.raises(ValueError) as ei:
        az.refresh_demand_summary(tmp_path)
    assert "6 summary rows" in str(ei.value) and "13 rows" in str(ei.value)
    assert "--force" in str(ei.value)
    assert csv.read_bytes() == _csv_rows(13) and not list(tmp_path.glob("*.tmp"))
    res = az.refresh_demand_summary(tmp_path, force=True)
    assert res["ran"] and res["rows"] == 6 and csv.read_bytes() == EXPECTED_CSV
    # 6 of 12 is not below half: written without force
    csv.write_bytes(_csv_rows(12))
    _set_mtime(csv, time.time() - 7200)
    assert az.refresh_demand_summary(tmp_path)["rows"] == 6
    assert csv.read_bytes() == EXPECTED_CSV
    assert az.existing_csv_rows(tmp_path / "missing.csv") is None


def test_write_retries_a_locked_csv_and_never_leaves_the_tmp(tmp_path, monkeypatch):
    """Rule of 2026-09-16: the swap is retried on PermissionError (csv open
    in Excel); when it keeps failing the error is re-raised, the previous csv
    is untouched and demand_plan_summary.csv.tmp is removed (it would sync
    to SharePoint)."""
    wb = _workbook(tmp_path / "New Export AZAP 091126.xlsx")
    df, _ = az.build_demand_summary(wb)
    csv = tmp_path / "out" / "demand_plan_summary.csv"
    csv.parent.mkdir()
    csv.write_bytes(b"previous")
    real_replace = os.replace
    calls = {"n": 0, "fail": 2}

    def locked(src, dst):
        if str(src).endswith(".tmp"):
            calls["n"] += 1
            if calls["n"] <= calls["fail"]:
                raise PermissionError(13, "The process cannot access the file", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(az, "REPLACE_PAUSE_S", 0.0)
    monkeypatch.setattr(az.os, "replace", locked)
    az.write_demand_summary(df, csv)                     # 2 refusals, then it lands
    assert calls["n"] == 3 and csv.read_bytes() == EXPECTED_CSV
    assert not list(csv.parent.glob("*.tmp"))

    csv.write_bytes(b"previous")
    calls.update(n=0, fail=99)
    with pytest.raises(PermissionError):
        az.write_demand_summary(df, csv)
    assert calls["n"] == az.REPLACE_ATTEMPTS
    assert csv.read_bytes() == b"previous" and not list(csv.parent.glob("*.tmp"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(*args, timeout=120):
    return subprocess.run([sys.executable, str(SCRIPTS / "azap_demand_summary.py"), *args],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=timeout)


def test_cli_dry_run_prints_rows_and_writes_nothing(tmp_path):
    _workbook(tmp_path / "New Export AZAP 091126.xlsx")
    r = _cli("--folder", str(tmp_path), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (tmp_path / "demand_plan_summary.csv").exists()
    assert "dry run" in r.stdout and "nothing written" in r.stdout and "W38..W44" in r.stdout
    body = r.stdout.splitlines()
    assert body[1] == "Week,Product,Tons" and body[2] == "38,120448,50" and body[-1] == "44,280351,20.5"
    assert len(body) == 2 + 6
    # the real thing writes, reports, and is idempotent
    r = _cli("--folder", str(tmp_path))
    assert r.returncode == 0 and "6 rows" in r.stdout and "built" in r.stdout, r.stdout + r.stderr
    assert (tmp_path / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV
    r = _cli("--folder", str(tmp_path))
    assert r.returncode == 0 and "up to date" in r.stdout
    r = _cli("--folder", str(tmp_path), "--force", "--weeks", "1", "--out", str(tmp_path / "w1.csv"))
    assert r.returncode == 0 and "3 rows" in r.stdout and (tmp_path / "w1.csv").is_file()


def test_cli_exits_1_on_error(tmp_path):
    r = _cli("--folder", str(tmp_path / "empty"))
    assert r.returncode == 1 and "no AZAP workbook" in r.stderr
    (tmp_path / "New Export AZAP 091126.xlsx").write_bytes(b"not a workbook")
    r = _cli("--folder", str(tmp_path))
    assert r.returncode == 1 and r.stderr.startswith("ERROR:")


def test_cli_write_guards_and_mtime_warning(tmp_path):
    """Rules of 2026-09-16 on the CLI: a 0-row build exits 1 and keeps the
    csv (--force does not override it); a build under half the existing rows
    exits 1 unless --force; --dry-run previews the refusal as a WARNING and
    exits 0; a window taken from the file's mtime prints a WARNING."""
    zero, shrink, undated = tmp_path / "zero", tmp_path / "shrink", tmp_path / "undated"
    _workbook(zero / "New Export AZAP 091126.xlsx", rows=WRONG_FACTORY_ROWS)
    (zero / "demand_plan_summary.csv").write_bytes(EXPECTED_CSV)
    _set_mtime(zero / "demand_plan_summary.csv", time.time() - 7200)
    r = _cli("--folder", str(zero), "--force")
    assert r.returncode == 1 and "0 summary rows" in r.stderr and "W38..W44" in r.stderr
    assert (zero / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV
    r = _cli("--folder", str(zero), "--dry-run")
    assert r.returncode == 0 and "a real run would refuse to write" in r.stderr

    _workbook(shrink / "New Export AZAP 091126.xlsx")
    (shrink / "demand_plan_summary.csv").write_bytes(_csv_rows(20))
    _set_mtime(shrink / "demand_plan_summary.csv", time.time() - 7200)
    r = _cli("--folder", str(shrink))
    assert r.returncode == 1 and "6 summary rows" in r.stderr and "20 rows" in r.stderr
    assert (shrink / "demand_plan_summary.csv").read_bytes() == _csv_rows(20)
    r = _cli("--folder", str(shrink), "--force")
    assert r.returncode == 0 and "6 rows" in r.stdout, r.stdout + r.stderr

    wb = _workbook(undated / "New Export AZAP - Copy.xlsx")
    _set_mtime(wb, datetime(2026, 9, 1, 9, 0).timestamp())
    r = _cli("--folder", str(undated), "--dry-run")
    assert r.returncode == 0 and "WARNING" in r.stderr and "modified date 2026-09-01" in r.stderr
    r = _cli("--folder", str(undated))
    assert r.returncode == 0 and "WARNING" in r.stderr and "W37..W43" in r.stderr
    # (2026-09-16) up to date: the csv's source is named and the warning repeats
    r = _cli("--folder", str(undated))
    assert r.returncode == 0 and "up to date (built from New Export AZAP - Copy.xlsx)" in r.stdout
    assert "WARNING" in r.stderr and "W37..W43" in r.stderr
    # a year typo saved after the export in use is printed, whatever the run does
    _set_mtime(_workbook(undated / "New Export AZAP 091825.xlsx"), _ts(2026, 9, 18, 19))
    r = _cli("--folder", str(undated), "--dry-run")
    assert r.returncode == 0 and "WARNING: New Export AZAP 091825.xlsx was saved" in r.stderr


def _cli_module():
    spec = importlib.util.spec_from_file_location("azap_demand_summary_cli",
                                                  SCRIPTS / "azap_demand_summary.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_default_folder_comes_from_the_live_data_conf(tmp_path, monkeypatch, capsys):
    """Rule of 2026-09-16: without --folder the CLI takes the first folder of
    source_dirs (or source_dir) — tracked conf merged with the BOM-carrying
    local conf — that holds an AZAP workbook; none -> exit 1 with "no AZAP
    workbook in the configured source folders; pass --folder". No user path
    is hard-coded in the script."""
    assert "jbdil" not in (SCRIPTS / "azap_demand_summary.py").read_text(encoding="utf-8")
    cli = _cli_module()
    empty, holder = tmp_path / "fs_vif", tmp_path / "fs_manual"
    empty.mkdir()
    _workbook(holder / "New Export AZAP 091126.xlsx")
    conf, local = tmp_path / "fs-live-data.conf.json", tmp_path / "fs-live-data.local.json"
    conf.write_text('{"source_dirs": [], "files": []}', encoding="utf-8")
    monkeypatch.setattr(cli, "CONF", conf)
    monkeypatch.setattr(cli, "LOCAL_CONF", local)

    assert cli.main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "no AZAP workbook in the configured source folders; pass --folder" in err

    local.write_bytes(BOM + json.dumps({"source_dirs": [str(empty), str(holder)]}).encode("utf-8"))
    assert cli.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"folder: {holder}" in out and "W38..W44" in out and "44,280351,20.5" in out
    assert not (holder / "demand_plan_summary.csv").exists()

    local.unlink()
    conf.write_text(json.dumps({"source_dir": str(holder)}), encoding="utf-8")
    assert cli.main([]) == 0
    assert "6 rows" in capsys.readouterr().out
    assert (holder / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV

    conf.write_text(json.dumps({"source_dirs": [str(empty)]}), encoding="utf-8")
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "pass --folder" in err and str(empty) in err


# ---------------------------------------------------------------------------
# the live-data sync (folder mode) builds the csv into the source folder
# ---------------------------------------------------------------------------

FILES = ["manprg.txt", "cip_info.csv", "demand_plan_summary.csv"]


def _conf(src_dirs, ref_dir, **extra) -> dict:
    conf = {"files": FILES, "data_reference_dir": str(ref_dir),
            "source_dirs": [str(d) for d in src_dirs], "settle_seconds": 60}
    conf.update(extra)
    return conf


def _write(path: Path, text: str, age_s: float = 600.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def test_pull_folder_mode_builds_the_summary_before_copying(tmp_path):
    """One pass: the csv is rebuilt INTO the workbook's folder (the one file
    the sync writes to a source), copied into data/reference and demand_plan
    re-derived — all in the same pass, since the csv carries the workbook's
    settled mtime. A second pass is a no-op; a folder without a workbook is
    never written to."""
    pull = _pull()
    vif, manual, ref = tmp_path / "fs_vif", tmp_path / "fs_manual", tmp_path / "ref"
    _write(vif / "manprg.txt", "MO 1")
    _write(manual / "cip_info.csv", "line,last_cip")
    wb = _workbook(manual / "New Export AZAP 091126.xlsx", age_s=3600)
    vif_before = sorted(p.name for p in vif.iterdir())

    res = pull.pull_once(_conf([vif, manual], ref))

    assert res.ok, res.problems
    assert "demand_plan_summary.csv (built from New Export AZAP 091126.xlsx)" in res.updated
    assert "demand_plan_summary.csv" in res.updated and "demand_plan.csv (derived)" in res.updated
    assert (manual / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV
    assert (ref / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV
    assert (ref / "demand_plan.csv").is_file() and (ref / "manprg.txt").read_text() == "MO 1"
    assert sorted(p.name for p in manual.iterdir()) == [
        "New Export AZAP 091126.xlsx", "cip_info.csv", "demand_plan_summary.csv"]
    assert sorted(p.name for p in vif.iterdir()) == vif_before
    assert (manual / "demand_plan_summary.csv").stat().st_mtime_ns == wb.stat().st_mtime_ns
    # the app's heartbeat reader (helpers/live_sync) drops entries ending in
    # ")" as notes, so the "(built from …)" line never shows up as a file name
    assert [u for u in res.updated if u.endswith(")")] == [
        "demand_plan_summary.csv (built from New Export AZAP 091126.xlsx)",
        "demand_plan.csv (derived)", "manprg.asof.json (stamped)"]

    res2 = pull.pull_once(_conf([vif, manual], ref))
    assert res2.ok and res2.updated == [] and res2.skipped == []


def test_pull_broken_or_settling_workbook_costs_only_the_summary(tmp_path):
    pull = _pull()
    manual, ref = tmp_path / "fs_manual", tmp_path / "ref"
    _write(manual / "cip_info.csv", "line,last_cip")
    bad = _write(manual / "New Export AZAP 091126.xlsx", "not a workbook", age_s=600)
    res = pull.pull_once(_conf([manual], ref))
    assert not res.ok
    assert any(p.startswith("demand_plan_summary.csv from New Export AZAP 091126.xlsx:")
               for p in res.problems)
    assert (ref / "cip_info.csv").is_file()                      # the rest of the pass ran
    assert not (manual / "demand_plan_summary.csv").exists()
    # a workbook modified seconds ago may still be being copied in: wait a pass
    bad.unlink()
    _workbook(manual / "New Export AZAP 091126.xlsx", age_s=0)
    res = pull.pull_once(_conf([manual], ref))
    assert res.ok and not (manual / "demand_plan_summary.csv").exists()
    assert any("New Export AZAP 091126.xlsx" in s and "settling" in s for s in res.skipped)
    res = pull.pull_once(_conf([manual], ref, settle_seconds=0))
    assert res.ok and (manual / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV


def test_pull_refused_build_is_a_problem_on_every_pass(tmp_path):
    """Rule of 2026-09-16: a 0-row build (wrong Factory value) is a problem
    on EVERY pass — not only the first, after which a header-only csv used
    to make pass 2 report ok — and the previous csv survives in the source
    folder and in data/reference."""
    pull = _pull()
    manual, ref = tmp_path / "fs_manual", tmp_path / "ref"
    good = manual / "demand_plan_summary.csv"
    manual.mkdir()
    good.write_bytes(EXPECTED_CSV)
    _set_mtime(good, time.time() - 7200)
    _workbook(manual / "New Export AZAP 091126.xlsx", rows=WRONG_FACTORY_ROWS, age_s=600)
    for _ in range(2):
        res = pull.pull_once(_conf([manual], ref))
        assert not res.ok
        assert any(p.startswith("demand_plan_summary.csv from New Export AZAP 091126.xlsx:")
                   and "0 summary rows" in p for p in res.problems), res.problems
        assert good.read_bytes() == EXPECTED_CSV and not list(manual.glob("*.tmp"))
        assert (ref / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV
    heartbeat = json.loads((ref / "live_sync.json").read_text(encoding="utf-8"))
    assert heartbeat["ok"] is False and any("0 summary rows" in p for p in heartbeat["problems"])


def test_pull_mtime_window_is_a_warning_note_not_a_problem(tmp_path):
    """Rule of 2026-09-16: a workbook whose name has no MMDDYY builds from
    its mtime date; the pass stays ok but the "(built from ...)" note carries
    a WARNING (Home's sync row shows it; the heartbeat readers still treat
    it as a note since it ends in ")"). Every later pass that keeps that
    csv repeats the WARNING on an "(unchanged, from ...)" note — it used to
    drop off the heartbeat one pass (5 min) after the build."""
    pull = _pull()
    manual, ref = tmp_path / "fs_manual", tmp_path / "ref"
    wb = _workbook(manual / "New Export AZAP - Copy.xlsx")
    _set_mtime(wb, datetime(2026, 9, 1, 9, 0).timestamp())
    res = pull.pull_once(_conf([manual], ref))
    assert res.ok, res.problems
    notes = [u for u in res.updated if u.startswith("demand_plan_summary.csv (built from")]
    assert len(notes) == 1 and notes[0].endswith(")")
    assert "WARNING" in notes[0] and "modified date 2026-09-01" in notes[0]
    assert "demand_plan_summary.csv" in res.updated          # the csv itself still lands
    for _ in range(2):
        res = pull.pull_once(_conf([manual], ref))
        assert res.ok and res.problems == [] and res.skipped == []
        assert "demand_plan_summary.csv" not in res.updated   # nothing rebuilt, nothing copied
        assert len(res.updated) == 1
        note = res.updated[0]
        assert note.startswith("demand_plan_summary.csv (unchanged, from New Export AZAP - Copy.xlsx;")
        assert note.endswith(")") and "WARNING" in note and "W37..W43" in note
        heartbeat = json.loads((ref / "live_sync.json").read_text(encoding="utf-8"))
        assert heartbeat["ok"] is True and heartbeat["updated"] == [note]


def test_pull_reports_a_passed_over_workbook_on_every_pass(tmp_path):
    """Rule of 2026-09-16: a workbook saved after the export in use but
    passed over by the ranking is reported on EVERY pass. A year typo in its
    name (091825) is a problem: the demand keeps last week's window, and the
    pass used to write ok=True, updated=[], problems=[]. Renamed right, it
    is built at once. An AutoSave on last week's export afterwards is a
    "(NOTE: ...)" entry that keeps the pass ok and rebuilds nothing."""
    pull = _pull()
    manual, ref = tmp_path / "fs_manual", tmp_path / "ref"
    csv = manual / "demand_plan_summary.csv"
    old = _workbook(manual / "New Export AZAP 091126.xlsx")
    _set_mtime(old, _ts(2026, 9, 11, 19))
    assert pull.pull_once(_conf([manual], ref)).ok
    typo = _workbook(manual / "New Export AZAP 091825.xlsx", rows=NEXT_ROWS)
    _set_mtime(typo, _ts(2026, 9, 18, 19))
    for _ in range(2):
        res = pull.pull_once(_conf([manual], ref))
        assert not res.ok and res.updated == []
        assert len(res.problems) == 1, res.problems
        p = res.problems[0]
        assert p.startswith("demand_plan_summary.csv: New Export AZAP 091825.xlsx was saved")
        assert "New Export AZAP 091126.xlsx" in p and "365 days" in p
        heartbeat = json.loads((ref / "live_sync.json").read_text(encoding="utf-8"))
        assert heartbeat["ok"] is False and heartbeat["problems"] == res.problems
    assert csv.read_bytes() == EXPECTED_CSV

    new = typo.rename(manual / "New Export AZAP 091826.xlsx")
    res = pull.pull_once(_conf([manual], ref))
    assert res.ok, res.problems
    assert "demand_plan_summary.csv (built from New Export AZAP 091826.xlsx)" in res.updated
    assert csv.read_bytes().splitlines()[1] == b"39,280351,22"
    assert (ref / "demand_plan_summary.csv").read_bytes() == csv.read_bytes()

    _set_mtime(old, _ts(2026, 9, 21, 9))                    # last week's export AutoSaved
    for _ in range(2):
        res = pull.pull_once(_conf([manual], ref))
        assert res.ok and res.problems == []
        assert len(res.updated) == 1, res.updated
        note = res.updated[0]
        assert note.startswith("demand_plan_summary.csv (NOTE: New Export AZAP 091126.xlsx")
        assert note.endswith(")") and f"built from {new.name}" in note
    assert csv.read_bytes().splitlines()[1] == b"39,280351,22"


# ---------------------------------------------------------------------------
# the work PC's push script rebuilds too (GitHub mode, 2026-09-16)
# ---------------------------------------------------------------------------

def _push():
    spec = importlib.util.spec_from_file_location("fs_live_push", SCRIPTS / "fs-live-push.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_push_rebuilds_the_summary_in_folders_holding_a_workbook(tmp_path):
    """Rule of 2026-09-16: from a repo checkout the push script rebuilds
    demand_plan_summary.csv in each watched folder holding an AZAP workbook
    (never in the others), reports it, and a broken workbook or a settling
    one costs only the rebuild — it never raises."""
    push = _push()
    vif, manual = tmp_path / "fs_vif", tmp_path / "fs_manual"
    _write(vif / "manprg.txt", "MO 1")
    _workbook(manual / "New Export AZAP 091126.xlsx", age_s=3600)
    assert push.rebuild_azap_summaries({}, [vif]) == []
    lines = push.rebuild_azap_summaries({"settle_seconds": 60}, [vif, tmp_path / "gone", manual])
    assert len(lines) == 1 and "built from New Export AZAP 091126.xlsx" in lines[0]
    assert "6 rows (W38..W44)" in lines[0]
    assert (manual / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV
    assert sorted(p.name for p in vif.iterdir()) == ["manprg.txt"]
    assert "up to date" in push.rebuild_azap_summaries({}, [manual])[0]

    bad = tmp_path / "bad"
    _write(bad / "New Export AZAP 091826.xlsx", "not a workbook")
    lines = push.rebuild_azap_summaries({}, [bad])
    assert len(lines) == 1 and lines[0].startswith("AZAP rebuild failed in")
    fresh = tmp_path / "fresh"
    _workbook(fresh / "New Export AZAP 091826.xlsx", age_s=0)
    lines = push.rebuild_azap_summaries({"settle_seconds": 60}, [fresh])
    assert "settling" in lines[0] and not (fresh / "demand_plan_summary.csv").exists()


def test_push_names_passed_over_workbooks_and_repeats_the_mtime_warning(tmp_path):
    """Rule of 2026-09-16: the push script's rebuild names a year-typo
    workbook saved after the export in use ("AZAP rebuild WARNING:") and an
    AutoSaved older one ("AZAP rebuild note:") on every pass, and repeats the
    no-MMDDYY warning while the csv built from that workbook is in use."""
    push = _push()
    typo = tmp_path / "typo"
    _set_mtime(_workbook(typo / "New Export AZAP 091126.xlsx"), _ts(2026, 9, 11, 19))
    _set_mtime(_workbook(typo / "New Export AZAP 091825.xlsx"), _ts(2026, 9, 18, 19))
    for first in ("built from New Export AZAP 091126.xlsx", "up to date"):
        lines = push.rebuild_azap_summaries({}, [typo])
        assert len(lines) == 2 and first in lines[0]
        assert lines[1].startswith("AZAP rebuild WARNING: New Export AZAP 091825.xlsx was saved")

    autosave = tmp_path / "autosave"
    _set_mtime(_workbook(autosave / "New Export AZAP 091126.xlsx"), _ts(2026, 9, 21, 9))
    _set_mtime(_workbook(autosave / "New Export AZAP 091826.xlsx", rows=NEXT_ROWS),
               _ts(2026, 9, 18, 8))
    lines = push.rebuild_azap_summaries({}, [autosave])
    assert "built from New Export AZAP 091826.xlsx" in lines[0]
    assert lines[1].startswith("AZAP rebuild note: New Export AZAP 091126.xlsx")

    undated = tmp_path / "undated"
    _set_mtime(_workbook(undated / "New Export AZAP - Copy.xlsx"), _ts(2026, 9, 1, 9))
    for first in ("built from", "up to date"):
        lines = push.rebuild_azap_summaries({}, [undated])
        assert len(lines) == 2 and first in lines[0]
        assert lines[1].startswith("AZAP rebuild WARNING:") and "W37..W43" in lines[1]


def test_push_without_a_repo_checkout_skips_the_rebuild_in_one_line(tmp_path, monkeypatch):
    """Rule of 2026-09-16: a hand-copied push script (no code/ next to it)
    prints exactly one "AZAP rebuild skipped: <reason> - run
    scripts/azap_demand_summary.py" line, writes nothing and never raises."""
    push = _push()
    manual = tmp_path / "fs_manual"
    _workbook(manual / "New Export AZAP 091126.xlsx", age_s=3600)
    monkeypatch.setattr(push, "__file__", str(tmp_path / "FlowstateLive" / "fs-live-push.py"))
    lines = push.rebuild_azap_summaries({}, [manual])
    assert len(lines) == 1 and lines[0].startswith("AZAP rebuild skipped:")
    assert lines[0].endswith("- run scripts/azap_demand_summary.py")
    assert not (manual / "demand_plan_summary.csv").exists()


def test_push_once_rebuilds_before_resolving_the_file_list(tmp_path, monkeypatch):
    """Rule of 2026-09-16: push_once runs the rebuild before it resolves the
    files to copy, so the same pass pushes the fresh csv."""
    push = _push()
    manual = tmp_path / "fs_manual"
    _workbook(manual / "New Export AZAP 091126.xlsx", age_s=3600)
    seen: list[bool] = []
    monkeypatch.setattr(push, "ensure_clone", lambda conf: tmp_path / "clone")
    monkeypatch.setattr(push, "sync_with_remote", lambda *a: None)
    monkeypatch.setattr(push, "commits_ahead", lambda *a: 0)
    monkeypatch.setattr(push, "resolve_entries_multi",
                        lambda entries, dirs: seen.append(
                            (Path(dirs[0]) / "demand_plan_summary.csv").is_file()) or [])
    conf = {"remote": "origin", "branch": "main", "files": FILES,
            "source_dirs_work": [str(manual)], "settle_seconds": 60}
    assert push.push_once(conf) == []
    assert seen == [True]


def test_push_once_expands_the_fs_data_root_to_its_subfolders(tmp_path, monkeypatch):
    """Rule of 2026-09-17: source_dirs_work naming the drop ROOT hands
    [root/fs_vif, root/fs_manual] on to the rebuild and the file list; the
    summary is rebuilt in fs_manual, never in the root (whose own workbook
    is ignored)."""
    push = _push()
    root = tmp_path / "fs_data"
    _write(root / "fs_vif" / "manprg.txt", "MO 1")
    _workbook(root / "fs_manual" / "New Export AZAP 091126.xlsx", age_s=3600)
    _workbook(root / "New Export AZAP 091826.xlsx", age_s=3600)      # in the root: ignored
    handed: list[list[Path]] = []
    monkeypatch.setattr(push, "ensure_clone", lambda conf: tmp_path / "clone")
    monkeypatch.setattr(push, "sync_with_remote", lambda *a: None)
    monkeypatch.setattr(push, "commits_ahead", lambda *a: 0)
    monkeypatch.setattr(push, "resolve_entries_multi",
                        lambda entries, dirs: handed.append([Path(d) for d in dirs]) or [])
    conf = {"remote": "origin", "branch": "main", "files": FILES,
            "source_dirs_work": [str(root)], "settle_seconds": 60}
    assert push.push_once(conf) == []
    assert handed == [[root / "fs_vif", root / "fs_manual"]]
    assert (root / "fs_manual" / "demand_plan_summary.csv").read_bytes() == EXPECTED_CSV
    assert not (root / "demand_plan_summary.csv").exists()
    # the conf reader itself stays pure: the root as written
    assert push.source_dirs_work(conf) == [root]


def test_cli_default_folder_expands_the_fs_data_root(tmp_path, monkeypatch, capsys):
    """Rule of 2026-09-17: a conf whose source_dirs name the drop ROOT reads
    as its fs_vif + fs_manual, so the CLI finds the workbook in fs_manual."""
    cli = _cli_module()
    root = tmp_path / "fs_data"
    (root / "fs_vif").mkdir(parents=True)
    _workbook(root / "fs_manual" / "New Export AZAP 091126.xlsx")
    conf, local = tmp_path / "fs-live-data.conf.json", tmp_path / "fs-live-data.local.json"
    conf.write_text(json.dumps({"source_dirs": [str(root)], "files": []}), encoding="utf-8")
    monkeypatch.setattr(cli, "CONF", conf)
    monkeypatch.setattr(cli, "LOCAL_CONF", local)
    assert cli.main(["--dry-run"]) == 0
    assert f"folder: {root / 'fs_manual'}" in capsys.readouterr().out
    # a root with no workbook anywhere: the subfolders are what was checked
    (root / "fs_manual" / "New Export AZAP 091126.xlsx").unlink()
    assert cli.main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert str(root / "fs_vif") in err and str(root / "fs_manual") in err


# ---------------------------------------------------------------------------
# the importer's ISO week -> year rule (self-anchoring)
# ---------------------------------------------------------------------------

def _summary(path: Path, weeks, sku="280480", tons=10.0) -> Path:
    lines = ["Week,Product,Tons"] + [f"{w},{sku},{tons}" for w in weeks]
    path.write_bytes(BOM + ("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return path


def test_importer_maps_a_new_year_wrap_forward(tmp_path, monkeypatch):
    """Self-anchoring with weeks 51,52,53,1,2 seen in mid-December 2026 (a
    53-week ISO year): week 1 is the coming January (week_index 3), not the
    January that already passed (the old rule gave it 2026 -> 2025-12-29)."""
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 12, 14))
    dem, meta = dsi.import_summary(_summary(tmp_path / "s.csv", [51, 52, 53, 1, 2]))
    assert dem["week_index"].tolist() == [0, 1, 2, 3, 4]
    assert meta.anchor == datetime(2026, 12, 14) and meta.anchor_iso_week == 51
    assert dem["order_id"].tolist() == ["280480-W0", "280480-W1", "280480-W2",
                                        "280480-W3", "280480-W4"]
    # early January: week 1 is THIS ISO year (its Monday, 2025-12-29, is within the look-back)
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 1, 5))
    dem, meta = dsi.import_summary(_summary(tmp_path / "j.csv", [1, 2]))
    assert meta.anchor == datetime(2025, 12, 29) and dem["week_index"].tolist() == [0, 1]
    # a plain mid-year window stays in the current year
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 9, 15))
    dem, meta = dsi.import_summary(_summary(tmp_path / "m.csv", [38, 39, 44]))
    assert meta.anchor == datetime(2026, 9, 14) and dem["week_index"].tolist() == [0, 1, 6]
    # a first week more than 12 weeks in the past is read as next year's, and
    # (2026-09-16) the rest of the file follows it: W23 is 3 weeks after W20
    dem, meta = dsi.import_summary(_summary(tmp_path / "p.csv", [20]))
    assert meta.anchor == datetime(2027, 5, 17) and dem["week_index"].tolist() == [0]
    dem, meta = dsi.import_summary(_summary(tmp_path / "q.csv", [20, 23]))
    assert meta.anchor == datetime(2027, 5, 17) and dem["week_index"].tolist() == [0, 3]
    # ... but a file holding the CURRENT week (W38) starts there: W20 is next May
    dem, meta = dsi.import_summary(_summary(tmp_path / "c.csv", [20, 38]))
    assert meta.anchor == datetime(2026, 9, 14) and dem["week_index"].tolist() == [35, 0]


def test_importer_maps_forward_and_never_pushes_the_current_week_a_year_out(tmp_path, monkeypatch):
    """Rules of 2026-09-16 (revised): every week maps FORWARD from the run
    start, so a horizon of 28+ weeks (azap_demand_summary.py --weeks 36)
    keeps its tail in the future — the "nearest Monday" rule put W12..W20
    in March..May 2026, the anchor 6 months back, with no warning — and the
    weeks more than 26 weeks out are listed in ONE warning. A file holding
    the current ISO week starts at the best-ranked week that lands it on
    this week's Monday: [20, 38] and [38, 39, 20] read in W38 of 2026 keep
    W38 now and W20 in May 2027 (the gap rule alone started them at W20 and
    pushed W38 to 2027-09-20)."""
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 9, 16))
    weeks = list(range(38, 54)) + list(range(1, 21))        # W38 2026 .. W20 2027: 36 weeks
    dem, meta = dsi.import_summary(_summary(tmp_path / "long.csv", weeks))
    assert meta.anchor == datetime(2026, 9, 14) and dem["week_index"].tolist() == list(range(36))
    assert len(meta.warnings) == 1 and meta.warnings[0].startswith("9 ISO week(s) more than 26")
    assert "W12 -> 2027-03-22 (+27)" in meta.warnings[0] and "W20 -> 2027-05-17 (+35)" in meta.warnings[0]
    # up to 26 weeks after the first one: no warning
    dem, meta = dsi.import_summary(_summary(tmp_path / "w27.csv", weeks[:27]))    # W38..W11
    assert dem["week_index"].tolist() == list(range(27)) and meta.warnings == []

    dem, meta = dsi.import_summary(_summary(tmp_path / "r.csv", [38, 39, 20]))
    assert meta.anchor == datetime(2026, 9, 14) and dem["week_index"].tolist() == [0, 1, 35]
    assert meta.warnings == ["1 ISO week(s) more than 26 weeks after the file's first week W38 "
                             "(2026-09-14), mapped forward: W20 -> 2027-05-17 (+35)"]
    # last week ahead of the current one still leads the run
    dem, meta = dsi.import_summary(_summary(tmp_path / "s.csv", [37, 38, 20]))
    assert meta.anchor == datetime(2026, 9, 7) and dem["week_index"].tolist() == [0, 1, 36]

    # the 36-week horizon read 13 weeks late (2026-12-14 = W51, in the file): the
    # run starts at W39 so W51 is this week; W38, now behind the look-back, is
    # pushed a year on and named in the warning
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 12, 14))
    dem, meta = dsi.import_summary(_summary(tmp_path / "late.csv", weeks))
    idx = dict(zip(weeks, dem["week_index"].tolist()))
    assert meta.anchor == datetime(2026, 9, 21)
    assert (idx[39], idx[51], idx[1], idx[20], idx[38]) == (0, 12, 15, 34, 52)
    assert "W38 -> 2027-09-20 (+52)" in meta.warnings[0]


def test_importer_resolves_the_year_once_for_the_whole_file(tmp_path, monkeypatch):
    """Rule of 2026-09-16: the look-back picks the year of the week that
    starts the file's run only; every other week lands on the first Monday
    of its number on or after that one. A contiguous W38..W44 re-imported on
    2026-12-14 (W38 now more
    than 12 weeks back) stays contiguous — the per-week rule gave
    [52, 0, 1, 2, 3, 4, 5]."""
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 12, 14))
    weeks = [38, 39, 40, 41, 42, 43, 44]
    dem, meta = dsi.import_summary(_summary(tmp_path / "late.csv", weeks))
    assert dem["week_index"].tolist() == [0, 1, 2, 3, 4, 5, 6]
    assert meta.anchor == datetime(2027, 9, 20) and meta.anchor_iso_week == 38
    # a week earlier the look-back still reaches W38 of 2026: same shape, this year
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 12, 7))
    dem, meta = dsi.import_summary(_summary(tmp_path / "ontime.csv", weeks))
    assert dem["week_index"].tolist() == [0, 1, 2, 3, 4, 5, 6]
    assert meta.anchor == datetime(2026, 9, 14)
    # the New-Year wrap written sorted by week number still starts at W51
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 12, 14))
    dem, meta = dsi.import_summary(_summary(tmp_path / "sorted.csv", [1, 2, 51, 52, 53]))
    assert meta.anchor == datetime(2026, 12, 14)
    assert dem["week_index"].tolist() == [3, 4, 0, 1, 2]
    # a row out of order sits before the anchor week instead of a year later
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 9, 15))
    dem, meta = dsi.import_summary(_summary(tmp_path / "ooo.csv", [38, 39, 40, 41, 37]))
    assert meta.anchor == datetime(2026, 9, 7) and dem["week_index"].tolist() == [1, 2, 3, 4, 0]
    assert dsi._run_start_week([38, 39, 40, 41, 37]) == 37
    assert dsi._run_start_week([51, 52, 53, 1, 2]) == 51 and dsi._run_start_week([]) is None


def test_importer_explicit_anchor_keeps_the_legacy_year_mapping(tmp_path, monkeypatch):
    """An explicit anchor still maps weeks into the anchor's year first."""
    monkeypatch.setattr(dsi, "_today", lambda: date(2026, 12, 14))
    dem, meta = dsi.import_summary(_summary(tmp_path / "a.csv", [51, 52]),
                                   anchor=datetime(2026, 12, 14))
    assert dem["week_index"].tolist() == [0, 1] and meta.anchor == datetime(2026, 12, 14)
    dem, meta = dsi.import_summary(_summary(tmp_path / "b.csv", [38, 39]),
                                   anchor=datetime(2026, 9, 7))
    assert dem["week_index"].tolist() == [1, 2]


# ---------------------------------------------------------------------------
# the real drop (outside data/reference: the live-read guard does not apply)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not (REAL_WB.exists() and REAL_CSV.exists()),
                    reason="real AZAP drop not on this box")
def test_real_drop_reproduces_the_hand_made_csv():
    """Every (Week, Product, Tons) of the current hand-made csv (238 rows,
    drop of 2026-09-15) comes out of the recipe, to 0.0005 t."""
    df, meta = az.build_demand_summary(REAL_WB)
    assert meta["export_date"] == date(2026, 9, 11) and meta["weeks"] == [38, 39, 40, 41, 42, 43, 44]
    assert meta["dropped_products"] == {"Trial": 1}
    raw = REAL_CSV.read_bytes()
    assert raw.startswith(BOM) and b"\r\n" in raw
    lines = [l for l in raw.decode("utf-8-sig").split("\r\n") if l]
    assert lines[0] == "Week,Product,Tons" and len(lines) == 239
    theirs = {(int(w), p): float(t) for w, p, t in (l.split(",") for l in lines[1:])}
    mine = {(int(r.Week), r.Product): r.Tons for r in df.itertuples()}
    assert set(mine) == set(theirs) and len(mine) == 238
    off = {k: (mine[k], theirs[k]) for k in theirs if abs(mine[k] - theirs[k]) > 0.0005}
    assert off == {}
    # and the text form is the same too (formatting pinned, order aside)
    assert sorted(az.summary_csv_text(df).split("\r\n")) == sorted(lines + [""])
