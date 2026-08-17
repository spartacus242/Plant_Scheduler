#!/usr/bin/env python3
"""Diagnostic: the SKU-week coverage ledger on the LIVE data dir.

Prints what Reconcile / the Plan page will show: per-week totals, the
murky-case flags, and the full detail for any SKUs passed as arguments.

Usage:
    python scripts/diag_demand_coverage.py [SKU ...]
    python scripts/diag_demand_coverage.py --now "2026-08-20 12:00" 280480

--now simulates a mid-week anchor (the Thursday workflow): the horizon and
current state are rebuilt as of that clock, and manprg MOs whose window has
fully elapsed by then are treated as completed (what the file would say),
so the completed-actuals leg of the ledger is exercised for real.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

import pandas as pd  # noqa: E402

DATA = ROOT / "data"


def _simulated_state(cfg, now: datetime):
    """Current state as manprg WOULD read at `now`: rows whose nominal
    window has fully elapsed get Qty made = Fct qty (completed)."""
    from helpers.config import datasources_config
    from helpers.current_state import build_current_state
    from helpers.horizon import resolve
    from helpers.manprg_import import read_manprg

    hz = resolve(cfg, now=now)
    ds = datasources_config(cfg)
    ref = DATA / "reference"
    mp_paths = [p.strip() for p in str(ds.get("manprg_files", "")).split(";")
                if p.strip()] or [str(ref / "manprg.txt"),
                                  str(ref / "manprg2.txt")]
    manprg = read_manprg([p for p in mp_paths if Path(p).exists()])
    frame = manprg.frame.copy()
    elapsed = 0
    for idx, r in frame.iterrows():
        start = r.get("start_dt")
        hours = float(pd.to_numeric(pd.Series([r.get("hours")]),
                                    errors="coerce").iloc[0] or 0)
        if start is None or pd.isna(start):
            continue
        if pd.Timestamp(start) + timedelta(hours=hours) <= pd.Timestamp(now) \
                and str(r.get("item", "")).strip().upper() not in ("CIP",):
            frame.at[idx, "made_cas"] = r.get("fct_cas")
            frame.at[idx, "made_kg"] = r.get("fct_kg")
            elapsed += 1
    manprg.frame = frame
    print(f"[sim] now={now:%Y-%m-%d %H:%M} — {elapsed} manprg row(s) whose "
          "window elapsed marked completed")
    state = build_current_state(hz, manprg=manprg,
                                cip_path=str(ref / "cip_info.csv"), cfg=cfg)
    return hz, state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("skus", nargs="*", help="SKUs to detail")
    ap.add_argument("--now", default=None,
                    help='simulate the clock, e.g. "2026-08-20 12:00"')
    args = ap.parse_args()

    from helpers.config import load_toml
    from helpers.demand_coverage import build_ledger_from_data

    cfg = load_toml()
    state = None
    if args.now:
        now = datetime.strptime(args.now, "%Y-%m-%d %H:%M")
        _, state = _simulated_state(cfg, now)
        # build_ledger_from_data resolves its own horizon from the real
        # clock; hand it the simulated state but ALSO a simulated horizon —
        # easiest is to monkey-set anchor_mode + planning_start_date.
        cfg.setdefault("scheduler", {})
        cfg["scheduler"]["anchor_mode"] = "fixed"
        cfg["scheduler"]["planning_start_date"] = now.strftime(
            "%Y-%m-%d 00:00:00")

    ledger = build_ledger_from_data(DATA, cfg, state=state)
    if ledger is None:
        print("no demand_plan.csv — nothing to show")
        return 1

    df = ledger.to_frame()
    cur = ledger.anchor_week_key
    print(f"\nanchor week key: {cur} — {len(df)} ledger row(s), "
          f"{ledger.unknown_kg_blocks} unknown-kg block(s), "
          f"produced (completed MOs) total {ledger.produced_total_kg:,.0f} kg")
    for n in ledger.notes:
        print(f"  note: {n}")

    wk = df.groupby("week_label", sort=False).agg(
        demand=("gross_kg", "sum"), committed=("committed_kg", "sum"),
        made=("produced_kg", "sum"), covered=("applied_kg", "sum"),
        net=("net_kg", "sum"), orders=("order_id", "count"),
        full=("status", lambda s: int((s == "COVERED").sum())))
    print("\nPer-week totals (kg):")
    print(wk.to_string(float_format=lambda v: f"{v:,.0f}"))

    if ledger.overcommitted:
        print("\nOVERCOMMITTED flags:")
        for sku, d in sorted(ledger.overcommitted.items(),
                             key=lambda kv: -kv[1]["surplus_kg"]):
            print(f"  {sku}: +{d['surplus_kg']:,.0f} kg surplus "
                  f"(supply {d['total_supply_kg']:,.0f} vs demand "
                  f"{d['total_gross_kg']:,.0f} through {d['last_week_label']})")
    if ledger.no_demand:
        top = sorted(ledger.no_demand.items(), key=lambda kv: -kv[1])[:10]
        print("\nCommitted SKUs absent from the demand plan (top 10):")
        for sku, kg in top:
            print(f"  {sku}: {kg:,.0f} kg")

    for sku in args.skus:
        sub = df[df["sku"] == sku]
        print(f"\nDetail — {sku}:")
        print(sub.drop(columns=["week_key", "sku"]).to_string(
            index=False, float_format=lambda v: f"{v:,.0f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
