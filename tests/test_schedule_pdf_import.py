# tests/test_schedule_pdf_import.py — parse the plant weekly schedule PDF.
#
# Golden values from the real 'WW33 2026 Production Schedule.pdf'
# (data/reference/schedule_sample_ww33.pdf): 51 blocks across lines P09-P22
# plus P00/P10 etc., week 08/09-08/15/2026, MO numbers 29xxx, hours + kg.

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.schedule_pdf_import import parse_schedule_pdf, to_calendar_rows  # noqa: E402

SAMPLE = ROOT / "data" / "reference" / "schedule_sample_ww33.pdf"


@pytest.fixture(scope="module")
def res():
    return parse_schedule_pdf(str(SAMPLE))


def test_block_count_and_year(res):
    assert res.year == 2026
    assert len(res.blocks) == 51
    assert res.warnings == []


def test_classification(res):
    by_type = {}
    for b in res.blocks:
        by_type[b.block_type] = by_type.get(b.block_type, 0) + 1
    assert by_type["production"] > 20
    assert by_type["cip"] == 14
    assert by_type["trial"] >= 4  # TRIALS + MISCON


def test_spot_values(res):
    # P09 570468 on 08/10 00:01, 20.06h, 15048.0 kg
    b = next(b for b in res.blocks if b.item == "570468" and b.line == "P09")
    assert b.hours == pytest.approx(20.06)
    assert b.qty_kg == pytest.approx(15048.0)
    assert b.date == "08/10" and b.time == "00:01"
    assert b.mo == "29901"
    # CIP on P22 08/10 07:00 8.00h
    c = next(b for b in res.blocks
             if b.block_type == "cip" and b.line == "P22")
    assert c.hours == pytest.approx(8.0)


def test_to_calendar_rows(res):
    anchor = datetime(2026, 8, 10, 0, 0)  # Monday of WW33 (08/10 is Monday)
    rows = to_calendar_rows(res, anchor)
    assert len(rows) == len(res.blocks)
    b = next(r for r in rows if r["sku"] == "570468")
    # 08/10 00:01 -> ~0.017h past Monday 00:00
    assert b["start_h"] == pytest.approx(0.0167, abs=0.01)
    assert b["end_h"] == pytest.approx(b["start_h"] + 20.06, abs=0.01)
    assert b["line_id"] == 0  # P09 -> 0
    assert b["qty_kg"] == pytest.approx(15048.0)
    # every production row has a real line_id
    assert all(r["line_id"] >= 0 for r in rows if r["block_type"] == "production")
