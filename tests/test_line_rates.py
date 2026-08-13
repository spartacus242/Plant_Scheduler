# tests/test_line_rates.py — flat per-line rates (P2 Dispatch 1, charter §6.1-6.3).

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code" / "solver"))

from data_loader import Data, Files, Params  # noqa: E402
from helpers.scorecard_engine import _load_line_avg_rates  # noqa: E402

REF = ROOT / "data" / "reference"


def _stage_workdir(tmp_path: Path) -> Path:
    """Copy the real reference inputs into an isolated work dir."""
    for f in REF.iterdir():
        if f.is_file():
            shutil.copy(f, tmp_path / f.name)
    return tmp_path


def _load(wd: Path) -> Data:
    F = Files(wd)
    P = Params()
    P.planning_start_date = "2026-08-10 00:00:00"
    P.use_sku_rates = False
    d = Data(P, F)
    d.load()
    return d


def test_flat_line_rates_without_month_no_keyerror_and_overrides(tmp_path):
    wd = _stage_workdir(tmp_path)
    pd.DataFrame([
        {"line_id": 0, "Line": "P09", "rate_kgph": 737},
        {"line_id": 1, "Line": "P10", "rate_kgph": 702},
    ]).to_csv(wd / "line_rates.csv", index=False)

    d = _load(wd)  # must not raise KeyError on the Month-less file

    for (lid, sku), rate in d.rate.items():
        if lid == 0:
            assert rate == 737.0
        elif lid == 1:
            assert rate == 702.0


def test_line_rates_with_month_filters_to_planning_month(tmp_path):
    wd = _stage_workdir(tmp_path)
    pd.DataFrame([
        {"line_id": 0, "Line": "P09", "rate_kgph": 100, "Month": 7},
        {"line_id": 0, "Line": "P09", "rate_kgph": 900, "Month": 8},
    ]).to_csv(wd / "line_rates.csv", index=False)

    d = _load(wd)  # planning_start_date is August -> plan_month == 8

    for (lid, sku), rate in d.rate.items():
        if lid == 0:
            assert rate == 900.0


def test_scorecard_load_line_avg_rates_uses_flat_file_when_sku_rates_off(tmp_path, monkeypatch):
    ref = tmp_path / "reference"
    ref.mkdir()
    pd.DataFrame([
        {"line_id": 0, "Line": "P09", "rate_kgph": 737},
        {"line_id": 1, "Line": "P10", "rate_kgph": 702},
    ]).to_csv(ref / "line_rates.csv", index=False)

    monkeypatch.setattr("helpers.scorecard_engine.load_toml", lambda: {"scheduler": {"use_sku_rates": False}})

    rates = _load_line_avg_rates(ref)
    assert rates["P09"] == 737.0
    assert rates["P10"] == 702.0
