# tests/test_stock_drop_wiring.py — wiring of the ERP drop of 2026-09-15 into
# the stock engine, WITHOUT the loaders: depot groups + default toggles
# (stockcheck/coverage.py), extra lot frames in available_stock / lot_detail,
# the presence anchor (ediact.csv OR the legacy "ediact 3.csv") shared by
# data_health / reconcile_engine / the report cache, the snapshot mtime gate,
# and the report built from a snapshot that carries the new frames
# (jestkamb / jestkexq / PKG-REC) on synthetic data. Every frame is built here
# in the loader contract's column shape; vif_import's loaders are never
# called for the new files, so these tests hold before and after the loader
# lands.

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import helpers.config as _hc  # noqa: E402
from helpers import stock_report_cache as src  # noqa: E402
from stockcheck import api  # noqa: E402

# the real toml reader, captured before the autouse pinned_cfg patches it
# (the page smoke test boots the app on the real config, 2026-09-16)
_REAL_LOAD_TOML = _hc.load_toml
from stockcheck import coverage as cov  # noqa: E402
from stockcheck.vif_import import VifSnapshot  # noqa: E402

DEV_VIF = ROOT / "data" / "stockcheck" / "dev_vif"
# the OLD names the bundled fixture holds (the drop's names are synthesised)
OLD_VIF_NAMES = ("ediact 3.csv", "ediact 4.csv", "jestkexp.csv",
                 "jestkexp2.csv", "azapart.csv", "rmpkitems.csv")
ANCHOR = "2026-09-01 00:00:00"
SNAP = datetime(2026, 8, 31, 6, 0, 0)
STAMP = "2026-08-31 06:00:00"
TODAY = date(2026, 9, 1)
CFG = {"scheduler": {"planning_start_date": ANCHOR}}

LOT_COLS = ["item", "designation", "scn", "status", "statut", "bbd", "batch",
            "qty", "unit", "depot", "location", "precaut", "supplier_batch",
            "source"]
SLIP_COLS = ["receipt_date", "item", "designation", "batch", "qty", "unit",
             "po8", "slip", "user", "source"]


def _lot(item, depot, status, qty, *, batch="462151", unit="EA",
         source="jestkamb.csv") -> dict:
    return {"item": item, "designation": f"D {item}", "scn": "1", "status": status,
            "statut": status.upper(), "bbd": pd.Timestamp("2027-01-01"),
            "batch": batch, "qty": float(qty), "unit": unit, "depot": depot,
            "location": "L1", "precaut": "", "supplier_batch": "",
            "source": source}


def _lots(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=LOT_COLS)


# --------------------------------------------------------------------------
# coverage.py — depot groups, defaults, extra frames
# --------------------------------------------------------------------------

def test_default_toggles_per_depot_group():
    """Decision of 2026-09-15: Ava on for the plant, AMB, SA1/SA2 and M12
    depots; QCR/QCP off for EVERY status; Loc/Out/OL off everywhere; the
    seven pre-drop depots keep exactly their old defaults."""
    t = cov.default_toggles()
    assert "OL" in cov.STATUSES
    assert cov.QC_DEPOTS == ("QCR", "QCP")
    assert set(cov.ALL_DEPOTS) == {"M01", "SB1", "SC1", "SF1", "M02", "SFG", "M21",
                                   "AMB", "SA1", "SA2", "M12", "QCR", "QCP"}
    assert set(t) == {f"{d}|{s}" for d in cov.ALL_DEPOTS for s in cov.STATUSES}
    for dep in ("M01", "SB1", "SC1", "SF1", "M02", "SFG", "M21",
                "AMB", "SA1", "SA2", "M12"):
        assert t[f"{dep}|Ava"] is True, dep
        assert not t[f"{dep}|Loc"] and not t[f"{dep}|Out"] and not t[f"{dep}|OL"], dep
    for dep in cov.QC_DEPOTS:
        for s in cov.STATUSES:
            assert t[f"{dep}|{s}"] is False, (dep, s)
    # every group label the toggle UI renders, in order, covering all depots
    labels = [lbl for lbl, _ in cov.DEPOT_GROUPS]
    assert labels == ["Raw materials", "Packaging", "Off-site AMB",
                      "Apples SA1-SA2", "Semi-finished M12",
                      "Quality control QCR-QCP"]
    assert cov.RM_DEPOTS == ("M01", "SB1", "SC1", "SF1", "M02")
    assert cov.PKG_DEPOTS == ("SFG", "M21")
    assert cov.IN_HOUSE_ITEMS == {"BT001", "BT002"}


def test_available_stock_sums_extra_frames_and_keeps_positional_signature():
    """available_stock(rm, pkg, toggles) is the WW33 call; `extra` adds the
    drop's frames. AMB Ava counts by default, QC never does until toggled,
    OL is off, an unknown depot|status key is excluded."""
    rm = _lots([_lot("730009", "M01", "Ava", 100, unit="Kg", source="jestkexp.csv"),
                _lot("730009", "M01", "Loc", 50, unit="Kg", source="jestkexp.csv")])
    pkg = _lots([_lot("754800", "SFG", "Ava", 10, source="jestkexp2.csv")])
    amb = _lots([_lot("730009", "AMB", "Ava", 1000, unit="Kg"),
                 _lot("730009", "AMB", "Loc", 5000, unit="Kg")])
    qc = _lots([_lot("730009", "QCR", "Ava", 700, unit="Kg", source="jestkexq.csv"),
                _lot("754800", "QCP", "Ava", 7, source="jestkexq.csv")])
    semi = _lots([_lot("HSM764059", "M12", "Ava", 3, unit="Kg", source="jestkexp5.csv"),
                  _lot("HSM764059", "M12", "OL", 9, unit="Kg", source="jestkexp5.csv")])
    odd = _lots([_lot("730009", "ZZZ", "Ava", 99999, unit="Kg", source="x.csv")])
    # the old two-frame call: numbers exactly as before
    assert cov.available_stock(rm, pkg) == {"730009": 100.0, "754800": 10.0}
    assert cov.available_stock(rm, pkg, None) == {"730009": 100.0, "754800": 10.0}
    got = cov.available_stock(rm, pkg, None, extra=[amb, qc, semi, odd])
    assert got == {"730009": 1100.0, "754800": 10.0, "HSM764059": 3.0}
    # opting QC in
    on = {**cov.default_toggles(), "QCR|Ava": True, "QCP|Ava": True, "M12|OL": True}
    got = cov.available_stock(rm, pkg, on, extra=(amb, qc, semi))
    assert got == {"730009": 1800.0, "754800": 17.0, "HSM764059": 12.0}
    # empty / None extras are skipped like an empty pkg frame
    assert cov.available_stock(rm, None, extra=[None, pd.DataFrame()]) == {"730009": 100.0}


def test_partial_saved_toggles_take_defaults_for_new_depots():
    """A settings.json saved before the drop knows only the seven old
    depots. Its missing AMB/SA/M12 keys must read as their DEFAULTS (Ava
    on), not as 'off' — otherwise every planner with saved settings
    silently loses the off-site and apple stock. An explicit False still
    wins, and a depot outside every group stays excluded."""
    rm = _lots([_lot("730009", "M01", "Ava", 100, unit="Kg", source="jestkexp.csv")])
    amb = _lots([_lot("730009", "AMB", "Ava", 1000, unit="Kg")])
    apples = _lots([_lot("730065", "SA1", "Ava", 400, unit="Kg", source="jestksav.csv")])
    old_keys = {f"{d}|{s}": (s == "Ava") for d in cov.RM_DEPOTS + cov.PKG_DEPOTS
                for s in ("Ava", "Loc", "Out")}
    got = cov.available_stock(rm, None, old_keys, extra=[amb, apples])
    assert got == {"730009": 1100.0, "730065": 400.0}
    got = cov.available_stock(rm, None, {**old_keys, "AMB|Ava": False}, extra=[amb, apples])
    assert got == {"730009": 100.0, "730065": 400.0}
    # the old rule for the old depots is untouched: M01|Ava False -> nothing
    got = cov.available_stock(rm, None, {**old_keys, "M01|Ava": False}, extra=[amb])
    assert got == {"730009": 1000.0}


def test_lot_detail_spans_extra_frames():
    rm = _lots([_lot("730009", "M01", "Ava", 100, unit="Kg", source="jestkexp.csv")])
    pkg = _lots([_lot("754800", "SFG", "Ava", 10, source="jestkexp2.csv")])
    amb = _lots([_lot("730009", "AMB", "Loc", 5000, unit="Kg")])
    qc = _lots([_lot("730009", "QCR", "Ava", 700, unit="Kg", source="jestkexq.csv")])
    old = cov.lot_detail(rm, pkg, "730009")
    assert list(old["source"]) == ["jestkexp.csv"]
    new = cov.lot_detail(rm, pkg, "730009", extra=[amb, qc])
    assert list(new["source"]) == ["jestkexp.csv", "jestkamb.csv", "jestkexq.csv"]
    assert list(new["depot"]) == ["M01", "AMB", "QCR"]
    assert cov.lot_detail(rm, pkg, "nope", extra=[amb]).empty


# --------------------------------------------------------------------------
# presence anchor: ediact.csv OR "ediact 3.csv"
# --------------------------------------------------------------------------

def test_find_bom_file_accepts_either_name(tmp_path):
    """The drop replaced "ediact 3.csv" with ediact.csv: the shared anchor
    finds either, prefers the new name when both exist, ignores a stray
    folder by that name and returns None for an empty folder."""
    assert src.find_bom_file(tmp_path) is None
    (tmp_path / "ediact 3.csv").write_text("x", encoding="utf-8")
    assert src.find_bom_file(tmp_path) == tmp_path / "ediact 3.csv"
    (tmp_path / "ediact.csv").write_text("x", encoding="utf-8")
    assert src.find_bom_file(tmp_path).name == "ediact.csv"
    other = tmp_path / "other"
    (other / "ediact.csv").mkdir(parents=True)          # a folder is not a file
    assert src.find_bom_file(other) is None
    assert src.find_bom_file(str(tmp_path)).name == "ediact.csv"


def test_stock_report_inputs_resolve_reference_on_ediact_csv(tmp_path):
    """reconcile_engine.stock_report_inputs: data/reference is the VIF
    folder when it holds the BOM export under EITHER name; otherwise the
    bundled dev fixtures. A saved vif_folder still wins."""
    from helpers.reconcile_engine import stock_report_inputs
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    (dd / "stockcheck").mkdir()
    vif, tog = stock_report_inputs(dd)
    assert vif == str(dd / "stockcheck" / "dev_vif") and tog == {}
    (dd / "reference" / "ediact.csv").write_text("x", encoding="utf-8")
    assert stock_report_inputs(dd)[0] == str(dd / "reference")
    (dd / "reference" / "ediact.csv").unlink()
    (dd / "reference" / "ediact 3.csv").write_text("x", encoding="utf-8")
    assert stock_report_inputs(dd)[0] == str(dd / "reference")
    (dd / "stockcheck" / "settings.json").write_text(
        json.dumps({"vif_folder": "Z:/somewhere", "toggles": {"AMB|Ava": False}}),
        encoding="utf-8")
    assert stock_report_inputs(dd) == ("Z:/somewhere", {"AMB|Ava": False})


def test_data_health_vif_rule_accepts_ediact_csv(tmp_path):
    """The Command Center's VIF row anchors on the BOM export: fresh
    ediact.csv alone reads OK (no "ediact 3.csv" needed); neither name in
    data/reference falls back to the dev fixtures (STALE) or MISSING."""
    from helpers import data_health as dh
    from test_data_health import _cfg, _empty_data_dir, _min_catalog, _touch
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "ediact 3.csv").unlink()
    _touch(dd / "reference" / "ediact.csv", 0.1)
    hit = next(h for h in dh.assess(dd, _cfg()) if h.key == "vif_stock")
    assert hit.state == dh.OK, hit
    (dd / "reference" / "ediact.csv").unlink()
    hit = next(h for h in dh.assess(dd, _cfg()) if h.key == "vif_stock")
    assert hit.state == dh.MISSING, hit
    # dev fixtures under the data dir, under the NEW name: STALE (warn)
    _touch(dd / "stockcheck" / "dev_vif" / "ediact.csv", 0.1)
    hit = next(h for h in dh.assess(dd, _cfg()) if h.key == "vif_stock")
    assert hit.state == dh.STALE and "dev fixtures" in hit.detail, hit


def test_vif_file_names_cover_the_drop(tmp_path, monkeypatch):
    """The mtime lists (report cache signature + refresh_vif_snapshot gate)
    carry every recognised export, old and new, so a new lot file landing
    in the folder moves the signature."""
    for names in (src._VIF_FILES, api._vif_file_names()):
        assert {"ediact.csv", "ediact 3.csv", "ediact 4.csv", "jestkexp.csv",
                "jestkexp2.csv", "jestkamb.csv", "jestkexq.csv", "jestksav.csv",
                "jestkexp5.csv", "PKG-REC.csv", "azapart.csv",
                "rmpkitems.csv"} <= set(names)
        assert len(names) == len(set(names))
    import helpers.config as hc
    import helpers.paths as hp
    toml = tmp_path / "flowstate.toml"
    toml.write_text("[stock]\n", encoding="utf-8")
    monkeypatch.setattr(hp, "toml_path", lambda: toml)
    monkeypatch.setattr(hc, "load_toml", lambda path=None: {})
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    vif = tmp_path / "vif"
    vif.mkdir()
    base = src.current_signature(dd, str(vif), {}, today=TODAY)
    (vif / "jestkamb.csv").write_text("x", encoding="utf-8")
    assert src.current_signature(dd, str(vif), {}, today=TODAY) != base
    (vif / "PKG-REC.csv").write_text("x", encoding="utf-8")
    assert src.current_signature(dd, str(vif), {}, today=TODAY) != base


# --------------------------------------------------------------------------
# the report on a snapshot carrying the drop's frames
# --------------------------------------------------------------------------

BOARD = (
    "block_id,block_type,line_id,line_name,start_h,end_h,label,order_id,sku,"
    "sku_description,qty_kg,locked,attrs\n"
    # 24 h @ 1000 kg/h of 120430 -> 66,667 sleeves (754800) vs 3,980 on hand
    "cs_a,production,0,P09,24,48,120430,1,120430,APL GGS,,False,\n"
)
DEMAND = (
    "order_id,sku,week_index,qty_target,lower_pct,upper_pct,due_start_hour,"
    "due_end_hour,priority\n"
    "280351-W0,280351,0,2160,0.9,1.1,0,167,3\n"
)
RATES = ("line_id,sku,line_name,capable,calc_rate_kgph\n"
         "0,120430,P09,1,1000.0\n")


@pytest.fixture(scope="module")
def vif(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("vif")
    t = time.mktime(SNAP.timetuple())
    for name in OLD_VIF_NAMES:
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


def _x3_line(po, item, qty, unit, receipt) -> str:
    from stockcheck import x3_po_export as x3
    v = {k: "" for k in x3.KEYS}
    v.update(company="M2", site="02", order_type="1ACDE", order_no=po,
             supplier_code="000048", supplier_name="GRAPHIC PACKAGING",
             order_date="24/07/2026", line_no="000001", line_seq="000001",
             line_status="20", item=str(item), item_name="SLV 4x90",
             qty_ordered=f"{int(round(qty * 10000)):013d}",
             qty_remaining=f"{int(round(qty * 10000)):013d}",
             order_unit=unit, stat_unit="KG",
             receipt_date=receipt.strftime("%d/%m/%Y"),
             requested_date=receipt.strftime("%d/%m/%Y"),
             receipt_location="RP1", currency_order="USD")
    return "|".join(v[k] for k in x3.KEYS)


def _write_po(path: Path) -> Path:
    from stockcheck import x3_po_export as x3
    header = "|".join(fr for _, fr, _ in x3.COLUMNS)
    rows = [_x3_line("30043600", 754800, 90000, "EA", datetime(2026, 9, 1))]
    path.write_bytes(("\r\n".join([header] + rows) + "\r\n").encode("cp1252"))
    return path


def _drop_snapshot(real, slip_date=pd.Timestamp("2026-08-31")):
    """refresh_vif_snapshot wrapper: the real (old-name) snapshot plus the
    drop's frames in the loader contract's shape — an AMB lot of the
    sleeve, a QC lot of it, a receipt slip for the PO, one garbage slip —
    and the loader notes."""
    def patched(vif_folder, data_dir):
        snap = real(vif_folder, data_dir)
        assert isinstance(snap, VifSnapshot)
        snap.frames["jestkamb.csv"] = _lots([
            _lot("754800", "AMB", "Ava", 1000),
            _lot("754800", "AMB", "Loc", 5000)])
        snap.frames["jestkexq.csv"] = _lots([
            _lot("754800", "QCR", "Ava", 700, source="jestkexq.csv")])
        snap.frames["PKG-REC.csv"] = pd.DataFrame([
            {"receipt_date": slip_date, "item": "754800", "designation": "SLV 4x90",
             "batch": "N62430001", "qty": 90000.0, "unit": "EA",
             "po8": "30043600", "slip": "30120501", "user": "DWOODSTR",
             "source": "PKG-REC.csv"},
            # garbage on the SAME PO (a slip is only examined when a PO line
            # asks for it): NaT date -> noted, never raised
            {"receipt_date": pd.NaT, "item": "754800", "designation": "SLV 4x90",
             "batch": "", "qty": 1.0, "unit": "EA", "po8": "30043600",
             "slip": "30120999", "user": "X", "source": "PKG-REC.csv"},
        ], columns=SLIP_COLS)
        for name in ("jestkamb.csv", "jestkexq.csv", "PKG-REC.csv"):
            snap.source_files[name] = STAMP
        snap.notes = ["jestkami.csv: empty export", "ediact.csv: 8443 duplicate rows dropped"]
        return snap
    return patched


def test_report_counts_extra_lot_frames_and_lands_by_receipt_slip(dd, vif, tmp_path,
                                                                  monkeypatch):
    """stock_check_report on a snapshot with jestkamb / jestkexq / PKG-REC:
    the AMB Ava lot joins the opening (3,980 + 1,000), the QC lot does not
    until toggled, a settings-era partial toggle dict still counts AMB, the
    PO line is LANDED by its receipt slip (before the lot rule, so it is
    not a receipt), the slip summary and the loader notes ride on the
    report, and the pre-drop keys keep their shape. The slip predates the
    stock count (2026-08-30 vs the 08-31 export): one dated ON the export
    day needs its batch in the lots (2026-09-16 second pass, see
    test_snapshot_day_slip_outside_the_lots_is_counted_on_its_date)."""
    real = api.refresh_vif_snapshot
    monkeypatch.setattr(api, "refresh_vif_snapshot",
                        _drop_snapshot(real, slip_date=pd.Timestamp("2026-08-30")))
    po = _write_po(tmp_path / "order_npa.csv")
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False, po_path=po)
    assert not rep.get("error"), rep
    meta = rep["supply_meta"]
    assert set(meta) == {"rules", "snapshot_h", "snapshot_stamp",
                         "receipts_window_end_h", "feed_state", "opening",
                         "tracked", "in_house", "units", "designations"}
    assert meta["opening"]["754800"] == pytest.approx(4980.0)
    assert meta["snapshot_stamp"] == {"rm": STAMP, "pkg": STAMP, "amb": STAMP, "qc": STAMP}
    assert meta["snapshot_h"]["754800"] == -18.0
    inb = rep["inbound"]
    assert inb["state"] == "ok"
    assert inb["join"]["landed"] == 1 and inb["join"]["used"] == 0
    line = next(ln for ln in inb["lines"] if ln["po8"] == "30043600")
    assert line["fate"] == "landed"
    assert line["reason"] == "receipt slip 30120501 on 2026-08-30: 90,000 of 90,000"
    assert inb["receipts"] == {}
    assert inb["slips"] == {"n_slips": 2, "n_pos": 1, "date_min": "2026-08-30",
                            "date_max": "2026-08-30", "source_mtime": STAMP,
                            "n_landed": 1, "n_counted": 0}
    # the NaT-dated slip is noted like a malformed lot, never raised
    assert any("30120999" in e and "unreadable" in e for e in inb["errors"]), inb["errors"]
    notes = ["jestkami.csv: empty export", "ediact.csv: 8443 duplicate rows dropped"]
    assert rep["import_notes"] == notes and inb["notes"] == notes
    # the truck is not counted: 4,980 of 66,667 on hand and nothing inbound
    sup = rep["schedule_view"][0]["supply"]
    assert sup["verdict"] == "SHORT" and sup["binding"] is None
    json.dumps(rep, allow_nan=False)

    # QC opted in -> +700; a pre-drop partial toggle dict -> AMB still counts
    rep2 = api.stock_check_report(dd, vif, {**cov.default_toggles(), "QCR|Ava": True},
                                  today=TODAY, receiving_path=False, po_path=po)
    assert rep2["supply_meta"]["opening"]["754800"] == pytest.approx(5680.0)
    old_keys = {f"{d}|{s}": (s == "Ava") for d in cov.RM_DEPOTS + cov.PKG_DEPOTS
                for s in ("Ava", "Loc", "Out")}
    rep3 = api.stock_check_report(dd, vif, old_keys,
                                  today=TODAY, receiving_path=False, po_path=po)
    assert rep3["supply_meta"]["opening"]["754800"] == pytest.approx(4980.0)


def test_future_dated_slip_leaves_the_line_counted(dd, vif, tmp_path, monkeypatch):
    """A slip dated after today is not evidence: the PO line stays 'used'
    and the block reads DEPENDENT on the truck as before the drop."""
    real = api.refresh_vif_snapshot
    monkeypatch.setattr(api, "refresh_vif_snapshot",
                        _drop_snapshot(real, slip_date=pd.Timestamp("2026-09-02")))
    po = _write_po(tmp_path / "order_npa.csv")
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False, po_path=po)
    inb = rep["inbound"]
    assert inb["join"]["used"] == 1 and inb["join"]["landed"] == 0
    assert list(inb["receipts"]) == ["754800"] and inb["slips"]["n_landed"] == 0
    assert rep["schedule_view"][0]["supply"]["verdict"] == "DEPENDENT"


def test_snapshot_day_slip_outside_the_lots_is_counted_on_its_date(dd, vif, tmp_path,
                                                                   monkeypatch):
    """Snapshot-day slips (review of 2026-09-16): the lot exports run mid-day,
    so a slip dated ON the export day whose batch is in no lot was booked
    after the count. The report counts the truck as a receipt on the slip
    date @ 16:00 (08-31 16:00 = hour -8) instead of landing it — the block
    reads DEPENDENT on it, not SHORT — and inbound.slips.n_counted says so.
    With the slip's batch in a lot export (a QC lot here) it lands."""
    real = api.refresh_vif_snapshot
    monkeypatch.setattr(api, "refresh_vif_snapshot", _drop_snapshot(real))
    po = _write_po(tmp_path / "order_npa.csv")
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False, po_path=po)
    inb = rep["inbound"]
    line = _po_line(rep)
    assert line["fate"] == "used", line
    assert "booked after the stock export: 90,000 of 90,000" in line["reason"]
    assert [(r["ready_h"], r["qty"]) for r in inb["receipts"]["754800"]] == [(-8.0, 90000.0)]
    assert inb["slips"]["n_landed"] == 0 and inb["slips"]["n_counted"] == 1
    assert rep["schedule_view"][0]["supply"]["verdict"] == "DEPENDENT"

    def in_lots(snap):
        snap.frames["jestkexq.csv"] = _lots([
            _lot("754800", "QCR", "Ava", 90000, batch="N62430001",
                 source="jestkexq.csv")])

    monkeypatch.setattr(api, "refresh_vif_snapshot",
                        _mutating_snapshot(_drop_snapshot(real), in_lots))
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False, po_path=po)
    assert _po_line(rep)["fate"] == "landed"
    assert rep["inbound"]["slips"]["n_landed"] == 1 and rep["inbound"]["receipts"] == {}


def test_old_two_file_snapshot_reports_exactly_as_before(dd, vif, tmp_path):
    """No drop frames at all (an old snapshot / old loader): the stamps hold
    only rm + pkg, import_notes is empty, opening is the dev fixture's
    3,980 — the WW33 numbers, untouched."""
    po = _write_po(tmp_path / "order_npa.csv")
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False, po_path=po)
    assert not rep.get("error"), rep
    assert rep["supply_meta"]["snapshot_stamp"] == {"rm": STAMP, "pkg": STAMP}
    assert rep["supply_meta"]["opening"]["754800"] == pytest.approx(3980.0)
    # the loader's notes (whatever it says about absent optional files) ride
    # on both keys, as lists
    assert isinstance(rep["import_notes"], list)
    assert rep["inbound"]["notes"] == rep["import_notes"]
    assert rep["inbound"]["slips"]["n_slips"] == 0
    assert rep["inbound"]["join"]["used"] == 1
    assert rep["schedule_view"][0]["supply"]["verdict"] == "DEPENDENT"


def test_error_report_names_both_bom_files(dd, tmp_path):
    """An empty VIF folder: the error report names ediact.csv and the
    legacy name so the planner knows which file the bridge must deliver."""
    empty = tmp_path / "empty_vif"
    empty.mkdir()
    rep = api.stock_check_report(dd, empty, today=TODAY, receiving_path=False,
                                 po_path=False)
    assert "ediact.csv" in rep["error"] and "ediact 3.csv" in rep["error"]
    assert isinstance(rep["import_notes"], list)


def test_refresh_gate_signs_every_recognised_name_without_reimporting(dd, vif, tmp_path):
    """refresh_vif_snapshot re-imports only when a recognised file's mtime
    moved — and, once imported, serves the SAME snapshot on the next call
    even though the folder holds names the loader did not load (the gate
    remembers what it signed instead of trusting source_files, which the
    loader fills only for loaded files). A new drop file appearing
    (jestkamb.csv) is one re-import, then gated again."""
    folder = tmp_path / "vif2"
    shutil.copytree(vif, folder)
    # a name the OLD loader does not know: must not force an import per call
    (folder / "jestkexp4.csv").write_text("x", encoding="utf-8")
    snaps = dd / "stockcheck" / "snapshots"
    first = api.refresh_vif_snapshot(folder, dd)
    assert first.frames and (snaps / "latest").exists()
    n = len([p for p in snaps.iterdir() if p.is_dir()])
    again = api.refresh_vif_snapshot(folder, dd)
    assert len([p for p in snaps.iterdir() if p.is_dir()]) == n
    assert again.imported_at == first.imported_at
    assert dict(getattr(again, "gate_mtimes")) and "jestkexp4.csv" in again.gate_mtimes
    # a recognised file lands (a readable AMB export: the rm layout carries
    # the same 13 columns) -> one re-import -> gated again
    shutil.copy(folder / "jestkexp.csv", folder / "jestkamb.csv")
    t = time.mktime(datetime(2026, 9, 1, 7, 0, 0).timetuple())
    os.utime(folder / "jestkamb.csv", (t, t))
    time.sleep(1.1)                      # imported_at is second-resolution
    third = api.refresh_vif_snapshot(folder, dd)
    assert len([p for p in snaps.iterdir() if p.is_dir()]) == n + 1
    assert third.imported_at != first.imported_at
    assert "jestkamb.csv" in third.gate_mtimes
    fourth = api.refresh_vif_snapshot(folder, dd)
    assert fourth.imported_at == third.imported_at
    assert len([p for p in snaps.iterdir() if p.is_dir()]) == n + 1


def test_refresh_gate_retries_an_unreadable_file_without_growing_the_snapshots(
        dd, vif, tmp_path, monkeypatch):
    """A file the loader could not read (snap.errors "name: exc") is left
    out of the signed set, so the next call imports again (a share hiccup
    must not hide a depot until the next export) — but an identical outcome
    is not saved again: one import per call, never one pickle per call.
    Once the file reads, the new snapshot is saved and the gate closes.
    The loader itself never fails on the dev fixture, so the hiccup is
    injected around it."""
    folder = tmp_path / "vif3"
    shutil.copytree(vif, folder)
    shutil.copy(folder / "jestkexp.csv", folder / "jestkamb.csv")
    real = api.import_vif_folder
    hiccup = {"on": True}

    def flaky(f):
        snap = real(f)
        if hiccup["on"]:
            snap.frames.pop("jestkamb.csv", None)
            snap.source_files.pop("jestkamb.csv", None)
            snap.errors.append("jestkamb.csv: [Errno 5] share hiccup")
        return snap

    monkeypatch.setattr(api, "import_vif_folder", flaky)
    snaps = dd / "stockcheck" / "snapshots"
    first = api.refresh_vif_snapshot(folder, dd)
    assert any(e.startswith("jestkamb.csv") for e in first.errors)
    assert "jestkamb.csv" not in first.gate_mtimes
    assert "jestkexp.csv" in first.gate_mtimes
    n = len([p for p in snaps.iterdir() if p.is_dir()])
    assert n == 1                             # the errored outcome is saved once
    time.sleep(1.1)                           # imported_at is second-resolution
    second = api.refresh_vif_snapshot(folder, dd)
    assert second.imported_at != first.imported_at          # retried
    assert any(e.startswith("jestkamb.csv") for e in second.errors)
    assert len([p for p in snaps.iterdir() if p.is_dir()]) == n   # not re-saved
    # the file reads again -> saved, then gated
    hiccup["on"] = False
    time.sleep(1.1)
    third = api.refresh_vif_snapshot(folder, dd)
    assert not third.errors and "jestkamb.csv" in third.gate_mtimes
    assert "jestkamb.csv" in third.frames
    assert len([p for p in snaps.iterdir() if p.is_dir()]) == n + 1
    fourth = api.refresh_vif_snapshot(folder, dd)
    assert fourth.imported_at == third.imported_at
    assert len([p for p in snaps.iterdir() if p.is_dir()]) == n + 1


# --------------------------------------------------------------------------
# engine fixes of 2026-09-16: gate snapshot date, never-counted frames,
# alias stamps, missing semi-finished recipes
# --------------------------------------------------------------------------

def _mutating_snapshot(real, mutate):
    """refresh_vif_snapshot wrapper: the real (old-name) snapshot, then
    `mutate(snap)` adds the drop frames a test needs."""
    def patched(vif_folder, data_dir):
        snap = real(vif_folder, data_dir)
        mutate(snap)
        return snap
    return patched


def _write_po_rows(path: Path, rows) -> Path:
    """order_npa.csv with [(po, item, qty, unit, receipt datetime)]."""
    from stockcheck import x3_po_export as x3
    header = "|".join(fr for _, fr, _ in x3.COLUMNS)
    lines = [_x3_line(*r) for r in rows]
    path.write_bytes(("\r\n".join([header] + lines) + "\r\n").encode("cp1252"))
    return path


def _po_line(rep, po8="30043600") -> dict:
    return next(ln for ln in rep["inbound"]["lines"] if ln["po8"] == po8)


def test_gate_snapshot_date_ignores_a_stale_side_export(dd, vif, tmp_path, monkeypatch):
    """Gate snapshot date (2026-09-16): the overdue rule dates from the rm /
    pkg exports only. A semi-finished export that kept an old mtime (3 days
    older) must not pull the date back — that let a receipt already in the
    counted on-hand through as a second receipt. The stale export gets an
    import note (more than 24 h older than the newest lot export) on both
    import_notes and inbound.notes; per-item snapshot hours are unchanged."""
    stale = "2026-08-28 06:00:00"

    def mutate(snap):
        snap.frames["jestkexp5.csv"] = _lots([
            _lot("HSM764059", "M12", "Ava", 500, unit="Kg", batch="N62400001",
                 source="jestkexp5.csv")])
        snap.source_files["jestkexp5.csv"] = stale

    monkeypatch.setattr(api, "refresh_vif_snapshot",
                        _mutating_snapshot(api.refresh_vif_snapshot, mutate))
    po = _write_po_rows(tmp_path / "order_npa.csv",
                        [("30043600", 754800, 90000, "EA", datetime(2026, 8, 30))])
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False, po_path=po)
    assert not rep.get("error"), rep
    line = _po_line(rep)
    # before the fix: snapshot date 2026-08-28 -> "used", counted twice
    assert line["fate"] == "overdue", line
    assert "before stock export 2026-08-31" in line["reason"]
    meta = rep["supply_meta"]
    assert meta["snapshot_stamp"]["semi"] == stale
    assert meta["snapshot_h"]["754800"] == -18.0
    stale_notes = [n for n in rep["import_notes"] if n.startswith("jestkexp5.csv:")]
    assert len(stale_notes) == 1, rep["import_notes"]
    assert "3.0 d before the newest lot export (jestkexp.csv 2026-08-31 06:00:00)" in stale_notes[0]
    assert rep["inbound"]["notes"] == rep["import_notes"]


def test_semi_finished_lots_never_count(dd, vif, tmp_path, monkeypatch):
    """Rule of 2026-08-14 (applied 2026-09-16): a house-made semi-finished
    intermediate never gates a SKU on its own stock. The jestkexp5 frame is
    kept OUT of available_stock, tracked_items and the landed rule's
    stock_lots, so with recipes missing the item reads NOT_TRACKED (never
    AT_RISK on a WIP lot); its lots still show in the lot detail. The
    snapshot's missing_recipes ride on the report."""
    def mutate(snap):
        snap.frames["jestkexp5.csv"] = _lots([
            _lot("758006", "M12", "Ava", 1, source="jestkexp5.csv"),
            # an in-window lot (2026-08-31) that would land the PO line
            _lot("754800", "M01", "Ava", 50000, batch="N62430001",
                 source="jestkexp5.csv")])
        snap.source_files["jestkexp5.csv"] = STAMP
        snap.missing_recipes = ["HSM764059", "RF730060"]

    monkeypatch.setattr(api, "refresh_vif_snapshot",
                        _mutating_snapshot(api.refresh_vif_snapshot, mutate))
    po = _write_po(tmp_path / "order_npa.csv")
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False, po_path=po)
    assert not rep.get("error"), rep
    pallet = next(i for i in rep["schedule_view"][0]["items"] if i["item"] == "758006")
    assert pallet["status"] == "NOT_TRACKED", pallet
    meta = rep["supply_meta"]
    assert "758006" not in meta["tracked"] and "758006" not in meta["opening"]
    assert meta["opening"]["754800"] == pytest.approx(3980.0)      # not + 50,000
    assert _po_line(rep)["fate"] == "used"                         # not landed
    assert meta["snapshot_stamp"]["semi"] == STAMP
    assert rep["missing_recipes"] == ["HSM764059", "RF730060"]
    # the lot detail still lists the semi-finished lots
    snap = api.refresh_vif_snapshot(vif, dd)
    lots = api._lot_frames(snap)
    detail = cov.lot_detail(snap.frames["jestkexp.csv"], snap.frames["jestkexp2.csv"],
                            "754800", extra=[df for n, df in lots
                                             if n not in ("jestkexp.csv", "jestkexp2.csv")])
    assert "jestkexp5.csv" in set(detail["source"])


def test_recipe_less_intermediate_never_gates_through_its_alternates(dd, vif, tmp_path,
                                                                     monkeypatch):
    """Rule of 2026-08-14, review of 2026-09-16: with "ediact 4.csv" absent a
    semi-finished input cannot be exploded, and the blank-qty alternates
    the walker attaches to it (the real drop: HSMBT001 + lemon juice 750061)
    made the group tracked — AT_RISK / SHORT blocks, DO_NOT_SCHEDULE orders.
    Here RFSPTP222 is made the largest Kg primary of 280351's slurry so the
    tracked 730021 (475 kg) becomes its alternate. The report grades it
    NOT_TRACKED (note names the missing recipe) on the flat view, and the
    unit recipe carries it with no alternates and outside `tracked`, so the
    supply verdict lists it untracked instead of SHORT."""
    def mutate(snap):
        snap.frames.pop("ediact 4.csv", None)
        e3 = snap.frames["ediact 3.csv"].copy()
        hit = (e3["Activity"] == "SPTP764066") & (e3["item"] == "RFSPTP222")
        assert hit.any()
        e3.loc[hit, "qty_act"] = 500.0             # now the largest Kg input
        snap.frames["ediact 3.csv"] = e3

    monkeypatch.setattr(api, "refresh_vif_snapshot",
                        _mutating_snapshot(api.refresh_vif_snapshot, mutate))
    (dd / "calendar_blocks.csv").write_text(
        BOARD + "cs_b,production,0,P09,60,84,280351,2,280351,KIRK,21600,False,\n",
        encoding="utf-8")
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False,
                                 po_path=False)
    assert not rep.get("error"), rep
    block = next(b for b in rep["schedule_view"] if b["block_id"] == "cs_b")
    rf = next(i for i in block["items"] if i["item"] == "RFSPTP222")
    assert "730021" in [a["item"] for a in rf["alternates"]]
    assert rf["status"] == "NOT_TRACKED", rf
    assert rf["note"] == "semi-finished, recipe missing (ediact 4.csv)"
    need = next(e for e in rep["sku_needs"]["280351"]["items"] if e["item"] == "RFSPTP222")
    assert need["alts"] == []
    assert "RFSPTP222" not in rep["supply_meta"]["tracked"]
    sup = block["supply"]
    assert "RFSPTP222" in sup["untracked"]
    assert all(e["item"] != "RFSPTP222" for e in sup.get("items") or [])
    for d in rep["demand_view"]:
        for c in d["constraining"]:
            if c["item"] == "RFSPTP222":
                assert c["status"] == "NOT_TRACKED"


def test_apple_lots_count_on_hand_and_land_only_snapshot_day_lines(dd, vif, tmp_path,
                                                                  monkeypatch):
    """Fresh apples (2026-09-16): jestksav lots (SA1/SA2) come off daily
    trucks, so a lot dated near a FUTURE PO line is another truck — that
    line stays a receipt. Review of 2026-09-16: dropping the frame from the
    landed rule altogether also stopped the snapshot day's own apple lots
    from landing that day's lines (130,000 kg of 730070 counted twice), so
    the frame rides to the gate as daily_lots: a same-day line lands on a
    same-day lot. The frame still feeds on hand and tracked. The future
    line comes first in the file, so it would take the lot if it could."""
    def mutate(snap):
        snap.frames["jestksav.csv"] = _lots([
            _lot("754800", "SA1", "Ava", 90000, batch="N62430001",
                 source="jestksav.csv")])
        snap.source_files["jestksav.csv"] = STAMP

    monkeypatch.setattr(api, "refresh_vif_snapshot",
                        _mutating_snapshot(api.refresh_vif_snapshot, mutate))
    po = _write_po_rows(tmp_path / "order_npa.csv",
                        [("30043600", 754800, 40000, "EA", datetime(2026, 9, 1)),
                         ("30043601", 754800, 40000, "EA", datetime(2026, 8, 31))])
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False, po_path=po)
    meta = rep["supply_meta"]
    assert meta["opening"]["754800"] == pytest.approx(93980.0)
    assert "754800" in meta["tracked"]
    assert _po_line(rep, "30043600")["fate"] == "used"            # future truck
    same_day = _po_line(rep, "30043601")
    assert same_day["fate"] == "landed", same_day                 # in the export
    assert same_day["reason"] == "90000 of 40000 already in lots dated 2026-08-31"
    assert rep["inbound"]["join"]["landed"] == 1
    assert [r["po8"] for r in rep["inbound"]["receipts"]["754800"]] == ["30043600"]


def test_alias_loaded_lot_frame_keeps_its_stamp(dd, vif, tmp_path, monkeypatch):
    """Alias stamps (2026-09-16): a lot frame the loader filled from its
    alias (jestkexp4.csv for jestkexp.csv) may be stamped under the real
    file only. The report falls back to the frame's 'source' file, then to
    the LOT_FALLBACKS alias — never a blank rm stamp."""
    alias_stamp = "2026-08-31 05:00:00"

    def mutate(snap):
        snap.source_files.pop("jestkexp.csv", None)
        snap.source_files["jestkexp4.csv"] = alias_stamp
        snap.frames["jestkexp.csv"] = snap.frames["jestkexp.csv"].assign(
            source="jestkexp4.csv")

    monkeypatch.setattr(api, "refresh_vif_snapshot",
                        _mutating_snapshot(api.refresh_vif_snapshot, mutate))
    rep = api.stock_check_report(dd, vif, today=TODAY, receiving_path=False,
                                 po_path=False)
    meta = rep["supply_meta"]
    assert meta["snapshot_stamp"]["rm"] == alias_stamp
    assert meta["snapshot_h"]["730022"] == -19.0          # an rm item

    # unit level: source column -> real file; else the alias; else blank
    rm = _lots([_lot("730009", "M01", "Ava", 1, unit="Kg", source="jestkexp4.csv")])
    snap = VifSnapshot(frames={"jestkexp.csv": rm},
                       source_files={"jestkexp4.csv": alias_stamp}, imported_at="")
    assert api._frame_stamp(snap, "jestkexp.csv", rm) == alias_stamp
    rm_named = rm.assign(source="jestkexp.csv")
    assert api._frame_stamp(snap, "jestkexp.csv", rm_named) == alias_stamp
    assert api._frame_stamp(snap, "jestkexp.csv", None) == ""
    assert api._frame_stamp(snap, "jestkexp2.csv", rm_named) == ""
    snap.source_files["jestkexp.csv"] = STAMP              # canonical wins
    assert api._frame_stamp(snap, "jestkexp.csv", rm) == STAMP


def test_vif_health_row_warns_on_missing_semi_recipes(tmp_path, monkeypatch):
    """VIF health row (2026-09-16): when the BOM export stands without
    "ediact 4.csv" beside it and vif_import.missing_semi_recipes names
    activities without a recipe, the row is STALE (warn, never blocking)
    with the count and the fix. The BOM is loaded once per file version,
    not at all when "ediact 4.csv" is present, and a helper that is absent
    or raises leaves the row OK."""
    from helpers import data_health as dh
    from stockcheck import vif_import as vi
    from test_data_health import _cfg, _empty_data_dir, _min_catalog, _touch
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "ediact 3.csv").unlink()
    _touch(dd / "reference" / "ediact.csv", 0.1)
    loads, calls = [], []

    def fake_load(p):
        loads.append(Path(p).name)
        return pd.DataFrame({"PF": ["X"]})

    def fake_missing(frames):
        calls.append(sorted(frames))
        return ["RF730060", "HSM764059", "HSM764060", "RF730061"]

    monkeypatch.setattr(vi, "load_ediact", fake_load)
    monkeypatch.setattr(vi, "missing_semi_recipes", fake_missing, raising=False)
    monkeypatch.setattr(dh, "_MISSING_RECIPES_MEMO", {})

    def row():
        return next(h for h in dh.assess(dd, _cfg()) if h.key == "vif_stock")

    hit = row()
    assert hit.state == dh.STALE and hit.severity == 1, hit
    assert "4 semi-finished recipe(s) missing from ediact.csv" in hit.detail
    assert "add ediact 4.csv (semi-finished recipes) to the VIF folder" in hit.detail
    assert any("ediact 4.csv" in a for a in hit.actions)
    assert loads == ["ediact.csv"] and calls == [["ediact 3.csv"]]
    row()                                     # same file version: memoised
    assert len(loads) == 1
    _touch(dd / "reference" / "ediact 4.csv", 0.1)
    assert row().state == dh.OK and len(loads) == 1
    (dd / "reference" / "ediact 4.csv").unlink()

    def boom(frames):
        raise RuntimeError("helper broke")

    monkeypatch.setattr(vi, "missing_semi_recipes", boom)
    monkeypatch.setattr(dh, "_MISSING_RECIPES_MEMO", {})
    assert row().state == dh.OK
    monkeypatch.delattr(vi, "missing_semi_recipes", raising=False)
    assert row().state == dh.OK


def test_stock_check_page_warns_on_missing_semi_recipes(tmp_path, monkeypatch):
    """Stock Check page (2026-09-16): a report whose missing_recipes is
    non-empty, or whose import_notes say "semi-finished recipes missing",
    shows a warning above the report naming the fix; none otherwise."""
    monkeypatch.setattr(_hc, "load_toml", _REAL_LOAD_TOML)
    from test_pages_smoke import _boot, _seed_saved_report, _supply_report

    def warnings(rep, sub):
        d = tmp_path / sub
        d.mkdir()
        _seed_saved_report(d, monkeypatch, rep)
        at = _boot("pages/stock_check.py", d)
        assert not at.exception
        return "\n".join(str(w.value) for w in at.warning)

    w = warnings({**_supply_report(), "missing_recipes": ["HSM764059", "RF730060"]}, "a")
    assert "Semi-finished recipes missing for 2 intermediate(s) (HSM764059, RF730060)" in w
    assert "ediact 4.csv" in w
    w = warnings({**_supply_report(),
                  "import_notes": ["ediact.csv: semi-finished recipes missing (24)"]}, "b")
    assert "Semi-finished recipes missing:" in w and "ediact 4.csv" in w
    assert "Semi-finished recipes missing" not in warnings(_supply_report(), "c")
