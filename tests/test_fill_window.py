# tests/test_fill_window.py -- fill-window scoring for Scenario F proposals.
#
# Design (scenario-f-fill-the-tail-2026-08-14, honesty rules): an F proposal
# shares its committed layer with the official board BY CONSTRUCTION, so the
# honest comparison is both calendars windowed to the fill region (after each
# line's committed tail) and judged against the RESIDUAL demand left after
# committed production. Scoring whole calendars charged the proposal for
# changeovers/CIP the fill necessarily costs while comparing unequal scopes
# (~2-week board vs 3-week plan) -- flagged three times before this landed
# (F design, agent dry-run 1, solver un-blinding notes).
#
# Window rule pinned here:
#   - pre-gate production/trial blocks are EXCLUDED (the plant's own plan),
#   - EXCEPT the last pre-gate production block per line (the changeover
#     base: the committed->fill boundary changeover IS the solver's cost),
#   - CIP blocks are ALWAYS kept (clean history; dropping pre-gate CIPs
#     corrupts clock-since-last-clean inside the fill region),
#   - residual demand: committed kg subtracted, fully-covered orders DROPPED
#     (they are the plant's work -- counting them "late" against the fill
#     was the core unfairness).
#
# Synthetic calendars only; committed blocks stay within [0, 24) h so their
# midpoint lands in ISO week 0 under ANY anchor weekday (the first true
# Monday boundary is >= 24 h out even from a Sunday anchor).

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "code") not in sys.path:
    sys.path.insert(0, str(ROOT / "code"))

from helpers.calendar_io import CALENDAR_COLUMNS  # noqa: E402
from helpers.config import scorecard_config  # noqa: E402
from helpers.scorecard_engine import (  # noqa: E402
    _apply_fill_window,
    _residual_fill_demand,
    score_calendar,
    score_changeovers,
)
from helpers.version_manager import load_version, save_version  # noqa: E402

CFG = scorecard_config({})


def _block(**kw) -> dict:
    row = {
        "block_id": kw.get("block_id", "b1"),
        "block_type": kw.get("block_type", "production"),
        "line_id": kw.get("line_id", 9),
        "line_name": kw.get("line_name", "P09"),
        "start_h": float(kw.get("start_h", 0.0)),
        "end_h": float(kw.get("end_h", 10.0)),
        "label": kw.get("label", ""),
        "order_id": kw.get("order_id", "O1"),
        "sku": kw.get("sku", "S1"),
        "sku_description": kw.get("sku_description", ""),
        "qty_kg": kw.get("qty_kg", None),
        "locked": False,
        "attrs": "",
    }
    return row


def _calendar(blocks: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(blocks, columns=CALENDAR_COLUMNS)
    for c in ("start_h", "end_h", "qty_kg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# --------------------------------------------------------------------------
# the window rule
# --------------------------------------------------------------------------
def test_window_keeps_base_cips_and_fill_drops_the_rest():
    cal = _calendar([
        _block(block_id="cA", start_h=0, end_h=8, sku="X", qty_kg=1000.0),
        _block(block_id="cB", start_h=8, end_h=18, sku="Y", qty_kg=1000.0),
        _block(block_id="cip1", block_type="cip", start_h=18, end_h=20,
               order_id="", sku=""),
        _block(block_id="tr1", block_type="trial", start_h=20, end_h=22,
               order_id="", sku="TRIAL"),
        _block(block_id="fC", start_h=30, end_h=40, sku="Z", qty_kg=500.0),
    ])
    windowed, committed_prod = _apply_fill_window(cal, {"P09": 22.0})

    kept = set(windowed["block_id"])
    # base (last pre-gate production) + pre-gate CIP + the fill block
    assert kept == {"cB", "cip1", "fC"}
    # committed production (pre-gate, ALL of it) feeds the demand subtraction
    assert set(committed_prod["block_id"]) == {"cA", "cB"}


def test_window_gate_keys_are_case_insensitive_and_default_to_zero():
    cal = _calendar([
        _block(block_id="a", line_name="P09", start_h=0, end_h=10, sku="X"),
        _block(block_id="b", line_name="P10", line_id=10, start_h=0, end_h=10,
               sku="Y"),
    ])
    windowed, _ = _apply_fill_window(cal, {"p09": 10.0})

    # P09 gated at 10 -> its only block becomes the base and is kept;
    # P10 has no gate -> gate 0 -> its block is fill region, kept.
    assert set(windowed["block_id"]) == {"a", "b"}


def test_boundary_changeover_is_charged_but_committed_ones_are_not():
    """Committed X->Y never reaches the windowed frame; the base block keeps
    the Y->Z boundary changeover visible -- that one IS the fill decision."""
    cal = _calendar([
        _block(block_id="cA", start_h=0, end_h=8, sku="X", qty_kg=1000.0),
        _block(block_id="cB", start_h=8, end_h=18, sku="Y", qty_kg=1000.0),
        _block(block_id="fC", start_h=30, end_h=40, sku="Z", qty_kg=500.0),
    ])
    full = score_changeovers(cal, CFG, {})
    windowed, _ = _apply_fill_window(cal, {"P09": 18.0})
    win = score_changeovers(windowed, CFG, {})

    assert full["sku_transitions"] == 2   # X->Y (committed) + Y->Z (boundary)
    assert win["sku_transitions"] == 1    # only the boundary survives


# --------------------------------------------------------------------------
# residual demand
# --------------------------------------------------------------------------
def _demand_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {"order_id": "O1", "sku": "S1", "week_index": 0, "qty_target": 5000.0,
         "lower_pct": 0.9, "upper_pct": 1.1, "due_start_hour": 0,
         "due_end_hour": 400.0, "qty_min": 4500.0, "qty_max": 5500.0},
        {"order_id": "O2", "sku": "S2", "week_index": 0, "qty_target": 4000.0,
         "lower_pct": 0.9, "upper_pct": 1.1, "due_start_hour": 0,
         "due_end_hour": 400.0, "qty_min": 3600.0, "qty_max": 4400.0},
    ])


def test_residual_demand_drops_covered_orders_and_rederives_bounds():
    committed = _calendar([
        _block(block_id="cA", start_h=0, end_h=20, sku="S1", qty_kg=6000.0),
    ])
    residual, n_covered = _residual_fill_demand(_demand_frame(), committed)

    # S1's 6000 committed kg covers O1's 5000 target entirely -> dropped
    assert n_covered == 1
    assert list(residual["order_id"]) == ["O2"]
    # bounds re-derived from the (untouched) residual target
    assert residual["qty_max"].iloc[0] == pytest.approx(4400.0)
    assert residual["qty_min"].iloc[0] == pytest.approx(3600.0)


def test_residual_demand_passes_through_without_required_columns():
    dem = pd.DataFrame([{"order_id": "O1", "sku": "S1", "qty_max": 8000.0}])
    committed = _calendar([_block(qty_kg=6000.0)])
    residual, n_covered = _residual_fill_demand(dem, committed)

    assert n_covered == 0
    assert residual is dem


# --------------------------------------------------------------------------
# end to end: the unfairness this view fixes
# --------------------------------------------------------------------------
def _write_reference(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "demand_plan.csv").write_text(
        "order_id,sku,week_index,qty_target,lower_pct,upper_pct,"
        "due_start_hour,due_end_hour\n"
        "O1,S1,0,5000,0.9,1.1,0,400\n"
        "O2,S2,0,4000,0.9,1.1,0,400\n",
        encoding="utf-8")


def test_windowed_compare_isolates_the_fill_decision(tmp_path):
    """Official board = committed layer only; F proposal = same committed
    layer + one fill block covering O2. Windowed with the same gates:
    the official leaves the residual order late, the proposal covers it,
    and O1 (covered by committed production) is late on NEITHER side."""
    _write_reference(tmp_path)
    committed = [
        _block(block_id="cA", start_h=0, end_h=20, sku="S1", order_id="MO1",
               qty_kg=6000.0),
    ]
    fill = [
        _block(block_id="fB", start_h=30, end_h=70, sku="S2", order_id="O2",
               qty_kg=4000.0),
    ]
    gates = {"P09": 20.0}
    official = score_calendar(_calendar(committed), week_label="official",
                              data_dir=tmp_path, cfg=CFG, fill_gates=gates)
    proposal = score_calendar(_calendar(committed + fill), week_label="F",
                              data_dir=tmp_path, cfg=CFG, fill_gates=gates)

    # O1 is the plant's work (fully covered by committed kg): late on neither
    # side. O2 is the fill's job: late for the empty official fill region,
    # covered by the proposal.
    assert official.service["orders_total"] == 1
    assert proposal.service["orders_total"] == 1
    assert official.service["orders_late"] == 1
    assert proposal.service["orders_late"] == 0
    # the shared committed layer contributes identically on both sides:
    # no changeovers on the official side, only the boundary one on the
    # proposal side (S1 base -> S2 fill)
    assert official.changeovers["sku_transitions"] == 0
    assert proposal.changeovers["sku_transitions"] == 1
    assert any("Fill-window view" in n for n in proposal.notes)


def test_score_without_gates_is_untouched(tmp_path):
    """fill_gates=None is the existing full-horizon path -- same numbers as
    before this feature (the whole rest of the suite pins that behaviour;
    this is the direct regression guard)."""
    _write_reference(tmp_path)
    cal = _calendar([
        _block(block_id="cA", start_h=0, end_h=20, sku="S1", order_id="MO1",
               qty_kg=6000.0),
    ])
    full = score_calendar(cal, week_label="t", data_dir=tmp_path, cfg=CFG)

    # both demand orders in scope, neither dropped
    assert full.service["orders_total"] == 2
    assert not any("Fill-window view" in n for n in full.notes)


# --------------------------------------------------------------------------
# persistence: gates ride with the saved version
# --------------------------------------------------------------------------
def test_save_version_carries_fill_gates_metadata(tmp_path):
    cal = _calendar([_block(qty_kg=1000.0)])
    slug = save_version("F test", cal, {"composite": 50.0}, tmp_path,
                        source="solver:balanced",
                        extra_meta={"fill_gates": {"P09": 20.0},
                                    "name": "MUST NOT SHADOW"})
    data = load_version(slug, tmp_path)

    assert data["metadata"]["fill_gates"] == {"P09": 20.0}
    # extras must never shadow core keys
    assert data["metadata"]["name"] == "F test"
