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
#
# AS-OF (audit C01, 2026-09-03): the export carries NO observation timestamp,
# and its 'Qty made' counters are batch-posted, so "cases made / hours since
# start" is only honest when the hours are measured up to the moment the
# CONTENT was observed — not the moment a page happens to render. The bridge
# (scripts/fs-live-pull.py) stamps data/reference/manprg.asof.json whenever
# the manprg content changes; `read_manprg` exposes that as `as_of` (falling
# back to the newest file mtime when no stamp exists or its sha no longer
# matches the files).

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

ASOF_STAMP_NAME = "manprg.asof.json"

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
    # When the manprg CONTENT was observed (C01): the bridge's as-of stamp,
    # else the newest file mtime. None only when no file could be read.
    as_of: pd.Timestamp | None = None
    as_of_source: str = ""      # "stamp" | "mtime" | ""


def content_sha256(paths: list[str | Path]) -> str:
    """sha256 over the raw bytes of the existing files, in the given order.

    Mirrored VERBATIM in scripts/fs-live-pull.py (the bridge may run without
    the repo on sys.path); keep both in step.
    """
    h = hashlib.sha256()
    for p in paths:
        p = Path(p)
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def read_asof_stamp(paths: list[str | Path],
                    stamp_path: str | Path | None = None,
                    ) -> tuple[pd.Timestamp | None, str, list[str]]:
    """(as_of, source, warnings) for the manprg files.

    The stamp (next to the first existing file unless `stamp_path` is given)
    is trusted only when its sha256 matches the files' current content — a
    file replaced by hand or by another tool must not inherit an old stamp.
    Without a usable stamp the newest file mtime is the observation time.
    """
    warnings: list[str] = []
    existing = [Path(p) for p in paths if Path(p).is_file()]
    if not existing:
        return None, "", warnings
    sp = Path(stamp_path) if stamp_path else existing[0].parent / ASOF_STAMP_NAME
    if sp.is_file():
        try:
            meta = json.loads(sp.read_text(encoding="utf-8"))
            ts = pd.Timestamp(str(meta.get("as_of") or ""))
            if pd.isna(ts):
                raise ValueError("as_of missing")
            if ts.tzinfo is not None:
                # local wall-clock, like every other timestamp in the app
                local_tz = datetime.now().astimezone().tzinfo
                ts = ts.tz_convert(local_tz).tz_localize(None)
            sha = str(meta.get("sha256") or "")
            if sha and sha != content_sha256(paths):
                warnings.append(
                    f"{sp.name}: sha256 does not match the manprg files — "
                    "content changed outside the bridge; using file mtime "
                    "as the observation time")
            else:
                return ts, "stamp", warnings
        except (OSError, ValueError, TypeError) as exc:
            warnings.append(f"{sp.name} unreadable ({exc}); using file mtime")
    mtime = max(p.stat().st_mtime for p in existing)
    return pd.Timestamp(datetime.fromtimestamp(mtime)), "mtime", warnings


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
    res.as_of, res.as_of_source, asof_warn = read_asof_stamp(list(paths))
    res.warnings.extend(asof_warn)

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
