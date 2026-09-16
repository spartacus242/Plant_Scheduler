# tests/test_stockcheck_projection_report.py — slice 3 through
# stock_check_report: demand_view[i].projected + the `projection` header on
# the synthetic board of test_stockcheck_report_supply (dev_vif exports,
# pinned anchor 2026-09-01, one sleeves PO landing 9/1 16:00 = h16).
#
# Demand weeks anchor at Monday 2026-08-31 (sidecar), one day BEFORE the
# board anchor: W0 = [-24, 144), W1 = [144, 312) in board hours — the
# demand-anchor vs board-anchor conversion the plan's §9 asks for.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from test_stockcheck_report_supply import (  # noqa: E402,F401
    _report, _write_pos, dd, pinned_cfg, vif)

DEMAND = (
    "order_id,sku,week_index,qty_target,lower_pct,upper_pct,due_start_hour,"
    "due_end_hour,priority\n"
    "280351-W0,280351,0,2160,0.9,1.1,0,167,3\n"
    "120430-W1,120430,1,10000,0.9,1.1,168,335,3\n"
)
SIDECAR = json.dumps({"anchor": "2026-08-31 00:00:00", "anchor_iso_week": 36})


@pytest.fixture()
def dd2(dd):
    (dd / "reference" / "demand_plan.csv").write_text(DEMAND, encoding="utf-8")
    (dd / "reference" / "demand_plan.source.json").write_text(SIDECAR, encoding="utf-8")
    return dd


def _by_order(rep):
    return {d["order_id"]: d for d in rep["demand_view"]}


def test_projection_rides_on_demand_view(dd2, vif, tmp_path):
    rep = _report(dd2, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    assert not rep.get("error"), rep
    json.dumps(rep, allow_nan=False)
    hdr = rep["projection"]
    assert hdr["demand_anchor"] == "2026-08-31 00:00:00"
    assert hdr["buffer_h"] == 96.0 and hdr["dns_ratio"] == 0.9
    assert hdr["feed_state"] == "ok"
    dv = _by_order(rep)

    # 280351: the board's cs_b (4 h @ 1000 kg/h = 4,000 kg, inside W0)
    # already covers the 2,160 kg target → COVERED, headroom as the cap,
    # no floor
    p = dv["280351-W0"]["projected"]
    assert p["status"] == "COVERED" and p["earliest_start_h"] is None
    assert p["board_kg"] == pytest.approx(4000.0) and p["residual_kg"] == 0.0
    assert p["ratio_gross"] == pytest.approx(4000 / 2160)
    assert p["cap_kg"] is not None and p["cap_kg"] > 0
    assert p["window_h"] == [-24.0, 144.0]

    # 120430-W1: the board's three sleeve runs (66,667 + 16,667 + 5,556 EA)
    # overdraw the 3,980 on hand; cs_d (4 h @ 500 kg/h = 2,000 kg) sits in
    # W1 and credits it, leaving 8,000 kg = 1,851.85 cases = 22,222 EA to
    # plan. Only the 90,000 EA truck (ready h16, usable h112 < ws 144)
    # supports anything: 3,980 + 90,000 - 88,889 = 5,091 EA = 424 cases =
    # 1,833 kg → still under 90 % → CAPPED with the floor at h112.
    p = dv["120430-W1"]["projected"]
    assert p["window_h"] == [144.0, 312.0]
    assert p["board_kg"] == pytest.approx(2000.0, abs=1.0)
    assert p["residual_kg"] == pytest.approx(8000.0, abs=1.0)
    assert p["status"] == "DNS"
    assert p["earliest_start_h"] == pytest.approx(112.0)
    assert p["cap_kg"] == pytest.approx(5091 / 12 * 4.32, rel=1e-3)
    assert p["onhand_ratio"] is not None and p["onhand_ratio"] < 0
    assert 0.2 < p["ratio"] < 0.3
    assert p["constraining"] == "754800"
    assert [r["po8"] for r in p["receipts"]] == ["30043600"]
    assert "PO 30043600 (754800) Tue 9/1 16:00" in p["text"]
    assert "from Sat 9/5 16:00" in p["text"]
    assert hdr["n_floors"] == 1 and hdr["n_capped"] == 1
    assert hdr["by_status"] == {"OK": 0, "COVERED": 1, "LIFTED": 0, "DNS": 1,
                                "NO_DATA": 0}


def test_projection_without_po_feed_counts_no_receipts(dd2, vif):
    rep = _report(dd2, vif, po_path=False)
    p = _by_order(rep)["120430-W1"]["projected"]
    assert rep["projection"]["feed_state"] == "missing"
    assert p["status"] == "DNS" and p["earliest_start_h"] is None
    assert p["cap_kg"] == 0.0                       # the board overdraws the pile
    assert "PO feed missing" in p["text"]


def test_reconcile_names_capped_orders_from_the_projection(dd2, vif, tmp_path):
    from helpers.reconcile_engine import stock_findings
    rep = _report(dd2, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    f = next(x for x in stock_findings(rep) if x.key == "stock_dns_demand")
    assert f.title.startswith("1 demand order(s) capped by components")
    assert "120430-W1" in f.detail
    assert f.context["orders"] == ["120430-W1"] and f.context["lifted"] == []


def test_stock_reports_demand_rows_show_solver_columns(dd2, vif, tmp_path):
    from helpers import stock_reports as sr
    rep = _report(dd2, vif, po_path=_write_pos(tmp_path / "open_pos.xlsx"))
    rows = sr.demand_rows(rep, None, lambda w: f"W{w}", stamp=lambda h: f"h{h:.0f}")
    by = {r["SKU"]: r for r in rows}
    assert by["120430"]["Solver"] == "CAPPED"
    assert by["120430"]["Cap kg"] == 1833
    assert by["120430"]["Earliest start"] == "h112"
    assert by["280351"]["Solver"] == "COVERED" and by["280351"]["Cap kg"] == ""
    assert by["280351"]["Earliest start"] == ""
    # a report without the projection shows none of these columns
    for d in rep["demand_view"]:
        d.pop("projected")
    rows = sr.demand_rows(rep, None, lambda w: f"W{w}")
    assert "Solver" not in rows[0]
