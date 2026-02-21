# helpers/safe_io.py — Atomic file-write helpers for CSV and TOML.
#
# Every write goes to a temp file in the same directory, then os.replace()
# swaps it into place.  os.replace() is atomic on both POSIX and Windows,
# so a crash mid-write never corrupts the target file.

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_value(v):
    """Prefix string values that could trigger formula injection in Excel."""
    if isinstance(v, str) and v and v[0] in _FORMULA_PREFIXES:
        return "'" + v
    return v


def safe_write_csv(
    df: pd.DataFrame,
    path: Path | str,
    *,
    sanitize: bool = True,
    **to_csv_kwargs,
) -> None:
    """Write *df* to *path* atomically via temp-file + os.replace().

    When *sanitize* is True (default), string cells starting with ``=``,
    ``+``, ``-``, ``@``, tab, or CR are prefixed with a single-quote to
    prevent CSV injection when opened in Excel.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    to_csv_kwargs.setdefault("index", False)
    if sanitize:
        obj_cols = df.select_dtypes(include=["object"]).columns
        if len(obj_cols):
            df = df.copy()
            df[obj_cols] = df[obj_cols].map(_sanitize_csv_value)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            df.to_csv(f, **to_csv_kwargs)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def safe_write_toml(cfg: dict, path: Path | str) -> None:
    """Write *cfg* to *path* atomically via temp-file + os.replace()."""
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
