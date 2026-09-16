# tests/test_bom_entry_activities.py — which activities BomGraph.explode
# enters a SKU through (fix of 2026-09-16).
#
# The ediact.csv export of 2026-09-15 files the alternative FG recipe
# "280351-DS" (same Output item, same inputs) under PF 280351, where the
# legacy "ediact 3.csv" kept it under its own PF. Walking every activity
# that equals the SKU OR starts with "<sku>-" at full quantity doubled all
# of 280351's requirements. Tiny synthetic frames only — no data/reference.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from stockcheck.bom import BomGraph  # noqa: E402

COLS = ["PF", "Activity", "flow_type", "effective_date", "family",
        "item_type", "item", "designation", "qty_act", "unit"]
EFF = pd.Timestamp("2026-06-26")


def _row(pf, act, family, kind, item, qty, unit):
    return {"PF": pf, "Activity": act, "flow_type": "", "effective_date": EFF,
            "family": family, "item_type": kind, "item": item,
            "designation": f"D {item}", "qty_act": qty, "unit": unit}


def _fg(pf, act, out_item):
    """An FG activity: 2,640 CAS out of 2,640 SLV of TL1 + 10 pallets (the
    SU / POU sub-activities come from _subs)."""
    return [
        _row(pf, act, "FG", "Output", out_item, 2640.0, "CAS"),
        _row(pf, act, "FG", "Input", "TL1", 2640.0, "SLV"),
        _row(pf, act, "FG", "Input", "758006", 10.0, "EA"),
    ]


def _subs(pf):
    return [
        _row(pf, "TL1", "SU", "Output", "TL1", 1000.0, "SLV"),
        _row(pf, "TL1", "SU", "Input", "754751", 1000.0, "EA"),
        _row(pf, "TL1", "SU", "Input", "VP1", 24000.0, "POU"),
        _row(pf, "VP1", "POU", "Output", "VP1", 1000.0, "POU"),
        _row(pf, "VP1", "POU", "Input", "752420", 22.88, "M2"),
    ]


def _needs(bom: BomGraph, sku: str) -> dict:
    exp = bom.explode(sku, 1.0, as_of="2026-09-16")
    assert exp.status == "OK", exp
    return {(g.primary_item, g.unit): round(g.need_qty, 9)
            for g in exp.requirements}


OLD_NEEDS = {("758006", "EA"): round(10 / 2640, 9), ("754751", "EA"): 1.0,
             ("752420", "M2"): 0.54912}


def test_exact_activity_is_the_only_entry_when_it_exists():
    """Rule (2026-09-16): an activity whose code equals the SKU is the ONLY
    entry point. A sku-prefixed variant filed under the same PF (the
    alternative recipe 280351-DS of ediact.csv) is never walked beside it —
    the per-case needs equal the legacy export's, never doubled."""
    rows = _fg("280351", "280351", "280351") + _subs("280351")
    alt = _fg("280351", "280351-DS", "280351")
    new_export = pd.DataFrame(rows + alt, columns=COLS)
    got = _needs(BomGraph(new_export), "280351")
    assert got == OLD_NEEDS                       # 752420 0.54912, not 1.09824
    # the legacy layout (variant under its own PF) explodes identically
    legacy = pd.DataFrame(
        rows + [dict(r, PF="280351-DS") for r in alt]
        + [dict(r, PF="280351-DS") for r in _subs("280351")], columns=COLS)
    assert _needs(BomGraph(legacy), "280351") == OLD_NEEDS
    assert _needs(BomGraph(legacy), "280351-DS") == OLD_NEEDS


def test_prefixed_variant_is_the_entry_when_no_exact_activity_exists():
    """Rule (2026-09-16): with no activity == SKU the sku-prefixed variant
    ('280358-A') stays the entry point, exactly as before the fix."""
    rows = _fg("280358", "280358-A", "280358") + _subs("280358")
    bom = BomGraph(pd.DataFrame(rows, columns=COLS))
    assert _needs(bom, "280358") == OLD_NEEDS


def test_several_variants_without_exact_activity_keep_todays_behaviour():
    """Documented, unchanged (2026-09-16): several sku-prefixed variants and
    no exact activity -> every variant is walked (no export generation
    has this shape today; a change here must be a deliberate decision)."""
    rows = (_fg("280360", "280360-A", "280360")
            + _fg("280360", "280360-B", "280360") + _subs("280360"))
    got = _needs(BomGraph(pd.DataFrame(rows, columns=COLS)), "280360")
    assert got == {k: pytest.approx(2 * v) for k, v in OLD_NEEDS.items()}


def test_family_fallback_when_no_self_activity_at_all():
    """No exact and no prefixed activity: the FG-family activity of the PF
    is still the entry (the pre-existing fallback)."""
    rows = _fg("280999", "FGX", "280999") + _subs("280999")
    bom = BomGraph(pd.DataFrame(rows, columns=COLS))
    assert _needs(bom, "280999") == OLD_NEEDS


# --------------------------------------------------------------------------
# recipe-less semi-finished intermediates (review of 2026-09-16)
# --------------------------------------------------------------------------

def _slurry_fg(pf):
    """An FG that consumes 100 Kg of HSMBT001 per 1,000 CAS, with lemon
    juice 750061 as its blank-qty (alternate) sibling, plus a pallet."""
    return [
        _row(pf, pf, "FG", "Output", pf, 1000.0, "CAS"),
        _row(pf, pf, "FG", "Input", "HSMBT001", 100.0, "Kg"),
        _row(pf, pf, "FG", "Input", "750061", None, "Kg"),
        _row(pf, pf, "FG", "Input", "758006", 10.0, "EA"),
    ]


def test_recipe_less_semi_finished_group_never_gates_on_its_alternates():
    """Rule of 2026-08-14 (an HSM / semi-finished intermediate never gates a
    SKU), second pass 2026-09-16: with "ediact 4.csv" absent BomGraph cannot
    explode through HSMBT001, and its blank-qty alternate 750061 (tracked,
    nearly out) made the group AT_RISK — 3 board blocks and 12 demand orders
    on the real drop. explode() now flags the group no_recipe (alternates
    kept for display) and coverage_for_requirement grades it NOT_TRACKED, so
    the SKU status is what it is without the group; a purchased item with
    no activity is never flagged. With the recipe loaded the group is gone
    and its sub-component is checked instead."""
    from dataclasses import replace

    from stockcheck import coverage as cov

    frame = pd.DataFrame(_slurry_fg("280614"), columns=COLS)
    exp = BomGraph(frame).explode("280614", 1000.0, as_of="2026-09-16")
    groups = {g.primary_item: g for g in exp.requirements}
    assert set(groups) == {"HSMBT001", "758006"}
    hsm = groups["HSMBT001"]
    assert hsm.no_recipe is True and [a["item"] for a in hsm.alternates] == ["750061"]
    assert groups["758006"].no_recipe is False

    avail = {"750061": 5.0, "758006": 50.0}          # 5 kg of juice vs 100 kg
    tracked = set(avail)
    rows = {g.primary_item: cov.coverage_for_requirement(g, avail, tracked)
            for g in exp.requirements}
    assert rows["HSMBT001"]["status"] == "NOT_TRACKED"
    assert rows["HSMBT001"]["note"] == "semi-finished, recipe missing (ediact 4.csv)"
    assert rows["HSMBT001"]["alternates"][0]["available"] == 5.0
    sku = cov.worst_status([r["status"] for r in rows.values() if r["need"]])
    assert sku == cov.worst_status([rows["758006"]["status"]]) == "OK"
    # the same group unflagged is what the planner saw before the fix
    before = cov.coverage_for_requirement(replace(hsm, no_recipe=False), avail, tracked)
    assert before["status"] == "AT_RISK"

    # with the semi-finished recipe the intermediate is exploded through
    recipe = pd.DataFrame([
        _row("HSMBT001", "HSMBT001", "R", "Output", "HSMBT001", 1000.0, "Kg"),
        _row("HSMBT001", "HSMBT001", "R", "Input", "730009", 900.0, "Kg"),
    ], columns=COLS)
    exp4 = BomGraph(frame, recipe).explode("280614", 1000.0, as_of="2026-09-16")
    got = {g.primary_item: g for g in exp4.requirements}
    assert set(got) == {"730009", "758006"}
    assert got["730009"].need_qty == pytest.approx(90.0)
    assert not any(g.no_recipe for g in exp4.requirements)
