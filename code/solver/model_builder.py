# model_builder.py — CP-SAT model construction for Flowstate Phase 2 scheduler.
# CIP redesign: CIPs as first-class solver intervals; production splits via seg_a/seg_b.

from __future__ import annotations
import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from ortools.sat.python import cp_model

from data_loader import Params, Data, available_hours_line

WEEK0_END = 167  # due_end_hour <= 167 -> Week 0 (Saturday 23:00 from Mon 00:00)
WEEK1_START = 168  # Week-1 orders have due_start >= 168
WEEK0_FILL_START = 120
MAX_GAP_W0_W1_HOURS = 1


def _producible_kg_in_window(P: Params, data: Data, o: dict, lines) -> int:
    """Provable upper bound on what ONE order can produce in its due window.

    Sum over capable lines of usable hours in [ds_eff, min(H, de+1)] × rate,
    where usable subtracts the line's availability gate and downtime overlaps.
    ds_eff mirrors the interval construction in build_model exactly (week-1
    orders may fill week 0 from WEEK0_FILL_START when allow_week1_in_week0).
    Contention with other orders and min-run rounding are deliberately
    ignored — the bound must only ever OVER-estimate, so clamping qty_min to
    it removes provably-impossible demand and nothing else.
    """
    H = P.horizon_h
    ds_raw, de = int(o["due_start"]), int(o["due_end"])
    if ds_raw > WEEK0_END and getattr(P, "allow_week1_in_week0", False):
        ds_eff = WEEK0_FILL_START
    else:
        ds_eff = ds_raw
    win_end = min(H, de + 1)
    total = 0.0
    for l in lines:
        r = data.rate.get((l, o["sku"])) or 0
        if r <= 0:
            continue
        gate = max(0, data.init_map.get(l, {}).get("available_from", 0))
        a = max(0, ds_eff, gate)
        b = win_end
        if b <= a:
            continue
        usable = float(b - a)
        for dt in data.downtimes:
            if dt["line_id"] != l:
                continue
            overlap = min(b, dt["end"]) - max(a, dt["start"])
            if overlap > 0:
                usable -= overlap
        if usable > 0:
            total += usable * r
    return int(total)


def _usable_hours_on_line(P: Params, data: Data, l: int,
                          ds_eff: int, de: int) -> float:
    """Hours line `l` can actually host work inside [ds_eff, de+1]:
    window minus the availability gate and downtime overlaps. Zero means the
    pair is DEAD — no feasible schedule can place this order there."""
    b = min(P.horizon_h, de + 1)
    gate = max(0, data.init_map.get(l, {}).get("available_from", 0))
    a = max(0, ds_eff, gate)
    if b <= a:
        return 0.0
    usable = float(b - a)
    for dt in data.downtimes:
        if dt["line_id"] != l:
            continue
        overlap = min(b, dt["end"]) - max(a, dt["start"])
        if overlap > 0:
            usable -= overlap
    return max(0.0, usable)


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
) -> Tuple[cp_model.CpModel, Dict[str, Any]]:
    """min_prod_score (two-pass CO minimization, 2026-08-17): when set —
    soft-demand + maximize_production only — the weighted fill score
    (tier-1 production incl. week gradient) becomes a HARD floor and the
    objective flips to MINIMIZING the weighted changeover load. Pass 1
    maximizes fill; pass 2 re-solves holding >= (1-eps) of that fill and
    buys back changeovers the flat trade never could."""
    model = cp_model.CpModel()
    orders = data.orders
    lines = data.lines
    H = P.horizon_h
    mlpo = (
        P.max_lines_per_order
        if max_lines_per_order_override is None
        else max_lines_per_order_override
    )

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
    lateness = {}       # IntVar: hours past due_end+1 (only when relax_due)
    week_dev = {}       # (early, late) IntVars: AZAP-week deviation (cross_week)

    for l in lines:
        for o_idx, o in enumerate(orders):
            key = (l, o_idx)
            oid = o["order_id"]
            present[key] = model.NewBoolVar(f"present_l{l}_o{oid}")
            ds_raw, de = int(o["due_start"]), int(o["due_end"])
            # Week-1 orders can fill end of Week-0 if allowed
            if ds_raw > WEEK0_END and P.allow_week1_in_week0:
                ds_eff = WEEK0_FILL_START
            else:
                ds_eff = ds_raw
            max_len = max(0, min(H, de + 1) - max(0, ds_eff))
            # Dead-pair pruning (2026-08-14): a normal order whose window on
            # this line is fully eaten by the gate/downtimes can NEVER run
            # here. Forcing absence up front (and excluding the pair from the
            # changeover web below) removes thousands of interval + pairwise
            # vars — in fill mode most lines are gated deep into the horizon
            # and the search was drowning (600s moved W35 fill by only 50t).
            if (not o.get("is_current_mo") and not o.get("is_trial")
                    and not relax_due
                    and _usable_hours_on_line(P, data, l, max(0, ds_eff), de) <= 0):
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

            if not continue_due_block:
                pass
            elif relax_due:
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
                    # qty_min == qty_max == remaining for current MOs
                    # (data_loader.py:315-316): prod is pinned to the remaining
                    # kg, so a floor above remaining/rate is unsatisfiable
                    # (integer run hours x rate > remaining -> INFEASIBLE at
                    # every relax level, dispatch 5 A/B). The floor applies
                    # only when the remaining work physically supports it.
                    max_hours_qty = (qmin / r) if (r is not None and r > 0) else 0.0
                    if max_hours_qty >= P.min_run_hours:
                        min_run = min(max_len, max(1, P.min_run_hours))
                    else:
                        min_run = 0
                else:
                    min_run_from_pct = (
                        math.ceil(P.min_run_pct_of_qty * qmin / r) if r > 0 else 0
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

    # ── NoOverlap prep: collect intervals per line ────────────────────────
    line_intervals = {l: [] for l in lines}
    for l in lines:
        for o_idx in range(len(orders)):
            key = (l, o_idx)
            line_intervals[l].append(seg_a_interval[key])
            line_intervals[l].append(seg_b_interval[key])
    for dt in data.downtimes:
        l = dt["line_id"]
        if l in line_intervals:
            s = max(0, dt["start"])
            e = min(H, dt["end"])
            d = max(0, e - s)
            if d > 0:
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
        # An order with NO capable, available line (e.g. 570560/280698 absent
        # from capabilities, or one only fitting a fully-down line) can never
        # be produced — zero its floor so it reports "short of qmin" instead of
        # making the whole model INFEASIBLE.
        producible = any(
            (data.rate.get((l, o["sku"])) or 0) > 0
            and available_hours_line(P, data, l) > 0
            for l in lines)
        qmin_raw = (int(o["qty_min"]) if not relax_demand else 0) if producible else 0
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
        qmin = qmin_raw
        if qmin_raw > 0 and not o.get("is_current_mo") and not o.get("is_trial"):
            cap_kg = _producible_kg_in_window(P, data, o, lines)
            if cap_kg < qmin_raw:
                qmin = cap_kg
                print(f"[producible] {o['order_id']}: qty_min {qmin_raw} -> "
                      f"{qmin} (window capacity: capable lines x usable "
                      f"hours in [{int(o['due_start'])},{int(o['due_end'])}+1])")
        qmax = int(o["qty_max"])
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


    # ── Changeover constraints (pairwise ordering + setup times) ──────────
    #
    # Successor variables track which order *immediately follows* which on
    # each line.  This lets us compute a weighted changeover cost where
    # topload-format changes are penalized more heavily than other changes.
    succ = {}           # (l, i_idx, j_idx) -> BoolVar: j immediately follows i
    weighted_co_cost_per_line = []  # list of IntVars (one per line)
    cip_absorbable = []  # (l, i_idx, j_idx, succ_key, delta) for CIP absorption

    if (phase in ("sanity3", "full")) and (not ignore_co):
        # Per-machine changeover weights
        W_top = P.co_topload_weight
        W_ttp = P.co_ttp_weight
        W_ffs = P.co_ffs_weight
        W_cp = P.co_casepacker_weight
        W_base = P.co_base_weight
        W_conv_org = P.co_conv_org_weight
        W_cinn = P.co_cinn_weight
        W_flavor = P.co_flavor_weight

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
            co_cost_terms = []
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
                    mc = data.machine_changes.get(
                        (i_sku, j_sku),
                        {"ttp": 1, "ffs": 1, "topload": 1, "casepacker": 1,
                         "conv_to_org": 0, "cinn_to_non": 0, "added_flavors": 0},
                    )
                    # added_flavors can be negative (reward for removing flavors)
                    flavor_cost = W_flavor * mc.get("added_flavors", 0)
                    pair_cost = max(0, (
                        W_base
                        + W_top * mc["topload"]
                        + W_ttp * mc["ttp"]
                        + W_ffs * mc["ffs"]
                        + W_cp * mc["casepacker"]
                        + W_conv_org * mc.get("conv_to_org", 0)
                        + W_cinn * mc.get("cinn_to_non", 0)
                        + flavor_cost
                    ))
                    if pair_cost > 0:
                        co_cost_terms.append(succ[key] * pair_cost)
                        # Track pairs where a CIP between orders can absorb
                        # conv→org / cinn→non penalties.
                        absorb_delta = (
                            W_conv_org * mc.get("conv_to_org", 0)
                            + W_cinn * mc.get("cinn_to_non", 0)
                        )
                        if absorb_delta > 0:
                            cip_absorbable.append(
                                (l, i_idx, j_idx, key, absorb_delta)
                            )

            if co_cost_terms:
                max_possible = len(elig) * (
                    W_base + W_top + W_ttp + W_ffs + W_cp + W_conv_org + W_cinn
                )
                co_cost_l = model.NewIntVar(
                    0, max_possible, f"co_cost_l{l}"
                )
                model.Add(co_cost_l == sum(co_cost_terms))
                weighted_co_cost_per_line.append(co_cost_l)

    # ── Week-0 / Week-1 gap constraint ────────────────────────────────────
    # Skipped in cross-week mode: that constraint pins Week-1 production to
    # start immediately after the line's last Week-0 order, which structurally
    # forbids the interleaving/merging cross-week mode exists to allow.
    if P.allow_week1_in_week0 and not cross_week:
        # The week-boundary stitch (gap <= MAX_GAP_W0_W1_HOURS) is a FRESH-PLAN
        # rule: don't leave an idle wall between weeks the solver itself
        # planned. Committed manprg MOs are exempt — their windows are plant
        # fact with HARD start floors, and classifying them here forced
        # impossible stitches (measured 2026-08-14: P09's forced-w0 MOs end
        # <= h151 while its forced-w1 MO starts >= h177 — a 26h wall the 1h
        # gap rule forbids at hard dues, one root of the level-0/1
        # infeasibility in current-MO mode).
        week0_order_idxs = [
            o_idx
            for o_idx, o in enumerate(orders)
            if int(o["due_end"]) <= WEEK0_END
            and not o.get("is_current_mo")
        ]
        week1_order_idxs = [
            o_idx
            for o_idx, o in enumerate(orders)
            if int(o["due_start"]) >= WEEK1_START
            and not o.get("is_current_mo")
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

    cip_model_vars: Dict[int, list] = {}
    if phase == "full":
        dur = P.cip_duration_h
        for l in lines:
            # Per-line CIP interval from line_cip_hrs.csv; falls back to global setting
            interval = data.cip_interval_map.get(l, P.cip_interval_h)
            carry = int(
                data.init_map.get(l, {}).get("carryover_run_hours", 0)
            )
            avail_from = int(
                data.init_map.get(l, {}).get("available_from", 0)
            )

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

            # Clock-based CIP trigger ─────────────────────────────────
            # CIP is mandatory when wall-clock span of production on a
            # line (plus carryover from prior horizon) reaches 120 h.
            s_or_H_list = []
            e_or_0_list = []
            for o_idx in elig_idxs:
                key = (l, o_idx)
                s_oH = model.NewIntVar(0, H, f"cipSoH_l{l}_o{o_idx}")
                model.Add(s_oH == seg_a_start[key]).OnlyEnforceIf(
                    present[key]
                )
                model.Add(s_oH == H).OnlyEnforceIf(present[key].Not())
                s_or_H_list.append(s_oH)
                e_o0 = model.NewIntVar(0, H, f"cipEo0_l{l}_o{o_idx}")
                model.Add(e_o0 == eff_end[key]).OnlyEnforceIf(
                    present[key]
                )
                model.Add(e_o0 == 0).OnlyEnforceIf(present[key].Not())
                e_or_0_list.append(e_o0)

            first_start_l = model.NewIntVar(0, H, f"first_start_l{l}")
            model.AddMinEquality(first_start_l, s_or_H_list)
            last_end_l = model.NewIntVar(0, H, f"last_end_l{l}")
            model.AddMaxEquality(last_end_l, e_or_0_list)

            any_on_line = model.NewBoolVar(f"any_on_line_l{l}")
            model.Add(
                sum(present[(l, oi)] for oi in elig_idxs) >= 1
            ).OnlyEnforceIf(any_on_line)
            model.Add(
                sum(present[(l, oi)] for oi in elig_idxs) == 0
            ).OnlyEnforceIf(any_on_line.Not())

            clock_span = model.NewIntVar(0, H, f"clock_span_l{l}")
            model.Add(
                clock_span == last_end_l - first_start_l
            ).OnlyEnforceIf(any_on_line)
            model.Add(clock_span == 0).OnlyEnforceIf(any_on_line.Not())

            # CIP needed flags
            b1 = model.NewBoolVar(f"cip1_needed_l{l}")
            b2 = model.NewBoolVar(f"cip2_needed_l{l}")
            b3 = model.NewBoolVar(f"cip3_needed_l{l}")
            model.Add(clock_span + carry >= interval).OnlyEnforceIf(b1)
            model.Add(
                clock_span + carry <= interval - 1
            ).OnlyEnforceIf(b1.Not())
            model.Add(
                clock_span + carry >= 2 * interval
            ).OnlyEnforceIf(b2)
            model.Add(
                clock_span + carry <= 2 * interval - 1
            ).OnlyEnforceIf(b2.Not())
            model.Add(
                clock_span + carry >= 3 * interval
            ).OnlyEnforceIf(b3)
            model.Add(
                clock_span + carry <= 3 * interval - 1
            ).OnlyEnforceIf(b3.Not())
            model.AddImplication(b2, b1)
            model.AddImplication(b3, b2)

            remaining = max(0, interval - carry)

            # CIP 1: wide window — can start from avail_from up to deadline
            c1s = model.NewIntVar(0, H, f"cip1_s_l{l}")
            c1e = model.NewIntVar(0, H, f"cip1_e_l{l}")
            c1_int = model.NewOptionalIntervalVar(
                c1s, dur, c1e, b1, f"cip1_l{l}"
            )
            line_intervals[l].append(c1_int)
            model.Add(c1s >= avail_from).OnlyEnforceIf(b1)
            # ── CIP MAX-INTERVAL DEADLINE: HARD, ALWAYS ──────────────
            # FOOD-SAFETY / COMPLIANCE CONSTRAINT.  A CIP may be pulled
            # EARLIER than this point freely (the only lower bound is
            # avail_from), but it may NEVER start later.  `interval` here
            # is the line's max_cip_hrs from data/reference/line_cip_hrs.csv
            # (P09/P12/P14/P17-P22 = 120h, P10/P11/P13/P15/P16 = 144h).
            # DO NOT relax this at any relax level and DO NOT gate it on
            # cip_flex - exceeding max CIP hours is a compliance violation.
            #
            # CIP deadline is absolute from the line's availability, not
            # from when production first starts.  With carryover hours the
            # line has already been running *before* the planning horizon,
            # so the CIP is due at (avail + remaining) hours into the
            # horizon regardless of when production actually begins.
            model.Add(
                c1s <= avail_from + remaining
            ).OnlyEnforceIf(b1)

            # Cross-phase CIP deadline: if we know the absolute hour of the
            # previous CIP (from InitialStates), CIP 1 must start before
            # that time + interval to avoid a gap that the validator rejects.
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
                    last_cip_end_hour = int(
                        (cip_dt - anchor).total_seconds() / 3600
                    )
                    if 0 <= last_cip_end_hour < H:
                        abs_deadline = last_cip_end_hour + interval
                        model.Add(
                            c1s <= abs_deadline
                        ).OnlyEnforceIf(b1)
                except (ValueError, TypeError):
                    pass  # bad datetime — fall back to relative window

            # CIP 2: wide window from c1e to c1e + interval
            c2s = model.NewIntVar(0, H, f"cip2_s_l{l}")
            c2e = model.NewIntVar(0, H, f"cip2_e_l{l}")
            c2_int = model.NewOptionalIntervalVar(
                c2s, dur, c2e, b2, f"cip2_l{l}"
            )
            line_intervals[l].append(c2_int)
            model.Add(c2s >= c1e).OnlyEnforceIf(b2)
            # HARD max-interval (see CIP 1 note): never later than one full
            # line interval after the previous CIP ended.  Earlier is free.
            model.Add(c2s <= c1e + interval).OnlyEnforceIf(b2)

            # CIP 3: wide window from c2e to c2e + interval
            c3s = model.NewIntVar(0, H, f"cip3_s_l{l}")
            c3e = model.NewIntVar(0, H, f"cip3_e_l{l}")
            c3_int = model.NewOptionalIntervalVar(
                c3s, dur, c3e, b3, f"cip3_l{l}"
            )
            line_intervals[l].append(c3_int)
            model.Add(c3s >= c2e).OnlyEnforceIf(b3)
            # HARD max-interval (see CIP 1 note). Earlier is free.
            model.Add(c3s <= c2e + interval).OnlyEnforceIf(b3)

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
                total_run_l + dur * b1 + dur * b2 + dur * b3 <= avail_h
            )

            cip_model_vars[l] = [
                (c1s, c1e, b1),
                (c2s, c2e, b2),
                (c3s, c3e, b3),
            ]

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
            ).OnlyEnforceIf(b1)

            # seg_b requires a CIP between its segments ───────────────
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
                # If seg_b is present, at least one CIP must be between
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

    # ── CIP deferral: collect present-CIP starts for objective term ──────
    #
    # WHAT cip_defer DOES: sum(cip_starts) enters every objective branch as
    # `- cip_defer_total * W_cip`, i.e. a REWARD for a later CIP start.  It
    # exists so CIPs drift toward their legal deadline instead of being
    # dumped at hour 0, which would forfeit usable run hours.
    #
    # THE TENSION with the plant rule ("a CIP may be moved EARLIER, never
    # later than the line's max interval"): with a large W_cip the solver
    # always parks a CIP at the last legal moment, so it will never pull one
    # forward even when doing so would absorb a changeover (a 6h CIP is
    # longer than any changeover, so a CIP landing between two different
    # SKUs makes that changeover free).
    #
    # cip_flex resolves it: when enabled, W_cip is scaled down by
    # P.objective_cip_flex_weight / 100 so the deferral reward no longer
    # dominates the changeover savings and pulling a CIP forward a few hours
    # becomes a decision the solver can actually make.  The max-interval
    # deadline above is untouched and stays HARD - only the *preference* for
    # lateness is softened, never the legal limit.
    all_cip_starts: list = []
    for l in lines:
        if l in cip_model_vars:
            for ck_s, ck_e, ck_b in cip_model_vars[l]:
                w_s = model.NewIntVar(
                    0, H, f"cip_defer_{l}_{len(all_cip_starts)}"
                )
                model.Add(w_s == ck_s).OnlyEnforceIf(ck_b)
                model.Add(w_s == 0).OnlyEnforceIf(ck_b.Not())
                all_cip_starts.append(w_s)
    cip_defer_total = sum(all_cip_starts) if all_cip_starts else 0
    W_cip = P.objective_cip_defer_weight
    if cip_flex:
        W_cip = max(
            0,
            (P.objective_cip_defer_weight
             * P.objective_cip_flex_weight) // 100,
        )

    # ── CIP absorption: waive conv→org / cinn→non when CIP is between ──
    #
    # When a CIP falls between two adjacent orders, the line is fully
    # cleaned, so conv_to_org and cinn_to_non changeover penalties are
    # absorbed.  We give a bonus (cost reduction) equal to the waived
    # penalty whenever the solver places a CIP between such a pair.
    cip_absorb_bonus_terms = []
    if cip_absorbable and cip_model_vars:
        for l_ab, i_ab, j_ab, skey, delta in cip_absorbable:
            cip_vars_l = cip_model_vars.get(l_ab, [])
            if not cip_vars_l:
                continue
            absorb_vars = []
            for k, (ck_s, ck_e, ck_b) in enumerate(cip_vars_l):
                ab = model.NewBoolVar(
                    f"cipAb_l{l_ab}_o{i_ab}_{j_ab}_c{k}"
                )
                model.AddImplication(ab, succ[skey])
                model.AddImplication(ab, ck_b)
                model.Add(
                    eff_end[(l_ab, i_ab)] <= ck_s
                ).OnlyEnforceIf(ab)
                model.Add(
                    ck_e <= seg_a_start[(l_ab, j_ab)]
                ).OnlyEnforceIf(ab)
                absorb_vars.append(ab)
            if absorb_vars:
                model.Add(sum(absorb_vars) <= 1)
                for ab in absorb_vars:
                    cip_absorb_bonus_terms.append(ab * delta)

    # ── Line compactness (idle-time penalty) ──────────────────────────────
    #
    # Penalize per-line idle time: span − production − CIP hours.
    # CIP-segmented blocks (seg_a → CIP → seg_b) are NOT penalized because
    # the CIP hours are subtracted from the span.  Only true dead-time
    # (gaps between runs or between a run and a changeover) is penalized.
    line_idle_vars: list = []
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

            # Span = last end − first start
            span_c = model.NewIntVar(0, H, f"cidle_sp_l{l}")
            model.Add(
                span_c == last_e - first_s
            ).OnlyEnforceIf(any_c)
            model.Add(span_c == 0).OnlyEnforceIf(any_c.Not())

            # Total production hours on this line
            prod_c = model.NewIntVar(0, H, f"cidle_pr_l{l}")
            model.Add(
                prod_c
                == sum(
                    run_h[(l, o_idx)]
                    for o_idx in range(len(orders))
                )
            )

            # CIP hours on this line (subtracted so CIP splits aren't penalized)
            cip_h_expr = 0
            if l in cip_model_vars and cip_model_vars[l]:
                cip_h_expr = sum(
                    dur_cip * ck_b
                    for _, _, ck_b in cip_model_vars[l]
                )

            # Idle = span − production − CIP hours  (≥ 0 by NoOverlap)
            idle_c = model.NewIntVar(0, H, f"cidle_l{l}")
            model.Add(
                idle_c == span_c - prod_c - cip_h_expr
            ).OnlyEnforceIf(any_c)
            model.Add(idle_c == 0).OnlyEnforceIf(any_c.Not())

            line_idle_vars.append(idle_c)

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

    # AZAP week-deviation penalty (cross_week only; empty dict -> 0 otherwise).
    # Cost per hour an order runs outside the week AZAP asked for. Added to
    # every objective branch so the mode behaves identically in all of them.
    week_dev_total = (
        sum(e + lt for (e, lt) in week_dev.values()) if week_dev else 0
    )
    W_week = P.objective_week_deviation_weight
    week_pen = week_dev_total * W_week if week_dev else 0

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
            secondary = co_term + total_idle * W_idle - cip_defer_total * W_cip
        elif objective_mode == "spread-load":
            co_term = (
                weighted_co_total
                if use_weighted_co
                else flat_co_total * 10
            )
            secondary = co_term + makespan + total_idle * W_idle - cip_defer_total * W_cip
        else:  # balanced (default)
            W1 = P.objective_makespan_weight
            W2 = P.objective_changeover_weight
            co_term = (
                weighted_co_total * W2
                if use_weighted_co
                else flat_co_total * W2
            )
            secondary = makespan * W1 + co_term + total_idle * W_idle - cip_defer_total * W_cip
        # Scale production so it always dominates secondary terms.
        # Max secondary is ~50k; prod_sum * 1000 puts production in the
        # hundreds-of-millions range, guaranteeing it is never sacrificed.
        if min_prod_score is not None and prod_score is not None:
            # PASS 2 (two-pass CO minimization): fill quality is a FLOOR,
            # changeover load is the objective. co_term already carries the
            # per-machine weights (FFS/topload >> TTP) times W2; the x100
            # keeps it above idle/makespan tiebreakers.
            model.Add(prod_score >= int(min_prod_score))
            co_min = (weighted_co_total if use_weighted_co
                      else flat_co_total * 100)
            model.Minimize(
                co_min * 100 + makespan + total_idle * W_idle
                - cip_defer_total * W_cip + late_total * W_late + week_pen
            )
        else:
            model.Maximize(
                prod_sum * 1000 + over_sum * over_coeff
                - secondary - late_total * W_late - week_pen
            )
    elif objective_mode == "min-changeovers":
        obj = model.NewIntVar(-(10**12), 10**12, "obj")
        if use_weighted_co:
            # Weighted: topload changes cost much more than other transitions
            model.Add(
                obj == weighted_co_total * 100
                + makespan
                + total_idle * W_idle
                - cip_defer_total * W_cip
                + late_total * W_late
                + week_pen
                + shortfall_pen
            )
        else:
            model.Add(
                obj == flat_co_total * 10000
                + makespan
                + total_idle * W_idle
                - cip_defer_total * W_cip
                + late_total * W_late
                + week_pen
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
                - cip_defer_total * W_cip
                + late_total * W_late
                + week_pen
                + shortfall_pen
            )
        else:
            model.Add(
                obj
                == max_line_run * 1000
                + flat_co_total * 10
                + makespan
                + total_idle * W_idle
                - cip_defer_total * W_cip
                + late_total * W_late
                + week_pen
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
                - cip_defer_total * W_cip
                + late_total * W_late
                + week_pen
                + shortfall_pen
            )
        else:
            model.Add(
                obj == makespan * W1
                + flat_co_total * W2
                + total_idle * W_idle
                - cip_defer_total * W_cip
                + late_total * W_late
                + week_pen
                + shortfall_pen
            )
        model.Minimize(obj)

    vars_dict = {
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
        "lateness": lateness,
        "week_dev": week_dev,
        # soft-demand fill score (None otherwise) — pass 1 reads it, pass 2
        # floors on it (two-pass CO minimization)
        "prod_score": prod_score,
    }
    return model, vars_dict
