# code/helpers/cip_import.py — CIP schedule + limits from cip_info.csv.
#
# cip_info.csv (UTF-8 BOM, comma): ID,LineEquipment,PreviousCIP,
#   MaxHoursBetweenCIP,ScheduledCIP,Notes
# 'NULL' strings mean empty. Used both to DRAW scheduled CIPs on the calendar
# and to DRIVE the per-line CIP interval limit (MaxHoursBetweenCIP).

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class CipInfo:
    line: str                       # P09
    previous_cip: pd.Timestamp | None
    max_hours_between: float
    scheduled_cip: pd.Timestamp | None
    notes: str


@dataclass
class CipInfoResult:
    by_line: dict[str, CipInfo] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _ts(v) -> pd.Timestamp | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.upper() == "NULL":
        return None
    return pd.to_datetime(s, errors="coerce")


def read_cip_info(path: str | Path) -> CipInfoResult:
    res = CipInfoResult()
    p = Path(path)
    if not p.exists():
        res.warnings.append(f"missing: {p.name}")
        return res
    df = pd.read_csv(p, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    for _, r in df.iterrows():
        line = str(r.get("LineEquipment", "")).strip()
        if not line:
            continue
        try:
            max_h = float(str(r.get("MaxHoursBetweenCIP", "")).strip() or 0)
        except ValueError:
            max_h = 0.0
        res.by_line[line] = CipInfo(
            line=line,
            previous_cip=_ts(r.get("PreviousCIP")),
            max_hours_between=max_h,
            scheduled_cip=_ts(r.get("ScheduledCIP")),
            notes=str(r.get("Notes", "")).strip(),
        )
    return res
