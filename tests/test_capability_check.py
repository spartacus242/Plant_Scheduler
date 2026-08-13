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
    check_demand_capabilities,
    fix_demand_rows,
    fix_rows_for,
    load_capabilities,
    load_manprg_mos,
    load_plan_evidence,
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
    """Live-manprg regression: the 4 known conflicts were resolved by the
    one-click capability fix (2026-08-10, browser-verified). The real-data
    check must now pass clean — a conflict here means the table regressed."""
    ref = ROOT / "data" / "test_fixtures" / "live_2026-08-13"  # pinned snapshot, not live data
    if not (ref / "manprg.txt").exists():
        pytest.skip("no real manprg export")
    caps = load_capabilities(ref / "capabilities_rates.csv")
    mos = load_manprg_mos([ref / "manprg.txt", ref / "manprg2.txt"])
    res = check_capabilities(caps, mos)
    assert res.count == 0, f"expected clean after fix, got: {res.conflicts}"


def test_real_manprg_pairs_are_capable_after_fix():
    """The specific pairs the fix flipped must be capable=1 in the table."""
    ref = ROOT / "data" / "test_fixtures" / "live_2026-08-13"  # pinned snapshot, not live data
    if not (ref / "manprg.txt").exists():
        pytest.skip("no real manprg export")
    caps = load_capabilities(ref / "capabilities_rates.csv")
    for sku, line in [("251200", "P22"), ("280612", "P12"),
                      ("280611", "P12"), ("280614", "P12")]:
        row = caps[(caps["sku"] == sku) & (caps["line_name"] == line)]
        assert not row.empty, f"{sku}@{line} missing"
        assert int(row.iloc[0]["capable"]) == 1, f"{sku}@{line} not capable"
        assert float(row.iloc[0]["calc_rate_kgph"]) > 0, f"{sku}@{line} rate 0"


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


# ── demand coverage check (SKUs with no capable line anywhere) ────────────

DEMAND = pd.DataFrame([
    {"sku": "280581", "qty_target": 5000},   # capable on P09 -> fine
    {"sku": "999999", "qty_target": 12550},  # absent from table -> flagged
    {"sku": "570560", "qty_target": 35160},  # present but capable=0 everywhere
    {"sku": "120448", "qty_target": 1000},   # capable on P09/P10 -> fine
])


def test_demand_check_flags_skus_without_capable_line():
    res = check_demand_capabilities(CAPS, DEMAND)
    kinds = [c.kind for c in res.conflicts]
    skus = [c.sku for c in res.conflicts]
    assert kinds == ["DEMAND_NO_CAPABLE_LINE", "DEMAND_NO_CAPABLE_LINE"]
    assert set(skus) == {"999999", "570560"}
    # detail carries the unschedulable kg
    d = {c.sku: c.detail for c in res.conflicts}
    assert "12,550 kg" in d["999999"]


def test_demand_check_requires_rate_above_zero():
    # capable=1 but rate=0 must still be flagged (solver can't place it)
    caps = _caps([("999999", "P09", 1, 0.0)])
    res = check_demand_capabilities(caps, DEMAND[DEMAND["sku"] == "999999"])
    assert res.count == 1
    assert res.conflicts[0].kind == "DEMAND_NO_CAPABLE_LINE"


def test_demand_check_empty_demand():
    res = check_demand_capabilities(CAPS, pd.DataFrame(columns=["sku", "qty_target"]))
    assert res.count == 0


def test_fix_demand_rows_appends_evidence_line_only():
    conflicts = [CapabilityConflict(sku="570560", line_name="—", mo="",
                                    kind="DEMAND_NO_CAPABLE_LINE",
                                    detail="35,160 kg")]
    changed, unfixable = fix_demand_rows(
        CAPS, conflicts, evidence={"570560": {"P16"}}, default_rate=0.0)
    assert unfixable == []
    # full block: existing table lines + the evidence line
    assert len(changed) == len(set(CAPS["line_name"]) | {"P16"})
    p16 = changed[changed["line_name"] == "P16"].iloc[0]
    assert p16["capable"] == 1 and p16["calc_rate_kgph"] == 0.0
    # other lines capable=0
    other = changed[changed["line_name"] != "P16"]
    assert (other["capable"].astype(int) == 0).all()


def test_fix_demand_rows_unfixable_without_evidence():
    conflicts = [CapabilityConflict(sku="999999", line_name="—", mo="",
                                    kind="DEMAND_NO_CAPABLE_LINE",
                                    detail="12,550 kg")]
    changed, unfixable = fix_demand_rows(
        CAPS, conflicts, evidence={}, default_rate=0.0)
    assert changed.empty
    assert unfixable == ["999999"]


def test_load_plan_evidence(tmp_path):
    p = tmp_path / "sku_plan_evidence.csv"
    p.write_text("sku,line_name,source,mo\n570560,P16,Week32,29803\n"
                 "570560,P16,WW31,29734\n280698,P17,WW30,1\n",
                 encoding="utf-8")
    ev = load_plan_evidence(p)
    assert ev["570560"] == {"P16"}
    assert ev["280698"] == {"P17"}
    assert load_plan_evidence(tmp_path / "missing.csv") == {}


def test_merge_fixes_flips_existing_and_appends_new():
    from helpers.capability_check import merge_fixes
    caps = pd.DataFrame([
        {"line_id": 0, "sku": "280581", "line_name": "P09", "capable": 1, "calc_rate_kgph": 540.0},
        {"line_id": 1, "sku": "280581", "line_name": "P10", "capable": 0, "calc_rate_kgph": 540.0},
        {"line_id": 7, "sku": "120448", "line_name": "P16", "capable": 0, "calc_rate_kgph": 540.0},
    ])
    changed = pd.DataFrame([
        # existing pair: flip 280581@P10 capable (rate kept)
        {"sku": "280581", "line_name": "P10", "capable": 1, "calc_rate_kgph": 540.0},
        # new pair: append 570560@P16 (the demand-evidence case)
        {"sku": "570560", "line_name": "P16", "capable": 1, "calc_rate_kgph": 597.9},
    ])
    out = merge_fixes(caps, changed)
    assert len(out) == len(caps) + 1  # 1 appended, 1 flipped
    flipped = out[(out["sku"] == "280581") & (out["line_name"] == "P10")].iloc[0]
    assert flipped["capable"] == 1 and flipped["calc_rate_kgph"] == 540.0
    appended = out[(out["sku"] == "570560") & (out["line_name"] == "P16")].iloc[0]
    assert appended["capable"] == 1 and appended["calc_rate_kgph"] == 597.9
    assert appended["line_id"] == 7  # P16 -> line_id 7 (derived from table)


def test_merge_fixes_line_id_defaults_to_99_for_unknown_line():
    from helpers.capability_check import merge_fixes
    caps = pd.DataFrame([
        {"line_id": 0, "sku": "280581", "line_name": "P09", "capable": 1, "calc_rate_kgph": 540.0},
        {"line_id": 1, "sku": "280581", "line_name": "P10", "capable": 0, "calc_rate_kgph": 540.0},
    ])  # no P16 in table -> line_id not derivable
    changed = pd.DataFrame([
        {"sku": "570560", "line_name": "P16", "capable": 1, "calc_rate_kgph": 540.0},
    ])
    out = merge_fixes(caps, changed)
    appended = out[(out["sku"] == "570560") & (out["line_name"] == "P16")].iloc[0]
    assert appended["line_id"] == 99  # not derivable -> unknown marker
