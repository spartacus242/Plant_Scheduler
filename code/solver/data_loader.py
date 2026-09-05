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
    # Early-fill policy (plant decision 2026-09-04): how far before its own
    # due_start a DEMAND order may start when allow_week1_in_week0 is on.
    #   None      -> unbounded: next-week demand may be pre-built as far back
    #                into earlier weeks as free capacity allows; the only
    #                floors are the horizon, the line availability gate,
    #                committed downtime windows and (Scenario E) the end of
    #                every committed current-state MO on the line.
    #   int hours -> every demand order may start at most that many hours
    #                before its own due_start (the pre-2026-09-04 rule was
    #                model_builder.EARLY_FILL_HOURS = 48, SECOND week only).
    # [scheduler] early_fill_hours = "unbounded" | "none" | <int>; absent =
    # None. Producing in the right week stays a SOFT preference either way
    # (week gradient + objective_week_deviation_weight per early hour).
    early_fill_hours: int | None = None
    # Due-week policy: [scheduler] due_week_policy = "hard" | "soft".
    #   "hard" (SHIPPED DEFAULT, 2026-09-04 night) -> a DEMAND order's week
    #       end is a wall: due_end + 1 at relax levels 0-1 (level 2 relax_due
    #       prices lateness per hour). Early fill is unchanged (decision #1,
    #       early_fill_hours above) and the early kg-week price below still
    #       ranks the nearer week first on a contested hour.
    #   "soft" (OPT-IN; plant decision 2026-09-04 #2: "Treat the week numbers
    #       on the demand_plan_summary.csv as preferential order, but not
    #       necessarily the hard borders ... if a sku has tons scheduled in
    #       week 1 and week 3, and it makes sense to combine all kgs for that
    #       sku in one run in week 2, then that should be explored") -> the
    #       due week is a PREFERENCE in both directions: the order may also
    #       finish late inside the horizon, and every kg placed in the wrong
    #       week is priced per kg per whole week of deviation with the two
    #       weights below. The horizon end, the line gate, committed windows /
    #       MOs and every current MO's or trial's own window stay hard.
    #   Why soft is not the default: benchmarked 2026-09-04 at 600 s on the
    #       live board (bench/results/policy_soft_v2, two seeds) soft delivered
    #       2.22 / 2.06 kt on time vs 3.59 / 3.36 kt under hard (-38 %), 463 /
    #       602 t late, W1 on-time 52 / 54 % vs 86 / 81 %, changeover hours
    #       80.8 / 85.2 vs 73.8 / 73.2, and 6 of 8 discretionary large (>= 30
    #       t) late orders saved no changeover -- pass-1 search stalls on the
    #       larger soft model, so the lateness is search residue, not a priced
    #       trade. Hard keeps 0 late kg and still finds 6-9 at-wall
    #       consolidations for free.
    due_week_policy: str = "hard"
    # Deviation prices, in FILL UNITS PER TONNE PER WEEK-STEP, where 1 kg of
    # tier-1 fill = 1,000 units (the pass-2 currency; "kg-eq" below = the
    # fill value of one kg). late_kg_week_weight is used ONLY under
    # due_week_policy = "soft" (hard builds no lateness terms for demand
    # orders); early_kg_week_weight grades pre-building under both policies.
    # v2 pricing (2026-09-04 evening): the SAME fraction of a kg's fill value
    # is charged in every pass -- pass 2 applies weight / 1000 per kg-week
    # (fill ~ 1,000 per kg), pass 1 applies weight per kg-week (fill = 1e6 per
    # kg). v1 applied weight / 1000 in both, so the price was 0.2 % of fill in
    # pass 1 and 200 % in pass 2, and pass 2 bought pass 1's deviation back
    # with changeovers (model_builder).
    # One FFS change under the Scenario F weights = (5 + 600) x 100 x K(33)
    # = 1,996,500 units ~ 2,000 kg-eq (2 h of a 1,000 kg/h line).
    #   late_kg_week_weight    200,000 -> ONE TONNE ONE WEEK LATE costs
    #       200 kg-eq = 20 % of the tonne's own fill value per week late. So
    #       a 10 t run slipping one week costs one FFS change, a 30 t order
    #       three, 100 t ten: a small run (<= 10 t) may slip a week to save
    #       ONE major changeover or to join a larger same-SKU run; a large
    #       order (>= 30 t) does not move for fewer than several. Late still
    #       beats never up to 4 weeks late (4 x 20 % < 100 %). Per kg per
    #       week-step: 200 in pass 2, 200,000 in pass 1.
    #   early_kg_week_weight    50,000 -> 1/4 of late = 50 kg-eq = 5 % of the
    #       tonne's fill value per week early (inventory is cheaper than a
    #       missed week). The nearer week wins a contested hour by 5 % per
    #       week -- what the fixed-48h rule got for free and the unbounded
    #       policy lost (W1 fill 86 % vs 53 %) -- and three weeks early
    #       (3 x 5 % = 15 %) still leaves a kg worth making, so pre-building
    #       beats idling on a 4-week horizon. Per kg per week-step: 50 in
    #       pass 2, 50,000 in pass 1. See helpers/solver_rules.py rows
    #       due_week_policy / late_kg_week_weight / early_kg_week_weight.
    #   Opt-in note: 200,000 / 50,000 are the opt-in defaults; late 400,000 /
    #       early 100,000 is the only tested soft setting under which every
    #       late order stayed <= 1 week late (bench 2026-09-04).
    # [scheduler] late_kg_week_weight / early_kg_week_weight (integers >= 0;
    # 0 disables that side's price, not the freedom).
    late_kg_week_weight: int = 200_000
    early_kg_week_weight: int = 50_000
    # Pass-2 makespan coefficient ([scheduler] pass2_makespan_weight, int >=
    # 0, default 1 = unchanged: a pure tiebreaker). Used ONLY in the pass-2
    # fill-exchange objective Minimize(co*100*K + makespan * W + ... -
    # prod_score). 1 kg-eq ~ 1,000 pass-2 units, so a weight of 100,000 makes
    # one hour of shorter plan worth 100 kg-eq = 0.1 t of fill; 1,000,000
    # makes it 1 t (an hour of a 1,000 kg/h line).
    pass2_makespan_weight: int = 1
    # Opt-in legacy one-sided week-0/week-1 gap stitch (fix SB-1 retired it;
    # model_builder reads the flag with getattr). [scheduler] legacy_week_stitch.
    legacy_week_stitch: bool = False
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


def parse_early_fill_hours(value) -> int | None:
    """[scheduler] early_fill_hours -> Params.early_fill_hours.

    None / "unbounded" / "none" / "" -> None (no limit, the plant decision of
    2026-09-04); an int, float or numeric string -> whole hours (>= 0).
    Anything else raises ValueError so a typo cannot silently become a
    policy. Used by phase2_scheduler.params_from_config; the independent
    validator restates the same parse on purpose (it shares no code with the
    model it checks).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"early_fill_hours: expected hours or 'unbounded', got {value!r}")
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "unbounded", "none", "unlimited", "inf"):
            return None
        try:
            value = float(s)
        except ValueError:
            raise ValueError(
                f"early_fill_hours: expected hours or 'unbounded', got {value!r}") from None
    try:
        h = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"early_fill_hours: expected hours or 'unbounded', got {value!r}") from None
    if math.isnan(h) or math.isinf(h) or h < 0:
        raise ValueError(f"early_fill_hours must be >= 0 hours, got {value!r}")
    return int(round(h))


DUE_WEEK_POLICIES = ("soft", "hard")


def parse_due_week_policy(value) -> str:
    """[scheduler] due_week_policy -> Params.due_week_policy.

    None / "" / missing -> "hard" (the shipped default since 2026-09-04
    night; "soft" is the opt-in of plant decision 2026-09-04 #2, see
    Params); "soft" / "hard" (any case) -> as given. Anything else raises
    ValueError so a typo cannot silently become a policy. The independent
    validator restates the same parse on purpose (it shares no code with the
    model).
    """
    if value is None:
        return "hard"
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(
            f"due_week_policy: expected 'soft' or 'hard', got {value!r}")
    s = value.strip().lower()
    if s == "":
        return "hard"
    if s not in DUE_WEEK_POLICIES:
        raise ValueError(
            f"due_week_policy: expected 'soft' or 'hard', got {value!r}")
    return s


def parse_kg_week_weight(value, name: str, default: int) -> int:
    """[scheduler] late_kg_week_weight / early_kg_week_weight -> whole
    fill-reward units per tonne per week-step (>= 0). None / "" -> the
    default; a number or numeric string -> int(round()); junk, negative,
    NaN or a bool raise ValueError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return int(default)
    if isinstance(value, bool):
        raise ValueError(f"{name}: expected a number >= 0, got {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: expected a number >= 0, got {value!r}") from None
    if math.isnan(f) or math.isinf(f) or f < 0:
        raise ValueError(f"{name}: expected a number >= 0, got {value!r}")
    return int(round(f))


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
        # An identifier must never be defaulted (fix SA-12 / audit
        # quality-10): the old fillna(0) turned any unparsable line_id (a
        # line NAME, a shifted column, a header that slipped through) into
        # line 0 = P09, silently declaring that SKU capable on P09 at the
        # foreign line's rate. Raise, naming the rows.
        _lid = pd.to_numeric(cap["line_id"], errors="coerce")
        _bad = cap.index[_lid.isna()].tolist()
        if _bad:
            _ex = ", ".join(
                f"row {i}: line_id={cap.at[i, 'line_id']!r}"
                for i in _bad[:5])
            raise ValueError(
                f"capabilities_rates.csv: {len(_bad)} row(s) with an "
                f"unparsable line_id ({_ex}{', ...' if len(_bad) > 5 else ''})"
                " — an identifier is never defaulted to line 0")
        cap["line_id"] = _lid.astype(int)
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

        Quantity bounds (fix SA-5 / audit C61, 2026-09-03): the model makes
        produced = round(rate) x integer run hours, so the old
        qty_min == qty_max == round(remaining) was satisfiable ONLY when the
        remaining kg happened to be a whole multiple of the rounded rate —
        23,000 kg @ 737 kg/h made Scenario E level 0 structurally INFEASIBLE
        and level 1 then recorded a bogus tonnage_trim. The bounds now
        bracket the remaining kg at whole hours of the locked line's rate:
        qty_min = ir x floor(remaining / ir), qty_max = ir x ceil(remaining /
        ir) with ir = round(rate) (at least one hour when anything remains).
        The raw remaining kg is kept in `qty_remaining` for reporting. When
        the rate is unknown (no line, rate <= 0) the old exact bounds stay.
        """
        name_to_id = {v: k for k, v in self.line_names.items()}
        out: List[dict] = []
        seen_mo: dict[str, int] = {}
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
            qmin = qmax = int(round(remaining))
            rate = float(self.rate.get((line_id, sku)) or 0.0)
            ir = int(round(rate))
            if ir > 0:
                n_lo = int(math.floor(remaining / ir))
                n_hi = int(math.ceil(remaining / ir))
                if n_lo < 1:
                    n_lo = 1  # anything remaining takes at least one hour
                if n_hi < n_lo:
                    n_hi = n_lo
                qmin, qmax = ir * n_lo, ir * n_hi
            # Duplicate MO numbers (fix SA-12 / audit adversarial-10): the
            # same MO on two lines used to yield two orders with the SAME
            # order_id, so mo_changes.csv reported both against one set of
            # blocks. Suffix the repeat (`<mo>#2|CUR`) and warn; the
            # "|CUR" SUFFIX consumers key on (endswith) is preserved and
            # mo_id still carries the bare MO number.
            order_id = f"{mo}|CUR"
            if mo in seen_mo:
                seen_mo[mo] += 1
                order_id = f"{mo}#{seen_mo[mo]}|CUR"
                print(f"[current_mo] WARNING: MO {mo} appears more than once "
                      f"(row {row_i}, line {line_name}); order_id -> "
                      f"{order_id}. Check manprg for a duplicated MO number.")
            else:
                seen_mo[mo] = 1
            out.append(
                dict(
                    order_id=order_id,
                    sku=sku,
                    due_start=due_start,
                    due_end=due_end,
                    qty_min=qmin,
                    qty_max=qmax,
                    qty_remaining=int(round(remaining)),
                    priority=0,  # current MOs outrank new demand
                    is_current_mo=True,
                    mo_id=mo,
                    locked_line=line_id if locked else None,
                    source=str(r.get("source", "manprg")),
                    # The MO's manprg planned start in solver hours (may be
                    # negative for a running MO); absent column -> None.
                    # write_mo_changes reports it as orig_start_h with
                    # orig_start_src = "manprg" (INTEGRATE, agent V handoff).
                    manprg_start_h=(
                        None if pd.isna(r.get("manprg_start_h", float("nan")))
                        else int(round(float(r.get("manprg_start_h"))))),
                )
            )
        return out

    def _parse_demand(self, dem: pd.DataFrame) -> List[dict]:
        """Demand rows -> order dicts.

        Validation (fix SA-12 / audit adversarial-4, 2026-09-03): a malformed
        row used to poison the whole plan silently — a negative qty_target
        gave qty_min -4500 / qty_max -5500 and `prod <= qmax` made the model
        INFEASIBLE at EVERY relax level with no row named; lower_pct >
        upper_pct (qmin > qmax) did the same at level 0 and the ladder then
        "rescued" the run by dropping every order's demand floor. Such rows
        now raise a ValueError naming the row. A qty_target of exactly 0 is
        legitimate (Scenario F netting writes fully-covered weeks as 0) and
        is kept.
        """
        out = []
        for row_i, r in dem.iterrows():
            sku = str(r.get("sku", ""))
            if not sku or sku.lower() == "nan":
                raise ValueError(f"demand_plan.csv row {row_i}: missing 'sku'")
            order_id = str(r.get("order_id", ""))
            if not order_id or order_id.lower() == "nan":
                wk = r.get("week_index")
                if pd.isna(wk):
                    ds = num_or_default(r.get("due_start_hour"), 0)
                    wk = 0 if ds <= 167 else 1
                order_id = f"W{int(wk)}-{sku}"
            where = f"demand_plan.csv row {row_i} ({order_id}, sku {sku})"

            def _num(name: str, v, allow_blank: bool = False):
                if v is None or (isinstance(v, float) and math.isnan(v)) or (
                        isinstance(v, str) and not v.strip()):
                    if allow_blank:
                        return None
                    raise ValueError(f"{where}: {name} is blank")
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{where}: {name}={v!r} is not a number") from None
                if math.isnan(f) or math.isinf(f):
                    raise ValueError(f"{where}: {name}={v!r} is not a number")
                return f

            qty_target = _num("qty_target", r.get("qty_target"),
                              allow_blank=True)
            if qty_target is None:
                qty_target = 0.0
            if qty_target < 0:
                raise ValueError(
                    f"{where}: qty_target={qty_target} is negative")
            lower_pct = _num("lower_pct", r.get("lower_pct"), allow_blank=True)
            upper_pct = _num("upper_pct", r.get("upper_pct"), allow_blank=True)
            qty_min = _num("qty_min", r.get("qty_min"), allow_blank=True)
            qty_max = _num("qty_max", r.get("qty_max"), allow_blank=True)
            if lower_pct is not None and upper_pct is not None:
                if lower_pct < 0 or upper_pct < 0:
                    raise ValueError(
                        f"{where}: lower_pct={lower_pct} / upper_pct="
                        f"{upper_pct} must be >= 0")
                if lower_pct > upper_pct:
                    raise ValueError(
                        f"{where}: lower_pct={lower_pct} > upper_pct="
                        f"{upper_pct} (qty_min would exceed qty_max)")
                qmin = int(math.floor(qty_target * float(lower_pct)))
                qmax = int(math.ceil(qty_target * float(upper_pct)))
            elif qty_min is not None and qty_max is not None:
                qmin = int(qty_min)
                qmax = int(qty_max)
                if qmin < 0 or qmax < 0:
                    raise ValueError(
                        f"{where}: qty_min={qmin} / qty_max={qmax} must be "
                        ">= 0")
                if qmin > qmax:
                    raise ValueError(
                        f"{where}: qty_min={qmin} > qty_max={qmax}")
            else:
                raise ValueError(
                    f"{where}: needs lower_pct/upper_pct or qty_min/qty_max.")
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
