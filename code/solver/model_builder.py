# model_builder.py — CP-SAT model construction for Flowstate Phase 2 scheduler.
# CIP redesign: CIPs as first-class solver intervals; production splits via seg_a/seg_b.

from __future__ import annotations
import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from ortools.sat.python import cp_model

from data_loader import Params, Data, available_hours_line

# ── Week frame ─────────────────────────────────────────────────────────────
# LEGACY Monday-anchor constants (fix SB-1 / audit C19, time-2, modelcore-2,
# bench F2+F3, 2026-09-03). Until 2026-09-03 the model classified weeks with
# these three numbers: due_end <= 167 -> "week 0", due_start >= 168 -> "week
# 1", and every "week 1" order could start at hour 120. That is only right
# when the horizon anchor is a Monday. The live Scenario F frame is anchored
# on the staging day (a Wednesday: W0 = 0-119, W1 = 120-287, W2 = 288-455,
# W3 = 456-503), so W1 fell in NEITHER class, W2 and W3 were both "week 1"
# and both opened at hour 120 — one and two weeks early — and the one-sided
# gap stitch then forced W3 AHEAD of W1 on every line carrying both (live:
# 1.09 kt = 32.5 % of scheduled kg made before its own due week; P11
# W0@48-120 -> W3@120-124 -> W1@124-144). The week frame is now DERIVED from
# the demand windows (week_frame / effective_due_start below); the constants
# stay only for external readers and the opt-in legacy stitch.
WEEK0_END = 167  # LEGACY: due_end_hour <= 167 -> Week 0 (Monday-anchor frame only)
WEEK1_START = 168  # LEGACY: Week-1 orders have due_start >= 168 (Monday-anchor frame only)
WEEK0_FILL_START = 120  # LEGACY: = 168 - EARLY_FILL_HOURS under a Monday anchor
MAX_GAP_W0_W1_HOURS = 1  # legacy_week_stitch only (Params flag, default off)
# LEGACY documented value of the early-fill allowance: until the plant
# decision of 2026-09-04 only the SECOND demand week could reach back into the
# first, by these 48 h (the original "week-1 fills the last two days of
# week-0" intent, 168 - 120). The live rule is Params.early_fill_hours
# (None = unbounded, the default; an integer applies to EVERY demand order);
# set [scheduler] early_fill_hours = 48 to get this allowance back — for all
# later weeks, since the plant sees no reason to privilege the second one.
EARLY_FILL_HOURS = 48


def week_frame(orders) -> dict:
    """Demand-derived week frame (fix SB-1).

    Week boundaries are the sorted distinct `due_start` values of the DEMAND
    orders (current MOs and trials excluded — their windows are plant fact).
    Returns {"starts": [...], "first_end": max due_end of the first week's
    orders (None without demand), "second_start": the second distinct start
    (None with a single week)}. Since the plant decision of 2026-09-04 the
    frame no longer drives the start floors (effective_due_start reads
    Params.early_fill_hours); it still drives the legacy stitch, the
    two-phase boundary (phase2_scheduler._two_phase_boundary) and telemetry.
    """
    demand = [o for o in orders
              if not o.get("is_current_mo") and not o.get("is_trial")]
    starts = sorted({int(o["due_start"]) for o in demand})
    first_end = None
    if starts:
        first_end = max(int(o["due_end"]) for o in demand
                        if int(o["due_start"]) == starts[0])
    return {
        "starts": starts,
        "first_end": first_end,
        "second_start": starts[1] if len(starts) > 1 else None,
    }


def effective_due_start(P: Params, o: dict, frame: Optional[dict] = None) -> int:
    """Hard due-window START floor of an order.

    Plant decision 2026-09-04 ("early fill can go as far into the previous
    weeks as needed, except cannot be placed before an already scheduled
    MO"), replacing fix SB-1's second-week-only 48 h rule:
    * current MOs / trials: their own due_start (plant fact);
    * allow_week1_in_week0 off: due_start (no early fill at all);
    * Params.early_fill_hours None (unbounded, the default): 0 — the due
      window contributes NO start floor; what bounds the start is the
      horizon, the line availability gate, committed downtime windows
      (NoOverlap) and, in Scenario E, the end of every committed
      current-state MO on the line (build_model, committed-MO floor);
    * Params.early_fill_hours = h (integer): max(0, due_start - h) for EVERY
      demand order — the old allowance generalised to all later weeks (the
      plant sees no reason to privilege the second one). h = 48 reproduces
      the pre-decision allowance; h = 0 pins every order to its due_start.
    The due-window END (due_end + 1) stays hard in every case (relax_due
    prices lateness). Hours between this floor and the order's own
    due_start are NOT free: week_dev prices them at
    objective_week_deviation_weight per hour in every mode, and the fill
    week gradient (maximize_production) rewards the nearest due week first
    — together the "right week first" soft preference, so early fill only
    happens into capacity nobody nearer needs. `frame` (week_frame) is
    accepted for signature compatibility and no longer read.
    """
    ds_raw = int(o["due_start"])
    if o.get("is_current_mo") or o.get("is_trial"):
        return ds_raw
    if not getattr(P, "allow_week1_in_week0", False):
        return ds_raw
    efh = getattr(P, "early_fill_hours", None)
    if efh is None:
        return 0
    return max(0, ds_raw - int(efh))


# -- Soft due weeks (OPT-IN: [scheduler] due_week_policy = "soft") -----------
# The shipped default is "hard" (2026-09-04 night): a demand order's due_end
# + 1 is a wall at relax levels 0-1. Soft weeks were the plant's request
# (decision 2026-09-04 #2: "Treat the week numbers on the
# demand_plan_summary.csv as preferential order, but not necessarily the hard
# borders. We want to be able to look 3-4 weeks into the plan, and if a sku
# has tons scheduled in week 1 and week 3, and it makes sense to combine all
# kgs for that sku in one run in week 2, then that should be explored."), but
# benchmarked at 600 s on the live board (bench/results/policy_soft_v2) the
# soft model delivered 2.22 / 2.06 kt on time vs 3.59 / 3.36 kt under hard
# (-38 %), 463 / 602 t late, W1 on-time 52 / 54 % vs 86 / 81 %, changeover
# hours 80.8 / 85.2 vs 73.8 / 73.2, and 6 of 8 discretionary large (>= 30 t)
# late orders saved no changeover: pass-1 search stalls on the larger soft
# model, so the lateness is search residue, not the priced trade described
# below. Hard keeps 0 late kg and still finds 6-9 at-wall consolidations for
# free. The early kg-week price applies under BOTH policies (pre-building is
# decision #1's freedom); the late price and the widened window are soft-only.
#
# Under Params.due_week_policy == "soft" a DEMAND order's due window is a
# preference in BOTH directions: it may start early (decision #1, the
# early-fill policy) and it may finish late, anywhere inside the horizon.
# Every kg placed outside its week is priced per kg per WHOLE WEEK-STEP of
# deviation (WEEK_STEP_H = 168 h): kg produced in the hours before due_start
# count one step, kg more than a week before it two steps, ...; kg produced
# at or after due_end + 1 count one step, a further week later two steps.
# The step is the plant's unit ("tons in week X"), so a kg one hour into the
# next week is as late as a kg six days into it; the per-hour terms
# (objective_late_weight, week_deviation_weight) stay as smooth tiebreakers
# inside a step. The prices are Params.late_kg_week_weight /
# early_kg_week_weight in FILL UNITS per TONNE per step, where 1 kg of tier-1
# fill = 1,000 units -- the pass-2 currency ("fill-equivalent kg", kg-eq:
# 1 kg-eq = 1,000 units = the fill value of one kg). Committed MOs and trials
# keep their own windows hard; the horizon end, the gate and committed
# windows stay hard for everyone.
#
# ONE CURRENCY IN EVERY PASS (v2 pricing, 2026-09-04 evening). The invariant:
# the deviation price is the same FRACTION of the fill value of the same kg
# in every pass. Pass 2 minimizes co*100*K + ... + dev_pen - prod_score with
# one kg of fill ~ 1,000 units, so its coefficient per kg-week is
# weight / 1000. Pass 1 maximizes prod_sum * 1000, one kg of fill = 1e6
# units, so its coefficient per kg-week is weight / 1000 x 1000 = weight
# (PASS1_FILL_SCALE). v1 used weight / 1000 in BOTH passes, which made the
# same price 0.2 % of a kg of fill in pass 1 and 200 % in pass 2: pass 1
# put kg wherever fill was easiest (925 t-weeks early on live_F), then
# pass 2 bought the deviation back with changeovers (co_load 39,350 vs
# 27,570 for the fixed-week solver, utilisation 76.9 vs 83.1 %). Kg-eq
# arithmetic at the defaults (Scenario F weights, K = 33):
#   one FFS change      = (5 + 600) x 100 x K = 1,996,500 units ~ 2,000 kg-eq
#                         (FILL_EXCHANGE_HOURS_PER_FFS h of a 1,000 kg/h line)
#   late 200,000 /t-wk  = 200 kg-eq per tonne-week = 20 % of the tonne's fill
#                         value per week late -> 10 t one week late ~ one FFS
#                         change, 30 t ~ three, 100 t ~ ten; late still beats
#                         never up to 4 weeks late (4 x 20 % < 100 %).
#   early 50,000 /t-wk  = 50 kg-eq per tonne-week = 5 % per week early (1/4
#                         of late: inventory is cheaper than a missed week;
#                         the nearer week wins a contested hour by 5 % per
#                         week, which the fixed-48h rule got for free).
# Non-fill branches (Scenarios A-E) have no fill term; there the changeover
# load is multiplied by co_multiplier instead of 100 x K, so the coefficient
# is weight / 1000 x co_multiplier / (100 K) and the deviation:changeover
# ratio equals pass 2's.
WEEK_STEP_H = 168
KG_PER_TONNE = 1000
# Pass 1 scales fill x1000 (prod_sum * 1000); its deviation price scales with it.
PASS1_FILL_SCALE = 1000


def due_week_policy_of(P: Params) -> str:
    """'hard' (the shipped default) or 'soft' (opt-in) -- tolerant of Params
    objects built before the field existed (tests, older pickles)."""
    pol = str(getattr(P, "due_week_policy", "hard") or "hard").strip().lower()
    return pol if pol in ("soft", "hard") else "hard"


def soft_due_weeks(P: Params, o: dict) -> bool:
    """True when `o`'s due END is a priced preference: a demand order (not a
    committed MO, not a trial) under the soft due-week policy."""
    if o.get("is_current_mo") or o.get("is_trial"):
        return False
    return due_week_policy_of(P) == "soft"


def effective_due_end(P: Params, o: dict, cross_week: bool = False) -> int:
    """EXCLUSIVE end of the hours an order may occupy: due_end + 1 (capped at
    the horizon) under the hard policy, for committed MOs and trials; the
    horizon end for demand orders under the soft due-week policy (their
    lateness is priced, not forbidden) and in cross-week mode."""
    H = int(P.horizon_h)
    if cross_week or soft_due_weeks(P, o):
        return H
    return max(0, min(H, int(o["due_end"]) + 1))


def dev_kg_coefficients(P: Params, co_multiplier: Optional[int] = None,
                        fill_scale: int = 1) -> Tuple[int, int]:
    """(early, late) objective coefficient PER KG PER WEEK-STEP.

    Invariant (v2 pricing): the deviation price is the same FRACTION of the
    fill value of the same kg in every pass. The weights are fill units per
    TONNE per step with 1 kg of fill = 1,000 units (the pass-2 currency), so:

      pass 2 / fill_scale=1     : weight / 1000 per kg-week
                                  (one kg of fill ~ 1,000 units there)
      pass 1 / fill_scale=1000  : weight / 1000 x 1000 = weight per kg-week
                                  (pass 1 maximizes prod_sum * 1000, one kg
                                  of fill = 1e6 units) -- PASS1_FILL_SCALE
      non-fill (co_multiplier)  : weight / 1000 x co_multiplier / (100 K)
                                  (Scenarios A-E: no fill term; the
                                  changeover load carries co_multiplier =
                                  W2 / 100 / 1 instead of pass 2's 100 x K,
                                  so the deviation:changeover ratio is
                                  pass 2's)

    At the defaults (late 200,000, early 50,000; K = 33): pass 2 (50, 200),
    pass 1 (50,000, 200,000) -- 5 % / 20 % of a kg of fill per week in both;
    one FFS change ~ 2,000 kg-eq, so 10 t one week late ~ one FFS change.
    A positive weight never rounds to 0 (a price the solver can see is the
    point). Use dev_kg_coefficients_pass1 / _pass2 for the named forms.
    """
    scale = max(1, int(fill_scale))

    def _per_kg(weight: int) -> int:
        w = max(0, int(weight))
        if w <= 0:
            return 0
        c = w / KG_PER_TONNE
        if co_multiplier is not None:
            c = c * float(co_multiplier) / (100.0 * default_fill_exchange_rate(P))
        else:
            c = c * scale
        return max(1, int(round(c)))
    return (_per_kg(getattr(P, "early_kg_week_weight", 0)),
            _per_kg(getattr(P, "late_kg_week_weight", 0)))


def dev_kg_coefficients_pass1(P: Params) -> Tuple[int, int]:
    """(early, late) per kg-week in the PASS-1 currency (fill x1000):
    weight per kg-week. Default (50,000, 200,000)."""
    return dev_kg_coefficients(P, None, PASS1_FILL_SCALE)


def dev_kg_coefficients_pass2(P: Params) -> Tuple[int, int]:
    """(early, late) per kg-week in the PASS-2 currency (1 kg of fill ~
    1,000 units): weight / 1000 per kg-week. Default (50, 200)."""
    return dev_kg_coefficients(P, None, 1)


def _earliest_cip_fit(data: Data, l: int, t0: int, dur: int, H: int) -> int:
    """First hour >= t0 at which a `dur`-hour CIP overlaps no downtime row of
    line `l` (fix SB-3: used when the line is already past its CIP interval
    at the availability gate, so the clean is pinned as early as the line
    physically allows instead of at a deadline that has already passed)."""
    t = max(0, int(t0))
    limit = max(0, int(H) - int(dur))
    moved = True
    while moved and t < limit:
        moved = False
        for dt in data.downtimes:
            if dt["line_id"] != l:
                continue
            s, e = int(dt["start"]), int(dt["end"])
            if s < t + dur and e > t:
                t = max(t, e)
                moved = True
    return min(t, limit)


def _median_line_rate(data: Data, l: int) -> float:
    """Median capable rate (kg/h) of line `l`; falls back to the median over
    all capable pairs of the plant, then to 1,000 kg/h. Prices one CIP at
    dur x rate kg (fix SB-4)."""
    def _med(vals):
        vals = sorted(v for v in vals if v > 0)
        return float(vals[len(vals) // 2]) if vals else 0.0
    own = _med(float(data.rate.get(k) or 0) for k, cap in data.capable.items()
               if cap and k[0] == l)
    if own > 0:
        return own
    plant = _med(float(data.rate.get(k) or 0) for k, cap in data.capable.items() if cap)
    return plant if plant > 0 else 1000.0


def _blocked_hours_in_window(data: Data, l: int, a: int, b: int) -> float:
    """Hours of [a, b) covered by line `l`'s downtime rows, counted as an
    interval UNION (fix SA-8 / audit adversarial-1, 2026-09-03). The old
    per-row `usable -= overlap` subtracted overlapping or duplicated rows
    once EACH, so two identical [0,200) rows on a 336 h horizon left 0 usable
    hours instead of 136 and the line vanished from the plan (every order a
    dead pair, every floor clamped to 0). Same union rule as
    data_loader.available_hours_line, which fixed the identical bug in the
    per-line budget on 2026-08-14."""
    if b <= a:
        return 0.0
    ivs = []
    for dt in data.downtimes:
        if dt["line_id"] != l:
            continue
        s = max(a, int(dt["start"]))
        e = min(b, int(dt["end"]))
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
    return float(blocked)


def _capable_rate(data: Data, l: int, sku: str) -> float:
    """Rate of (line, sku) for feasibility bounds: > 0 only when the pair is
    flagged capable == 1 AND has a positive rate (fix SA-8 / audit
    adversarial-3 + modelcore-8). With line_rates.csv active,
    data_loader writes the flat line rate onto EVERY pair of the line —
    capable == 0 rows included (kept so trials can look them up) — so a
    rate-only test counted 2,520 non-zero entries against 764 capable pairs
    on the live file and inflated the producible bound ~4x; 105 t of
    unmakeable demand was then reported as a solver shortfall."""
    if not data.capable.get((l, sku)):
        return 0.0
    r = data.rate.get((l, sku)) or 0
    return float(r) if r > 0 else 0.0


def _producible_kg_in_window(P: Params, data: Data, o: dict, lines,
                             mlpo: Optional[int] = None,
                             frame: Optional[dict] = None) -> int:
    """Provable upper bound on what ONE order can produce in its due window.

    Sum over CAPABLE lines of usable hours in [ds_eff, min(H, de+1)] × rate,
    where usable subtracts the line's availability gate and the UNION of its
    downtime windows; when `mlpo` (max_lines_per_order) is given only the
    `mlpo` largest per-line capacities are summed, because an order may use
    at most that many lines (fix SA-11 / adversarial-3).
    ds_eff mirrors the interval construction in build_model exactly
    (effective_due_start: 0 under the unbounded early-fill policy, plant
    decision 2026-09-04; due_start - early_fill_hours when bounded). `frame`
    is accepted for compatibility (week_frame) and no longer needed.
    Contention with other orders and min-run rounding are deliberately
    ignored — the bound must only ever OVER-estimate, so clamping qty_min to
    it removes provably-impossible demand and nothing else.
    """
    H = P.horizon_h
    ds_eff = effective_due_start(P, o, frame)
    # Soft due weeks (2026-09-04 #2): a demand order may finish late inside
    # the horizon, so its window for the bound is [ds_eff, H).
    win_end = effective_due_end(P, o)
    per_line = []
    for l in lines:
        r = _capable_rate(data, l, o["sku"])
        if r <= 0:
            continue
        gate = max(0, data.init_map.get(l, {}).get("available_from", 0))
        a = max(0, ds_eff, gate)
        b = win_end
        if b <= a:
            continue
        usable = float(b - a) - _blocked_hours_in_window(data, l, a, b)
        if usable > 0:
            per_line.append(usable * r)
    per_line.sort(reverse=True)
    if mlpo is not None and mlpo > 0:
        per_line = per_line[:mlpo]
    return int(sum(per_line))


def _usable_hours_on_line(P: Params, data: Data, l: int,
                          ds_eff: int, de: int) -> float:
    """Hours line `l` can actually host work inside [ds_eff, de+1]:
    window minus the availability gate and the UNION of downtime overlaps.
    Zero means the pair is DEAD — no feasible schedule can place this order
    there."""
    b = min(P.horizon_h, de + 1)
    gate = max(0, data.init_map.get(l, {}).get("available_from", 0))
    a = max(0, ds_eff, gate)
    if b <= a:
        return 0.0
    usable = float(b - a) - _blocked_hours_in_window(data, l, a, b)
    return max(0.0, usable)


# ── Pass-2 fill exchange rate (fix SA-3 / audit C73, F4) ──────────────────
#
# Pass 2 of the two-pass Scenario F solve minimizes the weighted changeover
# load. Until 2026-09-03 the only thing holding fill was a GLOBAL floor at
# (1 - eps) x pass-1's score: the whole epsilon was fungible, so pass 2 spent
# it on cosmetics (E2: 100 t trimmed to 99 t to end 1 h earlier with NOTHING
# to save; E3b: a 2 t sole-line order deleted to save one changeover). The
# repaired pass 2 (a) floors every order at min(pass-1 produced, target) and
# (b) puts fill back into the objective at an explicit exchange rate K:
#
#     Minimize  co_min*100*K + makespan + idle*W_idle + cip_cost
#               + late*W_late + week_pen - prod_score
#
# so one pass-2 changeover unit (co_min x 100) is worth exactly K fill units
# and 1 fill unit = 1 kg x tier weight (1000 + week gradient, ~1010).
# Calibration (documented in helpers/solver_rules.py): ONE FFS change under
# the Scenario F weights costs base 5 + ffs 600 = 605 -> 60,500 pass-2 units;
# the plant values that change at about 2 h of a 1,000 kg/h line = 2,000 kg
# x ~1,010 = 2,020,000 fill units -> K = 2,020,000 / 60,500 = 33.4 -> 33.
# The helper derives K from the live weights the same way so the rate keeps
# meaning "one FFS change ~ 2 h of line time" whatever [changeover] says.
FILL_EXCHANGE_HOURS_PER_FFS = 2.0     # plant rule: one FFS change ~ 2 h
FILL_EXCHANGE_REF_RATE_KGPH = 1000.0  # of a 1,000 kg/h line
FILL_EXCHANGE_TIER_WEIGHT = 1010      # kg -> fill units (1000 base + ~1 week gradient)
# Fix SB-4: one CIP costs dur x median line rate kg, priced at the tier-1 base
# weight per kg (see the CIP count cost block in build_model).
CIP_COST_FILL_UNITS_PER_KG = 1000


def default_fill_exchange_rate(P: Params) -> int:
    """K such that one FFS change (co base + ffs weight, x100 in pass 2) is
    worth FILL_EXCHANGE_HOURS_PER_FFS hours of a 1,000 kg/h line in fill
    units. Under the Scenario F weights (base 5, ffs 600) this is 33; under
    the Params defaults (5 + 10) it is 1,347. Never below 1."""
    ffs_change = 100 * max(1, int(P.co_base_weight) + int(P.co_ffs_weight))
    kg_equiv = FILL_EXCHANGE_HOURS_PER_FFS * FILL_EXCHANGE_REF_RATE_KGPH
    return max(1, int(round(kg_equiv * FILL_EXCHANGE_TIER_WEIGHT / ffs_change)))


def build_model(
    P: Params,
    data: Data,
    phase: str,
    relax_demand: bool,
    ignore_co: bool,
    max_lines_per_order_override: Optional[int] = None,
    maximize_production: bool = False,
    objective_mode: str = "balanced",
    relax_due: bool = False,
    cross_week: bool = False,
    cip_flex: bool = False,
    min_prod_score: Optional[int] = None,
    order_floors: Optional[Dict[int, int]] = None,
    fill_exchange_rate: Optional[int] = None,
) -> Tuple[cp_model.CpModel, Dict[str, Any]]:
    """min_prod_score (two-pass CO minimization, 2026-08-17): when set —
    soft-demand + maximize_production only — the weighted fill score
    (tier-1 production incl. week gradient) becomes a HARD floor and the
    objective flips to MINIMIZING the weighted changeover load. Pass 1
    maximizes fill; pass 2 re-solves holding >= (1-eps) of that fill and
    buys back changeovers the flat trade never could.

    order_floors / fill_exchange_rate (fix SA-3, 2026-09-03; audit C73/F4):
    the pass-2 repair. `order_floors` maps order INDEX -> kg floor (the
    caller passes min(pass-1 produced, qty_target)); each entry becomes
    `produced[o_idx] >= floor`, so pass 2 may resequence but never delete an
    order or shave its tail. `fill_exchange_rate` K puts fill back into the
    pass-2 objective: Minimize(co_min*100*K + makespan + idle*W_idle +
    cip_cost + late*W_late + week_pen - prod_score); see
    default_fill_exchange_rate for the calibration. Both default to None,
    which reproduces the pre-fix behaviour exactly (min_prod_score alone).
    """
    model = cp_model.CpModel()
    orders = data.orders
    lines = data.lines
    H = P.horizon_h
    mlpo = (
        P.max_lines_per_order
        if max_lines_per_order_override is None
        else max_lines_per_order_override
    )
    soft_demand_on = bool(getattr(P, "soft_demand", False))
    # Demand-derived week frame (fix SB-1): shared by the producible clamp,
    # the start floors and the legacy stitch.
    wk_frame = week_frame(orders)

    # ── Demand floors, clamped to provable window capacity (computed ONCE,
    # up front, so the run-bound block below and the produced/demand block
    # further down see the SAME number; fix SA-9 / audit C62, modelcore-4).
    #
    # An order with NO capable, available line (e.g. 570560/280698 absent
    # from capabilities, or one only fitting a fully-down line) can never
    # be produced — zero its floor so it reports "short of qmin" instead of
    # making the whole model INFEASIBLE.
    # Producible-zeroing v2 (2026-08-14, user-approved): the bool above
    # only catches TOTAL impossibility. A demand order whose qty_min
    # exceeds what its capable lines can physically produce INSIDE its
    # due window (gates + downtimes subtracted) is just as provably
    # impossible and used to turn the whole level-0 model INFEASIBLE —
    # the auto-relax ladder then lands on ignore_co and changeovers stop
    # being optimized at all (measured: +82 unoptimized changeovers on
    # the 2026-08-14 dataset). Clamp qty_min to the per-order window
    # capacity. The bound ignores contention and min-run rounding, so it
    # OVER-estimates what one order could get — clamping to it can never
    # exclude a feasible solution, only demands no schedule could meet.
    # Committed MOs and trials keep the legacy behavior untouched
    # (dispatch-5 contracts; manprg is ground truth).
    # 2026-09-03 (SA-8/SA-11): the bound now requires capable == 1 (not
    # just rate > 0), unions overlapping downtimes and keeps at most
    # max_lines_per_order lines.
    qmin_clamped: Dict[int, int] = {}   # demand qmin after the window clamp (relax ignored)
    qmin_floor: Dict[int, int] = {}     # the floor actually enforced (0 under relax_demand)
    for o_idx, o in enumerate(orders):
        is_cmo_or_trial = bool(o.get("is_current_mo") or o.get("is_trial"))
        if is_cmo_or_trial:
            # Legacy: any positive rate on an available line makes it
            # producible (manprg is ground truth; capabilities may disagree).
            producible = any(
                (data.rate.get((l, o["sku"])) or 0) > 0
                and available_hours_line(P, data, l) > 0
                for l in lines)
        else:
            producible = any(
                _capable_rate(data, l, o["sku"]) > 0
                and available_hours_line(P, data, l) > 0
                for l in lines)
        q_demand = int(o["qty_min"]) if producible else 0
        if q_demand > 0 and not is_cmo_or_trial:
            cap_kg = _producible_kg_in_window(P, data, o, lines, mlpo=mlpo,
                                              frame=wk_frame)
            if cap_kg < q_demand:
                print(f"[producible] {o['order_id']}: qty_min {q_demand} -> "
                      f"{cap_kg} (window capacity: capable lines x usable "
                      f"hours in [{int(o['due_start'])},{int(o['due_end'])}+1], "
                      f"at most {mlpo} lines)")
                q_demand = cap_kg
        qmin_clamped[o_idx] = q_demand
        qmin_floor[o_idx] = 0 if relax_demand else q_demand

    # ── Per (line, order) decision variables ──────────────────────────────
    #
    # Each assignment gets TWO optional interval segments:
    #   seg_a  – primary production block (present iff order is on this line)
    #   seg_b  – optional continuation after a CIP splits the run (same SKU)
    #
    # A CIP can land between seg_a and seg_b without charging a changeover
    # because the line resumes the same SKU immediately after the CIP.

    dead_pairs: set = set()  # (l, o_idx) provably unusable — pruned
    present = {}        # BoolVar: order assigned to this line
    run_h = {}          # IntVar: total run hours on this line (seg_a + seg_b)
    seg_a_run = {}
    seg_a_start = {}
    seg_a_end = {}
    seg_a_interval = {}
    seg_b_present = {}  # BoolVar: continuation segment after CIP
    seg_b_run = {}
    seg_b_start = {}
    seg_b_end = {}
    seg_b_interval = {}
    eff_end = {}        # IntVar: effective order end (seg_b_end or seg_a_end)
    lateness = {}       # IntVar: hours past due_end+1 (relax_due, or soft due weeks)
    week_dev = {}       # (early, late) IntVars: AZAP-week deviation (cross_week)
    # Kg-week deviation (soft due weeks, 2026-09-04 #2): per (line, order) the
    # hour vars of each early / late week-step; ir_dev holds the integer rate
    # that turns them into kg. Coefficients per kg-step in the fill currency.
    early_steps: Dict[Tuple[int, int], list] = {}
    late_steps: Dict[Tuple[int, int], list] = {}
    ir_dev: Dict[Tuple[int, int], int] = {}
    # Pass-2 currency coefficients decide WHICH steps to build (a zero weight
    # builds none); the pass-1 (x1000) pair is exposed beside them.
    c_early_raw, c_late_raw = dev_kg_coefficients_pass2(P)
    c_early_p1, c_late_p1 = dev_kg_coefficients_pass1(P)
    avail_l_of = {l: max(0, int(data.init_map.get(l, {}).get("available_from", 0)))
                  for l in lines}

    for l in lines:
        for o_idx, o in enumerate(orders):
            key = (l, o_idx)
            oid = o["order_id"]
            present[key] = model.NewBoolVar(f"present_l{l}_o{oid}")
            ds_raw, de = int(o["due_start"]), int(o["due_end"])
            # Due-window start floor (effective_due_start has the rule):
            # plant decision 2026-09-04 — 0 under the default unbounded
            # early-fill policy (only the gate / committed work bound the
            # start), due_start - early_fill_hours when the knob is set.
            ds_eff = effective_due_start(P, o, wk_frame)
            # Due-window END (exclusive): due_end + 1 under the hard policy,
            # for committed MOs and trials; the horizon end for demand orders
            # under the soft due-week policy (plant decision 2026-09-04 #2 —
            # lateness is priced per kg-week below, never forbidden).
            de_win = effective_due_end(P, o, cross_week)
            soft_late = soft_due_weeks(P, o) and not cross_week
            max_len = max(0, de_win - max(0, ds_eff))
            # Dead-pair pruning (2026-08-14): a normal order whose window on
            # this line is fully eaten by the gate/downtimes can NEVER run
            # here. Forcing absence up front (and excluding the pair from the
            # changeover web below) removes thousands of interval + pairwise
            # vars — in fill mode most lines are gated deep into the horizon
            # and the search was drowning (600s moved W35 fill by only 50t).
            # Under the soft policy the window is [ds_eff, H), so only a pair
            # whose line is blocked to the horizon is dead.
            if (not o.get("is_current_mo") and not o.get("is_trial")
                    and not relax_due
                    and _usable_hours_on_line(P, data, l, max(0, ds_eff), de_win - 1) <= 0):
                dead_pairs.add(key)
            run_h[key] = model.NewIntVar(
                0, max(H, max_len), f"runh_l{l}_o{oid}"
            )

            # seg_a: primary segment (present iff order on this line)
            seg_a_run[key] = model.NewIntVar(
                0, max(H, max_len), f"saR_l{l}_o{oid}"
            )
            seg_a_start[key] = model.NewIntVar(0, H, f"saS_l{l}_o{oid}")
            seg_a_end[key] = model.NewIntVar(0, H, f"saE_l{l}_o{oid}")
            seg_a_interval[key] = model.NewOptionalIntervalVar(
                seg_a_start[key],
                seg_a_run[key],
                seg_a_end[key],
                present[key],
                f"saI_l{l}_o{oid}",
            )

            # seg_b: optional continuation after CIP (same SKU, no changeover)
            seg_b_present[key] = model.NewBoolVar(f"sbP_l{l}_o{oid}")
            seg_b_run[key] = model.NewIntVar(
                0, max(H, max_len), f"sbR_l{l}_o{oid}"
            )
            seg_b_start[key] = model.NewIntVar(0, H, f"sbS_l{l}_o{oid}")
            seg_b_end[key] = model.NewIntVar(0, H, f"sbE_l{l}_o{oid}")
            seg_b_interval[key] = model.NewOptionalIntervalVar(
                seg_b_start[key],
                seg_b_run[key],
                seg_b_end[key],
                seg_b_present[key],
                f"sbI_l{l}_o{oid}",
            )

            # Effective end: seg_b_end when split, seg_a_end otherwise
            eff_end[key] = model.NewIntVar(0, H, f"effE_l{l}_o{oid}")
            model.Add(eff_end[key] == seg_b_end[key]).OnlyEnforceIf(
                seg_b_present[key]
            )
            model.Add(eff_end[key] == seg_a_end[key]).OnlyEnforceIf(
                seg_b_present[key].Not()
            )

            # Linking constraints
            model.AddImplication(seg_b_present[key], present[key])
            model.Add(seg_a_run[key] + seg_b_run[key] == run_h[key])
            model.Add(seg_b_start[key] >= seg_a_end[key]).OnlyEnforceIf(
                seg_b_present[key]
            )
            model.Add(seg_b_run[key] == 0).OnlyEnforceIf(
                seg_b_present[key].Not()
            )
            # Anchor free variables when not present (keeps eff_end / makespan correct)
            model.Add(seg_a_end[key] == 0).OnlyEnforceIf(present[key].Not())

            # Due window
            # Start stays hard in all modes. When relax_due is set, the
            # window END becomes soft: the order may finish up to
            # de + 1 + lateness, with lateness penalized in the objective.
            if cross_week:
                # ── Cross-week mode ──────────────────────────────────
                # AZAP's week becomes a weighted PREFERENCE instead of a
                # hard wall: the order may run anywhere inside the full
                # 0..H horizon (the horizon bounds themselves stay hard,
                # enforced by the IntVar domains above).  Deviation from
                # the AZAP window - starting earlier than ds_eff or
                # finishing later than de+1 - is charged in the objective
                # at P.objective_week_deviation_weight per hour, so the
                # solver only moves an order when the changeover /
                # campaign saving outweighs the deviation cost.
                # Demand bounds (qty_min/qty_max) are NOT touched here:
                # moving WHEN something runs is allowed, not making it is
                # not.
                early_max = max(0, ds_eff)
                late_max = max(0, H - (de + 1))
                early_v = model.NewIntVar(
                    0, early_max, f"wkEarly_l{l}_o{oid}"
                )
                late_v = model.NewIntVar(
                    0, late_max, f"wkLate_l{l}_o{oid}"
                )
                model.Add(early_v == 0).OnlyEnforceIf(present[key].Not())
                model.Add(late_v == 0).OnlyEnforceIf(present[key].Not())
                model.Add(
                    early_v >= ds_eff - seg_a_start[key]
                ).OnlyEnforceIf(present[key])
                model.Add(
                    late_v >= seg_a_end[key] - (de + 1)
                ).OnlyEnforceIf(present[key])
                model.Add(
                    late_v >= seg_b_end[key] - (de + 1)
                ).OnlyEnforceIf(seg_b_present[key])
                week_dev[key] = (early_v, late_v)
                continue_due_block = False
            else:
                model.Add(
                    seg_a_start[key] >= ds_eff
                ).OnlyEnforceIf(present[key])
                continue_due_block = True
                # Fix SB-1 (bench F2): early fill is PRICED in the default
                # mode too. Hours the order runs before its own due_start
                # (inside the [ds_eff, ds_raw) early-fill window — the whole
                # horizon before due_start under the unbounded policy) cost
                # objective_week_deviation_weight per hour, so pulling next
                # week's order into this week is a decision with a price
                # instead of a free move: the "right week first" soft
                # preference (plant decision 2026-09-04 keeps it). The end
                # wall above stays hard, so the late leg is a constant 0
                # here (cross_week prices it).
                if ds_eff < ds_raw:
                    early_v = model.NewIntVar(
                        0, ds_raw - ds_eff, f"wkEarly_l{l}_o{oid}"
                    )
                    model.Add(early_v == 0).OnlyEnforceIf(present[key].Not())
                    model.Add(
                        early_v >= ds_raw - seg_a_start[key]
                    ).OnlyEnforceIf(present[key])
                    week_dev[key] = (early_v, 0)

            if not continue_due_block:
                pass
            elif relax_due or soft_late:
                # Soft due END. relax_due (ladder level 2) prices every
                # order's lateness per hour; the soft due-week policy (plant
                # decision 2026-09-04 #2) opens the same lateness for DEMAND
                # orders at every level and prices it per kg per week-step
                # below (late_kg_week_weight) on top of the per-hour term —
                # so under "soft" level 2 adds nothing for demand orders and
                # only still relaxes committed MOs' windows.
                late_max = max(0, H - (de + 1))
                lateness[key] = model.NewIntVar(
                    0, late_max, f"late_l{l}_o{oid}"
                )
                model.Add(lateness[key] == 0).OnlyEnforceIf(present[key].Not())
                model.Add(
                    seg_a_end[key] <= de + 1 + lateness[key]
                ).OnlyEnforceIf(present[key])
                model.Add(
                    seg_b_end[key] <= de + 1 + lateness[key]
                ).OnlyEnforceIf(seg_b_present[key])
            else:
                model.Add(seg_a_end[key] <= de + 1).OnlyEnforceIf(present[key])
                model.Add(seg_b_end[key] <= de + 1).OnlyEnforceIf(
                    seg_b_present[key]
                )

            # Trial orders: pinned line, fixed start/end, CIP can split
            if o.get("is_trial"):
                tl = o["trial_line"]
                if l == tl:
                    model.Add(present[key] == 1)
                    model.Add(
                        seg_a_start[key] == o["trial_start_hour"]
                    )
                    # Fix effective end (seg_a_end or seg_b_end)
                    model.Add(
                        eff_end[key] == o["trial_end_hour"]
                    )
                    # Pin total production hours when computed from
                    # target_kgs (trial_run_hours != None).  This
                    # prevents the solver from shrinking the trial to
                    # min_run_hours and leaving a huge idle gap.
                    trial_run = o.get("trial_run_hours")
                    if trial_run is not None:
                        model.Add(run_h[key] == trial_run)
                    # Allow CIP to split: seg_b determined by solver
                    # Per-segment minimums (avoid short stubs)
                    model.Add(
                        seg_a_run[key] >= P.min_run_hours
                    ).OnlyEnforceIf(present[key])
                    model.Add(
                        seg_b_run[key] >= P.min_run_hours
                    ).OnlyEnforceIf(seg_b_present[key])
                else:
                    model.Add(present[key] == 0)
                    model.Add(run_h[key] == 0)
                continue  # skip normal capability / run-bound logic

            # Current-state MOs locked to their manprg line (already in VIF):
            # the solver may not shift them, but may split/reorder/trim them.
            # Presence is REQUIRED at every relax level — committed work must
            # appear; only the tonnage is adjustable. The auto-relax ladder
            # skips levels 1-2 (changeovers + relaxed demand drop MOs) and
            # jumps straight to level 3 (ignore_co) which is fast and keeps
            # every MO on its locked line.
            is_current = bool(o.get("is_current_mo"))
            if is_current:
                if o.get("locked_line") is None or l != o["locked_line"]:
                    model.Add(present[key] == 0)
                    model.Add(run_h[key] == 0)
                    continue
                # On the locked line presence is forced, then we FALL THROUGH
                # to the run-bound block below so the current-MO min-run floor
                # (see the comment at the `is_current` branch there) and the
                # per-segment seg_a / seg_b minimums actually apply. Committed
                # work must not be chopped into short stubs.
                model.Add(present[key] == 1)

            if key in dead_pairs:
                model.Add(present[key] == 0)
                model.Add(run_h[key] == 0)
                continue

            # Capability / run bounds
            r = data.rate.get((l, o["sku"]))
            cap = data.capable.get((l, o["sku"]))
            ir_dev[key] = int(round(r)) if (r is not None and r > 0) else 0
            # Current-state MOs skip this gate on purpose: manprg is ground
            # truth -- the plant is physically running that (line, sku) pair,
            # so a capabilities table that disagrees is the thing that is
            # wrong. Zeroing a committed MO here would silently drop it; the
            # app's capability check surfaces the data defect instead.
            if not is_current and (
                (cap is None) or (cap == 0) or (r is None) or (r <= 0)
            ):
                model.Add(present[key] == 0)
                model.Add(run_h[key] == 0)
            else:
                qmin = int(o["qty_min"])
                # Current-state MOs have adjustable tonnage (may be trimmed
                # toward demand); a min-run derived from the FULL remaining
                # kg (e.g. 171h for a 184t MO) would make the MO unpresentable
                # inside a gated/CIP'd window and the solver skips it. For
                # those, the floor is just the global min_run_hours.
                if is_current:
                    # Current MOs: qty_min/qty_max bracket the remaining kg
                    # at whole hours of the locked line's rate
                    # (data_loader._parse_current_mo, fix SA-5): prod is
                    # pinned to (about) the remaining kg, so a floor above
                    # remaining/rate is unsatisfiable (integer run hours x
                    # rate > remaining -> INFEASIBLE at every relax level,
                    # dispatch 5 A/B). The floor applies only when the
                    # remaining work physically supports it.
                    max_hours_qty = (qmin / r) if (r is not None and r > 0) else 0.0
                    if max_hours_qty >= P.min_run_hours:
                        min_run = min(max_len, max(1, P.min_run_hours))
                    else:
                        min_run = 0
                else:
                    # min_run_pct_of_qty (fix SA-9 / audit C62): the share
                    # floor used the FULL demand qmin, so an order whose
                    # window capacity was clamped below qmin (or whose
                    # capable lines each hold less than pct x qmin) could not
                    # be placed on ANY line and produced nothing. The floor
                    # now uses the window-clamped qmin. Under soft demand
                    # (Scenario F) partial fills are the whole point — every
                    # kg counts, none is owed — so only min_run_hours applies
                    # there (documented in helpers/solver_rules.py).
                    if soft_demand_on:
                        min_run_from_pct = 0
                    else:
                        min_run_from_pct = (
                            math.ceil(P.min_run_pct_of_qty
                                      * qmin_clamped[o_idx] / r)
                            if r > 0 else 0
                        )
                    min_run = min(
                        max_len, max(1, P.min_run_hours, min_run_from_pct)
                    )
                model.Add(run_h[key] >= min_run).OnlyEnforceIf(present[key])
                model.Add(run_h[key] == 0).OnlyEnforceIf(present[key].Not())
                # Per-segment minimums (avoid wasteful short stubs) -- normal
                # orders only. For current-state MOs the segment floors
                # over-constrain against CIP-split geometry (a forced split can
                # leave < 4h on one side of a clean, which made the model
                # INFEASIBLE at every relax level -- dispatch 5 A/B). The
                # ORDER-level run_h floor above already guarantees committed
                # work runs >= min_run_hours in total, which is the
                # stub-prevention contract.
                if not is_current:
                    model.Add(
                        seg_a_run[key] >= min(P.min_run_hours, max_len)
                    ).OnlyEnforceIf(present[key])
                    model.Add(
                        seg_b_run[key] >= min(P.min_run_hours, max_len)
                    ).OnlyEnforceIf(seg_b_present[key])

                # ── Kg-week deviation (plant decision 2026-09-04 #2) ────
                # Kg of this order produced on this line outside its due
                # week, graded in whole week-steps, over the order's span
                # [seg_a_start, eff_end) on the line (a CIP split inside the
                # span over-counts its 6 h once — accepted, documented).
                #   early step k (k = 1..): kg in hours < ds_raw - 168(k-1)
                #     = rate x max(0, min(eff_end, T_k) - seg_a_start)
                #   late step k: kg in hours >= de + 1 + 168(k-1)
                #     = rate x max(0, eff_end - max(seg_a_start, T_k))
                # Each var is pinned EXACTLY (min/max equalities), so the
                # telemetry reads true kg whatever the objective branch. Only
                # steps whose threshold lies inside the pair's window are
                # built; a zero weight builds none.
                if (not is_current and not cross_week and ir_dev[key] > 0):
                    if c_early_raw > 0 and ds_eff < ds_raw:
                        k = 1
                        while True:
                            T = ds_raw - WEEK_STEP_H * (k - 1)
                            if T <= max(0, ds_eff, avail_l_of[l]):
                                break
                            m_v = model.NewIntVar(0, T, f"devEm_l{l}_o{oid}_k{k}")
                            model.AddMinEquality(m_v, [eff_end[key], T])
                            hb = model.NewIntVar(0, T, f"devEh_l{l}_o{oid}_k{k}")
                            model.AddMaxEquality(hb, [m_v - seg_a_start[key], 0])
                            model.Add(hb == 0).OnlyEnforceIf(present[key].Not())
                            early_steps.setdefault(key, []).append(hb)
                            k += 1
                    if c_late_raw > 0 and soft_late:
                        k = 1
                        while True:
                            T = de + 1 + WEEK_STEP_H * (k - 1)
                            if T >= H:
                                break
                            M_v = model.NewIntVar(T, H, f"devLm_l{l}_o{oid}_k{k}")
                            model.AddMaxEquality(M_v, [seg_a_start[key], T])
                            ha = model.NewIntVar(0, H - T, f"devLh_l{l}_o{oid}_k{k}")
                            model.AddMaxEquality(ha, [eff_end[key] - M_v, 0])
                            model.Add(ha == 0).OnlyEnforceIf(present[key].Not())
                            late_steps.setdefault(key, []).append(ha)
                            k += 1

    # Kg-week deviation totals (plain int 0 when nothing is priced).
    early_kg_weeks = (
        sum(ir_dev[key] * hb for key, hbs in early_steps.items() for hb in hbs)
        if early_steps else 0)
    late_kg_weeks = (
        sum(ir_dev[key] * ha for key, has in late_steps.items() for ha in has)
        if late_steps else 0)
    # Kg late at all (step 1 only) per (line, order) — for the report.
    late_kg_by_pair = {key: ir_dev[key] * has[0] for key, has in late_steps.items()}
    early_kg_by_pair = {key: ir_dev[key] * hbs[0] for key, hbs in early_steps.items()}

    # ── NoOverlap prep: collect intervals per line ────────────────────────
    line_intervals = {l: [] for l in lines}
    for l in lines:
        for o_idx in range(len(orders)):
            key = (l, o_idx)
            line_intervals[l].append(seg_a_interval[key])
            line_intervals[l].append(seg_b_interval[key])
    # Downtime windows are COALESCED per line before they become fixed
    # intervals (fix SA-8 / audit adversarial-1, 2026-09-03): two overlapping
    # or duplicated rows on one line are both fixed, so NoOverlap saw two
    # immovable intervals sharing hours and the whole model was INFEASIBLE
    # — the union is the physical fact (the line is down once).
    dt_by_line: Dict[int, list] = {}
    for dt in data.downtimes:
        l = dt["line_id"]
        if l in line_intervals:
            s = max(0, int(dt["start"]))
            e = min(H, int(dt["end"]))
            if e > s:
                dt_by_line.setdefault(l, []).append((s, e))
    for l, ivs in dt_by_line.items():
        merged: list = []
        for s, e in sorted(ivs):
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for s, e in merged:
            d = e - s
            s_var = model.NewIntVar(s, s, f"dt_s_l{l}_{s}_{e}")
            e_var = model.NewIntVar(e, e, f"dt_e_l{l}_{s}_{e}")
            line_intervals[l].append(
                model.NewIntervalVar(s_var, d, e_var, f"DT_l{l}_{s}_{e}")
            )
    # NOTE: AddNoOverlap called AFTER CIP intervals are added (see below)

    # ── Produced quantity & demand bounds ──────────────────────────────────
    produced = {}
    shortfall_terms = []  # soft-demand slack vars (Scenario F)
    for o_idx, o in enumerate(orders):
        prod = model.NewIntVar(0, 10**9, f"produced_{o['order_id']}")
        terms = []
        for l in lines:
            r = data.rate.get((l, o["sku"]))
            if r is None or r <= 0:
                continue
            ir = int(round(r))
            terms.append(ir * run_h[(l, o_idx)])
        if terms:
            model.Add(prod == sum(terms))
        else:
            model.Add(prod == 0)
        produced[o_idx] = prod
        # Demand floor: producible check + window clamp, computed once up
        # front (see qmin_floor above; fix SA-9).
        qmin = qmin_floor[o_idx]
        qmax = int(o["qty_max"])
        # Pass-2 per-order floors (fix SA-3): never below what pass 1 made
        # up to the target. Capped at qmax so a caller rounding error can
        # never make the model INFEASIBLE by itself.
        if order_floors and o_idx in order_floors:
            fl = int(order_floors[o_idx])
            if fl > 0:
                model.Add(prod >= min(fl, qmax))
        if (getattr(P, "soft_demand", False) and qmin > 0
                and not o.get("is_current_mo") and not o.get("is_trial")):
            # Soft demand (Scenario F): the solver may fall short of qty_min,
            # but EVERY missing kg costs objective_shortfall_weight — filling
            # W34/W35 always beats leaving them empty (the relax ladder's
            # all-or-nothing qty_min=0 removed any incentive to produce, so
            # later weeks stayed empty; measured 2026-08-14).
            short_v = model.NewIntVar(0, qmin, f"short_{o['order_id']}")
            model.Add(prod + short_v >= qmin)
            shortfall_terms.append(short_v)
        else:
            model.Add(prod >= qmin)
        model.Add(prod <= qmax)
        # Trials are always pinned to exactly 1 line; skip mlpo constraint
        if not o.get("is_trial"):
            model.Add(sum(present[(l, o_idx)] for l in lines) <= mlpo)

    # Soft demand: bias the SEARCH toward assigning fill orders (present=1
    # first). The objective already makes filling pay; without this hint
    # CP-SAT's default search settles on a sparse incumbent early and
    # improves glacially (measured: W35 stuck ~240-300t of 1,364t across
    # 300s/600s/pruned runs). Fixed search only guides the first solutions -
    # optimality semantics are untouched.
    if getattr(P, "soft_demand", False):
        fill_present = [
            present[(l, o_idx)]
            for l in lines
            for o_idx, o in enumerate(orders)
            if (l, o_idx) in present and (l, o_idx) not in dead_pairs
            and not o.get("is_current_mo") and not o.get("is_trial")
        ]
        if fill_present:
            model.AddDecisionStrategy(
                fill_present,
                cp_model.CHOOSE_FIRST,
                cp_model.SELECT_MAX_VALUE,
            )

    # Soft-demand penalty (0 when the mode is off or nothing is short).
    shortfall_pen = model.NewIntVar(0, 10**11, "shortfall_pen")
    if shortfall_terms:
        model.Add(shortfall_pen == sum(shortfall_terms)
                  * int(getattr(P, "objective_shortfall_weight", 1)))
    else:
        model.Add(shortfall_pen == 0)

    # -- Line availability floor (ALWAYS enforced) -------------------------
    # initial_states.available_from_hour is when a line is genuinely free:
    # the end of the MO it is currently running plus anything already queued
    # behind it (see code/helpers/current_state.py). Nothing may be scheduled
    # on the line before that hour.
    #
    # This used to be applied ONLY inside the changeover block below, which is
    # skipped when phase not in (sanity3, full) or when ignore_co is set. Relax
    # level 3 turns ignore_co on, so escalating the ladder silently unlocked
    # every running MO and the solver happily planned over work in progress
    # (measured: 12/12 lines started before their gate). The floor is a
    # physical fact, not a changeover preference, so it is enforced here
    # unconditionally at every relax level.
    for l in lines:
        avail_l = int(data.init_map.get(l, {}).get("available_from", 0))
        if avail_l <= 0:
            continue
        for o_idx, o in enumerate(orders):
            key = (l, o_idx)
            if key not in seg_a_start:
                continue
            if o.get("is_trial"):
                continue  # trials are pinned to an explicit start hour
            model.Add(
                seg_a_start[key] >= avail_l
            ).OnlyEnforceIf(present[key])
            if key in seg_b_start:
                model.Add(
                    seg_b_start[key] >= avail_l
                ).OnlyEnforceIf(present[key])

    # -- Committed-MO floor (ALWAYS enforced; plant decision 2026-09-04) ----
    # "Early fill can go as far into the previous weeks as needed, except
    # cannot be placed before an already scheduled MO." In Scenario F the
    # scheduled MOs arrive as the availability gate + committed downtime
    # windows (both hard above / in NoOverlap). In Scenario E the running and
    # queued MOs are ORDERS locked to their line (is_current_mo), and with the
    # unbounded early-fill policy a demand order could otherwise slip in
    # front of them. So on every line each DEMAND order must start at or
    # after the effective end of every committed MO present on that line.
    # Gated on BOTH presences (an absent order's interval collapses to 0 —
    # see the changeover block for the same trap). Trials keep their pinned
    # window; MO-vs-MO order stays free (manprg's own sequence rules).
    for l in lines:
        cmo_idxs = [
            c_idx for c_idx, c in enumerate(orders)
            if c.get("is_current_mo") and (l, c_idx) in present
            and (c.get("locked_line") is None or c.get("locked_line") == l)
        ]
        if not cmo_idxs:
            continue
        for o_idx, o in enumerate(orders):
            key = (l, o_idx)
            if key not in present or key in dead_pairs:
                continue
            if o.get("is_current_mo") or o.get("is_trial"):
                continue
            for c_idx in cmo_idxs:
                ckey = (l, c_idx)
                model.Add(
                    seg_a_start[key] >= eff_end[ckey]
                ).OnlyEnforceIf([present[key], present[ckey]])


    # ── Changeover constraints (pairwise ordering + setup times) ──────────
    #
    # Successor variables track which order *immediately follows* which on
    # each line.  This lets us compute a weighted changeover cost where
    # topload-format changes are penalized more heavily than other changes.
    succ = {}           # (l, i_idx, j_idx) -> BoolVar: j immediately follows i
    weighted_co_cost_per_line = []  # list of IntVars (one per line)
    # CIP absorption entries: (l, pred_end, j_idx, gate, delta) — when a
    # solver CIP lands between `pred_end` (an IntVar: the predecessor's
    # eff_end; or an int: the line's availability gate for the INITIAL
    # changeover, fix SA-2) and order j's start while `gate` (the succ / first
    # BoolVar of that adjacency) holds, `delta` is refunded.
    cip_absorbable = []
    # cip_req_after pairs whose full cost is waived by a FIXED committed-CIP
    # window (Scenario F: solver CIPs stand down; projected CIPs arrive as
    # downtime rows with a CIP reason): same tuple shape, delta = full cost.
    cip_window_waivable = []
    co_init_cost_terms: list = []  # priced initial changeovers (telemetry)

    # Restructured 2026-09-03 (fix SA-6 / audit C63, modelcore-6): the
    # ordering variables, first flags and setup-TIME floors are built whenever
    # the phase carries changeovers, and `ignore_co` (relax level 3) now skips
    # ONLY the cost terms. Before, level 3 dropped the setup hours too, so
    # zero-gap schedules between different SKUs were written, imported and
    # scored as normal FEASIBLE plans (validator: CHANGEOVER_GAP errors).
    # Setup time is physics; the price is a preference.
    if phase in ("sanity3", "full"):
        price_co = not ignore_co
        # Per-machine changeover weights
        W_top = P.co_topload_weight
        W_ttp = P.co_ttp_weight
        W_ffs = P.co_ffs_weight
        W_cp = P.co_casepacker_weight
        W_base = P.co_base_weight
        W_conv_org = P.co_conv_org_weight
        W_cinn = P.co_cinn_weight
        W_flavor = P.co_flavor_weight
        W_cip_req = P.co_cip_req_weight
        _MC_DEFAULT = {"ttp": 1, "ffs": 1, "topload": 1, "casepacker": 1,
                       "conv_to_org": 0, "cinn_to_non": 0, "added_flavors": 0}

        def _pair_cost(from_sku: str, to_sku: str):
            """Weighted cost of switching from_sku -> to_sku and the matching
            machine-change row. SAME SKU -> (0, None): a run of one SKU
            continuing into the next order of that SKU is not a changeover
            (fix SA-1 / audit C72, F9: the pair was absent from the matrix,
            so the all-1 default charged a phantom 1080 under F weights and
            the solver split same-SKU campaigns with foreign SKUs; even a
            present row still charged W_base). CLEAN -> 0 as for the time
            offset. Unknown pairs keep the conservative all-1 default."""
            if from_sku == to_sku or from_sku == "CLEAN":
                return 0, None
            mc = data.machine_changes.get((from_sku, to_sku), _MC_DEFAULT)
            # added_flavors can be negative (reward for removing flavors)
            flavor_cost = W_flavor * mc.get("added_flavors", 0)
            cip_req_cost = W_cip_req * mc.get("cip_req_after", 0)
            cost = max(0, (
                W_base
                + W_top * mc["topload"]
                + W_ttp * mc["ttp"]
                + W_ffs * mc["ffs"]
                + W_cp * mc["casepacker"]
                + W_conv_org * mc.get("conv_to_org", 0)
                + W_cinn * mc.get("cinn_to_non", 0)
                + flavor_cost
                + cip_req_cost
            ))
            return cost, mc

        def _absorb_delta(mc, cost: int) -> int:
            """What a CIP between the two runs refunds: cip_req_after pairs
            (a CIP is REQUIRED between them, 2026-08-26) waive their ENTIRE
            cost at a clean — retooling happens during the CIP; otherwise
            only the conv->org / cinn->non hygiene penalties."""
            if mc is None:
                return 0
            if mc.get("cip_req_after", 0):
                return cost
            return (W_conv_org * mc.get("conv_to_org", 0)
                    + W_cinn * mc.get("cinn_to_non", 0))

        for l in lines:
            elig = [
                o_idx
                for o_idx, o in enumerate(orders)
                if (l, o_idx) not in dead_pairs
                and ((
                    o.get("is_trial") and o.get("trial_line") == l
                ) or (
                    not o.get("is_trial")
                    and data.capable.get((l, o["sku"]))
                    and (data.rate.get((l, o["sku"])) or 0) > 0
                ))
            ]
            any_present = model.NewBoolVar(f"any_present_l{l}")
            model.Add(
                sum(present[(l, i)] for i in elig) >= 1
            ).OnlyEnforceIf(any_present)
            model.Add(
                sum(present[(l, i)] for i in elig) == 0
            ).OnlyEnforceIf(any_present.Not())

            # First order on line: changeover from initial SKU
            first_flags = []
            init_sku = str(
                data.init_map.get(l, {}).get("initial_sku", "CLEAN")
            )
            long_flag = int(
                data.init_map.get(l, {}).get("long_shutdown_flag", 0)
            )
            long_extra = int(
                data.init_map.get(l, {}).get("long_shutdown_extra", 4)
            )
            avail = int(data.init_map.get(l, {}).get("available_from", 0))
            co_cost_terms_init: list = []  # first_i * pair_cost(init -> i)
            co_bound_l = 0                 # sum of all cost coefficients on l
            for i_idx in elig:
                i = orders[i_idx]
                first_i = model.NewBoolVar(
                    f"first_l{l}_o{i['order_id']}"
                )
                first_flags.append(first_i)
                model.AddImplication(first_i, present[(l, i_idx)])
                for j_idx in elig:
                    if j_idx == i_idx:
                        continue
                    # Gate on present[j]: an ABSENT order's interval collapses
                    # to start = end = 0 (present.Not() => seg_a_end == 0), so
                    # comparing against absent orders forced the line's first
                    # start <= 0 — impossible once the current-state overlay
                    # introduced real availability gates (> 0). One line could
                    # dodge it by making EVERY elig order present (4h min-run
                    # each), but max_lines_per_order caps an order at 2 lines,
                    # so >= 3 gated lines sharing elig SKUs were pigeonhole-
                    # INFEASIBLE at every relax level below 3 (2026-08-14
                    # bisection: nocmo FEASIBLE, 1-2 MO-lines FEASIBLE, 6+
                    # INFEASIBLE). "First" only ranks against orders that are
                    # actually on the line.
                    model.Add(
                        seg_a_start[(l, i_idx)]
                        <= seg_a_start[(l, j_idx)]
                    ).OnlyEnforceIf([first_i, present[(l, j_idx)]])
                base = (
                    data.setup.get((init_sku, i["sku"]), 0)
                    if init_sku != "CLEAN"
                    else 0
                )
                eff = base + (long_extra if long_flag == 1 else 0)
                if eff > 0 or avail > 0:
                    model.Add(
                        seg_a_start[(l, i_idx)] >= avail + eff
                    ).OnlyEnforceIf([first_i, present[(l, i_idx)]])
                # Price the INITIAL changeover (fix SA-2 / audit C31, F1):
                # the switch from the SKU the line is holding into its first
                # order cost TIME but never entered the objective, so a line
                # could open with an arbitrarily expensive format change for
                # free (live F: 11 initial changeovers, 8 of them major; B3
                # and B2_noweek1 chose an extra major because the first one
                # was free). Same formula, defaults and waivers as succ pairs;
                # CLEAN or same SKU -> 0 (handled in _pair_cost).
                if price_co:
                    init_cost, init_mc = _pair_cost(init_sku, i["sku"])
                    if init_cost > 0:
                        co_cost_terms_init.append(first_i * init_cost)
                        co_bound_l += init_cost
                        co_init_cost_terms.append(first_i * init_cost)
                        delta0 = _absorb_delta(init_mc, init_cost)
                        if delta0 > 0:
                            cip_absorbable.append(
                                (l, avail, i_idx, first_i, delta0))
                            if init_mc.get("cip_req_after", 0):
                                cip_window_waivable.append(
                                    (l, avail, i_idx, first_i, init_cost))
            if first_flags:
                model.Add(sum(first_flags) == 1).OnlyEnforceIf(any_present)
                model.Add(sum(first_flags) == 0).OnlyEnforceIf(
                    any_present.Not()
                )

            # Pairwise ordering: uses eff_end for "end of order"
            # Also create successor variables for adjacency tracking.
            for a in range(len(elig)):
                for b in range(a + 1, len(elig)):
                    i_idx, j_idx = elig[a], elig[b]
                    i, j = orders[i_idx], orders[j_idx]
                    b_ij = model.NewBoolVar(
                        f"b_l{l}_{i['order_id']}__{j['order_id']}"
                    )
                    model.AddImplication(b_ij, present[(l, i_idx)])
                    model.AddImplication(b_ij, present[(l, j_idx)])
                    setup_ij = data.setup.get((i["sku"], j["sku"]), 0)
                    setup_ji = data.setup.get((j["sku"], i["sku"]), 0)
                    # i before j: j's seg_a starts after i's effective end
                    model.Add(
                        seg_a_start[(l, j_idx)]
                        >= eff_end[(l, i_idx)] + setup_ij
                    ).OnlyEnforceIf(b_ij)
                    # j before i: i's seg_a starts after j's effective end.
                    # MUST be gated on BOTH presences: b_ij implies both
                    # present, so (i absent, j present) FORCES b_ij false —
                    # and this branch then demanded start_i(=0, absent
                    # intervals collapse to 0) >= eff_end_j + setup, i.e.
                    # every present order had to END at 0 whenever any
                    # earlier-indexed elig order was absent. Same absorb-
                    # every-elig-order pressure as the first-flag bug, second
                    # source (2026-08-14). Ordering only exists between two
                    # orders that are actually on the line.
                    model.Add(
                        seg_a_start[(l, i_idx)]
                        >= eff_end[(l, j_idx)] + setup_ji
                    ).OnlyEnforceIf([
                        b_ij.Not(),
                        present[(l, i_idx)],
                        present[(l, j_idx)],
                    ])

                    if not price_co:
                        continue  # ignore_co: time floors only, no adjacency web
                    # Successor variables: j immediately follows i (or vice versa)
                    s_ij = model.NewBoolVar(
                        f"succ_l{l}_{i['order_id']}__{j['order_id']}"
                    )
                    s_ji = model.NewBoolVar(
                        f"succ_l{l}_{j['order_id']}__{i['order_id']}"
                    )
                    succ[(l, i_idx, j_idx)] = s_ij
                    succ[(l, j_idx, i_idx)] = s_ji
                    # Successor implies ordering direction
                    model.AddImplication(s_ij, b_ij)
                    model.AddImplication(s_ji, b_ij.Not())
                    # Successor implies both present
                    model.AddImplication(s_ij, present[(l, i_idx)])
                    model.AddImplication(s_ij, present[(l, j_idx)])
                    model.AddImplication(s_ji, present[(l, i_idx)])
                    model.AddImplication(s_ji, present[(l, j_idx)])

            if not price_co:
                continue  # ignore_co: no succ web, no cost terms on this line

            # Chain constraints: each order has at most one successor / one predecessor
            for a_pos, a_idx in enumerate(elig):
                # At most one successor for order a
                succ_from_a = [
                    succ[(l, a_idx, b_idx)]
                    for b_pos, b_idx in enumerate(elig)
                    if b_pos != a_pos and (l, a_idx, b_idx) in succ
                ]
                if succ_from_a:
                    model.Add(sum(succ_from_a) <= 1)
                # At most one predecessor for order a
                succ_to_a = [
                    succ[(l, b_idx, a_idx)]
                    for b_pos, b_idx in enumerate(elig)
                    if b_pos != a_pos and (l, b_idx, a_idx) in succ
                ]
                if succ_to_a:
                    model.Add(sum(succ_to_a) <= 1)

            # Total successor links = present orders - 1  (chain integrity)
            all_succ_l = [
                succ[(l, elig[a], elig[b])]
                for a in range(len(elig))
                for b in range(len(elig))
                if a != b and (l, elig[a], elig[b]) in succ
            ]
            if all_succ_l:
                n_present_l = model.NewIntVar(
                    0, len(elig), f"n_present_l{l}"
                )
                model.Add(
                    n_present_l == sum(present[(l, i)] for i in elig)
                )
                n_succ_l = model.NewIntVar(
                    0, len(elig), f"n_succ_l{l}"
                )
                model.Add(n_succ_l == sum(all_succ_l))
                # If any orders present, successors = present - 1
                model.Add(
                    n_succ_l == n_present_l - 1
                ).OnlyEnforceIf(any_present)
                model.Add(n_succ_l == 0).OnlyEnforceIf(
                    any_present.Not()
                )

            # Weighted changeover cost for this line
            # cost = sum over adjacent pairs of:
            #   base + topload_weight * topload_change + ttp * ttp_change + ...
            # plus the priced initial changeover (co_cost_terms_init, above).
            co_cost_terms = list(co_cost_terms_init)
            for a in range(len(elig)):
                for b in range(len(elig)):
                    if a == b:
                        continue
                    i_idx, j_idx = elig[a], elig[b]
                    key = (l, i_idx, j_idx)
                    if key not in succ:
                        continue
                    i_sku = orders[i_idx]["sku"]
                    j_sku = orders[j_idx]["sku"]
                    pair_cost, mc = _pair_cost(i_sku, j_sku)
                    if pair_cost > 0:
                        co_cost_terms.append(succ[key] * pair_cost)
                        co_bound_l += pair_cost
                        # Track pairs where a CIP between orders can absorb
                        # conv→org / cinn→non penalties (or the whole cost
                        # for cip_req_after pairs, see _absorb_delta).
                        if mc.get("cip_req_after", 0):
                            cip_window_waivable.append(
                                (l, eff_end[(l, i_idx)], j_idx, succ[key],
                                 pair_cost)
                            )
                        absorb_delta = _absorb_delta(mc, pair_cost)
                        if absorb_delta > 0:
                            cip_absorbable.append(
                                (l, eff_end[(l, i_idx)], j_idx, succ[key],
                                 absorb_delta)
                            )

            # Domain bound (fix SA-7 / audit C33): the old closed form
            # len(elig) x sum(weights) omitted the flavor term (and now the
            # initial changeover), so a legal sequence with several
            # added_flavors pairs could exceed the domain and be declared
            # INFEASIBLE. The sum of every term's coefficient (co_bound_l,
            # accumulated as terms are added) is always a valid upper bound
            # on a weighted sum of 0/1 variables.
            #
            # The per-line cost var is created even when the line has NO
            # priced pair (fix SA-1 follow-up): the objective falls back to
            # the FLAT switch count only when no line carries a weighted
            # cost, and a line whose adjacencies are all same-SKU (cost 0)
            # used to be "priced" by that fallback at W2 per adjacency —
            # the phantom same-SKU changeover through the back door.
            co_cost_l = model.NewIntVar(
                0, max(1, co_bound_l), f"co_cost_l{l}"
            )
            if co_cost_terms:
                model.Add(co_cost_l == sum(co_cost_terms))
            else:
                model.Add(co_cost_l == 0)
            weighted_co_cost_per_line.append(co_cost_l)

    # ── Week-0 / Week-1 gap constraint (LEGACY, opt-in) ───────────────────
    # Skipped in cross-week mode: that constraint pins Week-1 production to
    # start immediately after the line's last Week-0 order, which structurally
    # forbids the interleaving/merging cross-week mode exists to allow.
    # 2026-09-03 (fix SB-1 / audit C19, bench F3): OFF by default. The stitch
    # is one-sided — it only says "week-1 work starts within 1 h of the
    # week-0 tail" — so with the 120 h fill floor it forced every week-0
    # tail to END at >= 119 (bench B1: both lines idle to h75 for a 44 h
    # order) and, with the Monday-anchor classes, forced W3 AHEAD of W1 on
    # the live Wednesday frame. Idle is priced from the gate (SA-10) and
    # early fill is priced per hour (above), so the stitch has nothing left
    # to do. It survives behind Params.legacy_week_stitch (read with getattr;
    # default False) with the classes taken from the demand frame: week 0 =
    # the first demand week, week 1 = the SECOND demand week only.
    legacy_stitch = bool(getattr(P, "legacy_week_stitch", False))
    if P.allow_week1_in_week0 and not cross_week and legacy_stitch:
        # The week-boundary stitch (gap <= MAX_GAP_W0_W1_HOURS) is a FRESH-PLAN
        # rule: don't leave an idle wall between weeks the solver itself
        # planned. Committed manprg MOs are exempt — their windows are plant
        # fact with HARD start floors, and classifying them here forced
        # impossible stitches (measured 2026-08-14: P09's forced-w0 MOs end
        # <= h151 while its forced-w1 MO starts >= h177 — a 26h wall the 1h
        # gap rule forbids at hard dues, one root of the level-0/1
        # infeasibility in current-MO mode).
        first_end = wk_frame.get("first_end")
        second_start = wk_frame.get("second_start")
        week0_order_idxs = [
            o_idx
            for o_idx, o in enumerate(orders)
            if first_end is not None and int(o["due_end"]) <= first_end
            and not o.get("is_current_mo") and not o.get("is_trial")
        ]
        week1_order_idxs = [
            o_idx
            for o_idx, o in enumerate(orders)
            if second_start is not None
            and int(o["due_start"]) == second_start
            and not o.get("is_current_mo") and not o.get("is_trial")
        ]
        if week0_order_idxs and week1_order_idxs:
            for l in lines:
                end_or_zero = {}
                for o_idx in week0_order_idxs:
                    key = (l, o_idx)
                    end_or_zero[key] = model.NewIntVar(
                        0, H, f"end_or_zero_l{l}_o{o_idx}"
                    )
                    model.Add(
                        end_or_zero[key] == eff_end[key]
                    ).OnlyEnforceIf(present[key])
                    model.Add(end_or_zero[key] == 0).OnlyEnforceIf(
                        present[key].Not()
                    )
                last_w0_end = model.NewIntVar(0, H, f"last_w0_end_l{l}")
                model.AddMaxEquality(
                    last_w0_end,
                    [
                        end_or_zero[(l, o_idx)]
                        for o_idx in week0_order_idxs
                    ],
                )

                start_or_H = {}
                for o_idx in week1_order_idxs:
                    key = (l, o_idx)
                    start_or_H[key] = model.NewIntVar(
                        0, H, f"start_or_H_l{l}_o{o_idx}"
                    )
                    model.Add(
                        start_or_H[key] == seg_a_start[key]
                    ).OnlyEnforceIf(present[key])
                    model.Add(start_or_H[key] == H).OnlyEnforceIf(
                        present[key].Not()
                    )
                first_w1_start = model.NewIntVar(
                    0, H, f"first_w1_start_l{l}"
                )
                model.AddMinEquality(
                    first_w1_start,
                    [
                        start_or_H[(l, o_idx)]
                        for o_idx in week1_order_idxs
                    ],
                )

                has_w0 = model.NewBoolVar(f"has_w0_l{l}")
                model.Add(
                    sum(
                        present[(l, o_idx)]
                        for o_idx in week0_order_idxs
                    )
                    >= 1
                ).OnlyEnforceIf(has_w0)
                model.Add(
                    sum(
                        present[(l, o_idx)]
                        for o_idx in week0_order_idxs
                    )
                    == 0
                ).OnlyEnforceIf(has_w0.Not())
                has_w1 = model.NewBoolVar(f"has_w1_l{l}")
                model.Add(
                    sum(
                        present[(l, o_idx)]
                        for o_idx in week1_order_idxs
                    )
                    >= 1
                ).OnlyEnforceIf(has_w1)
                model.Add(
                    sum(
                        present[(l, o_idx)]
                        for o_idx in week1_order_idxs
                    )
                    == 0
                ).OnlyEnforceIf(has_w1.Not())
                has_both = model.NewBoolVar(f"has_both_w0_w1_l{l}")
                model.Add(has_both == 1).OnlyEnforceIf([has_w0, has_w1])
                model.Add(has_both == 0).OnlyEnforceIf(has_w0.Not())
                model.Add(has_both == 0).OnlyEnforceIf(has_w1.Not())
                model.Add(
                    first_w1_start - last_w0_end <= MAX_GAP_W0_W1_HOURS
                ).OnlyEnforceIf(has_both)

    # ── CIP: first-class solver intervals with wide placement windows ─────
    #
    # CIPs are modelled as explicit interval variables in the NoOverlap
    # constraint.  They can be placed anywhere before their clock-hour
    # deadline and may split a production run (seg_a → CIP → seg_b).
    # CIP duration (6 h) >= max changeover time, so when a CIP falls
    # between two different-SKU orders the changeover is absorbed.
    #
    # 2026-09-03 rewrite (fixes SB-2 / SB-3 / SB-5; audit C44, C43, C46):
    #  * the CLOCK is the plant's wall clock since the previous clean
    #    (cip_info: ScheduledCIP = PreviousCIP + MaxHoursBetweenCIP). At
    #    horizon hour t the line has been dirty for t + carry hours, where
    #    carry = carryover_run_hours_since_last_cip_at_t0 is the wall-clock
    #    age of the clean at t0 (negative when the clean happened after t0,
    #    e.g. a week-0 clean feeding the two-phase week-1 states). Committed
    #    and idle hours count — the old model measured only the span of the
    #    solver's own orders (last_end - first_start), which let an E-mode
    #    CIP be 15-160 h late on every live line;
    #  * the number of slots is ceil((H + carry) / interval) + 1 per line,
    #    not a hard-coded 3 (which silently capped a 504 h line at
    #    4 x interval - carry + 18 h of usable time);
    #  * a fill order may resume after a FIXED committed CIP window (a
    #    downtimes.csv row whose reason contains "CIP") exactly as after a
    #    solver CIP, so a Scenario F order is no longer capped at one
    #    inter-CIP gap while the solver's own CIPs are stood down.

    cip_model_vars: Dict[int, list] = {}
    cip_warnings: list = []
    fixed_cip_windows: Dict[int, list] = {}
    for dt in data.downtimes:
        if "cip" not in str(dt.get("reason", "")).lower():
            continue
        s_w = max(0, int(dt["start"]))
        e_w = min(H, int(dt["end"]))
        if e_w > s_w:
            fixed_cip_windows.setdefault(dt["line_id"], []).append((s_w, e_w))
    if phase == "full":
        dur = P.cip_duration_h
        for l in lines:
            # Per-line CIP interval from line_cip_hrs.csv; falls back to global setting
            interval = int(data.cip_interval_map.get(l, P.cip_interval_h))
            carry = int(
                data.init_map.get(l, {}).get("carryover_run_hours", 0)
            )
            avail_from = int(
                data.init_map.get(l, {}).get("available_from", 0)
            )
            line_label = data.line_names.get(l, f"L{l}")

            # Eligible orders on this line (including trials pinned here)
            elig_idxs = [
                o_idx
                for o_idx, o in enumerate(orders)
                if (
                    o.get("is_trial") and o.get("trial_line") == l
                ) or (
                    not o.get("is_trial")
                    and data.capable.get((l, o["sku"]))
                    and (data.rate.get((l, o["sku"])) or 0) > 0
                )
            ]
            if not elig_idxs:
                cip_model_vars[l] = []
                continue

            # Last production end on the line (0 when nothing is scheduled)
            e_or_0_list = []
            for o_idx in elig_idxs:
                key = (l, o_idx)
                e_o0 = model.NewIntVar(0, H, f"cipEo0_l{l}_o{o_idx}")
                model.Add(e_o0 == eff_end[key]).OnlyEnforceIf(
                    present[key]
                )
                model.Add(e_o0 == 0).OnlyEnforceIf(present[key].Not())
                e_or_0_list.append(e_o0)
            last_end_l = model.NewIntVar(0, H, f"last_end_l{l}")
            model.AddMaxEquality(last_end_l, e_or_0_list)

            # ── Dirty clock at t0 ────────────────────────────────────
            # Two sources describe the previous clean: the carry column
            # (wall-clock hours since it, at t0) and the optional absolute
            # last_cip_end_datetime. In consistent data they agree
            # (carry == -last_cip_end_hour); when they do not, the DIRTIER
            # reading wins (food safety). Negative hours (a clean before
            # the anchor) are valid input — the old `0 <= hour < H` guard
            # made the datetime branch dead for every pre-anchor clean,
            # i.e. for every live line.
            carry_eff = carry
            last_cip_dt_str = str(
                data.init_map.get(l, {}).get(
                    "last_cip_end_datetime", ""
                ) or ""
            ).strip()
            if last_cip_dt_str and last_cip_dt_str.lower() != "nan":
                try:
                    anchor = datetime.strptime(
                        P.planning_start_date, "%Y-%m-%d %H:%M:%S"
                    )
                    cip_dt = datetime.strptime(
                        last_cip_dt_str, "%Y-%m-%d %H:%M:%S"
                    )
                    last_cip_end_hour = math.floor(
                        (cip_dt - anchor).total_seconds() / 3600
                    )
                    if last_cip_end_hour < H:
                        carry_eff = max(carry, -last_cip_end_hour)
                except (ValueError, TypeError):
                    pass  # bad datetime — the carry column alone

            # ── Slot count (fix SB-2 / C44) ──────────────────────────
            # Enough slots for the whole horizon at this line's interval,
            # plus one spare so the trigger below can always be satisfied;
            # capped by how many cleans physically fit in H hours.
            if interval <= 0:
                n_slots = 1
            else:
                n_slots = math.ceil((H + max(0, carry_eff)) / interval) + 1
            n_slots = max(1, min(n_slots, H // max(1, dur) + 1))

            # ── Wall-clock trigger (fix SB-3 / C43) ──────────────────
            # CIP k is mandatory iff the dirty clock at the last production
            # end reaches k x interval: last_end + carry >= k*interval.
            # Hours before the first solver block (committed MO, gate, idle)
            # count, because the plant's clock does not stop for them.
            b_slots: list = []
            for k in range(n_slots):
                bk = model.NewBoolVar(f"cip{k + 1}_needed_l{l}")
                thr = (k + 1) * interval
                model.Add(last_end_l + carry_eff >= thr).OnlyEnforceIf(bk)
                model.Add(last_end_l + carry_eff <= thr - 1).OnlyEnforceIf(
                    bk.Not()
                )
                if k > 0:
                    model.AddImplication(bk, b_slots[k - 1])
                b_slots.append(bk)

            # ── CIP 1 deadline: HARD, ALWAYS ─────────────────────────
            # FOOD-SAFETY / COMPLIANCE CONSTRAINT.  A CIP may be pulled
            # EARLIER than this point freely (the only lower bound is
            # avail_from), but it may NEVER start later.  `interval` here
            # is the line's max_cip_hrs from data/reference/line_cip_hrs.csv
            # (P09/P12/P14/P17-P22 = 120h, P10/P11/P13/P15/P16 = 144h).
            # DO NOT relax this at any relax level and DO NOT gate it on
            # cip_flex - exceeding max CIP hours is a compliance violation.
            #
            # Plant rule (fix SB-3): the clean is due `interval` wall-clock
            # hours after the previous one ended, i.e. at horizon hour
            # interval - carry — NOT avail_from + (interval - carry), which
            # let the deadline slide by the whole availability gate.
            # (= last_cip_end_hour + interval when the absolute datetime is
            # the dirtier source — the cross-phase deadline of the two-phase
            # week-1 states.)
            deadline = interval - carry_eff

            if deadline < avail_from:
                # The line is already past its CIP interval when it becomes
                # available (running MO longer than the remaining clock):
                # the honest answer is a clean as early as the line
                # physically allows, and a warning the planner can see.
                fit = _earliest_cip_fit(data, l, avail_from, dur, H)
                cip_warnings.append(
                    f"{line_label}: line already past its CIP interval at "
                    f"the gate (CIP due h{deadline}, available from "
                    f"h{avail_from}, carry {carry_eff} h / interval {interval} h)"
                    f" — CIP 1 pinned to h{fit}"
                )
                deadline = fit

            # ── Slots: interval vars, chain constraints ──────────────
            slots: list = []
            prev_end = None
            for k, bk in enumerate(b_slots):
                cs = model.NewIntVar(0, H, f"cip{k + 1}_s_l{l}")
                ce = model.NewIntVar(0, H, f"cip{k + 1}_e_l{l}")
                c_int = model.NewOptionalIntervalVar(
                    cs, dur, ce, bk, f"cip{k + 1}_l{l}"
                )
                line_intervals[l].append(c_int)
                if k == 0:
                    model.Add(cs >= avail_from).OnlyEnforceIf(bk)
                    model.Add(cs <= deadline).OnlyEnforceIf(bk)
                else:
                    model.Add(cs >= prev_end).OnlyEnforceIf(bk)
                    # HARD max-interval (see CIP 1 note): never later than
                    # one full line interval after the previous CIP ended.
                    # Earlier is free.
                    model.Add(cs <= prev_end + interval).OnlyEnforceIf(bk)
                prev_end = ce
                slots.append((cs, ce, bk))

            # Aggregate: production + CIP time <= available hours
            total_run_l = model.NewIntVar(0, H, f"total_run_l{l}")
            model.Add(
                total_run_l
                == sum(
                    run_h[(l, o_idx)] for o_idx in range(len(orders))
                )
            )
            avail_h = available_hours_line(P, data, l)
            model.Add(
                total_run_l + sum(dur * bk for bk in b_slots) <= avail_h
            )

            cip_model_vars[l] = slots

            # Key constraint: production after last CIP must be <= interval
            # Without this, CIPs can bunch early leaving a long uncovered tail.
            cip_ends_or_0 = []
            for k, (ck_s, ck_e, ck_b) in enumerate(
                cip_model_vars[l]
            ):
                ce_or_0 = model.NewIntVar(
                    0, H, f"cipEoZ_l{l}_c{k}"
                )
                model.Add(ce_or_0 == ck_e).OnlyEnforceIf(ck_b)
                model.Add(ce_or_0 == 0).OnlyEnforceIf(ck_b.Not())
                cip_ends_or_0.append(ce_or_0)
            last_cip_end_l = model.NewIntVar(
                0, H, f"last_cip_end_l{l}"
            )
            model.AddMaxEquality(last_cip_end_l, cip_ends_or_0)
            model.Add(
                last_end_l - last_cip_end_l <= interval
            ).OnlyEnforceIf(b_slots[0])

            # seg_b requires a clean between its segments ─────────────
            # Either one of the solver's CIP slots, or (fix SB-5) a FIXED
            # committed CIP window of this line; other blocks may sit in
            # between in both cases (same rule as before for solver CIPs).
            for o_idx in elig_idxs:
                key = (l, o_idx)
                links = []
                for k, (ck_s, ck_e, ck_b) in enumerate(
                    cip_model_vars[l]
                ):
                    link = model.NewBoolVar(
                        f"cipLnk_l{l}_o{o_idx}_c{k}"
                    )
                    # CIP k sits between seg_a end and seg_b start
                    model.Add(
                        seg_a_end[key] <= ck_s
                    ).OnlyEnforceIf(link)
                    model.Add(
                        ck_e <= seg_b_start[key]
                    ).OnlyEnforceIf(link)
                    # CIP must actually be present for the link to hold
                    model.AddImplication(link, ck_b)
                    links.append(link)
                for k, (s_w, e_w) in enumerate(fixed_cip_windows.get(l, [])):
                    link = model.NewBoolVar(
                        f"cipLnkW_l{l}_o{o_idx}_w{k}"
                    )
                    model.Add(seg_a_end[key] <= s_w).OnlyEnforceIf(link)
                    model.Add(seg_b_start[key] >= e_w).OnlyEnforceIf(link)
                    links.append(link)
                # If seg_b is present, at least one clean must be between
                model.Add(sum(links) >= 1).OnlyEnforceIf(
                    seg_b_present[key]
                )

    # Disallow seg_b when CIPs are not modelled (non-full phase)
    if phase != "full":
        for l in lines:
            for o_idx in range(len(orders)):
                model.Add(seg_b_present[(l, o_idx)] == 0)

    # ── AddNoOverlap (after CIP intervals added) ─────────────────────────
    for l in lines:
        model.AddNoOverlap(line_intervals[l])

    # ── CIP count cost (fix SB-4 / audit C42, bench F6) ──────────────────
    #
    # RETIRED: the cip_defer REWARD (`- sum(cip_start) * W_cip` in every
    # objective branch, scaled by objective_cip_flex_weight under cip_flex).
    # It rewarded a LATER and a MORE NUMEROUS clean: the solver split a run,
    # parked the tail hundreds of hours later and "needed" up to three
    # cleans purely to collect the reward (audit exp6: a 30 h order got two
    # CIPs and a negative objective under the live weights; exp1b2/exp5:
    # runs pushed to the deadline, phantom second cleans). Both Params
    # (objective_cip_defer_weight, objective_cip_flex_weight) are kept for
    # config compatibility but no longer reach the model.
    #
    # In its place every present CIP costs dur x (median capable rate of the
    # line) kg, expressed in fill units (CIP_COST_FILL_UNITS_PER_KG = the
    # tier-1 base weight, 1 kg = 1,000). Under the Scenario F weights that
    # is 6 h x 1,000 kg/h x 1,000 = 6,000,000 per clean, above the largest
    # absorption bonus a clean can earn ((1080 + 30 + 20 + 150) x 300 =
    # 384,000 in pass 1; x 100 x K = 33 -> 4.2 M in pass 2) and above every
    # makespan/idle tiebreaker, so an unnecessary clean is never profitable;
    # at the same time it is only ~6 kg of pass-1 fill (which is scaled
    # x 1,000 on top of the tier weight), so a clean that unlocks real
    # production is still taken. The count itself is pinned by the
    # wall-clock trigger above; this term only removes the incentive to
    # stretch a line's last end across the next threshold.
    cip_cost_terms: list = []
    for l in lines:
        if not cip_model_vars.get(l):
            continue
        kg_per_cip = int(round(P.cip_duration_h * _median_line_rate(data, l)))
        for ck_s, ck_e, ck_b in cip_model_vars[l]:
            cip_cost_terms.append(
                ck_b * (kg_per_cip * CIP_COST_FILL_UNITS_PER_KG))
    cip_cost_total = sum(cip_cost_terms) if cip_cost_terms else 0

    # ── CIP absorption: waive conv→org / cinn→non when CIP is between ──
    #
    # When a CIP falls between two adjacent orders, the line is fully
    # cleaned, so conv_to_org and cinn_to_non changeover penalties are
    # absorbed.  We give a bonus (cost reduction) equal to the waived
    # penalty whenever the solver places a CIP between such a pair.
    # Tuple shape (2026-09-03, fix SA-2): (l, pred_end, j_idx, gate, delta)
    # where pred_end is the predecessor's eff_end IntVar, or the int
    # availability gate for the priced INITIAL changeover, and gate is the
    # succ / first BoolVar of that adjacency.
    cip_absorb_bonus_terms = []
    absorb_vars_by_gate: Dict[str, list] = {}  # gate var name -> its absorb bools
    if cip_absorbable and cip_model_vars:
        for n_ab, (l_ab, pred_end, j_ab, gate_ab, delta) in enumerate(
                cip_absorbable):
            cip_vars_l = cip_model_vars.get(l_ab, [])
            if not cip_vars_l:
                continue
            absorb_vars = []
            for k, (ck_s, ck_e, ck_b) in enumerate(cip_vars_l):
                ab = model.NewBoolVar(
                    f"cipAb_l{l_ab}_n{n_ab}_o{j_ab}_c{k}"
                )
                model.AddImplication(ab, gate_ab)
                model.AddImplication(ab, ck_b)
                model.Add(ck_s >= pred_end).OnlyEnforceIf(ab)
                model.Add(
                    ck_e <= seg_a_start[(l_ab, j_ab)]
                ).OnlyEnforceIf(ab)
                absorb_vars.append(ab)
            if absorb_vars:
                model.Add(sum(absorb_vars) <= 1)
                absorb_vars_by_gate.setdefault(
                    gate_ab.Name(), []).extend(absorb_vars)
                for ab in absorb_vars:
                    cip_absorb_bonus_terms.append(ab * delta)

    # Fixed committed-CIP windows also waive required-CIP pairs (2026-08-26):
    # a cip_req_after transition straddling such a window is clean, so its
    # ENTIRE pair cost is refunded.
    # 2026-09-03 (fix SA-2 follow-up): this used to run ONLY on lines without
    # solver CIP variables — but in phase "full" every line with eligible
    # orders gets CIP variables even under the Scenario F stand-down
    # (interval 100,000), so the waiver never fired in the very mode it was
    # written for. It now runs on every line; double refund (a solver CIP
    # AND a fixed window both between the same pair) is excluded by the
    # shared "at most one refund per adjacency" constraint below.
    if cip_window_waivable:
        fixed_cips: Dict[int, list] = {}
        for dt in data.downtimes:
            if "cip" not in str(dt.get("reason", "")).lower():
                continue
            s = max(0, int(dt["start"]))
            e = min(H, int(dt["end"]))
            if e > s:
                fixed_cips.setdefault(dt["line_id"], []).append((s, e))
        for n_w, (l_w, pred_end, j_w, gate_w, full_cost) in enumerate(
                cip_window_waivable):
            wins = fixed_cips.get(l_w, [])
            if not wins:
                continue
            w_vars = []
            for k, (s, e) in enumerate(wins):
                w = model.NewBoolVar(f"cipWin_l{l_w}_n{n_w}_o{j_w}_k{k}")
                model.AddImplication(w, gate_w)
                if isinstance(pred_end, int):
                    if pred_end > s:
                        # committed window starts before the gate: it cannot
                        # sit between the gate and the first order
                        model.Add(w == 0)
                else:
                    model.Add(pred_end <= s).OnlyEnforceIf(w)
                model.Add(seg_a_start[(l_w, j_w)] >= e).OnlyEnforceIf(w)
                w_vars.append(w)
            if w_vars:
                # at most ONE refund per adjacency: fixed windows and solver
                # CIPs (absorb block above) share the budget
                model.Add(
                    sum(w_vars)
                    + sum(absorb_vars_by_gate.get(gate_w.Name(), [])) <= 1)
                for w in w_vars:
                    cip_absorb_bonus_terms.append(w * full_cost)

    # ── Line compactness (idle-time penalty) ──────────────────────────────
    #
    # Penalize per-line idle time: span − production − CIP hours.
    # CIP-segmented blocks (seg_a → CIP → seg_b) are NOT penalized because
    # the CIP hours are subtracted from the span.  Only true dead-time
    # (gaps between runs or between a run and a changeover) is penalized.
    line_idle_vars: list = []
    line_idle_by_line: Dict[int, Any] = {}  # exposed for tests/telemetry
    W_idle = P.objective_idle_weight
    if W_idle > 0:
        dur_cip = P.cip_duration_h
        for l in lines:
            elig = [
                o_idx
                for o_idx, o in enumerate(orders)
                if (o.get("is_trial") and o.get("trial_line") == l)
                or (
                    not o.get("is_trial")
                    and data.capable.get((l, o["sku"]))
                    and (data.rate.get((l, o["sku"])) or 0) > 0
                )
            ]
            if not elig:
                continue

            # Any orders assigned to this line?
            any_c = model.NewBoolVar(f"cidle_any_l{l}")
            model.Add(
                sum(present[(l, i)] for i in elig) >= 1
            ).OnlyEnforceIf(any_c)
            model.Add(
                sum(present[(l, i)] for i in elig) == 0
            ).OnlyEnforceIf(any_c.Not())

            # First production start on line (H when not present)
            s_list = []
            for o_idx in elig:
                v = model.NewIntVar(0, H, f"cS_l{l}_o{o_idx}")
                model.Add(
                    v == seg_a_start[(l, o_idx)]
                ).OnlyEnforceIf(present[(l, o_idx)])
                model.Add(v == H).OnlyEnforceIf(
                    present[(l, o_idx)].Not()
                )
                s_list.append(v)
            first_s = model.NewIntVar(0, H, f"cidle_fs_l{l}")
            model.AddMinEquality(first_s, s_list)

            # Last production end on line (0 when not present)
            e_list = []
            for o_idx in elig:
                v = model.NewIntVar(0, H, f"cE_l{l}_o{o_idx}")
                model.Add(
                    v == eff_end[(l, o_idx)]
                ).OnlyEnforceIf(present[(l, o_idx)])
                model.Add(v == 0).OnlyEnforceIf(
                    present[(l, o_idx)].Not()
                )
                e_list.append(v)
            last_e = model.NewIntVar(0, H, f"cidle_le_l{l}")
            model.AddMaxEquality(last_e, e_list)

            # Span = last end − the line's availability gate (fix SA-10 /
            # bench F3, 2026-09-03). Measured from the FIRST block, leading
            # idle was free while internal idle cost W_idle/h, so the
            # cheapest plan was always the one that started late (bench B1:
            # both lines idled to h76; repro: idle_weight 3 moved a 40 h
            # order from h0 to h128 for nothing). Idle hours before the first
            # run are hours the plant paid for just the same. Every start is
            # >= avail_l (availability floor above), so span >= 0 whenever
            # any order is present. first_s is kept for telemetry.
            avail_l_idle = max(0, int(
                data.init_map.get(l, {}).get("available_from", 0)))
            span_c = model.NewIntVar(0, H, f"cidle_sp_l{l}")
            model.Add(
                span_c == last_e - avail_l_idle
            ).OnlyEnforceIf(any_c)
            model.Add(span_c == 0).OnlyEnforceIf(any_c.Not())
            model.Add(first_s >= avail_l_idle).OnlyEnforceIf(any_c)

            # Total production hours on this line
            prod_c = model.NewIntVar(0, H, f"cidle_pr_l{l}")
            model.Add(
                prod_c
                == sum(
                    run_h[(l, o_idx)]
                    for o_idx in range(len(orders))
                )
            )

            # CIP hours on this line (subtracted so CIP splits aren't
            # penalized) — only for CIPs INSIDE the span (fix SB-7 / audit
            # C51): a clean that starts at or after the last production end
            # is not inside [gate, last_end], so subtracting its hours would
            # make idle = span - prod - 6 negative for a compact run and
            # force 6 h of phantom idle (or a phantom split) inside the
            # span. Every CIP starts >= the gate, so "inside" == starts
            # before last_e. (The leading-CIP case of the audit — carry 119,
            # clean at the gate then a 40 h run — is already correct since
            # SA-10 measures the span from the gate.)
            cip_h_expr = 0
            if l in cip_model_vars and cip_model_vars[l]:
                inside_terms = []
                for k_c, (ck_s, ck_e, ck_b) in enumerate(cip_model_vars[l]):
                    ins = model.NewBoolVar(f"cidle_in_l{l}_c{k_c}")
                    model.AddImplication(ins, ck_b)
                    model.Add(ck_s <= last_e - 1).OnlyEnforceIf(ins)
                    model.Add(ck_s >= last_e).OnlyEnforceIf([ck_b, ins.Not()])
                    inside_terms.append(dur_cip * ins)
                cip_h_expr = sum(inside_terms)

            # Idle = span − production − CIP hours  (≥ 0 by NoOverlap)
            idle_c = model.NewIntVar(0, H, f"cidle_l{l}")
            model.Add(
                idle_c == span_c - prod_c - cip_h_expr
            ).OnlyEnforceIf(any_c)
            model.Add(idle_c == 0).OnlyEnforceIf(any_c.Not())

            line_idle_vars.append(idle_c)
            line_idle_by_line[l] = idle_c

    total_idle = sum(line_idle_vars) if line_idle_vars else 0

    # ── Objective ─────────────────────────────────────────────────────────
    #
    # Changeover cost: when successor variables are available (changeovers
    # enabled), use the weighted per-machine cost.  Otherwise fall back to
    # a simple count of changeovers (jobs - 1) per line.
    changeovers_per_line = []
    for l in lines:
        total_jobs_l = model.NewIntVar(
            0, len(orders), f"total_jobs_l{l}"
        )
        model.Add(
            total_jobs_l
            == sum(present[(l, o_idx)] for o_idx in range(len(orders)))
        )
        any_present_l = model.NewBoolVar(f"any_present_obj_l{l}")
        model.Add(total_jobs_l >= 1).OnlyEnforceIf(any_present_l)
        model.Add(total_jobs_l == 0).OnlyEnforceIf(any_present_l.Not())
        changeovers_l = model.NewIntVar(
            0, len(orders), f"changeovers_l{l}"
        )
        model.Add(changeovers_l == total_jobs_l - 1).OnlyEnforceIf(
            any_present_l
        )
        model.Add(changeovers_l == 0).OnlyEnforceIf(any_present_l.Not())
        changeovers_per_line.append(changeovers_l)

    # Use weighted changeover cost when available, flat count as fallback
    use_weighted_co = len(weighted_co_cost_per_line) > 0
    weighted_co_total = (
        sum(weighted_co_cost_per_line) if use_weighted_co else 0
    )
    # Subtract CIP absorption bonus from weighted changeover cost
    if cip_absorb_bonus_terms and use_weighted_co:
        weighted_co_total = weighted_co_total - sum(cip_absorb_bonus_terms)
    flat_co_total = sum(changeovers_per_line)

    all_eff_end = [
        eff_end[(l, o_idx)]
        for l in lines
        for o_idx in range(len(orders))
    ]
    makespan = model.NewIntVar(0, P.horizon_h, "makespan")
    if all_eff_end:
        model.AddMaxEquality(makespan, all_eff_end)
    else:
        model.Add(makespan == 0)

    # Soft due-date penalty (relax_due only; empty dict -> 0 otherwise)
    late_total = sum(lateness.values()) if lateness else 0
    W_late = P.objective_late_weight

    # AZAP week-deviation penalty. Cost per hour an order runs outside the
    # week AZAP asked for: cross_week prices early AND late hours; the
    # default mode prices the early-fill hours before an order's own
    # due_start (fix SB-1, late leg constant 0 there). With the unbounded
    # early-fill policy (plant decision 2026-09-04) this and the fill week
    # gradient ARE the "right week first" preference — the only thing that
    # keeps a later week from taking hours a nearer week needs. Added to
    # every objective branch so the mode behaves identically in all of them.
    week_dev_total = (
        sum(e + lt for (e, lt) in week_dev.values()) if week_dev else 0
    )
    W_week = P.objective_week_deviation_weight
    week_pen = week_dev_total * W_week if week_dev else 0

    # Kg-week deviation price (plant decision 2026-09-04 #2; v2 pricing).
    # One invariant in every branch: the price is the same FRACTION of the
    # fill value of the same kg. Pass 2 takes weight / 1000 per kg-week
    # (fill ~ 1,000 per kg); pass 1 takes weight (fill = 1e6 per kg,
    # fill_scale = PASS1_FILL_SCALE); the non-fill branches rescale to their
    # changeover multiplier so the deviation:changeover ratio is pass 2's
    # (dev_kg_coefficients). `dev_used` records the pair the built branch
    # actually applied (telemetry / tests).
    dev_used: Dict[str, Any] = {"coeff": None}

    def _dev_pen(co_multiplier: Optional[int], fill_scale: int = 1) -> Any:
        c_e, c_l = dev_kg_coefficients(P, co_multiplier, fill_scale)
        dev_used["coeff"] = (c_e, c_l)
        pen: Any = 0
        if early_steps and c_e > 0:
            pen = pen + early_kg_weeks * c_e
        if late_steps and c_l > 0:
            pen = pen + late_kg_weeks * c_l
        return pen

    prod_score = None  # set in the soft-demand maximize branch only
    if maximize_production:
        # Current-state MOs are already-committed work (in VIF, on a locked
        # line). They must win over brand-new demand when capacity is tight:
        # weight their production much higher than demand orders so the
        # solver places them first and trims demand instead. (10x — an MO
        # kg is worth ten demand kg, so dropping an MO is only worthwhile
        # when it frees enormous demand capacity.)
        over_sum = 0
        over_coeff = 0
        if getattr(P, "soft_demand", False):
            # Two-tier fill reward (Scenario F, user rule "target is
            # 90-110%"): each kg up to qty_target pays full weight; kg
            # between target and qty_max pay over_target_reward_pct % of
            # the BASE tier-1 per-kg reward. Base = the 1000 tier times the
            # x1000 production scaling in the objective below — the week
            # gradient is deliberately excluded so the pct means one fixed
            # thing whatever the week count: pct=5 -> 50_000 per over kg vs
            # 1_000_000 per base tier-1 kg = exactly 5%. (The old hardcoded
            # over_sum*50 claimed "5%" but missed the x1000 scaling — it was
            # really ~0.005%, a near-pure tiebreaker; the 0.0 default keeps
            # that observed behavior.) A flat reward drove every scheduled
            # order to its 110% cap while other orders sat at 0 (measured
            # 2026-08-14 run 10) — meeting ALL targets must beat
            # over-filling any one of them. qty_max stays the hard wall.
            over_coeff = int(round(float(getattr(
                P, "over_target_reward_pct", 0.0)) * 10_000))
            #
            # Week-proximity gradient (user rule "right tonnage on the
            # right ISO week", 2026-08-15): when residual demand exceeds
            # free capacity, a flat per-kg reward hands the NEAREST week's
            # scarce hours to whichever week's orders make the biggest
            # runs — measured run 17: W34's own demand filled 14% while
            # W36 (early-filling into W34's hours) hit 92%. Each week
            # earlier than the last pays +1% per step, so contested hours
            # serve the nearest due week first; the gradient is far below
            # the 100%-vs-5% tier split, so it can only ever re-ORDER
            # weeks, not starve total fill.
            week_ends = sorted({
                int(o["due_end"]) for o in orders
                if not o.get("is_current_mo") and not o.get("is_trial")})
            wk_rank = {e: i for i, e in enumerate(week_ends)}
            n_wk = len(week_ends)

            def _w1(o: dict) -> int:
                r = wk_rank.get(int(o["due_end"]), n_wk - 1)
                return 1000 + 10 * max(0, n_wk - 1 - r)

            tier1 = []
            over_terms = []
            for o_idx, o in enumerate(orders):
                if o.get("is_current_mo"):
                    tier1.append(produced[o_idx] * 10000)
                    continue
                tgt = int(o.get("qty_target") or 0)
                if tgt <= 0 or tgt >= int(o["qty_max"]):
                    tier1.append(produced[o_idx] * _w1(o))
                    continue
                capped = model.NewIntVar(
                    0, tgt, f"prodcap_{o['order_id']}")
                model.AddMinEquality(capped, [produced[o_idx], tgt])
                tier1.append(capped * _w1(o))
                # pct=0 = no incentive at all: skip the term, don't just
                # zero its coefficient. The target cap above stays either
                # way — fulfillment reward never exceeds qty_target.
                if over_coeff > 0:
                    over_terms.append(produced[o_idx] - capped)
            prod_sum = sum(tier1)
            if over_terms:
                over_sum = sum(over_terms)
            # Exposed so a two-pass caller can read pass 1's fill score and
            # floor pass 2 on it (includes week gradient + target caps —
            # holding this scalar holds both total fill and week allocation).
            prod_score = model.NewIntVar(0, 10**12, "prod_score")
            model.Add(prod_score == prod_sum)
        else:
            prod_score = None
            prod_sum = sum(
                produced[o_idx] * (10 if orders[o_idx].get("is_current_mo") else 1)
                for o_idx in range(len(orders))
            )
        # Production is the primary objective.  Secondary terms from the
        # user's selected objective mode act as tiebreakers so the solver
        # honours changeover / idle / CIP preferences when production is
        # equal.  Production is scaled so it always dominates.
        secondary = 0
        if objective_mode == "min-changeovers":
            co_term = (
                weighted_co_total * 100
                if use_weighted_co
                else flat_co_total * 10000
            )
            secondary = co_term + total_idle * W_idle + cip_cost_total
        elif objective_mode == "spread-load":
            co_term = (
                weighted_co_total
                if use_weighted_co
                else flat_co_total * 10
            )
            secondary = co_term + makespan + total_idle * W_idle + cip_cost_total
        else:  # balanced (default)
            W1 = P.objective_makespan_weight
            W2 = P.objective_changeover_weight
            co_term = (
                weighted_co_total * W2
                if use_weighted_co
                else flat_co_total * W2
            )
            secondary = makespan * W1 + co_term + total_idle * W_idle + cip_cost_total
        # Scale production so it always dominates secondary terms: one
        # tier-1 kg is worth ~1,000 x 1,000 = 1e6 objective units, so under
        # the Scenario F weights (x300) one FFS change (605) is 181,500 =
        # 0.18 kg, one topload change (455) 0.137 kg, a TTP-only switch (10)
        # 0.003 kg (live secondary total ~10 M = ~10 kg). Fill is never
        # sacrificed for a preference in pass 1 — which is also why pass 1
        # alone cannot minimize changeovers; that is pass 2's job.
        pass2 = prod_score is not None and (
            min_prod_score is not None or fill_exchange_rate is not None)
        if pass2:
            # PASS 2 (two-pass CO minimization): fill quality is a FLOOR,
            # changeover load is the objective. co_term already carries the
            # per-machine weights (FFS/topload >> TTP) times W2; the x100
            # keeps it above idle/makespan tiebreakers.
            if min_prod_score is not None:
                model.Add(prod_score >= int(min_prod_score))
            co_min = (weighted_co_total if use_weighted_co
                      else flat_co_total * 100)
            if fill_exchange_rate is not None:
                # Fix SA-3 (audit C73/F4): fill stays IN the objective at an
                # explicit exchange rate K — one pass-2 changeover unit
                # (co_min x 100) is worth K fill units (kg x tier weight), so
                # the solver only gives up fill when the changeover it saves
                # is worth more than the kg it costs; a tail is never shaved
                # for a makespan unit (weight 1 vs ~1010 per kg) and a small
                # order is never deleted unless its kg are worth less than the
                # changeover it forces. See default_fill_exchange_rate.
                K = max(1, int(fill_exchange_rate))
                # Kg-week deviation in the pass-2 currency: weight / 1000
                # per kg-week = 20 % (late) / 5 % (early) of the kg's fill
                # value per week at the defaults; one FFS change ~ 2,000
                # kg-eq, so 10 t one week late ~ one FFS change (v2 pricing,
                # 2026-09-04 #2).
                # Makespan: [scheduler] pass2_makespan_weight (default 1 =
                # a pure tiebreaker: one hour of shorter plan is worth 1
                # unit ~ 0.001 kg of fill). 1 kg-eq ~ 1,000 pass-2 units, so
                # a weight of 100,000 makes one hour of shorter plan worth
                # 100 kg-eq = 0.1 t of fill, 1,000,000 makes it 1 t.
                W_ms2 = max(0, int(getattr(P, "pass2_makespan_weight", 1)))
                model.Minimize(
                    co_min * 100 * K + makespan * W_ms2 + total_idle * W_idle
                    + cip_cost_total + late_total * W_late
                    + week_pen + _dev_pen(None) - prod_score
                )
            else:
                model.Minimize(
                    co_min * 100 + makespan + total_idle * W_idle
                    + cip_cost_total + late_total * W_late + week_pen
                    + _dev_pen(None)
                )
        else:
            # Pass 1: fill (1e6 per kg) > kg-week deviation (200,000 late /
            # 50,000 early per kg-step at the defaults = 20 % / 5 % of the
            # kg's fill value per week, the SAME fraction pass 2 prices --
            # PASS1_FILL_SCALE) >> changeovers, so late or early production
            # still beats no production (up to 4 weeks late) and the nearer
            # week wins a contested hour by 5 % per week.
            model.Maximize(
                prod_sum * 1000 + over_sum * over_coeff
                - secondary - late_total * W_late - week_pen
                - _dev_pen(None, PASS1_FILL_SCALE)
            )
    elif objective_mode == "min-changeovers":
        obj = model.NewIntVar(-(10**12), 10**12, "obj")
        if use_weighted_co:
            # Weighted: topload changes cost much more than other transitions
            model.Add(
                obj == weighted_co_total * 100
                + makespan
                + total_idle * W_idle
                + cip_cost_total
                + late_total * W_late
                + week_pen
                + _dev_pen(100)
                + shortfall_pen
            )
        else:
            model.Add(
                obj == flat_co_total * 10000
                + makespan
                + total_idle * W_idle
                + cip_cost_total
                + late_total * W_late
                + week_pen
                + _dev_pen(100)
                + shortfall_pen
            )
        model.Minimize(obj)
    elif objective_mode == "spread-load":
        max_line_run = model.NewIntVar(
            0, P.horizon_h, "max_line_run"
        )
        line_runs = []
        for l in lines:
            lr = model.NewIntVar(
                0, P.horizon_h, f"line_run_total_{l}"
            )
            model.Add(
                lr
                == sum(
                    run_h[(l, o_idx)]
                    for o_idx in range(len(orders))
                )
            )
            line_runs.append(lr)
        if line_runs:
            model.AddMaxEquality(max_line_run, line_runs)
        else:
            model.Add(max_line_run == 0)
        obj = model.NewIntVar(-(10**12), 10**12, "obj")
        if use_weighted_co:
            model.Add(
                obj
                == max_line_run * 1000
                + weighted_co_total
                + makespan
                + total_idle * W_idle
                + cip_cost_total
                + late_total * W_late
                + week_pen
                + _dev_pen(1)
                + shortfall_pen
            )
        else:
            model.Add(
                obj
                == max_line_run * 1000
                + flat_co_total * 10
                + makespan
                + total_idle * W_idle
                + cip_cost_total
                + late_total * W_late
                + week_pen
                + _dev_pen(1)
                + shortfall_pen
            )
        model.Minimize(obj)
    else:  # balanced (default)
        W1 = P.objective_makespan_weight
        W2 = P.objective_changeover_weight
        obj = model.NewIntVar(-(10**12), 10**12, "obj")
        if use_weighted_co:
            # Weighted changeover cost replaces flat count * W2.
            # W2 is still used as a scaling multiplier.
            model.Add(
                obj == makespan * W1
                + weighted_co_total * W2
                + total_idle * W_idle
                + cip_cost_total
                + late_total * W_late
                + week_pen
                + _dev_pen(W2)
                + shortfall_pen
            )
        else:
            model.Add(
                obj == makespan * W1
                + flat_co_total * W2
                + total_idle * W_idle
                + cip_cost_total
                + late_total * W_late
                + week_pen
                + _dev_pen(W2)
                + shortfall_pen
            )
        model.Minimize(obj)

    vars_dict = {
        # Setup TIME between different SKUs is enforced whenever the phase
        # carries the changeover block (fix SA-6: ignore_co at relax level 3
        # drops only the PRICE). phase2_scheduler's level-3 label reads it.
        "setup_times_enforced": phase in ("sanity3", "full"),
        "present": present,
        "run_h": run_h,
        "seg_a_start": seg_a_start,
        "seg_a_end": seg_a_end,
        "seg_a_run": seg_a_run,
        "seg_b_present": seg_b_present,
        "seg_b_start": seg_b_start,
        "seg_b_end": seg_b_end,
        "seg_b_run": seg_b_run,
        "eff_end": eff_end,
        "produced": produced,
        "cip_vars": cip_model_vars,
        # Per-CIP count cost expression (fix SB-4), plain int 0 without CIP
        # slots; and the model-build warnings (fix SB-3: lines already past
        # their CIP interval at the gate).
        "cip_cost": cip_cost_total,
        "warnings": cip_warnings,
        "lateness": lateness,
        # Soft due weeks (plant decision 2026-09-04 #2): the policy in force,
        # the kg-week deviation totals (LinearExpr, or plain int 0 when
        # nothing is priced / built) and the per-(line, order) kg late / early
        # (step 1 x rate: kg outside the week at all). phase2_scheduler's
        # feasibility report reads them.
        "due_week_policy": due_week_policy_of(P),
        "late_kg_weeks": late_kg_weeks,
        "early_kg_weeks": early_kg_weeks,
        "late_kg": late_kg_by_pair,
        "early_kg": early_kg_by_pair,
        # Per (line, order): the kg of each week-step (step k = kg k or more
        # weeks outside the window), so a report can sum kg-weeks per order.
        "late_kg_steps": {key: [ir_dev[key] * ha for ha in has]
                          for key, has in late_steps.items()},
        "early_kg_steps": {key: [ir_dev[key] * hb for hb in hbs]
                           for key, hbs in early_steps.items()},
        # (early, late) per kg-week in each currency (v2 pricing): "pass2"
        # = weight / 1000 (fill ~ 1,000 per kg; also the non-fill base),
        # "pass1" = weight (fill = 1e6 per kg), "used" = the pair the built
        # objective branch applied (None when no branch priced deviation).
        "dev_kg_coefficients": {
            "pass1": (c_early_p1, c_late_p1),
            "pass2": (c_early_raw, c_late_raw),
            "used": dev_used["coeff"],
        },
        # (early, late) per (line, order): cross_week -> both IntVars; default
        # mode -> (early IntVar, 0) for every demand order whose start floor
        # lies before its due_start (fix SB-1; every later week under the
        # unbounded early-fill policy, plant decision 2026-09-04).
        "week_dev": week_dev,
        # Demand-derived week frame the floors were built from (fix SB-1).
        "week_frame": wk_frame,
        # soft-demand fill score (None otherwise) — pass 1 reads it, pass 2
        # floors on it (two-pass CO minimization)
        "prod_score": prod_score,
        # Changeover-load expression for live telemetry: the weighted CO
        # cost when per-machine weights exist, else the flat switch count.
        # A plain int 0 (no lines / ignore_co) means "nothing to watch".
        "co_load": weighted_co_total if use_weighted_co else flat_co_total,
        # Priced initial changeovers (fix SA-2): sum of first_i * cost; a
        # plain int 0 when none is priced.
        "co_init_load": sum(co_init_cost_terms) if co_init_cost_terms else 0,
        # Per-line idle IntVars (fix SA-10): span from the availability gate
        # to the last end minus production and CIP hours. Empty when
        # objective_idle_weight == 0.
        "line_idle": line_idle_by_line,
    }
    return model, vars_dict
