# tests/test_current_mo.py — current-state MO solver input + change table.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code" / "solver"))

from data_loader import Data, Files, Params  # noqa: E402
from phase2_scheduler import write_mo_changes  # noqa: E402

P = Params()
P.planning_start_date = "2026-08-10 00:00:00"


def _files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def _write_caps(tmp_path: Path) -> None:
    rows = []
    for lid, name in [(0, "P09"), (1, "P10")]:
        for sku in ("280581", "280450"):
            rows.append({"line_id": lid, "sku": sku, "line_name": name,
                         "capable": 1, "calc_rate_kgph": 540.0})
    pd.DataFrame(rows).to_csv(tmp_path / "capabilities_rates.csv", index=False)


def _write_required(tmp_path: Path) -> None:
    """Write the CSVs Data.load() reads unconditionally."""
    _write_caps(tmp_path)
    pd.DataFrame(
        columns=["from_sku", "to_sku", "setup_hours", "setup_rounded",
                 "ttp_change", "ffs_change", "topload_change",
                 "casepacker_change", "conv_to_org_change", "cinn_to_non",
                 "added_flavors"]
    ).to_csv(tmp_path / "changeovers.csv", index=False)
    pd.DataFrame(
        columns=["line_id", "line_name", "initial_sku", "available_from_hour",
                 "long_shutdown_flag", "long_shutdown_extra_setup_hours",
                 "carryover_run_hours_since_last_cip_at_t0",
                 "last_cip_end_datetime", "comment"]
    ).to_csv(tmp_path / "initial_states.csv", index=False)
    pd.DataFrame(
        columns=["order_id", "sku", "week_index", "qty_target", "lower_pct",
                 "upper_pct", "due_start_hour", "due_end_hour", "priority"]
    ).to_csv(tmp_path / "demand_plan.csv", index=False)


def _write_cmo(tmp_path: Path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_csv(tmp_path / "current_mo.csv", index=False)


def _write_empty_demand(tmp_path: Path) -> None:
    pass  # _write_required covers demand_plan.csv


def _make_data(tmp_path: Path) -> Data:
    _write_required(tmp_path)
    d = Data(P, _files(tmp_path))
    d.load()
    return d


def test_current_mo_parses_locked_orders(tmp_path):
    _write_cmo(tmp_path, [{
        "mo": "29901", "line_name": "P09", "sku": "280581",
        "remaining_kg": 15200, "due_start_h": 0, "due_end_h": 335,
        "locked_line": 1, "source": "manprg",
    }])
    d = _make_data(tmp_path)
    mos = [o for o in d.orders if o.get("is_current_mo")]
    assert len(mos) == 1
    o = mos[0]
    assert o["order_id"] == "29901|CUR"
    assert o["locked_line"] == 0          # P09 -> line_id 0
    assert o["qty_min"] == 15200 and o["qty_max"] == 15200
    assert o["priority"] == 0


def test_current_mo_skips_zero_remaining(tmp_path):
    _write_cmo(tmp_path, [{
        "mo": "29901", "line_name": "P09", "sku": "280581",
        "remaining_kg": 0, "due_start_h": 0, "due_end_h": 335,
        "locked_line": 1, "source": "manprg",
    }])
    d = _make_data(tmp_path)
    assert [o for o in d.orders if o.get("is_current_mo")] == []


def test_current_mo_unknown_line_raises(tmp_path):
    _write_cmo(tmp_path, [{
        "mo": "X", "line_name": "P99", "sku": "280581",
        "remaining_kg": 100, "due_start_h": 0, "due_end_h": 335,
        "locked_line": 1, "source": "manprg",
    }])
    with pytest.raises(ValueError, match="P99"):
        _make_data(tmp_path)


def test_mo_changes_reports_trim_split_reorder(tmp_path):
    d = _make_data(tmp_path)
    d.orders.append({
        "order_id": "29901|CUR", "sku": "280581", "due_start": 10,
        "due_end": 300, "qty_min": 10000, "qty_max": 10000,
        "priority": 0, "is_current_mo": True, "mo_id": "29901",
        "locked_line": 0, "source": "manprg",
    })
    schedule = [
        {"line_id": 0, "line_name": "P09", "order_id": "29901|CUR",
         "sku": "280581", "start_hour": 20, "end_hour": 30, "run_hours": 10},
        {"line_id": 0, "line_name": "P09", "order_id": "29901|CUR",
         "sku": "280581", "start_hour": 40, "end_hour": 45, "run_hours": 5},
    ]
    bounds = [{"order_id": "29901|CUR", "sku": "280581",
               "qty_min": 10000, "qty_max": 10000, "produced": 8500,
               "in_bounds": True}]
    write_mo_changes(tmp_path, d, schedule, bounds)
    out = pd.read_csv(tmp_path / "mo_changes.csv", dtype={"mo": str})
    assert len(out) == 1
    row = out.iloc[0]
    assert row["mo"] == "29901"
    assert row["orig_qty_kg"] == 10000 and row["new_qty_kg"] == 8500
    assert row["delta_kg"] == -1500
    assert row["split_count"] == 2
    assert "tonnage_trim" in row["reason"]
    assert "split" in row["reason"]
    assert "reordered" in row["reason"]  # orig start 10 -> planned 20


def test_mo_changes_unmoved(tmp_path):
    d = _make_data(tmp_path)
    d.orders.append({
        "order_id": "29901|CUR", "sku": "280581", "due_start": 5,
        "due_end": 300, "qty_min": 5000, "qty_max": 5000,
        "priority": 0, "is_current_mo": True, "mo_id": "29901",
        "locked_line": 0, "source": "manprg",
    })
    schedule = [
        {"line_id": 0, "line_name": "P09", "order_id": "29901|CUR",
         "sku": "280581", "start_hour": 5, "end_hour": 15, "run_hours": 10},
    ]
    bounds = [{"order_id": "29901|CUR", "sku": "280581",
               "qty_min": 5000, "qty_max": 5000, "produced": 5000,
               "in_bounds": True}]
    write_mo_changes(tmp_path, d, schedule, bounds)
    out = pd.read_csv(tmp_path / "mo_changes.csv")
    assert out.iloc[0]["reason"] == "unmoved"


def test_mo_changes_writes_header_when_no_mos(tmp_path):
    d = _make_data(tmp_path)
    write_mo_changes(tmp_path, d, [], [])
    out = pd.read_csv(tmp_path / "mo_changes.csv")
    assert list(out.columns) == ["mo", "line_name", "sku", "source",
                                 "orig_qty_kg", "new_qty_kg", "delta_kg",
                                 "orig_start_h", "new_start_h", "new_end_h",
                                 "split_count", "reason"]
    assert len(out) == 0
