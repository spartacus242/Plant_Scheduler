# data_loader.py — Params, Files, Data, and helpers for Flowstate Phase 2 scheduler.

from __future__ import annotations
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def num_or_default(v, default: int) -> int:
    try:
        if pd.isna(v):
            return default
        return int(v)
    except Exception:
        return default


def float_or_default(v, default: float) -> float:
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


@dataclass
class Params:
    horizon_h: int = 336
    changeover_penalty: float = 0.15
    cip_interval_h: int = 120
    cip_duration_h: int = 6
    max_lines_per_order: int = 3
    stale_threshold_days: int = 90
    stale_setup_extra_h: int = 4
    long_shutdown_default_h: int = 4
    planning_start_date: str = "2026-02-15 00:00:00"
    # Min runtime per (line, order): at least min_run_hours or 50% of qty_min on that line
    min_run_hours: int = 4
    min_run_pct_of_qty: float = 0.5
    # Allow Week-1 orders to be produced in Week-0 to fill slack and smooth week-to-week
    allow_week1_in_week0: bool = True
    # Objective: minimize makespan * W1 + total_changeovers * W2
    objective_makespan_weight: int = 1
    objective_changeover_weight: int = 100


class Files:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        self.caps = str(data_dir / "Capabilities & Rates.csv")
        self.chg = str(data_dir / "Changeovers.csv")
        self.init = str(data_dir / "InitialStates.csv")
        self.dem = str(data_dir / "DemandPlan.csv")
        self.downtime = str(data_dir / "Downtimes.csv")
        self.last_run = str(data_dir / "LineSKU_LastRun.csv")


class Data:
    def __init__(self, P: Params, F: Files):
        self.P = P
        self.F = F
        self.lines = []
        self.line_names = {}
        self.capable = {}
        self.rate = {}
        self.setup = {}
        self.init_map = {}
        self.downtimes = []
        self.orders = []
        self.last_map = {}

    def load(self) -> None:
        cap = pd.read_csv(self.F.caps)
        cap["line_id"] = pd.to_numeric(cap["line_id"], errors="coerce").fillna(0).astype(int)
        cap["sku"] = cap["sku"].astype(str)
        cap["capable"] = pd.to_numeric(cap.get("capable", 0), errors="coerce").fillna(0).astype(int)
        cap["rate_uph"] = pd.to_numeric(cap.get("rate_uph", 0), errors="coerce").fillna(0.0).astype(float)
        self.lines = sorted(cap["line_id"].unique().tolist())
        for _, r in cap.iterrows():
            lid = int(r["line_id"])
            sku = str(r["sku"])
            self.line_names[lid] = str(r.get("line_name", f"L{lid}"))
            self.capable[(lid, sku)] = int(r["capable"]) if not pd.isna(r["capable"]) else 0
            self.rate[(lid, sku)] = float(r["rate_uph"]) if not pd.isna(r["rate_uph"]) else 0.0
        # Changeovers
        chg = pd.read_csv(self.F.chg)
        chg["from_sku"] = chg["from_sku"].astype(str)
        chg["to_sku"] = chg["to_sku"].astype(str)
        chg["setup_hours"] = pd.to_numeric(chg["setup_hours"], errors="coerce").fillna(0.0)
        chg["setup_rounded"] = chg["setup_hours"].apply(round_half_up)
        for _, r in chg.iterrows():
            self.setup[(str(r["from_sku"]), str(r["to_sku"]))] = int(r["setup_rounded"])
        # Initial states
        init = pd.read_csv(self.F.init)
        for c, d in {
            "initial_sku": "CLEAN",
            "available_from_hour": 0,
            "long_shutdown_flag": 0,
            "long_shutdown_extra_setup_hours": self.P.long_shutdown_default_h,
            "carryover_run_hours_since_last_cip_at_t0": 0,
            "last_cip_end_datetime": "",
            "comment": "",
        }.items():
            if c not in init.columns:
                init[c] = d
        init["line_id"] = pd.to_numeric(init["line_id"], errors="coerce").fillna(0).astype(int)
        init["initial_sku"] = init["initial_sku"].astype(str).fillna("CLEAN")
        init.loc[init["initial_sku"].str.strip().eq(""), "initial_sku"] = "CLEAN"
        init["available_from_hour"] = pd.to_numeric(init["available_from_hour"], errors="coerce").fillna(0).astype(int)
        init["long_shutdown_flag"] = pd.to_numeric(init["long_shutdown_flag"], errors="coerce").fillna(0).astype(int)
        init["long_shutdown_extra_setup_hours"] = (
            pd.to_numeric(init["long_shutdown_extra_setup_hours"], errors="coerce")
            .fillna(self.P.long_shutdown_default_h)
            .astype(int)
        )
        init["carryover_run_hours_since_last_cip_at_t0"] = (
            pd.to_numeric(init["carryover_run_hours_since_last_cip_at_t0"], errors="coerce").fillna(0).astype(int)
        )
        for _, r in init.iterrows():
            lid = int(r["line_id"])
            self.init_map[lid] = dict(
                initial_sku=str(r["initial_sku"]).strip() or "CLEAN",
                available_from=int(r["available_from_hour"]),
                long_shutdown_flag=int(r["long_shutdown_flag"]),
                long_shutdown_extra=int(r["long_shutdown_extra_setup_hours"]),
                carryover_run_hours=int(r["carryover_run_hours_since_last_cip_at_t0"]),
                last_cip_end_datetime=(str(r.get("last_cip_end_datetime", "")) or None),
                comment=str(r.get("comment", "")),
            )
        # Downtimes
        if os.path.exists(self.F.downtime):
            dt = pd.read_csv(self.F.downtime)
            for _, r in dt.iterrows():
                self.downtimes.append(
                    dict(
                        line_id=num_or_default(r.get("line_id"), 0),
                        start=num_or_default(r.get("start_hour"), 0),
                        end=num_or_default(r.get("end_hour"), 0),
                        reason=str(r.get("reason", "")),
                    )
                )
        # Demand
        dem = pd.read_csv(self.F.dem)
        self.orders = self._parse_demand(dem)

    def _parse_demand(self, dem: pd.DataFrame) -> List[dict]:
        out = []
        for _, r in dem.iterrows():
            sku = str(r.get("sku", ""))
            if not sku:
                raise ValueError("DemandPlan row missing 'sku'")
            order_id = str(r.get("order_id", ""))
            if not order_id or order_id.lower() == "nan":
                wk = r.get("week_index")
                if pd.isna(wk):
                    ds = num_or_default(r.get("due_start_hour"), 0)
                    wk = 0 if ds <= 167 else 1
                order_id = f"W{int(wk)}-{sku}"
            qty_target = float_or_default(r.get("qty_target"), 0.0)
            lower_pct = r.get("lower_pct")
            upper_pct = r.get("upper_pct")
            qty_min = r.get("qty_min")
            qty_max = r.get("qty_max")
            if pd.notna(lower_pct) and pd.notna(upper_pct):
                qmin = int(math.floor(qty_target * float(lower_pct)))
                qmax = int(math.ceil(qty_target * float(upper_pct)))
            elif pd.notna(qty_min) and pd.notna(qty_max):
                qmin = num_or_default(qty_min, 0)
                qmax = num_or_default(qty_max, 0)
            else:
                raise ValueError(f"Demand row for sku={sku} needs pct bounds or qty_min/max.")
            out.append(
                dict(
                    order_id=order_id,
                    sku=sku,
                    due_start=num_or_default(r.get("due_start_hour"), 0),
                    due_end=num_or_default(r.get("due_end_hour"), 336 - 1),
                    qty_min=qmin,
                    qty_max=qmax,
                    priority=num_or_default(r.get("priority"), 999),
                )
            )
        return out


def available_hours_line(P: Params, data: Data, l: int) -> int:
    H = P.horizon_h
    avail_block = min(max(0, data.init_map.get(l, {}).get("available_from", 0)), H)
    blocked = avail_block
    for dt in data.downtimes:
        if dt["line_id"] != l:
            continue
        s = max(0, dt["start"])
        e = min(H, dt["end"])
        if e > s:
            blocked += e - s
    return max(0, H - blocked)
