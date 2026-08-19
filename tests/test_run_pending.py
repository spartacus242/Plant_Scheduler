# tests/test_run_pending.py — mid-solve reattach manifest (finding 6).
#
# Navigating away mid-solve kills the Streamlit script but not the solver
# subprocess; run_pending.json in the work dir lets generate.py reattach and
# finish the save instead of silently losing the run.

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers import scenario_runner as sr  # noqa: E402


def _dead_pid() -> int:
    """A pid guaranteed to have exited (fresh, so reuse is implausible)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait(timeout=60)
    return p.pid


def _work(tmp_path: Path, scenario_id: str = "F") -> Path:
    w = tmp_path / "data" / "_scenario_work" / scenario_id
    w.mkdir(parents=True)
    return w


SCN = {"id": "F", "name": "Fill the tail", "objective": "min_changeovers",
       "fill_mode": True}


def test_manifest_roundtrip(tmp_path):
    work = _work(tmp_path)
    sr._write_pending_manifest(work, SCN, time_limit=600, timeout_s=2460.0,
                               pid=4242, cs_notes=["staged 3 gates"])
    m = sr.read_pending_manifest(work)
    assert m is not None
    assert m["scenario"]["id"] == "F"
    assert m["pid"] == 4242
    assert m["time_limit"] == 600
    assert m["cs_notes"] == ["staged 3 gates"]
    assert not sr.pending_is_stale(m)
    sr.clear_pending_manifest(work)
    assert sr.read_pending_manifest(work) is None
    sr.clear_pending_manifest(work)  # idempotent


def test_corrupt_manifest_reads_empty_and_stale(tmp_path):
    work = _work(tmp_path)
    sr.pending_manifest_path(work).write_text("{not json", encoding="utf-8")
    m = sr.read_pending_manifest(work)
    assert m == {}
    assert sr.pending_is_stale(m)


def test_stale_after_a_day(tmp_path):
    work = _work(tmp_path)
    sr._write_pending_manifest(work, SCN, time_limit=600, timeout_s=100.0,
                               pid=1, cs_notes=None)
    m = sr.read_pending_manifest(work)
    assert not sr.pending_is_stale(m)
    assert sr.pending_is_stale(m, now=datetime.now() + timedelta(hours=25))


def test_list_pending_runs_attaches_work_dir(tmp_path):
    wf = _work(tmp_path, "F")
    wa = _work(tmp_path, "A")
    sr._write_pending_manifest(wf, SCN, time_limit=600, timeout_s=1.0,
                               pid=1, cs_notes=None)
    sr._write_pending_manifest(wa, {"id": "A", "name": "Scenario A",
                                    "objective": "min_changeovers"},
                               time_limit=60, timeout_s=1.0, pid=1,
                               cs_notes=None)
    runs = sr.list_pending_runs(tmp_path / "data")
    assert {Path(r["work_dir"]).name for r in runs} == {"A", "F"}
    assert sr.list_pending_runs(tmp_path / "other") == []


def test_pid_alive_detects_dead_and_live():
    assert not sr._pid_alive(_dead_pid())
    assert not sr._pid_alive(None)
    assert not sr._pid_alive("garbage")
    live = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert sr._pid_alive(live.pid)
    finally:
        live.kill()
        live.wait(timeout=60)


def _fake_outputs(work: Path) -> None:
    pd.DataFrame([{
        "line_id": 0, "line_name": "P09", "start_hour": 0.0,
        "end_hour": 10.0, "sku": "111", "order_id": "111-W1",
        "qty_kg": 5400.0,
    }]).to_csv(work / "schedule_phase2.csv", index=False)
    (work / "_solver_stdout.txt").write_text("solver done\n", encoding="utf-8")


def test_resume_finished_run_collects_and_save_clears_manifest(
        tmp_path, monkeypatch):
    """Solver finished while the page was away: resume must rebuild the
    result from the work dir, the save must persist it AND retire the
    manifest so the next load does not reattach again."""
    dd = tmp_path / "data"
    work = _work(tmp_path)
    _fake_outputs(work)
    sr._write_pending_manifest(work, SCN, time_limit=600, timeout_s=2460.0,
                               pid=_dead_pid(), cs_notes=["gates staged"])
    monkeypatch.setattr(sr, "score_calendar",
                        lambda cal, **kw: {"composite": 42.0})
    scenario = dict(SCN, intent="test")
    result = sr.resume_scenario(scenario, dd)
    assert result["ok"] is True
    assert len(result["calendar"]) == 1
    assert "gates staged" in result["log"]
    # finished-but-unsaved: manifest survives until the save attempt
    assert sr.read_pending_manifest(work) is not None
    slug = sr.save_scenario_version(scenario, result, dd)
    assert sr.read_pending_manifest(work) is None
    from helpers.version_manager import list_versions
    assert any(v["slug"] == slug for v in list_versions(dd))


def test_resume_failed_run_clears_manifest(tmp_path):
    """No outputs on disk (crash / kill): not ok, and the manifest is
    retired so the page does not reattach forever."""
    dd = tmp_path / "data"
    work = _work(tmp_path)
    sr._write_pending_manifest(work, SCN, time_limit=600, timeout_s=2460.0,
                               pid=_dead_pid(), cs_notes=None)
    result = sr.resume_scenario(dict(SCN), dd)
    assert result["ok"] is False
    assert result["calendar"] is None
    assert sr.read_pending_manifest(work) is None
