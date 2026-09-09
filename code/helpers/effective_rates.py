# code/helpers/effective_rates.py
#
# Effective line x SKU rates (2026-09-08): the plant's capabilities table
# with MEASURED kg/h from the historical run log overlaid wherever the
# evidence is strong enough. Every computation reader (solver staging,
# scorecard, calendar, fill, stock check) goes through
# load_effective_capabilities so they all price hours the same way.
#
# Provenance is kept on every row:
#   calc_rate_kgph        the EFFECTIVE rate consumers use
#   calc_rate_kgph_model  the plant's own calc rate, untouched
#   rate_source           "measured_recent" | "measured_alltime" | "model"
#   rate_evidence_runs    usable runs behind a measured rate (0 for model)
#
# Nothing here is a threshold: the policy (which column, how many runs, on
# or off) is the [effective_rates] section of
# data/reference/historical/historical_run_log.rules.toml, and the numbers
# are data/reference/historical/rates_by_line_sku.csv, both rebuilt by
# scripts/derive_historical_tables.py. The capabilities file itself is
# never written to — it is synced both ways with the plant by the bridge.
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pandas as pd

from helpers.capability_check import load_capabilities

HIST_SUBDIR = "historical"
RULES_NAME = "historical_run_log.rules.toml"
_SIDE = re.compile(r"^(P\d+)([AB])$")


def infer_data_dir(caps_path: str | Path) -> Path | None:
    """data dir for a reference/capabilities_rates.csv path; None otherwise
    (a solver work-dir copy is already effective and must not be re-overlaid)."""
    p = Path(caps_path).resolve()
    return p.parent.parent if p.parent.name == "reference" else None


def load_rate_policy(dd: str | Path) -> dict | None:
    """[effective_rates] from the rules TOML; None when absent or disabled."""
    rules = Path(dd) / "reference" / HIST_SUBDIR / RULES_NAME
    if not rules.exists():
        return None
    pol = tomllib.loads(rules.read_text(encoding="utf-8")).get("effective_rates") or {}
    return pol if pol.get("enabled", False) else None


def _measured_lookup(table: Path, pol: dict) -> pd.DataFrame:
    """(line_group, sku) -> rate, source, runs per the policy's primary /
    fallback columns. Rows without enough evidence are dropped."""
    m = pd.read_csv(table, dtype={"sku": str})
    m["line"] = m["line"].astype(str).str.strip().str.upper()
    m["sku"] = m["sku"].astype(str).str.strip()
    p_rate, p_runs, p_min = pol["primary_rate_column"], pol["primary_runs_column"], int(pol["primary_min_runs"])
    f_rate, f_runs, f_min = pol["fallback_rate_column"], pol["fallback_runs_column"], int(pol["fallback_min_runs"])
    for c in (p_rate, p_runs, f_rate, f_runs):
        m[c] = pd.to_numeric(m.get(c), errors="coerce")
    primary = m[p_rate].notna() & (m[p_rate] > 0) & (m[p_runs] >= p_min)
    fallback = ~primary & m[f_rate].notna() & (m[f_rate] > 0) & (m[f_runs] >= f_min)
    out = pd.DataFrame({
        "line_group": m["line"], "sku": m["sku"],
        "_rate": m[p_rate].where(primary, m[f_rate]),
        "rate_source": pd.Series("measured_recent", index=m.index).where(primary, "measured_alltime"),
        "rate_evidence_runs": m[p_runs].where(primary, m[f_runs]).fillna(0).astype(int),
    })
    return out[primary | fallback].drop_duplicates(["line_group", "sku"])


def apply_measured_rates(df: pd.DataFrame, dd: str | Path | None) -> pd.DataFrame:
    """Overlay measured rates onto a normalized capabilities frame.

    Capable flags are never changed. When the policy is off, or the
    historical tables are missing, every row is `rate_source = model` and
    calc_rate_kgph is the plant's own number — behaviour identical to
    before the overlay existed.
    """
    out = df.copy()
    out["calc_rate_kgph"] = pd.to_numeric(out.get("calc_rate_kgph", 0), errors="coerce").fillna(0.0).astype(float)
    out["calc_rate_kgph_model"] = out["calc_rate_kgph"]
    out["rate_source"] = "model"
    out["rate_evidence_runs"] = 0
    pol = load_rate_policy(dd) if dd is not None else None
    if pol is None:
        return out
    table = Path(dd) / "reference" / HIST_SUBDIR / str(pol.get("table", "rates_by_line_sku.csv"))
    if not table.exists() or "line_name" not in out.columns or "sku" not in out.columns:
        return out
    look = _measured_lookup(table, pol)
    divisor = float(pol.get("side_divisor", 2.0))

    names = out["line_name"].astype(str).str.strip().str.upper()
    sides = names.str.extract(_SIDE)
    out["_line_group"] = sides[0].fillna(names)
    out["_is_side"] = sides[1].notna()
    out["_sku"] = out["sku"].astype(str).str.strip()
    merged = out.merge(look, left_on=["_line_group", "_sku"], right_on=["line_group", "sku"],
                       how="left", suffixes=("", "_m"))
    hit = merged["_rate"].notna()
    rate = merged["_rate"].where(~merged["_is_side"], merged["_rate"] / divisor)
    out["calc_rate_kgph"] = rate.where(hit, out["calc_rate_kgph"]).astype(float).to_numpy()
    out["rate_source"] = merged["rate_source_m"].where(hit, "model").to_numpy()
    out["rate_evidence_runs"] = merged["rate_evidence_runs_m"].where(hit, 0).fillna(0).astype(int).to_numpy()
    return out.drop(columns=["_line_group", "_is_side", "_sku"])


def load_effective_capabilities(caps_path: str | Path, dd: str | Path | None = None) -> pd.DataFrame:
    """capabilities_rates.csv normalized (helpers.capability_check.load_capabilities)
    with the measured overlay applied. `dd` defaults to the data dir the
    path lives in; a path outside a reference/ dir gets no overlay."""
    df = load_capabilities(caps_path)
    return apply_measured_rates(df, dd if dd is not None else infer_data_dir(caps_path))


def rate_source_summary(df: pd.DataFrame) -> dict[str, int]:
    """{rate_source: rows} over capable rows — for health / UI notes."""
    if "rate_source" not in df.columns:
        return {}
    cap = pd.to_numeric(df.get("capable", 0), errors="coerce").fillna(0) == 1
    return df.loc[cap, "rate_source"].value_counts().to_dict()
