# tests/test_capability_check.py — manprg vs capabilities table validation.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.capability_check import (  # noqa: E402
    CapabilityConflict,
    average_rate_for_line,
    check_capabilities,
    fix_rows_for,
    load_capabilities,
    load_manprg_mos,
)


def _caps(rows: list[tuple[str, str, int, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["sku", "line_name", "capable", "calc_rate_kgph"])


CAPS = _caps([
    ("280581", "P09", 1, 540.0),
    ("280581", "P10", 0, 540.0),
    ("120448", "P09", 1, 540.0),
    ("120448", "P10", 1, 600.0),
    ("120448", "P11", 0, 540.0),
])


def test_no_conflicts_when_everything_matches():
    mos = [("280581", "P09", "A"), ("120448", "P10", "B")]
    res = check_capabilities(CAPS, mos)
    assert res.count == 0


def test_sku_missing_detected():
    mos = [("251200", "P22", "C")]
    res = check_capabilities(CAPS, mos)
    assert res.count == 1
    c = res.conflicts[0]
    assert c.kind == "SKU_MISSING" and c.sku == "251200" and c.line_name == "P22"


def test_line_not_capable_detected():
    mos = [("280581", "P10", "A")]  # P10 has 280581 but capable=0
    res = check_capabilities(CAPS, mos)
    assert res.count == 1
    c = res.conflicts[0]
    assert c.kind == "LINE_NOT_CAPABLE"
    assert "capable lines" in c.detail


def test_cip_and_trials_rows_are_ignored():
    mos = [("CIP", "P09", "M1"), ("TRIALS", "P10", "M2"),
           ("280581", "P09", "A")]
    res = check_capabilities(CAPS, mos)
    assert res.count == 0


def test_duplicate_mos_are_deduplicated():
    mos = [("280581", "P09", "A"), ("280581", "P09", "A")]
    res = check_capabilities(CAPS, mos)
    assert res.count == 0


def test_manprg_line_prefix_is_stripped():
    mos = [("280581", "LMH-P09", "A")]
    res = check_capabilities(CAPS, mos)
    assert res.count == 0


def test_real_manprg_reports_four_known_conflicts():
    ref = ROOT / "data" / "reference"
    if not (ref / "manprg.txt").exists():
        pytest.skip("no real manprg export")
    caps = load_capabilities(ref / "capabilities_rates.csv")
    mos = load_manprg_mos([ref / "manprg.txt", ref / "manprg2.txt"])
    res = check_capabilities(caps, mos)
    kinds = sorted(c.kind for c in res.conflicts)
    assert kinds == ["LINE_NOT_CAPABLE"] * 4
    pairs = sorted((c.sku, c.line_name) for c in res.conflicts)
    assert ("251200", "P22") in pairs
    assert ("280612", "P12") in pairs
    assert ("280611", "P12") in pairs
    assert ("280614", "P12") in pairs


def test_fix_rows_adds_full_line_block_for_missing_sku():
    conflicts = [CapabilityConflict("251200", "P22", "C", "SKU_MISSING")]
    fixed = fix_rows_for(CAPS, conflicts, default_rate=650.0)
    # P22 is not in the fixture's table, so it is appended; the other
    # present lines (P09/P10/P11) also get rows with capable=0.
    assert len(fixed) == 4
    p22 = fixed[fixed["line_name"] == "P22"].iloc[0]
    assert p22["capable"] == 1 and p22["calc_rate_kgph"] == 650.0
    others = fixed[fixed["line_name"] != "P22"]
    assert (others["capable"] == 0).all()
    assert (others["calc_rate_kgph"] == 0).all()


def test_fix_rows_flips_existing_row_to_capable_keeping_rate():
    conflicts = [CapabilityConflict("280581", "P10", "A", "LINE_NOT_CAPABLE")]
    fixed = fix_rows_for(CAPS, conflicts, default_rate=999.0)
    assert len(fixed) == 1
    row = fixed.iloc[0]
    assert row["line_name"] == "P10" and row["capable"] == 1
    assert row["calc_rate_kgph"] == 540.0, "keeps the existing rate, not default"


def test_fix_rows_default_rate_fills_zero_rate_existing():
    caps = _caps([("280581", "P09", 1, 540.0),
                  ("280581", "P10", 0, 0.0)])
    conflicts = [CapabilityConflict("280581", "P10", "A", "LINE_NOT_CAPABLE")]
    fixed = fix_rows_for(caps, conflicts, default_rate=700.0)
    assert fixed.iloc[0]["calc_rate_kgph"] == 700.0


def test_average_rate_for_line():
    assert average_rate_for_line(CAPS, "P10") == 600.0  # only capable row
    assert average_rate_for_line(CAPS, "P11") == 0.0    # nothing capable
    assert average_rate_for_line(CAPS, "P09") == 540.0
