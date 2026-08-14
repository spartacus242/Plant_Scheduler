# tests/test_agent_policy.py — agent input policies (user-approved
# 2026-08-14): DNS trim of the solver's work-dir demand copy.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.agent_policy import dns_ratios, trim_dns_demand  # noqa: E402


def _demand(rows):
    base = {"order_id": "X-W0", "sku": "X", "week_index": 0,
            "qty_target": 10000, "lower_pct": 0.9, "upper_pct": 1.1,
            "due_start_hour": 0, "due_end_hour": 167, "priority": 3}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_dns_ratios_takes_worst_week_and_guards_none():
    rep = {"demand_view": [
        {"sku": "111", "status": "DO_NOT_SCHEDULE", "achievable_ratio": 0.6},
        {"sku": "111", "status": "DO_NOT_SCHEDULE", "achievable_ratio": 0.2},
        {"sku": "222", "status": "DO_NOT_SCHEDULE", "achievable_ratio": None},
        {"sku": "333", "status": "OK", "achievable_ratio": 2.0},
    ]}
    r = dns_ratios(rep)
    assert r == {"111": 0.2, "222": 0.0}


def test_trim_zeroes_qmin_and_caps_qmax_at_achievable():
    dem = _demand([
        {"order_id": "111-W0", "sku": "111"},
        {"order_id": "444-W0", "sku": "444"},  # not DNS — untouched
    ])
    out, notes = trim_dns_demand(dem, {"111": 0.4})
    trimmed = out[out["order_id"] == "111-W0"].iloc[0]
    assert trimmed["lower_pct"] == 0.0
    assert trimmed["upper_pct"] == 0.4          # capped at achievable
    untouched = out[out["order_id"] == "444-W0"].iloc[0]
    assert untouched["lower_pct"] == 0.9 and untouched["upper_pct"] == 1.1
    assert len(notes) == 1 and "111-W0" in notes[0]
    assert "9,000→0" in notes[0]                 # qty_min 10000*0.9 -> 0


def test_trim_never_raises_qmax():
    # achievable 1.5 > upper 1.1: cap keeps the ORIGINAL upper bound
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    out, _ = trim_dns_demand(dem, {"111": 1.5})
    assert out.iloc[0]["upper_pct"] == 1.1


def test_trim_noop_without_dns():
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    out, notes = trim_dns_demand(dem, {})
    assert notes == [] and out.iloc[0]["lower_pct"] == 0.9
