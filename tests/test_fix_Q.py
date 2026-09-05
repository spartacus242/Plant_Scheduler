# tests/test_fix_Q.py — fix Q (scorecard engine), 2026-09-03.
#
# Regression tests for the verified audit findings fixed in
# helpers/scorecard_engine.py, helpers/overnight_score.py and
# helpers/naive_baseline.py. EVERY expected value below is derived BY HAND in
# the comment next to it — never copied from a run. Findings covered:
#
#   Q-1  C45/cip-5 + scorecard-4/5/9/12  cip_forfeited on the plant's WALL
#        clock, PreviousCIP seed (negative allowed), only DISPLACED production
#        charged, early_h reported separately, duplicate CIP rows merged.
#   Q-2  C47/scorecard-7  overdue tolerance 0 (configurable); INHERITED
#        events reported, not gated.
#   Q-3  C22/time-6 + scorecard-2/3/6/10  service scope stops at the horizon,
#        inclusive due_end (+1), at-risk is a warning, adherence waterfall
#        credit, kg-weighted service level.
#   Q-4  C35/changeover-7 + scorecard-14  recipe vs format classification
#        (TTP is cooking -> recipe), hand-verified on 5 live pairs.
#   Q-5  C30/changeover-2 + ui-5  ONE CIP-waiver rule: weekly_breakdown and
#        overnight_score.weighted_co_load / transition_cost.
#   Q-6  scorecard-1/adversarial-8 + quality-2  empty calendar never
#        outscores a real board; missing inputs -> category None + flag.
#   Q-7  scorecard-11  campaigns = maximal same-SKU runs, kg-weighted mean.
#   Q-8  C57/netting-5 + scorecard-8  ledger_inputs hook forwarded verbatim.
#   Q-9  ui-2  build_demand_targets — one rule.
#   Q-10 adversarial-7  calendar sanity list.
#   Q-11 quality-4  changeover lookup: value-identical to the legacy build,
#        memoised per (path, mtime).
#   Q-13 scorecard-13  naive baseline placed in the graded frame.
#
# Synthetic calendars only; the ONE live-file test (Q-11) reads the frozen
# audit snapshot and skips when it is not on this machine. No CP-SAT.

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "code") not in sys.path:
    sys.path.insert(0, str(ROOT / "code"))

from helpers.calendar_io import CALENDAR_COLUMNS  # noqa: E402
from helpers.config import scorecard_config  # noqa: E402
from helpers.scorecard_engine import (  # noqa: E402
    ScorecardResult,
    _co_lookup,
    _is_format_change,
    _is_recipe_change,
    _load_changeovers,
    _residual_fill_demand,
    build_demand_targets,
    calendar_sanity,
    campaign_runs,
    category_scores,
    cip_last_clean_hours,
    load_co_map,
    score_calendar,
    score_campaigns,
    score_changeovers,
    score_cip,
    score_service,
    weekly_breakdown,
)

CFG = scorecard_config({})
SNAPSHOT = Path(
    "C:/Users/jbdil/AppData/Local/Temp/claude/"
    "C--Users-jbdil-Flowstate-Plant-Scheduler/"
    "4676946a-cd87-4d76-9139-b1ba1df3896a/scratchpad/snapshot")


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def _blk(bid, btype="production", start=0.0, end=10.0, sku="S", order_id="O1",
         line_id=12, line_name="P12", qty_kg=None, attrs=""):
    return {
        "block_id": bid, "block_type": btype, "line_id": line_id,
        "line_name": line_name, "start_h": float(start), "end_h": float(end),
        "label": sku or btype, "order_id": order_id if btype == "production" else "",
        "sku": sku if btype == "production" else "", "sku_description": "",
        "qty_kg": qty_kg, "locked": False, "attrs": attrs,
    }


def _cal(rows):
    df = pd.DataFrame(rows, columns=CALENDAR_COLUMNS)
    df["qty_kg"] = pd.to_numeric(df["qty_kg"], errors="coerce").astype(float)
    return df


def _pin_horizon(monkeypatch, anchor, hours=504):
    from helpers import horizon as hzmod

    hz = hzmod.Horizon(anchor=anchor, start=anchor,
                       end=anchor + timedelta(hours=hours), now=anchor,
                       mode="fixed", hours=hours, config_anchor=anchor)
    monkeypatch.setattr(hzmod, "resolve", lambda *a, **k: hz)


ANCHOR = datetime(2026, 8, 17)  # Monday, ISO W34


# ==========================================================================
# Q-1 / Q-2  score_cip: wall clock, displaced production, seed, merge, gate
# ==========================================================================
IV = {"P12": 120.0}
RATES = {"P12": 1000.0}


def _cip(rows, seed=None, cfg=None):
    return score_cip(_cal(rows), cfg or CFG, IV, RATES, seed)


def test_cip_A_idle_then_prod_clean_exactly_at_interval_forfeits_nothing():
    """PURE-3 A: idle 0-60, prod 60-120, CIP 120-126, seed 0.
    wall_since = 120 - 0 = 120 -> early_h = max(0, 120-120) = 0 -> nothing
    forfeited (old code: 60 h / 60,000 kg, the idle hours). Displaced (diag)
    = production inside [120-6, 126+6] = [114,120] = 6 h. Overdue: block end
    120 - 0 = 120 <= 120 -> 0."""
    r = _cip([_blk("p", start=60, end=120), _blk("c", "cip", 120, 126)])
    assert r["cip_forfeited_h"] == 0.0 and r["cip_forfeited_kg"] == 0.0
    assert r["cip_early_h"] == 0.0 and r["cip_early_count"] == 0
    assert r["cip_displaced_h"] == 6.0
    assert r["cip_overdue"] == 0


def test_cip_B_clean_at_hour_zero_charges_only_the_six_displaced_hours():
    """PURE-3 B: CIP 0-6 then prod 6-100, seed 0 (line clean at t0, so this
    clean is 120 h early). Displaced = prod inside [-6, 12] = [6,12] = 6 h ->
    6 h / 6,000 kg (old: the whole 120 h interval = 120,000 kg). early_h 120.
    Overdue: block end 100 - clean end 6 = 94 <= 120 -> 0."""
    r = _cip([_blk("c", "cip", 0, 6), _blk("p", start=6, end=100)])
    assert r["cip_forfeited_h"] == 6.0 and r["cip_forfeited_kg"] == 6000.0
    assert r["cip_early_h"] == 120.0 and r["cip_early_count"] == 1
    assert r["cip_overdue"] == 0


def test_cip_C_full_interval_of_production_then_clean_is_free():
    """PURE-3 C: prod 0-120, CIP 120-126: wall 120 -> early 0 -> 0 kg;
    displaced [114,120] = 6 h; overdue 120 <= 120 -> 0."""
    r = _cip([_blk("p", start=0, end=120), _blk("c", "cip", 120, 126)])
    assert r["cip_forfeited_kg"] == 0.0 and r["cip_displaced_h"] == 6.0
    assert r["cip_overdue"] == 0


def test_cip_D_planned_downtime_inside_the_interval_is_never_charged():
    """PURE-3 D: prod 0-40, maintenance 40-80, prod 80-120, CIP 120-126:
    wall clock 120 -> not early -> 0 (old: charged the 40 h of maintenance)."""
    r = _cip([_blk("p", start=0, end=40), _blk("m", "maintenance", 40, 80),
              _blk("p2", start=80, end=120), _blk("c", "cip", 120, 126)])
    assert r["cip_forfeited_h"] == 0.0 and r["cip_forfeited_kg"] == 0.0
    assert r["cip_overdue"] == 0


def test_cip_F_two_cleans_second_one_early_charges_six_hours_each():
    """PURE-3 F: CIP 0-6, prod 6-60, CIP 60-66, prod 66-180, seed 0.
    Event 1 (0,6): wall 0 -> early 120; displaced = prod in [-6,12] = 6.
    Event 2 (60,66): wall 60-6 = 54 -> early 66; displaced = prod in
    [54,72] = [54,60] 6 + [66,72] 6 = 12 -> min(dur 6, 12) = 6.
    forfeited 12 h / 12,000 kg (old 186 h); early_h 120+66 = 186, count 2.
    Overdue: (6,60) clock 60-6 = 54; (66,180) clock 180-66 = 114 -> 0."""
    r = _cip([_blk("c1", "cip", 0, 6), _blk("p1", start=6, end=60),
              _blk("c2", "cip", 60, 66), _blk("p2", start=66, end=180)])
    assert r["cip_forfeited_h"] == 12.0 and r["cip_forfeited_kg"] == 12000.0
    assert r["cip_early_h"] == 186.0 and r["cip_early_count"] == 2
    assert r["cip_displaced_h"] == 12.0
    assert r["cip_overdue"] == 0


def test_cip_G_overlapping_duplicate_rows_are_one_clean_event():
    """PURE-3 G (live P12 shape): prod 120-236, CIP 236-242 AND 240-246,
    prod 246-300, seed 0. Rows merge into ONE event (236,246): count 2,
    events 1, duplicate_rows 1 (counted ONCE, not once per pass), cip_hours
    12 (row durations, reported). wall 236 -> not early -> 0 kg (old: 124 h
    / 124,000 kg, the second row charged a fresh interval). Displaced =
    prod in [226,256]: [226,236] 10 + [246,256] 10 = 20 -> min(10, 20) = 10.
    Overdue: block (120,236) ends 236 h after the seed with no clean before
    it -> 1 genuine overdue cycle; (246,300) clock 54 -> fine."""
    r = _cip([_blk("p1", start=120, end=236), _blk("c1", "cip", 236, 242),
              _blk("c2", "cip", 240, 246), _blk("p2", start=246, end=300)])
    assert r["cip_count"] == 2 and r["cip_events"] == 1
    assert r["cip_duplicate_rows"] == 1
    assert r["cip_hours"] == 12.0
    assert r["cip_forfeited_kg"] == 0.0 and r["cip_displaced_h"] == 10.0
    assert r["cip_overdue"] == 1 and r["cip_overdue_inherited"] == 0


def test_cip_H_I_tolerance_is_zero_by_default_and_configurable():
    """PURE-3 H/I: prod 0-131 (11 h over a 120 h interval) and 0-133.
    Default tolerance 0 -> BOTH overdue (the old hard-coded +12 h hid H).
    cip_overdue_tolerance_h = 12 restores the old reading for H only."""
    h = _cip([_blk("p", start=0, end=131)])
    i = _cip([_blk("p", start=0, end=133)])
    assert h["cip_overdue"] == 1 and i["cip_overdue"] == 1
    lenient = dict(CFG, cip_overdue_tolerance_h=12.0)
    assert _cip([_blk("p", start=0, end=131)], cfg=lenient)["cip_overdue"] == 0
    assert _cip([_blk("p", start=0, end=133)], cfg=lenient)["cip_overdue"] == 1


def test_cip_J_negative_seed_carries_pre_anchor_dirt_into_the_walk():
    """PURE-3 J: previous clean 100 h BEFORE the anchor, prod 0-125, no CIP.
    Seed -100 -> clock at block end = 125 - (-100) = 225 > 120 -> overdue 1.
    Seedless (legacy hour-0 clean) reads 125 > 120 -> also 1; with seed
    -100 and a shorter 0-30 run: 130 > 120 -> overdue 1 while seedless says
    30 -> 0. The negative seed is what makes the guard see it."""
    assert _cip([_blk("p", start=0, end=125)], {"P12": -100.0})["cip_overdue"] == 1
    assert _cip([_blk("p", start=0, end=30)], {"P12": -100.0})["cip_overdue"] == 1
    assert _cip([_blk("p", start=0, end=30)])["cip_overdue"] == 0


def test_cip_K_pre_horizon_hours_are_never_charged():
    """Seed -50 (clean 50 h before the anchor), prod 0-30, CIP 30-36, prod
    36-100. wall_since = 30 - (-50) = 80 -> early 40 h; displaced = prod in
    [24,42] = [24,30] 6 + [36,42] 6 = 12 -> min(6,12) = 6 h -> 6,000 kg.
    Same board with seed -100: wall 130 -> not early -> 0 kg; but the
    first run ends 30 - (-100) = 130 h after the clean -> overdue 1 (the
    plant should have cleaned by hour 20). Both halves read ONE clock."""
    rows = [_blk("p1", start=0, end=30), _blk("c", "cip", 30, 36),
            _blk("p2", start=36, end=100)]
    r50 = _cip(rows, {"P12": -50.0})
    assert r50["cip_early_h"] == 40.0
    assert r50["cip_forfeited_h"] == 6.0 and r50["cip_forfeited_kg"] == 6000.0
    assert r50["cip_overdue"] == 0
    r100 = _cip(rows, {"P12": -100.0})
    assert r100["cip_forfeited_kg"] == 0.0 and r100["cip_early_h"] == 0.0
    assert r100["cip_overdue"] == 1


def test_cip_L_early_clean_on_an_idle_line_displaces_nothing():
    """prod 0-50 then CIP 100-106 on an idle line, seed 0: wall 100 -> early
    20 h (reported, count 1) but displaced = prod in [94,112] = 0 -> 0 kg.
    Idle time is never capacity the planner lost."""
    r = _cip([_blk("p", start=0, end=50), _blk("c", "cip", 100, 106)])
    assert r["cip_early_h"] == 20.0 and r["cip_early_count"] == 1
    assert r["cip_forfeited_h"] == 0.0 and r["cip_forfeited_kg"] == 0.0


def test_cip_overdue_inherited_is_reported_and_does_not_gate():
    """A committed manprg block (attrs current_state:running) tripping the
    clock: cip_overdue 1, cip_overdue_inherited 1, detail committed True.
    The category gate uses (overdue - inherited) = 0 -> NOT gated -> scores
    on forfeited kg: 150,000 / 1,500,000 -> 100 * (1 - 0.1) = 90.0.
    The same numbers with inherited 0 -> gate -> 0.0."""
    r = _cip([_blk("p", start=0, end=131, attrs="current_state:running")])
    assert r["cip_overdue"] == 1 and r["cip_overdue_inherited"] == 1
    assert r["cip_overdue_detail"][0]["committed"] is True

    raw = {
        "changeovers": {"cip_req_violations": 1, "cip_req_inherited": 1,
                        "weighted_co": 0.0},
        "cip": {"cip_overdue": 1, "cip_overdue_inherited": 1,
                "cip_forfeited_kg": 150000.0},
        "trials": {"available": False},
        "campaigns": {"production_blocks": 3, "avg_campaign_h": 30.0,
                      "short_campaign_count": 0},
        "service": {"available": False},
    }
    assert category_scores(raw, CFG)["cip"] == 90.0
    raw["cip"]["cip_overdue_inherited"] = 0
    assert category_scores(raw, CFG)["cip"] == 0.0
    raw["cip"]["cip_overdue_inherited"] = 1
    raw["changeovers"]["cip_req_inherited"] = 0
    assert category_scores(raw, CFG)["cip"] == 0.0


def test_cip_last_clean_hours_keeps_negative_seeds(tmp_path):
    """PreviousCIP 2026-08-15 12:00 vs anchor 08-17 00:00 = -36 h; a clean
    after the anchor stays positive; NULL absent; a seed at/after the
    horizon is dropped when max_h is given (a future 'clean' cannot excuse
    anything)."""
    (tmp_path / "cip_info.csv").write_text(
        "ID,LineEquipment,PreviousCIP,MaxHoursBetweenCIP,ScheduledCIP,Notes\n"
        "1,P12,2026-08-15 12:00,120,NULL,\n"
        "2,P13,2026-08-17 06:00,120,NULL,\n"
        "3,P14,NULL,120,NULL,\n"
        "4,P15,2026-09-10 00:00,120,NULL,\n",
        encoding="utf-8-sig")
    got = cip_last_clean_hours(tmp_path, ANCHOR, max_h=504.0)
    assert got == {"P12": -36.0, "P13": 6.0}


# ==========================================================================
# Q-3  score_service
# ==========================================================================
def _svc_demand():
    return pd.DataFrame([
        # in scope, target (900+1100)/2 = 1000
        {"order_id": "O1", "sku": "S1", "qty_min": 900.0, "qty_max": 1100.0,
         "due_start_hour": 0.0, "due_end_hour": 167.0},
        # window starts AT the 504 h horizon -> beyond scope
        {"order_id": "O2", "sku": "S2", "qty_min": 900.0, "qty_max": 1100.0,
         "due_start_hour": 504.0, "due_end_hour": 671.0},
        # in scope, never made
        {"order_id": "O3", "sku": "S3", "qty_min": 900.0, "qty_max": 1100.0,
         "due_start_hour": 0.0, "due_end_hour": 167.0},
    ])


def _svc(cal, horizon=504.0):
    return score_service(cal, CFG, _svc_demand(), None, {}, horizon_h=horizon)


def test_service_scope_stops_at_horizon_and_mo_block_credits_by_waterfall():
    """Block order_id 'MO-77' (NOT a demand id), SKU S1, 1000 kg, 150-168.
    Credit (adherence waterfall): pass 1 window [0,167] overlaps 17 of the
    18 h -> 1000*17/18 = 944.4 to O1 (cap 1100); the 55.6 remainder has no
    own order -> pool -> pass 2 earliest-due O1 (room 155.6) -> O1 = 1000.
    delivered_h = 168 (cum >= 900 at the first event). deadline = 167+1 =
    168 -> ON TIME (old rule `end > due` called it late); 168 >= 168-24 ->
    at risk (warning). Scope: O2 due_start 504 >= 504 -> beyond, so
    in_scope 2, late 1 (O3), on_time 1, on_time_kg 1000 / demand 2000 =
    50.0 %, excess 0 (1000 <= 1100), unfilled 0."""
    cal = _cal([_blk("b", start=150, end=168, sku="S1", order_id="MO-77",
                     qty_kg=1000.0)])
    s = _svc(cal)
    assert (s["orders_total"], s["orders_in_scope"], s["orders_beyond_horizon"]) == (3, 2, 1)
    assert (s["orders_late"], s["orders_on_time"], s["orders_at_risk"]) == (1, 1, 1)
    assert s["orders_unfilled"] == 0
    assert (s["on_time_kg"], s["demand_kg"], s["on_time_kg_pct"]) == (1000.0, 2000.0, 50.0)
    assert s["excess_inventory_kg"] == 0.0 and s["excess_inventory_kg_estimated"] is False
    # horizon_h None = legacy scope (every order): O2 becomes a late order
    s_all = _svc(cal, horizon=None)
    assert s_all["orders_in_scope"] == 3 and s_all["orders_late"] == 2


def test_service_deadline_is_inclusive_due_end_plus_one():
    """Same block ending 168.0 -> on time; ending 168.5 -> late, and the kg
    credited after the deadline earn no on-time kg (0.0 %)."""
    on = _svc(_cal([_blk("b", start=150, end=168.0, sku="S1",
                         order_id="O1", qty_kg=1000.0)]))
    late = _svc(_cal([_blk("b", start=150, end=168.5, sku="S1",
                           order_id="O1", qty_kg=1000.0)]))
    assert on["orders_late"] == 1 and on["orders_on_time"] == 1
    assert late["orders_late"] == 2 and late["orders_on_time"] == 0
    assert late["orders_at_risk"] == 0 and late["on_time_kg_pct"] == 0.0


def test_service_below_qty_min_is_unfilled_and_late():
    """500 kg credited against qty_min 900 -> never 'delivered': unfilled 1,
    late 2 (O1 + O3); on-time kg still counts the 500 (500/2000 = 25 %)."""
    s = _svc(_cal([_blk("b", start=0, end=10, sku="S1", order_id="O1",
                        qty_kg=500.0)]))
    assert s["orders_unfilled"] == 1 and s["orders_late"] == 2
    assert s["on_time_kg_pct"] == 25.0


def test_service_score_never_drops_when_an_order_is_delivered_just_in_time():
    """scorecard-3 monotonicity. No block: level 0 (2/2 late), kg 0 %, excess
    None -> service 0.0. Add the just-in-time block (at risk): level
    100*(1-1/2) = 50, kg 50, excess 0 -> 100 -> mean 66.7. at-risk is a
    warning, not a penalty - the score must go UP."""
    def _cats(cal):
        raw = {"changeovers": score_changeovers(cal, CFG, {}),
               "cip": score_cip(cal, CFG, {}, {}),
               "trials": {"available": False},
               "campaigns": score_campaigns(cal, CFG),
               "service": _svc(cal)}
        return category_scores(raw, CFG)
    empty = _cats(_cal([]))
    jit = _cats(_cal([_blk("b", start=150, end=168, sku="S1", order_id="O1",
                           qty_kg=1000.0)]))
    assert empty["service"] == 0.0
    assert jit["service"] == pytest.approx(66.7, abs=0.05)
    assert jit["service"] > empty["service"]


# ==========================================================================
# Q-4  recipe vs format
# ==========================================================================
def _flags(**kw):
    base = {"topload_change": 0, "ffs_change": 0, "casepacker_change": 0,
            "ttp_change": 0, "conv_to_org_change": 0, "cinn_to_non": 0,
            "added_flavors": 0, "setup_hours": 0.0, "cip_req_after": 0}
    base.update(kw)
    return base


# Five live pairs from the 2026-09-02 snapshot board (reference/changeovers.csv
# rows, read by hand), with the plant classification:
#   FORMAT = topload / FFS / casepacker retooled;  RECIPE = what is cooked or
#   mixed changes (conv<->org, cinnamon, flavor count, TTP).
LIVE_PAIRS = [
    # P09 280451 -> 280493: ttp only, added_flavors -2 -> recipe (TTP is
    # cooking; a flavor REMOVAL re-doses the mix too), not format.
    ("280451", "280493", _flags(ttp_change=1, added_flavors=-2), True, False),
    # P19 280256 -> 280351: ttp + 1 flavor, setup 0.25 -> recipe, not format.
    ("280256", "280351", _flags(ttp_change=1, added_flavors=1, setup_hours=0.25), True, False),
    # P09 280493 -> 280581: topload + casepacker + ttp -> recipe AND format.
    ("280493", "280581", _flags(topload_change=1, casepacker_change=1,
                                ttp_change=1, setup_hours=1.0), True, True),
    # P09 120448 -> 280169: cinnamon + ttp + 1 flavor -> recipe, not format.
    ("120448", "280169", _flags(ttp_change=1, cinn_to_non=1, added_flavors=1,
                                setup_hours=0.75), True, False),
    # P20 280584 -> 280344: topload + casepacker ONLY -> format, NOT recipe
    # (the old code called every one of these a recipe change).
    ("280584", "280344", _flags(topload_change=1, casepacker_change=1,
                                setup_hours=1.0), False, True),
]


@pytest.mark.parametrize("frm,to,flags,recipe,fmt", LIVE_PAIRS)
def test_recipe_format_classification_on_live_pairs(frm, to, flags, recipe, fmt):
    assert _is_recipe_change(flags, frm, to) is recipe
    assert _is_format_change(flags) is fmt


def test_recipe_format_edge_rules():
    """no row -> recipe (unknown = expensive kind), not format; row with no
    flag at all + different SKU -> recipe-only; conv_to_org alone -> recipe;
    same SKU -> neither."""
    assert _is_recipe_change(None, "A", "B") is True
    assert _is_format_change(None) is False
    assert _is_recipe_change(_flags(), "A", "B") is True
    assert _is_recipe_change(_flags(conv_to_org_change=1), "A", "B") is True
    assert _is_recipe_change(_flags(topload_change=1), "A", "A") is False
    assert _is_recipe_change(_flags(ffs_change=1), "A", "B") is False


def test_score_changeovers_counts_recipe_and_format_separately():
    """P12: A->B ffs-only (format), B->C ttp-only (recipe), C->D no row
    (recipe): transitions 3, recipe 2, format 1. total_co_hours: A->B setup
    0 -> format default 2.0 only; B->C setup 0 -> recipe default 1.0; C->D no
    row -> base 0.5 + recipe 1.0 = 1.5 -> 4.5 (old: every pair +1.0 recipe
    -> 6.5)."""
    cal = _cal([_blk("a", start=0, end=10, sku="A"), _blk("b", start=10, end=20, sku="B"),
                _blk("c", start=20, end=30, sku="C"), _blk("d", start=30, end=40, sku="D")])
    co_map = {("A", "B"): _flags(ffs_change=1), ("B", "C"): _flags(ttp_change=1)}
    co = score_changeovers(cal, CFG, co_map)
    assert co["sku_transitions"] == 3
    assert co["recipe_changes"] == 2 and co["format_changes"] == 1
    assert co["total_co_hours"] == 4.5


# ==========================================================================
# Q-5  one CIP-waiver rule everywhere
# ==========================================================================
def _waiver_rows(line_id=1, line_name="P10"):
    # S0 -> CIP -> S1 (waived), S1 -> S2 (a real transition)
    return [
        _blk("p0", start=0, end=10, sku="S0", line_id=line_id, line_name=line_name),
        _blk("c", "cip", 10, 16, line_id=line_id, line_name=line_name),
        _blk("p1", start=16, end=30, sku="S1", line_id=line_id, line_name=line_name),
        _blk("p2", start=40, end=50, sku="S2", line_id=line_id, line_name=line_name),
    ]


def test_weekly_breakdown_applies_the_cip_waiver(monkeypatch, tmp_path):
    """No standards file -> every pair is a recipe-only change. Only S1->S2
    counts: recipe_only 1, weighted_co 1.0, co_hours 0.5 + 1.0 = 1.5 (the
    old private loop counted S0->S1 too: 2 / 2.0 / 3.0). Same numbers as
    score_changeovers on the same calendar."""
    _pin_horizon(monkeypatch, ANCHOR)
    (tmp_path / "reference").mkdir()
    cal = _cal(_waiver_rows())
    df = weekly_breakdown(cal, data_dir=tmp_path, cfg=CFG)
    wk = df[df["week"] == "W34"].iloc[0]
    assert int(wk["recipe_only"]) == 1
    assert wk["weighted_co"] == 1.0 and wk["co_hours"] == 1.5
    assert int(wk["cip_count"]) == 1 and int(wk["blocks"]) == 3
    co = score_changeovers(cal, CFG, {})
    assert co["sku_transitions"] == 1 and co["transitions_at_cip"] == 1
    assert co["weighted_co"] == wk["weighted_co"]


def test_overnight_weighted_co_load_uses_the_same_waiver_and_default():
    """overnight_score v2: S0->S1 waived at the CIP (not a transition, no
    cost); S1->S2 with no standards row costs the recipe-only 1.0 (v1: 0).
    With an ffs row for S1->S2 the cost is 10; an ffs row for the WAIVED
    pair changes nothing."""
    from helpers.overnight_score import (OVERNIGHT_SCORE_VERSION, _normalize,
                                         weighted_co_load)

    assert OVERNIGHT_SCORE_VERSION == "v2"
    cal = _normalize(_cal(_waiver_rows(line_id="1", line_name="P09")))
    load, n = weighted_co_load(cal, cal.index, {})
    assert (n, load) == (1, 1.0)
    load, n = weighted_co_load(cal, cal.index, {("S1", "S2"): {"ffs_change": 1}})
    assert (n, load) == (1, 10.0)
    load, n = weighted_co_load(cal, cal.index, {("S0", "S1"): {"ffs_change": 1}})
    assert (n, load) == (1, 1.0)


# ==========================================================================
# Q-6  empty calendar / missing inputs
# ==========================================================================
def _full_data_dir(tmp_path):
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "demand_plan.csv").write_text(
        "order_id,sku,week_index,qty_target,lower_pct,upper_pct,"
        "due_start_hour,due_end_hour\nO1,S1,0,1000,0.9,1.1,0,167\n",
        encoding="utf-8")
    (ref / "line_cip_hrs.csv").write_text(
        "line_name,line_id,max_cip_hrs\nP09,9,144\n", encoding="utf-8")
    (ref / "capabilities_rates.csv").write_text(
        "line_name,line_id,sku,capable,calc_rate_kgph\nP09,9,S1,1,100\n",
        encoding="utf-8")
    (ref / "changeovers.csv").write_text(
        "from_sku,to_sku,setup_hours,ttp_change,ffs_change,topload_change,"
        "casepacker_change,conv_to_org_change,cinn_to_non,added_flavors,"
        "cip_req_after\nS1,S2,1.0,0,1,0,0,0,0,0,0\n", encoding="utf-8")
    (ref / "cip_info.csv").write_text(
        "ID,LineEquipment,PreviousCIP,MaxHoursBetweenCIP,ScheduledCIP,Notes\n"
        "1,P09,2026-08-16 00:00,144,NULL,\n", encoding="utf-8-sig")
    return tmp_path


def _one_block_board():
    return _cal([_blk("b", start=0, end=10, sku="S1", order_id="O1",
                      line_id=9, line_name="P09", qty_kg=1000.0)])


def test_empty_calendar_scores_zero_and_never_outscores_a_real_board(
        monkeypatch, tmp_path):
    """scorecard-1 / adversarial-8. Empty: service 0 (1/1 in-scope order
    late, 0 % kg), campaigns 0, changeovers / cip None (nothing to judge),
    trials None -> composite (0.30*0 + 0.15*0) / 0.45 = 0.0 (was 81.2 on
    the live data). One on-time 10 h block: service 100, changeovers 100,
    cip 100 (seed -24 h, clock 34 <= 144, nothing forfeited), campaigns
    mean(100 short, 100*10/24 = 41.7) = 70.8 -> composite > 0."""
    _pin_horizon(monkeypatch, ANCHOR)
    dd = _full_data_dir(tmp_path)
    empty = score_calendar(pd.DataFrame(columns=CALENDAR_COLUMNS),
                           week_label="e", data_dir=dd, cfg=CFG)
    assert empty.category_scores == {"changeovers": None, "cip": None,
                                     "trials": None, "campaigns": 0.0,
                                     "service": 0.0}
    assert empty.composite == 0.0
    assert empty.degraded == [] and empty.sanity == []
    assert any("empty" in n.lower() for n in empty.notes)
    board = score_calendar(_one_block_board(), week_label="b", data_dir=dd, cfg=CFG)
    assert board.category_scores["service"] == 100.0
    assert board.category_scores["cip"] == 100.0
    assert board.category_scores["campaigns"] == pytest.approx(70.8, abs=0.05)
    assert board.composite > empty.composite
    assert board.rankable is True


def test_missing_scoring_input_makes_the_category_na_and_flags_the_result(
        monkeypatch, tmp_path):
    """quality-2: without changeovers.csv the changeovers category is None
    (not the flattering 100 that zero flags produced), `degraded` names the
    file, the note says SCORING INPUT MISSING, and the result is not
    rankable - and it survives a to_dict/from_dict round trip. Without a
    rate table the CIP category is None instead of 100 (0 kg forfeited is
    not evidence)."""
    _pin_horizon(monkeypatch, ANCHOR)
    dd = _full_data_dir(tmp_path)
    board = _cal([
        _blk("a", start=0, end=10, sku="S1", order_id="O1", line_id=9,
             line_name="P09", qty_kg=1000.0),
        _blk("b", start=10, end=20, sku="S2", order_id="X", line_id=9,
             line_name="P09", qty_kg=1000.0),
    ])
    full = score_calendar(board, week_label="f", data_dir=dd, cfg=CFG)
    assert full.category_scores["changeovers"] is not None and full.rankable
    (dd / "reference" / "changeovers.csv").unlink()
    deg = score_calendar(board, week_label="d", data_dir=dd, cfg=CFG)
    assert deg.category_scores["changeovers"] is None
    assert deg.degraded == ["changeovers.csv"]
    assert any("SCORING INPUT MISSING" in n for n in deg.notes)
    assert deg.rankable is False
    again = ScorecardResult.from_dict(json.loads(json.dumps(deg.to_dict())))
    assert again.degraded == ["changeovers.csv"] and again.rankable is False

    (dd / "reference" / "capabilities_rates.csv").unlink()
    no_rates = score_calendar(board, week_label="r", data_dir=dd, cfg=CFG)
    assert no_rates.category_scores["cip"] is None
    assert any("rates" in d for d in no_rates.degraded)


# ==========================================================================
# Q-7  campaigns
# ==========================================================================
def test_campaign_metric_is_invariant_to_how_the_plan_is_drawn():
    """One 48 h block vs twelve contiguous 4 h blocks of the same SKU/order:
    both are ONE 48 h campaign -> avg_campaign_h 48, short_campaign_count 0,
    identical category score (100: 48 >= floor 24). The legacy block stats
    still differ (avg_run_h 48 vs 4) and stay reported."""
    one = _cal([_blk("b", start=0, end=48, qty_kg=4800.0)])
    twelve = _cal([_blk(f"b{i}", start=4 * i, end=4 * (i + 1), qty_kg=400.0)
                   for i in range(12)])
    c1, c12 = score_campaigns(one, CFG), score_campaigns(twelve, CFG)
    assert c1["avg_campaign_h"] == c12["avg_campaign_h"] == 48.0
    assert c1["campaign_count"] == c12["campaign_count"] == 1
    assert c1["short_campaign_count"] == c12["short_campaign_count"] == 0
    assert (c1["avg_run_h"], c12["avg_run_h"]) == (48.0, 4.0)

    def _cat(camp):
        raw = {"changeovers": {"weighted_co": 0.0}, "cip": {"cip_overdue": 0,
               "cip_forfeited_kg": 0.0}, "trials": {"available": False},
               "campaigns": camp, "service": {"available": False}}
        return category_scores(raw, CFG)["campaigns"]
    assert _cat(c1) == _cat(c12) == 100.0


def test_campaign_runs_merge_split_pieces_and_weight_by_kg():
    """P12: O1 0-10 and (after a CIP gap) 16-26 same SKU -> one 20 h campaign
    (1000+1000 kg); P13: 30 h / 6000 kg of another SKU. Plain mean =
    (20+30)/2 = 25; kg-weighted = (20*2000 + 30*6000) / 8000 = 220000/8000
    = 27.5. A block below short_run_h (4 h) inside a long campaign is NOT a
    short campaign."""
    cal = _cal([
        _blk("a1", start=0, end=10, sku="A", order_id="O1", qty_kg=1000.0),
        _blk("c", "cip", 10, 16),
        _blk("a2", start=16, end=26, sku="A", order_id="O1", qty_kg=1000.0),
        _blk("b", start=0, end=30, sku="B", order_id="O2", line_id=13,
             line_name="P13", qty_kg=6000.0),
    ])
    runs = sorted((r["hours"], r["kg"], r["blocks"]) for r in campaign_runs(cal[cal.block_type == "production"]))
    assert runs == [(20.0, 2000.0, 2), (30.0, 6000.0, 1)]
    c = score_campaigns(cal, CFG)
    assert c["avg_campaign_h"] == 25.0 and c["avg_campaign_h_kgw"] == 27.5
    short = _cal([_blk("s1", start=0, end=3, sku="A", qty_kg=1.0),
                  _blk("s2", start=3, end=6, sku="A", qty_kg=1.0)])
    cs = score_campaigns(short, CFG)
    assert cs["short_run_count"] == 2 and cs["short_campaign_count"] == 0


# ==========================================================================
# Q-8  ledger hook
# ==========================================================================
def test_residual_fill_demand_forwards_the_staging_ledger_inputs(monkeypatch):
    """The scorer must subtract with the SAME ledger legs staging used:
    completed rows, history demand, anchor, lookback. Captured kwargs must be
    the very objects passed; a None leg is not forwarded (subtract_committed
    keeps its own default)."""
    import helpers.plan_fill as pf

    seen = {}

    def _fake(demand, blocks, week_bounds=None, **kw):
        seen.update(kw)
        return demand.copy(), []
    monkeypatch.setattr(pf, "subtract_committed", _fake)
    demand = pd.DataFrame([{"order_id": "O1", "sku": "S1", "week_index": 0,
                            "qty_target": 1000.0, "lower_pct": 0.9,
                            "upper_pct": 1.1}])
    completed = [{"sku": "S1", "made_kg": 400.0}]
    hist = {("S1", 202634): 200.0}
    anchor = datetime(2026, 9, 2)
    out, dropped = _residual_fill_demand(
        demand, demand.iloc[0:0],
        {"completed": completed, "history_demand": hist, "anchor": anchor,
         "lookback_weeks": 6, "ignored": None})
    assert seen["completed"] is completed and seen["history_demand"] is hist
    assert seen["anchor"] == anchor and seen["lookback_weeks"] == 6
    assert "ignored" not in seen
    assert dropped == 0 and len(out) == 1
    # qty_min/qty_max re-derived from the (unchanged) target
    assert float(out["qty_min"].iloc[0]) == 900.0 and float(out["qty_max"].iloc[0]) == 1100.0
    seen.clear()
    _residual_fill_demand(demand, demand.iloc[0:0], None)
    assert seen == {}


# ==========================================================================
# Q-9  build_demand_targets
# ==========================================================================
def test_build_demand_targets_one_rule(tmp_path):
    """From a frame: qty_min/qty_max = target * 0.9 / 1.1 when the columns
    are absent, explicit columns win, due hours + shift_h. From a data dir:
    shift_h = demand anchor - planning anchor = 2026-09-01 - 2026-09-02 =
    -24 h (the calendar page's inline rule), so due 0/167 -> -24/143."""
    demand = pd.DataFrame([
        {"order_id": "A", "sku": "S", "qty_target": 1000.0, "lower_pct": 0.9,
         "upper_pct": 1.1, "due_start_hour": 0, "due_end_hour": 167, "week_index": 0},
        {"order_id": "B", "sku": "S", "qty_target": 1000.0, "qty_min": 950.0,
         "qty_max": 1050.0, "due_start_hour": 168, "due_end_hour": 335, "week_index": 1},
    ])
    t = build_demand_targets(demand=demand, shift_h=24.0)
    assert t[0] == {"order_id": "A", "sku": "S", "qty_min": 900.0, "qty_max": 1100.0,
                    "due_start_hour": 24.0, "due_end_hour": 191.0, "week_index": 0}
    assert t[1]["qty_min"] == 950.0 and t[1]["qty_max"] == 1050.0
    assert t[1]["due_start_hour"] == 192.0

    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "demand_plan.csv").write_text(
        "order_id,sku,qty_target,lower_pct,upper_pct,due_start_hour,due_end_hour\n"
        "A,S,1000,0.9,1.1,0,167\n", encoding="utf-8")
    (ref / "demand_plan.source.json").write_text(
        json.dumps({"anchor": "2026-09-01 00:00:00"}), encoding="utf-8")
    t2 = build_demand_targets(tmp_path, anchor=datetime(2026, 9, 2))
    assert t2[0]["due_start_hour"] == -24.0 and t2[0]["due_end_hour"] == 143.0
    assert build_demand_targets(tmp_path / "nowhere") == []


# ==========================================================================
# Q-10  sanity
# ==========================================================================
def test_calendar_sanity_flags_every_impossible_geometry():
    errs, warns = calendar_sanity(_cal([
        _blk("o1", start=0, end=10), _blk("o2", start=5, end=15),        # overlap
        _blk("rev", start=40, end=20, line_id=13, line_name="P13"),       # end < start
        _blk("neg", start=-50, end=10, line_id=14, line_name="P14"),      # negative
        _blk("zc", "cip", 100, 100, line_id=15, line_name="P15"),         # zero-length CIP
        _blk("nan", start=float("nan"), end=10, line_id=16, line_name="P16"),
        _blk("far", start=600, end=610, line_id=17, line_name="P17"),     # past horizon
        _blk("c1", "cip", 200, 206, line_id=18, line_name="P18"),
        _blk("c2", "cip", 203, 209, line_id=18, line_name="P18"),         # CIP on CIP
        _blk("dt", "line_down", 0, 1000, line_id=12, line_name="P12"),    # overlay: ignored
    ]), horizon_h=504.0)
    joined = " | ".join(errs)
    assert "1 block(s) with NaN" in joined
    assert "2 block(s) with end_h <= start_h" in joined       # rev + zero-length CIP
    assert "1 block(s) starting before hour 0" in joined
    assert "1 overlapping block pair(s)" in joined and "P12:o2" in joined
    assert len(errs) == 4
    assert any("past the 504 h horizon" in w for w in warns)
    assert any("duplicate CIP" in w for w in warns)
    assert calendar_sanity(pd.DataFrame(columns=CALENDAR_COLUMNS)) == ([], [])


def test_score_calendar_flags_an_impossible_board_and_refuses_to_rank_it(
        monkeypatch, tmp_path):
    _pin_horizon(monkeypatch, ANCHOR)
    dd = _full_data_dir(tmp_path)
    bad = _cal([_blk("a", start=0, end=10, sku="S1", order_id="O1", line_id=9,
                     line_name="P09", qty_kg=500.0),
                _blk("b", start=5, end=15, sku="S1", order_id="O1", line_id=9,
                     line_name="P09", qty_kg=500.0)])
    res = score_calendar(bad, week_label="bad", data_dir=dd, cfg=CFG)
    assert len(res.sanity) == 1 and "overlapping" in res.sanity[0]
    assert any(n.startswith("SANITY:") for n in res.notes)
    assert res.composite is not None       # numbers still shown...
    assert res.rankable is False           # ...but never ranked


# ==========================================================================
# Q-11  changeover lookup: value-identical, memoised
# ==========================================================================
def test_co_lookup_matches_the_legacy_iterrows_build_on_the_snapshot_file():
    ref = SNAPSHOT / "reference"
    if not (ref / "changeovers.csv").exists():
        pytest.skip("frozen audit snapshot not on this machine")
    df = _load_changeovers(ref)
    legacy: dict = {}
    for _, r in df.iterrows():                       # the pre-fix build
        legacy[(str(r["from_sku"]), str(r["to_sku"]))] = r.to_dict()
    new = _co_lookup(df)
    assert new.keys() == legacy.keys()
    assert len(new) > 50000
    for k, old_row in legacy.items():
        row = new[k]
        assert row.keys() == old_row.keys()
        for col, ov in old_row.items():
            nv = row[col]
            if isinstance(ov, float) and ov != ov:
                assert isinstance(nv, float) and nv != nv
            else:
                assert nv == ov, (k, col, nv, ov)


def test_load_co_map_is_memoised_per_file_version(tmp_path):
    ref = tmp_path
    p = ref / "changeovers.csv"
    p.write_text("from_sku,to_sku,setup_hours,ffs_change\nA,B,1.0,1\n", encoding="utf-8")
    m1 = load_co_map(ref)
    assert m1[("A", "B")]["ffs_change"] == 1 and float(m1[("A", "B")]["setup_hours"]) == 1.0
    assert load_co_map(ref) is m1                     # same object: no rebuild
    import os
    import time
    time.sleep(0.01)
    p.write_text("from_sku,to_sku,setup_hours,ffs_change\nA,B,2.0,0\n", encoding="utf-8")
    os.utime(p, None)
    m2 = load_co_map(ref)
    assert m2 is not m1 and float(m2[("A", "B")]["setup_hours"]) == 2.0
    assert load_co_map(tmp_path / "nowhere") == {}


# ==========================================================================
# Q-13  naive baseline frame
# ==========================================================================
def test_naive_baseline_places_inside_the_shifted_due_window(tmp_path):
    """Demand anchor 2026-08-31, planning anchor 2026-09-01 -> shift 24 h.
    Week-1 order due 168-335 in the demand frame = [144, 312) in the
    planning frame -> the block starts at 144 (old grid: 168) and runs
    1000 kg / 100 kg/h = 10 h. An order whose window starts at 600 lands
    at 576 >= the 504 h horizon -> unplaced with the frame reason."""
    from helpers.naive_baseline import build_naive_calendar

    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "demand_plan.csv").write_text(
        "order_id,sku,week_index,qty_target,due_start_hour,due_end_hour,priority\n"
        "W1,S,1,1000,168,335,1\nFAR,S,3,1000,600,767,1\n", encoding="utf-8")
    (ref / "capabilities_rates.csv").write_text(
        "line_id,line_name,sku,capable,calc_rate_kgph\n9,P09,S,1,100\n",
        encoding="utf-8")
    (ref / "demand_plan.source.json").write_text(
        json.dumps({"anchor": "2026-08-31 00:00:00"}), encoding="utf-8")
    res = build_naive_calendar(tmp_path, horizon_hours=504,
                               anchor=datetime(2026, 9, 1))
    assert len(res.placed) == 1
    blk = res.calendar.iloc[0]
    assert (float(blk["start_h"]), float(blk["end_h"])) == (144.0, 154.0)
    assert res.unplaced == [{"order_id": "FAR", "sku": "S",
                             "reason": "due window lies outside the horizon"}]
