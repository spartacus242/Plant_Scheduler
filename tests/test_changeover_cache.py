# tests/test_changeover_cache.py — cached + family-aware changeover loader.

from __future__ import annotations

import math
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys_path = str(ROOT / "code")
sys.path.insert(0, sys_path)
sys.path.insert(0, str(ROOT / "code" / "solver"))

from changeover_cache import (  # noqa: E402
    build_sku_families,
    compress_machine_changes,
    load_changeover_dicts,
    load_changeover_setup_nested,
)
from data_loader import Data, Files, Params  # noqa: E402

REF = ROOT / "data" / "reference"


def _stage_workdir(tmp_path: Path) -> Path:
    """Copy the real reference inputs into an isolated work dir."""
    for f in REF.iterdir():
        if f.is_file():
            shutil.copy(f, tmp_path / f.name)
    return tmp_path


def _direct_dicts(path: Path):
    """Replicate the legacy inline build (pre-cache) for an equivalence check."""
    chg = pd.read_csv(path)
    chg["from_sku"] = chg["from_sku"].astype(str)
    chg["to_sku"] = chg["to_sku"].astype(str)
    chg["setup_rounded"] = chg["setup_hours"].apply(
        lambda x: int(math.floor(float(x) + 0.5))
    )
    setup, mc, ctype = {}, {}, {}
    for _, r in chg.iterrows():
        pair = (str(r["from_sku"]), str(r["to_sku"]))
        setup[pair] = int(r["setup_rounded"])
        m = {
            "ttp": int(r["ttp_change"]),
            "ffs": int(r["ffs_change"]),
            "topload": int(r["topload_change"]),
            "casepacker": int(r["casepacker_change"]),
            "conv_to_org": int(r["conv_to_org_change"]),
            "cinn_to_non": int(r["cinn_to_non"]),
            "added_flavors": int(r["added_flavors"]),
        }
        mc[pair] = m
        ctype[pair] = f"{m['ttp']}-{m['ffs']}-{m['topload']}-{m['casepacker']}"
    return setup, mc, ctype


def test_load_changeover_dicts_matches_legacy(tmp_path):
    wd = _stage_workdir(tmp_path)
    got = load_changeover_dicts(wd / "changeovers.csv")
    expected = _direct_dicts(wd / "changeovers.csv")
    assert got[0] == expected[0]
    assert got[1] == expected[1]
    assert got[2] == expected[2]
    assert len(got[0]) == 44310  # full dense matrix preserved


def test_nested_matches_legacy(tmp_path):
    wd = _stage_workdir(tmp_path)
    nested = load_changeover_setup_nested(wd / "changeovers.csv")
    chg = pd.read_csv(wd / "changeovers.csv")
    for _, r in chg.iterrows():
        assert nested[str(r["from_sku"])][str(r["to_sku"])] == float(
            int(math.floor(float(r["setup_hours"]) + 0.5))
        )


def test_in_process_memoization(tmp_path):
    wd = _stage_workdir(tmp_path)
    p = wd / "changeovers.csv"
    a = load_changeover_dicts(p)
    b = load_changeover_dicts(p)
    assert a is b  # same object -> parse not repeated in-process
    x = load_changeover_setup_nested(p)
    y = load_changeover_setup_nested(p)
    assert x is y


def test_parquet_cache_written_and_reused(tmp_path):
    pytest.importorskip("pyarrow")
    from changeover_cache import _cache_path

    wd = _stage_workdir(tmp_path)
    p = wd / "changeovers.csv"
    mtime_ns = p.stat().st_mtime_ns
    cp = _cache_path(p, mtime_ns)
    load_changeover_dicts(p)
    assert cp.exists(), "parquet cache should be written on first parse"
    cached = pd.read_parquet(cp)
    assert len(cached) == 44310


def test_build_sku_families_and_compression():
    si = pd.DataFrame(
        {
            "sku": ["A", "B", "C", "D"],
            "ediact_sku_format": ["x", "x", "y", "y"],
            "recipe": ["r1", "r1", "r2", "r2"],
            "casepacker_format": ["c1", "c1", "c1", "c2"],
            "topload_format": ["t1", "t1", "t2", "t2"],
            "pouch_format": ["p1", "p1", "p1", "p1"],
            "is_organic": [0, 0, 1, 1],
            "flavor_count": [1, 1, 2, 2],
            "has_cinnamon": [0, 0, 0, 0],
        }
    )
    fam = build_sku_families(si)
    assert fam["A"] == fam["B"]  # same format attributes -> same family
    assert fam["C"] != fam["A"]  # different format -> different family

    mc = {
        ("A", "B"): {"ttp": 1, "ffs": 0, "topload": 1, "casepacker": 0,
                     "conv_to_org": 0, "cinn_to_non": 0, "added_flavors": 1},
        ("A", "C"): {"ttp": 1, "ffs": 0, "topload": 1, "casepacker": 1,
                     "conv_to_org": 0, "cinn_to_non": 0, "added_flavors": 2},
    }
    compressed = compress_machine_changes(mc, fam)
    # within-family (A->B): flags zeroed
    assert compressed[("A", "B")]["topload"] == 0
    assert compressed[("A", "B")]["added_flavors"] == 0
    assert compressed[("A", "B")]["ttp"] == 0
    # between-family (A->C): preserved
    assert compressed[("A", "C")] == mc[("A", "C")]


def test_data_loader_uses_cache_and_preserves_shape(tmp_path):
    wd = _stage_workdir(tmp_path)
    F = Files(wd)
    P = Params()
    P.planning_start_date = "2026-08-10 00:00:00"
    d = Data(P, F)
    d.load()
    expected = load_changeover_dicts(wd / "changeovers.csv")
    assert d.setup == expected[0]
    assert d.machine_changes == expected[1]
    assert d.changeover_type == expected[2]
    # families derived from sku_info and stored on Data
    assert d.sku_family
    assert "280121" in d.sku_family
