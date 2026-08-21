# tests/test_stockcheck_weeks.py — demand week_index <-> ISO week mapping.
#
# The Stock Check page once labeled week_index with timefmt's DEFAULT_ANCHOR
# fallback (2026-02-15 -> WW07 for a WW34 plan). These tests pin the real
# contract: labels come from the demand anchor, ordering from
# (iso_year, iso_week) — never the bare week number.

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.timefmt import week_index_to_iso  # noqa: E402
from stockcheck.weeks import (current_week_options, demand_anchor,  # noqa: E402
                              demand_week_indices, week_index_iso_parts,
                              week_index_label)

AUG_ANCHOR = "2026-08-17 00:00:00"   # Monday, ISO (2026, 34)
DEC_ANCHOR = "2026-12-14 00:00:00"   # Monday, ISO (2026, 51); 2026 has W53


def test_label_counts_from_demand_anchor():
    assert week_index_label(0, AUG_ANCHOR) == "WW34"
    assert week_index_label(1, AUG_ANCHOR) == "WW35"
    assert week_index_label(6, AUG_ANCHOR) == "WW40"


def test_label_matches_timefmt_convention():
    for k in range(7):
        assert (week_index_iso_parts(k, AUG_ANCHOR)[1]
                == week_index_to_iso(k, AUG_ANCHOR))


def test_label_wraps_at_year_end():
    # W51, W52, W53, then next year's W01/W02
    assert [week_index_label(k, DEC_ANCHOR) for k in range(5)] == [
        "WW51", "WW52", "WW53", "WW01", "WW02"]


def test_iso_parts_order_across_year_boundary():
    parts = [week_index_iso_parts(k, DEC_ANCHOR) for k in range(5)]
    assert parts == [(2026, 51), (2026, 52), (2026, 53), (2027, 1), (2027, 2)]
    assert parts == sorted(parts)  # bare week numbers would sort 1,2,51,52,53


def test_current_week_options_floors_at_current_iso_week():
    today = date(2026, 8, 21)  # ISO (2026, 34)
    assert current_week_options([0, 1, 2], AUG_ANCHOR, today) == [0, 1, 2]
    later = date(2026, 9, 2)   # ISO (2026, 36): drops WW34 and WW35
    assert current_week_options([0, 1, 2], AUG_ANCHOR, later) == [2]


def test_current_week_options_orders_across_year_wrap():
    today = date(2026, 12, 30)  # ISO (2026, 53)
    # W53 must precede next year's W01/W02; W51/W52 are past
    assert current_week_options([4, 3, 2, 1, 0], DEC_ANCHOR, today) == [2, 3, 4]


def test_demand_anchor_reads_source_json(tmp_path):
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "demand_plan.source.json").write_text(
        json.dumps({"anchor": AUG_ANCHOR, "anchor_iso_week": 34}),
        encoding="utf-8")
    assert demand_anchor(tmp_path) == datetime(2026, 8, 17)


def test_demand_anchor_fallback_is_current_monday(tmp_path):
    a = demand_anchor(tmp_path)  # no sidecar at all
    assert a.weekday() == 0
    assert a.date() <= date.today()
    assert (date.today() - a.date()).days < 7


def test_demand_week_indices_reads_plan(tmp_path):
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "demand_plan.csv").write_text(
        "order_id,sku,week_index,qty_target\n"
        "a-W1,111,1,10\nb-W0,111,0,10\nc-W2,222,2,10\nd-W2,333,2,10\n",
        encoding="utf-8")
    assert demand_week_indices(tmp_path) == [0, 1, 2]
    assert demand_week_indices(tmp_path / "nope") == []
