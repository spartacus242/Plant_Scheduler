# scripts/derive_historical_tables.py
#
# Builds the optimizer-facing tables from the clean historical run log.
# Reads ONLY data/reference/historical/historical_run_log_clean.csv and the
# [derive] section of historical_run_log.rules.toml; every number in the
# outputs traces back to those two files (plus capabilities_rates.csv and
# changeovers.csv, which are joined for comparison columns only).
#
# Outputs (data/reference/historical/):
#   rates_by_line_sku.csv   measured kg/h per line x SKU (all-time and recent), vs the model's calc_rate_kgph
#   rates_by_line.csv       line-level fallback
#   rates_by_sku.csv        SKU-level fallback (across lines, single-line equivalent)
#   campaign_norms.csv      run-length norms per SKU (hours and kg per run)
#   line_sku_affinity.csv   share of each SKU's hours by line (how the plant actually assigns SKUs)
#   transitions.csv         observed SKU-to-SKU transitions per line, with changeover flags where known
#   weekly_line_hours.csv   scheduled hours, kg and MO count per line per ISO week
#   derived_summary.md      what was built, from what window, with row counts
#
# Run: python scripts/derive_historical_tables.py
from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "data" / "reference" / "historical"
REF = ROOT / "data" / "reference"


def wquantile(values: pd.Series, weights: pd.Series, q: float) -> float:
    v, w = values.to_numpy(float), weights.to_numpy(float)
    ok = np.isfinite(v) & np.isfinite(w) & (w > 0)
    if not ok.any():
        return np.nan
    order = np.argsort(v[ok])
    v, w = v[ok][order], w[ok][order]
    cw = np.cumsum(w) - 0.5 * w
    return float(np.interp(q * w.sum(), cw, v))


def rate_stats(g: pd.DataFrame, D: dict, prefix: str) -> dict:
    hrs, kg = g["mo_scheduled_hrs"], g["qty_kg"]
    return {
        f"{prefix}n_runs": len(g),
        f"{prefix}hrs": round(hrs.sum(), 1),
        f"{prefix}kg": round(kg.sum(), 0),
        f"{prefix}rate_kgph": round(kg.sum() / hrs.sum(), 1) if hrs.sum() > 0 else np.nan,
        f"{prefix}rate_p{int(D['rate_low_pct']*100)}": round(wquantile(g["measured_rate_kgph"], hrs, D["rate_low_pct"]), 1),
        f"{prefix}rate_p{int(D['rate_high_pct']*100)}": round(wquantile(g["measured_rate_kgph"], hrs, D["rate_high_pct"]), 1),
        f"{prefix}oee_mean_hw": round(float(np.average(g["oee"], weights=hrs)), 4) if hrs.sum() > 0 else np.nan,
    }


def build(D: dict) -> dict[str, pd.DataFrame]:
    clean = pd.read_csv(HIST / "historical_run_log_clean.csv", parse_dates=["mo_start_date", "start_est", "end_est"])
    last = clean["mo_start_date"].max()
    recent_from = last - pd.DateOffset(years=int(D["recent_years"]))
    u = clean[clean["usable_default"] & (clean["mo_scheduled_hrs"] >= D["min_run_hrs"]) & clean["qty_kg"].notna()].copy()
    u["recent"] = u["mo_start_date"] >= recent_from
    out: dict[str, pd.DataFrame] = {}

    cap = pd.read_csv(REF / "capabilities_rates.csv")
    cap["line_group"] = cap["line_name"].str.replace(r"[AB]$", "", regex=True)
    cap = cap.groupby(["line_group", "sku"], as_index=False).agg(capable=("capable", "max"), calc_rate_kgph=("calc_rate_kgph", "sum"))
    cap = cap.rename(columns={"line_group": "line"})

    # --- rates by line x sku ------------------------------------------------
    rows = []
    for (line, sku), g in u.groupby(["line", "sku"]):
        r = {"line": line, "sku": sku, "is_double_line": bool(g["is_double_line"].iloc[0]),
             "first_run": g["mo_start_date"].min().date(), "last_run": g["mo_start_date"].max().date()}
        r.update(rate_stats(g, D, ""))
        gr = g[g["recent"]]
        r.update(rate_stats(gr, D, "recent_") if len(gr) else {f"recent_{k}": np.nan for k in ["n_runs", "hrs", "kg", "rate_kgph",
                 f"rate_p{int(D['rate_low_pct']*100)}", f"rate_p{int(D['rate_high_pct']*100)}", "oee_mean_hw"]})
        r["low_evidence"] = len(g) < D["min_runs_for_rate"]
        rows.append(r)
    rls = pd.DataFrame(rows).merge(cap, on=["line", "sku"], how="left")
    rls["in_capability_table"] = rls["calc_rate_kgph"].notna()
    rls["measured_over_calc"] = (rls["rate_kgph"] / rls["calc_rate_kgph"]).round(3)
    rls["recent_over_calc"] = (rls["recent_rate_kgph"] / rls["calc_rate_kgph"]).round(3)
    out["rates_by_line_sku"] = rls.sort_values(["line", "sku"])

    # --- fallbacks ----------------------------------------------------------
    rows = []
    for line, g in u.groupby("line"):
        r = {"line": line, "is_double_line": bool(g["is_double_line"].iloc[0]), "n_skus": g["sku"].nunique()}
        r.update(rate_stats(g, D, "")); r.update(rate_stats(g[g["recent"]], D, "recent_"))
        rows.append(r)
    out["rates_by_line"] = pd.DataFrame(rows)

    # SKU-level: express double-line runs as single-line equivalent so the number is comparable.
    us = u.copy()
    us["mo_scheduled_hrs"] = np.where(us["is_double_line"], us["mo_scheduled_hrs"] * 2.0, us["mo_scheduled_hrs"])
    us["measured_rate_kgph"] = us["qty_kg"] / us["mo_scheduled_hrs"]
    rows = []
    for sku, g in us.groupby("sku"):
        r = {"sku": sku, "n_lines": g["line"].nunique(), "lines": "|".join(sorted(g["line"].unique())),
             "pouch_weight_g": g["pouch_weight_g"].mode().iat[0] if g["pouch_weight_g"].notna().any() else np.nan,
             "first_run": g["mo_start_date"].min().date(), "last_run": g["mo_start_date"].max().date()}
        r.update(rate_stats(g, D, "single_equiv_")); r.update(rate_stats(g[g["recent"]], D, "recent_single_equiv_"))
        rows.append(r)
    out["rates_by_sku"] = pd.DataFrame(rows)

    # --- campaign norms (hours and kg per run) --------------------------------
    lo, hi = D["campaign_low_pct"], D["campaign_high_pct"]
    rows = []
    for sku, g in u.groupby("sku"):
        gr = g[g["recent"]]
        rows.append({
            "sku": sku, "n_runs": len(g),
            "run_hrs_median": round(g["mo_scheduled_hrs"].median(), 1),
            f"run_hrs_p{int(lo*100)}": round(g["mo_scheduled_hrs"].quantile(lo), 1),
            f"run_hrs_p{int(hi*100)}": round(g["mo_scheduled_hrs"].quantile(hi), 1),
            "run_hrs_max": round(g["mo_scheduled_hrs"].max(), 1),
            "run_kg_median": round(g["qty_kg"].median(), 0),
            f"run_kg_p{int(lo*100)}": round(g["qty_kg"].quantile(lo), 0),
            f"run_kg_p{int(hi*100)}": round(g["qty_kg"].quantile(hi), 0),
            "runs_per_year_recent": round(len(gr) / D["recent_years"], 1),
            "recent_n_runs": len(gr),
            "recent_run_hrs_median": round(gr["mo_scheduled_hrs"].median(), 1) if len(gr) else np.nan,
            "recent_run_kg_median": round(gr["qty_kg"].median(), 0) if len(gr) else np.nan,
        })
    out["campaign_norms"] = pd.DataFrame(rows)

    # --- line affinity: share of each SKU's hours by line --------------------
    aff = u.groupby(["sku", "line"]).agg(hrs=("mo_scheduled_hrs", "sum"), n_runs=("mo", "size")).reset_index()
    aff["share_of_sku_hrs"] = (aff["hrs"] / aff.groupby("sku")["hrs"].transform("sum")).round(3)
    ra = u[u["recent"]].groupby(["sku", "line"]).agg(recent_hrs=("mo_scheduled_hrs", "sum"), recent_n_runs=("mo", "size")).reset_index()
    ra["recent_share_of_sku_hrs"] = (ra["recent_hrs"] / ra.groupby("sku")["recent_hrs"].transform("sum")).round(3)
    aff = aff.merge(ra, on=["sku", "line"], how="left")
    aff["rank_by_recent_hrs"] = aff.groupby("sku")["recent_hrs"].rank(ascending=False, method="first")
    out["line_sku_affinity"] = aff.sort_values(["sku", "recent_hrs"], ascending=[True, False])

    # --- transitions per line -----------------------------------------------
    s = clean[clean["usable_default"]].sort_values(["line", "start_est", "source_row"]).copy()
    s["next_sku"] = s.groupby("line")["sku"].shift(-1)
    s["next_start"] = s.groupby("line")["start_est"].shift(-1)
    s["next_date"] = s.groupby("line")["mo_start_date"].shift(-1)
    s["next_row"] = s.groupby("line")["source_row"].shift(-1)
    t = s.dropna(subset=["next_sku"]).copy()
    t = t[(t["next_date"] - t["mo_start_date"]).dt.days <= D["transition_max_gap_days"]]
    t = t[t["sku"] != t["next_sku"]]
    # NOTE: changeover DURATION cannot be measured from this log. MO start
    # time-of-day is not recorded, so the estimated gap between consecutive
    # runs is ~0 by construction. Only transition counts are reported.
    t["recent"] = t["mo_start_date"] >= recent_from
    tr = t.groupby(["line", "sku", "next_sku"]).agg(
        n=("mo", "size"), recent_n=("recent", "sum"), last_seen=("next_date", "max"),
    ).reset_index().rename(columns={"sku": "from_sku", "next_sku": "to_sku"})
    tr["to_sku"] = tr["to_sku"].astype(int)
    tr["last_seen"] = tr["last_seen"].dt.date
    co_path = REF / "changeovers.csv"
    if co_path.exists():
        co = pd.read_csv(co_path, encoding="utf-8-sig")
        flag_cols = [c for c in co.columns if c not in ("from_sku", "to_sku")]
        tr = tr.merge(co, on=["from_sku", "to_sku"], how="left")
        tr["in_changeover_table"] = tr[flag_cols[0]].notna()
    out["transitions"] = tr.sort_values(["line", "n"], ascending=[True, False])

    # --- weekly line hours --------------------------------------------------
    wk = clean[clean["usable_default"]].groupby(["line", "iso_year", "iso_week"]).agg(
        n_mo=("mo", "size"), n_skus=("sku", "nunique"), hrs=("mo_scheduled_hrs", "sum"), kg=("qty_kg", "sum")).reset_index()
    wk["hrs"], wk["kg"] = wk["hrs"].round(1), wk["kg"].round(0)
    out["weekly_line_hours"] = wk

    out["_meta"] = pd.DataFrame([{"last_run_in_log": last.date(), "recent_from": recent_from.date(),
                                  "usable_runs": len(u), "recent_usable_runs": int(u["recent"].sum())}])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rules", type=Path, default=HIST / "historical_run_log.rules.toml")
    a = ap.parse_args()
    D = tomllib.loads(a.rules.read_text(encoding="utf-8"))["derive"]
    tables = build(D)
    meta = tables.pop("_meta").iloc[0]
    L = ["# Derived historical tables", "",
         f"- built from `historical_run_log_clean.csv` (last run {meta.last_run_in_log}); "
         f"recent window from {meta.recent_from} ({D['recent_years']} y)",
         f"- usable runs: {meta.usable_runs} (recent {meta.recent_usable_runs}); "
         f"runs under {D['min_run_hrs']} h ignored for rate/campaign stats",
         "", "| table | rows |", "|---|---|"]
    for name, df in tables.items():
        df.to_csv(HIST / f"{name}.csv", index=False)
        L.append(f"| {name}.csv | {len(df)} |")
    L += ["", "## Limitations", "",
          "- Changeover duration is NOT measurable from this log: MO start time-of-day is not recorded, "
          "so `transitions.csv` carries counts only. Timestamped run data would be needed for durations.",
          "- `rates_by_line_sku.csv` compares against `calc_rate_kgph` from the live capabilities table "
          "(double lines summed across A/B). `low_evidence` pairs have fewer than "
          f"{D['min_runs_for_rate']} usable runs all-time.",
          "- Recent columns are empty (NaN) where a pair or SKU has not run inside the recent window."]
    (HIST / "derived_summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
