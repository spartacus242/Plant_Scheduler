# tests/test_pinned_blocks.py -- planner-pinned demand blocks (fixed for the
# solver).
#
# The planner can pin a demand block on the Plant Calendar ("Fix for solver"
# in the block popup): the block becomes immovable like an MO and Scenario F
# stages it as committed line-time — a blocked window the solver plans
# around, its kg crediting the demand targets. Pinned state rides in the
# calendar's attrs column as the ';'-separated token 'pinned' and surfaces
# as a payload boolean (same pattern as current_state:completed).
#
# Pinned blocks must NEVER move the fill gate (line_free_from) or the
# changeover base: a block pinned deep in W35 would otherwise block fill of
# the whole line before it — the same trap projected CIPs hit.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "code") not in sys.path:
    sys.path.insert(0, str(ROOT / "code"))

from helpers.calendar_io import (  # noqa: E402
    CALENDAR_COLUMNS,
    calendar_to_gantt_payload,
    gantt_payload_to_calendar,
)
from helpers.plan_fill import (  # noqa: E402
    committed_windows,
    line_free_from,
    pinned_blocks,
    subtract_committed,
)


def _cal(rows: list[dict]) -> pd.DataFrame:
    base = {
        "block_id": "b1", "block_type": "production", "line_id": 9,
        "line_name": "P09", "start_h": 0.0, "end_h": 10.0, "label": "",
        "order_id": "O1", "sku": "S1", "sku_description": "",
        "qty_kg": None, "locked": False, "attrs": "",
    }
    df = pd.DataFrame([{**base, **r} for r in rows], columns=CALENDAR_COLUMNS)
    for c in ("start_h", "end_h", "qty_kg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# --------------------------------------------------------------------------
# attrs token <-> payload bool round trip
# --------------------------------------------------------------------------
def test_pinned_attr_surfaces_as_payload_bool():
    cal = _cal([
        {"block_id": "a", "attrs": "pinned"},
        {"block_id": "b", "attrs": "current_state:queued;pinned"},
        {"block_id": "c", "attrs": ""},
        # exact-token rule: 'unpinned' must NOT read as pinned
        {"block_id": "d", "attrs": "unpinned"},
    ])
    schedule, _ = calendar_to_gantt_payload(cal)
    by_id = {b["id"]: b for b in schedule}

    assert by_id["a"]["pinned"] is True
    assert by_id["b"]["pinned"] is True
    assert by_id["c"]["pinned"] is False
    assert by_id["d"]["pinned"] is False


def test_pin_toggle_round_trips_through_attrs():
    """The popup edits the payload BOOL; the save reconciles the attrs token.
    Pinning adds it, unpinning removes it, other provenance tokens survive."""
    cal = _cal([
        {"block_id": "a", "attrs": ""},
        {"block_id": "b", "attrs": "current_state:queued;pinned"},
    ])
    schedule, windows = calendar_to_gantt_payload(cal)
    by_id = {b["id"]: b for b in schedule}
    by_id["a"]["pinned"] = True     # planner pins a
    by_id["b"]["pinned"] = False    # planner unpins b

    back = gantt_payload_to_calendar(schedule, windows)
    attrs = dict(zip(back["block_id"], back["attrs"]))

    assert attrs["a"] == "pinned"
    assert attrs["b"] == "current_state:queued"

    # and the re-derived payload agrees
    schedule2, _ = calendar_to_gantt_payload(back)
    by_id2 = {b["id"]: b for b in schedule2}
    assert by_id2["a"]["pinned"] is True
    assert by_id2["b"]["pinned"] is False


# --------------------------------------------------------------------------
# the staging selector
# --------------------------------------------------------------------------
def test_pinned_blocks_selects_only_pinned_production():
    cal = _cal([
        {"block_id": "a", "attrs": "pinned"},
        {"block_id": "b", "attrs": ""},
        {"block_id": "t", "attrs": "pinned", "block_type": "trial"},
        {"block_id": "c", "attrs": "unpinned"},
        # a manprg MO is already committed via build_current_state — pinning
        # it must not double-commit its window and kg
        {"block_id": "mo", "attrs": "current_state:queued;pinned"},
    ])
    got = pinned_blocks(cal)

    assert list(got["block_id"]) == ["a"]


def test_pinned_blocks_handles_missing_attrs_and_empty():
    assert len(pinned_blocks(pd.DataFrame())) == 0
    no_attrs = pd.DataFrame([{"block_type": "production", "line_name": "P09"}])
    assert len(pinned_blocks(no_attrs)) == 0


# --------------------------------------------------------------------------
# staging semantics: window + demand credit, but never the gate
# --------------------------------------------------------------------------
def test_pinned_block_becomes_a_committed_window():
    pinned = pinned_blocks(_cal([
        {"block_id": "a", "attrs": "pinned", "start_h": 300.0, "end_h": 340.0},
    ]))
    wins = committed_windows(pinned, 504.0)

    assert len(wins) == 1
    assert wins[0]["line_name"] == "P09"
    assert wins[0]["start_hour"] == 300
    assert wins[0]["end_hour"] == 340


def test_pinned_block_credits_demand_but_does_not_gate_the_line():
    """A block pinned deep in the horizon reduces its SKU's demand target
    (the plant will make that kg) but the fill gate stays at the committed
    tail — the solver may still fill the hours BEFORE the pin."""
    committed_tail = _cal([
        {"block_id": "mo", "start_h": 0.0, "end_h": 40.0, "qty_kg": 1000.0},
    ])
    pinned = pinned_blocks(_cal([
        {"block_id": "pin", "attrs": "pinned", "start_h": 300.0,
         "end_h": 340.0, "qty_kg": 5000.0, "sku": "S2", "order_id": "O2"},
    ]))
    both = pd.concat([committed_tail, pinned], ignore_index=True)

    # the gate comes from committed WORK only (the manprg tail), never a pin
    assert line_free_from(committed_tail, 504.0) == {"P09": 40.0}

    demand = pd.DataFrame([
        {"order_id": "O2", "sku": "S2", "week_index": 1,
         "qty_target": 6000.0, "lower_pct": 0.9, "upper_pct": 1.1},
    ])
    residual, notes = subtract_committed(
        demand, both, week_bounds=[0.0, 168.0, 336.0])

    # pin midpoint 320h -> week 1 bucket -> 5000 kg credited, 1000 kg left
    assert residual["qty_target"].iloc[0] == 1000.0
    assert len(notes) == 1
