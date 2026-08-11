# changeover_cache.py — cached, family-aware changeover matrix loader.
#
# changeovers.csv is a dense 211x211 (44,310-row) long-format table. It is
# parsed on every solver solve (code/solver/data_loader.py) and again on every
# Generate-page render (code/pages/generate.py, code/pages/calendar.py). This
# module centralises that parse behind two layers:
#
#   * a parquet cache on disk, keyed by the source file's mtime_ns, so a fresh
#     Streamlit rerun or a fresh solver subprocess re-reads a small binary
#     column store instead of the 1.39 MB CSV + 44k-row Python iteration
#     (measured ~630 ms -> a few ms);
#   * an in-process memoisation dict keyed by (abspath, mtime_ns) so repeated
#     calls within one process reuse the already-built objects with zero IO.
#
# It also exposes the family-compression helpers (item 11 / item 28). See
# scripts/measure_changeover.py for the measured impact: because the model
# builds one cost term per ADJACENT pair (not per matrix entry), family
# compression does NOT shrink the model — it only zeroes the additive penalty
# for within-family adjacencies. The parse cache is the real, safe win.

from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

_CACHE_ROOT = Path(tempfile.gettempdir()) / "flowstate_changeover_cache"
_MEMO: Dict[Tuple[str, int], object] = {}

_MACHINE_COLS = ("ttp_change", "ffs_change", "topload_change", "casepacker_change")
_NEW_COLS = ("conv_to_org_change", "cinn_to_non", "added_flavors")
_DEFAULT_FAMILY_COLS = (
    "ediact_sku_format",
    "format",
    "recipe",
    "casepacker_format",
    "topload_format",
    "pouch_format",
    "is_organic",
    "flavor_count",
    "has_cinnamon",
)


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _norm_df(src: Path) -> pd.DataFrame:
    """Read the raw CSV and build the exact column set the model consumes."""
    chg = pd.read_csv(src)
    chg["from_sku"] = chg["from_sku"].astype(str)
    chg["to_sku"] = chg["to_sku"].astype(str)
    chg["setup_hours"] = pd.to_numeric(chg["setup_hours"], errors="coerce").fillna(0.0)
    chg["setup_rounded"] = chg["setup_hours"].apply(round_half_up)
    for col in _MACHINE_COLS:
        if col in chg.columns:
            chg[col] = pd.to_numeric(chg[col], errors="coerce").fillna(1).astype(int)
    for col in ("conv_to_org_change", "cinn_to_non"):
        if col in chg.columns:
            chg[col] = pd.to_numeric(chg[col], errors="coerce").fillna(0).astype(int)
    if "added_flavors" in chg.columns:
        chg["added_flavors"] = pd.to_numeric(
            chg["added_flavors"], errors="coerce"
        ).fillna(0).astype(int)
    return chg


def _cache_path(src: Path, mtime_ns: int) -> Path:
    h = hashlib.sha1(str(src.resolve()).encode("utf-8")).hexdigest()[:16]
    return _CACHE_ROOT / f"co_{h}_{mtime_ns}.parquet"


def _prune(src: Path, keep_mtime_ns: int) -> None:
    h = hashlib.sha1(str(src.resolve()).encode("utf-8")).hexdigest()[:16]
    try:
        for p in _CACHE_ROOT.glob(f"co_{h}_*.parquet"):
            if p.stem != f"co_{h}_{keep_mtime_ns}":
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def load_changeover_dataframe(path) -> pd.DataFrame:
    """Return the normalised changeover table, cached to parquet keyed by mtime.

    A new export (edited/overwritten changeovers.csv) transparently bypasses a
    stale parquet because the key includes the source mtime. Cache misses and
    any parquet read/write failure fall back to a direct CSV parse.
    """
    src = Path(path)
    mtime_ns = src.stat().st_mtime_ns
    cp = _cache_path(src, mtime_ns)
    try:
        if cp.exists():
            return pd.read_parquet(cp)
    except Exception:
        pass
    df = _norm_df(src)
    try:
        _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cp, index=False)
        _prune(src, mtime_ns)
    except Exception:
        pass
    return df


def load_changeover_dicts(path) -> Tuple[dict, dict, dict]:
    """Return (setup, machine_changes, changeover_type).

    The shapes are identical to the legacy data_loader build, so swapping the
    call site in data_loader.load() is behaviour-preserving. Memoised per
    (abspath, mtime_ns) within a process.
    """
    src = Path(path)
    mtime_ns = src.stat().st_mtime_ns
    key = (str(src.resolve()), mtime_ns)
    if key in _MEMO:
        return _MEMO[key]  # type: ignore[return-value]
    chg = load_changeover_dataframe(src)
    has_machine_cols = all(c in chg.columns for c in _MACHINE_COLS)
    has_new = all(c in chg.columns for c in _NEW_COLS)
    setup: dict = {}
    machine_changes: dict = {}
    changeover_type: dict = {}
    for _, r in chg.iterrows():
        pair = (str(r["from_sku"]), str(r["to_sku"]))
        setup[pair] = int(r["setup_rounded"])
        if has_machine_cols:
            mc = {
                "ttp": int(r["ttp_change"]),
                "ffs": int(r["ffs_change"]),
                "topload": int(r["topload_change"]),
                "casepacker": int(r["casepacker_change"]),
            }
        else:
            full = 1 if int(r["setup_rounded"]) > 0 else 0
            mc = {"ttp": full, "ffs": full, "topload": full, "casepacker": full}
        if has_new:
            mc["conv_to_org"] = int(r["conv_to_org_change"])
            mc["cinn_to_non"] = int(r["cinn_to_non"])
            mc["added_flavors"] = int(r["added_flavors"])
        else:
            mc["conv_to_org"] = 0
            mc["cinn_to_non"] = 0
            mc["added_flavors"] = 0
        machine_changes[pair] = mc
        changeover_type[pair] = (
            f"{mc['ttp']}-{mc['ffs']}-{mc['topload']}-{mc['casepacker']}"
        )
    result = (setup, machine_changes, changeover_type)
    _MEMO[key] = result
    return result


def load_changeover_setup_nested(path) -> dict:
    """Return {from_sku: {to_sku: setup_hours}} — the shape the Gantt/Generate
    page consumes. Memoised per (abspath, mtime_ns)."""
    src = Path(path)
    mtime_ns = src.stat().st_mtime_ns
    key = ("nested", str(src.resolve()), mtime_ns)
    if key in _MEMO:
        return _MEMO[key]  # type: ignore[return-value]
    chg = load_changeover_dataframe(src)
    nested: Dict[str, Dict[str, float]] = {}
    for _, r in chg.iterrows():
        nested.setdefault(str(r["from_sku"]), {})[str(r["to_sku"])] = float(
            r["setup_rounded"]
        )
    _MEMO[key] = nested
    return nested


def build_sku_families(
    sku_info_df: pd.DataFrame, cols: Optional[List[str]] = None
) -> Dict[str, str]:
    """Group SKUs that share every changeover-relevant format attribute into a
    single family id. SKUs sharing a family incur no format-change penalty
    between them (see compress_machine_changes)."""
    if cols is None:
        cols = [c for c in _DEFAULT_FAMILY_COLS if c in sku_info_df.columns]
    if not cols:
        return {str(s): str(s) for s in sku_info_df["sku"].astype(str)}
    fam: Dict[str, str] = {}
    for _, r in sku_info_df.iterrows():
        sku = str(r["sku"])
        fam[sku] = "|".join(str(r.get(c, "")) for c in cols)
    return fam


def compress_machine_changes(
    machine_changes: dict, sku_family: Dict[str, str]
) -> dict:
    """Return a copy of machine_changes where every WITHIN-family transition has
    its change flags zeroed (objective penalty collapses to between-family
    only). Between-family transitions are preserved unchanged. Physical
    setup_hours / timing is NOT touched by this function."""
    zero_keys = ("ttp", "ffs", "topload", "casepacker", "conv_to_org", "cinn_to_non")
    out: dict = {}
    for pair, mc in machine_changes.items():
        f_from = sku_family.get(pair[0])
        f_to = sku_family.get(pair[1])
        if f_from is not None and f_to is not None and f_from == f_to:
            compressed = dict(mc)
            for k in zero_keys:
                compressed[k] = 0
            compressed["added_flavors"] = 0
            out[pair] = compressed
        else:
            out[pair] = dict(mc)
    return out
