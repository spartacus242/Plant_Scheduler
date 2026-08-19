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


# ---------------------------------------------------------------------------
# Auto-eviction (2026-08-19): scenario runs save NEW timestamped versions, so
# at capacity the OLDEST auto-saved version (source solver:/agent:) makes way.
# User-named versions (digital_twin, manual, imports) are never evicted.
# ---------------------------------------------------------------------------

def _save_with(dd: Path, name: str, source: str, timestamp: str) -> str:
    import json
    slug = save_version(name, empty_calendar(), {"composite": None}, dd,
                        source=source)
    meta_p = dd / "versions" / slug / "metadata.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["timestamp"] = timestamp
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    return slug


def test_auto_evict_removes_oldest_auto_saved_first(tmp_path):
    dd = _dd(tmp_path)
    u1 = _save_with(dd, "user keep 1", "digital_twin", "2026-08-10T08:00:00")
    a1 = _save_with(dd, "auto old", "solver:balanced", "2026-08-11T08:00:00")
    a2 = _save_with(dd, "auto mid", "solver-custom:balanced", "2026-08-12T08:00:00")
    a3 = _save_with(dd, "auto new", "agent:proposal", "2026-08-13T08:00:00")
    u2 = _save_with(dd, "user keep 2", "manual", "2026-08-14T08:00:00")
    assert len(list_versions(dd)) == MAX_VERSIONS

    s1 = save_version("run 1", empty_calendar(), {"composite": None}, dd,
                      source="solver:balanced", auto_evict=True)
    slugs = {v["slug"] for v in list_versions(dd)}
    assert a1 not in slugs                      # oldest auto-saved went
    assert {u1, u2, a2, a3, s1} <= slugs
    assert len(slugs) == MAX_VERSIONS

    s2 = save_version("run 2", empty_calendar(), {"composite": None}, dd,
                      source="solver:balanced", auto_evict=True)
    slugs = {v["slug"] for v in list_versions(dd)}
    assert a2 not in slugs                      # oldest-first, one per save
    assert {u1, u2, a3, s1, s2} <= slugs


def test_auto_evict_never_touches_user_named(tmp_path):
    dd = _dd(tmp_path)
    for i in range(MAX_VERSIONS):
        _save_with(dd, f"user {i}", "digital_twin", f"2026-08-1{i}T08:00:00")
    with pytest.raises(ValueError, match=f"Maximum of {MAX_VERSIONS}"):
        save_version("scenario run", empty_calendar(), {"composite": None},
                     dd, source="solver:balanced", auto_evict=True)
    assert len(list_versions(dd)) == MAX_VERSIONS  # nothing was deleted


def test_manual_save_never_auto_evicts(tmp_path):
    """Only the runner path passes auto_evict — a manual save at capacity
    still refuses even when auto-saved versions exist."""
    dd = _dd(tmp_path)
    for i in range(MAX_VERSIONS):
        _save_with(dd, f"auto {i}", "solver:balanced", f"2026-08-1{i}T08:00:00")
    with pytest.raises(ValueError, match=f"Maximum of {MAX_VERSIONS}"):
        save_version("manual", empty_calendar(), {"composite": None}, dd)
    assert len(list_versions(dd)) == MAX_VERSIONS


def test_delete_clears_windows_readonly_orphan(tmp_path):
    # Walkthrough follow-up: real orphans carried the R attribute, so
    # shutil.rmtree died with WinError 5 (Access is denied) from the UI.
    import os
    import stat

    dd = _dd(tmp_path)
    orphan = dd / "versions" / "readonly_leftover"
    orphan.mkdir()
    inner = orphan / "stub.txt"
    inner.write_text("x", encoding="utf-8")
    os.chmod(inner, stat.S_IREAD)
    os.chmod(orphan, stat.S_IREAD)
    delete_version("readonly_leftover", dd)
    assert not orphan.exists()


def test_renamed_or_annotated_auto_save_is_never_evicted(tmp_path):
    # Review 2026-08-19: a solver run the user renamed ("KEEP - week 35
    # plan") or annotated kept source=solver:* and was silently deleted by
    # auto-evict. Curation must protect the slot.
    from helpers.version_manager import rename_version, update_notes

    dd = _dd(tmp_path)
    kept = _save_with(dd, "auto oldest", "solver:balanced",
                      "2026-08-10T08:00:00")
    rename_version(kept, "KEEP - week 35 plan", dd)
    noted = _save_with(dd, "auto notes", "agent:propose",
                       "2026-08-11T08:00:00")
    update_notes(noted, dd, pros="good W35 fill")
    evictable = _save_with(dd, "auto plain", "solver:balanced",
                           "2026-08-12T08:00:00")
    for i in range(MAX_VERSIONS - 3):
        _save_with(dd, f"user {i}", "digital_twin", f"2026-08-1{3 + i}T08:00:00")

    save_version("new run", empty_calendar(), {"composite": None}, dd,
                 source="solver:balanced", auto_evict=True)
    slugs = {v["slug"] for v in list_versions(dd)}
    assert kept in slugs and noted in slugs      # curated: protected
    assert evictable not in slugs                # plain auto: evicted


def test_evict_skips_invalid_slug_dirs_instead_of_wedging(tmp_path):
    # A hand-copied folder with solver metadata but a non-slug name must not
    # wedge every future auto-save (delete_version validates slugs).
    import json as _json

    dd = _dd(tmp_path)
    bad = dd / "versions" / "Hand-Copied Run"
    bad.mkdir()
    (bad / "metadata.json").write_text(_json.dumps({
        "name": "hand copy", "source": "solver:balanced",
        "timestamp": "2026-08-01T08:00:00", "scorecard": {}}),
        encoding="utf-8")
    evictable = _save_with(dd, "auto ok", "solver:balanced",
                           "2026-08-11T08:00:00")
    for i in range(MAX_VERSIONS - 2):
        _save_with(dd, f"user {i}", "digital_twin", f"2026-08-1{2 + i}T08:00:00")

    save_version("new run", empty_calendar(), {"composite": None}, dd,
                 source="solver:balanced", auto_evict=True)
    slugs = {v["slug"] for v in list_versions(dd)}
    assert evictable not in slugs        # the valid auto save was evicted
    assert bad.exists()                  # the odd folder was skipped, not fatal
