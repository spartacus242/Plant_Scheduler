# helpers/safe_io.py — Atomic CSV / TOML / JSON writes.

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_value(v):
    if isinstance(v, str) and v and v[0] in _FORMULA_PREFIXES:
        return "'" + v
    return v


def safe_write_csv(df: pd.DataFrame, path: Path | str, *, sanitize: bool = True, **kwargs) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("index", False)
    if sanitize:
        obj_cols = df.select_dtypes(include=["object"]).columns
        if len(obj_cols):
            df = df.copy()
            df[obj_cols] = df[obj_cols].map(_sanitize_csv_value)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            df.to_csv(f, **kwargs)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_file(path: Path | str, backup_dir: Path | str | None = None,
                *, tag: str = "") -> Path | None:
    """Copy `path` to `<backup_dir>/<stem>[.<tag>].<YYYYmmdd-HHMMSS>[-N]<suffix>`
    before it is overwritten. None when the source does not exist.

    Fix writeback-13 (audit 2026-09-03): only promote_version and pages/data
    backed the official board up; the board Save, float-link saves, the
    scorecard import and generate's naive_set_base overwrote it with no
    recovery path. Every writer now goes through save_calendar, which calls
    this. Default backup_dir = <path.parent>/_backups (data/_backups for the
    board). A same-second collision gets a -N suffix so nothing is clobbered.
    """
    import shutil
    from datetime import datetime

    src = Path(path)
    if not src.exists():
        return None
    bdir = Path(backup_dir) if backup_dir is not None else src.parent / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    mid = f".{tag}" if tag else ""
    dest = bdir / f"{src.stem}{mid}.{stamp}{src.suffix}"
    n = 1
    while dest.exists():
        n += 1
        dest = bdir / f"{src.stem}{mid}.{stamp}-{n}{src.suffix}"
    shutil.copy2(src, dest)
    return dest


def safe_write_json(obj: dict, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def safe_write_toml(cfg: dict, path: Path | str) -> None:
    import tomli_w

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(cfg, f)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
