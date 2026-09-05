# tests/test_solver_properties.py -- property tests for the CP-SAT model on a
# family of generated tiny problems (fix agent T, 2026-09-03; audit tests-11).
#
# Until now only three modules touched cp_model and the tiny solves asserted
# three hard rules; no test could see a semantic reversal of an objective knob
# or a comparative property (audit tests-11). This module states the charter's
# section-5 physics as PROPERTIES and checks them on 4 generated variants
# (fixed RNG seed, so the family is deterministic) plus a few hand-built pairs
# for the comparative properties:
#
#   P-A  production never negative; produced == sum over lines of
#        round(rate) x run hours (kg == rate x hours per block)
#   P-B  no overlap per line: production segments, CIP windows, downtimes
#   P-C  hard demand: qty_min <= produced <= qty_max at relax level 0
#   P-D  CIP interval never exceeded on the WALL CLOCK (from -carry at t0)
#   P-E  changeover gap >= setup hours between different SKUs (and after the
#        initial SKU), setup already rounded UP to whole hours by the loader
#   P-F  increasing demand never decreases total runtime
#   P-G  adding a fully-downed line never improves feasibility or fill
#   P-H  relaxing (ladder level up) never worsens the optimum objective
#   P-I  the independent validator agrees (no physical ERROR on any variant)
#
# EVERY expectation is computed by code that does not import model_builder:
# the checks read solver VALUES (present / seg_* / produced / cip_vars) and the
# input dicts, and recompute the physics with plain arithmetic. model_builder
# is imported only to BUILD the model under test.
#
# CPU discipline: every solve is <= 2 lines, <= 4 orders, 2 workers, <= 10 s
# (they finish in well under a second).

from __future__ import annotations

import random
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ortools.sat.python import cp_model  # noqa: E402

from data_loader import Data, Files, Params  # noqa: E402
from model_builder import build_model  # noqa: E402

WORKERS = 2
TIME_S = 10.0
SEED = 20260903
H = 168
SKUS = ["111", "222", "333"]


# ---------------------------------------------------------------------------
# tiny in-memory problems
# ---------------------------------------------------------------------------
def _params(**kw) -> Params:
    P = Params(
        horizon_h=H, min_run_hours=1, min_run_pct_of_qty=0.0,
        max_lines_per_order=2, allow_week1_in_week0=False,
        cip_interval_h=120, cip_duration_h=6,
        objective_makespan_weight=6, objective_changeover_weight=300,
        objective_cip_defer_weight=5, objective_idle_weight=3,
        objective_late_weight=200, objective_week_deviation_weight=40,
        co_topload_weight=450, co_ttp_weight=5, co_ffs_weight=600,
        co_casepacker_weight=20, co_base_weight=5, co_conv_org_weight=30,
        co_cinn_weight=20, co_flavor_weight=5, co_cip_req_weight=150,
        solver_random_seed=7,
    )
    for k, v in kw.items():
        setattr(P, k, v)
    return P


def _empty_data(P: Params) -> Data:
    d = Data(P, Files(Path("/nonexistent")))
    d.lines, d.line_names, d.init_map = [], {}, {}
    d.capable, d.rate, d.downtimes, d.cip_interval_map = {}, {}, [], {}
    d.setup, d.machine_changes, d.orders = {}, {}, []
    return d


def _add_line(d: Data, lid: int, rate: float, *, init_sku="CLEAN", avail=0,
              carry=0, interval=120, capable=None) -> None:
    d.lines.append(lid)
    d.line_names[lid] = f"L{lid + 1}"
    d.init_map[lid] = {"available_from": int(avail), "initial_sku": init_sku,
                       "carryover_run_hours": int(carry),
                       "long_shutdown_flag": 0, "long_shutdown_extra": 0,
                       "last_cip_end_datetime": None, "comment": ""}
    for sku in SKUS:
        ok = capable is None or sku in capable
        d.capable[(lid, sku)] = 1 if ok else 0
        d.rate[(lid, sku)] = float(rate) if ok else 0.0
    d.cip_interval_map[lid] = int(interval)


def _add_pair(d: Data, a: str, b: str, setup_h: int, *, cip_req=0) -> None:
    """Whole-hour setup (the loader rounds UP before the model sees it, SA-4);
    a cip_req_after pair is floored at the CIP duration by the loader too."""
    setup_h = max(setup_h, d.P.cip_duration_h) if cip_req else setup_h
    d.setup[(a, b)] = int(setup_h)
    d.machine_changes[(a, b)] = {"ttp": 1, "ffs": 1 if setup_h >= 4 else 0,
                                 "topload": 1 if setup_h >= 4 else 0,
                                 "casepacker": 1 if setup_h >= 2 else 0,
                                 "conv_to_org": 0, "cinn_to_non": 0,
                                 "added_flavors": 0, "cip_req_after": int(cip_req)}


def _add_order(d: Data, oid: str, sku: str, qty: int, *, lower=0.9, upper=1.1,
               due_start=0, due_end=H - 1, exact=False) -> None:
    import math
    qmin = qty if exact else int(math.floor(qty * lower))
    qmax = qty if exact else int(math.ceil(qty * upper))
    d.orders.append(dict(order_id=oid, sku=sku, due_start=due_start, due_end=due_end,
                         qty_min=qmin, qty_max=qmax, qty_target=qty, priority=3))


def make_variant(idx: int) -> tuple[Params, Data, dict]:
    """Deterministic variant idx of the family (fixed seed). Returns the
    problem plus a plain-dict 'spec' the checks read (never the model)."""
    rng = random.Random(SEED + idx)
    P = _params()
    d = _empty_data(P)
    n_lines = 1 + (idx % 2)
    rates = [rng.choice([10.0, 20.0, 25.0]) for _ in range(n_lines)]
    for lid in range(n_lines):
        init = rng.choice(["CLEAN", "111", "222"])
        _add_line(d, lid, rates[lid], init_sku=init,
                  avail=rng.choice([0, 0, 5, 12]), carry=rng.choice([0, 30, 60]),
                  interval=rng.choice([100, 120]))
    # full directional matrix, one cip_req pair (222 -> 333)
    for a in SKUS:
        for b in SKUS:
            if a != b:
                _add_pair(d, a, b, rng.choice([1, 2, 3, 4]),
                          cip_req=1 if (a, b) == ("222", "333") else 0)
    n_orders = rng.choice([2, 3, 4])
    for k in range(n_orders):
        sku = SKUS[k % 3]
        hours = rng.choice([6, 8, 10, 12])
        qty = int(hours * min(rates))          # fits any line
        _add_order(d, f"O{k + 1}", sku, qty)
    # one maintenance window on a random line
    dl = rng.randrange(n_lines)
    ds = rng.choice([20, 40, 70])
    d.downtimes.append({"line_id": dl, "start": ds, "end": ds + rng.choice([6, 12]),
                        "reason": "Maintenance"})
    spec = {
        "lines": {l: dict(rate=d.rate[(l, SKUS[0])], init=d.init_map[l]["initial_sku"],
                          avail=d.init_map[l]["available_from"],
                          carry=d.init_map[l]["carryover_run_hours"],
                          interval=d.cip_interval_map[l]) for l in d.lines},
        "downtimes": list(d.downtimes),
        "setup": dict(d.setup),
        "orders": {o["order_id"]: dict(o) for o in d.orders},
    }
    return P, d, spec


def solve(P: Params, d: Data, *, relax_demand=False, relax_due=False, ignore_co=False,
          maximize_production=True):
    model, v = build_model(P, d, "full", relax_demand, ignore_co,
                           max_lines_per_order_override=P.max_lines_per_order,
                           maximize_production=maximize_production,
                           objective_mode="balanced", relax_due=relax_due)
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = WORKERS
    s.parameters.max_time_in_seconds = TIME_S
    s.parameters.random_seed = int(P.solver_random_seed)
    st = s.Solve(model)
    # CP-SAT encodes Maximize as negated coefficients + scaling_factor -1;
    # the maximize_production branch MAXIMIZES (fill reward minus costs).
    # Normalise to a COST (lower is better) so the comparative properties
    # read the same way whichever sense the branch uses.
    v["_cost_sign"] = -1.0 if model.Proto().objective.scaling_factor < 0 else 1.0
    return s.StatusName(st), s, v


def extract(s: cp_model.CpSolver, v: dict, d: Data) -> dict:
    """Solver VALUES only -- the independent checks work on this dict.
    `cost` = the objective in lower-is-better form (see solve)."""
    segs = []   # (line, order_id, sku, start, end, run)
    for (l, o_idx), pres in v["present"].items():
        if not s.BooleanValue(pres):
            continue
        o = d.orders[o_idx]
        segs.append((l, o["order_id"], o["sku"], s.Value(v["seg_a_start"][(l, o_idx)]),
                     s.Value(v["seg_a_end"][(l, o_idx)]), s.Value(v["seg_a_run"][(l, o_idx)])))
        if s.BooleanValue(v["seg_b_present"][(l, o_idx)]):
            segs.append((l, o["order_id"], o["sku"], s.Value(v["seg_b_start"][(l, o_idx)]),
                         s.Value(v["seg_b_end"][(l, o_idx)]), s.Value(v["seg_b_run"][(l, o_idx)])))
    cips = []   # (line, start, end)
    for l, slots in (v.get("cip_vars") or {}).items():
        for (cs, ce, cp) in slots:
            if s.BooleanValue(cp):
                cips.append((l, s.Value(cs), s.Value(ce)))
    produced = {d.orders[i]["order_id"]: s.Value(var) for i, var in v["produced"].items()}
    return {"segs": segs, "cips": cips, "produced": produced,
            "objective": s.ObjectiveValue(),
            "cost": v.get("_cost_sign", 1.0) * s.ObjectiveValue()}


@pytest.fixture(scope="module", params=[0, 1, 2, 3])
def variant(request):
    P, d, spec = make_variant(request.param)
    status, s, v = solve(P, d)
    assert status in ("OPTIMAL", "FEASIBLE"), f"variant {request.param}: {status}"
    return {"idx": request.param, "P": P, "d": d, "spec": spec,
            "sol": extract(s, v, d), "status": status}


# ---------------------------------------------------------------------------
# P-A  produced == sum round(rate) x run hours; nothing negative
# ---------------------------------------------------------------------------
def test_pa_production_is_rate_times_hours_and_never_negative(variant):
    sol, spec = variant["sol"], variant["spec"]
    by_order: dict[str, int] = {}
    for (l, oid, sku, st, en, run) in sol["segs"]:
        assert run >= 1 and st >= 0 and en > st, (l, oid, st, en, run)
        assert en - st == run, f"{oid} on L{l+1}: block {st}-{en} is not run {run} h long"
        by_order[oid] = by_order.get(oid, 0) + int(round(spec["lines"][l]["rate"])) * run
    for oid, kg in sol["produced"].items():
        assert kg >= 0
        assert kg == by_order.get(oid, 0), (
            f"{oid}: produced {kg} != sum(round(rate) x run_h) {by_order.get(oid, 0)}")


# ---------------------------------------------------------------------------
# P-B  no overlap per line (production + CIP + downtime)
# ---------------------------------------------------------------------------
def test_pb_no_overlap_per_line(variant):
    sol, spec = variant["sol"], variant["spec"]
    for l in spec["lines"]:
        ivs = [(st, en, f"prod {oid}") for (ll, oid, _s, st, en, _r) in sol["segs"] if ll == l]
        ivs += [(st, en, "cip") for (ll, st, en) in sol["cips"] if ll == l]
        ivs += [(dt["start"], dt["end"], "downtime") for dt in spec["downtimes"] if dt["line_id"] == l]
        ivs.sort()
        for (a, b) in zip(ivs, ivs[1:]):
            assert b[0] >= a[1], f"L{l+1}: {a} overlaps {b}"


# ---------------------------------------------------------------------------
# P-C  hard demand honoured at relax level 0
# ---------------------------------------------------------------------------
def test_pc_hard_demand_within_bounds(variant):
    sol, spec = variant["sol"], variant["spec"]
    for oid, o in spec["orders"].items():
        kg = sol["produced"][oid]
        assert o["qty_min"] <= kg <= o["qty_max"], (oid, o["qty_min"], kg, o["qty_max"])


# ---------------------------------------------------------------------------
# P-D  CIP interval never exceeded on the wall clock
# ---------------------------------------------------------------------------
def test_pd_cip_interval_never_exceeded_wall_clock(variant):
    """The clean clock runs on the WALL: the last clean before t0 ended at
    hour -carry, so no production may END later than (previous clean end +
    interval) and no two cleans may be more than one interval apart. Food
    safety -- never relaxed at any level (model_builder's own comment)."""
    sol, spec = variant["sol"], variant["spec"]
    for l, ln in spec["lines"].items():
        cleans = sorted((st, en) for (ll, st, en) in sol["cips"] if ll == l)
        prod = sorted((st, en) for (ll, _o, _s, st, en, _r) in sol["segs"] if ll == l)
        if not prod:
            continue
        prev_end = -ln["carry"]
        for (cs, ce) in cleans:
            assert cs - prev_end <= ln["interval"], (
                f"L{l+1}: clean at {cs} is {cs - prev_end} h after the previous clean end "
                f"{prev_end} (interval {ln['interval']})")
            prev_end = ce
        # every production end vs the clean that precedes it
        for (st, en) in prod:
            before = [ce for (cs, ce) in cleans if ce <= st]
            ref = max(before) if before else -ln["carry"]
            assert en - ref <= ln["interval"], (
                f"L{l+1}: production ends {en}, {en - ref} h after the last clean end {ref} "
                f"(interval {ln['interval']})")


# ---------------------------------------------------------------------------
# P-E  changeover gap >= setup between different SKUs
# ---------------------------------------------------------------------------
def test_pe_changeover_gap_covers_the_setup_hours(variant):
    sol, spec = variant["sol"], variant["spec"]
    for l, ln in spec["lines"].items():
        segs = sorted(((st, en, sku, oid) for (ll, oid, sku, st, en, _r) in sol["segs"] if ll == l))
        if not segs:
            continue
        # initial SKU -> first block
        st0, _e0, sku0, oid0 = segs[0]
        if ln["init"] not in ("CLEAN", sku0):
            need = spec["setup"].get((ln["init"], sku0), 0)
            assert st0 - ln["avail"] >= need, (
                f"L{l+1}: first block {oid0} ({sku0}) starts {st0}, gate {ln['avail']}, "
                f"setup from initial {ln['init']} is {need} h")
        for (a, b) in zip(segs, segs[1:]):
            if a[2] == b[2]:
                continue
            need = spec["setup"].get((a[2], b[2]), 0)
            assert b[0] - a[1] >= need, (
                f"L{l+1}: {a[3]} ({a[2]}) ends {a[1]}, {b[3]} ({b[2]}) starts {b[0]}: "
                f"gap {b[0] - a[1]} < setup {need}")


# ---------------------------------------------------------------------------
# P-I  the independent validator agrees
# ---------------------------------------------------------------------------
def test_pi_independent_validator_finds_no_physical_error(variant, tmp_path):
    """Write the variant as a work dir and run code/solver/independent_validator
    (pandas-only, no project imports): no ERROR of a physical code."""
    from independent_validator import validate_work_dir

    P, d, sol = variant["P"], variant["d"], variant["sol"]
    work = tmp_path / f"v{variant['idx']}"
    work.mkdir()
    pd.DataFrame([{"line_id": l, "sku": s, "line_name": d.line_names[l],
                   "capable": d.capable[(l, s)], "calc_rate_kgph": d.rate[(l, s)]}
                  for l in d.lines for s in SKUS]).to_csv(work / "capabilities_rates.csv", index=False)
    pd.DataFrame([{"from_sku": a, "to_sku": b, "setup_hours": h,
                   "cip_req_after": d.machine_changes[(a, b)]["cip_req_after"]}
                  for (a, b), h in d.setup.items()]).to_csv(work / "changeovers.csv", index=False)
    pd.DataFrame([{"line_id": l, "line_name": d.line_names[l],
                   "initial_sku": d.init_map[l]["initial_sku"],
                   "available_from_hour": d.init_map[l]["available_from"],
                   "carryover_run_hours_since_last_cip_at_t0": d.init_map[l]["carryover_run_hours"]}
                  for l in d.lines]).to_csv(work / "initial_states.csv", index=False)
    pd.DataFrame([{"line_id": dt["line_id"], "line_name": d.line_names[dt["line_id"]],
                   "start_hour": dt["start"], "end_hour": dt["end"], "reason": dt["reason"]}
                  for dt in d.downtimes]).to_csv(work / "downtimes.csv", index=False)
    pd.DataFrame([{"line_id": l, "line_name": d.line_names[l], "max_cip_hrs": d.cip_interval_map[l]}
                  for l in d.lines]).to_csv(work / "line_cip_hrs.csv", index=False)
    pd.DataFrame([{"order_id": o["order_id"], "sku": o["sku"], "qty_target": o["qty_target"],
                   "qty_min": o["qty_min"], "qty_max": o["qty_max"],
                   "due_start_hour": o["due_start"], "due_end_hour": o["due_end"], "priority": 3}
                  for o in d.orders]).to_csv(work / "demand_plan.csv", index=False)
    (work / "flowstate.toml").write_text(
        f'[scheduler]\nhorizon_hours = {P.horizon_h}\nmin_run_hours = {P.min_run_hours}\n'
        f'max_lines_per_order = {P.max_lines_per_order}\nuse_sku_rates = true\n'
        f'allow_week1_in_week0 = false\n[cip]\ninterval_h = {P.cip_interval_h}\n'
        f'duration_h = {P.cip_duration_h}\n', encoding="utf-8")
    rows = [{"line_id": l, "line_name": d.line_names[l], "order_id": oid, "sku": sku,
             "start_hour": st, "end_hour": en, "run_hours": run,
             "qty_kg": int(round(d.rate[(l, sku)])) * run, "is_trial": False}
            for (l, oid, sku, st, en, run) in sol["segs"]]
    cips = [{"line_id": l, "line_name": d.line_names[l], "start_hour": st, "end_hour": en}
            for (l, st, en) in sol["cips"]]
    rep = validate_work_dir(work, schedule=pd.DataFrame(rows),
                            cips=pd.DataFrame(cips, columns=["line_id", "line_name", "start_hour", "end_hour"]))
    physical = [v for v in rep.errors()
                if v.code in ("OVERLAP", "IN_DOWNTIME", "CIP_INTERVAL", "CHANGEOVER_GAP",
                              "BEFORE_GATE", "INITIAL_SETUP", "LINE_NOT_CAPABLE",
                              "QTY_RATE_MISMATCH", "HORIZON")]
    assert not physical, "\n".join(v.as_line() for v in physical)


# ---------------------------------------------------------------------------
# P-F  increasing demand never decreases total runtime
# ---------------------------------------------------------------------------
def _mono_problem(extra_kg: int) -> tuple[Params, Data]:
    """2 identical lines @ 10 kg/h, exact quantities so runtime is pinned:
    O1 111 100 kg (10 h), O2 222 80 kg (8 h), O3 111 60 + extra kg."""
    P = _params()
    d = _empty_data(P)
    _add_line(d, 0, 10.0, init_sku="111")
    _add_line(d, 1, 10.0, init_sku="222")
    for a in SKUS:
        for b in SKUS:
            if a != b:
                _add_pair(d, a, b, 2)
    _add_order(d, "O1", "111", 100, exact=True)
    _add_order(d, "O2", "222", 80, exact=True)
    _add_order(d, "O3", "111", 60 + extra_kg, exact=True)
    return P, d


@pytest.mark.parametrize("extra_kg", [20, 50])
def test_pf_more_demand_never_less_runtime(extra_kg):
    """Hand: base total = (100 + 80 + 60) / 10 = 24 h; +20 kg -> 26 h;
    +50 kg -> 29 h. Equal rates make total runtime independent of the line
    assignment, so the delta is exact, not just >= 0."""
    P0, d0 = _mono_problem(0)
    st0, s0, v0 = solve(P0, d0)
    P1, d1 = _mono_problem(extra_kg)
    st1, s1, v1 = solve(P1, d1)
    assert st0 == "OPTIMAL" and st1 == "OPTIMAL", (st0, st1)
    run0 = sum(r for (*_x, r) in extract(s0, v0, d0)["segs"])
    run1 = sum(r for (*_x, r) in extract(s1, v1, d1)["segs"])
    assert run0 == 24
    assert run1 == 24 + extra_kg // 10
    assert run1 >= run0


# ---------------------------------------------------------------------------
# P-G  adding a fully-downed line never improves feasibility or fill
# ---------------------------------------------------------------------------
def _with_downed_line(P: Params, d: Data) -> Data:
    lid = max(d.lines) + 1
    _add_line(d, lid, 25.0, init_sku="CLEAN")
    d.downtimes.append({"line_id": lid, "start": 0, "end": P.horizon_h, "reason": "Down"})
    return d


@pytest.mark.parametrize("idx", [0, 1, 2, 3])
def test_pg_downed_line_never_improves_the_solve(idx):
    P, d, _spec = make_variant(idx)
    st0, s0, v0 = solve(P, d)
    base = extract(s0, v0, d)
    P2, d2, _ = make_variant(idx)
    d2 = _with_downed_line(P2, d2)
    st1, s1, v1 = solve(P2, d2)
    assert st0 == st1 == "OPTIMAL", (st0, st1)
    new = extract(s1, v1, d2)
    downed = max(d2.lines)
    assert not [sg for sg in new["segs"] if sg[0] == downed], "production on a fully-downed line"
    assert not [c for c in new["cips"] if c[0] == downed], "a clean on a fully-downed line"
    assert sum(new["produced"].values()) == sum(base["produced"].values())
    assert new["cost"] >= base["cost"] - 1e-6, (
        f"variant {idx}: objective improved from cost {base['cost']} to {new['cost']} "
        "by adding a line that cannot run")


def test_pg_downed_line_does_not_rescue_an_infeasible_problem():
    """One line @ 10 kg/h, one exact 1,800 kg order = 180 h > 168 h horizon:
    INFEASIBLE, and a second line that is down for the whole horizon must
    leave it INFEASIBLE."""
    P = _params()
    d = _empty_data(P)
    _add_line(d, 0, 10.0)
    _add_pair(d, "111", "222", 1)
    _add_pair(d, "222", "111", 1)
    _add_order(d, "O1", "111", 1800, exact=True)
    st0, _s, _v = solve(P, d)
    assert st0 == "INFEASIBLE", st0
    d = _with_downed_line(P, d)
    st1, _s, _v = solve(P, d)
    assert st1 == "INFEASIBLE", st1


# ---------------------------------------------------------------------------
# P-H  relaxing never worsens the optimum
# ---------------------------------------------------------------------------
LADDER = [
    dict(relax_demand=False, relax_due=False, ignore_co=False),
    dict(relax_demand=True, relax_due=False, ignore_co=False),
    dict(relax_demand=True, relax_due=True, ignore_co=False),
    dict(relax_demand=True, relax_due=True, ignore_co=True),
]


@pytest.mark.parametrize("idx", [0, 1])
def test_ph_relaxing_a_level_never_worsens_the_optimum(idx):
    """Each ladder level only REMOVES constraints or cost terms (qty_min
    floors, the hard due cap -> priced lateness, changeover cost), so the
    level-k optimum is feasible at level k+1 at no higher cost. Setup TIME
    stays at level 3 (SA-6 / C63) -- a physics rule, not a preference -- so
    the level-3 schedule is still checked for changeover gaps.

    Hand check on variant 0 (one line @ 20 kg/h, 4 orders): levels 0-2 all
    reach cost 680,000 fill - 18,000 weighted CO - 234 makespan - 15 idle =
    -661,751; level 3 prices the same 3 adjacencies flat (3 x 300 = 900)
    instead of 60 x 300, so its cost is -678,851 -- lower, as it must be."""
    costs = []
    last = None
    for lvl, flags in enumerate(LADDER):
        P, d, spec = make_variant(idx)
        st, s, v = solve(P, d, **flags)
        assert st == "OPTIMAL", (idx, lvl, st)
        sol = extract(s, v, d)
        costs.append(sol["cost"])
        last = (sol, spec)
    for k in range(len(costs) - 1):
        assert costs[k + 1] <= costs[k] + 1e-6, (
            f"variant {idx}: level {k + 1} optimum cost {costs[k + 1]} worse than level {k} {costs[k]}")
    # level 3 keeps setup TIME between different SKUs
    sol, spec = last
    for l in spec["lines"]:
        segs = sorted(((st, en, sku) for (ll, _o, sku, st, en, _r) in sol["segs"] if ll == l))
        for a, b in zip(segs, segs[1:]):
            if a[2] != b[2]:
                assert b[0] - a[1] >= spec["setup"][(a[2], b[2])], (l, a, b)
