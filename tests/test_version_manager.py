# tests/test_version_manager.py — version slots: one truth for dirs vs list.
#
# Walkthrough finding 5 (2026-08-18): data/versions had 8 directories while
# list_versions showed 5 — orphaned folders (no metadata.json) were invisible
# to the UI and the MAX check, so "5 / 5 saved" and the on-disk truth
# disagreed. Orphans must be SURFACED and COUNTED.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.calendar_io import empty_calendar  # noqa: E402
from helpers.version_manager import (  # noqa: E402
    MAX_VERSIONS,
    delete_version,
    list_versions,
    save_version,
    upsert_version,
)


def _dd(tmp_path: Path) -> Path:
    dd = tmp_path / "data"
    (dd / "versions").mkdir(parents=True)
    return dd


def test_orphan_dir_surfaces_in_list(tmp_path):
    dd = _dd(tmp_path)
    (dd / "versions" / "leftover").mkdir()
    save_version("Real", empty_calendar(), {"composite": 1.0}, dd)
    versions = list_versions(dd)
    assert len(versions) == 2
    orphan = next(v for v in versions if v["slug"] == "leftover")
    assert orphan.get("orphan") is True
    real = next(v for v in versions if v["slug"] == "real")
    assert not real.get("orphan")


def test_corrupt_metadata_is_orphan(tmp_path):
    dd = _dd(tmp_path)
    bad = dd / "versions" / "broken"
    bad.mkdir()
    (bad / "metadata.json").write_text("{not json", encoding="utf-8")
    versions = list_versions(dd)
    assert [v["slug"] for v in versions] == ["broken"]
    assert versions[0].get("orphan") is True


def test_orphans_count_toward_max(tmp_path):
    """4 real + 1 orphan = 5 slots used: the save must refuse, and the error
    must tell the planner about the orphaned folder."""
    dd = _dd(tmp_path)
    for i in range(MAX_VERSIONS - 1):
        save_version(f"v{i}", empty_calendar(), {"composite": None}, dd)
    (dd / "versions" / "ghost").mkdir()
    assert len(list_versions(dd)) == MAX_VERSIONS
    with pytest.raises(ValueError, match="orphan"):
        save_version("one too many", empty_calendar(), {"composite": None}, dd)


def test_delete_orphan_frees_slot(tmp_path):
    dd = _dd(tmp_path)
    for i in range(MAX_VERSIONS - 1):
        save_version(f"v{i}", empty_calendar(), {"composite": None}, dd)
    (dd / "versions" / "ghost").mkdir()
    delete_version("ghost", dd)
    assert len(list_versions(dd)) == MAX_VERSIONS - 1
    save_version("fits now", empty_calendar(), {"composite": None}, dd)
    assert len(list_versions(dd)) == MAX_VERSIONS


def test_upsert_heals_orphan_at_fixed_slug(tmp_path):
    """upsert_version on an existing orphan dir writes metadata into it
    (no new slot consumed) — the azap_baseline snapshot self-heals."""
    dd = _dd(tmp_path)
    for i in range(MAX_VERSIONS - 1):
        save_version(f"v{i}", empty_calendar(), {"composite": None}, dd)
    (dd / "versions" / "azap_baseline").mkdir()
    upsert_version("azap_baseline", "Current schedule", empty_calendar(),
                   {"composite": 2.0}, dd)
    versions = list_versions(dd)
    healed = next(v for v in versions if v["slug"] == "azap_baseline")
    assert not healed.get("orphan")
    assert len(versions) == MAX_VERSIONS


def test_max_reached_without_orphans_keeps_plain_message(tmp_path):
    dd = _dd(tmp_path)
    for i in range(MAX_VERSIONS):
        save_version(f"v{i}", empty_calendar(), {"composite": None}, dd)
    with pytest.raises(ValueError) as exc:
        save_version("overflow", empty_calendar(), {"composite": None}, dd)
    assert "orphan" not in str(exc.value)
    assert f"Maximum of {MAX_VERSIONS}" in str(exc.value)
