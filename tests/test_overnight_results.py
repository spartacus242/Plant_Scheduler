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
