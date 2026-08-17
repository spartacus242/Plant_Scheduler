# tests/test_holding_builder.py — under-target demand -> holding blocks.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.holding_builder import (  # noqa: E402
    average_rate_per_sku,
    build_holding,
)


def _demand(rows: list[dict]) -> pd.DataFrame:
    base = {"order_id": "X-W0", "sku": "280581", "qty_target": 10000,
            "lower_pct": 0.9, "upper_pct": 1.1, "week_index": 0,
            "due_start_hour": 0, "due_end_hour": 167, "priority": 3}
    return pd.DataFrame([{**base, **r} for r in rows])


def _produced(rows: list[dict]) -> pd.DataFrame:
    base = {"order_id": "X-W0", "sku": "280581", "qty_min": 9000,
            "qty_max": 11000, "produced": 9000, "in_bounds": True}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_under_qmin_creates_holding_block():
    dem = _demand([{"order_id": "280581-W0"}])
    prod = _produced([{"order_id": "280581-W0", "qty_min": 9000, "produced": 5000}])
    blocks = build_holding(dem, prod, rates={"280581": 540.0})
    assert len(blocks) == 1
    b = blocks[0]
    assert b.order_id == "280581-W0"
    assert b.qty_kg == pytest.approx(4000)   # 9000 − 5000
    assert b.run_hours == pytest.approx(4000 / 540.0, abs=0.01)
    assert b.reason == "under_qmin"


def test_zero_qty_creates_holding_block():
    dem = _demand([{"order_id": "280581-W0"}])
    prod = _produced([{"order_id": "280581-W0", "produced": 0}])
    blocks = build_holding(dem, prod, rates={"280581": 540.0})
    assert len(blocks) == 1
    assert blocks[0].reason == "zero_qty"
    assert blocks[0].qty_kg == pytest.approx(9000)


def test_met_order_has_no_holding_block():
    dem = _demand([{"order_id": "280581-W0"}])
    prod = _produced([{"order_id": "280581-W0", "produced": 9500}])
    assert build_holding(dem, prod, rates={"280581": 540.0}) == []


def test_current_mo_rows_never_hold():
    dem = _demand([{"order_id": "29901|CUR", "sku": "280581"}])
    prod = _produced([{"order_id": "29901|CUR", "qty_min": 10000, "produced": 0}])
    assert build_holding(dem, prod) == []


def test_zero_rate_gives_zero_run_hours_but_keeps_block():
    dem = _demand([{"order_id": "280581-W0"}])
    prod = _produced([{"order_id": "280581-W0", "produced": 0}])
    blocks = build_holding(dem, prod, rates={})
    assert len(blocks) == 1 and blocks[0].run_hours == 0.0


def test_average_rate_per_sku():
    caps = pd.DataFrame({
        "sku": ["280581", "280581", "280581", "251200"],
        "line_name": ["P09", "P10", "P11", "P22"],
        "capable": [1, 1, 0, 1],
        "calc_rate_kgph": [540.0, 600.0, 540.0, 800.0],
    })
    rates = average_rate_per_sku(caps)
    assert rates["280581"] == pytest.approx(570.0)  # mean of capable only
    assert rates["251200"] == pytest.approx(800.0)


def test_holding_block_payload_shape():
    dem = _demand([{"order_id": "280581-W0"}])
    prod = _produced([{"order_id": "280581-W0", "produced": 0}])
    blocks = build_holding(dem, prod, rates={"280581": 540.0})
    payload = blocks[0].to_payload()
    for key in ("id", "order_id", "sku", "qty_kg", "run_hours", "block_type"):
        assert key in payload


def test_payload_block_type_is_frontend_vocabulary():
    """Holding payloads speak the Gantt's vocabulary: "sku", never
    "production" — a production-typed card dragged onto a line dodged the
    pin gate and the KPI production checks (user report 2026-08-17)."""
    from helpers.holding_builder import HoldingBlock

    hb = HoldingBlock(id="h1", line_id=0, line_name="P09", order_id="X-W1",
                      sku="111", sku_description="", start_hour=0.0,
                      end_hour=1.0, run_hours=1.0, qty_kg=100.0)
    assert hb.to_payload()["block_type"] == "sku"
