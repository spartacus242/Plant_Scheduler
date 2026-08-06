# code/helpers/azap_import.py — Import the raw corporate AZAP export (.xlsx)
# into Flowstate's demand_plan.csv.
#
# Source layout (verified against the real 'Demand Plan Raw.xlsx'):
#   sheet 'pdp export AZAP n20', 11 columns:
#     Year/Month/Day Start  -> week bucket start (ALWAYS a Monday)
#     Machine               -> line P02..P22 (we keep only P09..P22)
#     Product               -> SKU
#     Tons                  -> metric tons for the week (x1000 = kg)
#     gen_idtypemaille      -> constant 4 (weekly granularity)
#     Year/Month/Day End    -> bucket end (always Start + 7 days)
#     Hours                 -> corporate's OWN run-hours estimate (IGNORED:
#                              Flowstate computes hours from calc_rate_kgph)
#
# Import semantics (approved):
#   * anchor  := earliest week_start in the file  -> new planning_start_date
#   * trim    := keep anchor .. anchor+2 weeks (3-week rolling window); the
#                rest is volatile corporate noise and is dropped
#   * output  := data/reference/demand_plan.csv (EXISTING schema, so the
#                solver / naive baseline / scorecard / Stock Check are
#                untouched) + demand_plan.source.json provenance sidecar

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

KEEP_MACHINES = {f"P{n:02d}" for n in range(9, 23)}  # P09..P22
WINDOW_WEEKS = 3

EXPECTED_COLS = [
    "Year Start", "Month Start", "Day Start", "Machine", "Product", "Tons",
    "gen_idtypemaille", "Year End", "Month End", "Day End", "Hours",
]


@dataclass
class AzapImportResult:
    anchor: date
    iso_weeks: list[int]              # ISO week numbers kept (len == weeks kept)
    rows_kept: int
    rows_dropped_window: int
    rows_dropped_machine: int
    buckets: int                      # distinct (sku, week) after aggregation
    total_kg: float
    warnings: list[str] = field(default_factory=list)
    demand_csv: Path | None = None
    provenance_json: Path | None = None


def read_raw(path: str | Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0, dtype={
        "Year Start": int, "Month Start": int, "Day Start": int,
        "Machine": str, "Product": str, "Tons": float,
        "Year End": int, "Month End": int, "Day End": int, "Hours": float,
    })
    missing = [c for c in EXPECTED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"AZAP export missing columns: {missing}")
    return df


def _week_starts(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(
        dict(year=df["Year Start"], month=df["Month Start"],
             day=df["Day Start"])).dt.date


def aggregate(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str], int, int]:
    """Filter machines, aggregate to (sku, week_start) kg. Returns
    (agg_df, warnings, rows_dropped_machine, rows_bad)."""
    warns: list[str] = []
    df = df.copy()
    df["Machine"] = df["Machine"].str.strip().str.upper()
    df["Product"] = df["Product"].str.strip()
    n0 = len(df)
    df = df[df["Machine"].isin(KEEP_MACHINES)]
    dropped_machine = n0 - len(df)
    df["week_start"] = _week_starts(df)
    df["week_end"] = pd.to_datetime(
        dict(year=df["Year End"], month=df["Month End"],
             day=df["Day End"])).dt.date

    non_monday = int((pd.to_datetime(df["week_start"]).dt.weekday != 0).sum())
    if non_monday:
        warns.append(f"{non_monday} rows have a non-Monday start (kept).")
    span_days = (pd.to_datetime(df["week_end"])
                 - pd.to_datetime(df["week_start"])).dt.days
    # verified: corporate End is the FOLLOWING Monday (exclusive) -> 7 days
    bad_span = int((span_days != 7).sum())
    if bad_span:
        warns.append(f"{bad_span} rows have end != start+7d (kept).")

    df["kg"] = df["Tons"].astype(float) * 1000.0
    agg = (df.groupby(["Product", "week_start"], as_index=False)["kg"].sum()
             .rename(columns={"Product": "sku"}))
    return agg, warns, dropped_machine, 0


def build_demand_plan(agg: pd.DataFrame) -> tuple[pd.DataFrame, date, int]:
    """Anchor = earliest week; keep anchor..anchor+2 weeks. Returns
    (demand_df, anchor, rows_dropped_window)."""
    anchor = min(agg["week_start"])
    last = anchor + timedelta(weeks=WINDOW_WEEKS - 1)
    in_window = agg[agg["week_start"] <= last]
    dropped = int((~agg["week_start"].isin(in_window["week_start"])).sum())
    out = in_window.copy()
    out["week_index"] = out["week_start"].apply(
        lambda d: (d - anchor).days // 7)
    out = out.sort_values(["week_index", "sku"]).reset_index(drop=True)
    out["order_id"] = out["sku"] + "-W" + out["week_index"].astype(str)
    out["qty_target"] = out["kg"].round(0).astype(int)
    out["lower_pct"] = 0.9
    out["upper_pct"] = 1.1
    out["due_start_hour"] = out["week_index"] * 168
    out["due_end_hour"] = out["week_index"] * 168 + 167
    out["priority"] = 3
    week_starts = sorted(out["week_start"].unique())
    demand = out[["order_id", "sku", "week_index", "qty_target", "lower_pct",
                  "upper_pct", "due_start_hour", "due_end_hour", "priority"]]
    return demand, anchor, dropped, week_starts


def iso_week(d: date) -> int:
    return d.isocalendar()[1]


def import_azap(path: str | Path, reference_dir: str | Path,
                update_anchor=None) -> AzapImportResult:
    """Full import: read -> aggregate -> trim -> write csv + provenance.
    update_anchor: optional callable(anchor_date_str) to persist the new
    planning_start_date (kept out of here so this stays pure)."""
    reference_dir = Path(reference_dir)
    df = read_raw(path)
    agg, warns, dropped_machine, _ = aggregate(df)
    demand, anchor, dropped_window, weeks = build_demand_plan(agg)

    res = AzapImportResult(
        anchor=anchor,
        iso_weeks=[iso_week(w) for w in weeks],
        rows_kept=len(demand),
        rows_dropped_window=dropped_window,
        rows_dropped_machine=dropped_machine,
        buckets=len(demand),
        total_kg=float(demand["qty_target"].sum()),
        warnings=warns,
    )

    csv_path = reference_dir / "demand_plan.csv"
    demand.to_csv(csv_path, index=False)
    res.demand_csv = csv_path

    prov = {
        "source_file": str(path),
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "anchor_monday": anchor.isoformat(),
        "iso_weeks": {f"W{i}": iso_week(w) for i, w in enumerate(weeks)},
        "rows_kept": res.rows_kept,
        "rows_dropped_window": res.rows_dropped_window,
        "rows_dropped_machine": res.rows_dropped_machine,
        "total_kg": res.total_kg,
        "warnings": res.warnings,
    }
    prov_path = reference_dir / "demand_plan.source.json"
    prov_path.write_text(json.dumps(prov, indent=2), encoding="utf-8")
    res.provenance_json = prov_path

    if update_anchor is not None:
        update_anchor(anchor.strftime("%Y-%m-%d 00:00:00"))
    return res
