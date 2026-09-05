# tests/test_fix_W.py — write-back / versions / calendar save+export fixes
# (audit 2026-09-03, agent W). Findings: writeback-1, -2, -3, -4, -9, -12,
# -13, quality-8, quality-12. Every expected value is derived BY HAND in the
# comments; no solver is run.
#
# Frames used throughout (all hand-picked):
#   BOARD anchor  (toml planning_start_date)   = 2026-09-02 00:00  (hour 0)
#   ROLLING anchor (today, versions are stamped) = 2026-09-03 00:00
#   shift = version_anchor - board_anchor = +24 h  (add to version hours)
#   "now" = 2026-09-03 12:00  ->  board hour 36, rolling hour 12

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers import version_manager as vm  # noqa: E402
from helpers.calendar_io import (  # noqa: E402
    CALENDAR_COLUMNS,
    import_solver_schedule,
    load_calendar,
    reconcile_block_identity,
    save_calendar,
)
from helpers.week_lock import (  # noqa: E402
    LockViolation,
    check_lock_violations,
    default_lock_through,
    read_lock,
    write_lock,
)

BOARD_ANCHOR = datetime(2026, 9, 2, 0, 0, 0)
ROLL_ANCHOR = datetime(2026, 9, 3, 0, 0, 0)
NOW = datetime(2026, 9, 3, 12, 0, 0)
NOW_BOARD_H = 36.0     # (NOW - BOARD_ANCHOR) = 1 d 12 h
SHIFT_H = 24.0         # ROLL_ANCHOR - BOARD_ANCHOR


def _row(**kw) -> dict:
    base = {
        "block_id": "b", "block_type": "production", "line_id": 0,
        "line_name": "P09", "start_h": 0.0, "end_h": 1.0, "label": "",
        "order_id": "", "sku": "", "sku_description": "", "qty_kg": None,
        "locked": False, "attrs": "",
    }
    base.update(kw)
    return base


def _cal(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=CALENDAR_COLUMNS)
    for c in ("start_h", "end_h", "qty_kg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# The synthetic BOARD (board frame, hours from 2026-09-02 00:00):
#   cs_run   P09 running MO 30001, sku 111, 20-44 h   (current_state:running)
#   cs_q     P09 queued  MO 30002, sku 222, 44-60 h   (current_state:queued)
#   cs_sp    P10 split MO 30003 sku 333: pieces 10-30 and 36-50 (ONE id, ;split)
#   cip_1    P10 CIP 30-36 h                          (between the split pieces)
#   blk_p    P09 planner-PINNED demand block 120448-W1, sku 444, 100-110 h
#   prod_old P09 finished block 120440-W0, sku 555, 0-18 h  (end 18 <= now 36)
# Downtimes are drawn from downtimes.csv (display-only 'dt_' ids), never
# stored — one is included in the export payload to prove it is carried.
# ---------------------------------------------------------------------------
def _board() -> pd.DataFrame:
    return _cal([
        _row(block_id="cs_run", line_id=0, line_name="P09", start_h=20.0,
             end_h=44.0, label="111", order_id="30001", sku="111",
             qty_kg=24000.0, locked=True, attrs="current_state:running"),
        _row(block_id="cs_q", line_id=0, line_name="P09", start_h=44.0,
             end_h=60.0, label="222", order_id="30002", sku="222",
             qty_kg=16000.0, locked=True, attrs="current_state:queued"),
        _row(block_id="cs_sp", line_id=1, line_name="P10", start_h=10.0,
             end_h=30.0, label="333", order_id="30003", sku="333",
             qty_kg=20000.0, locked=True, attrs="current_state:running;split"),
        _row(block_id="cs_sp", line_id=1, line_name="P10", start_h=36.0,
             end_h=50.0, label="333", order_id="30003", sku="333",
             qty_kg=14000.0, locked=True, attrs="current_state:running;split"),
        _row(block_id="cip_1", block_type="cip", line_id=1, line_name="P10",
             start_h=30.0, end_h=36.0, label="CIP", sku="CIP",
             attrs="current_state:cip"),
        _row(block_id="blk_p", line_id=0, line_name="P09", start_h=100.0,
             end_h=110.0, label="444", order_id="120448-W1", sku="444",
             qty_kg=10000.0, attrs="pinned"),
        _row(block_id="prod_old", line_id=0, line_name="P09", start_h=0.0,
             end_h=18.0, label="555", order_id="120440-W0", sku="555",
             qty_kg=18000.0),
    ])


# The VERSION Scenario F would save from that board, in the ROLLING frame
# (hours from 2026-09-03 00:00 = board hour 24):
#   cs_run  re-forecast (trimmed) : rolling 0-16   -> board 24-40
#   cs_q    moved queued MO       : rolling 16-30  -> board 40-54
#   cs_sp   split pieces          : rolling -14..6 and 12..26 -> board 10-30, 36-50
#   cip_1                         : rolling 6-12   -> board 30-36
#   blk_p   pinned, copied VERBATIM from the board (board frame!) 100-110
#   prod_f  new fill block 120450-W2 sku 666, rolling 40-50 -> board 64-74
def _version_cal() -> pd.DataFrame:
    return _cal([
        _row(block_id="cs_run", line_id=0, line_name="P09", start_h=0.0,
             end_h=16.0, label="111", order_id="30001", sku="111",
             qty_kg=16000.0, locked=True, attrs="current_state:running"),
        _row(block_id="cs_q2", line_id=0, line_name="P09", start_h=16.0,
             end_h=30.0, label="222", order_id="30002", sku="222",
             qty_kg=14000.0, locked=True, attrs="current_state:queued"),
        _row(block_id="cs_sp", line_id=1, line_name="P10", start_h=-14.0,
             end_h=6.0, label="333", order_id="30003", sku="333",
             qty_kg=20000.0, locked=True, attrs="current_state:running;split"),
        _row(block_id="cs_sp", line_id=1, line_name="P10", start_h=12.0,
             end_h=26.0, label="333", order_id="30003", sku="333",
             qty_kg=14000.0, locked=True, attrs="current_state:running;split"),
        _row(block_id="cip_1", block_type="cip", line_id=1, line_name="P10",
             start_h=6.0, end_h=12.0, label="CIP", sku="CIP",
             attrs="current_state:cip"),
        _row(block_id="blk_p", line_id=0, line_name="P09", start_h=100.0,
             end_h=110.0, label="444", order_id="120448-W1", sku="444",
             qty_kg=10000.0, attrs="pinned"),
        _row(block_id="prod_f", line_id=0, line_name="P09", start_h=40.0,
             end_h=50.0, label="666", order_id="120450-W2", sku="666",
             qty_kg=10000.0),
    ])


@pytest.fixture
def dd(tmp_path, monkeypatch) -> Path:
    """data dir with the board on disk; board anchor pinned to 2026-09-02."""
    d = tmp_path / "data"
    (d / "versions").mkdir(parents=True)
    save_calendar(_board(), d / "calendar_blocks.csv")
    monkeypatch.setattr(vm, "planning_anchor", lambda *a, **k: BOARD_ANCHOR)
    return d


def _write_version(dd: Path, slug: str, cal: pd.DataFrame,
                   anchor: datetime | None) -> None:
    vd = dd / "versions" / slug
    vd.mkdir(parents=True, exist_ok=True)
    save_calendar(cal, vd / "calendar_blocks.csv")
    meta = {"name": slug, "timestamp": "2026-09-03T08:00:00",
            "source": "solver:F", "scorecard": {}}
    if anchor is not None:
        meta["planning_anchor"] = f"{anchor:%Y-%m-%d %H:%M:%S}"
    (vd / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def _by_id(cal: pd.DataFrame) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for r in cal.to_dict("records"):
        out.setdefault(str(r["block_id"]), []).append(
            (float(r["start_h"]), float(r["end_h"])))
    for v in out.values():
        v.sort()
    return out


# ===========================================================================
# writeback-1: promote applies the frame shift EXACTLY ONCE per block
# ===========================================================================
def test_promote_shifts_each_block_once_pinned_keeps_wall_clock(dd):
    """Version stamped yesterday+1 (rolling 2026-09-03) onto a board anchored
    2026-09-02: shift = +24 h for every rolling-frame row; the pinned block
    copied verbatim from the board (same id + same hours) is shifted ZERO
    times and stays at board hour 100 = 2026-09-06 04:00 wall clock.
    Before the fix it landed at 124 h = 2026-09-07 04:00 (one day late)."""
    _write_version(dd, "f_run", _version_cal(), ROLL_ANCHOR)
    res = vm.promote_version("f_run", dd)
    assert res["promoted"] is True
    assert res["shift_h"] == SHIFT_H
    assert res["verbatim_rows"] == 1                     # blk_p only
    board = load_calendar(dd / "calendar_blocks.csv")
    pos = _by_id(board)
    # rolling -> board: +24 h each, applied once
    assert pos["cs_run"] == [(24.0, 40.0)]              # 0-16 + 24
    assert pos["cs_sp"] == [(10.0, 30.0), (36.0, 50.0)]  # -14..6 / 12..26 + 24
    assert pos["cip_1"] == [(30.0, 36.0)]               # 6-12 + 24
    assert pos["prod_f"] == [(64.0, 74.0)]              # 40-50 + 24
    # the pinned block: verbatim board copy, NOT shifted
    assert pos["blk_p"] == [(100.0, 110.0)]
    wall = BOARD_ANCHOR + pd.Timedelta(hours=100)
    assert wall == datetime(2026, 9, 6, 4, 0, 0)
    # finished production that the version did not carry is gone from the
    # board only because this version did not include it — see the
    # writeback-3 test for the save path that must include it.
    assert "prod_old" not in pos


def test_promote_still_shifts_a_rebased_pinned_row(dd):
    """Robustness to the staging-side fix (C-9: pinned rows rebased into the
    rolling frame before staging): a pinned row at rolling 76-86 (= board
    100-110 - 24) no longer shares the board's hours, so it is NOT a verbatim
    copy and gets the +24 h shift -> board 100-110. Same wall clock either
    way; the shift is applied exactly once in both worlds."""
    ver = _version_cal()
    ver.loc[ver["block_id"] == "blk_p", ["start_h", "end_h"]] = [76.0, 86.0]
    _write_version(dd, "f_rebased", ver, ROLL_ANCHOR)
    res = vm.promote_version("f_rebased", dd)
    assert res["verbatim_rows"] == 0
    pos = _by_id(load_calendar(dd / "calendar_blocks.csv"))
    assert pos["blk_p"] == [(100.0, 110.0)]


def test_current_state_rows_never_count_as_verbatim(dd):
    """A current_state row whose deterministic id AND hours happen to equal
    a board row's is still shifted: cs ids can collide across frames, and
    current_state rows are always re-staged in the version's own frame."""
    ver = _cal([_row(block_id="cs_run", line_id=0, line_name="P09",
                     start_h=20.0, end_h=44.0, order_id="30001", sku="111",
                     locked=True, attrs="current_state:running")])
    _write_version(dd, "f_cs", ver, ROLL_ANCHOR)
    res = vm.promote_version("f_cs", dd)
    assert res["verbatim_rows"] == 0
    pos = _by_id(load_calendar(dd / "calendar_blocks.csv"))
    assert pos["cs_run"] == [(44.0, 68.0)]              # 20-44 + 24


def test_save_version_stamps_the_caller_frame(dd, monkeypatch):
    """The Plant Calendar saves a BOARD-frame calendar; with the explicit
    planning_anchor it promotes with shift 0 (hours unchanged). Before the
    fix it was stamped with the rolling anchor -> +24 h on promote."""
    slug = vm.save_version("twin", _board(), {}, dd, source="digital_twin",
                           planning_anchor=BOARD_ANCHOR)
    meta = json.loads((dd / "versions" / slug / "metadata.json").read_text())
    assert meta["planning_anchor"] == "2026-09-02 00:00:00"
    assert vm.board_frame_shift_h(slug, dd) == 0.0
    res = vm.promote_version(slug, dd)
    assert res["shift_h"] == 0.0
    pos = _by_id(load_calendar(dd / "calendar_blocks.csv"))
    assert pos["blk_p"] == [(100.0, 110.0)] and pos["cs_run"] == [(20.0, 44.0)]


# ===========================================================================
# writeback-2 / writeback-12: export in the VERSION's frame, finished rows
# included and flagged
# ===========================================================================
def _sheet(xbytes: bytes, name: str) -> pd.DataFrame:
    return pd.read_excel(BytesIO(xbytes), sheet_name=name)


def test_version_export_uses_the_versions_own_anchor(dd):
    """Version anchored board+72 h holding start_h 60 means wall clock
    board_anchor + 132 h = 2026-09-02 00:00 + 5 d 12 h = 2026-09-07 12:00.
    The old export stamped 2026-09-04 12:00 (board frame, 72 h early)."""
    v_anchor = datetime(2026, 9, 5, 0, 0, 0)            # board + 72 h
    ver = _cal([_row(block_id="x", start_h=60.0, end_h=70.0,
                     order_id="O1", sku="1"),
                _row(block_id="y", start_h=10.0, end_h=20.0,
                     order_id="O2", sku="2")])
    _write_version(dd, "late", ver, v_anchor)
    # now = version hour 65 -> y (ends 20) completed, x (ends 70) planned
    xb = vm.export_version_excel("late", dd, now=v_anchor + pd.Timedelta(hours=65))
    cal = _sheet(xb, "Calendar").set_index("block_id")
    assert str(cal.loc["x", "start"]) == "2026-09-07 12:00"
    assert str(cal.loc["x", "end"]) == "2026-09-07 22:00"
    assert cal.loc["x", "status"] == "planned"
    assert cal.loc["y", "status"] == "completed"
    frame = _sheet(xb, "Frame").set_index("key")
    assert frame.loc["planning_anchor", "value"] == "2026-09-05 00:00:00"
    # promote agrees: start_h 60 + 72 = 132 board hours
    vm.promote_version("late", dd)
    pos = _by_id(load_calendar(dd / "calendar_blocks.csv"))
    assert pos["x"] == [(132.0, 142.0)]


def test_board_export_includes_finished_rows_flagged_completed():
    """The whole board incl. prod_old (end 18 <= now 36) plus a downtime
    window; twice -> identical bytes; 'status' = completed only for rows that
    ended by now. Anchor explicit, no wall clock involved."""
    board = _board()
    board = pd.concat([board, _cal([
        _row(block_id="dt_0", block_type="line_down", line_id=2,
             line_name="P11", start_h=200.0, end_h=212.0, label="Down",
             attrs="ref_downtime")])], ignore_index=True)
    a = vm.export_calendar_excel(board, {"cip": {"count": 1}},
                                 anchor=BOARD_ANCHOR, now_h=NOW_BOARD_H)
    b = vm.export_calendar_excel(board, {"cip": {"count": 1}},
                                 anchor=BOARD_ANCHOR, now_h=NOW_BOARD_H)
    sa, sb = _sheet(a, "Calendar"), _sheet(b, "Calendar")
    pd.testing.assert_frame_equal(sa, sb)
    assert len(sa) == len(board) == 8
    # openpyxl reads whole-hour starts back as ints -> key on "%g"
    st = sa.set_index(sa["block_id"] + "@" + sa["start_h"].astype(float).map(
        lambda v: f"{v:g}"))["status"]
    assert st["prod_old@0"] == "completed"                # end 18 <= 36
    assert st["cs_sp@10"] == "completed"                  # end 30 <= 36
    assert st["cs_run@20"] == "planned"                   # end 44 > 36
    assert st["cip_1@30"] == "completed"                  # end 36 <= 36
    assert st["blk_p@100"] == "planned"
    assert st["dt_0@200"] == "planned"
    # wall clock of the pinned block from the board anchor
    row = sa[sa["block_id"] == "blk_p"].iloc[0]
    assert str(row["start"]) == "2026-09-06 04:00"
    assert "Scorecard" in pd.ExcelFile(BytesIO(a)).sheet_names


def test_export_without_now_has_no_status_column():
    xb = vm.export_calendar_excel(_board(), None, anchor=BOARD_ANCHOR)
    assert "status" not in _sheet(xb, "Calendar").columns


# ===========================================================================
# writeback-3: the Plant Calendar's version save / export carry the hidden
# past rows (source-level guard: the page cannot be executed under pytest)
# ===========================================================================
def test_calendar_page_passes_the_full_board_to_version_and_export():
    src = (Path(__file__).resolve().parent.parent / "code" / "pages"
           / "calendar.py").read_text(encoding="utf-8")
    assert "_full_board = drop_display_overlays(" in src
    assert "pd.concat([_past_rows, working]" in src
    # the old payloads must be gone from BOTH write paths
    assert "save_version(\n                save_name or \"Option\",\n                drop_display_overlays(working)" not in src
    assert "export_calendar_excel(\n        drop_display_overlays(working)" not in src
    # every remaining save_version / export call hands over the full board
    assert src.count("_full_board") >= 3
    assert "planning_anchor=_anchor" in src               # frame stamp fix


# ===========================================================================
# writeback-4: board identity + locks survive import / promote
# ===========================================================================
def _write_schedule(path: Path, rows: list[tuple]) -> None:
    pd.DataFrame(rows, columns=["order_id", "sku", "line_id", "line_name",
                                "start_hour", "end_hour", "qty_kg",
                                "is_trial"]).to_csv(path, index=False)


def test_import_keeps_board_identity_for_unchanged_work(tmp_path):
    """Board (board frame): blk_a O1 P09 10-20 locked+pinned; cs_sp O2 P10
    split pieces 0-5 and 7-12 (one id); cip_c P10 12-18.
    Solver schedule (same frame): O1 at 10.2 (|0.2| <= 0.5 -> match),
    O2 at 0.0 and 7.1 (both pieces match -> both get cs_sp), O3 at 30 (new),
    CIP at 12.0 (match). Expected ids: blk_a, cs_sp, cs_sp, cip_c, and a
    fresh prod_* for O3; blk_a keeps locked=True and 'pinned'."""
    board = _cal([
        _row(block_id="blk_a", order_id="O1", sku="1", line_id=0,
             line_name="P09", start_h=10.0, end_h=20.0, locked=True,
             attrs="pinned"),
        _row(block_id="cs_sp", order_id="O2", sku="2", line_id=1,
             line_name="P10", start_h=0.0, end_h=5.0, locked=True,
             attrs="current_state:running;split"),
        _row(block_id="cs_sp", order_id="O2", sku="2", line_id=1,
             line_name="P10", start_h=7.0, end_h=12.0, locked=True,
             attrs="current_state:running;split"),
        _row(block_id="cip_c", block_type="cip", line_id=1, line_name="P10",
             start_h=12.0, end_h=18.0, sku="CIP"),
    ])
    sched = tmp_path / "schedule_phase2.csv"
    _write_schedule(sched, [
        ("O1", "1", 0, "P09", 10.2, 20.2, 5000, False),
        ("O2", "2", 1, "P10", 0.0, 5.0, 2500, False),
        ("O2", "2", 1, "P10", 7.1, 12.1, 2500, False),
        ("O3", "3", 0, "P09", 30.0, 40.0, 5000, False),
    ])
    cip = tmp_path / "cip_windows.csv"
    pd.DataFrame([{"line_id": 1, "line_name": "P10", "start_hour": 12.0,
                   "end_hour": 18.0}]).to_csv(cip, index=False)

    plain = import_solver_schedule(sched, cip)
    assert not plain["locked"].any()                     # old behaviour intact
    assert all(str(i).startswith(("prod_", "cip_")) for i in plain["block_id"])

    out = import_solver_schedule(sched, cip, board=board)
    by = {(str(r["order_id"]), float(r["start_h"])): r
          for r in out.to_dict("records")}
    assert by[("O1", 10.2)]["block_id"] == "blk_a"
    assert by[("O1", 10.2)]["locked"] is True or by[("O1", 10.2)]["locked"] == True  # noqa: E712
    assert "pinned" in str(by[("O1", 10.2)]["attrs"]).split(";")
    assert by[("O2", 0.0)]["block_id"] == "cs_sp"
    assert by[("O2", 7.1)]["block_id"] == "cs_sp"
    assert str(by[("O3", 30.0)]["block_id"]).startswith("prod_")   # genuinely new
    assert not by[("O3", 30.0)]["locked"]
    cips = out[out["block_type"] == "cip"]
    assert list(cips["block_id"]) == ["cip_c"]


def test_reconcile_uses_each_board_row_once_and_honours_frame_shift():
    """Board in a frame 24 h AHEAD of the new calendar (board hour = new
    hour + 24): board O1 at 34 matches new O1 at 10 with board_shift_h=-24.
    Two new rows near ONE board row: the nearer (10.0) takes the id, the
    other (10.4, also within tol) stays fresh."""
    board = _cal([_row(block_id="B1", order_id="O1", sku="1", line_name="P09",
                       start_h=34.0, end_h=44.0, locked=True)])
    new = _cal([
        _row(block_id="n1", order_id="O1", sku="1", line_name="P09",
             start_h=10.4, end_h=20.4),
        _row(block_id="n2", order_id="O1", sku="1", line_name="P09",
             start_h=10.0, end_h=20.0),
    ])
    out, stats = reconcile_block_identity(new, board, board_shift_h=-24.0)
    ids = dict(zip(out["start_h"], out["block_id"]))
    assert ids[10.0] == "B1" and ids[10.4] == "n1"
    assert stats == {"matched": 1, "new": 1}
    # a different line never matches
    other = new.copy()
    other["line_name"] = "P10"
    out2, stats2 = reconcile_block_identity(other, board, board_shift_h=-24.0)
    assert stats2 == {"matched": 0, "new": 2}
    assert set(out2["block_id"]) == {"n1", "n2"}


def test_promote_carries_locks_for_matching_blocks(dd):
    """A version whose fill row sits where the board's LOCKED pinned block is
    (same order/line/start after the frame shift) inherits blk_p's id and
    lock instead of a fresh id: version 'prod_x' 120448-W1 at rolling 76
    -> board 100 == blk_p's start -> block_id blk_p, locked True, pinned."""
    ver = _cal([_row(block_id="prod_x", line_id=0, line_name="P09",
                     start_h=76.0, end_h=86.0, order_id="120448-W1",
                     sku="444", qty_kg=10000.0)])
    board = load_calendar(dd / "calendar_blocks.csv")
    board.loc[board["block_id"] == "blk_p", "locked"] = True
    save_calendar(board, dd / "calendar_blocks.csv")
    _write_version(dd, "f_match", ver, ROLL_ANCHOR)
    res = vm.promote_version("f_match", dd)
    assert res["matched"] == 1 and res["new"] == 0
    out = load_calendar(dd / "calendar_blocks.csv")
    assert list(out["block_id"]) == ["blk_p"]
    assert bool(out.iloc[0]["locked"]) is True
    assert "pinned" in str(out.iloc[0]["attrs"]).split(";")
    assert float(out.iloc[0]["start_h"]) == 100.0


# ===========================================================================
# writeback-9: the 2-week lock is enforced server-side
# ===========================================================================
def test_check_lock_violations_hand_cases():
    """lock at 336 h. Old board: A @100, B @400. Cases:
    (a) identical -> []; (b) A moved to 101 -> 1 removed + 1 added = 2 lines;
    (c) B moved to 401 -> [] (starts after the lock); (d) new block C @50
    -> 1 'added'; (e) A deleted -> 1 'moved/removed'; (f) A's kg 0 vs None
    (Gantt placeholder round trip) -> []; (g) no lock -> []."""
    old = _cal([_row(block_id="A", order_id="O1", sku="1", start_h=100.0,
                     end_h=110.0, qty_kg=0.0),
                _row(block_id="B", order_id="O2", sku="2", start_h=400.0,
                     end_h=410.0)])
    assert check_lock_violations(old.copy(), old, 336.0) == []
    b = old.copy(); b.loc[b["block_id"] == "A", ["start_h", "end_h"]] = [101.0, 111.0]
    v = check_lock_violations(b, old, 336.0)
    assert len(v) == 2 and any("moved/removed" in x for x in v) \
        and any("added" in x for x in v)
    c = old.copy(); c.loc[c["block_id"] == "B", ["start_h", "end_h"]] = [401.0, 411.0]
    assert check_lock_violations(c, old, 336.0) == []
    d = pd.concat([old, _cal([_row(block_id="C", order_id="O3", sku="3",
                                   start_h=50.0, end_h=60.0)])])
    assert [x[:5] for x in check_lock_violations(d, old, 336.0)] == ["added"]
    e = old[old["block_id"] != "A"]
    assert [x[:13] for x in check_lock_violations(e, old, 336.0)] == ["moved/removed"]
    f = old.copy(); f["qty_kg"] = [None, None]
    assert check_lock_violations(f, old, 336.0) == []
    assert check_lock_violations(b, old, None) == []


def test_save_calendar_refuses_a_move_inside_the_lock(tmp_path):
    d = tmp_path / "data"; d.mkdir()
    p = d / "calendar_blocks.csv"
    save_calendar(_board(), p)
    moved = _board()
    moved.loc[moved["block_id"] == "blk_p", ["start_h", "end_h"]] = [104.0, 114.0]
    with pytest.raises(LockViolation) as exc:
        save_calendar(moved, p, lock_h=336.0)
    assert "blk_p" in str(exc.value) and len(exc.value.violations) == 2
    # nothing written, no backup taken for a refused save
    assert _by_id(load_calendar(p))["blk_p"] == [(100.0, 110.0)]
    assert not (d / "_backups").exists()
    # explicit override writes and reports the violations
    res = save_calendar(moved, p, lock_h=336.0, lock_override=True)
    assert len(res["lock_violations"]) == 2 and res["backup"]
    assert _by_id(load_calendar(p))["blk_p"] == [(104.0, 114.0)]
    # moving a locked block OUT of the window is a change to the committed
    # weeks too (its slot empties) -> refused
    out_of = load_calendar(p)
    out_of.loc[out_of["block_id"] == "blk_p", ["start_h", "end_h"]] = [400.0, 410.0]
    with pytest.raises(LockViolation):
        save_calendar(out_of, p, lock_h=336.0)
    # a block that starts beyond the lock (L @ 400) may move freely (-> 420)
    with_late = pd.concat([load_calendar(p), _cal([_row(
        block_id="L", order_id="O9", sku="9", start_h=400.0, end_h=410.0)])])
    save_calendar(with_late, p, lock_h=336.0)          # adding beyond: fine
    free = load_calendar(p)
    free.loc[free["block_id"] == "L", ["start_h", "end_h"]] = [420.0, 430.0]
    res2 = save_calendar(free, p, lock_h=336.0)
    assert res2["lock_violations"] == []
    assert _by_id(load_calendar(p))["L"] == [(420.0, 430.0)]


def test_promote_refuses_when_the_version_replans_the_locked_weeks(dd):
    """lock_state.json = board anchor + 336 h (charter default). The F
    version trims the running MO (24-40 vs 20-44 on the board) -> inside
    the lock -> promote REFUSED, official untouched, no pre-promote backup.
    With lock_override=True it promotes."""
    write_lock(dd, default_lock_through(BOARD_ANCHOR))
    assert read_lock(dd) == datetime(2026, 9, 16, 0, 0, 0)
    _write_version(dd, "f_run", _version_cal(), ROLL_ANCHOR)
    before = (dd / "calendar_blocks.csv").read_bytes()
    with pytest.raises(LockViolation) as exc:
        vm.promote_version("f_run", dd)
    assert exc.value.locked_through_h == 336.0
    assert (dd / "calendar_blocks.csv").read_bytes() == before
    assert not list((dd / "_backups").glob("*")) if (dd / "_backups").exists() else True
    res = vm.promote_version("f_run", dd, lock_override=True)
    assert res["promoted"] and res["lock_h"] == 336.0
    assert any("lock override" in w for w in res["warnings"])
    assert _by_id(load_calendar(dd / "calendar_blocks.csv"))["cs_run"] == [(24.0, 40.0)]


def test_promote_passes_when_only_the_flexible_week_changes(dd):
    """Lock through board hour 60 (just after the queued MO): the version
    keeps every committed row where it is and only adds fill at 64 -> no
    violation, promote goes through."""
    write_lock(dd, BOARD_ANCHOR + pd.Timedelta(hours=60))
    ver = _board()
    ver = pd.concat([ver, _cal([_row(block_id="prod_f", order_id="120450-W2",
                                     sku="666", start_h=64.0, end_h=74.0)])])
    _write_version(dd, "twin", ver, BOARD_ANCHOR)
    res = vm.promote_version("twin", dd)
    assert res["promoted"] and res["lock_h"] == 60.0
    assert "prod_f" in _by_id(load_calendar(dd / "calendar_blocks.csv"))


# ===========================================================================
# writeback-13: backups from every writer + stale-write detection
# ===========================================================================
def test_save_calendar_backs_up_the_board_and_detects_a_stale_write(tmp_path):
    d = tmp_path / "data"; d.mkdir()
    p = d / "calendar_blocks.csv"
    r0 = save_calendar(_board(), p)
    assert r0["backup"] is None                       # first write: nothing to back up
    loaded_mtime = p.stat().st_mtime
    # someone else writes in between (force a visibly older recorded mtime)
    os.utime(p, (loaded_mtime - 100, loaded_mtime - 100))
    stale_mtime = loaded_mtime - 100
    save_calendar(_board(), p)                        # "another tab" saves
    r = save_calendar(_board(), p, expected_mtime=stale_mtime)
    assert r["stale"] is True
    assert any("changed on disk" in w for w in r["warnings"])
    backups = sorted((d / "_backups").glob("calendar_blocks.*.csv"))
    assert len(backups) == 2                          # one per overwrite
    assert r["backup"] and Path(r["backup"]).exists()
    # the backup IS the clobbered board (same rows)
    pd.testing.assert_frame_equal(
        load_calendar(Path(r["backup"])).reset_index(drop=True),
        load_calendar(p).reset_index(drop=True))
    # same-mtime save: not stale
    r2 = save_calendar(_board(), p, expected_mtime=p.stat().st_mtime)
    assert r2["stale"] is False


def test_version_and_workdir_files_are_not_backed_up(tmp_path):
    vdir = tmp_path / "data" / "versions" / "v1"
    vdir.mkdir(parents=True)
    save_calendar(_board(), vdir / "calendar_blocks.csv")
    save_calendar(_board(), vdir / "calendar_blocks.csv")
    assert not (vdir / "_backups").exists()
    assert not (tmp_path / "data" / "_backups").exists()
    w = tmp_path / "work"; w.mkdir()
    save_calendar(_board(), w / "x.calendar.csv")
    save_calendar(_board(), w / "x.calendar.csv")
    assert not (w / "_backups").exists()
    # explicit dir forces one anywhere; None disables even for the board
    r = save_calendar(_board(), w / "x.calendar.csv", backup_dir=w / "bk")
    assert Path(r["backup"]).parent == w / "bk"
    d = tmp_path / "data"
    save_calendar(_board(), d / "calendar_blocks.csv", backup_dir=None)
    r3 = save_calendar(_board(), d / "calendar_blocks.csv", backup_dir=None)
    assert r3["backup"] is None and not (d / "_backups").exists()


def test_promote_makes_exactly_one_backup(dd):
    _write_version(dd, "f_run", _version_cal(), ROLL_ANCHOR)
    res = vm.promote_version("f_run", dd)
    files = list((dd / "_backups").glob("calendar_blocks.*.csv"))
    assert len(files) == 1 and files[0].name.startswith("calendar_blocks.pre-promote.")
    assert res["backup"] == str(files[0])
    # the backup restores the pre-promote board row for row
    assert _by_id(load_calendar(files[0])) == _by_id(_board())


# ===========================================================================
# quality-8: corrupt hours are dropped with a warning, never moved to 0
# ===========================================================================
def test_load_calendar_drops_unreadable_hours_with_a_warning(tmp_path):
    p = tmp_path / "calendar_blocks.csv"
    p.write_text(
        "block_id,block_type,line_id,line_name,start_h,end_h,label,order_id,"
        "sku,sku_description,qty_kg,locked,attrs\n"
        "good,production,0,P09,100,124,,O1,1,,1000,False,\n"
        "blank,production,0,P09,,124,,O2,2,,1000,False,\n"
        "text,production,0,P09,n/a,124,,O3,3,,1000,False,\n"
        "noend,production,0,P09,10,,,O4,4,,1000,False,\n",
        encoding="utf-8")
    notes: list[str] = []
    cal = load_calendar(p, warnings=notes)
    assert list(cal["block_id"]) == ["good"]
    assert float(cal.iloc[0]["start_h"]) == 100.0        # untouched
    assert len(notes) == 3 and cal.attrs["load_warnings"] == notes
    assert {n.split("block ")[1].split(" ")[0] for n in notes} == {"blank", "text", "noend"}
    assert all("NOT moved to hour 0" in n for n in notes)
    with pytest.raises(ValueError, match="corrupt calendar rows"):
        load_calendar(p, strict=True)
    # a clean file: no warnings, attrs present and empty
    save_calendar(cal, p)
    clean = load_calendar(p)
    assert clean.attrs["load_warnings"] == [] and len(clean) == 1


# ===========================================================================
# quality-12: no frame stamp -> no silent promote
# ===========================================================================
def test_unstamped_version_does_not_promote_silently(dd):
    _write_version(dd, "legacy", _version_cal(), anchor=None)
    before = (dd / "calendar_blocks.csv").read_bytes()
    with pytest.raises(ValueError, match="planning_anchor"):
        vm.promote_version("legacy", dd)
    assert (dd / "calendar_blocks.csv").read_bytes() == before
    assert not (dd / "_backups").exists()
    # the planner can assert the frame explicitly: hours pass through (+0)
    res = vm.promote_version("legacy", dd, assume_board_frame=True)
    assert res["promoted"] and res["shift_h"] == 0.0
    assert _by_id(load_calendar(dd / "calendar_blocks.csv"))["cs_run"] == [(0.0, 16.0)]


def test_malformed_stamp_is_refused_too(dd):
    vd = dd / "versions" / "bad"
    vd.mkdir()
    save_calendar(_version_cal(), vd / "calendar_blocks.csv")
    (vd / "metadata.json").write_text(json.dumps(
        {"name": "bad", "planning_anchor": "yesterday-ish"}), encoding="utf-8")
    assert vm.board_frame_shift_h("bad", dd) is None
    with pytest.raises(ValueError, match="planning_anchor"):
        vm.promote_version("bad", dd)


def test_save_version_never_writes_an_unstamped_version(dd, monkeypatch):
    """The stamp is load-bearing: when it cannot be produced the save RAISES
    instead of writing metadata without planning_anchor (the old
    try/except/pass). Also: a garbage anchor string is refused rather than
    silently becoming the 2026-02-15 default."""
    from helpers import horizon as hz
    monkeypatch.setattr(hz, "resolve", lambda *a, **k: (_ for _ in ()).throw(
        PermissionError("toml unreadable")))
    with pytest.raises(ValueError, match="time frame"):
        vm.save_version("unstamped", _board(), {}, dd)
    assert not any(d.name.startswith("unstamped") for d in (dd / "versions").iterdir())
    with pytest.raises(ValueError, match="time frame"):
        vm.save_version("garbage", _board(), {}, dd, planning_anchor="not a date")
    # explicit anchor bypasses the (broken) horizon entirely
    slug = vm.save_version("explicit", _board(), {}, dd, planning_anchor="2026-09-02 00:00:00")
    meta = json.loads((dd / "versions" / slug / "metadata.json").read_text())
    assert meta["planning_anchor"] == "2026-09-02 00:00:00"
    slug2 = vm.upsert_version("fixed_slug", "n", _board(), {}, dd,
                              planning_anchor=BOARD_ANCHOR)
    meta2 = json.loads((dd / "versions" / slug2 / "metadata.json").read_text())
    assert meta2["planning_anchor"] == "2026-09-02 00:00:00"


# ===========================================================================
# the lock file itself is written atomically
# ===========================================================================
def test_write_lock_is_atomic_and_round_trips(tmp_path):
    write_lock(tmp_path, datetime(2026, 9, 16))
    assert read_lock(tmp_path) == datetime(2026, 9, 16)
    assert not list(tmp_path.glob("*.tmp"))
    write_lock(tmp_path, None)
    assert read_lock(tmp_path) is None
