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


def build_model(
    P: Params,
    data: Data,
    phase: str,
    relax_demand: bool,
    ignore_co: bool,
    max_lines_per_order_override: Optional[int] = None,
    maximize_production: bool = False,
    objective_mode: str = "balanced",
) -> Tuple[cp_model.CpModel, Dict[str, Any]]:
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
            model.Add(seg_a_start[key] >= ds_eff).OnlyEnforceIf(present[key])
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

            # Capability / run bounds
            r = data.rate.get((l, o["sku"]))
            cap = data.capable.get((l, o["sku"]))
            if (cap is None) or (cap == 0) or (r is None) or (r <= 0):
                model.Add(present[key] == 0)
                model.Add(run_h[key] == 0)
            else:
                qmin = int(o["qty_min"])
                min_run_from_pct = (
                    math.ceil(P.min_run_pct_of_qty * qmin / r) if r > 0 else 0
                )
                min_run = min(
                    max_len, max(1, P.min_run_hours, min_run_from_pct)
                )
                model.Add(run_h[key] >= min_run).OnlyEnforceIf(present[key])
                model.Add(run_h[key] == 0).OnlyEnforceIf(present[key].Not())
                # Per-segment minimums (avoid wasteful short stubs)
                model.Add(
                    seg_a_run[key] >= P.min_run_hours
                ).OnlyEnforceIf(present[key])
                model.Add(
                    seg_b_run[key] >= P.min_run_hours
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
        qmin = int(o["qty_min"]) if not relax_demand else 0
        qmax = int(o["qty_max"])
        model.Add(prod >= qmin)
        model.Add(prod <= qmax)
        # Trials are always pinned to exactly 1 line; skip mlpo constraint
        if not o.get("is_trial"):
            model.Add(sum(present[(l, o_idx)] for l in lines) <= mlpo)

    # ── Changeover constraints (pairwise ordering + setup times) ──────────
    #
    # Successor variables track which order *immediately follows* which on
    # each line.  This lets us compute a weighted changeover cost where
    # topload-format changes are penalized more heavily than other changes.
    succ = {}           # (l, i_idx, j_idx) -> BoolVar: j immediately follows i
    weighted_co_cost_per_line = []  # list of IntVars (one per line)

    if (phase in ("sanity3", "full")) and (not ignore_co):
        # Per-machine changeover weights
        W_top = P.co_topload_weight
        W_ttp = P.co_ttp_weight
        W_ffs = P.co_ffs_weight
        W_cp = P.co_casepacker_weight
        W_base = P.co_base_weight

        for l in lines:
            elig = [
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
                    model.Add(
                        seg_a_start[(l, i_idx)]
                        <= seg_a_start[(l, j_idx)]
                    ).OnlyEnforceIf(first_i)
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
                    # j before i: i's seg_a starts after j's effective end
                    model.Add(
                        seg_a_start[(l, i_idx)]
                        >= eff_end[(l, j_idx)] + setup_ji
                    ).OnlyEnforceIf(b_ij.Not())

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
                        {"ttp": 1, "ffs": 1, "topload": 1, "casepacker": 1},
                    )
                    pair_cost = (
                        W_base
                        + W_top * mc["topload"]
                        + W_ttp * mc["ttp"]
                        + W_ffs * mc["ffs"]
                        + W_cp * mc["casepacker"]
                    )
                    if pair_cost > 0:
                        co_cost_terms.append(succ[key] * pair_cost)

            if co_cost_terms:
                max_possible = len(elig) * (
                    W_base + W_top + W_ttp + W_ffs + W_cp
                )
                co_cost_l = model.NewIntVar(
                    0, max_possible, f"co_cost_l{l}"
                )
                model.Add(co_cost_l == sum(co_cost_terms))
                weighted_co_cost_per_line.append(co_cost_l)

    # ── Week-0 / Week-1 gap constraint ────────────────────────────────────
    if P.allow_week1_in_week0:
        week0_order_idxs = [
            o_idx
            for o_idx, o in enumerate(orders)
            if int(o["due_end"]) <= WEEK0_END
        ]
        week1_order_idxs = [
            o_idx
            for o_idx, o in enumerate(orders)
            if int(o["due_start"]) >= WEEK1_START
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
        interval = P.cip_interval_h
        for l in lines:
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
            model.Add(c2s <= c1e + interval).OnlyEnforceIf(b2)

            # CIP 3: wide window from c2e to c2e + interval
            c3s = model.NewIntVar(0, H, f"cip3_s_l{l}")
            c3e = model.NewIntVar(0, H, f"cip3_e_l{l}")
            c3_int = model.NewOptionalIntervalVar(
                c3s, dur, c3e, b3, f"cip3_l{l}"
            )
            line_intervals[l].append(c3_int)
            model.Add(c3s >= c2e).OnlyEnforceIf(b3)
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
    # Incentivise the solver to push CIPs as close to the 120h deadline as
    # possible by adding sum(cip_starts) to the objective.  Absent CIPs
    # contribute 0 so they don't distort the term.
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

    if maximize_production:
        prod_sum = sum(produced[o_idx] for o_idx in range(len(orders)))
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
        model.Maximize(prod_sum * 1000 - secondary)
    elif objective_mode == "min-changeovers":
        obj = model.NewIntVar(-(10**12), 10**12, "obj")
        if use_weighted_co:
            # Weighted: topload changes cost much more than other transitions
            model.Add(
                obj == weighted_co_total * 100
                + makespan
                + total_idle * W_idle
                - cip_defer_total * W_cip
            )
        else:
            model.Add(
                obj == flat_co_total * 10000
                + makespan
                + total_idle * W_idle
                - cip_defer_total * W_cip
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
            )
        else:
            model.Add(
                obj
                == max_line_run * 1000
                + flat_co_total * 10
                + makespan
                + total_idle * W_idle
                - cip_defer_total * W_cip
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
            )
        else:
            model.Add(
                obj == makespan * W1
                + flat_co_total * W2
                + total_idle * W_idle
                - cip_defer_total * W_cip
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
    }
    return model, vars_dict
