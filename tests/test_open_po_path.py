# tests/test_open_po_path.py — open-PO report resolver (supply timeline §4).
#
# Order: [datasources] po_report_path -> data/reference/open_pos.xlsx ->
# open_pos.csv -> None. Everything runs against a tmp_path data dir; the
# flowstate.toml read is monkeypatched so the repo's own config never leaks in.

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.reconcile_engine import open_po_path  # noqa: E402


def _dd(tmp_path: Path) -> Path:
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    return dd


def test_none_when_nothing_present(tmp_path):
    assert open_po_path(_dd(tmp_path), {}) is None


def test_bridge_xlsx_first(tmp_path):
    dd = _dd(tmp_path)
    (dd / "reference" / "open_pos.xlsx").write_bytes(b"x")
    (dd / "reference" / "open_pos.csv").write_text("a\n", encoding="utf-8")
    assert open_po_path(dd, {}) == dd / "reference" / "open_pos.xlsx"


def test_csv_fallback(tmp_path):
    dd = _dd(tmp_path)
    (dd / "reference" / "open_pos.csv").write_text("a\n", encoding="utf-8")
    assert open_po_path(dd, {}) == dd / "reference" / "open_pos.csv"


def test_configured_override_wins_over_bridge_copy(tmp_path):
    dd = _dd(tmp_path)
    (dd / "reference" / "open_pos.xlsx").write_bytes(b"x")
    custom = tmp_path / "it" / "NPA Open POs -9.01.xlsx"
    custom.parent.mkdir()
    custom.write_bytes(b"y")
    cfg = {"datasources": {"po_report_path": str(custom)}}
    assert open_po_path(dd, cfg) == custom


def test_configured_override_is_authoritative_even_when_absent(tmp_path):
    """cip_info precedent: a set path is returned as-is so the consumer can
    say "missing at <configured path>" rather than silently read the bridge
    copy. Blank / whitespace counts as not set."""
    dd = _dd(tmp_path)
    (dd / "reference" / "open_pos.xlsx").write_bytes(b"x")
    ghost = tmp_path / "nowhere.xlsx"
    assert open_po_path(dd, {"datasources": {"po_report_path": str(ghost)}}) == ghost
    assert open_po_path(dd, {"datasources": {"po_report_path": "   "}}) == \
        dd / "reference" / "open_pos.xlsx"


def test_accepts_str_data_dir(tmp_path):
    dd = _dd(tmp_path)
    (dd / "reference" / "open_pos.csv").write_text("a\n", encoding="utf-8")
    assert open_po_path(str(dd), {}) == dd / "reference" / "open_pos.csv"


def test_cfg_none_reads_flowstate_toml(tmp_path, monkeypatch):
    dd = _dd(tmp_path)
    custom = tmp_path / "cfg_pos.xlsx"
    custom.write_bytes(b"z")
    import helpers.config as hc
    monkeypatch.setattr(hc, "load_toml",
                        lambda path=None: {"datasources": {"po_report_path": str(custom)}})
    assert open_po_path(dd) == custom
    monkeypatch.setattr(hc, "load_toml", lambda path=None: {})
    assert open_po_path(dd) is None


def test_bridge_fallback_skips_a_directory_named_like_the_file(tmp_path):
    """A stray FOLDER called open_pos.xlsx (or .csv) is not a report: the
    resolver falls through, never hands a directory to the loader."""
    dd = _dd(tmp_path)
    (dd / "reference" / "open_pos.xlsx").mkdir()
    (dd / "reference" / "open_pos.csv").write_text("a\n", encoding="utf-8")
    assert open_po_path(dd, {}) == dd / "reference" / "open_pos.csv"
    (dd / "reference" / "open_pos.csv").unlink()
    (dd / "reference" / "open_pos.csv").mkdir()
    assert open_po_path(dd, {}) is None


def test_configured_override_directory_is_returned_as_is(tmp_path):
    """Contract: the override is authoritative even when unusable — the
    consumer (data_health / the report) says why; the resolver never
    silently swaps in the bridge copy."""
    dd = _dd(tmp_path)
    (dd / "reference" / "open_pos.xlsx").write_bytes(b"x")
    folder = tmp_path / "it_drop"
    folder.mkdir()
    assert open_po_path(dd, {"datasources": {"po_report_path": str(folder)}}) == folder
