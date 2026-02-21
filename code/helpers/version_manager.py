# helpers/version_manager.py — Save / load / delete sandbox schedule versions.
#
# Each version is stored in data/versions/<slug>/ with:
#   schedule.csv, cip_windows.csv, metadata.json
# Max 5 versions.

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

MAX_VERSIONS = 5


def _versions_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "versions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "version"


def _unique_slug(name: str, data_dir: Path) -> str:
    base = _slugify(name)
    vd = _versions_dir(data_dir)
    if not (vd / base).exists():
        return base
    for i in range(2, MAX_VERSIONS + 10):
        candidate = f"{base}_{i}"
        if not (candidate_path := vd / candidate).exists():
            return candidate
    return f"{base}_{int(datetime.now().timestamp())}"


# ── Public API ────────────────────────────────────────────────────────────


def list_versions(data_dir: Path) -> list[dict[str, Any]]:
    """Return list of saved versions sorted by timestamp (newest first)."""
    vd = _versions_dir(data_dir)
    versions: list[dict] = []
    for d in sorted(vd.iterdir()):
        meta_path = d / "metadata.json"
        if d.is_dir() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["slug"] = d.name
                versions.append(meta)
            except Exception:
                continue
    versions.sort(key=lambda v: v.get("timestamp", ""), reverse=True)
    return versions


def save_version(
    name: str,
    schedule: list[dict],
    cip_blocks: list[dict],
    kpis: dict[str, Any],
    data_dir: Path,
) -> str:
    """Persist a sandbox version. Returns the slug.

    Raises ValueError if MAX_VERSIONS already reached.
    """
    existing = list_versions(data_dir)
    if len(existing) >= MAX_VERSIONS:
        raise ValueError(
            f"Maximum of {MAX_VERSIONS} versions reached. "
            "Delete a version before saving a new one."
        )

    slug = _unique_slug(name, data_dir)
    dest = _versions_dir(data_dir) / slug
    dest.mkdir(parents=True, exist_ok=True)

    # Schedule CSV
    if schedule:
        pd.DataFrame(schedule).to_csv(dest / "schedule.csv", index=False)
    # CIP CSV
    if cip_blocks:
        pd.DataFrame(cip_blocks).to_csv(dest / "cip_windows.csv", index=False)

    meta = {
        "name": name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "kpis": kpis,
    }
    (dest / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return slug


def load_version(slug: str, data_dir: Path) -> dict[str, Any]:
    """Load schedule, CIP blocks, and metadata for a version."""
    vdir = _versions_dir(data_dir) / slug
    result: dict[str, Any] = {"schedule": [], "cip_blocks": [], "metadata": {}}
    sched_path = vdir / "schedule.csv"
    if sched_path.exists():
        df = pd.read_csv(sched_path)
        result["schedule"] = df.to_dict("records")
    cip_path = vdir / "cip_windows.csv"
    if cip_path.exists():
        df = pd.read_csv(cip_path)
        result["cip_blocks"] = df.to_dict("records")
    meta_path = vdir / "metadata.json"
    if meta_path.exists():
        result["metadata"] = json.loads(meta_path.read_text(encoding="utf-8"))
    return result


def rename_version(slug: str, new_name: str, data_dir: Path) -> None:
    meta_path = _versions_dir(data_dir) / slug / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["name"] = new_name
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def delete_version(slug: str, data_dir: Path) -> None:
    vdir = _versions_dir(data_dir) / slug
    if vdir.exists() and vdir.is_dir():
        shutil.rmtree(vdir)


def delete_all_versions(data_dir: Path) -> None:
    vd = _versions_dir(data_dir)
    if vd.exists():
        shutil.rmtree(vd)
        vd.mkdir(parents=True, exist_ok=True)


def promote_version(slug: str, data_dir: Path) -> None:
    """Copy version files to the main schedule_phase2.csv / cip_windows.csv."""
    vdir = _versions_dir(data_dir) / slug
    dd = Path(data_dir)
    sched_src = vdir / "schedule.csv"
    cip_src = vdir / "cip_windows.csv"
    if sched_src.exists():
        shutil.copy2(sched_src, dd / "schedule_phase2.csv")
    if cip_src.exists():
        shutil.copy2(cip_src, dd / "cip_windows.csv")


def export_version_excel(slug: str, data_dir: Path) -> bytes:
    """Return version schedule as Excel bytes for download."""
    vdir = _versions_dir(data_dir) / slug
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        sched_path = vdir / "schedule.csv"
        if sched_path.exists():
            pd.read_csv(sched_path).to_excel(writer, sheet_name="Schedule", index=False)
        cip_path = vdir / "cip_windows.csv"
        if cip_path.exists():
            pd.read_csv(cip_path).to_excel(writer, sheet_name="CIP Windows", index=False)
    return buf.getvalue()
