# data_loader.py — Params, Files, Data, and helpers for Flowstate Phase 2 scheduler.

from __future__ import annotations
import math
import os
from dataclasses import dataclass
from datetime import datetime
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
    # CIP deferral: reward pushing CIPs toward the 120h deadline (higher = more deferral)
    objective_cip_defer_weight: int = 10
    # Idle-time penalty: penalize per-line idle gaps (span − production − CIP hours)
    objective_idle_weight: int = 0
    # Per-machine changeover weights (used in weighted changeover objective)
    co_topload_weight: int = 50
    co_ttp_weight: int = 10
    co_ffs_weight: int = 10
    co_casepacker_weight: int = 10
    co_base_weight: int = 5
    # Organic-conversion and cinnamon changeover penalties
    co_conv_org_weight: int = 30
    co_cinn_weight: int = 20
    # Per-added-flavor penalty (negative added_flavors = reward)
    co_flavor_weight: int = 5


class Files:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        self.caps = str(data_dir / "capabilities_rates.csv")
        self.chg = str(data_dir / "changeovers.csv")
        self.init = str(data_dir / "initial_states.csv")
        self.dem = str(data_dir / "demand_plan.csv")
        self.downtime = str(data_dir / "downtimes.csv")
        self.last_run = str(data_dir / "line_sku_last_run.csv")
        self.trials = str(data_dir / "trials.csv")
        self.line_rates = str(data_dir / "line_rates.csv")
        self.line_cip_hrs = str(data_dir / "line_cip_hrs.csv")
        self.sku_info = str(data_dir / "sku_info.csv")


class Data:
    def __init__(self, P: Params, F: Files):
        self.P = P
        self.F = F
        self.lines = []
        self.line_names = {}
        self.capable = {}
        self.rate = {}
        self.setup = {}
        self.machine_changes = {}   # (from_sku, to_sku) -> {ttp, ffs, topload, casepacker, conv_to_org, cinn_to_non, added_flavors}
        self.changeover_type = {}   # (from_sku, to_sku) -> "1-0-1-1" string
        self.cip_interval_map = {}  # line_id -> max_cip_hrs (per-line CIP interval)
        self.sku_desc: dict[str, str] = {}  # sku -> ediact_sku_description
        self.init_map = {}
        self.downtimes = []
        self.orders = []
        self.last_map = {}

    def load(self) -> None:
        # ── Capabilities (capable flags + fallback SKU-specific rates) ───
        cap = pd.read_csv(self.F.caps)
        cap["line_id"] = pd.to_numeric(cap["line_id"], errors="coerce").fillna(0).astype(int)
        cap["sku"] = cap["sku"].astype(str)
        cap["capable"] = pd.to_numeric(cap.get("capable", 0), errors="coerce").fillna(0).astype(int)
        # Support both old column name (rate_uph) and new (calc_rate_kgph)
        rate_col = "calc_rate_kgph" if "calc_rate_kgph" in cap.columns else "rate_uph"
        cap[rate_col] = pd.to_numeric(cap.get(rate_col, 0), errors="coerce").fillna(0.0).astype(float)
        self.lines = sorted(cap["line_id"].unique().tolist())
        for _, r in cap.iterrows():
            lid = int(r["line_id"])
            sku = str(r["sku"])
            self.line_names[lid] = str(r.get("line_name", f"L{lid}"))
            self.capable[(lid, sku)] = int(r["capable"]) if not pd.isna(r["capable"]) else 0
            self.rate[(lid, sku)] = float(r[rate_col]) if not pd.isna(r[rate_col]) else 0.0

        # ── SKU descriptions (sku_info.csv) ──────────────────────────────
        if os.path.exists(self.F.sku_info):
            si = pd.read_csv(self.F.sku_info)
            si["sku"] = si["sku"].astype(str)
            for _, r in si.iterrows():
                self.sku_desc[str(r["sku"])] = str(r.get("ediact_sku_description", ""))

        # ── Line rates (monthly, overrides SKU-specific rates per line) ──
        # Uses the month from planning_start_date to pick the correct rate.
        if os.path.exists(self.F.line_rates):
            try:
                anchor = datetime.strptime(self.P.planning_start_date, "%Y-%m-%d %H:%M:%S")
            except Exception:
                anchor = datetime(2026, 2, 15, 0, 0, 0)
            plan_month = anchor.month
            lr = pd.read_csv(self.F.line_rates)
            lr["line_id"] = pd.to_numeric(lr["line_id"], errors="coerce").fillna(0).astype(int)
            lr["Month"] = pd.to_numeric(lr["Month"], errors="coerce").fillna(0).astype(int)
            lr["rate_kgph"] = pd.to_numeric(lr["rate_kgph"], errors="coerce").fillna(0.0)
            lr_month = lr[lr["Month"] == plan_month]
            # Build per-line rate: {line_id: rate_kgph}
            line_rate_map: dict = {}
            for _, r in lr_month.iterrows():
                line_rate_map[int(r["line_id"])] = float(r["rate_kgph"])
            # Override self.rate for all (line, sku) pairs present in line_rate_map.
            # Keep rates even for non-capable pairs so trials can look them up.
            for (lid, sku) in self.capable:
                if lid in line_rate_map:
                    self.rate[(lid, sku)] = line_rate_map[lid]

        # ── Per-line CIP intervals ────────────────────────────────────────
        if os.path.exists(self.F.line_cip_hrs):
            lc = pd.read_csv(self.F.line_cip_hrs)
            lc["line_id"] = pd.to_numeric(lc["line_id"], errors="coerce").fillna(0).astype(int)
            lc["max_cip_hrs"] = pd.to_numeric(lc["max_cip_hrs"], errors="coerce").fillna(
                self.P.cip_interval_h
            ).astype(int)
            for _, r in lc.iterrows():
                self.cip_interval_map[int(r["line_id"])] = int(r["max_cip_hrs"])

        # ── Changeovers ──────────────────────────────────────────────────
        chg = pd.read_csv(self.F.chg)
        chg["from_sku"] = chg["from_sku"].astype(str)
        chg["to_sku"] = chg["to_sku"].astype(str)
        chg["setup_hours"] = pd.to_numeric(chg["setup_hours"], errors="coerce").fillna(0.0)
        chg["setup_rounded"] = chg["setup_hours"].apply(round_half_up)
        # Machine-level changeover columns (backward-compat: default to 1 if missing)
        has_machine_cols = all(
            c in chg.columns for c in ("ttp_change", "ffs_change", "topload_change", "casepacker_change")
        )
        if has_machine_cols:
            for col in ("ttp_change", "ffs_change", "topload_change", "casepacker_change"):
                chg[col] = pd.to_numeric(chg[col], errors="coerce").fillna(1).astype(int)
        # New columns: conv_to_org_change, cinn_to_non, added_flavors
        has_new_co_cols = all(
            c in chg.columns for c in ("conv_to_org_change", "cinn_to_non", "added_flavors")
        )
        if has_new_co_cols:
            for col in ("conv_to_org_change", "cinn_to_non"):
                chg[col] = pd.to_numeric(chg[col], errors="coerce").fillna(0).astype(int)
            chg["added_flavors"] = pd.to_numeric(chg["added_flavors"], errors="coerce").fillna(0).astype(int)
        for _, r in chg.iterrows():
            pair = (str(r["from_sku"]), str(r["to_sku"]))
            self.setup[pair] = int(r["setup_rounded"])
            if has_machine_cols:
                mc = {
                    "ttp": int(r["ttp_change"]),
                    "ffs": int(r["ffs_change"]),
                    "topload": int(r["topload_change"]),
                    "casepacker": int(r["casepacker_change"]),
                }
            else:
                full = 1 if int(r["setup_rounded"]) > 0 else 0
                mc = {"ttp": full, "ffs": full, "topload": full, "casepacker": full}
            if has_new_co_cols:
                mc["conv_to_org"] = int(r["conv_to_org_change"])
                mc["cinn_to_non"] = int(r["cinn_to_non"])
                mc["added_flavors"] = int(r["added_flavors"])
            else:
                mc["conv_to_org"] = 0
                mc["cinn_to_non"] = 0
                mc["added_flavors"] = 0
            self.machine_changes[pair] = mc
            self.changeover_type[pair] = (
                f"{mc['ttp']}-{mc['ffs']}-{mc['topload']}-{mc['casepacker']}"
            )
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
        # Trials (optional — pinned-line, fixed-time production blocks)
        if os.path.exists(self.F.trials):
            tri = pd.read_csv(self.F.trials)
            if not tri.empty:
                self.orders.extend(self._parse_trials(tri))

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


    def _parse_trials(self, tri: pd.DataFrame) -> List[dict]:
        """Parse trials.csv into order dicts with trial-specific fields.

        Each trial is pinned to a specific line with a fixed start time.
        Either end_datetime or target_kgs (or both) must be supplied.
        """
        # Build reverse lookup: line_name -> line_id
        name_to_id = {v: k for k, v in self.line_names.items()}
        try:
            anchor = datetime.strptime(
                self.P.planning_start_date, "%Y-%m-%d %H:%M:%S"
            )
        except Exception:
            anchor = datetime(2026, 2, 15, 0, 0, 0)

        out: List[dict] = []
        for row_i, r in tri.iterrows():
            line_name = str(r.get("line_name", "")).strip()
            sku = str(r.get("sku", "")).strip()
            if not line_name or not sku:
                raise ValueError(
                    f"trials.csv row {row_i}: line_name and sku are required"
                )
            line_id = name_to_id.get(line_name)
            if line_id is None:
                raise ValueError(
                    f"trials.csv row {row_i}: line_name '{line_name}' "
                    "not found in capabilities_rates.csv"
                )

            # Parse start_datetime (required)
            start_raw = str(r.get("start_datetime", "")).strip()
            if not start_raw or start_raw.lower() == "nan":
                raise ValueError(
                    f"trials.csv row {row_i}: start_datetime is required"
                )
            start_dt = pd.to_datetime(start_raw)
            start_hour = int(
                (start_dt - pd.Timestamp(anchor)).total_seconds() / 3600
            )
            if start_hour < 0:
                raise ValueError(
                    f"trials.csv row {row_i}: start_datetime "
                    f"({start_raw}) is before planning start "
                    f"({self.P.planning_start_date})"
                )

            # Parse end_datetime (optional)
            end_raw = str(r.get("end_datetime", "")).strip()
            has_end = end_raw and end_raw.lower() != "nan"
            end_hour = None
            if has_end:
                end_dt = pd.to_datetime(end_raw)
                end_hour = int(
                    (end_dt - pd.Timestamp(anchor)).total_seconds() / 3600
                )

            # Parse target_kgs (optional)
            kgs_raw = r.get("target_kgs")
            has_kgs = pd.notna(kgs_raw) and float(kgs_raw) > 0
            target_kgs = float(kgs_raw) if has_kgs else 0.0

            if not has_end and not has_kgs:
                raise ValueError(
                    f"trials.csv row {row_i}: at least one of "
                    "end_datetime or target_kgs must be provided"
                )

            # Resolve end_hour from target_kgs if needed
            trial_run_hours = None  # production-only hours (set when computed from target_kgs)
            if not has_end:
                rate = self.rate.get((line_id, sku))
                if rate is None or rate <= 0:
                    raise ValueError(
                        f"trials.csv row {row_i}: target_kgs given "
                        f"but no rate found for ({line_name}, {sku}) "
                        "in capabilities_rates.csv. Provide end_datetime "
                        "instead."
                    )
                run_hours = math.ceil(target_kgs / rate)
                end_hour = start_hour + run_hours

                # Widen the trial window to accommodate CIP blocks.
                # Without this, the solver sacrifices production hours
                # to fit the CIP within the original window.
                carryover = int(
                    self.init_map.get(line_id, {}).get(
                        "carryover_run_hours", 0
                    )
                )
                line_cip_interval = self.cip_interval_map.get(
                    line_id, self.P.cip_interval_h
                )
                num_cips = 0
                while (
                    run_hours
                    + num_cips * self.P.cip_duration_h
                    + carryover
                    >= (num_cips + 1) * line_cip_interval
                ):
                    num_cips += 1
                    if num_cips > 3:  # model supports max 3 CIPs
                        break
                if num_cips > 0:
                    end_hour += num_cips * self.P.cip_duration_h
                trial_run_hours = run_hours  # production hours only

            run_hours = end_hour - start_hour
            if run_hours <= 0:
                raise ValueError(
                    f"trials.csv row {row_i}: computed run_hours "
                    f"<= 0 (start_hour={start_hour}, "
                    f"end_hour={end_hour})"
                )

            # Resolve qty bounds — qty_max must cover actual production
            # Use production-only hours (not CIP-extended span) for qty
            effective_run = (
                trial_run_hours if trial_run_hours is not None
                else run_hours
            )
            rate = self.rate.get((line_id, sku))
            actual_production = (
                int(math.ceil(rate * effective_run)) if rate and rate > 0
                else 0
            )
            if has_kgs:
                qty_min = 0
                qty_max = max(int(target_kgs), actual_production)
            elif rate and rate > 0:
                qty_min = 0
                qty_max = actual_production
            else:
                qty_min = 0
                qty_max = 0

            order_id = f"TRIAL-{sku}-L{line_name}"
            out.append(
                dict(
                    order_id=order_id,
                    sku=sku,
                    due_start=start_hour,
                    due_end=end_hour - 1,
                    qty_min=qty_min,
                    qty_max=qty_max,
                    priority=0,  # highest priority
                    is_trial=True,
                    trial_line=line_id,
                    trial_start_hour=start_hour,
                    trial_end_hour=end_hour,
                    trial_run_hours=trial_run_hours,  # production hours (None if explicit end_datetime)
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
