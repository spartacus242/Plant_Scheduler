# tests/test_stockcheck_report_supply.py — stock_check_report §4 additive
# keys (anchor / supply_meta / sku_needs / inbound / quality + per-block
# supply) on a SYNTHETIC board over the bundled dev_vif exports.
#
# The dev VIF files are copied to tmp with a pinned mtime (git keeps no
# mtimes, and the snapshot hour comes from the export's mtime); the toml is
# monkeypatched so the anchor and the [stock] rules never drift with the
# repo's own config. Numbers: SKU 120430 needs 12 sleeves (754800) per case
# and dev stock holds 3,980 of them, so a 24,000 kg run (5,555.6 cases →
# 66,667 EA) is short unless a PO lands.

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from stockcheck.api import (  # noqa: E402
    receiving_schedule_path, stock_check_report, supply_input_paths)
from stockcheck.vif_import import VIF_FILES  # noqa: E402

DEV_VIF = ROOT / "data" / "stockcheck" / "dev_vif"
ANCHOR = "2026-09-01 00:00:00"
SNAP = datetime(2026, 8, 31, 6, 0, 0)          # stock export stamp → h -18
TODAY = date(2026, 9, 1)
CFG = {"scheduler": {"planning_start_date": ANCHOR}}
L = 96.0                                        # 4 d buffer (default rules)

# Column order copied from the IT extract (tests/test_po_import.py); the
# order unit is the 'U' right after 'Qty ordered at the origin'.
HEADERS = [
    "Order date", "Receipt number", "Supplier", "Supplier designation",
    "Order number", "Received item designation", "Supplier ord/rec reference",
    "Initial Receipt Date", "Receipt date", "Ordered item",
    "Qty ordered at the origin", "U", "Received item", "Qty invoiced", "UC",
    "Actual price", "U", "Actual price 3", "U", "Actual amount", "U",
    "Actual amount 2", "U", "Arrival area", "Batch", "Contract/Offer",
    "Supplier item reference",
]

BOARD = (
    "block_id,block_type,line_id,line_name,start_h,end_h,label,order_id,sku,"
    "sku_description,qty_kg,locked,attrs\n"
    # A: 24 h @ 1000 kg/h of 120430 → 66,667 sleeves vs 3,980 on hand
    "cs_a,production,0,P09,24,48,120430,1,120430,APL GGS,,False,\n"
    # B: 4 h of 280351 — every recipe item comfortably on hand
    "cs_b,production,0,P09,48,52,280351,2,280351,OFV,,False,current_state:queued\n"
    # C: locked 120430 run starting 60 h in (12 h @ 500 kg/h → 16,667 EA)
    "cs_c,production,1,P10,60,72,120430,3,120430,APL GGS,,True,\n"
    # D: 120430 far out (start 200 h) → the PO is reliable by then
    "cs_d,production,1,P10,200,204,120430,4,120430,APL GGS,,False,\n"
    # CIP rows never reach the schedule view
    "cip_1,cip,0,P09,52,56,CIP,,,,,False,current_state:cip_projected\n"
)
DEMAND = (
    "order_id,sku,week_index,qty_target,lower_pct,upper_pct,due_start_hour,"
    "due_end_hour,priority\n"
    "280351-W0,280351,0,2160,0.9,1.1,0,167,3\n"
    "280480-W1,280480,1,2520,0.9,1.1,168,335,3\n"
)
RATES = ("line_id,sku,line_name,capable,calc_rate_kgph\n"
         "0,120430,P09,1,1000.0\n0,280351,P09,1,1000.0\n"
         "1,120430,P10,1,500.0\n")


@pytest.fixture(scope="module")
def vif(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("vif")
    t = time.mktime(SNAP.timetuple())
    for name in VIF_FILES:
        shutil.copy(DEV_VIF / name, d / name)
        os.utime(d / name, (t, t))
    return d


@pytest.fixture()
def dd(tmp_path) -> Path:
    d = tmp_path / "data"
    (d / "reference").mkdir(parents=True)
    (d / "stockcheck").mkdir()
    (d / "calendar_blocks.csv").write_text(BOARD, encoding="utf-8")
    (d / "reference" / "demand_plan.csv").write_text(DEMAND, encoding="utf-8")
    (d / "reference" / "capabilities_rates.csv").write_text(RATES, encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def pinned_cfg(monkeypatch):
    import helpers.config as hc
    monkeypatch.setattr(hc, "load_toml", lambda path=None: dict(CFG))


def _row(po, item, qty, unit, receipt, initial, *, designation="SLV 4x90",
         area="RP1", receipt_no=" " * 20):
    return [datetime(2026, 7, 24), receipt_no, 48, "GRAPHIC PACKAGING  ", po,
            designation + "   ", "   ", initial, receipt, item, qty, unit,
            "   ", 0, unit, 1.5, "USD/" + unit.strip(), 1.5, "USD/" + unit.strip(),
            100, "USD", 100, "USD", area, "   ", "M2 02 1AMAR 30002236", None]


PO_ROWS = [
    # the truck the 120430 runs wait for: 9/1 16:00 = h16
    _row("2 02 1ACDE 30043600", 754800, 90000, "EA ", datetime(2026, 9, 1),
         datetime(2026, 8, 30)),
    # tooling code nobody consumes
    _row("2 02 1ACDE 30043601", 899999, 1, "EA ", datetime(2026, 9, 5),
         datetime(2026, 9, 5), designation="GPI TOOLING"),
    # already received (Receipt number filled)
    _row("2 02 1ACDE 30043602", 756150, 5000, "EA ", datetime(2026, 8, 30),
         datetime(2026, 8, 30), receipt_no="R000123", designation="WRAP"),
    # dated before the stock export → overdue, never counted
    _row("2 02 1ACDE 30043603", 752700, 5000, "M2 ", datetime(2026, 8, 28),
         datetime(2026, 8, 28), designation="FILM"),
]


def _write_pos(path: Path, rows=PO_ROWS) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "NPA Open POs"
    ws.append(HEADERS)
    for r in rows:
        ws.append(r)
    ws.append(["?"] + [None] * (len(HEADERS) - 1))
    wb.save(path)
    wb.close()
    return path


def _write_dock_sheet(path: Path) -> Path:
    """One W-tab in the receiving layout: a date header at col A, then a
    RECEIVING row (Dest, PO, category, _, carrier, Plan APT)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "W36"
    ws.append([datetime(2026, 9, 1)])
    ws.append(["RECEIVING", "30043600", "GPI", None, "XPO", "9AM"])
    wb.save(path)
    wb.close()
    return path


def _report(dd, vif, **kw):
    kw.setdefault("today", TODAY)
    kw.setdefault("receiving_path", False)
    return stock_check_report(dd, vif, **kw)


def _by_block(rep) -> dict:
    return {r["block_id"]: r for r in rep["schedule_view"]}


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------

def test_new_keys_and_json_safe(dd, vif, tmp_path):
    rep = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    assert not rep.get("error"), rep
    assert rep["anchor"] == ANCHOR
    meta = rep["supply_meta"]
    assert set(meta) == {"rules", "snapshot_h", "snapshot_stamp",
                         "receipts_window_end_h", "feed_state", "opening",
                         "tracked", "in_house", "units", "designations"}
    assert meta["rules"]["min_days_after_delivery"] == 4
    assert meta["snapshot_stamp"] == {"rm": "2026-08-31 06:00:00",
                                      "pkg": "2026-08-31 06:00:00"}
    assert meta["snapshot_h"]["754800"] == -18.0      # pkg frame
    assert meta["snapshot_h"]["730022"] == -18.0      # rm frame
    assert meta["in_house"] == ["BT001", "BT002"]
    assert meta["opening"]["754800"] == pytest.approx(3980.0)
    assert meta["units"]["754800"] == "EA" and meta["units"]["730022"] == "Kg"
    assert meta["designations"]["754800"].startswith("SLV 4x90")
    # tracked = recipe items with stock rows; pallets etc. are not
    assert "754800" in meta["tracked"] and "758006" not in meta["tracked"]
    assert set(meta["opening"]) == set(meta["tracked"]) == set(meta["snapshot_h"])
    # sku_needs: board ∪ demand SKUs, unit recipe with alternates
    assert set(rep["sku_needs"]) == {"120430", "280351", "280480"}
    n = rep["sku_needs"]["120430"]
    assert n["kg_per_case"] == pytest.approx(4.32)
    slv = next(e for e in n["items"] if e["item"] == "754800")
    assert slv == {"item": "754800", "per_case": 12.0, "unit": "EA", "alts": []}
    puree = next(e for e in rep["sku_needs"]["280351"]["items"]
                 if e["item"] == "730009")
    assert "730021" in puree["alts"]
    assert set(rep["quality"]) == {"unjoinable_items", "unit_mismatch",
                                   "landed_unverifiable"}
    for row in rep["schedule_view"]:
        # fix K-8 (audit stock-14): the key is the Gantt's blockKey
        # `id|start` at full precision, no longer `id@start:.2f`
        assert row["key"] == f"{row['block_id']}|{row['start_h']:g}"
        assert row["supply"]["key"] == row["key"]
    assert [r["block_id"] for r in rep["schedule_view"]] == ["cs_a", "cs_b", "cs_c", "cs_d"]
    json.dumps(rep, allow_nan=False)          # strict: no NaN/inf anywhere


# --------------------------------------------------------------------------
# verdicts with a fresh feed
# --------------------------------------------------------------------------

def test_po_line_turns_blocks_dependent_and_backed(dd, vif, tmp_path):
    po = _write_pos(tmp_path / "open_pos.xlsx")
    rep = _report(dd, vif, po_path=po)
    inb = rep["inbound"]
    assert inb["state"] == "ok" and rep["supply_meta"]["feed_state"] == "ok"
    assert inb["source_path"] == str(po) and inb["n_rows"] == 4
    assert inb["max_receipt_date"] == "2026-09-05" and inb["errors"] == []
    assert inb["join"] == {"used": 1, "received": 1, "overdue": 1, "landed": 0,
                           "unjoinable": 1, "unit_mismatch": 0,
                           "offsite_no_transfer": 0}
    assert inb["appt_join"] == {"matched": 0, "total": 1}
    assert [ln["fate"] for ln in inb["lines"]] == ["used", "unjoinable",
                                                   "received", "overdue"]
    assert list(inb["receipts"]) == ["754800"]
    r = inb["receipts"]["754800"][0]
    assert r["ready_h"] == 16.0 and r["qty"] == 90000.0 and r["tier"] == "erp"
    assert r["po8"] == "30043600"
    # window: the file's last date (9/5 23:59) outlasts the counted truck
    assert rep["supply_meta"]["receipts_window_end_h"] == pytest.approx(4 * 24 + 23 + 59 / 60)
    assert rep["quality"]["unjoinable_items"] == ["899999"]

    by = _by_block(rep)
    a, b, c, d = (by[k]["supply"] for k in ("cs_a", "cs_b", "cs_c", "cs_d"))
    # A: on hand 3,980 of 66,667; the truck lands 8 h before start (< 4 d)
    assert a["verdict"] == "DEPENDENT" and not a["backed"] and not a["minor"]
    assert a["item"] == "754800" and a["action"] == "move"
    assert a["binding"]["po8"] == "30043600" and a["binding"]["ready_h"] == 16.0
    assert a["lead_h"] == pytest.approx(8.0)
    assert a["safe_from_h"] == pytest.approx(16.0 + L)
    assert a["covered_frac"] == pytest.approx(3980 / 66666.67, abs=1e-3)
    assert a["text"].startswith("🚚 754800: on hand covers 6%")
    assert "PO 30043600 lands Tue 9/1 16:00" in a["text"]     # real stamps
    assert "safe from Sat 9/5 16:00" in a["text"]
    assert "758006" in a["untracked"]                          # pallets: listed only
    # B: everything on hand
    assert b["verdict"] == "OK" and not b["backed"]
    assert b["text"].startswith("✅ ") and b["text"].endswith(": on hand covers the run")
    # C: same truck, 44 h lead, locked → chase the PO instead of moving
    assert c["verdict"] == "DEPENDENT" and c["action"] == "chase_po"
    assert c["binding"]["po8"] == "30043600"
    assert c["co_consumers"] == {}          # A drew 24-48, nothing overlaps 60-72
    # D: 200 h out, the truck is 184 h ahead → reliable
    assert d["verdict"] == "OK" and d["backed"] and d["action"] == "none"
    assert d["text"].endswith("· backed")
    # flat statuses are the untouched WW33 engine
    assert by["cs_a"]["status"] == "AT_RISK" and by["cs_b"]["status"] == "OK"


def test_appointment_joins_on_po8(dd, vif, tmp_path):
    po = _write_pos(tmp_path / "open_pos.xlsx")
    dock = _write_dock_sheet(tmp_path / "dock.xlsx")
    rep = _report(dd, vif, po_path=po, receiving_path=dock)
    inb = rep["inbound"]
    assert inb["receiving_path"] == str(dock) and inb["errors"] == []
    assert inb["appt_join"] == {"matched": 1, "total": 1}
    r = inb["receipts"]["754800"][0]
    assert r["tier"] == "appt" and r["ready_h"] == 11.0    # 9 AM + 2 h
    assert inb["lines"][0]["tier"] == "appt"
    a = _by_block(rep)["cs_a"]["supply"]
    assert a["verdict"] == "DEPENDENT" and a["lead_h"] == pytest.approx(13.0)
    # an unreadable sheet is reported, never fatal
    bad = tmp_path / "bad.xlsm"
    bad.write_bytes(b"not a workbook")
    rep2 = _report(dd, vif, po_path=po, receiving_path=bad)
    assert any("bad.xlsm" in e for e in rep2["inbound"]["errors"])
    assert rep2["inbound"]["appt_join"] == {"matched": 0, "total": 1}


def test_locked_and_running_flags(dd, vif, tmp_path):
    board = BOARD.replace("cs_a,production,0,P09,24,48,120430,1,120430,APL GGS,,False,",
                          "cs_a,production,0,P09,24,48,120430,1,120430,APL GGS,,False,"
                          "current_state:running;pct=45.1")
    (dd / "calendar_blocks.csv").write_text(board, encoding="utf-8")
    rep = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    a = _by_block(rep)["cs_a"]["supply"]
    # running (ERP) ⇒ locked ⇒ chase_po; the draw is unchanged because the
    # block starts after the stock export (the count at h-18 still holds
    # every sleeve the run needs; the 45.1 % made since is on cases_left,
    # which the engine only reads for a block that spans the count hour)
    assert a["verdict"] == "DEPENDENT" and a["action"] == "chase_po"
    assert a["covered_frac"] == pytest.approx(3980 / 66666.67, abs=1e-3)


def test_running_row_spanning_the_count_draws_only_its_remainder(dd, vif, tmp_path):
    """Fix K-2 (audit stock-3): the report path hard-coded cases_left=None,
    so a running MO drew the uniform TIME tail of its board quantity.

    cs_a runs -30..48 h (78 h @ 1000 kg/h = 78,000 kg; 4.32 kg/case ->
    18,055.6 cases -> x12 = 216,667 sleeves), ERP says 45.1 % made. Stock
    count S = -18 h.
      * old (uniform tail): 216,667 x (48 - -18) / 78 = 183,333 EA from S
      * new (remainder):    216,667 x (1 - 0.451)     = 118,950 EA from S
    On hand 3,980 -> covered = 3,980 / 118,950 = 0.0335 (old 0.0217). The
    9/1 16:00 truck (90,000) lands mid-run: 3,980 + 90,000 < 118,950, the
    planned curve still crosses -> SHORT either way; the draw is what moves.
    A row written after fix C04 (qty_kg already the remainder, `fct_kg=`
    token) is NOT scaled again: same block with qty_kg 42,120 (= 78,000 x
    0.54) and fct_kg -> 42,120 / 4.32 x 12 = 117,000 EA drawn from S."""
    row = "cs_a,production,0,P09,-30,48,120430,1,120430,APL GGS,,False,"
    legacy = row + "current_state:running;pct=45.1"
    (dd / "calendar_blocks.csv").write_text(BOARD.replace(
        "cs_a,production,0,P09,24,48,120430,1,120430,APL GGS,,False,", legacy),
        encoding="utf-8")
    rep = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    a = _by_block(rep)["cs_a"]["supply"]
    need = next(e for e in a["items"] if e["item"] == "754800")["need"]
    assert need == pytest.approx(216666.67 * (1 - 0.451), rel=1e-4)
    assert a["covered_frac"] == pytest.approx(3980 / need, abs=1e-4)
    assert a["verdict"] == "SHORT" and a["action"] == "chase_po"
    assert a["mid_run"] is True
    c04 = row.replace(",,False,", ",42120,False,") + \
        "current_state:running;pct=45.1;fct_kg=78000;made_kg=35880"
    (dd / "calendar_blocks.csv").write_text(BOARD.replace(
        "cs_a,production,0,P09,24,48,120430,1,120430,APL GGS,,False,", c04),
        encoding="utf-8")
    rep = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    row_a = _by_block(rep)["cs_a"]
    assert row_a["qty_source"] == "board"
    slv = next(e for e in row_a["supply"]["items"] if e["item"] == "754800")
    assert slv["need"] == pytest.approx(117000.0, rel=1e-6)


# --------------------------------------------------------------------------
# feed states
# --------------------------------------------------------------------------

def test_missing_po_file_reads_no_data_never_short(dd, vif):
    rep = _report(dd, vif)                        # po_path None, nothing to resolve
    inb = rep["inbound"]
    assert inb["state"] == "missing" and inb["source_path"] == ""
    assert inb["lines"] == [] and inb["receipts"] == {}
    assert inb["join"]["used"] == 0
    assert rep["supply_meta"]["feed_state"] == "missing"
    assert rep["supply_meta"]["receipts_window_end_h"] == 0.0
    verdicts = {r["block_id"]: r["supply"]["verdict"] for r in rep["schedule_view"]}
    assert verdicts == {"cs_a": "NO_DATA", "cs_b": "OK", "cs_c": "NO_DATA",
                        "cs_d": "NO_DATA"}
    a = _by_block(rep)["cs_a"]["supply"]
    assert a["text"].startswith("? 754800: on hand covers 6%")
    assert a["action"] == "move" and a["binding"] is None
    json.dumps(rep, allow_nan=False)


def test_bridge_file_resolves_when_po_path_none(dd, vif, tmp_path):
    _write_pos(dd / "reference" / "open_pos.xlsx")
    rep = _report(dd, vif)
    assert rep["inbound"]["state"] == "ok"
    assert rep["inbound"]["source_path"] == str(dd / "reference" / "open_pos.xlsx")
    assert _by_block(rep)["cs_a"]["supply"]["verdict"] == "DEPENDENT"


def test_configured_override_absent_is_missing_with_path(dd, vif, tmp_path, monkeypatch):
    import helpers.config as hc
    _write_pos(dd / "reference" / "open_pos.xlsx")     # bridge copy present…
    ghost = tmp_path / "it" / "NPA Open POs -9.01.xlsx"
    monkeypatch.setattr(hc, "load_toml", lambda path=None: {
        **CFG, "datasources": {"po_report_path": str(ghost)}})
    rep = _report(dd, vif)                              # …but the override wins
    inb = rep["inbound"]
    assert inb["state"] == "missing" and inb["source_path"] == str(ghost)
    assert any("not found" in e for e in inb["errors"])
    assert all(r["supply"]["verdict"] in ("OK", "NO_DATA") for r in rep["schedule_view"])
    # po_path=False skips the feed entirely
    rep2 = _report(dd, vif, po_path=False)
    assert rep2["inbound"]["state"] == "missing" and rep2["inbound"]["source_path"] == ""


def test_stale_feed_is_no_data(dd, vif, tmp_path):
    po = _write_pos(tmp_path / "open_pos.xlsx")
    rep = _report(dd, vif, po_path=po, today=date(2026, 9, 20))
    assert rep["inbound"]["state"] == "stale"
    assert rep["inbound"]["join"]["used"] == 1            # gated, only flagged
    assert _by_block(rep)["cs_a"]["supply"]["verdict"] == "NO_DATA"
    assert _by_block(rep)["cs_b"]["supply"]["verdict"] == "OK"


def test_empty_feed(dd, vif, tmp_path):
    rep = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx", rows=[]))
    assert rep["inbound"]["state"] == "empty" and rep["inbound"]["n_rows"] == 0
    assert _by_block(rep)["cs_a"]["supply"]["verdict"] == "NO_DATA"


# --------------------------------------------------------------------------
# the flat report is untouched by the supply section
# --------------------------------------------------------------------------

def test_flat_views_identical_with_and_without_feed(dd, vif, tmp_path):
    with_po = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    without = _report(dd, vif, po_path=False)
    for k in ("demand_view", "item_reverse", "no_bom_skus", "unk",
              "availability_toggles"):
        assert with_po[k] == without[k], k
    flat = ("block_id", "sku", "line_id", "line_name", "start_h", "end_h",
            "qty_kg", "cases", "status", "items", "unk", "cycles")
    for a, b in zip(with_po["schedule_view"], without["schedule_view"]):
        assert {k: a[k] for k in flat} == {k: b[k] for k in flat}
    assert with_po["sku_needs"] == without["sku_needs"]
    assert with_po["supply_meta"]["opening"] == without["supply_meta"]["opening"]


def test_unknown_quantity_is_no_data_not_green(dd, vif, tmp_path):
    """An unrated (line, SKU) has qty 0 in the flat path; the timeline must
    read it as unknown, never as a covered run."""
    (dd / "reference" / "capabilities_rates.csv").write_text(
        "line_id,sku,line_name,capable,calc_rate_kgph\n0,280351,P09,1,1000.0\n",
        encoding="utf-8")
    rep = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    by = _by_block(rep)
    assert by["cs_a"]["cases"] == 0.0 and by["cs_a"]["status"] == "OK"   # flat
    assert by["cs_a"]["supply"]["verdict"] == "NO_DATA"
    assert by["cs_a"]["supply"]["text"] == "? supply: no recipe or quantity data"
    assert by["cs_b"]["supply"]["verdict"] == "OK"


# --------------------------------------------------------------------------
# input resolution shared with the cache
# --------------------------------------------------------------------------

def test_receiving_schedule_path_and_supply_inputs(dd, tmp_path, monkeypatch):
    import helpers.paths as hp
    assert receiving_schedule_path(dd) is None
    dev = dd / "stockcheck" / "dev_receiving_schedule.xlsm"
    dev.write_bytes(b"x")
    assert receiving_schedule_path(dd) == dev
    ref = dd / "reference" / "Shipping Receiving Schedule NPA - 2024.xlsm"
    ref.write_bytes(b"x")
    assert receiving_schedule_path(dd) == ref            # bridge copy wins
    toml = tmp_path / "flowstate.toml"
    monkeypatch.setattr(hp, "toml_path", lambda: toml)
    paths = supply_input_paths(dd, {})
    assert paths == [dd / "reference" / "open_pos.xlsx",
                     dd / "reference" / "open_pos.csv", ref, dev, toml]
    custom = tmp_path / "custom.xlsx"
    paths = supply_input_paths(dd, {"datasources": {"po_report_path": str(custom)}})
    assert custom in paths and paths.index(custom) == 2


# --------------------------------------------------------------------------
# gate diagnostics and the window bound come from the gate, once
# --------------------------------------------------------------------------

def test_gate_notes_surface_in_inbound_errors(dd, vif, tmp_path, monkeypatch):
    """A stock lot whose qty is not numeric is noted by the gate (and counted
    as 0); the note must land in inbound.errors next to the file's own
    import errors instead of dying inside feed_info. The extra lot is toggled
    off (status Out) so the opening stays 3,980 and only the note moves."""
    import pandas as pd
    import stockcheck.api as api
    real = api.refresh_vif_snapshot

    def poisoned(vif_folder, data_dir):
        snap = real(vif_folder, data_dir)
        pkg = snap.frames["jestkexp2.csv"]
        extra = pkg[pkg["item"] == "754800"].head(1).copy()
        extra["qty"] = "n/a"
        extra["status"] = "Out"
        extra["batch"] = "JUNK"
        snap.frames["jestkexp2.csv"] = pd.concat([pkg, extra], ignore_index=True)
        return snap

    monkeypatch.setattr(api, "refresh_vif_snapshot", poisoned)
    rep = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    errs = rep["inbound"]["errors"]
    assert any("754800" in e and "'n/a'" in e and "not numeric" in e
               for e in errs), errs
    # additive: the feed still reads ok, the truck still counts, opening same
    assert rep["inbound"]["state"] == "ok" and rep["inbound"]["join"]["used"] == 1
    assert rep["supply_meta"]["opening"]["754800"] == pytest.approx(3980.0)
    assert _by_block(rep)["cs_a"]["supply"]["verdict"] == "DEPENDENT"
    json.dumps(rep, allow_nan=False)


def test_window_end_is_the_gates_value(dd, vif, tmp_path, monkeypatch):
    """ONE source for receipts_window_end_h: the gate already bounds it by
    23:59 of the file's last receipt date; the report must add nothing."""
    from stockcheck import timeline as tl
    seen = {}
    real = tl.gate_receipts

    def spy(*a, **k):
        out = real(*a, **k)
        seen["window"] = out[2]["receipts_window_end_h"]
        return out

    monkeypatch.setattr(tl, "gate_receipts", spy)
    rep = _report(dd, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    assert rep["supply_meta"]["receipts_window_end_h"] == pytest.approx(seen["window"])
    assert seen["window"] == pytest.approx(4 * 24 + 23 + 59 / 60)   # 9/5 23:59


def test_configured_override_directory_is_reported(dd, vif, tmp_path, monkeypatch):
    """po_report_path aimed at IT's drop FOLDER: the resolver hands it back
    by contract, the report says it is a directory — never reads the bridge
    copy behind the planner's back."""
    import helpers.config as hc
    _write_pos(dd / "reference" / "open_pos.xlsx")
    folder = tmp_path / "it_drop"
    folder.mkdir()
    monkeypatch.setattr(hc, "load_toml", lambda path=None: {
        **CFG, "datasources": {"po_report_path": str(folder)}})
    rep = _report(dd, vif)
    inb = rep["inbound"]
    assert inb["state"] == "missing" and inb["source_path"] == str(folder)
    assert any("directory" in e and "it_drop" in e for e in inb["errors"]), inb["errors"]
    assert all(r["supply"]["verdict"] in ("OK", "NO_DATA") for r in rep["schedule_view"])
