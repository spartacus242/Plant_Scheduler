# code/stockcheck/vif_import.py — Import VIF text exports into typed frames.
#
# Dialects verified against the real exports (see .hermes/plans/stock-check-design.md):
#   ediact 3.csv / ediact 4.csv : ';', cp1252, header row
#   jestkexp.csv / jestkexp2.csv: ';', cp1252, header row (column order DIFFERS)
#   azapart.csv                 : ';', cp1252, NO header (Column1..Column8)
#   rmpkitems.csv               : ',', cp1252, 7 junk preamble rows, then header
#
# Item codes are STRINGS everywhere ('730009-A', 'BT002'). Never numeric.

from __future__ import annotations

import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

EDIACT_COLS = [
    "PF", "Activity", "flow_type", "effective_date", "family",
    "item_type", "item", "designation", "qty_act", "unit",
]

JESTKEXP_COLS = [  # raw materials
    "item", "designation", "scn", "status", "statut", "bbd",
    "batch", "qty", "unit", "depot", "location",
]

JESTKEXP2_COLS = [  # packaging: Batch/BBD before Status
    "item", "designation", "scn", "batch", "bbd", "status", "statut",
    "qty", "unit", "depot", "location",
]

VIF_FILES = ("ediact 3.csv", "ediact 4.csv", "jestkexp.csv",
             "jestkexp2.csv", "azapart.csv", "rmpkitems.csv")


@dataclass
class VifSnapshot:
    frames: dict[str, pd.DataFrame]
    source_files: dict[str, str]  # filename -> mtime ISO
    imported_at: str
    errors: list[str] = field(default_factory=list)


def _read_semicolon(path: Path, header: int | None = 0) -> pd.DataFrame:
    return pd.read_csv(path, delimiter=";", encoding="cp1252",
                       dtype=str, keep_default_na=False, header=header)


def _num(series: pd.Series) -> pd.Series:
    """VIF numeric column → float. The live export writes thousands with a
    comma ('1,600' cases) which to_numeric refuses — that single quirk made
    8,403 of 15,267 BOM quantities NaN and every explosion UNK (2026-08-14).
    Decimals are dot-separated throughout, so stripping commas is safe."""
    return pd.to_numeric(
        series.str.strip().str.replace(",", "", regex=False).replace("", None),
        errors="coerce")


def _dates(series: pd.Series) -> pd.Series:
    """VIF date column → datetime, auto-detecting %d/%m/%Y vs %m/%d/%Y.

    One live export mixes conventions per column (measured 2026-08-14:
    ediact effective_date is day-first, jestkexp BBD is month-first), so the
    convention is chosen per column: whichever format leaves fewer NaT wins;
    ties keep the legacy day-first."""
    s = series.str.strip()
    dayfirst = pd.to_datetime(s, format="%d/%m/%Y", errors="coerce")
    monthfirst = pd.to_datetime(s, format="%m/%d/%Y", errors="coerce")
    return monthfirst if monthfirst.isna().sum() < dayfirst.isna().sum() else dayfirst


def load_ediact(path: Path) -> pd.DataFrame:
    df = _read_semicolon(path)
    df = df.iloc[:, :10]
    df.columns = EDIACT_COLS
    df["qty_act"] = _num(df["qty_act"])
    df["effective_date"] = _dates(df["effective_date"])
    for c in ("PF", "Activity", "family", "item_type", "item", "unit"):
        df[c] = df[c].str.strip()
    return df


EDIACT4_COLS = [
    # Same export family as ediact 3 but WITHOUT the flow-type column:
    # PF;Activity;Effective date;Famille;Item type;Item;Designation;Qty;(unit);Freinte
    "PF", "Activity", "effective_date", "family",
    "item_type", "item", "designation", "qty_act", "unit",
]


def load_ediact4(path: Path) -> pd.DataFrame:
    """ediact 4 = the semi-finished (HSM) recipes: Activity is the HSM code,
    Input rows its components per batch, the Output row the batch basis
    (e.g. 1,000 kg of slurry). Feeds BomGraph so HSM inputs explode through
    to their sub-components instead of counting as unstocked leaves."""
    df = _read_semicolon(path)
    df = df.iloc[:, :9]
    df.columns = EDIACT4_COLS
    df["qty_act"] = _num(df["qty_act"])
    df["effective_date"] = _dates(df["effective_date"])
    for c in ("PF", "Activity", "family", "item_type", "item", "unit"):
        df[c] = df[c].str.strip()
    return df


def load_jestkexp(path: Path, packaging: bool = False) -> pd.DataFrame:
    df = _read_semicolon(path)
    df = df.iloc[:, :11]
    df.columns = JESTKEXP2_COLS if packaging else JESTKEXP_COLS
    df["qty"] = _num(df["qty"]).fillna(0.0)
    df["bbd"] = _dates(df["bbd"])
    for c in ("item", "status", "unit", "depot", "location", "batch"):
        df[c] = df[c].str.strip()
    return df


def load_azapart(path: Path) -> pd.DataFrame:
    # header=None: the live export has NO header row — reading with a header
    # silently ate the first SKU (030480). The dev fixture's "Column1;..."
    # junk header becomes a data row instead, dropped by the filter below.
    df = _read_semicolon(path, header=None)
    df = df.iloc[:, :8]
    df.columns = ["sku", "description", "cas_unit", "kg_unit",
                  "cas_conv", "cnt_conv", "kg_per_case", "format"]
    df = df[df["sku"].str.strip().ne("") & df["sku"].ne("Column1")]
    df["sku"] = df["sku"].str.strip()
    df["kg_per_case"] = pd.to_numeric(df["kg_per_case"].str.strip(),
                                      errors="coerce")
    df["format"] = df["format"].str.strip()
    # duplicate sku rows exist (one with blank format): prefer non-blank format
    df = (df.sort_values("format", key=lambda s: s.str.len())
            .groupby("sku", as_index=False).last())
    return df.reset_index(drop=True)


def load_rmpkitems(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, delimiter=",", encoding="cp1252", dtype=str,
                     keep_default_na=False, skiprows=7)
    df = df.iloc[:, :2]
    df.columns = ["item", "designation"]
    df["item"] = df["item"].str.strip()
    df["designation"] = df["designation"].str.strip()
    return df[df["item"].ne("")].reset_index(drop=True)


def import_vif_folder(folder: str | Path) -> VifSnapshot:
    """Load all VIF exports from a folder. Missing/corrupt files are
    reported in .errors and simply absent from .frames (never fatal)."""
    folder = Path(folder)
    frames: dict[str, pd.DataFrame] = {}
    mtimes: dict[str, str] = {}
    errors: list[str] = []
    loaders = {
        "ediact 3.csv": lambda p: load_ediact(p),
        "ediact 4.csv": lambda p: load_ediact4(p),
        "jestkexp.csv": lambda p: load_jestkexp(p, packaging=False),
        "jestkexp2.csv": lambda p: load_jestkexp(p, packaging=True),
        "azapart.csv": load_azapart,
        "rmpkitems.csv": load_rmpkitems,
    }
    for name, loader in loaders.items():
        p = folder / name
        try:
            frames[name] = loader(p)
            mtimes[name] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
        except Exception as exc:  # noqa: BLE001 - import must never die
            errors.append(f"{name}: {exc}")
    return VifSnapshot(frames=frames, source_files=mtimes,
                       imported_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                       errors=errors)


def save_snapshot(snap: VifSnapshot, snapshots_dir: str | Path) -> Path:
    """Versioned snapshot; writes then moves a 'latest' pointer. A failed
    import (empty frames) never clobbers last-good."""
    if not snap.frames:
        raise ValueError("refusing to save empty snapshot")
    d = Path(snapshots_dir) / snap.imported_at.replace(":", "-").replace(" ", "_")
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "snapshot.pkl", "wb") as f:
        pickle.dump(snap, f)
    (Path(snapshots_dir) / "latest").write_text(d.name, encoding="utf-8")
    return d


def load_latest_snapshot(snapshots_dir: str | Path) -> VifSnapshot | None:
    d = Path(snapshots_dir)
    ptr = d / "latest"
    if not ptr.exists():
        return None
    pkl = d / ptr.read_text(encoding="utf-8").strip() / "snapshot.pkl"
    if not pkl.exists():
        return None
    with open(pkl, "rb") as f:
        return pickle.load(f)
