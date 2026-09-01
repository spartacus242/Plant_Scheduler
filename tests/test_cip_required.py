"""cip_req_after as TIME (user rule 2026-09-01): the solver's setup floor
reserves the clean's slot, the fill pipeline draws the CIP into it, and the
guard counts only solver-controllable violations."""
import pandas as pd

from helpers.calendar_io import CALENDAR_COLUMNS
from helpers.config import scorecard_config
from helpers.plan_fill import materialize_required_cips
from helpers.scorecard_engine import score_changeovers
from solver.changeover_cache import apply_cip_req_setup_floor

CFG = scorecard_config({})
FLAG = {("P", "X"): {"cip_req_after": 1}, ("X", "P"): {"cip_req_after": 1}}


def _blk(bid, line, s, e, sku, btype="production", attrs=""):
    return {"block_id": bid, "block_type": btype, "line_id": 1,
            "line_name": line, "start_h": float(s), "end_h": float(e),
            "label": sku, "order_id": bid.upper(), "sku": sku,
            "sku_description": "", "qty_kg": 100.0, "locked": False,
            "attrs": attrs}


def _cal(rows):
    return pd.DataFrame(rows, columns=CALENDAR_COLUMNS)


def test_setup_floor_lifts_only_flagged_pairs():
    setup = {("P", "X"): 2, ("X", "P"): 8, ("A", "B"): 1}
    mc = {("P", "X"): {"cip_req_after": 1}, ("X", "P"): {"cip_req_after": 1},
          ("A", "B"): {"cip_req_after": 0}}
    out = apply_cip_req_setup_floor(setup, mc, 6)
    assert out[("P", "X")] == 6      # lifted to the clean
    assert out[("X", "P")] == 8      # already longer: untouched
    assert out[("A", "B")] == 1      # unflagged: untouched
    assert setup[("P", "X")] == 2    # input dict not mutated
    assert apply_cip_req_setup_floor(setup, mc, 0) == setup


def test_materialize_draws_cip_into_the_forced_gap():
    cal = _cal([_blk("a", "P10", 0, 10, "P"), _blk("b", "P10", 16, 30, "X")])
    out, notes = materialize_required_cips(cal, FLAG, 6.0)
    cips = out[out["block_type"] == "cip"]
    assert len(cips) == 1
    c = cips.iloc[0]
    assert (c["start_h"], c["end_h"]) == (10.0, 16.0)
    assert c["attrs"] == "solver:cip_req"
    assert any("drawn" in n for n in notes)
    # ...and the scorecard now waives that transition entirely
    co = score_changeovers(out, CFG, FLAG)
    assert co["cip_req_violations"] == 0
    assert co["transitions_at_cip"] == 1


def test_materialize_skips_covered_and_short_gaps():
    covered = _cal([_blk("a", "P10", 0, 10, "P"),
                    _blk("c", "P10", 10, 16, "CIP", btype="cip"),
                    _blk("b", "P10", 16, 30, "X")])
    out, _ = materialize_required_cips(covered, FLAG, 6.0)
    assert (out["block_type"] == "cip").sum() == 1  # nothing added

    short = _cal([_blk("a", "P10", 0, 10, "P"), _blk("b", "P10", 12, 30, "X")])
    out, notes = materialize_required_cips(short, FLAG, 6.0)
    assert (out["block_type"] == "cip").sum() == 0
    assert any("left for the planner" in n for n in notes)

    unflagged = _cal([_blk("a", "P10", 0, 10, "A"), _blk("b", "P10", 16, 30, "B")])
    out, _ = materialize_required_cips(unflagged, FLAG, 6.0)
    assert (out["block_type"] == "cip").sum() == 0


def test_inherited_violations_are_marked_and_counted():
    """Both runs committed manprg blocks -> inherited (plant's own sequence);
    a committed->solver boundary is NOT inherited."""
    cal = _cal([
        _blk("m1", "P15", 0, 10, "P", attrs="current_state:running"),
        _blk("m2", "P15", 12, 20, "X", attrs="current_state:queued"),
        _blk("f1", "P15", 22, 30, "P"),
    ])
    co = score_changeovers(cal, CFG, FLAG)
    assert co["cip_req_violations"] == 2
    assert co["cip_req_inherited"] == 1
    flags = {(r["from_sku"], r["to_sku"]): r["committed"] for r in co["cip_req_detail"]}
    assert flags[("P", "X")] is True
    assert flags[("X", "P")] is False
