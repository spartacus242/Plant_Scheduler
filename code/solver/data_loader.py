# data_loader.py — Params, Files, Data, and helpers for Flowstate Phase 2 scheduler.

from __future__ import annotations
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd

try:
    from changeover_cache import load_changeover_dicts, build_sku_families
except ImportError:  # when imported as solver.data_loader
    from solver.changeover_cache import load_changeover_dicts, build_sku_families

BASE_DIR = Path(__file__).resolve().parent


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def num_or_default(v, default: int) -> int:
    try:
        if pd.isna(v):
            return default
        return int(v)
    except (ValueError, TypeError):
        return default


def float_or_default(v, default: float) -> float:
    try:
        if pd.isna(v):
            return default
        return float(v)
    except (ValueError, TypeError):
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
    # When True, keep per-SKU rates from capabilities_rates.csv instead of
    # overriding them with the flat line_rates.csv values.
    use_sku_rates: bool = False
    # Objective: minimize makespan * W1 + total_changeovers * W2
    objective_makespan_weight: int = 1
    objective_changeover_weight: int = 100
    # CIP deferral: reward pushing CIPs toward the 120h deadline (higher = more deferral)
    objective_cip_defer_weight: int = 10
    # Idle-time penalty: penalize per-line idle gaps (span − production − CIP hours)
    objective_idle_weight: int = 0
    # Soft due-date penalty (relax_due mode): cost per hour an order finishes
    # past its due window. Kept high so lateness is a last resort.
    objective_late_weight: int = 200
    # Cross-week mode: cost per hour an order runs outside the week AZAP
    # asked for. Only active when build_model(..., cross_week=True); AZAP's
    # week is then a weighted preference instead of a hard wall. Lower =
    # more willing to move a SKU between week 1 and week 2 to build a
    # longer campaign. Demand quantities stay hard either way.
    objective_week_deviation_weight: int = 40
    # CIP timing flexibility: percentage the cip_defer reward is scaled to
    # when cip_flex is on (100 = unchanged, 0 = no deferral pressure at all).
    # Lets the solver pull a CIP EARLIER to absorb a changeover. Never
    # affects the HARD max-interval deadline (food safety).
    objective_cip_flex_weight: int = 20
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
    # cip_req_after pairs (2026-08-26): a CIP is REQUIRED between these
    # SKUs; waived when the transition sits at a committed CIP window
    # (model_builder). Since 2026-09-01 the clean's HOURS are modeled as
    # setup time (Data.load setup floor), so this weight is only its
    # chemical/labor cost on top — modest by design, about a quarter of an
    # FFS change under Scenario F.
    co_cip_req_weight: int = 150
    # Soft demand (Scenario F): instead of hard qty_min (all-or-nothing via
    # the relax ladder), every kg short of qty_min costs shortfall_weight in
    # the objective. Filling always pays; shortage is reported, never hidden.
    soft_demand: bool = False
    objective_shortfall_weight: int = 1
    # Over-target reward (soft demand only): kg between an order's
    # qty_target and qty_max earn this percent of the BASE per-kg reward of
    # at-or-below-target kg (the 1000 tier x the x1000 production scaling;
    # week gradient excluded). Default 0.0 — the honest choice: the old
    # hardcoded over_sum*50 claimed "5%" but missed the x1000 scaling, so
    # every tuned run so far effectively ran at ~0.005% ≈ 0; default-off
    # preserves that observed behavior. UI range 0.0-5.0.
    over_target_reward_pct: float = 0.0
    # CP-SAT random_seed for EVERY solve pass (phase2_scheduler
    # apply_solver_seed). None = never touch the parameter, so CP-SAT keeps
    # its own default. Same model + different seed explores a different
    # search path — the overnight batch's seed arms diversify with it.
    solver_random_seed: int | None = None


class Files:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        self.caps = str(data_dir / "capabilities_rates.csv")
        self.chg = str(data_dir / "changeovers.csv")
        self.init = str(data_dir / "initial_states.csv")
        self.dem = str(data_dir / "demand_plan.csv")
        self.downtime = str(data_dir / "downtimes.csv")
        self.current_mo = str(data_dir / "current_mo.csv")
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
        self.sku_family: dict = {}  # sku -> family id (changeover grouping)
        self.sku_info_df = None  # raw sku_info DataFrame (for family grouping)
        self.init_map = {}
        self.downtimes = []
        self.orders = []

    def load(self) -> None:
        # ── Capabilities (capable flags + fallback SKU-specific rates) ───
        cap = pd.read_csv(self.F.caps)
        cap["line_id"] = pd.to_numeric(cap["line_id"], errors="coerce").fillna(0).astype(int)
        cap["sku"] = cap["sku"].astype(str)
        cap["capable"] = pd.to_numeric(cap.get("capable", 0), errors="coerce").fillna(0).astype(int)
        # Support old column names: rate_uph (legacy), rate_kgph (new VIF
        # export) and calc_rate_kgph (canonical) — all normalized to
        # calc_rate_kgph.
        rate_col = ("calc_rate_kgph" if "calc_rate_kgph" in cap.columns
                    else "rate_kgph" if "rate_kgph" in cap.columns
                    else "rate_uph")
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
            self.sku_info_df = si
            desc_col = next(
                (c for c in ("ediact_sku_description", "sku_description",
                             "description", "name", "designation")
                 if c in si.columns), None)
            for _, r in si.iterrows():
                self.sku_desc[str(r["sku"])] = (
                    str(r.get(desc_col, "")) if desc_col else "")
            self.sku_family = build_sku_families(si)

        # ── Line rates (monthly, overrides SKU-specific rates per line) ──
        # Skipped when use_sku_rates is True so the per-SKU rates from
        # capabilities_rates.csv are preserved.
        if not self.P.use_sku_rates and os.path.exists(self.F.line_rates):
            try:
                anchor = datetime.strptime(self.P.planning_start_date, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                anchor = datetime(2026, 2, 15, 0, 0, 0)
            plan_month = anchor.month
            lr = pd.read_csv(self.F.line_rates)
            lr["line_id"] = pd.to_numeric(lr["line_id"], errors="coerce").fillna(0).astype(int)
            lr["rate_kgph"] = pd.to_numeric(lr["rate_kgph"], errors="coerce").fillna(0.0)
            if "Month" in lr.columns:
                lr["Month"] = pd.to_numeric(lr["Month"], errors="coerce").fillna(0).astype(int)
                lr_active = lr[lr["Month"] == plan_month]
            else:
                # No Month column: every row is a flat rate for every month.
                lr_active = lr
            # Build per-line rate: {line_id: rate_kgph}
            line_rate_map: dict = {}
            for _, r in lr_active.iterrows():
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
        # Delegated to the cached loader (code/solver/changeover_cache.py):
        # parses the 44,310-row matrix once, persists a parquet keyed by the
        # source mtime, and memoises the three dicts in-process. Outputs are
        # byte-for-byte equivalent to the previous inline build.
        (
            self.setup,
            self.machine_changes,
            self.changeover_type,
        ) = load_changeover_dicts(self.F.chg)
        # cip_req_after pairs (2026-09-01): setup floor = CIP duration, so the
        # solver leaves the clean's slot; the fill pipeline then draws the
        # CIP into it (changeover_cache.apply_cip_req_setup_floor).
        try:
            from changeover_cache import apply_cip_req_setup_floor
        except ImportError:  # imported as solver.data_loader (tests, helpers)
            from solver.changeover_cache import apply_cip_req_setup_floor
        self.setup = apply_cip_req_setup_floor(
            self.setup, self.machine_changes, int(self.P.cip_duration_h))
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
        # Current-state MOs (optional — running/queued manprg MOs, locked to
        # their line, tonnage adjustable: qty_min is the MO remaining kg but
        # relax_demand may trim it; see model_builder).
        if os.path.exists(self.F.current_mo):
            cmo = pd.read_csv(self.F.current_mo)
            if not cmo.empty:
                self.orders.extend(self._parse_current_mo(cmo))

    def _parse_current_mo(self, cmo: pd.DataFrame) -> List[dict]:
        """Parse current_mo.csv into locked-line order dicts.

        Schema: mo,line_name,sku,remaining_kg,due_start_h,due_end_h,locked_line,source
        `locked_line` = 1 means the MO may only run on its manprg line (it is
        already in VIF and cannot be shifted). Tonnage is the *remaining* kg
        (fct − made); the solver may trim it under relax_demand but the
        objective rewards meeting it, and mo_changes.csv records the delta.
        """
        name_to_id = {v: k for k, v in self.line_names.items()}
        out: List[dict] = []
        for row_i, r in cmo.iterrows():
            line_name = str(r.get("line_name", "")).strip().upper()
            sku = str(r.get("sku", "")).strip()
            mo = str(r.get("mo", "")).strip()
            if not line_name or not sku or not mo:
                raise ValueError(
                    f"current_mo.csv row {row_i}: mo, line_name and sku are required")
            line_id = name_to_id.get(line_name)
            if line_id is None:
                raise ValueError(
                    f"current_mo.csv row {row_i}: line_name '{line_name}' "
                    "not found in capabilities_rates.csv")
            remaining = float_or_default(r.get("remaining_kg"), 0.0)
            if remaining <= 0:
                continue  # fully produced MO — nothing left to schedule
            due_start = num_or_default(r.get("due_start_h"), 0)
            # default = full horizon (audit 2026-08-15): 336-1 was the
            # pre-rolling 2-week window and clipped rows inside a 504h plan
            due_end = num_or_default(r.get("due_end_h"), self.P.horizon_h - 1)
            locked = int(num_or_default(r.get("locked_line"), 1)) == 1
            out.append(
                dict(
                    order_id=f"{mo}|CUR",
                    sku=sku,
                    due_start=due_start,
                    due_end=due_end,
                    qty_min=int(round(remaining)),
                    qty_max=int(round(remaining)),
                    priority=0,  # current MOs outrank new demand
                    is_current_mo=True,
                    mo_id=mo,
                    locked_line=line_id if locked else None,
                    source=str(r.get("source", "manprg")),
                )
            )
        return out

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
                    due_end=num_or_default(r.get("due_end_hour"),
                                           self.P.horizon_h - 1),
                    qty_min=qmin,
                    qty_max=qmax,
                    # 100%-of-demand point for the two-tier fill reward
                    # (soft demand): production past this earns almost
                    # nothing, so the solver meets every order's target
                    # before pushing any order toward qty_max.
                    qty_target=int(round(qty_target)) if qty_target > 0
                    else (qmin + qmax) // 2,
                    priority=num_or_default(r.get("priority"), 999),
                )
            )
        # Deduplicate auto-generated order IDs by appending a suffix
        seen: dict[str, int] = {}
        for o in out:
            oid = o["order_id"]
            if oid in seen:
                seen[oid] += 1
                o["order_id"] = f"{oid}_{seen[oid]}"
            else:
                seen[oid] = 0
        return out


def available_hours_line(P: Params, data: Data, l: int) -> int:
    # Blocked time is the interval UNION of [0, available_from) and the
    # line's downtime windows — NOT their sum. Scenario F stages the
    # committed plan as downtime rows covering exactly [0, gate), so the
    # naive sum counted every pre-gate hour twice and the per-line budget
    # `total_run + CIP <= avail_h` silently zeroed 8 of 14 lines (proven
    # 2026-08-14: solver capped total fill at 849t vs ~1.9t structural).
    H = P.horizon_h
    gate = min(max(0, data.init_map.get(l, {}).get("available_from", 0)), H)
    ivs = [(0, gate)] if gate > 0 else []
    for dt in data.downtimes:
        if dt["line_id"] != l:
            continue
        s = max(0, dt["start"])
        e = min(H, dt["end"])
        if e > s:
            ivs.append((s, e))
    blocked, cur_s, cur_e = 0, None, None
    for s, e in sorted(ivs):
        if cur_e is None or s > cur_e:
            blocked += (cur_e - cur_s) if cur_e is not None else 0
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        blocked += cur_e - cur_s
    return max(0, H - blocked)
