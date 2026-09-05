# tests/test_fix_CA.py — regression tests for fix group CA (current state:
# running-MO re-forecast, CIP merge, trials), forensic audit 2026-09-03.
#
# Every expected value is derived BY HAND in the comments. Fixture frame:
# NOW = Mon 2026-08-10 12:00, ANCHOR = 00:00 the same day (hour 0), 504 h.
# Base MO: 100 cas over 10 h, 5,000 kg -> 50 kg/cas; catalog 500 kg/h ->
# 10 cas/h == the planned rate.

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.cip_import import CipInfo, CipInfoResult  # noqa: E402
from helpers.current_state import (  # noqa: E402
    MADE_PART,
    QUEUED,
    REFORECAST_MAX_STALE_H,
    _clip_prod_around_cips,
    _reforecast,
    _reforecast_end,
    build_current_state,
    classify_rows,
    line_id_for,
    project_cips,
)
from helpers.horizon import Horizon  # noqa: E402
from helpers.manprg_import import (  # noqa: E402
    ASOF_STAMP_NAME,
    ManprgResult,
    content_sha256,
    read_asof_stamp,
    read_manprg,
)

NOW = datetime(2026, 8, 10, 12, 0)
ANCHOR = datetime(2026, 8, 10, 0, 0)
NOW_H = 12.0


def _hz(hours: int = 504, anchor: datetime = ANCHOR, now: datetime = NOW) -> Horizon:
    return Horizon(anchor=anchor, start=anchor, end=anchor + timedelta(hours=hours),
                   now=now, mode="today", hours=hours, config_anchor=anchor)


def _frame(rows: list[dict]) -> pd.DataFrame:
    base = {"line": "P09", "mo": "1", "item": "280581", "designation": "D",
            "hours": 10.0, "fct_cas": 100.0, "made_cas": None, "left_cas": 100.0,
            "fct_kg": 5000.0, "made_kg": None, "start_dt": pd.Timestamp(NOW)}
    return pd.DataFrame([{**base, **r} for r in rows])


def _caps(rate_kgph: float = 500.0, sku: str = "280581",
          line: str = "P09") -> pd.DataFrame:
    return pd.DataFrame([{"line_id": 0, "sku": sku, "line_name": line,
                          "capable": 1, "calc_rate_kgph": rate_kgph}])


def _state(rows, cips=None, cfg=None, hz=None, caps=None, now=NOW, as_of=None,
           lines=None, initial_states=None):
    mp = ManprgResult(frame=_frame(rows))
    return build_current_state(hz or _hz(), manprg=mp,
                               cips=cips or CipInfoResult(), cfg=cfg, now=now,
                               caps=caps, as_of=as_of, lines=lines,
                               initial_states=initial_states)


def _run_blk(st):
    return st.blocks[st.blocks["attrs"].str.contains("current_state:running")].iloc[0]


def _row(**kw) -> dict:
    """One classified row from the base fixture (helper for pure functions)."""
    return classify_rows(_frame([kw]))[0]


NO_CIP = {"cip": {"interval_h": -1}}


# =========================================================================
# CA-1  (C01 / C03)  the re-forecast measures the rate up to `as_of`
# =========================================================================

def test_ca1_actual_rate_is_invariant_in_now_and_end_is_extrapolated():
    # MO: 200 cas over 20 h (plan 10 cas/h), 10,000 kg. Observed at as_of:
    # 25 cas made 5 h after start -> actual 5 cas/h; 175 left.
    #   end = as_of + 175/5 = as_of + 35 h.
    # Rendered 20 h later the extrapolation rule gives
    #   end = now + max(0, 175 - 5*20)/5 = now + 15 h = as_of + 35 h  (same)
    as_of = NOW
    r = _row(mo="RUN", hours=20.0, fct_cas=200.0, made_cas=25.0, left_cas=175.0,
             fct_kg=10000.0, start_dt=pd.Timestamp(as_of) - timedelta(hours=5))
    a = _reforecast(r, now=as_of, catalog_cph=10.0, as_of=as_of)
    b = _reforecast(r, now=as_of + timedelta(hours=20), catalog_cph=10.0, as_of=as_of)
    assert a.actual_cph == pytest.approx(5.0)
    assert b.actual_cph == pytest.approx(5.0), "the rate must not decay with the clock"
    assert a.elapsed_h == pytest.approx(5.0) and b.elapsed_h == pytest.approx(5.0)
    assert a.end == as_of + timedelta(hours=35)
    assert b.end == as_of + timedelta(hours=35)
    assert a.stale is False and a.age_h == pytest.approx(0.0)
    assert b.stale is True and b.age_h == pytest.approx(20.0)
    # legacy behaviour (no as_of) measured 25 cas over 25 h = 1 cas/h
    legacy = _reforecast(r, now=as_of + timedelta(hours=20), catalog_cph=10.0)
    assert legacy.actual_cph == pytest.approx(1.0)


def test_ca1_board_end_is_identical_when_rendered_5h_after_the_pull():
    # same row through build_current_state at now = as_of and as_of + 5 h
    # (inside the 8 h stale limit): end_h = 12 + 35 = 47 both times.
    rows = [{"mo": "RUN", "hours": 20.0, "fct_cas": 200.0, "made_cas": 25.0,
             "left_cas": 175.0, "fct_kg": 10000.0,
             "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}]
    st0 = _state(rows, caps=_caps(), cfg=NO_CIP, now=NOW, as_of=NOW)
    st5 = _state(rows, caps=_caps(), cfg=NO_CIP,
                 now=NOW + timedelta(hours=5), as_of=NOW)
    assert _run_blk(st0)["end_h"] == pytest.approx(47.0)
    assert _run_blk(st5)["end_h"] == pytest.approx(47.0)
    assert st5.line_running_free_h["P09"] == pytest.approx(47.0)
    assert st0.as_of == NOW and st5.as_of == NOW and st5.now == NOW + timedelta(hours=5)
    assert not any("old" in w for w in st5.warnings)


def test_ca1_stale_content_falls_back_loudly_naming_the_age():
    # now = as_of + 20 h (> REFORECAST_MAX_STALE_H = 8): keep the manprg
    # estimate. Manprg estimate: frac_left = 1 - 25/200 = 0.875 ->
    # remaining 20*0.875 = 17.5 h from now; nominal end = start + 20 h =
    # as_of + 15 h (earlier) -> end = now + 17.5 = 12 + 20 + 17.5 = 49.5.
    rows = [{"mo": "RUN", "hours": 20.0, "fct_cas": 200.0, "made_cas": 25.0,
             "left_cas": 175.0, "fct_kg": 10000.0,
             "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}]
    st = _state(rows, caps=_caps(), cfg=NO_CIP,
                now=NOW + timedelta(hours=20), as_of=NOW)
    assert REFORECAST_MAX_STALE_H == 8.0
    assert _run_blk(st)["end_h"] == pytest.approx(49.5)
    assert "reforecast=" not in _run_blk(st)["attrs"]
    assert any("20.0h old" in w and ">8h" in w for w in st.warnings), st.warnings
    # the wrapper reports the same thing as "unusable"
    r = _row(mo="RUN", hours=20.0, fct_cas=200.0, made_cas=25.0, left_cas=175.0,
             fct_kg=10000.0, start_dt=pd.Timestamp(NOW) - timedelta(hours=5))
    end, why = _reforecast_end(r, NOW + timedelta(hours=20), 10.0, as_of=NOW)
    assert end is None and "20.0h old" in why


def test_ca1_as_of_defaults_to_the_manprg_result_stamp():
    # no explicit as_of: the ManprgResult's as_of (the bridge stamp) is used
    rows = [{"mo": "RUN", "hours": 20.0, "fct_cas": 200.0, "made_cas": 25.0,
             "left_cas": 175.0, "fct_kg": 10000.0,
             "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}]
    mp = ManprgResult(frame=_frame(rows), as_of=pd.Timestamp(NOW), as_of_source="stamp")
    st = build_current_state(_hz(), manprg=mp, cips=CipInfoResult(), cfg=NO_CIP,
                             now=NOW + timedelta(hours=3), caps=_caps())
    assert st.as_of == NOW and st.as_of_source == "stamp"
    assert _run_blk(st)["end_h"] == pytest.approx(47.0)
    # a stamp AHEAD of the clock is skew: fall back to now, and say so
    mp2 = ManprgResult(frame=_frame(rows), as_of=pd.Timestamp(NOW) + timedelta(hours=2))
    st2 = build_current_state(_hz(), manprg=mp2, cips=CipInfoResult(), cfg=NO_CIP,
                              now=NOW, caps=_caps())
    assert st2.as_of == NOW
    assert any("clock skew" in w for w in st2.warnings)


def _write_manprg(dir_: Path, name: str, mo: str = "30001") -> Path:
    hdr = ("Start date;Start time;Line;MO No.;Item;Designation;Pal;Type;Hours;"
           "Fct qty (Cas);Qty made (Cas);Fct qty [Kg];;Qty made [Kg];;Left (Cas)")
    row = f"08/10/2026;07:00;LMH-P09;{mo};280451;DESC;640;GGS;10.00;100;25;5000.000;Kg;1250.000;Kg;75"
    p = dir_ / name
    p.write_text(hdr + "\r\n" + row + "\r\n", encoding="cp1252")
    return p


def test_ca1_read_manprg_exposes_as_of_from_a_matching_stamp(tmp_path):
    p1 = _write_manprg(tmp_path, "manprg.txt")
    p2 = _write_manprg(tmp_path, "manprg2.txt", mo="30002")
    (tmp_path / ASOF_STAMP_NAME).write_text(json.dumps({
        "as_of": "2026-08-10T09:30:00",
        "sha256": content_sha256([p1, p2])}), encoding="utf-8")
    res = read_manprg([p1, p2])
    assert res.rows == 2
    assert res.as_of == pd.Timestamp("2026-08-10 09:30")
    assert res.as_of_source == "stamp"
    assert not any("sha256" in w for w in res.warnings)
    # content edited behind the bridge's back: the stamp no longer applies
    p2.write_text(p2.read_text(encoding="cp1252").replace(";25;", ";30;"),
                  encoding="cp1252")
    res2 = read_manprg([p1, p2])
    assert res2.as_of_source == "mtime"
    assert res2.as_of is not None
    assert any("sha256 does not match" in w for w in res2.warnings)
    # tz-aware stamps are read as local wall-clock
    ts = pd.Timestamp("2026-08-10 09:30").tz_localize(datetime.now().astimezone().tzinfo)
    (tmp_path / ASOF_STAMP_NAME).write_text(json.dumps({
        "as_of": ts.isoformat(), "sha256": content_sha256([p1, p2])}), encoding="utf-8")
    as_of, src, _ = read_asof_stamp([p1, p2])
    assert as_of == pd.Timestamp("2026-08-10 09:30") and src == "stamp"


def _pull_module():
    spec = importlib.util.spec_from_file_location(
        "fs_live_pull", ROOT / "scripts" / "fs-live-pull.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ca1_pull_script_stamps_when_manprg_content_changes(tmp_path):
    pull = _pull_module()
    p1 = _write_manprg(tmp_path, "manprg.txt")
    p2 = _write_manprg(tmp_path, "manprg2.txt", mo="30002")
    # first pass: manprg changed -> stamp written with the given as_of
    assert pull.stamp_manprg_asof(tmp_path, ["manprg.txt"],
                                  as_of="2026-08-10T09:30:00", head="abc1234") is True
    meta = json.loads((tmp_path / ASOF_STAMP_NAME).read_text(encoding="utf-8"))
    assert meta["as_of"] == "2026-08-10T09:30:00"
    assert meta["pull_head"] == "abc1234"
    # the script's sha mirrors the app's helper byte for byte
    assert meta["sha256"] == content_sha256([p1, p2]) == pull.content_sha256([p1, p2])
    assert read_manprg([p1, p2]).as_of == pd.Timestamp("2026-08-10 09:30")
    # nothing changed: the stamp is NOT rewritten (as-of stays put)
    assert pull.stamp_manprg_asof(tmp_path, ["cip_info.csv"], as_of="2026-08-10T12:00:00") is False
    assert json.loads((tmp_path / ASOF_STAMP_NAME).read_text())["as_of"] == "2026-08-10T09:30:00"
    # a missing stamp is created even without a change (first-time stamp)
    (tmp_path / ASOF_STAMP_NAME).unlink()
    assert pull.stamp_manprg_asof(tmp_path, [], as_of=None) is True
    # git %aI with an offset becomes local naive time
    local = datetime(2026, 8, 10, 9, 30).astimezone()
    assert pull._local_iso(local.isoformat()) == "2026-08-10T09:30:00"


# =========================================================================
# CA-2  (C02)  the 0.25x band edge clamps instead of falling off a cliff
# =========================================================================

def test_ca2_pace_024_and_026_end_within_10_percent():
    # plan 1000 cas / 100 h = 10 cas/h (50 t); catalog 10 cas/h; 50 h elapsed.
    #  pace 0.24: made 120, left 880 -> actual 2.4 < 2.5 floor -> CLAMPED 2.5
    #             end = 12 + 880/2.5 = 12 + 352 = 364.0
    #  pace 0.26: made 130, left 870 -> 2.6 cas/h -> end = 12 + 870/2.6
    #             = 12 + 334.615 = 346.615
    #  ratio 364.0/346.615 = 1.050 -> 5.0 % apart (old cliff: 3.85x)
    base = {"mo": "RUN", "hours": 100.0, "fct_cas": 1000.0, "fct_kg": 50000.0,
            "start_dt": pd.Timestamp(NOW) - timedelta(hours=50)}
    slow = _state([{**base, "made_cas": 120.0, "left_cas": 880.0}],
                  caps=_caps(), cfg=NO_CIP)
    ok = _state([{**base, "made_cas": 130.0, "left_cas": 870.0}],
                caps=_caps(), cfg=NO_CIP)
    e_slow, e_ok = _run_blk(slow)["end_h"], _run_blk(ok)["end_h"]
    assert e_slow == pytest.approx(364.0)
    assert e_ok == pytest.approx(12.0 + 870.0 / 2.6, abs=1e-3)   # end_h rounded to 3 dp
    assert abs(e_slow - e_ok) / e_ok < 0.10
    assert "rate_clamped" in _run_blk(slow)["attrs"]
    assert "rate_clamped" not in _run_blk(ok)["attrs"]
    assert any("rate clamped" in w and "2.4 cas/h" in w for w in slow.warnings), slow.warnings
    assert not any("re-forecast unavailable" in w for w in slow.warnings), \
        "never silently fall back to the planned rate"
    # the upper edge (a counter jump) still falls back, loudly:
    #  960 of 1000 made in 40 h = 24.0 cas/h > 2.0 x catalog 10 = 20 -> unusable
    #  (made must stay < Fct 1000, else classify_rows files the MO as completed
    #  and there is no running MO to warn about)
    fast = _state([{**base, "made_cas": 960.0, "left_cas": 40.0,
                    "start_dt": pd.Timestamp(NOW) - timedelta(hours=40)}],
                  caps=_caps(), cfg=NO_CIP)
    assert fast.running and _run_blk(fast) is not None
    assert "reforecast=" not in _run_blk(fast)["attrs"]
    assert any("re-forecast unavailable" in w and "outside" in w
               and "24.0 cas/h" in w for w in fast.warnings), fast.warnings


# =========================================================================
# CA-3  (C04 / netting-1 / reforecast-4 / adversarial-13)  remaining kg
# =========================================================================

def test_ca3_running_block_carries_remaining_kg_and_made_part_is_an_actual():
    # 50 % made at the anchor: 50 of 100 cas, 2,500 of 5,000 kg made,
    # started 5 h before now. Block kg = 5000 * 50/100 = 2,500 (remaining).
    st = _state([{"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0, "made_kg": 2500.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}],
                caps=_caps(), cfg=NO_CIP)
    blk = _run_blk(st)
    assert blk["qty_kg"] == pytest.approx(2500.0)
    assert "fct_kg=5000" in blk["attrs"] and "made_kg=2500" in blk["attrs"]
    # end: 50 cas / 5 h = 10 cas/h = plan -> 50 left -> +5 h -> block [7, 17]
    assert (blk["start_h"], blk["end_h"]) == (7.0, 17.0)
    made = [r for r in st.completed if r["kind"] == MADE_PART]
    assert len(made) == 1
    mp = made[0]
    assert mp["mo"] == "RUN" and mp["item"] == "280581" and mp["line"] == "P09"
    assert mp["made_kg"] == pytest.approx(2500.0)
    assert mp["start_dt"] == pd.Timestamp(NOW) - timedelta(hours=5), "true start"
    assert mp["end_dt"] == pd.Timestamp(NOW), "end = as_of"
    assert mp["hours"] == pytest.approx(5.0)
    assert mp["remaining_kg"] == pytest.approx(2500.0)
    assert mp["qty_kg"] == pytest.approx(5000.0), "row semantics: qty_kg is Fct kg"
    assert st.counts["completed"] == 0 and st.counts["made_part"] == 1
    # the running dict keeps the FULL Fct kg (scenario E derives remaining
    # itself from left/fct — unchanged contract)
    assert st.running[0]["qty_kg"] == 5000.0
    assert st.running[0]["remaining_kg"] == pytest.approx(2500.0)


def test_ca3_made_kg_is_derived_when_the_export_leaves_it_blank():
    # 40 of 100 cas made, made kg blank -> 5000 * 40/100 = 2,000 kg made,
    # 3,000 kg remaining.
    r = _row(mo="A", made_cas=40.0, left_cas=60.0, made_kg=None)
    assert r["made_kg"] == pytest.approx(2000.0)
    assert r["remaining_kg"] == pytest.approx(3000.0)


def test_ca3_ledger_sees_the_full_mo_once_made_plus_remaining():
    """Row shape handed to demand_coverage (owned by agent CB): made_part is
    consumed by _supply_from_completed via item / start_dt / hours / made_kg."""
    from helpers.demand_coverage import build_ledger
    st = _state([{"mo": "RUN", "made_cas": 50.0, "left_cas": 50.0, "made_kg": 2500.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)}],
                caps=_caps(), cfg=NO_CIP)
    demand = pd.DataFrame([{"order_id": "O-W33", "sku": "280581",
                            "qty_target": 6000.0,
                            "due_start_hour": 0.0, "due_end_hour": 168.0}])
    led = build_ledger(demand, st.blocks, completed=st.completed, anchor=ANCHOR)
    row = {r.order_id: r for r in led.rows}["O-W33"]
    # block [7,17] wholly in W33 -> committed 2,500; made_part midpoint
    # Aug 10 09:30 -> W33 -> produced 2,500; total = the 5,000 kg MO
    assert row.committed_kg == pytest.approx(2500.0)
    assert row.produced_kg == pytest.approx(2500.0)
    assert led.produced_total_kg == pytest.approx(2500.0)


# =========================================================================
# CA-4  (C09 / ingest-1)  the CIP merge pushes, never deletes hours
# =========================================================================

def _blk(mo, s, e, attrs="current_state:queued", qty=5000.0, locked=False):
    return {"block_id": f"b_{mo}", "block_type": "production", "line_id": 0,
            "line_name": "P09", "start_h": s, "end_h": e, "label": mo,
            "order_id": mo, "sku": "280581", "sku_description": "", "qty_kg": qty,
            "locked": locked, "attrs": attrs}


def _cip(s, e, bid="c1"):
    return {"block_id": bid, "block_type": "cip", "line_id": 0, "line_name": "P09",
            "start_h": s, "end_h": e, "label": "CIP", "order_id": "", "sku": "",
            "sku_description": "", "qty_kg": 0.0, "locked": True,
            "attrs": "current_state:cip_scheduled"}


def test_ca4_20h_mo_with_6h_cip_at_hour_10_keeps_20h_and_queue_starts_at_26():
    # MO A [0,20), CIP [10,16), queued Q [20,28)
    #   A -> [0,10) + [16,26)  (10 + 10 = 20 h kept; pushed by 6)
    #   Q -> starts at A's new end 26 -> [26,34) (8 h kept; pushed by 6)
    out, kept = _clip_prod_around_cips(
        [_blk("A", 0.0, 20.0), _blk("Q", 20.0, 28.0)], [_cip(10.0, 16.0)],
        warnings=[], line="P09")
    a = [(b["start_h"], b["end_h"]) for b in out if b["order_id"] == "A"]
    q = [(b["start_h"], b["end_h"]) for b in out if b["order_id"] == "Q"]
    assert a == [(0.0, 10.0), (16.0, 26.0)]
    assert q == [(26.0, 34.0)]
    assert all("split" in b["attrs"] and "pushed=6" in b["attrs"] for b in out)
    assert len(kept) == 1
    # kg pro-rated over the pieces by duration: 10/20 each of 5,000
    assert [b["qty_kg"] for b in out if b["order_id"] == "A"] == [2500.0, 2500.0]
    assert sum(b["qty_kg"] for b in out if b["order_id"] == "A") == 5000.0


def test_ca4_cip_overlapping_a_queued_start_marks_split_and_pushes():
    # Q [10,20) with CIP [8,14): the start is overlapped -> Q runs [14,24)
    out, kept = _clip_prod_around_cips(
        [_blk("Q", 10.0, 20.0)], [_cip(8.0, 14.0)], warnings=[], line="P09")
    assert [(b["start_h"], b["end_h"]) for b in out] == [(14.0, 24.0)]
    assert "split" in out[0]["attrs"] and "pushed=4" in out[0]["attrs"]
    assert len(kept) == 1


def test_ca4_end_to_end_queue_and_free_hours_follow_the_push():
    # running MO: 200 cas / 20 h plan, 100 made in 10 h (= plan rate),
    # 100 left -> end = 12 + 10 = 22 -> block [2, 22]; queued Q 8 h at now
    # -> [22, 30]. Scheduled CIP at now+3 h = [15, 21] (6 h):
    #   running -> [2,15) 13 h + [21,28) 7 h = 20 h; Q -> [28, 36]
    #   line_running_free_h = 28, line_free_h = 36
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=None, max_hours_between=-1,
        scheduled_cip=pd.Timestamp(NOW) + timedelta(hours=3), notes="")})
    st = _state([
        {"mo": "RUN", "hours": 20.0, "fct_cas": 200.0, "made_cas": 100.0,
         "left_cas": 100.0, "fct_kg": 10000.0,
         "start_dt": pd.Timestamp(NOW) - timedelta(hours=10)},
        {"mo": "Q", "made_cas": "", "hours": 8.0, "start_dt": pd.Timestamp(NOW)},
    ], cips=cips, caps=_caps(), cfg={"cip": {"interval_h": -1, "duration_h": 6}})
    prod = st.blocks[st.blocks["block_type"] == "production"].sort_values("start_h")
    spans = [(r.order_id, r.start_h, r.end_h) for r in prod.itertuples()]
    assert spans == [("RUN", 2.0, 15.0), ("RUN", 21.0, 28.0), ("Q", 28.0, 36.0)]
    cip = st.blocks[st.blocks["block_type"] == "cip"]
    assert [(c.start_h, c.end_h) for c in cip.itertuples()] == [(15.0, 21.0)]
    assert st.line_running_free_h["P09"] == pytest.approx(28.0)
    assert st.line_free_h["P09"] == pytest.approx(36.0)
    assert st.running[0]["est_end"] == ANCHOR + timedelta(hours=28)
    assert st.running[0]["pushed_by_cip_h"] == pytest.approx(6.0)
    assert st.queued[0]["placed_start"] == ANCHOR + timedelta(hours=28)
    assert st.queued[0]["placed_end"] == ANCHOR + timedelta(hours=36)
    # remaining 5,000 kg (100 of 200 cas left) pro-rated 13/20 and 7/20
    kg = [b for b in prod.itertuples() if b.order_id == "RUN"]
    assert kg[0].qty_kg == pytest.approx(5000.0 * 13 / 20)
    assert kg[1].qty_kg == pytest.approx(5000.0 * 7 / 20)


# =========================================================================
# CA-5  (C10 / ingest-2)  manprg CIP rows are the committed cleans
# =========================================================================

def test_ca5_manprg_cip_row_is_drawn_at_its_own_duration_and_phases_the_grid():
    # manprg CIP row: start now+10 h = hour 22, Hours 8 -> CIP [22, 30].
    # cip_info: PreviousCIP now-100 h, ScheduledCIP now+30 h (hour 42, differs
    # by 20 h -> warned, NOT drawn), interval 120. Grid from the last known
    # clean (22): 142, 262, 382, 502 (clipped to 504).
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=pd.Timestamp(NOW) - timedelta(hours=100),
        max_hours_between=120,
        scheduled_cip=pd.Timestamp(NOW) + timedelta(hours=30), notes="")})
    st = _state([{"mo": "CIP1", "item": "CIP", "designation": "CIP", "hours": 8.0,
                  "fct_cas": 2779.0, "made_cas": "", "left_cas": 2779.0,
                  "start_dt": pd.Timestamp(NOW) + timedelta(hours=10)}],
                cips=cips, cfg={"cip": {"duration_h": 6}})
    cip = st.blocks[st.blocks["block_type"] == "cip"].sort_values("start_h")
    spans = [(c.start_h, c.end_h) for c in cip.itertuples()]
    assert spans[0] == (22.0, 30.0), "the plant's 8 h clean, not a 6 h projection"
    assert spans[1:] == [(142.0, 148.0), (262.0, 268.0), (382.0, 388.0), (502.0, 504.0)]
    first = cip.iloc[0]
    assert bool(first["locked"]) is True and first["label"] == "CIP"
    assert "source=manprg" in first["attrs"] and "mo=CIP1" in first["attrs"]
    assert not any(abs(c.start_h - 42.0) < 1e-6 for c in cip.itertuples()), \
        "cip_info ScheduledCIP must not be drawn when manprg carries the clean"
    assert any("disagrees" in w for w in st.warnings), st.warnings
    assert st.cips[0]["source"] == "manprg" and st.cips[0]["duration_h"] == 8.0


def test_ca5_without_a_manprg_cip_row_cip_info_scheduled_is_still_drawn():
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=None, max_hours_between=120,
        scheduled_cip=pd.Timestamp(NOW) + timedelta(hours=5), notes="")})
    st = _state([], cips=cips, cfg={"cip": {"duration_h": 6}})
    cip = st.blocks[st.blocks["block_type"] == "cip"].sort_values("start_h")
    assert (cip.iloc[0]["start_h"], cip.iloc[0]["end_h"]) == (17.0, 23.0)
    assert bool(cip.iloc[0]["locked"]) is True


# =========================================================================
# CA-6  (staging-2)  TRIALS rows become trial blocks that survive the merge
# =========================================================================

def test_ca6_trials_row_becomes_one_trial_block_with_its_window():
    # TRIALS 24 h starting now+1 h -> block [13, 37], block_type trial, no kg
    st = _state([{"mo": "T1", "item": "TRIALS", "designation": "TRIALS",
                  "hours": 24.0, "fct_cas": 8334.0, "made_cas": "",
                  "left_cas": 8334.0, "fct_kg": 36002.88,
                  "start_dt": pd.Timestamp(NOW) + timedelta(hours=1)}],
                cfg=NO_CIP)
    tr = st.blocks[st.blocks["block_type"] == "trial"]
    assert len(tr) == 1
    t = tr.iloc[0]
    assert (t["start_h"], t["end_h"]) == (13.0, 37.0)
    assert t["label"] == "TRIAL" and t["order_id"] == "T1"
    assert pd.isna(t["qty_kg"]) or t["qty_kg"] is None
    assert st.line_free_h["P09"] == pytest.approx(37.0)


def test_ca6_trial_is_kept_and_pushed_around_a_cip_too():
    # trial [13, 37] with a scheduled CIP at hour 20 [20, 26]:
    #   pieces [13,20) 7 h + [26,43) 17 h = 24 h of blocked time
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=None, max_hours_between=-1,
        scheduled_cip=ANCHOR + timedelta(hours=20), notes="")})
    st = _state([{"mo": "T1", "item": "TRIALS", "hours": 24.0, "made_cas": "",
                  "start_dt": pd.Timestamp(NOW) + timedelta(hours=1)}],
                cips=cips, cfg={"cip": {"interval_h": -1, "duration_h": 6}})
    tr = st.blocks[st.blocks["block_type"] == "trial"].sort_values("start_h")
    assert [(t.start_h, t.end_h) for t in tr.itertuples()] == [(13.0, 20.0), (26.0, 43.0)]
    assert st.blocks["block_type"].value_counts().to_dict() == {"trial": 2, "cip": 1}


# =========================================================================
# CA-7  (adversarial-5)  future-dated "started" row is queued, warned
# =========================================================================

def test_ca7_future_start_with_made_is_queued_at_its_start_and_warned():
    warnings: list[str] = []
    rows = classify_rows(_frame([{"mo": "F", "made_cas": 500.0, "left_cas": 500.0,
                                  "fct_cas": 1000.0,
                                  "start_dt": pd.Timestamp(NOW) + timedelta(hours=186)}]),
                         now=NOW, warnings=warnings)
    assert rows[0]["kind"] == QUEUED and rows[0]["future_started"] is True
    assert len(warnings) == 1 and "MO F" in warnings[0] and "future" in warnings[0]
    # legacy call without `now` keeps the old rule (made > 0 -> running)
    assert classify_rows(_frame([{"mo": "F", "made_cas": 500.0, "fct_cas": 1000.0,
                                  "start_dt": pd.Timestamp(NOW) + timedelta(hours=186)}])
                         )[0]["kind"] == "running"
    st = _state([{"mo": "F", "made_cas": 500.0, "left_cas": 500.0, "fct_cas": 1000.0,
                  "start_dt": pd.Timestamp(NOW) + timedelta(hours=186)}], cfg=NO_CIP)
    blk = st.blocks.iloc[0]
    # queued at its own start: hour 12 + 186 = 198, 10 h -> 208; unlocked
    assert (blk["start_h"], blk["end_h"]) == (198.0, 208.0)
    assert bool(blk["locked"]) is False and "future_start_noise" in blk["attrs"]
    assert st.line_running_free_h["P09"] == pytest.approx(NOW_H), "never a gate"


# =========================================================================
# CA-8  (adversarial-6 / C12 / C91)  CIP projection phase & overdue lines
# =========================================================================

def test_ca8_overdue_line_is_announced_and_catch_up_clean_drawn_at_now():
    # PreviousCIP 200 h before now on a 120 h line: overdue by 80 h.
    # Catch-up at now (hour 12), then 132, 252, 372, 492.
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=pd.Timestamp(NOW) - timedelta(hours=200),
        max_hours_between=120, scheduled_cip=None, notes="")})
    st = _state([], cips=cips, cfg={"cip": {"duration_h": 6}})
    starts = sorted(st.blocks[st.blocks["block_type"] == "cip"]["start_h"])
    assert starts == [12.0, 132.0, 252.0, 372.0, 492.0]
    assert any("overdue by 80 h" in w for w in st.warnings), st.warnings


def test_ca8_scheduled_clean_past_the_interval_is_warned_but_kept():
    # last clean 130 h ago, next scheduled in 20 h: 150 h gap on a 120 h
    # line -> warning, the plant's date is drawn (hour 32), no catch-up.
    cips = CipInfoResult(by_line={"P09": CipInfo(
        line="P09", previous_cip=pd.Timestamp(NOW) - timedelta(hours=130),
        max_hours_between=120,
        scheduled_cip=pd.Timestamp(NOW) + timedelta(hours=20), notes="")})
    st = _state([], cips=cips, cfg={"cip": {"duration_h": 6}})
    starts = sorted(st.blocks[st.blocks["block_type"] == "cip"]["start_h"])
    assert starts[0] == 32.0
    assert any("30 h past the 120 h interval" in w for w in st.warnings), st.warnings


def test_ca8_no_history_grid_does_not_move_with_the_run_day():
    # No PreviousCIP / ScheduledCIP / manprg CIP row / MOs: phased from the
    # anchor week's ISO Monday (Aug 10). Monday run: 120, 240, 360, 480;
    # Tuesday run (anchor Aug 11): the SAME wall-clock cleans = 96, 216, ...
    info = CipInfo(line="P12", previous_cip=None, max_hours_between=120,
                   scheduled_cip=None, notes="")
    cips = CipInfoResult(by_line={"P12": info})
    mon = _state([], cips=cips, cfg={"cip": {"duration_h": 6}})
    tue_anchor = ANCHOR + timedelta(days=1)
    tue = _state([], cips=cips, cfg={"cip": {"duration_h": 6}},
                 hz=_hz(anchor=tue_anchor, now=tue_anchor + timedelta(hours=12)),
                 now=tue_anchor + timedelta(hours=12))
    mon_wall = sorted(ANCHOR + timedelta(hours=float(h))
                      for h in mon.blocks[mon.blocks["block_type"] == "cip"]["start_h"])
    tue_wall = sorted(tue_anchor + timedelta(hours=float(h))
                      for h in tue.blocks[tue.blocks["block_type"] == "cip"]["start_h"])
    assert mon_wall[:4] == [ANCHOR + timedelta(hours=h) for h in (120, 240, 360, 480)]
    assert tue_wall[:4] == mon_wall[:4], "grid must not slide 24 h per run day"
    assert any("no PreviousCIP" in w and "Monday" in w for w in mon.warnings), mon.warnings


def test_ca8_no_history_uses_initial_states_carryover_when_present():
    # carryover 65 h before the config anchor: last clean = hour -65 ->
    # grid at 55, 175, 295, 415 (interval 120); not overdue (77 h < 120).
    cips = CipInfoResult(by_line={"P12": CipInfo(
        line="P12", previous_cip=None, max_hours_between=120,
        scheduled_cip=None, notes="")})
    init = pd.DataFrame([{"line_id": 3, "line_name": "P12", "initial_sku": "CLEAN",
                          "available_from_hour": 0,
                          "carryover_run_hours_since_last_cip_at_t0": 65}])
    st = _state([], cips=cips, cfg={"cip": {"duration_h": 6}}, initial_states=init)
    starts = sorted(st.blocks[st.blocks["block_type"] == "cip"]["start_h"])
    assert starts == [55.0, 175.0, 295.0, 415.0]
    assert any("carryover 65 h" in w for w in st.warnings), st.warnings


def test_ca8_no_history_falls_back_to_the_first_committed_production_start():
    # queued MO at now+3 h (hour 15) on a line with no CIP data at all:
    # grid from 15 -> 135, 255, 375, 495 — a manprg timestamp, stable
    # across renders.
    st = _state([{"mo": "Q", "made_cas": "", "start_dt": pd.Timestamp(NOW) + timedelta(hours=3)}],
                cfg={"cip": {"interval_h": 120, "duration_h": 6}})
    starts = sorted(st.blocks[st.blocks["block_type"] == "cip"]["start_h"])
    assert starts == [135.0, 255.0, 375.0, 495.0]
    assert any("first committed production start" in w for w in st.warnings), st.warnings


def test_ca8_project_cips_committed_manprg_clean_dedupes_and_phases():
    # pure function: committed manprg clean at hour 22, prev at -88:
    # nothing drawn here for the committed one (the caller draws it), grid
    # 142, 262, 382, 502; a projection within 1 h of the committed is skipped
    info = CipInfo(line="P09", previous_cip=ANCHOR - timedelta(hours=88),
                   max_hours_between=120, scheduled_cip=None, notes="")
    hz = _hz()
    out = project_cips("P09", info, hz, 6.0, 120.0,
                       committed=[ANCHOR + timedelta(hours=22)], now=NOW)
    hours = [round((w - ANCHOR).total_seconds() / 3600, 3) for w, _ in out]
    assert hours == [142.0, 262.0, 382.0, 502.0]
    # legacy positional call still works (previous at now -> +120 grid)
    legacy = project_cips("P09", CipInfo("P09", pd.Timestamp(NOW), 120, None, ""),
                          hz, 6.0, 120.0)
    assert [round((w - ANCHOR).total_seconds() / 3600) for w, _ in legacy][:2] == [132, 252]


# =========================================================================
# CA-9  (C07 / C08)  note wording, superseded remaining cases
# =========================================================================

def test_ca9_note_prints_pace_and_time_factor_with_date_beyond_24h():
    # 42 cas in 10 h = 4.2 cas/h vs plan 10 -> 42 % of plan, 1/0.42 = 2.38 ->
    # "2.4x longer". 958 left / 4.2 = 228.1 h -> end hour 240.1; manprg
    # estimate now + 100*0.958 = 107.8 -> delta 132 h > 24 h -> dates shown.
    st = _state([{"mo": "RUN", "hours": 100.0, "fct_cas": 1000.0, "fct_kg": 50000.0,
                  "made_cas": 42.0, "left_cas": 958.0,
                  "start_dt": pd.Timestamp(NOW) - timedelta(hours=10)}],
                caps=_caps(), cfg=NO_CIP)
    blk = _run_blk(st)
    assert blk["end_h"] == pytest.approx(12.0 + 958.0 / 4.2)
    note = blk["attrs"].split("reforecast=", 1)[1]
    assert "at 42% of planned rate (2.4x longer)" in note
    import re
    assert re.search(r"ends \w{3} \d{2}-\d{2} \d{2}:\d{2} re-forecast", note), note
    assert re.search(r"manprg said \w{3} \d{2}-\d{2} \d{2}:\d{2}", note), note
    assert "slow" not in note


def test_ca9_superseded_mo_keeps_its_remaining_cases_in_warning_and_row():
    # OLD: 10 of 100 made, 90 left -> 90 cases / 4,500 kg remaining
    st = _state([
        {"mo": "OLD", "made_cas": 10.0, "left_cas": 90.0,
         "start_dt": pd.Timestamp(NOW) - timedelta(hours=50)},
        {"mo": "NEW", "made_cas": 20.0, "left_cas": 80.0,
         "start_dt": pd.Timestamp(NOW) - timedelta(hours=5)},
    ], cfg=NO_CIP)
    w = [w for w in st.warnings if "superseded" in w]
    assert len(w) == 1 and "90 cases / 4,500 kg remaining" in w[0], w
    old = [r for r in st.completed if r["mo"] == "OLD"][0]
    assert old["kind"] == "completed" and old["superseded_by"] == "NEW"
    assert old["remaining_kg"] == pytest.approx(4500.0) and old["left_cas"] == 90.0


# =========================================================================
# CA-10  (adversarial-10 / adversarial-11)  duplicate MOs, unknown lines
# =========================================================================

def test_ca10_duplicate_mo_number_on_two_lines_is_warned_and_kept_distinct():
    st = _state([
        {"mo": "777", "line": "P09", "made_cas": ""},
        {"mo": "777", "line": "P19", "made_cas": "", "hours": 4.0},
    ], cfg=NO_CIP)
    assert any("MO 777 appears on 2 lines" in w and "777@P09" in w and "777@P19" in w
               for w in st.warnings), st.warnings
    assert sorted(q["mo_uid"] for q in st.queued) == ["777@P09", "777@P19"]
    prod = st.blocks[st.blocks["block_type"] == "production"]
    assert len(prod) == 2 and set(prod["line_name"]) == {"P09", "P19"}
    assert prod["block_id"].nunique() == 2


def test_ca10_unknown_line_is_warned_and_skipped_never_synthesised():
    lines = pd.DataFrame({"line_id": [0, 10], "line_name": ["P09", "P19"]})
    assert line_id_for("P99", lines) == -1
    assert line_id_for("P19", lines) == 10
    assert line_id_for("P99", None) == 90, "no table: legacy digits fallback kept"
    st = _state([
        {"mo": "A", "line": "P09", "made_cas": ""},
        {"mo": "B", "line": "P99", "made_cas": ""},
    ], cfg={"cip": {"interval_h": 120, "duration_h": 6}}, lines=lines)
    assert set(st.blocks["line_name"]) == {"P09"}
    assert not (st.blocks["line_id"] == 90).any()
    assert "P99" not in st.line_free_h and "P99" not in st.line_running_free_h
    # the warning is FIRST (staging forwards only the first few warnings) and
    # uses the phrase scenario_runner used for the same case
    assert st.warnings[0].startswith(
        "unknown manprg line(s) skipped (not in lines.csv): P99"), st.warnings
    assert "1 manprg row(s) dropped" in st.warnings[0]
    # an unknown line in cip_info is skipped too
    cips = CipInfoResult(by_line={"P77": CipInfo("P77", None, 120, None, "")})
    st2 = _state([], cips=cips, lines=lines, cfg={"cip": {"duration_h": 6}})
    assert len(st2.blocks) == 0
    assert any("unknown cip_info line skipped (not in lines.csv): P77" in w
               for w in st2.warnings)


# =========================================================================
# data_health: manprg CONTENT age from the as-of stamp
# =========================================================================

def test_data_health_manprg_row_reports_content_age_from_the_stamp(tmp_path):
    sys.path.insert(0, str(ROOT / "tests"))
    import test_data_health as tdh  # noqa: E402  (fixture helpers)
    from helpers import data_health as dh

    dd = tdh._empty_data_dir(tmp_path)
    tdh._min_catalog(dd)
    p1 = _write_manprg(dd / "reference", "manprg.txt")
    p2 = _write_manprg(dd / "reference", "manprg2.txt", mo="30002")
    tdh._touch(p1, 0.1)
    tdh._touch(p2, 0.1)
    # file is fresh (6 min) but the CONTENT was observed 3 h ago -> STALE
    as_of = datetime.now() - timedelta(hours=3)
    (dd / "reference" / ASOF_STAMP_NAME).write_text(json.dumps({
        "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%S"),
        "sha256": content_sha256([p1, p2])}), encoding="utf-8")
    hit = next(h for h in dh.assess(dd, tdh._cfg()) if h.key == "manprg")
    assert hit.state == dh.STALE
    assert "as-of stamp" in hit.detail and "content observed" in hit.detail
    assert hit.age_h == pytest.approx(3.0, abs=0.05)
    # a stamp whose sha no longer matches is ignored -> file mtime (fresh)
    (dd / "reference" / ASOF_STAMP_NAME).write_text(json.dumps({
        "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%S"), "sha256": "deadbeef"}),
        encoding="utf-8")
    hit2 = next(h for h in dh.assess(dd, tdh._cfg()) if h.key == "manprg")
    assert hit2.state == dh.OK and "file mtime" in hit2.detail
