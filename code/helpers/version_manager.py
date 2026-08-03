# helpers/version_manager.py — Named calendar versions with scorecard snapshots.

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from helpers.calendar_io import load_calendar, save_calendar
from helpers.paths import versions_dir
from helpers.safe_io import safe_write_json

MAX_VERSIONS = 5
_SLUG_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid version slug {slug!r}: must be 1-64 lowercase alphanumeric/underscore."
        )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "version"


def _unique_slug(name: str, data_dir: Path) -> str:
    base = _slugify(name)
    vd = versions_dir(data_dir)
    if not (vd / base).exists():
        return base
    for i in range(2, MAX_VERSIONS + 10):
        candidate = f"{base}_{i}"
        if not (vd / candidate).exists():
            return candidate
    return f"{base}_{int(datetime.now().timestamp())}"


def list_versions(data_dir: Path) -> list[dict[str, Any]]:
    vd = versions_dir(data_dir)
    versions: list[dict] = []
    for d in sorted(vd.iterdir()):
        meta_path = d / "metadata.json"
        if d.is_dir() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["slug"] = d.name
                versions.append(meta)
            except (json.JSONDecodeError, OSError, KeyError):
                continue
    versions.sort(key=lambda v: v.get("timestamp", ""), reverse=True)
    return versions


def save_version(
    name: str,
    calendar: pd.DataFrame,
    scorecard: dict[str, Any],
    data_dir: Path,
    *,
    pros: str = "",
    cons: str = "",
    notes: str = "",
    source: str = "manual",
) -> str:
    existing = list_versions(data_dir)
    if len(existing) >= MAX_VERSIONS:
        raise ValueError(
            f"Maximum of {MAX_VERSIONS} versions reached. Delete one before saving."
        )
    slug = _unique_slug(name, data_dir)
    dest = versions_dir(data_dir) / slug
    dest.mkdir(parents=True, exist_ok=True)
    save_calendar(calendar, dest / "calendar_blocks.csv")
    meta = {
        "name": name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "pros": pros,
        "cons": cons,
        "notes": notes,
        "scorecard": scorecard,
    }
    safe_write_json(meta, dest / "metadata.json")
    return slug


def upsert_version(
    slug: str,
    name: str,
    calendar: pd.DataFrame,
    scorecard: dict[str, Any],
    data_dir: Path,
    *,
    pros: str = "",
    cons: str = "",
    notes: str = "",
    source: str = "manual",
) -> str:
    """Create or overwrite a version at a fixed slug (does not count toward MAX when updating)."""
    _validate_slug(slug)
    dest = versions_dir(data_dir) / slug
    creating = not dest.exists()
    if creating:
        existing = list_versions(data_dir)
        if len(existing) >= MAX_VERSIONS:
            raise ValueError(
                f"Maximum of {MAX_VERSIONS} versions reached. Delete one before saving."
            )
    dest.mkdir(parents=True, exist_ok=True)
    save_calendar(calendar, dest / "calendar_blocks.csv")
    meta = {
        "name": name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "pros": pros,
        "cons": cons,
        "notes": notes,
        "scorecard": scorecard,
    }
    safe_write_json(meta, dest / "metadata.json")
    return slug


def load_version(slug: str, data_dir: Path) -> dict[str, Any]:
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    result: dict[str, Any] = {"calendar": pd.DataFrame(), "metadata": {}}
    cal_path = vdir / "calendar_blocks.csv"
    if cal_path.exists():
        result["calendar"] = load_calendar(cal_path)
    meta_path = vdir / "metadata.json"
    if meta_path.exists():
        result["metadata"] = json.loads(meta_path.read_text(encoding="utf-8"))
    return result


def rename_version(slug: str, new_name: str, data_dir: Path) -> None:
    _validate_slug(slug)
    meta_path = versions_dir(data_dir) / slug / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["name"] = new_name
        safe_write_json(meta, meta_path)


def update_notes(slug: str, data_dir: Path, *, pros: str | None = None, cons: str | None = None, notes: str | None = None) -> None:
    _validate_slug(slug)
    meta_path = versions_dir(data_dir) / slug / "metadata.json"
    if not meta_path.exists():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if pros is not None:
        meta["pros"] = pros
    if cons is not None:
        meta["cons"] = cons
    if notes is not None:
        meta["notes"] = notes
    safe_write_json(meta, meta_path)


def delete_version(slug: str, data_dir: Path) -> None:
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    if vdir.exists() and vdir.is_dir():
        shutil.rmtree(vdir)


def delete_all_versions(data_dir: Path) -> None:
    vd = versions_dir(data_dir)
    if vd.exists():
        shutil.rmtree(vd)
        vd.mkdir(parents=True, exist_ok=True)


def promote_version(slug: str, data_dir: Path) -> None:
    """Copy version calendar to official calendar_blocks.csv."""
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    src = vdir / "calendar_blocks.csv"
    if src.exists():
        shutil.copy2(src, Path(data_dir) / "calendar_blocks.csv")


def export_version_excel(slug: str, data_dir: Path) -> bytes:
    _validate_slug(slug)
    vdir = versions_dir(data_dir) / slug
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        cal = vdir / "calendar_blocks.csv"
        if cal.exists():
            pd.read_csv(cal).to_excel(writer, sheet_name="Calendar", index=False)
        meta_path = vdir / "metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            sc = meta.get("scorecard", {})
            flat = []
            for section in ("changeovers", "cip", "trials", "maintenance", "campaigns", "service"):
                for k, v in (sc.get(section) or {}).items():
                    flat.append({"section": section, "metric": k, "value": v})
            if flat:
                pd.DataFrame(flat).to_excel(writer, sheet_name="Scorecard", index=False)
    return buf.getvalue()
