# tests/test_partial_week_prebuild.py — [scheduler] partial_week_demand
# (planner decision 2026-09-16: "the solver can pre-build the full week 41
# into the idle tail as inventory").
#
# The due week the horizon end cuts in two (ISO-41 on the 09-16 board: staged
# [456, 503] = 48 of 168 h) is staged under one of two modes:
#   "prebuild" (default): full target, full upper band, floor pro-rated;
#   "prorate"  (fix C20): target, floor and cap pro-rated (legacy; pinned in
#                          tests/test_fix_CB.py and test_staging_integration).
#
# Every expected number is derived by hand in the comment next to it.
# 2026 ISO calendar: W38 = Sep 14-20, W39 = Sep 21-27, W40 = Sep 28-Oct 4,
# W41 = Oct 5-11, W42 = Oct 12-18.

from __future__ import annotations

import json
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from helpers import scenario_runner as sr  # noqa: E402
from helpers.demand_coverage import apply_ledger, build_ledger  # noqa: E402
from helpers.plan_fill import (  # noqa: E402
    DEFAULT_PARTIAL_WEEK_DEMAND,
    partial_week_demand_mode,
    rebase_demand,
)

MON_0914 = datetime(2026, 9, 14)   # demand file anchor (ISO 38 Monday)
WED_0916 = datetime(2026, 9, 16)   # staging anchor of the 09-16 board
WED_0923 = datetime(2026, 9, 23)   # the next run, one week later
SHIFT = 48.0                       # WED_0916 - MON_0914
H = 504.0


def _raw(rows):
    """(order_id, sku, week_index, target) in the demand file's Monday frame."""
    return pd.DataFrame([{
        "order_id": o, "sku": s, "week_index": w, "qty_target": t,
        "lower_pct": 0.9, "upper_pct": 1.1,
        "due_start_hour": 168 * w, "due_end_hour": 168 * w + 167, "priority": 3,
    } for o, s, w, t in rows])


def _staged(rows):
    """Rows already in a staging frame: (order_id, sku, week, target, ds, de)."""
    return pd.DataFrame([{
        "order_id": o, "sku": s, "week_index": w, "qty_target": t,
        "lower_pct": 0.9, "upper_pct": 1.1,
        "due_start_hour": ds, "due_end_hour": de, "priority": 3,
    } for o, s, w, t, ds, de in rows])


def _blocks(rows):
    """(sku, start_h, end_h, kg) committed production blocks."""
    return pd.DataFrame([{
        "block_id": f"b{i}", "block_type": "production", "line_id": 0,
        "line_name": "P17", "start_h": float(s), "end_h": float(e),
        "label": sku, "order_id": f"MO{i}", "sku": sku, "sku_description": "",
        "qty_kg": float(kg), "locked": False, "attrs": "current_state:queued",
    } for i, (sku, s, e, kg) in enumerate(rows)])


def _row(df, oid):
    return df[df["order_id"] == oid].iloc[0]


# ═══════════════════════════════════════════════════════════════════════════
# the mode knob
# ═══════════════════════════════════════════════════════════════════════════

def test_mode_defaults_to_prebuild_and_rejects_typos():
    assert DEFAULT_PARTIAL_WEEK_DEMAND == "prebuild"
    assert partial_week_demand_mode(None) == "prebuild"
    assert partial_week_demand_mode({}) == "prebuild"
    assert partial_week_demand_mode({"partial_week_demand": "  "}) == "prebuild"
    assert partial_week_demand_mode({"partial_week_demand": "Prorate"}) == "prorate"
    assert partial_week_demand_mode({"partial_week_demand": "prebuild"}) == "prebuild"
    with pytest.raises(ValueError, match="partial_week_demand"):
        partial_week_demand_mode({"partial_week_demand": "pre-build"})
    with pytest.raises(ValueError):
        rebase_demand(_raw([("A-W0", "A", 0, 1000)]), SHIFT, H, mode="deferred")


# ═══════════════════════════════════════════════════════════════════════════
# rebase_demand numbers, both modes
# ═══════════════════════════════════════════════════════════════════════════

def test_prebuild_keeps_full_target_and_band_and_prorates_only_the_floor():
    """Wednesday anchor (+48h): W3 raw [504, 671] -> [456, 623] -> clipped
    [456, 503] = 48 of 168 h, frac 2/7.
      qty_target 1,715,000 unchanged (the full ISO week);
      qty_min = floor(0.9 x 1,715,000 x 2/7) = floor(441,000) = 441,000;
      qty_max = ceil(1.1 x 1,715,000) = 1,886,500 (full band);
      pct blanked (data_loader would prefer them over the explicit pair);
      optional pre-build = 1,715,000 x 5/7 = 1,225,000 kg.
    FAILS on the old code: the target was pro-rated to 490,000."""
    dem = _raw([("A-W0", "A", 0, 1000), ("A-W3", "A", 3, 1_715_000)])
    out, notes = rebase_demand(dem, SHIFT, H)                 # default mode
    w3 = _row(out, "A-W3")
    assert (w3["due_start_hour"], w3["due_end_hour"]) == (456.0, 503.0)
    assert w3["qty_target"] == 1_715_000
    assert (w3["qty_min"], w3["qty_max"]) == (441_000.0, 1_886_500.0)
    assert pd.isna(w3["lower_pct"]) and pd.isna(w3["upper_pct"])
    # a week fully inside is untouched: pct kept, no explicit bounds
    w0 = _row(out, "A-W0")
    assert w0["qty_target"] == 1000 and w0["lower_pct"] == 0.9
    assert pd.isna(w0["qty_min"]) and pd.isna(w0["qty_max"])
    note = next(n for n in notes if "partly inside" in n)
    assert "PRE-BUILD" in note and "DEFERRED" not in note
    assert "week 3: 1,225,000 kg" in note
    assert "floor pro-rated to the covered hours 441,000 kg" in note
    assert "cap 1,886,500 kg" in note and "target 1,715,000 kg" in note
    # the caller's frame is never mutated
    assert dem.loc[1, "qty_target"] == 1_715_000 and dem.loc[1, "lower_pct"] == 0.9


def test_prebuild_explicit_bounds_rows_follow_the_loader_precedence():
    """B-W3: pct blank, explicit 1,400,000 / 2,000,000 -> qty_min floor(1,400,000
    x 2/7) = 400,000; qty_max 2,000,000 unscaled; target unchanged.
    C-W3: pct 0.8/1.2 AND explicit 1/2 -> data_loader reads pct, so the band
    is pct x 70,000 = 56,000 / 84,000: qty_min floor(56,000 x 2/7) = 16,000,
    qty_max ceil(84,000) = 84,000, pct blanked."""
    dem = pd.DataFrame([
        {"order_id": "B-W3", "sku": "B", "week_index": 3, "qty_target": 1_715_000,
         "lower_pct": float("nan"), "upper_pct": float("nan"),
         "qty_min": 1_400_000.0, "qty_max": 2_000_000.0,
         "due_start_hour": 504, "due_end_hour": 671, "priority": 3},
        {"order_id": "C-W3", "sku": "C", "week_index": 3, "qty_target": 70_000,
         "lower_pct": 0.8, "upper_pct": 1.2, "qty_min": 1.0, "qty_max": 2.0,
         "due_start_hour": 504, "due_end_hour": 671, "priority": 3},
    ])
    out, _ = rebase_demand(dem, SHIFT, H, mode="prebuild")
    b = _row(out, "B-W3")
    assert (b["qty_target"], b["qty_min"], b["qty_max"]) == (1_715_000, 400_000.0, 2_000_000.0)
    c = _row(out, "C-W3")
    assert (c["qty_target"], c["qty_min"], c["qty_max"]) == (70_000, 16_000.0, 84_000.0)
    assert pd.isna(c["lower_pct"]) and pd.isna(c["upper_pct"])
    # prorate on the same frame = the legacy C20 arithmetic, untouched:
    # target 1,715,000 x 2/7 = 490,000; qty_min 400,000; qty_max 2,000,000 x 2/7
    # = 571,428.57 -> 571,428.6; pct columns left as they were
    leg, notes = rebase_demand(dem, SHIFT, H, mode="prorate")
    b = _row(leg, "B-W3")
    assert (b["qty_target"], b["qty_min"], b["qty_max"]) == (490_000.0, 400_000.0, 571_428.6)
    assert _row(leg, "C-W3")["lower_pct"] == 0.8
    assert any("DEFERRED" in n for n in notes)


def test_prorate_adds_no_bound_columns_prebuild_does():
    """Legacy frame shape: a pct-only demand file stays pct-only under
    prorate (C20 never wrote qty_min/qty_max for pct rows)."""
    dem = _raw([("A-W0", "A", 0, 1000), ("A-W3", "A", 3, 1_715_000)])
    leg, _ = rebase_demand(dem, SHIFT, H, mode="prorate")
    assert "qty_min" not in leg.columns and "qty_max" not in leg.columns
    assert _row(leg, "A-W3")["qty_target"] == 490_000.0            # x 2/7
    assert _row(leg, "A-W3")["lower_pct"] == 0.9
    pb, _ = rebase_demand(dem, SHIFT, H, mode="prebuild")
    assert {"qty_min", "qty_max"} <= set(pb.columns)


def test_prebuild_survives_int64_columns():
    """pandas 3 raises on NaN (or a fraction) written into an int64 column —
    the 2026-08-14 staging crash. pct 1/1 as int64 and int64 explicit
    columns: band = 1 x 1,715,000; qty_min floor(1,715,000 x 2/7) = 490,000,
    qty_max ceil(1,715,000) = 1,715,000; pct blanked without raising."""
    dem = pd.DataFrame({
        "order_id": ["A-W0", "A-W3"], "sku": ["A", "A"], "week_index": [0, 3],
        "qty_target": [1000, 1_715_000], "lower_pct": [1, 1], "upper_pct": [1, 1],
        "qty_min": [1000, 1_715_000], "qty_max": [1000, 1_715_000],
        "due_start_hour": [0, 504], "due_end_hour": [167, 671], "priority": [3, 3]})
    assert str(dem["lower_pct"].dtype) == "int64" and str(dem["qty_min"].dtype) == "int64"
    out, _ = rebase_demand(dem, SHIFT, H)
    w3 = _row(out, "A-W3")
    assert (w3["qty_min"], w3["qty_max"]) == (490_000.0, 1_715_000.0)
    assert pd.isna(w3["lower_pct"])
    assert _row(out, "A-W0")["lower_pct"] == 1.0


def test_prebuild_leaves_a_malformed_band_for_the_loader_to_refuse():
    """lower_pct 1.2 > upper_pct 1.1 on the cut week: data_loader must still
    raise naming the row (fix SA-12). Pro-rating the floor (0.34 x target <
    1.1 x target) and blanking pct would have let it load silently."""
    from data_loader import Data, Params

    dem = _raw([("A-W3", "A", 3, 70_000)]).assign(lower_pct=1.2)
    out, _ = rebase_demand(dem, SHIFT, H)
    w3 = _row(out, "A-W3")
    assert (w3["lower_pct"], w3["upper_pct"]) == (1.2, 1.1) and pd.isna(w3["qty_min"])
    with pytest.raises(ValueError, match="lower_pct=1.2 > upper_pct=1.1"):
        Data._parse_demand(SimpleNamespace(P=Params(horizon_h=504)), out)
    bad = pd.DataFrame([{"order_id": "B-W3", "sku": "B", "week_index": 3,
                         "qty_target": 1000, "qty_min": 900.0, "qty_max": 800.0,
                         "due_start_hour": 504, "due_end_hour": 671}])
    out, _ = rebase_demand(bad, SHIFT, H)
    assert (_row(out, "B-W3")["qty_min"], _row(out, "B-W3")["qty_max"]) == (900.0, 800.0)


# ═══════════════════════════════════════════════════════════════════════════
# the staged rows as the solver reads them
# ═══════════════════════════════════════════════════════════════════════════

def test_staged_prebuild_rows_parse_to_prorated_floor_and_full_band(tmp_path):
    """CSV round trip (what _stage_time_frame writes) through
    data_loader._parse_demand: blank pct -> the explicit pair wins.
      A-W3: qmin 441,000 (floor), qmax 1,886,500 (full band), target 1,715,000.
      A-W2 (fully inside, pct row): qmin floor(0.9 x 20,000) = 18,000."""
    from data_loader import Data, Params

    dem = _raw([("A-W2", "A", 2, 20_000), ("A-W3", "A", 3, 1_715_000)])
    out, _ = rebase_demand(dem, SHIFT, H)
    p = tmp_path / "demand_plan.csv"
    out.to_csv(p, index=False)
    back = pd.read_csv(p, dtype={"sku": str})
    orders = {o["order_id"]: o for o in Data._parse_demand(
        SimpleNamespace(P=Params(horizon_h=504)), back)}
    w3 = orders["A-W3"]
    assert (w3["qty_min"], w3["qty_max"], w3["qty_target"]) == (441_000, 1_886_500, 1_715_000)
    assert (w3["due_start"], w3["due_end"]) == (456, 503)
    assert orders["A-W2"]["qty_min"] == 18_000


def test_netting_on_a_prebuild_row_is_band_minus_credit():
    """Staging frame = WED_0916. A committed block of A at [460, 480] =
    Oct 5 04:00-20:00 = ISO 41 credits the W41 row (carry forward only, so
    A-W2 in ISO 40 gets nothing).
      100,000 kg: target 1,715,000-100,000 = 1,615,000; qty_min
        441,000-100,000 = 341,000; qty_max 1,886,500-100,000 = 1,786,500.
      500,000 kg: qty_min max(0, 441,000-500,000) = 0; qty_max 1,386,500;
        target 1,215,000."""
    dem, _ = rebase_demand(_raw([("A-W2", "A", 2, 20_000), ("A-W3", "A", 3, 1_715_000)]),
                           SHIFT, H)
    led = build_ledger(dem, _blocks([("A", 460, 480, 100_000)]), anchor=WED_0916)
    out, _ = apply_ledger(dem, led, explicit_bounds=True)
    w3 = _row(out, "A-W3")
    assert (w3["qty_target"], w3["qty_min"], w3["qty_max"]) == (1_615_000.0, 341_000.0, 1_786_500.0)
    assert w3["credit_kg"] == 100_000.0 and pd.isna(w3["lower_pct"])
    assert _row(out, "A-W2")["qty_target"] == 20_000.0
    led = build_ledger(dem, _blocks([("A", 460, 480, 500_000)]), anchor=WED_0916)
    out, _ = apply_ledger(dem, led, explicit_bounds=True)
    w3 = _row(out, "A-W3")
    assert (w3["qty_target"], w3["qty_min"], w3["qty_max"]) == (1_215_000.0, 0.0, 1_386_500.0)


# ═══════════════════════════════════════════════════════════════════════════
# ledger follow-through: pre-built kg credits the week it was built for
# ═══════════════════════════════════════════════════════════════════════════

def test_prebuild_committed_next_run_nets_the_week_it_was_built_for():
    """The NEXT run (anchor WED_0923): the 09-16 fill pre-built 30 t of X for
    ISO 41 into ISO 40 idle time and the plant committed it as an MO.
    Staging frame: W40 = [120, 287], W41 = [288, 455].
      X-W40 gross 10,000; X-W41 gross 56,000.
      MO1 X [150, 170] 10,000 kg (W40's own) + MO2 X [200, 230] 30,000 kg
      (Oct 1 08:00-Oct 2 14:00, ISO 40, the pre-build).
      W40: supply 40,000, applied 10,000, carry 30,000 -> W41 applied 30,000.
      W41 residual 26,000; bounds 0.9x56,000-30,000 = 20,400 /
      1.1x56,000-30,000 = 31,600: netted, not re-planned."""
    dem = _staged([("X-W40", "X", 1, 10_000, 120, 287),
                   ("X-W41", "X", 2, 56_000, 288, 455)])
    blocks = _blocks([("X", 150, 170, 10_000), ("X", 200, 230, 30_000)])
    led = build_ledger(dem, blocks, anchor=WED_0923)
    rows = {r.order_id: r for r in led.rows}
    assert rows["X-W40"].applied_kg == 10_000.0
    assert rows["X-W41"].carry_in_kg == 30_000.0 and rows["X-W41"].applied_kg == 30_000.0
    out, _ = apply_ledger(dem, led, explicit_bounds=True)
    w41 = _row(out, "X-W41")
    assert (w41["qty_target"], w41["qty_min"], w41["qty_max"]) == (26_000.0, 20_400.0, 31_600.0)
    assert _row(out, "X-W40")["qty_target"] == 0.0     # dropped by _overlay_fill
    assert not led.overcommitted
    # a week with NO demand row for X carries the same way (keys include
    # every week that has supply)
    led2 = build_ledger(dem[dem["order_id"] == "X-W41"],
                        _blocks([("X", 200, 230, 30_000)]), anchor=WED_0923)
    assert led2.rows[0].applied_kg == 30_000.0 and led2.rows[0].net_kg == 26_000.0


def test_surplus_beyond_week_k_nets_the_partial_prebuild_week_only_under_prebuild():
    """Same run as the 09-16 board (anchor WED_0916): X-W2 raw [336, 503] ->
    [288, 455] (ISO 40) gross 10,000; X-W3 -> [456, 503] (ISO 41) gross
    56,000. Committed X [300, 340] 40,000 kg in ISO 40: 10,000 to W40, 30,000
    carries to W41.
      prebuild: W41 gross 56,000 absorbs 30,000 -> target 26,000; qty_min
        max(0, floor(50,400 x 2/7) = 14,400 - 30,000) = 0; qty_max 61,600 -
        30,000 = 31,600. Nothing is left over.
      prorate: W41 gross 56,000 x 2/7 = 16,000 absorbs 16,000 -> target 0;
        14,000 kg surplus is absorbed by no staged week and the SKU reads
        OVERCOMMITTED (14,000 > max(10% x 26,000, 1,000))."""
    raw = _raw([("X-W2", "X", 2, 10_000), ("X-W3", "X", 3, 56_000)])
    blocks = _blocks([("X", 300, 340, 40_000)])
    pb, _ = rebase_demand(raw, SHIFT, H, mode="prebuild")
    led = build_ledger(pb, blocks, anchor=WED_0916)
    out, _ = apply_ledger(pb, led, explicit_bounds=True)
    w3 = _row(out, "X-W3")
    assert (w3["qty_target"], w3["qty_min"], w3["qty_max"]) == (26_000.0, 0.0, 31_600.0)
    assert not led.overcommitted
    pr, _ = rebase_demand(raw, SHIFT, H, mode="prorate")
    led_pr = build_ledger(pr, blocks, anchor=WED_0916)
    out_pr, _ = apply_ledger(pr, led_pr, explicit_bounds=True)
    assert _row(out_pr, "X-W3")["qty_target"] == 0.0
    assert led_pr.overcommitted["X"]["surplus_kg"] == 14_000.0


def test_prebuild_made_this_week_counts_but_c54_holds_back_last_weeks():
    """Completed-MO leg (kg already MADE), anchor WED_0923 (ISO 39):
      made Mon Sep 21 (ISO 39 = the anchor week), no W39 row for X ->
        not past, carries: X-W41 (56,000) applied 30,000.
      made Thu Sep 17 (ISO 38, BEFORE the anchor week), no live or history
        row for (X, W38) -> C54 quarantine: NOT carried, WARNING note. A
        pre-build finished in a past week is re-planned unless the demand
        history still carries a W38 row for X (then it settles there first
        and the surplus carries). This pins the documented C54 limit the
        pre-build follow-through inherits."""
    dem = _staged([("X-W41", "X", 2, 56_000, 288, 455)])
    this_week = [{"item": "X", "mo": "M1", "start_dt": pd.Timestamp(2026, 9, 21, 6),
                  "hours": 20.0, "qty_kg": 30_000.0, "made_kg": 30_000.0}]
    led = build_ledger(dem, None, completed=this_week, anchor=WED_0923)
    assert led.rows[0].applied_kg == 30_000.0
    last_week = [{**this_week[0], "start_dt": pd.Timestamp(2026, 9, 17, 6)}]
    led = build_ledger(dem, None, completed=last_week, anchor=WED_0923)
    assert led.rows[0].applied_kg == 0.0
    assert led.unsettled_past == {("X", 202638): 30_000.0}
    assert any(n.startswith("WARNING") for n in led.notes)
    # with a W38 history row (5,000 kg) the made kg settles there first
    led = build_ledger(dem, None, completed=last_week, anchor=WED_0923,
                       history_demand={("X", 202638): 5_000.0})
    assert led.rows[0].applied_kg == 25_000.0


# ═══════════════════════════════════════════════════════════════════════════
# consumers of the staged rows: stock policy
# ═══════════════════════════════════════════════════════════════════════════

def test_stock_policy_caps_a_prebuild_row_on_the_full_week():
    """The stock report grades the GROSS (full-week) row, so the cap belongs
    on the full target: prebuild A-W3 (explicit, no credit):
      flat DNS 0.5: qty_max min(1,886,500, 0.5 x 1,715,000) = 857,500, qty_min 0;
      projected cap 600,000 < qty_max: qty_max 600,000, qty_min 0.
    Both leave the row explicit (pct stays blank)."""
    from helpers.agent_policy import trim_dns_demand, trim_projected_demand

    dem, _ = rebase_demand(_raw([("A-W2", "A", 2, 20_000), ("A-W3", "A", 3, 1_715_000)]),
                           SHIFT, H)
    out, _ = trim_dns_demand(dem, {"A": 0.5})
    w3 = _row(out, "A-W3")
    assert (w3["qty_min"], w3["qty_max"]) == (0.0, 857_500.0)
    assert pd.isna(w3["lower_pct"])
    assert (_row(out, "A-W2")["lower_pct"], _row(out, "A-W2")["upper_pct"]) == (0.0, 0.5)
    caps = {"A-W3": {"cap_kg": 600_000.0, "status": "LIFTED", "sku": "A", "week_index": 3}}
    out, _ = trim_projected_demand(dem, caps)
    w3 = _row(out, "A-W3")
    assert (w3["qty_min"], w3["qty_max"]) == (0.0, 600_000.0)
    assert w3["qty_target"] == 1_715_000


@pytest.mark.parametrize("status,board,cap,allowed", [
    # DNS: board 20 t + headroom 15 t = 35 t on both rows (pct row 0.7 x 50 t)
    ("DNS", 20_000.0, 15_000.0, 35_000.0),
    # COVERED: yesterday's promoted pre-build (50 t) + 3 t head = 53 t
    ("COVERED", 50_000.0, 3_000.0, 53_000.0),
])
def test_projected_cap_on_an_uncredited_prebuild_row_includes_the_board(status, board,
                                                                       cap, allowed):
    """Review fix PR-1. The pre-build blanks pct on the clipped W3 row
    without netting anything (no ledger credit), so the row is still GROSS:
    the board's own kg in the week come on top of the headroom, exactly as on
    the identical pct row X-W2. Before the fix the blank pct sent X-W3 down
    the netted path and capped it at the headroom alone (15,000 / 3,000 kg),
    so a promoted pre-build could only be rebuilt 3 t of 50 t on the next
    run. A row the ledger really credited stays capped on its net target."""
    from helpers.agent_policy import trim_projected_demand

    raw = _raw([("X-W2", "X", 2, 50_000), ("X-W3", "X", 3, 50_000)])
    dem, _ = rebase_demand(raw, SHIFT, H, mode="prebuild")
    dem, _ = apply_ledger(dem, build_ledger(dem, None), explicit_bounds=True)
    assert pd.isna(_row(dem, "X-W3")["lower_pct"])            # explicit, blank pct
    assert pd.notna(_row(dem, "X-W2")["lower_pct"])           # still a pct row
    rec = {"status": status, "board_kg": board, "cap_kg": cap, "sku": "X"}
    caps = {"X-W2": {**rec, "week_index": 2}, "X-W3": {**rec, "week_index": 3}}
    out, notes = trim_projected_demand(dem, caps)
    w2, w3 = _row(out, "X-W2"), _row(out, "X-W3")
    assert (w2["lower_pct"], w2["qty_target"] * w2["upper_pct"]) == (0.0, allowed)
    assert (w3["qty_min"], w3["qty_max"]) == (0.0, allowed)
    assert pd.isna(w3["lower_pct"])

    # A credited netted row ignores board_kg: capped on its net target.
    netted = pd.DataFrame([{
        "order_id": "N-W0", "sku": "N", "week_index": 0, "qty_target": 6926.7,
        "lower_pct": float("nan"), "upper_pct": float("nan"),
        "qty_min": 4606.9, "qty_max": 9246.5, "credit_kg": 16271.3,
        "due_start_hour": 0, "due_end_hour": 119, "priority": 3}])
    out, _ = trim_projected_demand(netted, {"N-W0": {
        "status": status, "board_kg": 16_000.0, "cap_kg": 1127.2, "sku": "N",
        "week_index": 0}})
    assert (out.iloc[0]["qty_min"], out.iloc[0]["qty_max"]) == (0.0, 1127.2)


# ═══════════════════════════════════════════════════════════════════════════
# _stage_time_frame: mode read from the STAGED work toml
# ═══════════════════════════════════════════════════════════════════════════

_TOML = textwrap.dedent("""\
    [scheduler]
    planning_start_date = "2026-09-14 00:00:00"
    anchor_mode = "today"
    horizon_weeks = 3
    horizon_hours = {hours}
    time_limit = 60
    {extra}
    """)


def _stage(tmp_path, now, *, extra="", hours=504, rows=None):
    from helpers import horizon as hzmod

    dd = tmp_path / "plant"
    (dd / "reference").mkdir(parents=True)
    (dd / "reference" / "demand_plan.source.json").write_text(json.dumps(
        {"source": "demand_plan_summary.csv", "anchor": "2026-09-14 00:00:00"}),
        encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "flowstate.toml").write_text(
        _TOML.format(hours=hours, extra=extra), encoding="utf-8")
    raw = _raw(rows or [("A-W0", "A", 0, 1000), ("A-W2", "A", 2, 20_000),
                        ("A-W3", "A", 3, 1_715_000)])
    raw.to_csv(work / "demand_plan.csv", index=False)
    cfg = {"scheduler": {"planning_start_date": "2026-09-14 00:00:00",
                         "anchor_mode": "today", "horizon_hours": hours}}
    h = hzmod.resolve(cfg, now=now)
    notes = sr._stage_time_frame(work, dd, h)
    return pd.read_csv(work / "demand_plan.csv", dtype={"sku": str}), notes


def test_stage_time_frame_defaults_to_prebuild_when_the_key_is_absent(tmp_path):
    """Wed 09-16 10:00 -> anchor 09-16, shift +48: A-W3 keeps 1,715,000."""
    dem, notes = _stage(tmp_path, datetime(2026, 9, 16, 10))
    w3 = _row(dem, "A-W3")
    assert w3["qty_target"] == 1_715_000
    assert (w3["qty_min"], w3["qty_max"]) == (441_000.0, 1_886_500.0)
    assert pd.isna(w3["lower_pct"])
    assert any("PRE-BUILD" in n for n in notes)


def test_stage_time_frame_honours_prorate_in_the_work_toml(tmp_path):
    dem, notes = _stage(tmp_path, datetime(2026, 9, 16, 10),
                        extra='partial_week_demand = "prorate"')
    w3 = _row(dem, "A-W3")
    assert w3["qty_target"] == 490_000.0                       # x 48/168
    assert w3["lower_pct"] == 0.9
    assert any("1,225,000 kg DEFERRED" in n for n in notes)


def test_stage_time_frame_refuses_an_unknown_mode(tmp_path):
    with pytest.raises(sr.StagingError, match="partial_week_demand"):
        _stage(tmp_path, datetime(2026, 9, 16, 10),
               extra='partial_week_demand = "later"')


def test_stage_time_frame_rebases_on_a_known_zero_shift(tmp_path):
    """Monday 09-14 (the import day): staging anchor == demand anchor, shift
    0. W3 raw [504, 671] starts AT the 504 h horizon: dropped (the old
    `if shift_h:` skip staged it untouched, and with unbounded early fill
    the model gave it the window [0, 504) — 136 rows / 7.28 Mt on the
    09-14 file). W0 [0, 167] and W2 [336, 503] are untouched."""
    dem, notes = _stage(tmp_path, datetime(2026, 9, 14, 10))
    assert set(dem["order_id"]) == {"A-W0", "A-W2"}
    assert _row(dem, "A-W2")["qty_target"] == 20_000
    assert any("due week starts beyond the horizon" in n for n in notes)
    assert not any("re-based" in n for n in notes)            # no shift reported


def test_stage_time_frame_zero_shift_partial_week_follows_the_mode(tmp_path):
    """Zero shift, horizon_hours 480: W2 [336, 503] is cut at 479 -> 144 of
    168 h. prebuild: target 20,000, qty_min floor(18,000 x 144/168) =
    floor(15,428.57) = 15,428, qty_max ceil(22,000) = 22,000; W3 dropped."""
    dem, _ = _stage(tmp_path, datetime(2026, 9, 14, 10), hours=480)
    w2 = _row(dem, "A-W2")
    assert (w2["due_start_hour"], w2["due_end_hour"]) == (336.0, 479.0)
    assert (w2["qty_target"], w2["qty_min"], w2["qty_max"]) == (20_000, 15_428.0, 22_000.0)
    assert "A-W3" not in set(dem["order_id"])


def test_stage_time_frame_leaves_an_unanchored_demand_file_alone(tmp_path):
    """No anchor in demand_plan.source.json = unknown frame: the file is not
    re-based or rewritten (legacy behaviour kept)."""
    from helpers import horizon as hzmod

    dd = tmp_path / "plant"
    (dd / "reference").mkdir(parents=True)
    (dd / "reference" / "demand_plan.source.json").write_text("{}", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "flowstate.toml").write_text(_TOML.format(hours=504, extra=""), encoding="utf-8")
    raw = _raw([("A-W3", "A", 3, 1_715_000)])
    raw.to_csv(work / "demand_plan.csv", index=False)
    before = (work / "demand_plan.csv").read_bytes()
    h = hzmod.resolve({"scheduler": {"anchor_mode": "today", "horizon_hours": 504}},
                      now=datetime(2026, 9, 16, 10))
    sr._stage_time_frame(work, dd, h)
    assert (work / "demand_plan.csv").read_bytes() == before


def test_overlay_fill_stages_a_prebuild_week_the_solver_loads(tmp_path, monkeypatch):
    """End to end on test_staging_integration's sandbox with the key REMOVED
    from its toml (so the default applies): staging anchor Tue 2027-03-02,
    shift +24 h. 444-W3 raw [504, 671] -> [480, 647] -> clipped [480, 503]:
    24 of 168 h, frac 1/7.
      qty_target 1,715,000 (full week); qty_min floor(1,543,500 / 7) =
      220,500; qty_max ceil(1,886,500) = 1,886,500; no credit, pct blank;
      optional pre-build 1,715,000 x 6/7 = 1,470,000 kg.
    data_loader reads it back as qmin 220,500 / qmax 1,886,500 and the model
    builds."""
    import tomllib

    import phase2_scheduler as p2
    import test_staging_integration as tsi
    from data_loader import Data, Files
    from helpers import horizon as hzmod
    from model_builder import build_model

    real = hzmod.resolve
    monkeypatch.setattr(hzmod, "resolve", lambda cfg=None, now=None: real(cfg, now=tsi.NOW))
    dd = tsi.make_sandbox(tmp_path)
    root_toml = dd.parent / "flowstate.toml"
    text = root_toml.read_text(encoding="utf-8")
    root_toml.write_text("\n".join(ln for ln in text.splitlines()
                                   if "partial_week_demand" not in ln) + "\n",
                         encoding="utf-8")
    work = dd / "_scenario_work" / "F"
    sr._prepare_work_dir(dd, work)
    notes = sr._overlay_fill(work, dd)
    log = "\n".join(notes)
    assert "PRE-BUILD" in log and "week 3: 1,470,000 kg" in log and "DEFERRED" not in log
    dem = pd.read_csv(work / "demand_plan.csv", dtype={"sku": str}).set_index("order_id")
    r = dem.loc["444-W3"]
    assert (r["due_start_hour"], r["due_end_hour"]) == (480.0, 503.0)
    assert (r["qty_target"], r["qty_min"], r["qty_max"]) == (1_715_000.0, 220_500.0, 1_886_500.0)
    assert pd.isna(r["lower_pct"]) and pd.isna(r["upper_pct"])
    cfg = tomllib.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    P = p2.params_from_config(cfg)
    data = Data(P, Files(work))
    data.load()
    o = next(o for o in data.orders if o["order_id"] == "444-W3")
    assert (o["qty_min"], o["qty_max"], o["qty_target"]) == (220_500, 1_886_500, 1_715_000)
    model, _v = build_model(P, data, "full", False, False, maximize_production=True)
    assert len(model.Proto().variables) > 0


# ═══════════════════════════════════════════════════════════════════════════
# scorecard: the service scope judges the week the solver was given
# ═══════════════════════════════════════════════════════════════════════════

def test_scorecard_service_scope_matches_the_prebuild_staging(tmp_path, monkeypatch):
    """score_calendar's load_demand reads the REFERENCE file, shifts it into
    the planning frame and scores every row whose due window starts inside
    the horizon — ISO-41 at its FULL week: midpoint target (0.9 + 1.1)/2 x
    1,715,000 = 1,715,000, qty_max 1.1 x 1,715,000. The prebuild staging
    hands the solver the same 1,715,000 target and 1,886,500 cap; prorate
    handed it 490,000 = 2/7 of what the scorecard judged."""
    import helpers.config as cfgmod
    from helpers import horizon as hzmod
    from helpers.scorecard_engine import load_demand, score_service

    cfg = {"scheduler": {"planning_start_date": "2026-09-14 00:00:00",
                         "anchor_mode": "today", "horizon_hours": 504}}
    real = hzmod.resolve
    monkeypatch.setattr(hzmod, "resolve",
                        lambda c=None, now=None: real(cfg, now=datetime(2026, 9, 16, 10)))
    monkeypatch.setattr(cfgmod, "load_toml", lambda *a, **k: cfg)
    ref = tmp_path / "reference"
    ref.mkdir()
    raw = _raw([("A-W3", "A", 3, 1_715_000)])
    raw.to_csv(ref / "demand_plan.csv", index=False)
    (ref / "demand_plan.source.json").write_text(
        json.dumps({"anchor": "2026-09-14 00:00:00"}), encoding="utf-8")
    sc_dem = load_demand(ref)
    empty = pd.DataFrame(columns=["block_type", "order_id", "sku", "line_name",
                                  "line_id", "start_h", "end_h", "qty_kg"])
    svc = score_service(empty, {"at_risk_h": 24.0}, sc_dem, None, {},
                        horizon_h=H, caps={})
    assert svc["orders_in_scope"] == 1
    staged, _ = rebase_demand(raw, SHIFT, H, mode="prebuild")
    w3 = _row(staged, "A-W3")
    assert svc["demand_kg"] == pytest.approx(float(w3["qty_target"]), abs=0.5)
    assert float(sc_dem.iloc[0]["qty_max"]) == pytest.approx(w3["qty_max"], abs=1.0)
    legacy, _ = rebase_demand(raw, SHIFT, H, mode="prorate")
    assert _row(legacy, "A-W3")["qty_target"] == pytest.approx(svc["demand_kg"] * 2 / 7, abs=0.5)
