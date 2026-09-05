# tests/test_current_state.py — ground-truth calendar from manprg + cip_info.

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.cip_import import CipInfo, CipInfoResult  # noqa: E402
from helpers.current_state import (  # noqa: E402
    QUEUED,
    RUNNING,
    build_current_state,
    classify_rows,
    cip_interval_for,
    line_id_for,
    project_cips,
)
from helpers.horizon import Horizon  # noqa: E402
from helpers.manprg_import import ManprgResult, read_manprg  # noqa: E402

NOW = datetime(2026, 8, 10, 12, 0)
ANCHOR = datetime(2026, 8, 10, 0, 0)


def _hz(hours: int = 504) -> Horizon:
    return Horizon(anchor=ANCHOR, start=ANCHOR, end=ANCHOR + timedelta(hours=hours),
                   now=NOW, mode="today", hours=hours, config_anchor=ANCHOR)


def _frame(rows: list[dict]) -> pd.DataFrame:
    base = {"line": "P09", "mo": "1", "item": "280581", "designation": "D",
            "hours": 10.0, "fct_cas": 100.0, "made_cas": None, "left_cas": 100.0,
            "fct_kg": 5000.0, "start_dt": pd.Timestamp(NOW)}
    return pd.DataFrame([{**base, **r} for r in rows])


# --------------------------------------------------------------- classify

def test_classify_blank_made_is_queued_not_zero_production():
    rows = classify_rows(_frame([{"mo": "A", "made_cas": ""}]))
    assert rows[0]["kind"] == QUEUED
    assert rows[0]["started"] is False


def test_classify_partial_made_is_running():
    rows = classify_rows(_frame([{"mo": "A", "made_cas": 40.0, "left_cas": 60.0}]))
    assert rows[0]["kind"] == RUNNING
    assert rows[0]["completion_pct"] == 40.0


def test_classify_full_made_is_completed():
    rows = classify_rows(_frame([{"mo": "A", "made_cas": 100.0, "left_cas": 0.0}]))
    assert rows[0]["kind"] == "completed"


def test_classify_cip_pseudo_mo_is_never_production():
    rows = classify_rows(_frame([{"mo": "A", "item": "CIP", "fct_cas": 1042.0}]))
    assert rows[0]["kind"] == "cip"


def test_classify_strips_float_suffix_from_codes():
    rows = classify_rows(_frame([{"mo": "29901.0", "item": "280351.0"}]))
    assert rows[0]["mo"] == "29901" and rows[0]["item"] == "280351"


# --------------------------------------------------------------- placement

def _state(rows, cips=None, cfg=None, hz=None, caps=None):
    mp = ManprgResult(frame=_frame(rows))
    return build_current_state(hz or _hz(), manprg=mp,
                               cips=cips or CipInfoResult(), cfg=cfg, now=NOW,
                               caps=caps)


def test_completed_mos_are_counted_but_not_drawn():
    # User decision 2026-08-14 (revised): completed MOs stay OFF the board —
    # counted in state.completed (rn=1 semantics, same as the planner's SQL)
    # but never emitted as blocks.
    st = _state([{"mo": "DONE", "made_cas": 100.0, "left_cas": 0.0},
                 {"mo": "NEXT", "made_cas": ""}])
    assert "DONE" not in set(st.blocks["order_id"])
    assert "NEXT" in set(st.blocks["order_id"])
    assert len(st.completed) == 1


def test_running_mo_is_locked_and_never_ends_in_the_past():
    st = _state([{"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=40)}])
    blk = st.blocks[st.blocks["order_id"] == "RUN"].iloc[0]
    assert bool(blk["locked"]) is True
    assert blk["end_h"] > (NOW - ANCHOR).total_seconds() / 3600.0


def test_running_mo_started_before_the_anchor_is_clamped_into_the_window():
    st = _state([{"mo": "OLD", "made_cas": 50.0, "left_cas": 50.0, "hours": 200.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=190)}])
    blk = st.blocks[st.blocks["order_id"] == "OLD"].iloc[0]
    assert blk["start_h"] >= 0.0, "block must render inside the horizon"
    assert "clamped_to_anchor" in blk["attrs"]
    assert "started=" in blk["attrs"], "true start must be preserved"


def test_queued_mos_are_sequential_and_do_not_overlap():
    st = _state([
        {"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0, "hours": 10.0},
        {"mo": "Q1", "made_cas": "", "hours": 8.0,
         "start_dt": pd.Timestamp(NOW) + timedelta(hours=1)},
        {"mo": "Q2", "made_cas": "", "hours": 6.0,
         "start_dt": pd.Timestamp(NOW) + timedelta(hours=2)},
    ])
    prod = st.blocks[st.blocks["block_type"] == "production"].sort_values("start_h")
    starts = list(prod["start_h"])
    ends = list(prod["end_h"])
    for i in range(1, len(prod)):
        assert starts[i] >= ends[i - 1] - 1e-6, "queued MO overlaps its predecessor"
    assert bool(prod[prod["order_id"] == "Q1"].iloc[0]["locked"]) is False


def test_queued_mo_never_starts_before_now():
    st = _state([{"mo": "STALE", "made_cas": "",
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=30)}])
    blk = st.blocks.iloc[0]
    assert blk["start_h"] >= (NOW - ANCHOR).total_seconds() / 3600.0 - 1e-6


def test_line_free_hour_is_after_the_last_placed_block():
    st = _state([{"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0, "hours": 10.0}])
    free = st.line_free_h["P09"]
    prod = st.blocks[st.blocks["block_type"] == "production"]
    assert free >= prod["end_h"].max() - 1e-6


def test_two_started_mos_on_a_line_keep_only_the_latest_as_running():
    st = _state([
        {"mo": "OLD", "made_cas": 10.0, "left_cas": 90.0,
         "start_dt": pd.Timestamp(NOW) - timedelta(hours=50)},
        {"mo": "NEW", "made_cas": 20.0, "left_cas": 80.0,
         "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)},
    ])
    assert [r["mo"] for r in st.running] == ["NEW"]
    assert any("superseded" in w for w in st.warnings)


# ----------------------------------------- running-MO end re-forecast
# actual_rate = cases made / hours since start; end = now + left / rate.
# The base fixture plans 100 cas over 10h with 5000 kg, so kg/cas = 50 and
# a 500 kg/h catalog rate is exactly the planned 10 cas/h.

def _caps(rate_kgph: float = 500.0, sku: str = "280581",
          line: str = "P09") -> pd.DataFrame:
    return pd.DataFrame([{"line_id": 0, "sku": sku, "line_name": line,
                          "capable": 1, "calc_rate_kgph": rate_kgph}])


def _run_blk(st):
    return st.blocks[st.blocks["attrs"].str.contains("current_state:running")].iloc[0]


def test_slow_line_pushes_reforecast_end_later():
    # 25 cas in 5h = 5 cas/h (half the planned 10): 75 left -> +15h from now.
    st = _state([{"mo": "RUN", "made_cas": 25.0, "left_cas": 75.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}],
                caps=_caps())
    blk = _run_blk(st)
    assert blk["end_h"] == pytest.approx(27.0)      # now(12) + 75/5
    # manprg's pro-rata estimate said now + 7.5h = 19.5 — kept for honesty
    assert st.running[0]["manprg_end"] == pd.Timestamp(NOW) + timedelta(hours=7.5)
    assert "reforecast=" in blk["attrs"]
    # wording updated 2026-09-03 (fix CA-9 / audit C07): pace 0.5 used to
    # print "running 50% slow", which reads like a 50% time stretch; the
    # honest number is the time factor 1/pace = 2.0x.
    assert "at 50% of planned rate (2.0x longer)" in blk["attrs"]
    assert "re-forecast from actual rate" in blk["sku_description"]
    assert not any("re-forecast unavailable" in w for w in st.warnings)


def test_fast_line_pulls_reforecast_end_earlier():
    # 80 cas in 5h = 16 cas/h: 20 left -> +1.25h; manprg said nominal end 17.0.
    st = _state([{"mo": "RUN", "made_cas": 80.0, "left_cas": 20.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}],
                caps=_caps())
    blk = _run_blk(st)
    assert blk["end_h"] == pytest.approx(13.25)
    assert st.running[0]["manprg_end"] == pd.Timestamp(NOW) + timedelta(hours=5)
    # fix CA-9 / C07 wording: 16 cas/h over a 10 cas/h plan = 160% of plan
    assert "at 160% of planned rate (1.6x faster)" in blk["attrs"]


def test_reforecast_agreeing_with_manprg_adds_no_noise():
    # 50 cas in 5h = exactly the planned rate: same end, no tooltip line.
    st = _state([{"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}],
                caps=_caps())
    blk = _run_blk(st)
    assert blk["end_h"] == pytest.approx(17.0)
    assert "reforecast=" not in blk["attrs"]
    assert blk["sku_description"] == "D"


def _assert_fallback(st, expected_end_h: float, why_fragment: str):
    blk = _run_blk(st)
    assert blk["end_h"] == pytest.approx(expected_end_h), \
        "guard must fall back to the manprg estimate"
    assert "reforecast=" not in blk["attrs"]
    assert any("re-forecast unavailable" in w and why_fragment in w
               for w in st.warnings), st.warnings


def test_guard_short_elapsed_falls_back():
    # 0.5h elapsed: manprg pro-rata = now + 9h... nominal end wins at 21.5.
    st = _state([{"mo": "RUN", "made_cas": 10.0, "left_cas": 90.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=0.5)}],
                caps=_caps())
    _assert_fallback(st, 21.5, "elapsed")


def test_guard_low_completion_falls_back():
    # 1% done in 5h: counter barely moved -> manprg remaining 9.9h.
    st = _state([{"mo": "RUN", "made_cas": 1.0, "left_cas": 99.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}],
                caps=_caps())
    _assert_fallback(st, 21.9, "% complete")


def test_guard_future_start_is_queued_not_running():
    # Updated 2026-09-03 (fix CA-7 / audit adversarial-5). This test used to
    # pin the row as RUNNING with the re-forecast falling back to the manprg
    # estimate (block 14-24, locked) — which GATED the line until h24 on
    # garbage telemetry (live repro: a 09/10 start with 500 cases gated P09
    # for 195 h). A future-dated "started" row is data noise: it is QUEUED
    # at its own start (14h -> 24h), unlocked, with a warning naming the MO.
    st = _state([{"mo": "RUN", "made_cas": 10.0, "left_cas": 90.0,
                  "start_dt": pd.Timestamp(NOW) + timedelta(hours=2)}],
                caps=_caps())
    assert st.running == []
    assert [q["mo"] for q in st.queued] == ["RUN"]
    blk = st.blocks.iloc[0]
    assert "current_state:queued" in blk["attrs"]
    assert bool(blk["locked"]) is False
    assert blk["start_h"] == pytest.approx(14.0) and blk["end_h"] == pytest.approx(24.0)
    assert st.line_running_free_h["P09"] == pytest.approx(12.0), \
        "a future-dated row must never gate the line"
    assert any("RUN" in w and "future" in w for w in st.warnings), st.warnings


def test_guard_rate_below_band_is_clamped_not_dropped():
    # Updated 2026-09-03 (fix CA-2 / audit C02). 5 cas in 50h = 0.1 cas/h,
    # far under 0.25x of the 10 cas/h catalog. This used to FALL BACK to the
    # manprg estimate (21.5) — a cliff: pace 0.26 committed the line for
    # +305h while 0.24 gave +79h. Now the rate is CLAMPED to the band floor
    # (2.5 cas/h) and the re-forecast continues: 95 left / 2.5 = 38h ->
    # end_h = 12 + 38 = 50, with a "rate clamped" warning and attrs token.
    st = _state([{"mo": "RUN", "made_cas": 5.0, "left_cas": 95.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=50)}],
                caps=_caps())
    blk = _run_blk(st)
    assert blk["end_h"] == pytest.approx(50.0)
    assert "rate_clamped" in blk["attrs"]
    assert "reforecast=" in blk["attrs"] and "[rate clamped]" in blk["attrs"]
    assert any("rate clamped" in w for w in st.warnings), st.warnings
    assert not any("re-forecast unavailable" in w for w in st.warnings)


def test_guard_rate_above_band_falls_back():
    # 90 cas in 2h = 45 cas/h, over 2x of the 10 cas/h catalog.
    st = _state([{"mo": "RUN", "made_cas": 90.0, "left_cas": 10.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=2)}],
                caps=_caps())
    _assert_fallback(st, 20.0, "outside")   # nominal end start+10h wins


def test_guard_no_catalog_rate_falls_back():
    st = _state([{"mo": "RUN", "made_cas": 25.0, "left_cas": 75.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}],
                caps=None)
    _assert_fallback(st, 19.5, "no catalog rate")


def test_toggle_off_is_exact_legacy_behaviour():
    from helpers.current_state import reforecast_enabled
    assert reforecast_enabled(None) is True
    assert reforecast_enabled({"scheduler": {}}) is True
    assert reforecast_enabled(
        {"scheduler": {"reforecast_running_mo_ends": False}}) is False
    st = _state([{"mo": "RUN", "made_cas": 25.0, "left_cas": 75.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}],
                cfg={"scheduler": {"reforecast_running_mo_ends": False}},
                caps=_caps())
    blk = _run_blk(st)
    assert blk["end_h"] == pytest.approx(19.5)      # manprg pro-rata estimate
    assert "reforecast" not in blk["attrs"]
    assert blk["sku_description"] == "D"
    assert not any("re-forecast" in w for w in st.warnings)


def test_queued_mos_and_free_hours_shift_with_the_reforecast_end():
    st = _state([
        {"mo": "RUN", "made_cas": 25.0, "left_cas": 75.0,
         "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)},
        {"mo": "Q1", "made_cas": "", "hours": 8.0,
         "start_dt": pd.Timestamp(NOW) + timedelta(hours=1)},
    ], caps=_caps())
    q1 = st.blocks[st.blocks["order_id"] == "Q1"].iloc[0]
    assert q1["start_h"] >= 27.0 - 1e-6, \
        "queued MO must wait for the re-forecast end"
    assert st.line_running_free_h["P09"] == pytest.approx(27.0)


def test_netting_prorates_a_reforecast_stretched_mo_across_weeks():
    """A slow line stretches the running MO across the ISO-week boundary —
    its kg must be PRO-RATED into each week by time-overlap share, through
    the same ledger staging/netting uses. The old midpoint rule flipped the
    ENTIRE 50 t into whichever week held the midpoint: the live 2026-08-21
    re-forecast (10 of 11 lines slow) slid a whole day's worth of MOs over
    the Monday boundary and W35 read 1.91M kg of committed credit against
    ~1.1M kg of total weekly plant capacity."""
    from helpers.demand_coverage import build_ledger

    # planned 1000 cas over 100h (50 t); 100 cas in 40h = 2.5 cas/h (0.25x
    # of catalog — inside the band edge). 900 left -> end = now + 360h.
    #
    # Updated 2026-09-03 (fix CA-3 / audit C04): the running block now
    # carries the REMAINING kg (900/1000 * 50 t = 45 t) and the 5 t already
    # made travel as a `made_part` actuals row (start = true start Aug 8
    # 20:00, end = as_of = now Aug 10 12:00, midpoint Aug 9 16:00 -> ISO
    # W32). A W32 demand row absorbs it so the ledger shows the full 50 t.
    rows = [{"mo": "RUN", "hours": 100.0, "fct_cas": 1000.0,
             "made_cas": 100.0, "left_cas": 900.0, "fct_kg": 50000.0,
             "made_kg": 5000.0,
             "start_dt": pd.Timestamp(NOW) - timedelta(hours=40)}]
    demand = pd.DataFrame([
        {"order_id": "O-W32", "sku": "280581", "qty_target": 5000.0,
         "due_start_hour": -168.0, "due_end_hour": 0.0},
        {"order_id": "O-W33", "sku": "280581", "qty_target": 60000.0,
         "due_start_hour": 0.0, "due_end_hour": 168.0},
        {"order_id": "O-W34", "sku": "280581", "qty_target": 60000.0,
         "due_start_hour": 168.0, "due_end_hour": 336.0},
    ])

    # interval_h <= 0 stands the CIP projection down — a projected CIP would
    # split the 372h block and change the hand-computed shares below, which
    # is not what this test is about.
    no_cip = {"cip": {"interval_h": -1}}

    # legacy: manprg says end = now + 90h -> block [0, 102] sits wholly in
    # W33 (Monday anchor: boundary at 168) -> all 50 t credit W33.
    off = _state(rows, cfg={**no_cip,
                            "scheduler": {"reforecast_running_mo_ends": False}},
                 caps=_caps())
    led_off = build_ledger(demand, off.blocks, completed=off.completed,
                           anchor=ANCHOR)
    by_order_off = {r.order_id: r for r in led_off.rows}
    assert by_order_off["O-W33"].committed_kg == pytest.approx(45000.0)
    assert by_order_off["O-W34"].committed_kg == 0.0
    assert by_order_off["O-W32"].produced_kg == pytest.approx(5000.0)

    # re-forecast: end hour 372 -> block [0, 372] spans W33/W34/W35.
    # Hand-computed overlap shares of the 372h run (45 t remaining):
    #   W33 hours [0,168)   = 168/372 -> 45000*168/372 = 20322.581 kg
    #   W34 hours [168,336) = 168/372 -> 20322.581 kg
    #   W35 hours [336,372) =  36/372 ->  4354.839 kg (no demand row: carry)
    on = _state(rows, cfg=no_cip, caps=_caps())
    assert _run_blk(on)["end_h"] == pytest.approx(372.0)
    assert _run_blk(on)["qty_kg"] == pytest.approx(45000.0)
    led_on = build_ledger(demand, on.blocks, completed=on.completed,
                          anchor=ANCHOR)
    by_order_on = {r.order_id: r for r in led_on.rows}
    w33_kg = 45000.0 * 168.0 / 372.0
    assert by_order_on["O-W33"].committed_kg == pytest.approx(w33_kg)
    assert by_order_on["O-W33"].applied_kg == pytest.approx(w33_kg)
    assert by_order_on["O-W34"].committed_kg == pytest.approx(w33_kg)
    assert by_order_on["O-W34"].applied_kg == pytest.approx(w33_kg)
    assert by_order_on["O-W32"].produced_kg == pytest.approx(5000.0)
    # the whole MO still credits exactly once: 45 t of shares + 5 t made
    assert sum(r.committed_kg for r in led_on.rows) + 45000.0 * 36.0 / 372.0 \
        == pytest.approx(45000.0)
    assert led_on.produced_total_kg == pytest.approx(5000.0)


# --------------------------------------------------------------- CIP

def test_scheduled_cip_is_drawn_and_locked():
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=None, max_hours_between=120,
        scheduled_cip=pd.Timestamp(NOW) + timedelta(hours=5), notes="WO#1")})
    st = _state([], cips=cips)
    cip = st.blocks[st.blocks["block_type"] == "cip"]
    sched = cip[cip["label"] == "CIP"]
    assert len(sched) == 1 and bool(sched.iloc[0]["locked"]) is True


def test_future_cips_are_projected_at_the_per_line_interval():
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=pd.Timestamp(NOW), max_hours_between=120,
        scheduled_cip=None, notes="")})
    st = _state([], cips=cips)
    starts = sorted(st.blocks[st.blocks["block_type"] == "cip"]["start_h"])
    assert len(starts) >= 3, "weeks 2-3 must not be CIP-free"
    gaps = [round(b - a) for a, b in zip(starts, starts[1:])]
    assert set(gaps) == {120}


def test_per_line_max_hours_overrides_global_interval():
    cips = CipInfoResult(by_line={"P10": CipInfo(
        line="P10", previous_cip=None, max_hours_between=144,
        scheduled_cip=None, notes="")})
    assert cip_interval_for("P10", cips, {"cip": {"interval_h": 120}}) == 144
    assert cip_interval_for("P99", cips, {"cip": {"interval_h": 120}}) == 120


def test_projected_cips_stay_inside_the_horizon():
    info = CipInfo(line="P09", previous_cip=NOW, max_hours_between=120,
                   scheduled_cip=None, notes="")
    hz = _hz()
    for when, _kind in project_cips("P09", info, hz, 6.0, 120.0):
        assert when < hz.end


def test_projected_cip_straddling_the_horizon_end_is_clipped():
    # previous at h21, interval 120 -> projections at 141/261/381/501; the
    # last one would end at h507 on a 504h horizon (walkthrough finding
    # 2026-08-17: cip_projected blocks with end_h=509 > horizon 504).
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=pd.Timestamp(ANCHOR) + timedelta(hours=21),
        max_hours_between=120, scheduled_cip=None, notes="")})
    st = _state([], cips=cips, cfg={"cip": {"duration_h": 6}})
    cip = st.blocks[st.blocks["block_type"] == "cip"]
    assert (cip["end_h"] <= 504.0).all(), "no CIP may outrun the horizon"
    straddler = cip[cip["start_h"] == 501.0]
    assert len(straddler) == 1, "the straddling CIP must be kept, clipped"
    assert float(straddler.iloc[0]["end_h"]) == 504.0


# --------------------------------------------------------------- plumbing

def test_line_id_resolves_from_lines_csv_then_falls_back():
    lines = pd.DataFrame({"line_id": [0, 5], "line_name": ["P09", "P14"]})
    assert line_id_for("P14", lines) == 5
    assert line_id_for("P11", None) == 2


def test_blocks_match_the_calendar_schema():
    from helpers.calendar_io import CALENDAR_COLUMNS
    st = _state([{"mo": "A", "made_cas": ""}])
    assert list(st.blocks.columns) == CALENDAR_COLUMNS


@pytest.mark.skipif(not (ROOT / "data" / "reference" / "manprg.txt").exists(),
                    reason="real manprg export not present")
def test_real_manprg_and_cip_files_build_a_state():
    ref = ROOT / "data" / "reference"
    mp = read_manprg([ref / "manprg.txt", ref / "manprg2.txt"])
    assert mp.frame is not None and len(mp.frame) > 0
    st = build_current_state(_hz(), manprg=mp, cips=None,
                             cip_path=ref / "cip_info.csv", now=NOW)
    assert len(st.blocks) > 0
    # no production block overlaps another on the same line
    prod = st.blocks[st.blocks["block_type"] == "production"]
    # completed history may overlap the runs that superseded it - the
    # no-overlap invariant applies to the PLAN (running + queued) only
    prod = prod[~prod["attrs"].astype(str).str.contains(
        "current_state:completed", na=False)]
    for line, grp in prod.groupby("line_name"):
        g = grp.sort_values("start_h")
        for a, b in zip(g.itertuples(), list(g.itertuples())[1:]):
            assert b.start_h >= a.end_h - 1e-6, f"{line}: overlap {a.order_id}/{b.order_id}"


# --------------------------------------------------- CIP/production split

def _cip_block(line: str = "P09", start_h: float = 50.0, end_h: float = 56.0) -> dict:
    return {
        "block_id": f"cip_{line}_{start_h}", "block_type": "cip",
        "line_id": 0, "line_name": line, "start_h": start_h, "end_h": end_h,
        "label": "CIP (projected)", "order_id": "", "sku": "",
        "sku_description": "", "qty_kg": 0.0, "locked": False,
        "attrs": "current_state:cip_projected",
    }


def _prod_block(mo: str = "A", start_h: float = 0.0, end_h: float = 100.0) -> dict:
    return {
        "block_id": f"mo_{mo}", "block_type": "production",
        "line_id": 0, "line_name": "P09", "start_h": start_h, "end_h": end_h,
        "label": mo, "order_id": mo, "sku": "280581",
        "sku_description": "", "qty_kg": 5000.0, "locked": False,
        "attrs": "current_state:queued",
    }


def test_cip_in_middle_splits_production_into_two():
    from helpers.current_state import _clip_prod_around_cips
    out, kept = _clip_prod_around_cips(
        [_prod_block()], [_cip_block(start_h=40.0, end_h=50.0)],
        warnings=[], line="P09")
    assert len(out) == 2
    assert out[0]["end_h"] == 40.0 and out[1]["start_h"] == 50.0
    assert all("split" in b["attrs"] for b in out)
    assert len(kept) == 1, "CIP is never dropped when it merely splits"


def test_cip_after_production_leaves_production_whole():
    from helpers.current_state import _clip_prod_around_cips
    out, kept = _clip_prod_around_cips(
        [_prod_block(end_h=30.0)], [_cip_block(start_h=40.0, end_h=50.0)],
        warnings=[], line="P09")
    assert len(out) == 1 and out[0]["end_h"] == 30.0
    assert "split" not in out[0]["attrs"]
    assert len(kept) == 1


def test_cip_overlapping_a_queued_mo_start_pushes_the_mo():
    # Updated 2026-09-03 (fix CA-4 / audit C09). A CIP [5,25] over a QUEUED
    # MO [10,20] used to DROP the clean and keep the MO in place. A queued
    # MO is not running yet, so the committed clean wins and the MO is
    # pushed behind it: [25, 35] (its 10h intact), marked split+pushed=15.
    from helpers.current_state import _clip_prod_around_cips
    warnings: list[str] = []
    out, kept = _clip_prod_around_cips(
        [_prod_block(start_h=10.0, end_h=20.0)],
        [_cip_block(start_h=5.0, end_h=25.0)],
        warnings=warnings, line="P09")
    assert len(out) == 1 and out[0]["start_h"] == 25.0 and out[0]["end_h"] == 35.0
    assert "split" in out[0]["attrs"] and "pushed=15" in out[0]["attrs"]
    assert len(kept) == 1, "a committed clean is never deleted for a queued MO"
    assert warnings == []


def test_cip_overlapping_a_running_mo_start_is_dropped():
    # The plant IS producing now: a clean drawn over the running MO's start
    # is stale/misplaced and cannot push an in-progress MO. Old rule kept.
    from helpers.current_state import _clip_prod_around_cips
    warnings: list[str] = []
    run = _prod_block(start_h=10.0, end_h=20.0)
    run["attrs"] = "current_state:running;pct=40.0"
    run["locked"] = True
    out, kept = _clip_prod_around_cips(
        [run], [_cip_block(start_h=5.0, end_h=25.0)],
        warnings=warnings, line="P09")
    assert len(out) == 1 and out[0]["start_h"] == 10.0 and out[0]["end_h"] == 20.0
    assert len(kept) == 0, "CIP over a RUNNING MO's start is dropped"
    assert any("running MO" in w for w in warnings)


def test_multiple_cips_split_into_three_pieces():
    from helpers.current_state import _clip_prod_around_cips
    cips = [_cip_block(start_h=20.0, end_h=26.0, line="P09"),
            _cip_block(start_h=50.0, end_h=56.0, line="P09")]
    out, kept = _clip_prod_around_cips(
        [_prod_block(start_h=0.0, end_h=100.0)], cips, warnings=[], line="P09")
    assert len(out) == 3
    # Updated 2026-09-03 (fix CA-4 / audit C09): two 6h cleans inside a 100h
    # MO used to leave 88h of production; the MO keeps its 100h, so the
    # tail is pushed by 12h: [0,20) + [26,50) + [56,112) = 20+24+56 = 100h.
    assert [(b["start_h"], b["end_h"]) for b in out] == \
        [(0.0, 20.0), (26.0, 50.0), (56.0, 112.0)]
    assert all("pushed=12" in b["attrs"] for b in out)
    assert len(kept) == 2


def test_no_cip_passthrough_is_unchanged():
    from helpers.current_state import _clip_prod_around_cips
    out, kept = _clip_prod_around_cips(
        [_prod_block(start_h=0.0, end_h=30.0)], [], warnings=[], line="P09")
    assert len(out) == 1 and out[0]["end_h"] == 30.0
    assert kept == []


def test_full_state_build_has_no_cip_production_overlap():
    """End-to-end: a queued MO plus a scheduled CIP must not overlap."""
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=None, max_hours_between=120,
        scheduled_cip=pd.Timestamp(NOW) + timedelta(hours=30), notes="")})
    # queued MO spanning the scheduled CIP (starts at now, 60h long)
    st = _state([
        {"mo": "Q1", "made_cas": "", "hours": 60.0,
         "start_dt": pd.Timestamp(NOW)},
    ], cips=cips)
    prod = st.blocks[st.blocks["block_type"] == "production"]
    cip = st.blocks[st.blocks["block_type"] == "cip"]
    for _, p in prod.iterrows():
        for _, c in cip.iterrows():
            assert not (p["start_h"] < c["end_h"] and c["start_h"] < p["end_h"]), \
                f"CIP/production overlap: {p['order_id']} {p['start_h']}-{p['end_h']} vs {c['start_h']}-{c['end_h']}"
    # the MO was split: 2 pieces around the CIP
    assert len(prod) == 2, f"expected split, got {len(prod)} pieces"
