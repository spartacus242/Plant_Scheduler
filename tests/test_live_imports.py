# tests/test_live_imports.py — manprg merge + cip_info parsing.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.cip_import import read_cip_info  # noqa: E402
from helpers.manprg_import import read_manprg  # noqa: E402

M1 = ROOT / "data" / "reference" / "manprg.txt"
M2 = ROOT / "data" / "reference" / "manprg2.txt"
CIP = ROOT / "data" / "reference" / "cip_info.csv"


def test_manprg_merge_counts():
    res = read_manprg([M1, M2])
    assert res.rows == 70 + 34  # zero overlap union
    assert res.warnings == []


def test_manprg_current_mo_per_line():
    res = read_manprg([M1, M2])
    # spot checks from the probe
    assert res.current["P09"].mo == "29891"
    assert res.current["P09"].completion_pct == pytest.approx(75.6, abs=0.2)
    assert res.current["P14"].completion_pct == pytest.approx(100.0, abs=0.1)
    assert res.current["P17"].item == "280480"
    assert res.current["P17"].completion_pct == pytest.approx(73.7, abs=0.2)
    # manprg2 lines present too
    assert "P19" in res.current or "P20" in res.current
    assert res.current["P21"].mo == "29818"  # latest made>0 on P21


def test_manprg_by_mo():
    res = read_manprg([M1, M2])
    assert "29891" in res.by_mo
    assert res.by_mo["29891"].left_cas == pytest.approx(313)


def test_cip_info():
    res = read_cip_info(CIP)
    assert len(res.by_line) == 14
    p09 = res.by_line["P09"]
    assert p09.previous_cip is None  # NULL
    assert p09.max_hours_between == 120
    p10 = res.by_line["P10"]
    assert p10.previous_cip is not None
    assert p10.max_hours_between == 144
    assert "anti-static" in p10.notes
    # scheduled CIP parsed
    p14 = res.by_line["P14"]
    assert p14.scheduled_cip is not None
    assert p14.max_hours_between == 120
