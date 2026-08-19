# tests/test_scenario_naming.py — timestamped, honest scenario version names.
#
# User request 2026-08-19: every scenario run saves a NEW version whose name
# says what the solver actually did plus the run's start time — no more fixed
# slug silently overwriting the previous Fill-the-tail run. At capacity the
# oldest AUTO-SAVED version is evicted; user-named versions never are.

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers import scenario_runner as sr  # noqa: E402
from helpers.calendar_io import empty_calendar  # noqa: E402
from helpers.version_manager import (  # noqa: E402
    MAX_VERSIONS,
    list_versions,
    save_version,
)

TS = datetime(2026, 8, 19, 10, 36)


def _scn(sid: str) -> dict:
    return next(s for s in sr.SCENARIOS if s["id"] == sid)


# ---------------------------------------------------------------------------
# scenario_version_name
# ---------------------------------------------------------------------------

def test_two_pass_f_name_describes_both_passes():
    assert sr.scenario_version_name(_scn("F"), started_at=TS) == (
        "Scenario: Pass 1 - Seed & Fill, Pass 2 - CO Optimized 26-08-19 10:36")


def test_single_pass_fill_name():
    single = dict(_scn("F"), two_pass_co=False)
    assert sr.scenario_version_name(single, started_at=TS) == (
        "Scenario: Max Fill (committed plan fixed) 26-08-19 10:36")


def test_preset_names_keep_their_letter():
    assert sr.scenario_version_name(_scn("A"), started_at=TS) == (
        "Scenario: Minimum changeovers (A) 26-08-19 10:36")
    assert sr.scenario_version_name(_scn("E"), started_at=TS) == (
        "Scenario: Current state + demand (E) 26-08-19 10:36")


def test_custom_name_passes_through_with_id():
    custom = sr.make_custom_scenario("My tuning", "balanced")
    assert sr.scenario_version_name(custom, started_at=TS) == (
        "Scenario: My tuning (X) 26-08-19 10:36")


def test_iso_string_started_at_accepted():
    assert sr.scenario_version_name(
        _scn("F"), started_at="2026-08-19T10:36:00").endswith("26-08-19 10:36")


def test_garbage_started_at_falls_back_to_now():
    name = sr.scenario_version_name(_scn("F"), started_at="not-a-time")
    assert name.startswith("Scenario: Pass 1 - Seed & Fill")
    # timestamp suffix still present (yy-mm-dd hh:mm = 14 chars)
    datetime.strptime(name[-14:], "%y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# save_scenario_version — new version per run + eviction policy
# ---------------------------------------------------------------------------

def _dd(tmp_path: Path) -> Path:
    dd = tmp_path / "data"
    (dd / "versions").mkdir(parents=True)
    return dd


def _result(started_at: str) -> dict:
    return {"ok": True, "calendar": empty_calendar(),
            "scorecard": {"composite": 42.0}, "started_at": started_at}


def test_each_run_saves_a_new_version(tmp_path):
    dd = _dd(tmp_path)
    scn = dict(_scn("F"), intent="test")
    s1 = sr.save_scenario_version(scn, _result("2026-08-19T10:36:00"), dd)
    s2 = sr.save_scenario_version(scn, _result("2026-08-19T11:05:00"), dd)
    assert s1["slug"] != s2["slug"]
    versions = {v["slug"]: v for v in list_versions(dd)}
    assert s1["slug"] in versions and s2["slug"] in versions  # no overwrite
    assert versions[s2["slug"]]["name"] == (
        "Scenario: Pass 1 - Seed & Fill, Pass 2 - CO Optimized 26-08-19 11:05")
    assert versions[s2["slug"]]["source"] == "solver:balanced"
    assert s1["evicted"] == [] and s2["evicted"] == []


def _stamp(dd: Path, slug: str, timestamp: str) -> None:
    meta_p = dd / "versions" / slug / "metadata.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["timestamp"] = timestamp
    meta_p.write_text(json.dumps(meta), encoding="utf-8")


def test_run_at_capacity_evicts_oldest_auto_saved(tmp_path):
    dd = _dd(tmp_path)
    user = save_version("Planner keeper", empty_calendar(), {}, dd,
                        source="digital_twin")
    _stamp(dd, user, "2026-08-01T08:00:00")  # oldest of all — still safe
    autos = []
    for i in range(MAX_VERSIONS - 1):
        s = save_version(f"auto {i}", empty_calendar(), {}, dd,
                         source="solver:balanced")
        _stamp(dd, s, f"2026-08-1{i}T08:00:00")
        autos.append(s)
    saved = sr.save_scenario_version(
        dict(_scn("F"), intent="t"), _result("2026-08-19T10:36:00"), dd)
    assert saved["evicted"] == [autos[0]]     # oldest AUTO went, not the user's
    slugs = {v["slug"] for v in list_versions(dd)}
    assert user in slugs and saved["slug"] in slugs
    assert autos[0] not in slugs


def test_run_with_all_user_named_slots_raises(tmp_path):
    dd = _dd(tmp_path)
    for i in range(MAX_VERSIONS):
        save_version(f"user {i}", empty_calendar(), {}, dd,
                     source="digital_twin")
    with pytest.raises(ValueError, match=f"Maximum of {MAX_VERSIONS}"):
        sr.save_scenario_version(
            dict(_scn("F"), intent="t"), _result("2026-08-19T10:36:00"), dd)
    assert len(list_versions(dd)) == MAX_VERSIONS
