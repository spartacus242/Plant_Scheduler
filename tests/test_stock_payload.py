# tests/test_stock_payload.py — Calendar-side supply payload + caption.
#
# helpers/calendar_io.build_stock_payload turns the SAVED stock report into
# the Gantt's StockArgs (contract §5): None for an old cache, every hour
# shifted from the report's anchor frame into the page's, only recipe items
# carried. board_supply_summary re-grades the pushed board server-side with
# stockcheck/timeline for the caption (§7). Inputs are the synthetic cases
# from scripts/gen_stock_risk_fixture.py so every verdict is known.

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "scripts"))

import gen_stock_risk_fixture as gen  # noqa: E402
from helpers.calendar_io import (  # noqa: E402
    board_supply_summary,
    build_stock_payload,
)

ITEM = gen.ITEM
SKU = gen.SKU


def K(blk: dict) -> str:
    """The summary's key for a fixture block: board_supply_summary keys by
    timeline.block_key(id, start) = the Gantt's `id|start` (fix K-8, audit
    stock-14); the fixture's own explicit keys keep the older `id@start.2f`
    spelling, so they are re-derived here rather than copied."""
    return gen.tl.block_key({"block_id": blk["block_id"], "start_h": blk["start_h"]})
KG_PER_CASE = 2.16          # gen.needs default
REPORT_ANCHOR = "2026-09-01 00:00:00"
STOCK_ARG_KEYS = {
    "feed_state", "as_of", "receipts_window_end_h", "rules", "opening",
    "tracked", "in_house", "units", "designations", "snapshot_h", "receipts",
    "sku_needs", "cases_left",
}


def make_report(inputs: dict, *, anchor: str = REPORT_ANCHOR) -> dict:
    """A stock report carrying the contract §4 keys for one fixture case.
    An unreferenced item (999001) rides along in meta to prove the payload
    drops what no recipe uses."""
    return {
        "anchor": anchor,
        "schedule_view": [],
        "supply_meta": {
            "rules": dict(gen.tl.DEFAULT_RULES),
            "snapshot_h": {**inputs["snapshot_h"], "999001": 3.0},
            "snapshot_stamp": {"rm": "2026-09-01 14:46:00",
                               "pkg": "2026-09-01 14:50:00"},
            "receipts_window_end_h": inputs["receipts_window_end_h"],
            "feed_state": inputs["feed_state"],
            "opening": {**inputs["opening"], "999001": 12.0},
            "tracked": list(inputs["tracked"]) + ["999001"],
            "in_house": list(inputs["in_house"]),
            "units": {i: "EA" for i in inputs["opening"]} | {"999001": "KG"},
            "designations": {ITEM: "SLV 24x90 sleeve", "999001": "ink"},
        },
        "sku_needs": inputs["sku_needs"],
        "inbound": {
            "state": inputs["feed_state"],
            "source_mtime": "2026-09-01 06:10:00",
            "as_of": None,
            "receipts": inputs["receipts"],
        },
    }


def record(blk: dict, *, attrs: str = "", locked: bool | None = None,
           qty_kg: float | None = "auto", block_type: str = "production",
           order_id: str = "") -> dict:
    """A working-board production record for a fixture Block: kg is cases x
    kg_per_case so the summary recovers the fixture's case count."""
    kg = blk["cases"] * KG_PER_CASE if qty_kg == "auto" else qty_kg
    return {
        "block_id": blk["block_id"], "block_type": block_type,
        "line_id": 1, "line_name": blk["line_name"],
        "start_h": blk["start_h"], "end_h": blk["end_h"],
        "label": blk["sku"], "order_id": order_id, "sku": blk["sku"],
        "sku_description": "", "qty_kg": kg,
        "locked": blk["locked"] if locked is None else locked,
        "attrs": attrs,
    }


# --------------------------------------------------------------------------
# build_stock_payload
# --------------------------------------------------------------------------

def test_payload_none_without_supply_meta():
    assert build_stock_payload(None, {}, REPORT_ANCHOR) is None
    assert build_stock_payload({}, {}, REPORT_ANCHOR) is None
    old_cache = {"schedule_view": [], "demand_view": [], "anchor": REPORT_ANCHOR}
    assert build_stock_payload(old_cache, {}, REPORT_ANCHOR) is None
    assert build_stock_payload({"supply_meta": "nope"}, {}, REPORT_ANCHOR) is None


def test_payload_shape_matches_stock_args():
    inputs = gen.base_inputs()
    pay = build_stock_payload(make_report(inputs), {}, REPORT_ANCHOR,
                              cases_left={"MO1": 20000, "MO2": "n/a"})
    assert set(pay) == STOCK_ARG_KEYS
    assert pay["feed_state"] == "ok"
    assert pay["as_of"] == {"stock_rm": "9/1 14:46", "stock_pkg": "9/1 14:50",
                            "po": "9/1 06:10"}
    assert set(pay["rules"]) == {"min_days_after_delivery", "lead_measured_from",
                                 "dependent_frac_floor", "hard_block"}
    assert pay["rules"]["min_days_after_delivery"] == 4
    assert pay["rules"]["hard_block"] is False
    # same frame -> hours unchanged
    assert pay["receipts_window_end_h"] == gen.WINDOW_END
    assert pay["snapshot_h"] == {ITEM: gen.SNAPSHOT_H}
    assert pay["opening"] == {ITEM: 36000.0}
    assert pay["receipts"][ITEM][0]["ready_h"] == 40.0
    assert pay["receipts"][ITEM][0]["po8"] == "30043543"
    assert pay["sku_needs"][SKU]["kg_per_case"] == KG_PER_CASE
    assert pay["sku_needs"][SKU]["items"][0] == {
        "item": ITEM, "per_case": 1.0, "unit": "EA", "alts": []}
    # lean: the unreferenced item never travels; in_house passes through
    assert "999001" not in pay["opening"]
    assert "999001" not in pay["units"] and "999001" not in pay["designations"]
    assert "999001" not in pay["snapshot_h"]
    assert pay["tracked"] == [ITEM]
    assert pay["in_house"] == sorted(gen.IN_HOUSE)
    assert pay["units"] == {ITEM: "EA"}
    assert pay["designations"] == {ITEM: "SLV 24x90 sleeve"}
    # cases_left: numeric only, string keys
    assert pay["cases_left"] == {"MO1": 20000.0}


def test_payload_shifts_hours_into_the_page_frame():
    inputs = gen.base_inputs()
    # report computed against 08-31, page anchored a day later -> -24 h
    pay = build_stock_payload(make_report(inputs, anchor="2026-08-31 00:00:00"),
                              {}, "2026-09-01 00:00:00")
    assert pay["snapshot_h"][ITEM] == pytest.approx(gen.SNAPSHOT_H - 24.0)
    assert pay["receipts"][ITEM][0]["ready_h"] == pytest.approx(16.0)
    assert pay["receipts_window_end_h"] == pytest.approx(gen.WINDOW_END - 24.0)
    # qty / po8 / label untouched by the shift
    assert pay["receipts"][ITEM][0]["qty"] == 67200.0
    assert pay["receipts"][ITEM][0]["label"] == gen.R40["label"]
    # a report without an anchor is taken to be in the page frame already
    rep = make_report(inputs)
    del rep["anchor"]
    assert build_stock_payload(rep, {}, "2026-08-01")["snapshot_h"][ITEM] == \
        gen.SNAPSHOT_H


def test_payload_rules_come_from_stock_config():
    cfg = {"stock": {"min_days_after_delivery": 2, "hard_block": True,
                     "lead_measured_from": "depletion"}}
    pay = build_stock_payload(make_report(gen.base_inputs()), cfg, REPORT_ANCHOR)
    assert pay["rules"] == {"min_days_after_delivery": 2,
                            "lead_measured_from": "depletion",
                            "dependent_frac_floor": 0.05, "hard_block": True}


def test_payload_feed_state_and_stamps_degrade():
    inputs = gen.base_inputs(receipts={}, feed_state="missing")
    rep = make_report(inputs)
    rep["supply_meta"]["snapshot_stamp"] = {}
    rep["inbound"] = {"state": "missing", "source_mtime": "", "as_of": None}
    pay = build_stock_payload(rep, {}, REPORT_ANCHOR)
    assert pay["feed_state"] == "missing"
    assert pay["as_of"] == {"stock_rm": "", "stock_pkg": "", "po": ""}
    assert pay["receipts"] == {}
    assert pay["cases_left"] == {}


# --------------------------------------------------------------------------
# board_supply_summary
# --------------------------------------------------------------------------

def _payload(inputs: dict) -> dict:
    return build_stock_payload(make_report(inputs), {}, REPORT_ANCHOR)


def test_summary_case_a_counts_dependent_blocks():
    pay = _payload(gen.base_inputs())
    # queued P19 + the P10 split tail: both wait on PO 30043543
    recs = [record(gen.P19), record(gen.P10_TAIL)]
    s = board_supply_summary(recs, pay, {})
    assert (s["short"], s["dependent"], s["no_data"]) == (0, 2, 0)
    assert s["verdicts"] == {K(gen.P19): "DEPENDENT", K(gen.P10_TAIL): "DEPENDENT"}
    assert K(gen.P19) == "cs_44c4bde023|71.85"
    # add the running P10 MO (manprg running flag, committed) -> 3 dependent
    recs.append(record(gen.P10_RUN, attrs="current_state:running", locked=True))
    s = board_supply_summary(recs, pay, {})
    assert (s["short"], s["dependent"], s["no_data"]) == (0, 3, 0)
    assert set(s["verdicts"]) == {K(gen.P10_RUN), K(gen.P19), K(gen.P10_TAIL)}
    assert all(v == "DEPENDENT" for v in s["verdicts"].values())


def test_summary_matches_the_engine_on_the_fixture_board():
    """Same numbers as the golden case: the record -> Block translation
    (kg / kg_per_case, key = block_id|start) must not move a verdict."""
    pay = _payload(gen.base_inputs())
    recs = [record(gen.P10_RUN, attrs="current_state:running", locked=True),
            record(gen.P19), record(gen.P10_TAIL)]
    s = board_supply_summary(recs, pay, {})
    expected = gen.run_case(gen.CASES[0])["board"]
    by_blk = {b["key"]: b for b in gen.CASES[0]["inputs"]["blocks"]}
    assert s["verdicts"] == {K(by_blk[k]): v["verdict"] for k, v in expected.items()}


def test_summary_case_c_counts_short():
    pay = _payload(gen.base_inputs(receipts={ITEM: [gen.R112]}))
    recs = [record(gen.P10_RUN, attrs="current_state:running", locked=True),
            record(gen.P19), record(gen.P10_TAIL)]
    s = board_supply_summary(recs, pay, {})
    assert (s["short"], s["dependent"], s["no_data"]) == (2, 1, 0)
    assert s["verdicts"][K(gen.P19)] == "SHORT"
    assert s["verdicts"][K(gen.P10_TAIL)] == "DEPENDENT"


def test_summary_no_data_and_minor_are_not_dependent():
    # Updated 2026-09-03 (fix K-4 / audit stock-1): a fresh feed with NO
    # inbound for the item is a proven shortage even when its window (h20)
    # ends before the block — SHORT, not NO_DATA
    s = board_supply_summary([record(gen.P19)],
                             _payload(gen.base_inputs(
                                 receipts={}, receipts_window_end_h=20.0)), {})
    assert (s["short"], s["dependent"], s["no_data"]) == (1, 0, 0)
    # a MISSING feed is the genuine no-data case
    s = board_supply_summary([record(gen.P19)],
                             _payload(gen.base_inputs(
                                 receipts={}, feed_state="missing")), {})
    assert (s["short"], s["dependent"], s["no_data"]) == (0, 0, 1)
    # 3.5 % dependent share is a grey chip: counted nowhere, verdict kept
    s = board_supply_summary([record(gen.P19)],
                             _payload(gen.base_inputs(
                                 blocks=[gen.P19], opening={ITEM: 40000.0})), {})
    assert (s["short"], s["dependent"], s["no_data"]) == (0, 0, 0)
    assert s["verdicts"][K(gen.P19)] == "DEPENDENT"


def test_summary_skips_unknown_skus_windows_and_float_codes():
    """UPDATED 2026-09-03 (INTEGRATE, agent FE handoff ui-11): a production
    block whose SKU has NO recipe in the payload is no longer skipped — it is
    graded NO_DATA (grey "?", never green), exactly as the client pill
    (supplyGlue.toTimelineBlock) and the stock report (fix K-1) already did,
    so the caption's counts equal the pill's by construction. CIP windows are
    still skipped; a float-typed code still resolves to the recipe."""
    pay = _payload(gen.base_inputs())
    unknown = record({**gen.P19, "block_id": "unk", "sku": "999999"})
    cip = record(gen.P19, block_type="cip")
    # float-typed CSV code still resolves to the recipe
    floaty = record({**gen.P19, "sku": f"{SKU}.0"})
    s = board_supply_summary([unknown, cip, floaty], pay, {})
    unk_key = gen.tl.block_key({"block_id": "unk", "start_h": gen.P19["start_h"]})
    assert set(s["verdicts"]) == {K(gen.P19), unk_key}
    assert s["verdicts"][unk_key] == "NO_DATA"
    assert (s["dependent"], s["no_data"]) == (1, 1)


def test_summary_running_block_uses_cases_left_and_lock():
    inputs = gen.base_inputs(receipts={})
    rep = make_report(inputs)
    pay = build_stock_payload(rep, {}, REPORT_ANCHOR,
                              cases_left={"MO-RUN": 20000.0})
    run = record(gen.P10_RUN, attrs="current_state:running", locked=False,
                 order_id="MO-RUN")
    # G1 fixture: cases_left 20,000 of 36,000 on hand -> the running MO is OK
    s = board_supply_summary([run], pay, {})
    assert s["verdicts"][K(gen.P10_RUN)] == "OK"
    # without manprg's cases_left the uniform tail (25,667) still fits
    pay_nl = build_stock_payload(rep, {}, REPORT_ANCHOR)
    s = board_supply_summary([run], pay_nl, {})
    assert s["verdicts"][K(gen.P10_RUN)] == "OK"
    # a lock boundary past the block start marks it locked (chase_po, not
    # move) — the verdict is unchanged, the translation must not crash
    s = board_supply_summary([record(gen.P19)], _payload(gen.base_inputs()),
                             {}, locked_through_h=100.0)
    assert s["verdicts"][K(gen.P19)] == "DEPENDENT"


def test_summary_unknown_kg_draws_nothing():
    """UPDATED 2026-09-03 (INTEGRATE, agent FE handoff ui-11): a block with
    UNKNOWN kg used to draw nothing and read green (OK) — "we do not know"
    rendered as "fine". It now falls back to rate x hours when the page's
    caps table knows the line's rate for the SKU (the client's rule), else
    it is graded NO_DATA. Here no caps are given -> NO_DATA; the second
    (NaN) record is the same board row (same id|start) and is not drawn
    twice. With caps that reproduce the fixture's kg exactly the verdict
    equals the auto-kg run (DEPENDENT on the base inputs)."""
    pay = _payload(gen.base_inputs())
    s = board_supply_summary([record(gen.P19, qty_kg=None),
                              record(gen.P19, qty_kg=float("nan"))], pay, {})
    assert s["verdicts"] == {K(gen.P19): "NO_DATA"}
    assert (s["short"], s["dependent"], s["no_data"]) == (0, 0, 1)
    hours = float(gen.P19["end_h"]) - float(gen.P19["start_h"])
    rate = float(gen.P19["cases"]) * KG_PER_CASE / hours     # kg/h that rebuilds the fixture kg
    caps = {gen.P19["line_name"]: {SKU: rate}}
    s2 = board_supply_summary([record(gen.P19, qty_kg=None)], pay, {}, caps=caps)
    ref = board_supply_summary([record(gen.P19)], pay, {})
    assert s2["verdicts"] == ref["verdicts"] == {K(gen.P19): "DEPENDENT"}


def test_summary_without_payload_is_empty():
    assert board_supply_summary([record(gen.P19)], None, {}) == {
        "short": 0, "dependent": 0, "no_data": 0, "verdicts": {}}
    assert board_supply_summary([], _payload(gen.base_inputs()), {})["verdicts"] == {}


# --------------------------------------------------------------------------
# component kwargs
# --------------------------------------------------------------------------

def test_gantt_calendar_accepts_stock_and_focus_block():
    from components.gantt import gantt_calendar
    params = inspect.signature(gantt_calendar).parameters
    assert "stock" in params and params["stock"].default is None
    assert "focus_block" in params and params["focus_block"].default is None
    # the mount forwards them under the SandboxArgs names
    src = inspect.getsource(sys.modules["components.gantt"])
    assert "stock=stock" in src
    assert "focusBlock=focus_block" in src
