# tests/test_drop_mirror.py — the drop mirror of the live-data pull (2026-09-18).
#
# A planner's PC reads the SharePoint library OneDrive syncs into
# fs_data\fs_vif + fs_data\fs_manual. The dev PC has no SharePoint, it has the
# private GitHub repo the work PC pushes the same files to; to run the dev PC
# EXACTLY like a planner's PC, scripts/fs-live-pull.py can first drop the
# clone's files into a local fs_data by the conf's "drop_layout" and then run
# folder mode from that root. These tests pin what makes that an honest
# stand-in: routing by the shipped layout, GitHub's copy lands only when it is
# newer than the file already there (commit time vs file time — the csv the
# pass rebuilds from the AZAP workbook, or a newer hand-dropped export, is
# never clobbered), the landed file's time is the commit time, nothing is ever
# deleted, an unrouted file is a problem, and the heartbeat + health row name
# the mirror while the sources stay the folders.

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

from helpers import data_health as dh

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _pull():
    spec = importlib.util.spec_from_file_location("fs_live_pull_mirror", SCRIPTS / "fs-live-pull.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, text: str, age_s: float = 600.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


FILES = ["manprg.txt", "manprg2.txt", "cip_info.csv", "demand_plan_summary.csv",
         "order_npa.csv", "NPA Open POs*.xlsx -> open_pos.xlsx"]
LAYOUT = {"fs_vif": ["manprg.txt", "manprg2.txt", "order_npa.csv"],
          "fs_manual": ["cip_info.csv", "demand_plan_summary.csv", "open_pos.xlsx"]}


# ---------------------------------------------------------------------------
# the shipped routing table
# ---------------------------------------------------------------------------

def test_shipped_layout_routes_every_bridge_file_once():
    """Every name on the conf's files list lands in exactly one drop
    subfolder, no routing entry is stale, and the split follows the drop's
    own rule: the ERP's exports in fs_vif, people-maintained files in
    fs_manual (docs/vif_exports.md Layout)."""
    pull = _pull()
    conf = json.loads((SCRIPTS / "fs-live-data.conf.json").read_text(encoding="utf-8"))
    layout = conf["drop_layout"]
    assert set(layout) == set(pull.DROP_SUBFOLDERS) == {"fs_vif", "fs_manual"}
    routed = [n for names in layout.values() for n in names]
    assert len(routed) == len(set(routed)), "a name in both folders"
    dests = [pull._entry_dest(e) for e in conf["files"]]
    for d in dests:
        assert pull.layout_folder(d, layout) is not None, f"{d} is not routed"
    assert set(routed) <= set(dests), "routing entry for a file not on the list"
    for erp in ("manprg.txt", "ediact.csv", "jestkexp.csv", "PKG-REC.csv", "order_npa.csv"):
        assert pull.layout_folder(erp, layout) == "fs_vif", erp
    for manual in ("cip_info.csv", "demand_plan_summary.csv",
                   "Shipping Receiving Schedule NPA - 2024.xlsm", "open_pos.xlsx"):
        assert pull.layout_folder(manual, layout) == "fs_manual", manual
    assert pull.layout_folder("nope.csv", layout) is None
    assert pull.layout_folder("manprg.txt", "not a dict") is None


# ---------------------------------------------------------------------------
# mirror_clone_into_drop
# ---------------------------------------------------------------------------

def test_mirror_routes_by_layout_keeps_names_and_stamps_the_commit_time(tmp_path):
    pull = _pull()
    clone, drop = tmp_path / "clone", tmp_path / "fs_data"
    _write(clone / "manprg.txt", "m1", age_s=3600)
    _write(clone / "cip_info.csv", "c1", age_s=3600)
    _write(clone / "NPA Open POs -8.24.xlsx", "x", age_s=3600)
    _write(clone / "unrelated.txt", "u")
    t_manprg = time.time() - 7200
    asof = {"manprg.txt": t_manprg}.get          # None for the others
    mirrored, kept, problems = pull.mirror_clone_into_drop(clone, drop, FILES, LAYOUT, asof=asof)
    assert sorted(mirrored) == ["fs_manual/NPA Open POs -8.24.xlsx",
                                "fs_manual/cip_info.csv", "fs_vif/manprg.txt"]
    assert kept == [] and problems == []
    assert (drop / "fs_vif" / "manprg.txt").read_text(encoding="utf-8") == "m1"
    # the landed file carries the commit time (OneDrive keeps the SharePoint
    # modified time the same way; folder mode's manprg as-of reads it)
    assert abs((drop / "fs_vif" / "manprg.txt").stat().st_mtime - t_manprg) < 1
    # no commit time known: the clone file's own time is kept
    assert abs((drop / "fs_manual" / "cip_info.csv").stat().st_mtime
               - (clone / "cip_info.csv").stat().st_mtime) < 1
    # only conf files travel; the dated workbook keeps its own name for the
    # folder-mode glob to rename, exactly as on a planner PC
    assert not (drop / "fs_vif" / "unrelated.txt").exists()
    assert not (drop / "fs_manual" / "unrelated.txt").exists()
    assert (drop / "fs_manual" / "NPA Open POs -8.24.xlsx").exists()
    # same bytes on the next pass: nothing to do, nothing reported
    assert pull.mirror_clone_into_drop(clone, drop, FILES, LAYOUT, asof=asof) == ([], [], [])


def test_mirror_never_clobbers_a_newer_drop_copy_and_never_deletes(tmp_path):
    """The csv the pass rebuilt from the AZAP workbook (or an export the ERP
    wrote after the last push) is newer than GitHub's: it stays, and the pass
    says so. A file only the drop has is never removed."""
    pull = _pull()
    clone, drop = tmp_path / "clone", tmp_path / "fs_data"
    _write(clone / "demand_plan_summary.csv", "from github", age_s=3600)
    _write(drop / "fs_manual" / "demand_plan_summary.csv", "rebuilt locally", age_s=600)
    _write(drop / "fs_vif" / "erp_only.csv", "erp leftover")
    an_hour_ago = time.time() - 3600
    mirrored, kept, problems = pull.mirror_clone_into_drop(
        clone, drop, FILES, LAYOUT, asof=lambda n: an_hour_ago)
    assert mirrored == [] and problems == []
    assert len(kept) == 1 and kept[0].startswith("demand_plan_summary.csv (drop copy newer")
    assert (drop / "fs_manual" / "demand_plan_summary.csv").read_text(encoding="utf-8") == "rebuilt locally"
    assert (drop / "fs_vif" / "erp_only.csv").exists()
    # GitHub newer than the drop copy: it lands, stamped with GitHub's time
    a_minute_ago = time.time() - 60
    mirrored, kept, problems = pull.mirror_clone_into_drop(
        clone, drop, FILES, LAYOUT, asof=lambda n: a_minute_ago)
    assert mirrored == ["fs_manual/demand_plan_summary.csv"] and kept == [] and problems == []
    assert (drop / "fs_manual" / "demand_plan_summary.csv").read_text(encoding="utf-8") == "from github"
    assert abs((drop / "fs_manual" / "demand_plan_summary.csv").stat().st_mtime - a_minute_ago) < 1


def test_mirror_reports_an_unrouted_file_instead_of_guessing(tmp_path):
    pull = _pull()
    clone, drop = tmp_path / "clone", tmp_path / "fs_data"
    _write(clone / "order_npa.csv", "o")
    layout = {"fs_vif": ["manprg.txt"], "fs_manual": []}
    mirrored, kept, problems = pull.mirror_clone_into_drop(
        clone, drop, FILES, layout, asof=lambda n: None)
    assert mirrored == [] and kept == []
    assert len(problems) == 1 and problems[0].startswith("order_npa.csv: no drop_layout entry")
    assert not (drop / "fs_vif" / "order_npa.csv").exists()
    assert not (drop / "fs_manual" / "order_npa.csv").exists()


# ---------------------------------------------------------------------------
# pull_once with the mirror: GitHub -> fs_data, then folder mode from the root
# ---------------------------------------------------------------------------

class _Git:
    returncode = 0
    stderr = ""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _fake_git(calls, *, log_epoch: float):
    def fake(repo, *args):
        calls.append(args)
        if args and args[0] == "log":
            return _Git(str(int(log_epoch)))
        return _Git("abc1234\n")          # rev-parse before == after: HEAD unchanged
    return fake


def test_pull_once_mirrors_then_syncs_folder_mode_from_the_root(tmp_path, monkeypatch):
    pull = _pull()
    clone, drop, ref = tmp_path / "clone", tmp_path / "fs_data", tmp_path / "reference"
    _write(clone / "manprg.txt", "Start date;Start time;Line" + chr(10), age_s=3600)
    _write(clone / "cip_info.csv", "ID,LineEquipment" + chr(10) + "1,P09" + chr(10), age_s=3600)
    # the drop already holds an export nobody pushed yet — it must reach the app too
    _write(drop / "fs_vif" / "manprg2.txt", "local newer" + chr(10), age_s=120)
    calls: list = []
    monkeypatch.setattr(pull, "ensure_clone", lambda conf: clone)
    monkeypatch.setattr(pull, "git", _fake_git(calls, log_epoch=time.time() - 3600))
    conf = {"files": FILES, "data_reference_dir": str(ref), "settle_seconds": 60,
            "drop_layout": LAYOUT, "mirror_github_to": str(drop),
            "repo_url": "https://example.invalid/x.git", "remote": "origin", "branch": "main"}
    res = pull.pull_once(conf)              # no source_dirs: the mirror target IS the root
    assert res.mode == "folder"
    assert res.sources == [str(drop / "fs_vif"), str(drop / "fs_manual")]
    assert res.ok, res.problems
    assert sorted(res.mirror["mirrored"]) == ["fs_manual/cip_info.csv", "fs_vif/manprg.txt"]
    assert res.mirror["to"] == str(drop) and res.mirror["head"] is None and res.mirror["kept"] == []
    assert ("fetch", "origin", "main") in calls and ("pull", "--ff-only", "origin", "main") in calls
    # folder mode copied GitHub's files AND the drop-only export into data/reference
    assert (ref / "manprg.txt").read_text(encoding="utf-8").startswith("Start date")
    assert (ref / "manprg2.txt").read_text(encoding="utf-8") == "local newer" + chr(10)
    assert (ref / "cip_info.csv").exists()
    assert "manprg.txt" in res.updated and "manprg2.txt" in res.updated and "cip_info.csv" in res.updated
    hb = json.loads((ref / "live_sync.json").read_text(encoding="utf-8"))
    assert hb["mode"] == "folder" and hb["ok"] is True and hb["sources"] == res.sources
    assert hb["mirror"]["to"] == str(drop) and sorted(hb["mirror"]["mirrored"]) == sorted(res.mirror["mirrored"])
    # the console line and the health row name the mirror; the sources stay the folders
    assert pull._describe(res).startswith(f"GitHub -> {drop}: 2 file(s) dropped; synced from ")
    row = dh._live_sync_status(tmp_path, dict(dh.DEFAULT_CADENCE_H))
    assert row.state == dh.OK
    assert f"folder {drop / 'fs_vif'}; {drop / 'fs_manual'}" in row.detail
    assert "fed from GitHub (2 file(s) dropped this pass)" in row.detail
    # the manprg as-of is the commit time the mirror stamped (folder mode reads the file time)
    stamp = json.loads((ref / "manprg.asof.json").read_text(encoding="utf-8"))
    assert stamp["as_of"].startswith(time.strftime("%Y-%m-%d", time.localtime(time.time() - 3600))[:10])
    # second pass: nothing new anywhere, still ok, mirror reports 0 dropped
    res2 = pull.pull_once(conf)
    assert res2.ok and res2.mirror["mirrored"] == [] and res2.updated == []


def test_pull_once_mirror_failure_is_a_problem_but_the_drop_still_syncs(tmp_path, monkeypatch):
    """GitHub unreachable (no clone, no network): the pass reports it and still
    syncs whatever the drop holds — the planner keeps the last plant state
    and Home's row goes STALE with the reason, never a dead pass."""
    pull = _pull()
    drop, ref = tmp_path / "fs_data", tmp_path / "reference"
    _write(drop / "fs_vif" / "manprg.txt", "from the drop" + chr(10), age_s=600)

    def boom(conf):
        raise RuntimeError("clone failed — is the repo created + reachable?")

    monkeypatch.setattr(pull, "ensure_clone", boom)
    conf = {"files": FILES, "data_reference_dir": str(ref), "settle_seconds": 60,
            "drop_layout": LAYOUT, "mirror_github_to": str(drop),
            "source_dirs": [str(drop)], "repo_url": "https://example.invalid/x.git",
            "remote": "origin", "branch": "main"}
    res = pull.pull_once(conf)
    assert res.mode == "folder" and not res.ok
    assert any(p.startswith("drop mirror from GitHub failed: clone failed") for p in res.problems)
    assert (ref / "manprg.txt").read_text(encoding="utf-8") == "from the drop" + chr(10)
    assert res.mirror["mirrored"] == [] and res.mirror["to"] == str(drop)
    row = dh._live_sync_status(tmp_path, dict(dh.DEFAULT_CADENCE_H))
    assert row.state == dh.STALE and row.severity == 1 and "drop mirror from GitHub failed" in row.detail


def test_cli_mirror_switch_sets_the_conf_key(monkeypatch, tmp_path):
    """--mirror-github-to on the command line behaves like the local conf key."""
    pull = _pull()
    seen: dict = {}

    def fake_pull_once(conf):
        seen.update(conf)
        return pull.SyncResult(mode="folder", sources=[])

    monkeypatch.setattr(pull, "pull_once", fake_pull_once)
    monkeypatch.setattr(pull, "load_conf", lambda: {"files": FILES, "data_reference_dir": str(tmp_path)})
    monkeypatch.setattr("sys.argv", ["fs-live-pull.py", "--once", "--mirror-github-to", str(tmp_path / "fs_data")])
    assert pull.main() == 0
    assert seen[pull.MIRROR_KEY] == str(tmp_path / "fs_data")
