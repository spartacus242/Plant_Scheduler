# tests/test_stockcheck_engine.py — golden-value tests from real WW33 data.
#
# Run from repo root:  python -m pytest tests/test_stockcheck_engine.py -x -q
# (pytest is in the repo venv). Fixtures: data/stockcheck/dev_vif (real VIF
# export shapes generated from the WW33 stock-check workbook).

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from stockcheck import coverage as cov  # noqa: E402
from stockcheck.bom import BomGraph  # noqa: E402
from stockcheck.vif_import import import_vif_folder  # noqa: E402

VIF = ROOT / "data" / "stockcheck" / "dev_vif"


@pytest.fixture(scope="module")
def snap():
    s = import_vif_folder(VIF)
    assert not s.errors, s.errors
    return s


def test_import_shapes(snap):
    assert len(snap.frames["ediact 3.csv"]) > 14000
    assert len(snap.frames["jestkexp.csv"]) > 13000
    assert len(snap.frames["jestkexp2.csv"]) > 4000
    assert len(snap.frames["azapart.csv"]) > 400
    assert len(snap.frames["rmpkitems.csv"]) > 1900


def test_golden_1_stock_730009(snap):
    rm = snap.frames["jestkexp.csv"]
    ava_only = cov.available_stock(
        rm, snap.frames["jestkexp2.csv"],
        {k: (s == "Ava") for k in cov.default_toggles()
         for s in [k.split("|")[1]]})
    assert ava_only["730009"] == pytest.approx(252140.0)
    all_on = cov.available_stock(rm, snap.frames["jestkexp2.csv"],
                                 {k: True for k in cov.default_toggles()})
    assert all_on["730009"] == pytest.approx(485040.0)


def test_golden_2_explosion_280351(snap):
    bom = BomGraph(snap.frames["ediact 3.csv"])
    res = bom.explode("280351", 2640.0)
    assert res.status in ("OK", "UNK_PARTIAL")
    req = {g.primary_item: g for g in res.requirements}
    assert req["TL760116"].need_qty == pytest.approx(2640.0)
    assert req["TL760116"].unit == "SLV"
    # full chain: 2640 CAS * 24 POU/SLV * 45/1000 kg slurry * 432|454/1000
    expected = 2640 * 24 * (45 / 1000 * 454 / 1000 + 45 / 1000 * 432 / 1000)
    assert req["730009"].need_qty == pytest.approx(expected, abs=0.01)
    # alternate of 730009 in SPTP764066: 730009-V (blank qty)
    alt_items = [a["item"] for a in req["730009"].alternates]
    assert "730009-V" in alt_items


def test_golden_3_four_slurry_blend(snap):
    bom = BomGraph(snap.frames["ediact 3.csv"])
    res = bom.explode("280480", 1000.0)
    req = {g.primary_item: g for g in res.requirements}
    for slurry in ("GSSPTP9221", "GSSPTP9222", "GSSPTP9223", "GSSPTP9224"):
        assert slurry in req, f"{slurry} missing (blend = all four)"


def test_golden_4_no_bom_skus(snap):
    bom = BomGraph(snap.frames["ediact 3.csv"])
    no_bom = ["280181", "280464", "280465", "280466", "280467", "280468",
              "280469", "280473", "280474", "280481"]
    for sku in no_bom:
        assert bom.explode(sku, 100.0).status == "NO_BOM", sku


def test_golden_5_120430_fg_inputs(snap):
    bom = BomGraph(snap.frames["ediact 3.csv"])
    res = bom.explode("120430", 1600.0)
    req = {g.primary_item: g for g in res.requirements}
    assert req["756150"].need_qty == pytest.approx(1600.0)  # wrap, 1:1 per CAS
    assert req["756150"].unit == "EA"
    assert req["758006"].need_qty == pytest.approx(10.0)    # pallets


def test_coverage_status_thresholds():
    assert cov.item_status(1.5) == "OK"
    assert cov.item_status(1.10) == "OK"
    assert cov.item_status(1.05) == "TIGHT"
    assert cov.item_status(0.95) == "TIGHT"
    assert cov.item_status(0.94) == "AT_RISK"
    assert cov.item_status(0.0) == "AT_RISK"
    assert cov.item_status(float("inf")) == "OK"


def test_scn_rows_dropped(snap):
    bom = BomGraph(snap.frames["ediact 3.csv"])
    assert not bom.df["item"].str.endswith("SCN").any()


def test_report_end_to_end(snap, tmp_path):
    from stockcheck.api import stock_check_report
    rep = stock_check_report(ROOT / "data", VIF)
    assert "schedule_view" in rep and "demand_view" in rep
    assert rep["source_files"].get("ediact 3.csv")
    assert isinstance(rep["item_reverse"], dict)
