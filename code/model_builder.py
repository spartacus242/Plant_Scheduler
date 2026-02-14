# model_builder.py — CP-SAT model construction for Flowstate Phase 2 scheduler.

from __future__ import annotations
import math
from typing import Any, Dict, Optional, Tuple

from ortools.sat.python import cp_model

from data_loader import Params, Data, available_hours_line

WEEK0_END = 167  # due_end_hour <= 167 -> Week 0 (Saturday 23:00 from Mon 00:00)
WEEK1_START = 168  # Week-1 orders have due_start >= 168
# When allowing Week-1 in Week-0, Week-1 orders fill the tail of Week-0 from this hour to Saturday 23:00
WEEK0_FILL_START = 120
MAX_GAP_W0_W1_HOURS = 1  # max non-production (CIP/changeover only) between last W0 and first W1 on a line


def build_model(
    P: Params,
    data: Data,
    phase: str,
    relax_demand: bool,
    ignore_co: bool,
    max_lines_per_order_override: Optional[int] = None,
    maximize_production: bool = False,
) -> Tuple[cp_model.CpModel, Dict[str, Any]]:
    model = cp_model.CpModel()
    orders = data.orders
    lines = data.lines
    H = P.horizon_h
    mlpo = P.max_lines_per_order if max_lines_per_order_override is None else max_lines_per_order_override

    present, run_h, start, end, intervals = {}, {}, {}, {}, {}

    for l in lines:
        for o_idx, o in enumerate(orders):
            key = (l, o_idx)
            present[key] = model.NewBoolVar(f"present_l{l}_o{o['order_id']}")
            ds_raw, de = int(o["due_start"]), int(o["due_end"])
            # Week-1 orders can fill end of Week-0 (up to Sat 23:00) if allow_week1_in_week0
            if ds_raw > WEEK0_END and P.allow_week1_in_week0:
                ds_eff = WEEK0_FILL_START  # place Week-1 at end of Week-0, fill to Saturday 23:00
            else:
                ds_eff = ds_raw
            max_len = max(0, min(H, de + 1) - max(0, ds_eff))
            run_h[key] = model.NewIntVar(0, max(H, max_len), f"runh_l{l}_o{o['order_id']}")
            start[key] = model.NewIntVar(0, H, f"start_l{l}_o{o['order_id']}")
            end[key] = model.NewIntVar(0, H, f"end_l{l}_o{o['order_id']}")
            intervals[key] = model.NewOptionalIntervalVar(
                start[key], run_h[key], end[key], present[key], f"int_l{l}_o{o['order_id']}"
            )
            model.Add(start[key] >= ds_eff).OnlyEnforceIf(present[key])
            model.Add(end[key] <= de + 1).OnlyEnforceIf(present[key])
            r = data.rate.get((l, o["sku"]))
            cap = data.capable.get((l, o["sku"]))
            if (cap is None) or (cap == 0) or (r is None) or (r <= 0):
                model.Add(present[key] == 0)
                model.Add(run_h[key] == 0)
            else:
                qmin = int(o["qty_min"])
                min_run_from_pct = math.ceil(P.min_run_pct_of_qty * qmin / r) if r > 0 else 0
                min_run = min(max_len, max(1, P.min_run_hours, min_run_from_pct))
                model.Add(run_h[key] >= min_run).OnlyEnforceIf(present[key])
                model.Add(run_h[key] == 0).OnlyEnforceIf(present[key].Not())

    line_intervals = {l: [] for l in lines}
    for l in lines:
        for o_idx in range(len(orders)):
            line_intervals[l].append(intervals[(l, o_idx)])
    for dt in data.downtimes:
        l = dt["line_id"]
        if l in line_intervals:
            s = max(0, dt["start"])
            e = min(H, dt["end"])
            d = max(0, e - s)
            if d > 0:
                s_var = model.NewIntVar(s, s, f"dt_s_l{l}_{s}_{e}")
                e_var = model.NewIntVar(e, e, f"dt_e_l{l}_{s}_{e}")
                line_intervals[l].append(model.NewIntervalVar(s_var, d, e_var, f"DT_l{l}_{s}_{e}"))
    for l in lines:
        model.AddNoOverlap(line_intervals[l])

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
        model.Add(sum(present[(l, o_idx)] for l in lines) <= mlpo)

    if (phase in ("sanity3", "full")) and (not ignore_co):
        for l in lines:
            elig = [
                o_idx
                for o_idx, o in enumerate(orders)
                if data.capable.get((l, o["sku"])) and (data.rate.get((l, o["sku"])) or 0) > 0
            ]
            any_present = model.NewBoolVar(f"any_present_l{l}")
            model.Add(sum(present[(l, i)] for i in elig) >= 1).OnlyEnforceIf(any_present)
            model.Add(sum(present[(l, i)] for i in elig) == 0).OnlyEnforceIf(any_present.Not())
            first_flags = []
            init_sku = str(data.init_map.get(l, {}).get("initial_sku", "CLEAN"))
            long_flag = int(data.init_map.get(l, {}).get("long_shutdown_flag", 0))
            long_extra = int(data.init_map.get(l, {}).get("long_shutdown_extra", 4))
            avail = int(data.init_map.get(l, {}).get("available_from", 0))
            for i_idx in elig:
                i = orders[i_idx]
                first_i = model.NewBoolVar(f"first_l{l}_o{i['order_id']}")
                first_flags.append(first_i)
                model.AddImplication(first_i, present[(l, i_idx)])
                for j_idx in elig:
                    if j_idx == i_idx:
                        continue
                    model.Add(start[(l, i_idx)] <= start[(l, j_idx)]).OnlyEnforceIf(first_i)
                base = data.setup.get((init_sku, i["sku"]), 0) if init_sku != "CLEAN" else 0
                eff = base + (long_extra if long_flag == 1 else 0)
                if eff > 0 or avail > 0:
                    model.Add(start[(l, i_idx)] >= avail + eff).OnlyEnforceIf([first_i, present[(l, i_idx)]])
            if first_flags:
                model.Add(sum(first_flags) == 1).OnlyEnforceIf(any_present)
                model.Add(sum(first_flags) == 0).OnlyEnforceIf(any_present.Not())
            for a in range(len(elig)):
                for b in range(a + 1, len(elig)):
                    i_idx, j_idx = elig[a], elig[b]
                    i, j = orders[i_idx], orders[j_idx]
                    b_ij = model.NewBoolVar(f"b_l{l}_{i['order_id']}__{j['order_id']}")
                    model.AddImplication(b_ij, present[(l, i_idx)])
                    model.AddImplication(b_ij, present[(l, j_idx)])
                    setup_ij = data.setup.get((i["sku"], j["sku"]), 0)
                    setup_ji = data.setup.get((j["sku"], i["sku"]), 0)
                    model.Add(start[(l, j_idx)] >= end[(l, i_idx)] + setup_ij).OnlyEnforceIf(b_ij)
                    model.Add(start[(l, i_idx)] >= end[(l, j_idx)] + setup_ji).OnlyEnforceIf(b_ij.Not())

    # Max 1h non-production (CIP/changeover only) between last Week-0 and first Week-1 on each line
    if P.allow_week1_in_week0:
        week0_order_idxs = [o_idx for o_idx, o in enumerate(orders) if int(o["due_end"]) <= WEEK0_END]
        week1_order_idxs = [o_idx for o_idx, o in enumerate(orders) if int(o["due_start"]) >= WEEK1_START]
        if week0_order_idxs and week1_order_idxs:
            for l in lines:
                # last_w0_end[l] = max of end[(l,o)] over Week-0 orders that are present (else 0)
                end_or_zero = {}
                for o_idx in week0_order_idxs:
                    key = (l, o_idx)
                    end_or_zero[key] = model.NewIntVar(0, H, f"end_or_zero_l{l}_o{o_idx}")
                    model.Add(end_or_zero[key] == end[key]).OnlyEnforceIf(present[key])
                    model.Add(end_or_zero[key] == 0).OnlyEnforceIf(present[key].Not())
                last_w0_end = model.NewIntVar(0, H, f"last_w0_end_l{l}")
                model.AddMaxEquality(last_w0_end, [end_or_zero[(l, o_idx)] for o_idx in week0_order_idxs])

                # first_w1_start[l] = min of start[(l,o)] over Week-1 orders that are present (else H)
                start_or_H = {}
                for o_idx in week1_order_idxs:
                    key = (l, o_idx)
                    start_or_H[key] = model.NewIntVar(0, H, f"start_or_H_l{l}_o{o_idx}")
                    model.Add(start_or_H[key] == start[key]).OnlyEnforceIf(present[key])
                    model.Add(start_or_H[key] == H).OnlyEnforceIf(present[key].Not())
                first_w1_start = model.NewIntVar(0, H, f"first_w1_start_l{l}")
                model.AddMinEquality(first_w1_start, [start_or_H[(l, o_idx)] for o_idx in week1_order_idxs])

                has_w0 = model.NewBoolVar(f"has_w0_l{l}")
                model.Add(sum(present[(l, o_idx)] for o_idx in week0_order_idxs) >= 1).OnlyEnforceIf(has_w0)
                model.Add(sum(present[(l, o_idx)] for o_idx in week0_order_idxs) == 0).OnlyEnforceIf(has_w0.Not())
                has_w1 = model.NewBoolVar(f"has_w1_l{l}")
                model.Add(sum(present[(l, o_idx)] for o_idx in week1_order_idxs) >= 1).OnlyEnforceIf(has_w1)
                model.Add(sum(present[(l, o_idx)] for o_idx in week1_order_idxs) == 0).OnlyEnforceIf(has_w1.Not())
                has_both = model.NewBoolVar(f"has_both_w0_w1_l{l}")
                model.Add(has_both == 1).OnlyEnforceIf([has_w0, has_w1])
                model.Add(has_both == 0).OnlyEnforceIf(has_w0.Not())
                model.Add(has_both == 0).OnlyEnforceIf(has_w1.Not())
                model.Add(first_w1_start - last_w0_end <= MAX_GAP_W0_W1_HOURS).OnlyEnforceIf(has_both)

    if phase == "full":
        for l in lines:
            total_run = model.NewIntVar(0, P.horizon_h, f"total_run_l{l}")
            model.Add(total_run == sum(run_h[(l, o_idx)] for o_idx in range(len(orders))))
            cip_cnt = model.NewIntVar(0, 3, f"cip_count_l{l}")
            carry = int(data.init_map.get(l, {}).get("carryover_run_hours", 0))
            b1 = model.NewBoolVar(f"cip1_needed_l{l}")
            b2 = model.NewBoolVar(f"cip2_needed_l{l}")
            model.Add(total_run + carry >= 120).OnlyEnforceIf(b1)
            model.Add(total_run + carry <= 119).OnlyEnforceIf(b1.Not())
            model.Add(total_run + carry >= 240).OnlyEnforceIf(b2)
            model.Add(total_run + carry <= 239).OnlyEnforceIf(b2.Not())
            model.Add(cip_cnt >= b1 + b2)
            avail_h = available_hours_line(P, data, l)
            model.Add(total_run + cip_cnt * P.cip_duration_h <= avail_h)

    # Changeover count per line: (num jobs on line) - 1 when line has at least one job
    changeovers_per_line = []
    for l in lines:
        total_jobs_l = model.NewIntVar(0, len(orders), f"total_jobs_l{l}")
        model.Add(total_jobs_l == sum(present[(l, o_idx)] for o_idx in range(len(orders))))
        any_present_l = model.NewBoolVar(f"any_present_obj_l{l}")
        model.Add(total_jobs_l >= 1).OnlyEnforceIf(any_present_l)
        model.Add(total_jobs_l == 0).OnlyEnforceIf(any_present_l.Not())
        changeovers_l = model.NewIntVar(0, len(orders), f"changeovers_l{l}")
        model.Add(changeovers_l == total_jobs_l - 1).OnlyEnforceIf(any_present_l)
        model.Add(changeovers_l == 0).OnlyEnforceIf(any_present_l.Not())
        changeovers_per_line.append(changeovers_l)

    all_end = [end[(l, o_idx)] for l in lines for o_idx in range(len(orders))]
    makespan = model.NewIntVar(0, P.horizon_h, "makespan")
    if all_end:
        model.AddMaxEquality(makespan, all_end)
    else:
        model.Add(makespan == 0)

    # Objective: minimize makespan * W1 + changeovers * W2, or maximize total production (e.g. Week-1 phase)
    if maximize_production:
        model.Maximize(sum(produced[o_idx] for o_idx in range(len(orders))))
    else:
        W1 = P.objective_makespan_weight
        W2 = P.objective_changeover_weight
        obj = model.NewIntVar(0, 10**12, "obj")
        model.Add(obj == makespan * W1 + sum(changeovers_per_line) * W2)
        model.Minimize(obj)
    vars_dict = {
        "present": present,
        "run_h": run_h,
        "start": start,
        "end": end,
        "produced": produced,
    }
    return model, vars_dict
