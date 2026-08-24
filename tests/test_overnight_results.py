# tests/test_overnight_results.py — the pure overnight-optimizer reader.
#
# Fixtures live in tests/fixtures/optimizer/ (a realistic generation written
# to the SHARED CONTRACT); each test stages a copy into tmp_path so mutation
# (corrupting files, rewriting timestamps) never touches the fixture.

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers import overnight_results as onr  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "optimizer"
GEN = "20260819-0230-9f3a7c21"
CREATED = datetime(2026, 8, 19, 5, 11, 32)


def _stage(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "optimizer")
    return tmp_path


def _board_path(dd: Path) -> Path:
    return dd / "optimizer" / GEN / "leaderboard.json"


# -- load_latest -------------------------------------------------------------

def test_missing_dir_reads_as_not_set(tmp_path):
    assert onr.load_latest(tmp_path) is None
    assert onr.load_leaderboard(tmp_path) is None
    assert onr.load_brief(tmp_path) is None
    assert onr.summarize(tmp_path).state == onr.NOT_SET


def test_corrupt_latest_reads_as_none(tmp_path):
    dd = _stage(tmp_path)
    (dd / "optimizer" / "latest.json").write_text("{not json", encoding="utf-8")
    assert onr.load_latest(dd) is None
    assert onr.summarize(dd).state == onr.NOT_SET


def test_latest_missing_keys_reads_as_none(tmp_path):
    dd = _stage(tmp_path)
    (dd / "optimizer" / "latest.json").write_text(
        json.dumps({"generation": GEN}), encoding="utf-8")
    assert onr.load_latest(dd) is None


def test_load_latest_returns_pointer(tmp_path):
    dd = _stage(tmp_path)
    latest = onr.load_latest(dd)
    assert latest["generation"] == GEN
    assert latest["leaderboard"] == f"{GEN}/leaderboard.json"


# -- load_leaderboard --------------------------------------------------------

def test_load_leaderboard_resolves_relative_path(tmp_path):
    dd = _stage(tmp_path)
    board = onr.load_leaderboard(dd)
    assert board["generation"] == GEN
    assert len(board["candidates"]) == 7


def test_leaderboard_path_relative_to_data_dir_also_resolves(tmp_path):
    dd = _stage(tmp_path)
    latest = json.loads(
        (dd / "optimizer" / "latest.json").read_text(encoding="utf-8"))
    latest["leaderboard"] = f"optimizer/{GEN}/leaderboard.json"
    (dd / "optimizer" / "latest.json").write_text(
        json.dumps(latest), encoding="utf-8")
    assert onr.load_leaderboard(dd) is not None


def test_pointer_at_missing_leaderboard_reads_as_none(tmp_path):
    dd = _stage(tmp_path)
    _board_path(dd).unlink()
    assert onr.load_leaderboard(dd) is None
    assert onr.summarize(dd).state == onr.NOT_SET


def test_schema_violations_read_as_none(tmp_path):
    dd = _stage(tmp_path)
    board = json.loads(_board_path(dd).read_text(encoding="utf-8"))

    bad = dict(board, candidates=[])
    _board_path(dd).write_text(json.dumps(bad), encoding="utf-8")
    assert onr.load_leaderboard(dd) is None

    bad = dict(board)
    bad["candidates"] = [dict(board["candidates"][0])]
    del bad["candidates"][0]["overnight_score"]
    _board_path(dd).write_text(json.dumps(bad), encoding="utf-8")
    assert onr.load_leaderboard(dd) is None

    bad = dict(board)
    bad["candidates"] = [dict(board["candidates"][0],
                              overnight_score={"composite": "84"})]
    _board_path(dd).write_text(json.dumps(bad), encoding="utf-8")
    assert onr.load_leaderboard(dd) is None


# -- unscorable arms ---------------------------------------------------------
#
# json.dump writes a bare NaN for float('nan'), so an arm the engine failed to
# score arrives as a real float that poisons max() and formats as "nan".

def test_nan_arm_is_dropped_but_the_board_survives(tmp_path):
    dd = _stage(tmp_path)
    board = json.loads(_board_path(dd).read_text(encoding="utf-8"))
    board["candidates"][0]["overnight_score"]["composite"] = float("nan")
    _board_path(dd).write_text(json.dumps(board), encoding="utf-8")

    ov = onr.summarize(dd, now=CREATED + timedelta(hours=2))
    assert ov.state == onr.OK
    assert ov.n_runs == 6                      # champion dropped, 6 remain
    assert ov.best["run_id"] == "noise-1"      # next-highest real score
    assert ov.best_composite == 83.9
    assert "nan" not in ov.detail


def test_board_of_only_unscorable_arms_reads_as_not_set(tmp_path):
    dd = _stage(tmp_path)
    board = json.loads(_board_path(dd).read_text(encoding="utf-8"))
    for c in board["candidates"]:
        c["overnight_score"]["composite"] = float("nan")
    _board_path(dd).write_text(json.dumps(board), encoding="utf-8")
    assert onr.load_leaderboard(dd) is None
    assert onr.summarize(dd).state == onr.NOT_SET


def test_nan_baseline_yields_no_delta(tmp_path):
    dd = _stage(tmp_path)
    board = json.loads(_board_path(dd).read_text(encoding="utf-8"))
    board["board_baseline"]["overnight_score"]["composite"] = float("inf")
    _board_path(dd).write_text(json.dumps(board), encoding="utf-8")
    ov = onr.summarize(dd, now=CREATED + timedelta(hours=2))
    assert ov.board_composite is None and ov.delta_vs_board is None
    assert ov.detail == "7 runs · best 84.2 · noise ±0.6 — review"


# -- brief ---------------------------------------------------------------

def test_load_brief(tmp_path):
    dd = _stage(tmp_path)
    text = onr.load_brief(dd)
    assert text is not None and "Overnight brief" in text


def test_missing_brief_reads_as_none(tmp_path):
    dd = _stage(tmp_path)
    (dd / "optimizer" / "brief.md").unlink()
    assert onr.load_brief(dd) is None


# -- age classification --------------------------------------------------

def test_fresh_generation_is_ok(tmp_path):
    dd = _stage(tmp_path)
    ov = onr.summarize(dd, now=CREATED + timedelta(hours=6))
    assert ov.state == onr.OK
    assert ov.age_h == 6.0


def test_old_generation_is_stale(tmp_path):
    dd = _stage(tmp_path)
    ov = onr.summarize(dd, now=CREATED + timedelta(hours=30))
    assert ov.state == onr.STALE
    assert "30h old" in ov.detail


def test_freshness_boundary_is_26h(tmp_path):
    dd = _stage(tmp_path)
    fresh = onr.summarize(dd, now=CREATED + timedelta(hours=25, minutes=59))
    stale = onr.summarize(dd, now=CREATED + timedelta(hours=26))
    assert fresh.state == onr.OK
    assert stale.state == onr.STALE


def test_unparseable_created_is_stale(tmp_path):
    dd = _stage(tmp_path)
    board = json.loads(_board_path(dd).read_text(encoding="utf-8"))
    board["created"] = "yesterday-ish"
    _board_path(dd).write_text(json.dumps(board), encoding="utf-8")
    ov = onr.summarize(dd)
    assert ov.state == onr.STALE
    assert ov.age_h is None


# -- summarize ------------------------------------------------------------

def test_summarize_stats(tmp_path):
    dd = _stage(tmp_path)
    ov = onr.summarize(dd, now=CREATED + timedelta(hours=2))
    assert ov.generation == GEN
    assert ov.n_runs == 7
    assert ov.best["run_id"] == "champion"
    assert ov.best_composite == 84.2
    assert ov.board_composite == 71.4
    assert round(ov.delta_vs_board, 1) == 12.8
    assert ov.noise_runs == 3
    assert ov.noise_spread == 0.6
    assert [c["published"] for c in ov.published] == ["best", "runner_up"]
    assert ov.detail == "7 runs · best 84.2 (+12.8 vs board) · noise ±0.6 — review"


def test_summarize_without_baseline_and_noise(tmp_path):
    dd = _stage(tmp_path)
    board = json.loads(_board_path(dd).read_text(encoding="utf-8"))
    board["board_baseline"] = None
    board["noise_floor"] = None
    _board_path(dd).write_text(json.dumps(board), encoding="utf-8")
    ov = onr.summarize(dd, now=CREATED + timedelta(hours=2))
    assert ov.board_composite is None
    assert ov.delta_vs_board is None
    assert ov.noise_spread is None
    assert ov.detail == "7 runs · best 84.2 — review"


# -- published block (additive latest.json schema) -------------------------
# Rows carry each published candidate's OWN generation's baseline, so the
# chip never shows a cross-generation delta (the 2026-08-21 +4.54 lie).

OLDER_GEN = "20260818-2100-deadbeef"


def _add_published(dd: Path, rows) -> None:
    p = dd / "optimizer" / "latest.json"
    latest = json.loads(p.read_text(encoding="utf-8"))
    latest["published"] = rows
    p.write_text(json.dumps(latest), encoding="utf-8")


def test_published_block_drives_the_chip_delta(tmp_path):
    dd = _stage(tmp_path)
    _add_published(dd, [
        {"tag": "best", "version_slug": "overnight_best",
         "run_id": "old_champion", "generation": OLDER_GEN,
         "composite": 85.0, "board_baseline_composite": 84.4,
         "delta_same_gen": 0.6},
    ])
    ov = onr.summarize(dd, now=CREATED + timedelta(hours=2))
    assert ov.best_composite == 85.0
    assert ov.board_composite == 84.4
    assert ov.delta_vs_board == 0.6
    assert ov.published_best["run_id"] == "old_champion"
    # published best from an OLDER generation than latest — the chip says so
    assert "+0.6 vs its board, 10h ago" in ov.detail
    # the latest generation's own candidates still fill the leaderboard bits
    assert ov.best["run_id"] == "champion"
    assert ov.n_runs == 7


def test_published_block_same_generation_needs_no_age_note(tmp_path):
    dd = _stage(tmp_path)
    _add_published(dd, [
        {"tag": "best", "run_id": "champion", "generation": GEN,
         "composite": 84.2, "board_baseline_composite": 71.4,
         "delta_same_gen": 12.8},
    ])
    ov = onr.summarize(dd, now=CREATED + timedelta(hours=2))
    assert ov.delta_vs_board == 12.8
    assert "(+12.8 vs its board)" in ov.detail
    assert "ago" not in ov.detail


def test_published_row_without_delta_falls_back_same_generation(tmp_path):
    """A published best whose OWN generation had no baseline never borrows
    the latest generation's board — the chip falls back to the latest
    generation's best vs its own baseline."""
    dd = _stage(tmp_path)
    _add_published(dd, [
        {"tag": "best", "run_id": "old_champion", "generation": OLDER_GEN,
         "composite": 96.0, "board_baseline_composite": None,
         "delta_same_gen": None},
    ])
    ov = onr.summarize(dd, now=CREATED + timedelta(hours=2))
    assert ov.published_best is None
    assert ov.best_composite == 84.2
    assert round(ov.delta_vs_board, 1) == 12.8
    assert "(+12.8 vs board)" in ov.detail


def test_malformed_published_block_is_ignored(tmp_path):
    dd = _stage(tmp_path)
    for bad in ("not-a-list",
                [{"tag": "best"}, 42,
                 {"tag": "best", "generation": GEN, "composite": "84"}]):
        _add_published(dd, bad)
        ov = onr.summarize(dd, now=CREATED + timedelta(hours=2))
        assert ov.published_best is None
        assert round(ov.delta_vs_board, 1) == 12.8  # same-gen fallback


def test_published_entries_validation():
    good = {"tag": "best", "generation": GEN, "composite": 84.2,
            "delta_same_gen": 12.8}
    assert onr.published_entries({"published": [good]}) == [good]
    assert onr.published_entries(None) == []
    assert onr.published_entries({}) == []
    assert onr.published_entries({"published": {}}) == []
    kept = onr.published_entries({"published": [
        good, {"generation": 7, "composite": 1.0},
        {"generation": GEN, "composite": True}, "junk"]})
    assert kept == [good]


# -- same-generation helpers (shared with the Generate table) ---------------

def test_board_baseline_composite_and_all_within_noise():
    board = {"board_baseline": {"overnight_score": {"composite": 70.0}},
             "candidates": [{"overnight_score": {"composite": 70.4}},
                            {"overnight_score": {"composite": 69.1}}]}
    assert onr.board_baseline_composite(board) == 70.0
    assert onr.all_within_noise(board, 1.64)
    assert not onr.all_within_noise(board, 0.5)   # 69.1 is 0.9 off
    assert not onr.all_within_noise(board, None)
    assert onr.board_baseline_composite({"board_baseline": None}) is None
    assert not onr.all_within_noise(
        {"board_baseline": None, "candidates": []}, 1.0)


# -- formatters ------------------------------------------------------------

def test_guards_text():
    ok = {"guards": {"overlaps": 0, "pins_ok": True, "lock_ok": True, "cip_ok": True}}
    bad = {"guards": {"overlaps": 2, "pins_ok": False, "lock_ok": True, "cip_ok": False}}
    assert onr.guards_text(ok) == "OK"
    assert onr.guards_text(bad) == "2 overlaps · pins ✗ · CIP ✗"
    assert onr.guards_text({}) == "—"


def test_budgets_and_published_text():
    c = {"budgets": {"pass1_s": 600, "pass2_s": 300}, "published": "best"}
    assert onr.budgets_text(c) == "600s + 300s"
    assert onr.published_label(c) == "★ best"
    assert onr.published_label({"published": "runner_up"}) == "runner-up"
    assert onr.published_label({"published": None}) == ""
    assert onr.budgets_text({}) == "—"

import pytest  # noqa: E402


# -- the phase ledger: an unfinished night ------------------------------------
# state.json is written by the batch per generation; `published` is the only
# terminal phase. The 2026-08-22 reboot left 20260821-1900-5d421b01 with six
# scored arms, no state.json (pre-ledger), nothing published — the chip must
# name that night instead of reading as merely "stale".

NEWER = "20260820-1900-5d421b01"   # newer than the fixture's 20260819-0230
NOW = datetime(2026, 8, 22, 16, 0)


def _night(dd: Path, phase: str, updated: datetime | None, *,
           gen_id: str = NEWER, state: bool = True, arms_done: int = 6,
           arms_planned: int | None = 6) -> Path:
    gdir = dd / "optimizer" / gen_id
    gdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(_board_path(dd), gdir / "leaderboard.json")
    if state:
        st = {"generation": gen_id, "phase": phase,
              "updated": (updated.isoformat(timespec="seconds")
                          if updated else "garbage"),
              "arms_done": arms_done, "arms_planned": arms_planned,
              "batch": [gen_id]}
        (gdir / "state.json").write_text(json.dumps(st), encoding="utf-8")
    return gdir


def test_interrupted_night_is_the_newest_unfinished_generation(tmp_path):
    dd = _stage(tmp_path)
    _night(dd, "arms_done", datetime(2026, 8, 22, 0, 11))   # 16h of silence
    night = onr.unfinished_night(dd, now=NOW)
    assert night is not None
    assert night.generation == NEWER
    assert night.phase == "arms_done"
    assert night.interrupted
    assert night.age_h == pytest.approx(15.82, abs=0.01)
    assert night.arms_done == 6 and night.arms_planned == 6
    text = onr.night_text(night)
    assert "interrupted in phase arms_done" in text
    assert "6/6 arm(s)" in text and "silent 16h" in text
    assert onr.resume_command(NEWER) == \
        f"python scripts/overnight_batch.py --resume {NEWER}"
    ov = onr.summarize(dd, now=NOW)
    assert ov.night == night
    assert ov.detail.startswith(f"⚠ night {NEWER} interrupted")
    assert "7 runs" in ov.detail   # the published generation's stats follow
    assert ov.generation == GEN    # the pointer still names the published one


def test_running_night_is_not_an_alert(tmp_path):
    dd = _stage(tmp_path)
    _night(dd, "arms_running", NOW - timedelta(minutes=40), arms_done=2)
    night = onr.unfinished_night(dd, now=NOW)
    assert night is not None and not night.interrupted
    assert onr.night_text(night) == \
        "tonight's batch running: arms_running, 2/6 arm(s) done"
    assert onr.summarize(dd, now=NOW).detail.startswith(
        "☾ tonight's batch running")
    # the 5am wait heart-beats every 5 min: a fresh arms_done is sleeping,
    # not dead
    _night(dd, "arms_done", NOW - timedelta(minutes=4))
    assert not onr.unfinished_night(dd, now=NOW).interrupted
    # ... and STALL_H of silence flips it
    _night(dd, "arms_done", NOW - timedelta(hours=onr.STALL_H))
    assert onr.unfinished_night(dd, now=NOW).interrupted


def test_finished_or_superseded_nights_are_quiet(tmp_path):
    dd = _stage(tmp_path)
    _night(dd, "published", datetime(2026, 8, 22, 5, 30))   # terminal
    assert onr.unfinished_night(dd, now=NOW) is None
    shutil.rmtree(dd / "optimizer" / NEWER)
    # an unfinished generation OLDER than the published pointer is history
    _night(dd, "arms_done", datetime(2026, 8, 17, 0, 11),
           gen_id="20260816-1900-00000000")
    assert onr.unfinished_night(dd, now=NOW) is None
    # a dir without a leaderboard is not a generation
    (dd / "optimizer" / "20260823-1900-11111111").mkdir()
    assert onr.unfinished_night(dd, now=NOW) is None
    # no optimizer dir at all
    assert onr.unfinished_night(tmp_path / "nowhere", now=NOW) is None
    assert onr.summarize(dd, now=NOW).night is None
    # two unfinished: the newest wins
    _night(dd, "arms_done", datetime(2026, 8, 21, 0, 11),
           gen_id="20260820-1900-aaaa0000")
    _night(dd, "staged", datetime(2026, 8, 22, 0, 11),
           gen_id="20260821-1900-bbbb0000", arms_done=0)
    assert onr.unfinished_night(dd, now=NOW).generation == \
        "20260821-1900-bbbb0000"


def test_pre_ledger_generation_reads_as_interrupted_unknown(tmp_path):
    """The real 2026-08-21 dir: leaderboard + candidates, no state.json."""
    import os
    dd = _stage(tmp_path)
    gdir = _night(dd, "", None, state=False)
    stamp = datetime(2026, 8, 22, 0, 11).timestamp()
    os.utime(gdir / "leaderboard.json", (stamp, stamp))
    night = onr.unfinished_night(dd, now=NOW)
    assert night.phase == onr.PHASE_UNKNOWN
    assert night.interrupted
    assert night.age_h == pytest.approx(15.82, abs=0.01)
    assert night.arms_done == 7 and night.arms_planned is None
    assert "after 7 arm(s)" in onr.night_text(night)


def test_unreadable_heartbeat_is_interrupted(tmp_path):
    dd = _stage(tmp_path)
    _night(dd, "arms_done", None)   # "garbage" updated
    night = onr.unfinished_night(dd, now=NOW)
    assert night.interrupted and night.age_h is None
    assert "no readable heartbeat" in onr.night_text(night)
    # a phase-less state.json is no state at all -> the pre-ledger rule
    (dd / "optimizer" / NEWER / "state.json").write_text(
        json.dumps({"generation": NEWER}), encoding="utf-8")
    assert onr.load_state(dd, NEWER) is None
    assert onr.unfinished_night(dd, now=NOW).phase == onr.PHASE_UNKNOWN


def test_interrupted_first_night_without_any_pointer(tmp_path):
    """The first-ever night crashes: no latest.json, so the chip stays
    NOT_SET — but the night is named, not swallowed."""
    (tmp_path / "optimizer").mkdir()
    gdir = tmp_path / "optimizer" / NEWER
    gdir.mkdir()
    shutil.copy(FIXTURE / GEN / "leaderboard.json", gdir / "leaderboard.json")
    (gdir / "state.json").write_text(json.dumps(
        {"generation": NEWER, "phase": "arms_done",
         "updated": "2026-08-22T00:11:26", "arms_done": 6}), encoding="utf-8")
    ov = onr.summarize(tmp_path, now=NOW)
    assert ov.state == onr.NOT_SET
    assert ov.night is not None and ov.night.interrupted
    assert "no overnight generation yet" in ov.detail
    assert f"⚠ night {NEWER} interrupted" in ov.detail
