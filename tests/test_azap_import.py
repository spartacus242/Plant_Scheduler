# tests/test_azap_import.py — raw AZAP xlsx -> demand_plan.csv.
#
# Golden values from the real 'Demand Plan Raw.xlsx' (data/reference/
# azap_raw_sample.xlsx): 6,476 rows, P09-P22 -> 4,271 rows / 3,854 (sku,week)
# buckets over 83 weeks (2026-08-03 .. 2028-02-28); every start is a Monday,
# every span 7 days. Import anchors to 2026-08-03 (ISO week 32) and keeps
# weeks 32-34.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers import azap_import  # noqa: E402
from helpers.timefmt import week_index_to_iso, week_label  # noqa: E402

SAMPLE = ROOT / "data" / "reference" / "azap_raw_sample.xlsx"


def test_read_raw_shape():
    df = azap_import.read_raw(SAMPLE)
    assert len(df) == 6476
    assert not [c for c in azap_import.EXPECTED_COLS if c not in df.columns]


def test_read_raw_csv_matches_xlsx():
    """The CSV export (preferred) parses identically to the xlsx."""
    csv_sample = ROOT / "data" / "reference" / "azap_raw_sample.csv"
    df_csv = azap_import.read_raw(csv_sample)
    df_xlsx = azap_import.read_raw(SAMPLE)
    assert len(df_csv) == len(df_xlsx) == 6476
    assert list(df_csv.columns) == list(df_xlsx.columns)
    keep = [f"P{n:02d}" for n in range(9, 23)]
    assert (df_csv[df_csv["Machine"].isin(keep)]["Tons"].sum()
            == df_xlsx[df_xlsx["Machine"].isin(keep)]["Tons"].sum())


def test_aggregate_filters_machines():
    df = azap_import.read_raw(SAMPLE)
    agg, warns, dropped_machine, _ = azap_import.aggregate(df)
    assert dropped_machine == 6476 - 4271
    assert len(agg) == 3854
    # all week starts are Mondays in the raw file
    assert all(pd.Timestamp(d).weekday() == 0 for d in agg["week_start"])
    assert warns == []


def test_anchor_and_window():
    df = azap_import.read_raw(SAMPLE)
    agg, _, _, _ = azap_import.aggregate(df)
    demand, anchor, dropped, _weeks = azap_import.build_demand_plan(agg)
    assert anchor.isoformat() == "2026-08-03"
    assert set(demand["week_index"]) <= {0, 1, 2}
    # 83 raw weeks -> only 3 kept, so most buckets dropped
    assert dropped == 3854 - len(demand)
    # schema matches the existing demand_plan.csv contract
    assert list(demand.columns) == [
        "order_id", "sku", "week_index", "qty_target", "lower_pct",
        "upper_pct", "due_start_hour", "due_end_hour", "priority"]
    row = demand.iloc[0]
    assert row["due_end_hour"] - row["due_start_hour"] == 167
    assert row["lower_pct"] == 0.9 and row["upper_pct"] == 1.1


def test_multi_machine_bucket_sums():
    # 120436 on 2027-10-25 appears on P09+P14+P17 in the raw file -> one bucket
    df = azap_import.read_raw(SAMPLE)
    agg, _, _, _ = azap_import.aggregate(df)
    import datetime
    key = agg[(agg["sku"] == "120436")
              & (agg["week_start"] == datetime.date(2027, 10, 25))]
    assert len(key) == 1  # summed into a single bucket


def test_import_writes_csv_and_provenance(tmp_path):
    captured = {}
    res = azap_import.import_azap(SAMPLE, tmp_path,
                                  update_anchor=lambda s: captured.update(anchor=s))
    assert res.anchor.isoformat() == "2026-08-03"
    assert res.iso_weeks == [32, 33, 34]
    assert res.rows_dropped_machine == 6476 - 4271
    assert res.rows_kept == len(pd.read_csv(res.demand_csv))
    prov = json.loads(res.provenance_json.read_text())
    assert prov["anchor_monday"] == "2026-08-03"
    assert prov["iso_weeks"] == {"W0": 32, "W1": 33, "W2": 34}
    assert captured["anchor"] == "2026-08-03 00:00:00"


def test_iso_week_labels():
    anchor = "2026-08-03 00:00:00"  # Monday, ISO week 32
    assert week_index_to_iso(0, anchor) == 32
    assert week_index_to_iso(2, anchor) == 34
    assert week_label(1, anchor) == "WW33"
