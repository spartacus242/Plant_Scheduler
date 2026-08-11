"""Unit tests for the CP-SAT warm start (task-list item 10).

`build_hint_plan` is the pure half of the warm start: it maps a previous
`schedule_phase2.csv` onto the CURRENT (line, order) keys. Everything that can
go wrong with a stale hint - an order that no longer exists, a line that no
longer exists, an hour that no longer fits the horizon - has to be DROPPED and
COUNTED here, because a hint outside a variable's domain is a hard CP-SAT
error, not a warning.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from warm_start import build_hint_plan  # noqa: E402


def _data(order_ids, lines=(1, 2)):
    return SimpleNamespace(
        orders=[{"order_id": o} for o in order_ids],
        lines=list(lines),
    )


def _row(line_id, order_id, start, run):
    return {
        "line_id": line_id,
        "order_id": order_id,
        "start_hour": start,
        "end_hour": start + run,
        "run_hours": run,
    }


def test_maps_a_simple_previous_schedule():
    data = _data(["A-W0", "B-W0"])
    rows = [_row(1, "A-W0", 10, 20), _row(2, "B-W0", 0, 5)]

    plan, stats = build_hint_plan(data, 504, rows)

    assert stats["matched_rows"] == 2
    assert stats["assignments"] == 2
    entry = plan[(1, 0)]
    assert entry["present"] == 1
    assert entry["seg_a_start"] == 10
    assert entry["seg_a_run"] == 20
    assert entry["seg_a_end"] == 30
    assert entry["seg_b_present"] == 0
    # Derived vars must be hinted too, so the hint is as close to complete as
    # possible - CP-SAT only really benefits from complete hints.
    assert entry["run_h"] == 20
    assert entry["eff_end"] == 30
    assert plan[(2, 1)]["seg_a_start"] == 0


def test_two_rows_for_one_assignment_become_seg_a_and_seg_b():
    """A CIP split writes two schedule rows for the same (line, order)."""
    data = _data(["A-W0"])
    # Deliberately out of order: the mapping must sort by start hour.
    rows = [_row(1, "A-W0", 100, 10), _row(1, "A-W0", 20, 30)]

    plan, stats = build_hint_plan(data, 504, rows)

    entry = plan[(1, 0)]
    assert stats["split_assignments"] == 1
    assert entry["seg_a_start"] == 20 and entry["seg_a_run"] == 30
    assert entry["seg_b_present"] == 1
    assert entry["seg_b_start"] == 100 and entry["seg_b_run"] == 10


def test_order_that_no_longer_exists_is_dropped_and_counted():
    """A new demand import renames orders; the old hint must not leak in."""
    data = _data(["A-W0"])
    rows = [_row(1, "A-W0", 0, 5), _row(1, "GONE-W9", 0, 5)]

    plan, stats = build_hint_plan(data, 504, rows)

    assert stats["unknown_order"] == 1
    assert stats["matched_rows"] == 1
    assert (1, 0) in plan and len(plan) == 1


def test_line_that_no_longer_exists_is_dropped_and_counted():
    data = _data(["A-W0"], lines=(1,))
    rows = [_row(99, "A-W0", 0, 5)]

    plan, stats = build_hint_plan(data, 504, rows)

    assert stats["unknown_line"] == 1
    assert plan == {}


@pytest.mark.parametrize(
    "start,run",
    [
        (500, 20),   # runs off the end of the horizon
        (600, 5),    # starts past the horizon
    ],
)
def test_hours_outside_the_current_horizon_are_dropped(start, run):
    """The horizon shrinking must never produce an out-of-domain hint."""
    data = _data(["A-W0"])

    plan, stats = build_hint_plan(data, 504, [_row(1, "A-W0", start, run)])

    assert stats["out_of_horizon"] == 1
    assert plan == {}


def test_hint_values_always_lie_inside_the_variable_domains():
    """Every emitted value must satisfy 0 <= v <= H, the CP-SAT var bounds."""
    data = _data(["A-W0", "B-W0"])
    rows = [_row(1, "A-W0", 0, 504), _row(2, "B-W0", 503, 1)]
    H = 504

    plan, _ = build_hint_plan(data, H, rows)

    assert plan, "expected the boundary cases to map, not to be dropped"
    for entry in plan.values():
        for name, val in entry.items():
            if name in ("present", "seg_b_present"):
                assert val in (0, 1)
            else:
                assert 0 <= val <= H, f"{name}={val} outside 0..{H}"


def test_empty_previous_schedule_yields_no_plan_but_valid_stats():
    plan, stats = build_hint_plan(_data(["A-W0"]), 504, [])

    assert plan == {}
    assert stats["rows"] == 0 and stats["assignments"] == 0
