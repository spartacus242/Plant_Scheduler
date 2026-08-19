# tests/test_weekly_breakdown.py — weekly fulfillment credit is CAPPED.
#
# Finding 7 (audit 2026-08-18): weekly_breakdown credited each order with its
# full assigned kg, so overproducing one order raised the whole week's
# fulfilled_pct — excess on order A cannot serve order B's demand. The credit
# now caps at each order's own target (the min/max midpoint, the same 100%
# point compute_adherence divides by). kpi.ts weekFulfillmentCredit applies
# the identical cap client-side (tests/test_kpi_parity.py pins that side).

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "code") not in sys.path:
    sys.path.insert(0, str(ROOT / "code"))

from helpers.calendar_io import CALENDAR_COLUMNS  # noqa: E402
from helpers.config import scorecard_config  # noqa: E402
from helpers.horizon import Horizon  # noqa: E402
from helpers.scorecard_engine import weekly_breakdown  # noqa: E402

ANCHOR = datetime(2026, 8, 17)  # Monday, ISO W34


def _fixed_horizon(monkeypatch):
    hz = Horizon(
        anchor=ANCHOR,
        start=ANCHOR,
        end=ANCHOR + timedelta(hours=504),
        now=ANCHOR,
        mode="fixed",
        hours=504,
        config_anchor=ANCHOR,
    )
    monkeypatch.setattr("helpers.horizon.resolve", lambda *a, **k: hz)


def _block(bid, start, end, sku, order_id, qty_kg):
    return {
        "block_id": bid,
        "block_type": "production",
        "line_id": 1,
        "line_name": "P10",
        "start_h": float(start),
        "end_h": float(end),
        "label": sku,
        "order_id": order_id,
        "sku": sku,
        "sku_description": "",
        "qty_kg": float(qty_kg),
        "locked": False,
        "attrs": "",
    }


def test_week_fulfillment_caps_credit_at_order_target(monkeypatch, tmp_path):
    """One order overproduced (120% of target), one underproduced: the week
    must read (target + assigned_under) / target_sum, NOT
    (assigned_over + assigned_under) / target_sum."""
    _fixed_horizon(monkeypatch)
    ref = tmp_path / "reference"
    ref.mkdir(parents=True)
    # Two orders due in the anchor week, target 1000 each (band 900-1100).
    (ref / "demand_plan.csv").write_text(
        "order_id,sku,week_index,qty_target,lower_pct,upper_pct,"
        "due_start_hour,due_end_hour\n"
        "O1-W0,SKU_A,0,1000,0.9,1.1,0,168\n"
        "O2-W0,SKU_B,0,1000,0.9,1.1,0,168\n",
        encoding="utf-8",
    )

    cal = pd.DataFrame(
        [
            _block("b1", 0, 12, "SKU_A", "O1-W0", 1200.0),  # 120% of target
            _block("b2", 20, 26, "SKU_B", "O2-W0", 600.0),  # 60% of target
        ],
        columns=CALENDAR_COLUMNS,
    )

    df = weekly_breakdown(cal, data_dir=tmp_path, cfg=scorecard_config({}))
    wk = df[df["week"] == "W34"].iloc[0]

    assert wk["demand_kg"] == 2000
    # O1 credits min(1200, 1000) = 1000; O2 credits 600.
    assert wk["scheduled_kg"] == 1600
    assert wk["fulfilled_pct"] == 80.0  # NOT (1200+600)/2000 = 90.0
    # prod_kg stays the physical truth — only the fulfillment credit is capped
    assert wk["prod_kg"] == 1800
