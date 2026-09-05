# tests/test_live_bridge_glob.py — bridge conf glob entries ("<glob> -> <dest>").
#
# fs-live-push.py (work PC) and fs-live-pull.py (laptop) each carry a VERBATIM
# copy of resolve_entries — the work PC runs a hand-copied single script, so
# neither may import the other. Both copies are loaded here and run through
# the same cases so they cannot drift apart.

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                  SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["fs-live-pull.py", "fs-live-push.py"])
def resolve_entries(request):
    return _load(request.param).resolve_entries


def _touch(path: Path, age_h: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(path.name.encode())
    old = time.time() - age_h * 3600
    os.utime(path, (old, old))
    return path


def test_plain_entries_unchanged(tmp_path, resolve_entries):
    _touch(tmp_path / "manprg.txt", 1)
    out = resolve_entries(["manprg.txt", "missing.csv"], tmp_path)
    assert out == [(tmp_path / "manprg.txt", "manprg.txt")]


def test_glob_picks_newest_match_under_dest_name(tmp_path, resolve_entries):
    _touch(tmp_path / "NPA Open POs -8.24.xlsx", 48)
    newest = _touch(tmp_path / "NPA Open POs -9.01.xlsx", 1)
    _touch(tmp_path / "NPA Open POs -8.31.xlsx", 24)
    out = resolve_entries(["NPA Open POs*.xlsx -> open_pos.xlsx"], tmp_path)
    assert out == [(newest, "open_pos.xlsx")]


def test_glob_with_no_match_is_dropped(tmp_path, resolve_entries):
    out = resolve_entries(["NPA Open POs*.xlsx -> open_pos.xlsx"], tmp_path)
    assert out == []


def test_glob_ignores_directories(tmp_path, resolve_entries):
    (tmp_path / "NPA Open POs folder.xlsx").mkdir()
    f = _touch(tmp_path / "NPA Open POs -9.01.xlsx", 1)
    out = resolve_entries(["NPA Open POs*.xlsx -> open_pos.xlsx"], tmp_path)
    assert out == [(f, "open_pos.xlsx")]


def test_mixed_list_keeps_conf_order(tmp_path, resolve_entries):
    _touch(tmp_path / "manprg.txt", 1)
    po = _touch(tmp_path / "NPA Open POs -9.01.xlsx", 1)
    _touch(tmp_path / "cip_info.csv", 1)
    out = resolve_entries(
        ["manprg.txt", "NPA Open POs*.xlsx -> open_pos.xlsx", "cip_info.csv"],
        tmp_path)
    assert [d for _, d in out] == ["manprg.txt", "open_pos.xlsx", "cip_info.csv"]
    assert out[1][0] == po


def test_same_dest_from_plain_and_glob_newest_source_wins(tmp_path, resolve_entries):
    """The shipped conf lists both "open_pos.xlsx" and the dated glob so a
    fixed-name delivery from IT works without a conf edit. If both exist the
    newer one is copied — once, not twice."""
    fixed = _touch(tmp_path / "open_pos.xlsx", 30)
    dated = _touch(tmp_path / "NPA Open POs -9.01.xlsx", 1)
    entries = ["open_pos.xlsx", "NPA Open POs*.xlsx -> open_pos.xlsx"]
    out = resolve_entries(entries, tmp_path)
    assert out == [(dated, "open_pos.xlsx")]
    os.utime(fixed, (time.time(), time.time()))
    out = resolve_entries(entries, tmp_path)
    assert out == [(fixed, "open_pos.xlsx")]


def test_blank_glob_or_dest_is_dropped(tmp_path, resolve_entries):
    _touch(tmp_path / "x.xlsx", 1)
    assert resolve_entries(["x.xlsx -> ", " -> y.xlsx"], tmp_path) == []


def test_shipped_conf_carries_the_open_pos_entries():
    import json
    conf = json.loads((SCRIPTS / "fs-live-data.conf.json").read_text(encoding="utf-8"))
    assert "open_pos.xlsx" in conf["files"]
    assert "NPA Open POs*.xlsx -> open_pos.xlsx" in conf["files"]


def test_helper_copies_are_identical():
    """Guard against the two scripts drifting: the helper source must match."""
    import inspect
    a = inspect.getsource(_load("fs-live-pull.py").resolve_entries)
    b = inspect.getsource(_load("fs-live-push.py").resolve_entries)
    assert a == b


def test_bad_entries_cost_only_themselves(tmp_path, resolve_entries):
    """One rotten conf entry — a non-relative glob (pathlib refuses it with
    NotImplementedError), a non-string — must not abort the pass: the
    entries around it still resolve, in conf order."""
    good = _touch(tmp_path / "manprg.txt", 1)
    po = _touch(tmp_path / "NPA Open POs -9.01.xlsx", 1)
    absolute = f"{tmp_path / '*.xlsx'} -> abs.xlsx"
    out = resolve_entries(
        ["manprg.txt", absolute, None, 42, ["open_pos.xlsx"], {"x": 1},
         "NPA Open POs*.xlsx -> open_pos.xlsx"], tmp_path)
    assert out == [(good, "manprg.txt"), (po, "open_pos.xlsx")]
