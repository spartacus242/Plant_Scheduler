# scripts/ingest_historical_run_log.py
#
# Parses the plant's historical run log (one row per MO x line x SKU, 2018+)
# into an auditable clean table for the optimizer to read.
#
# Principles
#   * Nothing is dropped. Every source row is kept with its source line number,
#     its raw values, and a list of named flags. `usable_default` says whether
#     the row should feed rate/OEE learning; the reader may override.
#   * No thresholds live in this file. Everything tunable is in
#     data/reference/historical/historical_run_log.rules.toml.
#   * Interpretation (confirmed with the plant 2026-09-08):
#       - mo_start_date is an Excel serial date (1900 system).
#       - mo_scheduled_hrs is wall-clock line occupancy on a 24 h day, but the
#         MO start time-of-day is not recorded. Start/end times are therefore
#         ESTIMATES: an MO starts at midnight unless the previous MO on the
#         same line is estimated to end later that same day, in which case it
#         chains from that end (`start_time_source` records which).
#       - OEE = actual output / (rate x actual hours). Values above 1.0 are
#         keying errors and are excluded by default.
#       - The same MO number on two lines is genuine (tonnage moved between
#         lines). The same MO number with two SKUs is a keying error; the run
#         itself is still real, only the MO key is suspect.
#   * If the source carries a quantity column (name set in the rules file), a
#     measured kg/h is derived; otherwise those columns are left empty.
#
# Run:  python scripts/ingest_historical_run_log.py --src <raw.csv> [--copy-raw]
from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "reference" / "historical"
RAW_NAME = "npa_historical_run_log.raw.csv"
EXCEL_ORIGIN = "1899-12-30"


def parse(src: Path, rules: dict, repo: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    T = rules["thresholds"]
    raw = pd.read_csv(src, encoding="utf-8-sig", dtype=str)
    raw.columns = [c.strip() for c in raw.columns]

    def col(key: str, required: bool = True) -> pd.Series | None:
        for alias in rules["columns"][key]:
            if alias in raw.columns:
                return raw[alias]
        if required:
            raise SystemExit(f"{src.name}: none of {rules['columns'][key]} found for '{key}'; headers = {list(raw.columns)}")
        return None

    num = lambda s: pd.to_numeric(s, errors="coerce") if s is not None else pd.Series(np.nan, index=raw.index)

    df = pd.DataFrame({"source_row": np.arange(2, len(raw) + 2)})  # row 1 = header
    df["mo_start_serial"] = num(col("mo_start_date"))
    df["mo_start_date"] = pd.to_datetime(df["mo_start_serial"], unit="D", origin=EXCEL_ORIGIN).astype("datetime64[ns]")
    iso = df["mo_start_date"].dt.isocalendar()
    df["iso_year"], df["iso_week"] = iso.year, iso.week
    df["weekday"] = df["mo_start_date"].dt.day_name().str[:3]
    df["line"] = col("line").str.strip().str.upper()
    df["is_double_line"] = df["line"].isin(rules["lines"]["double"])
    df["mo"] = num(col("mo")).astype("Int64")
    df["sku"] = num(col("sku")).astype("Int64")
    df["oee_raw"] = col("oee").str.strip()
    df["oee"] = num(df["oee_raw"])
    df["mo_scheduled_hrs"] = num(col("hrs"))

    # Quantities (optional columns). kg = pouches x pouch weight; measured rate
    # is derived here, the source's own kg/h is kept only to audit against.
    df["pouches_produced"] = num(col("pouches", required=False))
    df["pouch_weight_g"] = num(col("pouch_weight_g", required=False))
    df["source_kg_per_hr"] = num(col("kg_per_hr", required=False))
    df["qty_kg"] = df["pouches_produced"] * df["pouch_weight_g"] / 1000.0
    df["measured_rate_kgph"] = df["qty_kg"] / df["mo_scheduled_hrs"].replace(0, np.nan)
    NR = rules["nominal_rate_kgph"]
    by_g = {float(k): float(v) for k, v in NR["single_by_pouch_g"].items() if k != "default"}
    single = df["pouch_weight_g"].map(by_g).fillna(float(NR["single_by_pouch_g"]["default"]))
    df["nominal_rate_kgph"] = np.where(df["is_double_line"], single * float(NR["double_multiplier"]), single)
    df["oee_from_rate"] = df["measured_rate_kgph"] / df["nominal_rate_kgph"]

    # --- estimated start/end clock, chained per line -------------------------
    df = df.sort_values(["line", "mo_start_serial", "source_row"]).reset_index(drop=True)
    start_est = df["mo_start_date"].copy()
    source = pd.Series("midnight", index=df.index)
    dur = pd.to_timedelta((df["mo_scheduled_hrs"].fillna(0) * 60).round(), unit="m")
    for _, idx in df.groupby("line").groups.items():
        prev_end = None
        for i in idx:
            s = df.at[i, "mo_start_date"]
            if prev_end is not None and prev_end > s and prev_end.normalize() == s:
                start_est.at[i] = prev_end
                source.at[i] = "chained_from_prev_mo"
            prev_end = start_est.at[i] + dur.at[i]
    df["start_est"] = start_est
    df["start_time_source"] = source
    df["end_est"] = df["start_est"] + dur
    nxt_start = df.groupby("line")["mo_start_date"].shift(-1)
    df["gap_to_next_start_days"] = (nxt_start - df["mo_start_date"]).dt.days
    df["prev_sku_same_line"] = df.groupby("line")["sku"].shift(1)

    # --- reference cross-checks (informational only) ------------------------
    sku_master = pd.read_csv(repo / "data/reference/sku_info.csv")
    cap = pd.read_csv(repo / "data/reference/capabilities_rates.csv")
    cap["line_group"] = cap["line_name"].str.replace(r"[AB]$", "", regex=True)
    capable = set(zip(cap.loc[cap.capable == 1, "line_group"], cap.loc[cap.capable == 1, "sku"]))
    known = set(zip(cap["line_group"], cap["sku"]))
    df["sku_in_master"] = df["sku"].isin(set(sku_master["sku"]))
    pairs = list(zip(df["line"], df["sku"]))
    df["pair_in_capability_table"] = [p in known for p in pairs]
    df["pair_flagged_capable"] = [p in capable for p in pairs]

    # --- MO multiplicity and sequence sanity --------------------------------
    g = df.groupby("mo").agg(mo_rows=("sku", "size"), mo_n_sku=("sku", "nunique"), mo_n_line=("line", "nunique"))
    df = df.join(g, on="mo")
    by_src = df.sort_values("source_row")
    med = by_src["mo"].astype(float).rolling(T["mo_seq_window"], center=True, min_periods=3).median()
    df["mo_seq_deviation"] = (by_src["mo"].astype(float) - med).abs().reindex(df.index)

    review = {
        "date_unparsed": df["mo_start_serial"].isna(),
        "oee_not_numeric": df["oee"].isna(),
        "oee_zero": df["oee"] == 0,
        "oee_low": (df["oee"] > 0) & (df["oee"] < T["oee_low"]),
        "oee_gt_1": df["oee"] > 1.0,
        "hrs_zero": df["mo_scheduled_hrs"] <= 0,
        "hrs_long": df["mo_scheduled_hrs"] > T["hrs_long"],
        "hrs_overlaps_next_mo": (df["mo_scheduled_hrs"] / 24.0) > (df["gap_to_next_start_days"] + T["overlap_tolerance_days"]),
        "mo_multi_sku_keying_error": df["mo_n_sku"] > 1,
        "mo_out_of_sequence": df["mo_seq_deviation"] > T["mo_seq_deviation"],
        "dup_date_line_sku": df.duplicated(["mo_start_serial", "line", "sku"], keep=False),
        "pouches_zero": df["pouches_produced"] <= 0,
        "pouch_weight_missing": df["pouches_produced"].notna() & df["pouch_weight_g"].isna(),
        "rate_mismatch_vs_source": (df["measured_rate_kgph"] - df["source_kg_per_hr"]).abs() > T["rate_mismatch_kgph"],
        "oee_inconsistent_with_rate": ((df["oee"] - df["oee_from_rate"]).abs() / df["oee"].replace(0, np.nan)) > T["oee_rate_mismatch_ratio"],
    }
    info = {
        "mo_split_across_lines": df["mo_n_line"] > 1,
        "sku_not_in_master": ~df["sku_in_master"],
        "pair_not_in_capability_table": ~df["pair_in_capability_table"],
        "pair_not_flagged_capable": df["pair_in_capability_table"] & ~df["pair_flagged_capable"],
    }
    rv = pd.DataFrame(review).fillna(False).astype(bool)
    inf = pd.DataFrame(info).fillna(False).astype(bool)
    df["review_flags"] = rv.apply(lambda r: "|".join(c for c in rv.columns if r[c]), axis=1)
    df["info_flags"] = inf.apply(lambda r: "|".join(c for c in inf.columns if r[c]), axis=1)
    df["n_review_flags"] = rv.sum(axis=1)
    unusable = [f for f in rules["usability"]["unusable_flags"] if f in rv.columns]
    df["usable_default"] = ~rv[unusable].any(axis=1)

    cols = [
        "source_row", "mo_start_date", "iso_year", "iso_week", "weekday", "line", "is_double_line",
        "mo", "sku", "oee", "mo_scheduled_hrs",
        "pouches_produced", "pouch_weight_g", "qty_kg", "measured_rate_kgph", "nominal_rate_kgph", "oee_from_rate",
        "start_est", "end_est", "start_time_source", "gap_to_next_start_days", "prev_sku_same_line",
        "sku_in_master", "pair_in_capability_table", "pair_flagged_capable",
        "mo_rows", "mo_n_sku", "mo_n_line",
        "usable_default", "n_review_flags", "review_flags", "info_flags",
        "mo_start_serial", "oee_raw", "source_kg_per_hr",
    ]
    out = df.sort_values("source_row")[cols].copy()
    out["mo_start_date"] = out["mo_start_date"].dt.strftime("%Y-%m-%d")
    for c in ("qty_kg", "measured_rate_kgph", "oee_from_rate"):
        out[c] = out[c].round(4)
    for c in ("start_est", "end_est"):
        out[c] = out[c].dt.strftime("%Y-%m-%d %H:%M")
    return out, rv, inf


def summary_md(src: Path, out: pd.DataFrame, rv: pd.DataFrame, inf: pd.DataFrame, rules: dict) -> str:
    L = [
        "# Historical run log parse summary", "",
        f"- source: `{src.name}`  rows: {len(out)}",
        f"- date range: {out.mo_start_date.min()} .. {out.mo_start_date.max()}",
        f"- usable_default: {int(out.usable_default.sum())}  "
        f"(unusable when any of: {', '.join(rules['usability']['unusable_flags'])})",
        f"- rows with a review flag: {int((out.n_review_flags > 0).sum())}",
        "", "## Review flags (possible data errors)", "", "| flag | rows |", "|---|---|",
    ]
    L += [f"| {c} | {int(rv[c].sum())} |" for c in rv.columns]
    L += ["", "## Info flags (not errors)", "", "| flag | rows |", "|---|---|"]
    L += [f"| {c} | {int(inf[c].sum())} |" for c in inf.columns]
    L += ["", "## Rows per year", "", "| iso_year | rows | usable |", "|---|---|---|"]
    yr = out.groupby("iso_year").agg(rows=("source_row", "size"), usable=("usable_default", "sum"))
    L += [f"| {y} | {r.rows} | {int(r.usable)} |" for y, r in yr.iterrows()]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=OUT_DIR / RAW_NAME, help="raw run-log CSV")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--rules", type=Path, default=None, help="rules TOML (default: <out-dir>/historical_run_log.rules.toml)")
    ap.add_argument("--copy-raw", action="store_true", help=f"copy --src into <out-dir>/{RAW_NAME} for provenance")
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    rules_path = a.rules or a.out_dir / "historical_run_log.rules.toml"
    rules = tomllib.loads(rules_path.read_text(encoding="utf-8"))
    if a.copy_raw and a.src.resolve() != (a.out_dir / RAW_NAME).resolve():
        shutil.copyfile(a.src, a.out_dir / RAW_NAME)

    out, rv, inf = parse(a.src, rules, ROOT)
    out.to_csv(a.out_dir / "historical_run_log_clean.csv", index=False)
    out[out["n_review_flags"] > 0].to_csv(a.out_dir / "historical_run_log_review.csv", index=False)
    md = summary_md(a.src, out, rv, inf, rules)
    (a.out_dir / "historical_run_log_summary.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
