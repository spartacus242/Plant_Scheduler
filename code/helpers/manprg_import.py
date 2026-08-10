# code/helpers/manprg_import.py — live MO progress from the manprg reports.
#
# manprg.txt / manprg2.txt are VIF 'manprg' exports refreshed ~every 15 min.
# They are split by line range (manprg = P09-P18, manprg2 = P19-P22) with ZERO
# MO overlap, so merging is a plain union. We keep the two-file logic because
# that is how the report is produced today.
#
# Layout (';' delim, cp1252): Start date;Start time;Line;MO No.;Item;...;
#   Fct qty (Cas);Qty made (Cas);Fct qty [Kg];;Qty made [Kg];;Left (Cas)
#
# Current MO per line = the row with the latest Start datetime that has
# Qty made (Cas) > 0. Completion % = Qty made / Fct qty.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

COLS = [
    "start_date", "start_time", "line", "mo", "item", "designation",
    "pal", "type", "hours", "fct_cas", "made_cas", "fct_kg", "_u1",
    "made_kg", "_u2", "left_cas",
]


@dataclass
class LineProgress:
    line: str                 # P09
    mo: str
    item: str
    designation: str
    completion_pct: float     # 0..100
    made_cas: float
    fct_cas: float
    left_cas: float
    start_dt: pd.Timestamp


@dataclass
class ManprgResult:
    current: dict[str, LineProgress] = field(default_factory=dict)  # line -> current MO
    by_mo: dict[str, LineProgress] = field(default_factory=dict)    # mo -> progress
    rows: int = 0
    warnings: list[str] = field(default_factory=list)
    # Raw merged rows (normalised columns, see COLS + start_dt). Needed by
    # helpers/current_state.py to rebuild the calendar from ground truth;
    # the aggregates above are lossy (one row per line / per MO only).
    frame: pd.DataFrame | None = None


def _read_one(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, delimiter=";", encoding="cp1252", dtype=str,
                     keep_default_na=False)
    df = df.iloc[:, :len(COLS)]
    df.columns = COLS[:df.shape[1]]
    for c in ("hours", "fct_cas", "made_cas", "fct_kg", "made_kg", "left_cas"):
        if c in df:
            df[c] = pd.to_numeric(df[c].str.strip(), errors="coerce")
    df["line"] = df["line"].str.strip().str.replace("LMH-", "", regex=False)
    df["start_dt"] = pd.to_datetime(
        df["start_date"].str.strip() + " " + df["start_time"].str.strip(),
        format="%m/%d/%Y %H:%M", errors="coerce")
    return df


def read_manprg(paths: list[str | Path]) -> ManprgResult:
    """Merge the (two) manprg files and compute per-line / per-MO progress."""
    res = ManprgResult()
    frames = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            res.warnings.append(f"missing: {p.name}")
            continue
        try:
            frames.append(_read_one(p))
        except Exception as exc:  # noqa: BLE001
            res.warnings.append(f"{p.name}: {exc}")
    if not frames:
        res.warnings.append("no manprg data")
        return res

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["mo", "line"], keep="last")
    res.rows = len(df)
    res.frame = df.reset_index(drop=True)

    def _progress(r) -> LineProgress:
        fct = r["fct_cas"] if pd.notna(r["fct_cas"]) else 0.0
        made = r["made_cas"] if pd.notna(r["made_cas"]) else 0.0
        pct = (made / fct * 100.0) if fct > 0 else 0.0
        return LineProgress(
            line=r["line"], mo=r["mo"], item=r["item"],
            designation=str(r["designation"]).strip(),
            completion_pct=round(pct, 1), made_cas=made, fct_cas=fct,
            left_cas=r["left_cas"] if pd.notna(r["left_cas"]) else 0.0,
            start_dt=r["start_dt"])

    for _, r in df.iterrows():
        lp = _progress(r)
        res.by_mo[r["mo"]] = lp

    # current MO per line = latest start with made>0
    active = df[df["made_cas"].fillna(0) > 0]
    for line, grp in active.groupby("line"):
        latest = grp.sort_values("start_dt").iloc[-1]
        res.current[line] = _progress(latest)
    return res
