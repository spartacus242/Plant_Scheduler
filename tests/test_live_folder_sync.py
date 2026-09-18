# tests/test_live_folder_sync.py — folder mode of the live-data bridge (2026-09-14).
#
# The planner's PC on the work network reads the ERP's drop folder directly:
# scripts/fs-live-pull.py copies from "source_dirs" instead of a GitHub clone.
# These tests pin what that mode promises: read-only towards the folder, one
# busy file costs only itself, a file still being written waits a pass, the
# newest copy wins across folders, demand_plan.csv is re-derived, the manprg
# as-of is the ERP's write time, and every pass leaves a heartbeat that the
# app's health row and the calendar's sync-before-rebuild read.

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helpers import data_health as dh
from helpers import live_sync

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
NL = chr(10)


def _pull():
    spec = importlib.util.spec_from_file_location("fs_live_pull", SCRIPTS / "fs-live-pull.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, text: str, age_s: float = 600.0) -> Path:
    """Create a source file with a controlled mtime (default: settled 10 min ago)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


FILES = ["manprg.txt", "manprg2.txt", "cip_info.csv", "demand_plan_summary.csv",
         "order_npa.csv", "NPA Open POs*.xlsx -> open_pos.xlsx"]


def _conf(src_dirs, ref_dir, **extra) -> dict:
    conf = {"files": FILES, "data_reference_dir": str(ref_dir),
            "source_dirs": [str(d) for d in src_dirs], "settle_seconds": 60}
    conf.update(extra)
    return conf


def _snapshot(folder: Path) -> list[tuple[str, int, bytes]]:
    return sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in folder.iterdir())


# ---------------------------------------------------------------------------
# conf parsing
# ---------------------------------------------------------------------------

def test_source_dirs_accepts_string_list_or_nothing():
    pull = _pull()
    assert pull.source_dirs({}) == []
    assert pull.source_dirs({"source_dirs": []}) == []
    assert pull.source_dirs({"source_dir": " X:/erp "}) == [Path("X:/erp")]
    assert pull.source_dirs({"source_dirs": ["a", "", None, 3, " b "]}) == [Path("a"), Path("b")]
    assert pull.source_dirs({"source_dirs": "one"}) == [Path("one")]
    assert pull.source_dirs({"source_dirs": 42}) == []


def test_shipped_conf_stays_in_github_mode():
    """The tracked conf documents the folder-mode keys but leaves them empty:
    the dev PC keeps pulling from GitHub until a local override says otherwise."""
    conf = json.loads((SCRIPTS / "fs-live-data.conf.json").read_text(encoding="utf-8"))
    assert conf["source_dirs"] == []
    assert conf["settle_seconds"] == 60


# ---------------------------------------------------------------------------
# folder mode, one pass
# ---------------------------------------------------------------------------

def test_folder_mode_copies_derives_stamps_and_reports(tmp_path):
    pull = _pull()
    src, ref = tmp_path / "erp_out", tmp_path / "ref"
    _write(src / "manprg.txt", "MO 1", age_s=3600)
    _write(src / "manprg2.txt", "MO 2", age_s=1800)           # newest manprg -> the as-of
    _write(src / "cip_info.csv", "line,last_cip")
    _write(src / "demand_plan_summary.csv", "Week,Product,kg_tons" + NL + "40,280480,12.5")
    _write(src / "NPA Open POs -9.14.xlsx", "old", age_s=7200)
    _write(src / "NPA Open POs -9.15.xlsx", "new", age_s=900)
    before = _snapshot(src)

    res = pull.pull_once(_conf([src], ref))

    assert res.mode == "folder" and res.ok, res.problems
    assert res.sources == [str(src)]
    assert (ref / "manprg.txt").read_text() == "MO 1"
    assert (ref / "open_pos.xlsx").read_text() == "new"        # dated glob -> fixed name, newest wins
    assert not (ref / "order_npa.csv").exists()                 # absent at the source: nothing invented
    assert (ref / "demand_plan.csv").is_file()                  # derived from the summary, never copied
    meta = json.loads((ref / "demand_plan.source.json").read_text(encoding="utf-8"))
    assert meta["source"] == "demand_plan_summary.csv"
    assert meta["anchor_iso_week"] == 40 and meta["weeks"] == [0]   # week_index 0 = ISO week 40
    assert "demand_plan.csv (derived)" in res.updated
    # the copy keeps the source mtime: the health page ages every feed file by it
    assert abs((ref / "cip_info.csv").stat().st_mtime - (src / "cip_info.csv").stat().st_mtime) < 1
    # manprg as-of = the ERP's write time of the newest manprg file, local wall clock
    stamp = json.loads((ref / pull.ASOF_STAMP_NAME).read_text(encoding="utf-8"))
    expect = time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.localtime((src / "manprg2.txt").stat().st_mtime))
    assert stamp["as_of"] == expect and stamp["pull_head"] is None
    # the heartbeat
    state = json.loads((ref / pull.SYNC_STATE_NAME).read_text(encoding="utf-8"))
    assert state["mode"] == "folder" and state["ok"] is True
    assert state["sources"] == [str(src)] and state["problems"] == []
    assert "manprg.txt" in state["updated"] and "open_pos.xlsx" in state["updated"]
    assert datetime.fromisoformat(state["finished"]) > datetime.now() - timedelta(minutes=1)
    # the source folder was only READ: same names, same bytes, same mtimes, nothing added
    assert _snapshot(src) == before
    assert not list(ref.glob("*.tmp"))

    # second pass: nothing new, heartbeat refreshed, source still untouched
    res2 = pull.pull_once(_conf([src], ref))
    assert res2.ok and res2.updated == [] and res2.skipped == []
    assert _snapshot(src) == before


def test_unreachable_folder_is_a_problem_not_a_crash(tmp_path):
    pull = _pull()
    good, ref, missing = tmp_path / "good", tmp_path / "ref", tmp_path / "nope"
    _write(good / "manprg.txt", "MO 1")
    res = pull.pull_once(_conf([missing, good], ref))
    assert (ref / "manprg.txt").is_file()                      # the reachable folder still synced
    assert not res.ok
    assert any("not reachable" in p and "nope" in p for p in res.problems)
    state = json.loads((ref / pull.SYNC_STATE_NAME).read_text(encoding="utf-8"))
    assert state["ok"] is False and state["problems"] == res.problems


def test_fresh_file_waits_a_pass_and_a_busy_file_costs_only_itself(tmp_path, monkeypatch):
    pull = _pull()
    src, ref = tmp_path / "src", tmp_path / "ref"
    _write(src / "manprg.txt", "MO 1")
    _write(src / "cip_info.csv", "being written", age_s=0)     # mtime = now: may still be open
    _write(src / "order_npa.csv", "PO lines")
    real_read = Path.read_bytes

    def busy(self):
        if self.name == "order_npa.csv":
            raise PermissionError(32, "The process cannot access the file because it is "
                                      "being used by another process")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", busy)
    res = pull.pull_once(_conf([src], ref))
    assert (ref / "manprg.txt").is_file()                      # the others still copied
    assert not (ref / "cip_info.csv").exists()
    assert any("cip_info.csv" in s and "settling" in s for s in res.skipped)
    assert not (ref / "order_npa.csv").exists()
    assert any(p.startswith("order_npa.csv:") for p in res.problems)
    assert not res.ok                                          # a busy file IS reported

    # settle_seconds 0 switches the wait off; the file lands on the next pass
    monkeypatch.setattr(Path, "read_bytes", real_read)
    res2 = pull.pull_once(_conf([src], ref, settle_seconds=0))
    assert (ref / "cip_info.csv").read_text() == "being written"
    assert res2.ok and "order_npa.csv" in res2.updated


def test_file_that_changes_while_read_waits_a_pass(tmp_path, monkeypatch):
    pull = _pull()
    src, ref = tmp_path / "src", tmp_path / "ref"
    _write(src / "manprg.txt", "MO 1")
    target = _write(src / "manprg2.txt", "MO 2")
    real_read = Path.read_bytes

    def sneaky(self):
        data = real_read(self)
        if self == target:                                     # the ERP appends mid-read
            with open(self, "ab") as fh:
                fh.write(b" and more")
        return data

    monkeypatch.setattr(Path, "read_bytes", sneaky)
    res = pull.pull_once(_conf([src], ref))
    assert (ref / "manprg.txt").is_file()
    assert not (ref / "manprg2.txt").exists()
    assert any("manprg2.txt" in s and "changed while reading" in s for s in res.skipped)
    assert res.ok                                              # a wait is not a failure


def test_two_folders_newest_copy_wins_in_conf_order(tmp_path):
    pull = _pull()
    erp, planning = tmp_path / "erp", tmp_path / "planning"
    _write(erp / "manprg.txt", "from erp", age_s=600)
    _write(planning / "manprg.txt", "from planning", age_s=6000)   # older copy loses
    _write(planning / "cip_info.csv", "cip", age_s=600)
    _write(erp / "demand_plan_summary.csv", "Week,Product,kg_tons" + NL + "41,280480,1", age_s=600)
    pairs = pull.resolve_entries_multi(FILES, [planning, erp, tmp_path / "absent"])
    assert [d for _, d in pairs] == ["manprg.txt", "cip_info.csv", "demand_plan_summary.csv"]
    assert pairs[0][0].read_text() == "from erp"
    assert pairs[1][0].parent == planning


def test_script_exit_code_tells_the_scheduler_and_installer(tmp_path):
    """--once exits 1 when the pass reported problems (a share that is not
    reachable), 0 when it synced cleanly — the installer's first sync and the
    Task Scheduler 'Last Run Result' both read it."""
    good, ref = tmp_path / "good", tmp_path / "ref"
    _write(good / "manprg.txt", "MO 1")
    base = [sys.executable, str(SCRIPTS / "fs-live-pull.py"), "--once", "--reference-dir", str(ref)]
    ok = subprocess.run(base + ["--source-dir", str(good)], capture_output=True, text=True,
                        cwd=str(ROOT), timeout=120)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "synced from" in ok.stdout and "manprg.txt" in ok.stdout
    bad = subprocess.run(base + ["--source-dir", str(tmp_path / "nope")], capture_output=True,
                         text=True, cwd=str(ROOT), timeout=120)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "PROBLEMS" in bad.stdout and "not reachable" in bad.stdout
    state = json.loads((ref / "live_sync.json").read_text(encoding="utf-8"))
    assert state["ok"] is False


# ---------------------------------------------------------------------------
# the fs_data drop root (2026-09-17)
# ---------------------------------------------------------------------------
# The plant drop is ONE synced folder, fs_data, holding fs_vif and fs_manual.
# -FeedDir "<path>\fs_data" used to sync nothing while the heartbeat said ok /
# nothing new. Now a listed folder holding those subfolders stands for them,
# and a reachable folder with none of the plant files is a problem.

def _azap_fixtures():
    """The AZAP workbook fakes live in tests/test_azap_demand.py (_workbook,
    EXPECTED_CSV); load that module by path so the fake is built ONE way."""
    spec = importlib.util.spec_from_file_location("azap_test_fixtures",
                                                  ROOT / "tests" / "test_azap_demand.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drop(tmp_path: Path, *, workbook: bool = True):
    """A drop root: fs_vif with two manprg files, fs_manual with cip_info.csv
    (+ the AZAP workbook), and a decoy workbook in the ROOT that must be ignored."""
    root = tmp_path / "fs_data"
    _write(root / "fs_vif" / "manprg.txt", "MO 1", age_s=3600)
    _write(root / "fs_vif" / "manprg2.txt", "MO 2", age_s=1800)
    _write(root / "fs_manual" / "cip_info.csv", "line,last_cip")
    az = _azap_fixtures()
    if workbook:
        az._workbook(root / "fs_manual" / "New Export AZAP 091126.xlsx", age_s=3600)
    az._workbook(root / "New Export AZAP 091826.xlsx", age_s=3600)     # in the root: ignored
    return root, az


def test_expand_drop_roots_rules(tmp_path):
    pull = _pull()
    root = tmp_path / "fs_data"
    vif, manual = root / "fs_vif", root / "fs_manual"
    flat = tmp_path / "erp_out"
    for d in (vif, manual, flat):
        d.mkdir(parents=True)
    # a root holding the layout is replaced by its subfolders, fs_vif first, and is NOT kept
    assert pull.expand_drop_roots([root]) == [vif, manual]
    assert pull.expand_drop_roots([str(root)]) == [vif, manual]
    # an explicit subfolder list is unchanged; a root next to its own fs_vif does not double
    assert pull.expand_drop_roots([vif, manual]) == [vif, manual]
    assert pull.expand_drop_roots([root, vif]) == [vif, manual]
    assert pull.expand_drop_roots([manual, root]) == [manual, vif]
    # a flat folder and a missing one come back as they are, in order
    assert pull.expand_drop_roots([flat, root, tmp_path / "nope"]) == [flat, vif, manual, tmp_path / "nope"]
    assert pull.expand_drop_roots([]) == []
    # a root with only fs_manual expands to that one folder
    only = tmp_path / "half"
    (only / "fs_manual").mkdir(parents=True)
    assert pull.expand_drop_roots([only]) == [only / "fs_manual"]
    # the conf reader stays pure: no filesystem look-up, the root comes back as written
    assert pull.source_dirs({"source_dirs": [str(root)]}) == [root]
    assert pull.DROP_SUBFOLDERS == ("fs_vif", "fs_manual")


def test_root_conf_syncs_the_subfolders_and_never_writes_the_root(tmp_path):
    """(a) a conf listing only <root>: both subfolders' files land in
    data/reference, the summary is rebuilt in fs_manual (never in the root,
    whose own workbook is ignored), the result and the heartbeat name the
    subfolders, and the root and fs_vif are left exactly as they were."""
    pull = _pull()
    root, az = _drop(tmp_path)
    ref = tmp_path / "ref"
    vif, manual = root / "fs_vif", root / "fs_manual"

    def root_snapshot():   # the root's own entries: its files' bytes + mtimes, its folders' names
        return sorted((p.name, p.is_dir(), p.stat().st_mtime_ns if p.is_file() else None,
                       p.read_bytes() if p.is_file() else b"") for p in root.iterdir())

    root_before, vif_before = root_snapshot(), _snapshot(vif)

    res = pull.pull_once(_conf([root], ref))

    assert res.mode == "folder" and res.ok, res.problems
    assert res.sources == [str(vif), str(manual)]
    assert (ref / "manprg.txt").read_text() == "MO 1" and (ref / "manprg2.txt").read_text() == "MO 2"
    assert (ref / "cip_info.csv").read_text() == "line,last_cip"
    assert (ref / "demand_plan_summary.csv").read_bytes() == az.EXPECTED_CSV
    assert (manual / "demand_plan_summary.csv").read_bytes() == az.EXPECTED_CSV
    assert (ref / "demand_plan.csv").is_file()
    assert "demand_plan_summary.csv (built from New Export AZAP 091126.xlsx)" in res.updated
    state = json.loads((ref / pull.SYNC_STATE_NAME).read_text(encoding="utf-8"))
    assert state["sources"] == [str(vif), str(manual)] and state["ok"] is True
    # the manprg as-of is the ERP's write time of the newest manprg in fs_vif
    stamp = json.loads((ref / pull.ASOF_STAMP_NAME).read_text(encoding="utf-8"))
    assert stamp["as_of"] == time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.localtime((vif / "manprg2.txt").stat().st_mtime))
    # nothing written into the root (its decoy workbook got no summary) nor into fs_vif
    assert root_snapshot() == root_before and _snapshot(vif) == vif_before
    assert not (root / "demand_plan_summary.csv").exists()
    assert sorted(p.name for p in root.iterdir()) == ["New Export AZAP 091826.xlsx", "fs_manual", "fs_vif"]

    res2 = pull.pull_once(_conf([root], ref))
    assert res2.ok and res2.updated == [] and res2.skipped == []


def test_explicit_subfolder_list_gives_the_same_result_without_duplicates(tmp_path):
    """(b) [root/fs_vif, root/fs_manual] and [root, root/fs_vif] both read as
    the two subfolders, once each — the pre-2026-09-17 spelling keeps working."""
    pull = _pull()
    root, az = _drop(tmp_path)
    vif, manual = root / "fs_vif", root / "fs_manual"
    for i, listed in enumerate(([vif, manual], [root, vif], [vif, root, manual])):
        ref = tmp_path / f"ref{i}"
        res = pull.pull_once(_conf(listed, ref))
        assert res.ok, res.problems
        assert res.sources == [str(vif), str(manual)]
        assert sorted(p.name for p in ref.iterdir() if not p.name.endswith(".json")) == [
            "cip_info.csv", "demand_plan.csv", "demand_plan_summary.csv", "manprg.txt", "manprg2.txt"]
        assert (ref / "demand_plan_summary.csv").read_bytes() == az.EXPECTED_CSV
        state = json.loads((ref / pull.SYNC_STATE_NAME).read_text(encoding="utf-8"))
        assert state["sources"] == [str(vif), str(manual)]


def test_same_file_in_both_subfolders_newest_wins_and_fs_vif_breaks_a_tie(tmp_path):
    """(c) a file present in fs_vif AND fs_manual is taken from wherever it
    is newest; with equal modified times fs_vif (listed first) wins."""
    pull = _pull()
    root, ref = tmp_path / "fs_data", tmp_path / "ref"
    _write(root / "fs_vif" / "manprg.txt", "vif older", age_s=6000)
    _write(root / "fs_manual" / "manprg.txt", "manual newer", age_s=600)
    tie = time.time() - 3600
    a = _write(root / "fs_vif" / "cip_info.csv", "cip from vif")
    b = _write(root / "fs_manual" / "cip_info.csv", "cip from manual")
    os.utime(a, (tie, tie))
    os.utime(b, (tie, tie))
    res = pull.pull_once(_conf([root], ref))
    assert res.ok, res.problems
    assert (ref / "manprg.txt").read_text() == "manual newer"
    assert (ref / "cip_info.csv").read_text() == "cip from vif"


def test_root_with_only_fs_manual_expands_to_that_folder(tmp_path):
    """(d) half a layout is still the layout: the root stands for fs_manual alone."""
    pull = _pull()
    root, ref = tmp_path / "fs_data", tmp_path / "ref"
    _write(root / "fs_manual" / "cip_info.csv", "cip")
    res = pull.pull_once(_conf([root], ref))
    assert res.ok, res.problems
    assert res.sources == [str(root / "fs_manual")]
    assert (ref / "cip_info.csv").read_text() == "cip"
    assert not (root / "demand_plan_summary.csv").exists()


def test_reachable_folder_without_plant_files_is_a_problem(tmp_path):
    """(e) a folder that IS reachable but holds none of the listed files —
    the silent-green failure — is a problem naming it: the pass is not ok,
    the script exits 1, and Home's row reads STALE with that text (never
    blocking). A folder whose only file is the summary the pass itself
    rebuilt, or that holds the AZAP workbook (settling or not), counts as
    holding plant files."""
    pull = _pull()
    good, empty, ref = tmp_path / "good", tmp_path / "wrong_folder", tmp_path / "ref"
    _write(good / "manprg.txt", "MO 1")
    empty.mkdir()
    _write(empty / "unrelated.txt", "not on the conf list")
    res = pull.pull_once(_conf([empty, good], ref))
    assert (ref / "manprg.txt").is_file()                      # the good folder still synced
    assert not res.ok
    expect = (f"no plant files found in {empty} — expected the fs_data layout "
              "(fs_vif, fs_manual) or the files on the conf list")
    assert res.problems == [expect]
    assert pull.empty_source_folders(FILES, [empty, good, tmp_path / "nope"]) == [empty]

    # Home's Live data sync row: STALE with the text, warning severity only
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    _heartbeat(dd / "reference", ok=False, problems=res.problems)
    row = dh._live_sync_status(dd, dict(dh.DEFAULT_CADENCE_H))
    assert row.state == dh.STALE and "no plant files found" in row.detail and row.severity == 1

    # the subprocess pattern: exit 1, PROBLEMS in the output, heartbeat not ok
    base = [sys.executable, str(SCRIPTS / "fs-live-pull.py"), "--once", "--reference-dir", str(ref)]
    bad = subprocess.run(base + ["--source-dir", str(empty)], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=120)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "PROBLEMS" in bad.stdout and "no plant files found" in bad.stdout and "wrong_folder" in bad.stdout
    state = json.loads((ref / "live_sync.json").read_text(encoding="utf-8"))
    assert state["ok"] is False and state["problems"] == [expect]
    # the root spelling through --source-dir expands too, and a good root is clean
    root, _ = _drop(tmp_path)
    ok = subprocess.run(base + ["--source-dir", str(root)], capture_output=True, text=True,
                        cwd=str(ROOT), timeout=120)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert str(root / "fs_vif") in ok.stdout and str(root / "fs_manual") in ok.stdout

    # fs_manual holding ONLY the workbook: the rebuilt csv is its contribution
    az = _azap_fixtures()
    lone = tmp_path / "lone"
    _write(lone / "fs_vif" / "manprg.txt", "MO 1")
    az._workbook(lone / "fs_manual" / "New Export AZAP 091126.xlsx", age_s=3600)
    res = pull.pull_once(_conf([lone], tmp_path / "ref_lone"))
    assert res.ok, res.problems
    assert (lone / "fs_manual" / "demand_plan_summary.csv").is_file()
    # ... and while that workbook is still settling (no csv yet) it is not "no plant files" either
    fresh = tmp_path / "fresh"
    _write(fresh / "fs_vif" / "manprg.txt", "MO 1")
    az._workbook(fresh / "fs_manual" / "New Export AZAP 091126.xlsx", age_s=0)
    res = pull.pull_once(_conf([fresh], tmp_path / "ref_fresh"))
    assert res.ok, res.problems
    assert any("settling" in s for s in res.skipped)
    assert not (fresh / "fs_manual" / "demand_plan_summary.csv").exists()


# ---------------------------------------------------------------------------
# helpers/live_sync — the app's side
# ---------------------------------------------------------------------------

def _fake_root(tmp_path: Path, conf: dict | None, stub: str | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    if stub is not None:
        (root / "scripts" / "fs-live-pull.py").write_text(stub, encoding="utf-8")
    if conf is not None:
        (root / "scripts" / "fs-live-data.conf.json").write_text(json.dumps(conf), encoding="utf-8")
    return root


def test_is_configured_needs_script_and_a_source(tmp_path):
    assert live_sync.configured_sources({"source_dir": " a "}) == ["a"]
    assert live_sync.configured_sources({"source_dirs": ["a", 1, "", "b"]}) == ["a", "b"]
    assert live_sync.configured_sources({"source_dirs": "x"}) == ["x"]
    assert live_sync.configured_sources({}) == []
    # no script at all
    assert live_sync.is_configured(_fake_root(tmp_path / "a", {"source_dirs": ["x"]})) is False
    # script + folder mode
    assert live_sync.is_configured(_fake_root(tmp_path / "b", {"source_dirs": ["x"]}, stub="")) is True
    # script + github mode whose clone does not exist here
    assert live_sync.is_configured(
        _fake_root(tmp_path / "c", {"clone_dir_personal": str(tmp_path / "no_clone")}, stub="")) is False
    # ... and one whose clone does
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    assert live_sync.is_configured(
        _fake_root(tmp_path / "d", {"clone_dir_personal": str(clone)}, stub="")) is True
    # the git-ignored local override wins over the tracked conf (BOM tolerated)
    root = _fake_root(tmp_path / "e", {"clone_dir_personal": str(tmp_path / "no_clone")}, stub="")
    (root / "scripts" / "fs-live-data.local.json").write_text(
        json.dumps({"source_dirs": ["//srv/share/out"]}), encoding="utf-8-sig")
    assert live_sync.is_configured(root) is True
    assert live_sync.load_bridge_conf(root)["source_dirs"] == ["//srv/share/out"]


def test_read_state(tmp_path):
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    assert live_sync.read_state(dd) is None
    (dd / "reference" / "live_sync.json").write_text("{not json", encoding="utf-8")
    assert live_sync.read_state(dd) is None
    (dd / "reference" / "live_sync.json").write_text(json.dumps({"mode": "folder"}), encoding="utf-8")
    assert live_sync.read_state(dd) == {"mode": "folder"}


def _stub_writing_heartbeat(ref: Path, updated: list[str], problems: list[str], rc: int) -> str:
    body = {"mode": "folder", "sources": ["//srv/share/out"], "ok": not problems,
            "updated": updated, "skipped": [], "problems": problems, "head": None}
    return NL.join([
        "import json, pathlib, sys, time",
        f"ref = pathlib.Path({str(ref)!r})",
        "ref.mkdir(parents=True, exist_ok=True)",
        f"body = {body!r}",
        "body['finished'] = time.strftime('%Y-%m-%dT%H:%M:%S')",
        "(ref / 'live_sync.json').write_text(json.dumps(body), encoding='utf-8')",
        "print('synced from //srv/share/out: updated ' + ', '.join(body['updated']))",
        f"sys.exit({rc})",
    ])


def test_run_sync_reads_the_heartbeat_the_pass_wrote(tmp_path):
    dd = tmp_path / "data"
    ref = dd / "reference"
    ref.mkdir(parents=True)
    conf = {"source_dirs": ["//srv/share/out"], "data_reference_dir": str(ref)}
    stub = _stub_writing_heartbeat(
        ref, ["manprg.txt", "cip_info.csv", "demand_plan.csv (derived)", "manprg.asof.json (stamped)"], [], 0)
    root = _fake_root(tmp_path, conf, stub)
    out = live_sync.run_sync(dd, root=root, python=sys.executable, timeout_s=60)
    assert out.ran and out.ok, out
    assert out.updated == ["manprg.txt", "cip_info.csv"]      # notes are not files
    assert out.summary == "Live data synced: updated manprg.txt, cip_info.csv."
    assert out.problems == []

    # a pass that reported a problem: not ok, the problem in the summary
    (root / "scripts" / "fs-live-pull.py").write_text(
        _stub_writing_heartbeat(ref, [], ["source folder not reachable: //srv/share/out"], 1),
        encoding="utf-8")
    out = live_sync.run_sync(dd, root=root, python=sys.executable, timeout_s=60)
    assert out.ran and not out.ok and out.updated == []
    assert "not reachable" in out.summary

    # nothing new
    (root / "scripts" / "fs-live-pull.py").write_text(_stub_writing_heartbeat(ref, [], [], 0),
                                                     encoding="utf-8")
    out = live_sync.run_sync(dd, root=root, python=sys.executable, timeout_s=60)
    assert out.ok and out.updated == [] and out.summary == "Live data synced: nothing new."


def test_run_sync_never_raises(tmp_path):
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    conf = {"source_dirs": ["//srv/share/out"]}
    # crash without a heartbeat: the last output line becomes the problem
    root = _fake_root(tmp_path / "crash", conf,
                      "import sys" + NL + "print('ERROR: boom')" + NL + "sys.exit(1)")
    out = live_sync.run_sync(dd, root=root, python=sys.executable, timeout_s=60)
    assert out.ran and not out.ok and out.problems == ["ERROR: boom"]
    # hang: bounded by the timeout
    root = _fake_root(tmp_path / "hang", conf, "import time" + NL + "time.sleep(30)")
    t0 = time.monotonic()
    out = live_sync.run_sync(dd, root=root, python=sys.executable, timeout_s=1.0)
    assert out.ran and not out.ok and "timed out" in out.summary
    assert time.monotonic() - t0 < 25
    # not configured here: nothing runs, nothing fails
    out = live_sync.run_sync(dd, root=_fake_root(tmp_path / "none", None), python=sys.executable)
    assert out.ran is False and out.ok is True


# ---------------------------------------------------------------------------
# helpers/data_health — the "Live data sync" row
# ---------------------------------------------------------------------------

def _heartbeat(ref: Path, **kw) -> None:
    body = {"mode": "folder", "sources": ["//srv/share/out"],
            "finished": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "ok": True,
            "updated": [], "skipped": [], "problems": [], "head": None}
    body.update(kw)
    (ref / "live_sync.json").write_text(json.dumps(body), encoding="utf-8")


def test_health_row_absent_ok_stale_problems_unreadable(tmp_path):
    dd = tmp_path / "data"
    ref = dd / "reference"
    ref.mkdir(parents=True)
    cad = dict(dh.DEFAULT_CADENCE_H)
    assert cad["live_sync"] == 1.0

    row = dh._live_sync_status(dd, cad)                        # never ran here: not a finding
    assert row.key == "live_sync" and row.state == dh.NOT_APPLICABLE and row.severity == 0
    assert row.source == "live_feed"

    _heartbeat(ref, updated=["manprg.txt", "cip_info.csv"])
    row = dh._live_sync_status(dd, cad)
    assert row.state == dh.OK
    assert "folder //srv/share/out" in row.detail and "manprg.txt, cip_info.csv" in row.detail
    assert row.age_h is not None and row.age_h < 0.1 and row.cadence_h == 1.0

    _heartbeat(ref, mode="github", sources=["https://github.com/x/y.git", "C:/clone"])
    row = dh._live_sync_status(dd, cad)
    assert row.state == dh.OK and "GitHub bridge" in row.detail and "nothing new" in row.detail

    old = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    _heartbeat(ref, finished=old)
    row = dh._live_sync_status(dd, cad)
    assert row.state == dh.STALE and "Flowstate Live Data Pull" in row.detail
    assert row.severity == 1                                   # warn, never blocking

    _heartbeat(ref, ok=False, problems=["source folder not reachable: //srv/share/out"])
    row = dh._live_sync_status(dd, cad)
    assert row.state == dh.STALE and "not reachable" in row.detail and row.actions

    (ref / "live_sync.json").write_text("{not json", encoding="utf-8")
    row = dh._live_sync_status(dd, cad)
    assert row.state == dh.STALE and "unreadable" in row.detail

    # the row leads the live-feed block and a [health] cadence override is honoured
    _heartbeat(ref, finished=old)
    rows = dh._live_feed_statuses(dd, {"health": {"cadence_h": {"live_sync": 6.0}}})
    assert rows[0].key == "live_sync" and rows[0].state == dh.OK
    assert [r.key for r in rows].count("live_sync") == 1


def test_app_source_folders_expand_the_fs_data_root(tmp_path):
    """helpers/live_sync.source_folders reads the conf like the sync does
    (2026-09-17): a configured fs_data root stands for its fs_vif +
    fs_manual, an explicit list or a flat folder is unchanged, GitHub mode
    gives []. The Data Files page looks for the AZAP workbook in these
    folders, so a root must not hide fs_manual from it."""
    from helpers import live_sync
    root = tmp_path / "fs_data"
    vif, manual = root / "fs_vif", root / "fs_manual"
    vif.mkdir(parents=True)
    manual.mkdir()
    flat = tmp_path / "flat"
    flat.mkdir()
    assert live_sync.source_folders({"source_dirs": [str(root)]}) == [str(vif), str(manual)]
    assert live_sync.source_folders({"source_dirs": [str(vif), str(manual)]}) == [str(vif), str(manual)]
    assert live_sync.source_folders({"source_dir": str(flat)}) == [str(flat)]
    assert live_sync.source_folders({"source_dirs": []}) == []
    # is_configured keeps judging the RAW conf: a root alone is "a source"
    assert live_sync.configured_sources({"source_dirs": [str(root)]}) == [str(root)]
