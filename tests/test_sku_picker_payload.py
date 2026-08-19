# tests/test_sku_picker_payload.py — blank-space SKU picker payload builders.
#
# The Plant Calendar page feeds the picker two lean payloads built in
# helpers/calendar_io: lineCapableSkus (demand-plan SKUs each line can run,
# capable==1) and coFlags ("FROM|TO" -> changeover-type bitmask, bit i =
# CO_FLAG_COLUMNS[i], zero masks omitted, both sides restricted to the demand
# plan). The frontend mirror of the bit order lives in utils/skuPicker.ts
# CO_FLAG_BITS — change both or neither.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.calendar_io import (  # noqa: E402
    CO_FLAG_COLUMNS,
    build_co_flags,
    build_line_capable_skus,
)

DEMAND_SKUS = {"A", "B", "C"}


def _co_row(frm, to, setup=1, **flags):
    row = {"from_sku": frm, "to_sku": to, "setup_hours": setup,
           "added_flavors": 0}
    for col in CO_FLAG_COLUMNS:
        row[col] = flags.get(col, 0)
    return row


@pytest.fixture()
def co_csv(tmp_path):
    rows = [
        _co_row("A", "B", ttp_change=1),                     # bit 0 -> 1
        _co_row("B", "A", ffs_change=1, topload_change=1),   # bits 1+2 -> 6
        _co_row("A", "C", **{c: 1 for c in CO_FLAG_COLUMNS}),  # all six -> 63
        _co_row("C", "A"),                                   # zero mask: omitted
        _co_row("A", "X", ttp_change=1),                     # X not in demand: dropped
        _co_row("X", "B", ttp_change=1),
    ]
    p = tmp_path / "changeovers.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def test_co_flags_bitmask(co_csv):
    flags = build_co_flags(co_csv, DEMAND_SKUS)
    assert flags == {"A|B": 1, "B|A": 6, "A|C": 63}


def test_co_flags_missing_file_or_empty_demand(tmp_path, co_csv):
    assert build_co_flags(tmp_path / "nope.csv", DEMAND_SKUS) == {}
    assert build_co_flags(co_csv, set()) == {}


@pytest.fixture()
def caps_csv(tmp_path):
    rows = [
        {"line_id": 0, "sku": "A", "line_name": "P09", "capable": 1,
         "calc_rate_kgph": 716.0},
        {"line_id": 0, "sku": "X", "line_name": "P09", "capable": 1,
         "calc_rate_kgph": 500.0},   # not in demand: dropped
        {"line_id": 1, "sku": "A", "line_name": "P10", "capable": 0,
         "calc_rate_kgph": 635.0},   # not capable: dropped
        {"line_id": 1, "sku": "B", "line_name": "P10", "capable": 1,
         "calc_rate_kgph": None},    # capable, unknown rate -> 0.0
    ]
    p = tmp_path / "capabilities_rates.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def test_line_capable_skus(caps_csv):
    out = build_line_capable_skus(caps_csv, DEMAND_SKUS)
    assert out == {
        "P09": [{"sku": "A", "rate": 716.0}],
        "P10": [{"sku": "B", "rate": 0.0}],
    }


def test_line_capable_skus_legacy_rate_column(tmp_path):
    p = tmp_path / "caps.csv"
    pd.DataFrame([
        {"line_id": 0, "sku": "A", "line_name": "P09", "capable": 1,
         "rate_kgph": 100.0},
    ]).to_csv(p, index=False)
    assert build_line_capable_skus(p, {"A"}) == {
        "P09": [{"sku": "A", "rate": 100.0}]}


# ---------------------------------------------------------------------------
# Size guard on the real reference data: the payload rides in every Gantt
# mount, so it must stay lean (well under the ~300KB rethink threshold).
# ---------------------------------------------------------------------------

REF = ROOT / "data" / "reference"


@pytest.mark.skipif(
    not (REF / "changeovers.csv").exists()
    or not (REF / "demand_plan.csv").exists(),
    reason="reference data not present",
)
def test_real_payload_size_is_lean():
    dem = pd.read_csv(REF / "demand_plan.csv", dtype={"sku": str})
    skus = set(dem["sku"].astype(str))
    flags = build_co_flags(REF / "changeovers.csv", skus)
    assert len(json.dumps(flags, separators=(",", ":"))) < 300_000
    if (REF / "capabilities_rates.csv").exists():
        caps = build_line_capable_skus(REF / "capabilities_rates.csv", skus)
        assert len(json.dumps(caps, separators=(",", ":"))) < 50_000
