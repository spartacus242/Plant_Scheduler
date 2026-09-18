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
    """git for the pull_once tests: HEAD never moves, every file has ONE commit
    at `log_epoch` carrying today's blob (git log answers that epoch for -1 and
    for --find-object alike; rev-parse '<rev>:<path>' the blob)."""
    def fake(repo, *args):
        calls.append(args)
        if args and args[0] == "log":
            return _Git(f"{int(log_epoch)}\n")       # -1 and --find-object alike
        if args and args[0] == "rev-parse" and ":" in args[-1]:
            return _Git("b10b0001\n")                  # the blob at HEAD:<path>
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


# ---------------------------------------------------------------------------
# second cut, the lesson of 2026-09-18 10:08: the work PC pushed a fresh
# manprg at 10:00 and re-pushed the 09-15 export at 10:08; the first cut
# stamped that old content with the fresh commit time (the drop looked new),
# and the settle rule had held the fresh 10:00 file a pass, long enough to
# be overwritten. Pinned here: the as-of is when the CONTENT first appeared,
# a revert never outranks a fresher drop copy, an identical copy with a
# later time is put back, and what the mirror wrote this pass never settles.
# ---------------------------------------------------------------------------

def _git_commit(repo: Path, name: str, text: str, when: str) -> None:
    import subprocess
    (repo / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid"}
    subprocess.run(["git", "-C", str(repo), "add", "--", name], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"update {name}"],
                   check=True, capture_output=True, env=env)


def test_clone_asof_is_when_the_current_content_first_appeared(tmp_path):
    import subprocess
    from datetime import datetime, timezone
    pull = _pull()
    repo = tmp_path / "clone"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True, capture_output=True)
    _git_commit(repo, "manprg.txt", "old export", "2026-09-15T11:38:16-06:00")
    _git_commit(repo, "manprg.txt", "fresh export", "2026-09-18T10:00:20-06:00")
    _git_commit(repo, "manprg.txt", "old export", "2026-09-18T10:08:18-06:00")    # the revert
    got = pull.clone_asof(repo, "manprg.txt")
    assert datetime.fromtimestamp(got, tz=timezone.utc) == datetime(2026, 9, 15, 17, 38, 16, tzinfo=timezone.utc)
    # genuinely new content: its own commit time
    _git_commit(repo, "manprg.txt", "newer export", "2026-09-18T10:30:00-06:00")
    got2 = pull.clone_asof(repo, "manprg.txt")
    assert datetime.fromtimestamp(got2, tz=timezone.utc) == datetime(2026, 9, 18, 16, 30, 0, tzinfo=timezone.utc)
    assert pull.clone_asof(repo, "never_committed.csv") is None


def test_mirror_revert_never_clobbers_a_fresher_drop_copy_and_retimes_identical_files(tmp_path):
    pull = _pull()
    clone, drop = tmp_path / "clone", tmp_path / "fs_data"
    _write(clone / "manprg.txt", "old export", age_s=60)                 # checked out just now
    _write(drop / "fs_vif" / "manprg.txt", "fresh export", age_s=16 * 60)  # the 10:00 export
    content_first_seen = time.time() - 3 * 86400                          # the 09-15 blob
    mirrored, kept, problems = pull.mirror_clone_into_drop(
        clone, drop, FILES, LAYOUT, asof=lambda n: content_first_seen)
    assert mirrored == [] and problems == []
    assert kept and kept[0].startswith("manprg.txt (drop copy newer")
    assert (drop / "fs_vif" / "manprg.txt").read_text(encoding="utf-8") == "fresh export"
    # an identical copy stamped with the revert's fresh commit time is put back
    # to the content's first appearance, and reported
    _write(clone / "manprg2.txt", "old export 2", age_s=60)
    _write(drop / "fs_vif" / "manprg2.txt", "old export 2", age_s=30 * 60)
    mirrored, kept, problems = pull.mirror_clone_into_drop(
        clone, drop, FILES, LAYOUT, asof=lambda n: content_first_seen)
    assert problems == [] and kept                                        # manprg.txt still kept
    assert len(mirrored) == 1 and mirrored[0].startswith("fs_vif/manprg2.txt (time set to ")
    assert abs((drop / "fs_vif" / "manprg2.txt").stat().st_mtime - content_first_seen) < 1
    assert (drop / "fs_vif" / "manprg2.txt").read_text(encoding="utf-8") == "old export 2"
    # already at the content's time: quiet
    assert pull.mirror_clone_into_drop(clone, drop, FILES, LAYOUT, asof=lambda n: content_first_seen)[0] == []


def test_pull_once_syncs_what_the_mirror_just_wrote_despite_the_settle_rule(tmp_path, monkeypatch):
    pull = _pull()
    clone, drop, ref = tmp_path / "clone", tmp_path / "fs_data", tmp_path / "reference"
    _write(clone / "manprg.txt", "fresh export", age_s=5)
    calls: list = []
    monkeypatch.setattr(pull, "ensure_clone", lambda conf: clone)
    monkeypatch.setattr(pull, "git", _fake_git(calls, log_epoch=time.time() - 9))   # committed 9 s ago
    conf = {"files": FILES, "data_reference_dir": str(ref), "settle_seconds": 60,
            "drop_layout": LAYOUT, "mirror_github_to": str(drop),
            "repo_url": "https://example.invalid/x.git", "remote": "origin", "branch": "main"}
    res = pull.pull_once(conf)
    assert res.ok, res.problems
    assert res.mirror["mirrored"] == ["fs_vif/manprg.txt"]
    assert "manprg.txt" in res.updated and res.skipped == []
    assert (ref / "manprg.txt").read_text(encoding="utf-8") == "fresh export"
    # a file the drop got from ELSEWHERE seconds ago still settles a pass
    _write(drop / "fs_vif" / "manprg2.txt", "being written", age_s=5)
    res2 = pull.pull_once(conf)
    assert any(s.startswith("manprg2.txt (modified") for s in res2.skipped)
    assert not (ref / "manprg2.txt").exists()


def test_adhoc_source_dir_never_mirrors_unless_asked(monkeypatch, tmp_path):
    """--source-dir means "sync from that folder": the machine's own drop
    mirror (local conf) stays out of it — a test or an ad-hoc run must never
    write into this box's fs_data. Asked for on the same command line, it
    still works."""
    pull = _pull()
    seen: dict = {}

    def fake_pull_once(conf):
        seen.clear()
        seen.update(conf)
        return pull.SyncResult(mode="folder", sources=[])

    monkeypatch.setattr(pull, "pull_once", fake_pull_once)
    monkeypatch.setattr(pull, "load_conf", lambda: {"files": FILES, "data_reference_dir": str(tmp_path),
                                                    pull.MIRROR_KEY: str(tmp_path / "machine_drop")})
    monkeypatch.setattr("sys.argv", ["fs-live-pull.py", "--once", "--source-dir", str(tmp_path / "adhoc")])
    assert pull.main() == 0
    assert pull.MIRROR_KEY not in seen and seen["source_dirs"] == [str(tmp_path / "adhoc")]
    monkeypatch.setattr("sys.argv", ["fs-live-pull.py", "--once", "--source-dir", str(tmp_path / "adhoc"),
                                     "--mirror-github-to", str(tmp_path / "adhoc")])
    assert pull.main() == 0
    assert seen[pull.MIRROR_KEY] == str(tmp_path / "adhoc")


def test_local_conf_env_override_picks_or_skips_the_machine_conf(monkeypatch, tmp_path):
    pull = _pull()
    other = tmp_path / "other.local.json"
    other.write_text(json.dumps({"settle_seconds": 7, pull.MIRROR_KEY: str(tmp_path / "d")}), encoding="utf-8")
    monkeypatch.setenv(pull.LOCAL_CONF_ENV, str(other))
    conf = pull.load_conf()
    assert conf["settle_seconds"] == 7 and conf[pull.MIRROR_KEY] == str(tmp_path / "d")
    monkeypatch.setenv(pull.LOCAL_CONF_ENV, "")
    conf = pull.load_conf()
    assert pull.MIRROR_KEY not in conf and conf.get("source_dirs", []) == []   # the tracked conf alone

