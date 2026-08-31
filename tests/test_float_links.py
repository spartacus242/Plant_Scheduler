"""Floating blocks (2026-08-28): attrs token after:<anchor_id>:<gap_h> ties a
block's start to another block's end — the running-MO re-forecast pushes or
pulls the follower by the same amount. Linking also pins (planner commitment);
the board only moves via the Calendar page's explicit apply."""
import pandas as pd
import pytest

from helpers.calendar_io import (CALENDAR_COLUMNS, apply_float_links,
                                 calendar_to_gantt_payload, clear_float_link,
                                 float_link_of, gantt_payload_to_calendar,
                                 set_float_link)


def _block(bid, start, end, *, btype="production", line=9, sku="S", attrs=""):
    return {"block_id": bid, "block_type": btype, "line_id": line,
            "line_name": f"P{line:02d}", "start_h": float(start),
            "end_h": float(end), "label": sku, "order_id": bid.upper(),
            "sku": sku, "sku_description": "", "qty_kg": 100.0,
            "locked": False, "attrs": attrs}


def _cal(blocks):
    return pd.DataFrame(blocks, columns=CALENDAR_COLUMNS)


def test_link_captures_gap_and_pins_then_follows_the_anchor():
    cal = _cal([_block("mo1", 0, 40, attrs="current_state:running"),
                _block("b1", 42, 60)])
    cal = set_float_link(cal, "b1", "mo1")
    attrs = cal.loc[cal["block_id"] == "b1", "attrs"].iloc[0]
    assert float_link_of(attrs) == ("mo1", 2.0)
    assert "pinned" in attrs.split(";")
    # MO runs slow: end re-forecasts 40 -> 47 -> follower moves +7, gap kept
    cal.loc[cal["block_id"] == "mo1", "end_h"] = 47.0
    out, notes = apply_float_links(cal)
    b = out[out["block_id"] == "b1"].iloc[0]
    assert (b["start_h"], b["end_h"]) == (49.0, 67.0)
    assert len(notes) == 1 and "+7.00h" in notes[0]
    # MO runs fast: pulled back
    cal.loc[cal["block_id"] == "mo1", "end_h"] = 35.0
    out, _ = apply_float_links(cal)
    assert out[out["block_id"] == "b1"].iloc[0]["start_h"] == 37.0


def test_chain_settles_in_order():
    cal = _cal([_block("mo1", 0, 40, attrs="current_state:running"),
                _block("b1", 40, 50), _block("b2", 50, 55)])
    cal = set_float_link(cal, "b1", "mo1")
    cal = set_float_link(cal, "b2", "b1")
    cal.loc[cal["block_id"] == "mo1", "end_h"] = 44.0
    out, notes = apply_float_links(cal)
    assert out[out["block_id"] == "b1"].iloc[0]["start_h"] == 44.0
    assert out[out["block_id"] == "b2"].iloc[0]["start_h"] == 54.0
    assert not any("cycle" in n for n in notes)


def test_missing_anchor_degrades_to_note():
    cal = _cal([_block("b1", 42, 60, attrs="after:ghost:2.0;pinned")])
    out, notes = apply_float_links(cal)
    assert out[out["block_id"] == "b1"].iloc[0]["start_h"] == 42.0
    assert any("missing" in n for n in notes)


def test_cycle_is_flagged_not_fatal():
    cal = _cal([_block("b1", 0, 10, attrs="after:b2:0"),
                _block("b2", 10, 20, attrs="after:b1:0")])
    out, notes = apply_float_links(cal)
    assert any("cycle" in n for n in notes)


def test_guards_and_unlink():
    cal = _cal([_block("mo1", 0, 40, attrs="current_state:running"),
                _block("b1", 42, 60)])
    with pytest.raises(ValueError):
        set_float_link(cal, "b1", "b1")
    with pytest.raises(ValueError):
        set_float_link(cal, "mo1", "b1")  # committed MO cannot float
    cal = set_float_link(cal, "b1", "mo1")
    cal = clear_float_link(cal, "b1")
    attrs = cal.loc[cal["block_id"] == "b1", "attrs"].iloc[0]
    assert float_link_of(attrs) is None
    assert "pinned" in attrs.split(";")  # unlink leaves the pin choice alone


def test_float_token_survives_gantt_round_trip():
    cal = _cal([_block("mo1", 0, 40, attrs="current_state:running"),
                _block("b1", 42, 60)])
    cal = set_float_link(cal, "b1", "mo1")
    schedule, windows = calendar_to_gantt_payload(cal)
    back = gantt_payload_to_calendar(schedule, windows)
    attrs = back.loc[back["block_id"] == "b1", "attrs"].iloc[0]
    assert float_link_of(attrs) == ("mo1", 2.0)
    assert "pinned" in attrs.split(";")
