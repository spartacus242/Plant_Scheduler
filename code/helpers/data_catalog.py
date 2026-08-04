# helpers/data_catalog.py -- registry of the app's input CSV files.
#
# One place that names every CSV Flowstate reads, what it means in plain English,
# and which columns must be present for a replacement upload to be accepted.
# Used by pages/home.py (Data status panel) and pages/data.py (upload / edit).

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from helpers.paths import data_dir, reference_dir

# Files whose first line may carry a UTF-8 BOM; read everything as utf-8-sig.
CSV_ENCODING = "utf-8-sig"


@dataclass(frozen=True)
class DataFile:
    key: str
    name: str
    subdir: str  # "" for data/, "reference" for data/reference/
    filename: str
    blurb: str
    key_columns: tuple = field(default_factory=tuple)

    def path(self, dd: Optional[Path] = None) -> Path:
        base = data_dir() if dd is None else Path(dd)
        if self.subdir == "reference":
            return reference_dir(base) / self.filename
        return base / self.filename

    def rel(self) -> str:
        return f"data/{self.subdir}/{self.filename}" if self.subdir else f"data/{self.filename}"


CATALOG: tuple = (
    DataFile(
        key="calendar_blocks",
        name="AZAP schedule (calendar blocks)",
        subdir="",
        filename="calendar_blocks.csv",
        blurb="This week's planned blocks -- what runs on which line, when.",
        key_columns=("block_id", "block_type", "line_id", "start_h", "end_h"),
    ),
    DataFile(
        key="lines",
        name="Lines",
        subdir="",
        filename="lines.csv",
        blurb="The production lines shown as Gantt rows.",
        key_columns=("line_id", "line_name"),
    ),
    DataFile(
        key="capabilities_rates",
        name="Line capabilities + rates",
        subdir="reference",
        filename="capabilities_rates.csv",
        blurb="Which SKUs each line can run, and how fast (kg/hr).",
        key_columns=("line_id", "sku", "capable"),
    ),
    DataFile(
        key="downtimes",
        name="Lines down (downtimes)",
        subdir="reference",
        filename="downtimes.csv",
        blurb="Planned line-down windows: maintenance, contractors, outages.",
        key_columns=("line_id", "start_hour", "end_hour"),
    ),
    DataFile(
        key="demand_plan",
        name="Demand plan",
        subdir="reference",
        filename="demand_plan.csv",
        blurb="Orders to cover this horizon, with target quantities and due windows.",
        key_columns=("order_id", "sku", "qty_target"),
    ),
    DataFile(
        key="changeovers",
        name="Changeovers",
        subdir="reference",
        filename="changeovers.csv",
        blurb="Setup hours and change flags for every SKU-to-SKU transition.",
        key_columns=("from_sku", "to_sku", "setup_hours"),
    ),
    DataFile(
        key="line_cip_hrs",
        name="CIP limits",
        subdir="reference",
        filename="line_cip_hrs.csv",
        blurb="Maximum run hours per line before a CIP is required.",
        key_columns=("line_id", "max_cip_hrs"),
    ),
    DataFile(
        key="initial_states",
        name="Initial line states",
        subdir="reference",
        filename="initial_states.csv",
        blurb="Where each line starts: current SKU, available-from hour, CIP carryover.",
        key_columns=("line_id", "initial_sku", "available_from_hour"),
    ),
    DataFile(
        key="sku_info",
        name="SKU info",
        subdir="reference",
        filename="sku_info.csv",
        blurb="SKU master: description, format, recipe, organic / flavor attributes.",
        key_columns=("sku", "recipe"),
    ),
    DataFile(
        key="trials",
        name="Trials",
        subdir="reference",
        filename="trials.csv",
        blurb="Trial runs that must be protected on the schedule.",
        key_columns=("line_name", "sku", "start_datetime"),
    ),
)


def by_key(key: str) -> Optional[DataFile]:
    for spec in CATALOG:
        if spec.key == key:
            return spec
    return None


def read_csv(spec: DataFile, dd: Optional[Path] = None) -> pd.DataFrame:
    return pd.read_csv(spec.path(dd), encoding=CSV_ENCODING, dtype=str, keep_default_na=False)


def status(spec: DataFile, dd: Optional[Path] = None) -> dict:
    """Existence / size / row-count summary for one catalog entry. Never raises."""
    p = spec.path(dd)
    info = {
        "key": spec.key,
        "name": spec.name,
        "rel": spec.rel(),
        "exists": p.exists(),
        "rows": None,
        "modified": None,
        "error": None,
    }
    if not p.exists():
        return info
    try:
        info["modified"] = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError as exc:
        info["error"] = str(exc)
        return info
    try:
        info["rows"] = int(len(read_csv(spec, dd)))
    except Exception as exc:  # a corrupt CSV should degrade, not crash the page
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def missing_columns(df: pd.DataFrame, spec: DataFile) -> list:
    cols = {str(c).strip().lstrip("\ufeff").lower() for c in df.columns}
    return [c for c in spec.key_columns if c.lower() not in cols]
