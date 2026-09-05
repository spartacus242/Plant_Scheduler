# tests/test_fix_T.py -- regression tests for fix group T (test-suite
# hardening, 2026-09-03). Audit findings: tests-1, tests-2, tests-6, tests-7,
# tests-9, tests-11 (+ tests-4/5 background). Every expected value is derived
# by hand in the comment next to the assertion.
#
#   T-1  tests-1   the solver_tiny fixture solves at level 0 and the artifact
#                  has the production shape (+ independent validator clean)
#   T-2  tests-2   _solution_to_rows / write_mo_changes semantics on a real
#                  (tiny) solution
#   T-3  tests-7   the orchestrator's own findings pinned independently of
#                  the fix agents' tests: C01 stale re-forecast, C61
#                  exact-quantity trap, C84 two-phase Params drop
#   T-4  tests-6   the live-data guard exists and is honest (self-test lives
#                  in tests/test_no_live_data_reads.py; here: the allowlist
#                  names only real files and the pinned fixtures are not live)
#   T-5  tests-9   node toolchain present; dist bundle not older than src;
#                  src string literals are in the bundle
#   T-6  tests-11  pure-Python properties: ISO week keys, netting invariant
#                  (gross = applied + net), re-forecast guards, split kg
#                  conservation (the Python side of the CIP split rule)

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conftest as cft  # noqa: E402

FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
FIXTURE = ROOT / "data" / "test_fixtures" / "solver_tiny"


# ===========================================================================
# T-1  the fixture solve (tests-1)
# ===========================================================================
def test_t1_fixture_solves_optimal_at_level_0_with_every_output(solver_tiny_solution):
    sol = solver_tiny_solution
    assert sol["status"] == "OPTIMAL", sol["status"]
    work = sol["work"]
    for name in ("schedule_phase2.csv", "cip_windows.csv", "produced_vs_bounds.csv",
                 "mo_changes.csv", "feasibility_report.json", "flowstate.toml",
                 "current_mo.csv", "demand_plan.csv", "downtimes.csv"):
        assert (work / name).exists(), name
    rep = json.loads((work / "feasibility_report.json").read_text(encoding="utf-8"))
    assert rep["relax_level"] == 0 and rep["relax_mode"] == "hard"
    assert rep["soft_demand"] is False and rep["orders_short_of_qmin"] == []
    assert rep["workers"] == 2 and rep["seed"] == 7


def test_t1_fixture_inputs_are_what_the_readme_says():
    """Guard against silent fixture drift: the hand derivations in this file
    and in the contract/probe tests depend on these numbers."""
    cmo = pd.read_csv(FIXTURE / "current_mo.csv")
    assert cmo.to_dict("records") == [{"mo": "MO1", "line_name": "L1", "sku": "A",
                                       "remaining_kg": 12500, "due_start_h": 0,
                                       "due_end_h": 47, "locked_line": 1, "source": "manprg"}]
    dem = pd.read_csv(FIXTURE / "demand_plan.csv").set_index("order_id")
    assert dem.loc["RUSH-C", "due_end_hour"] == 71 and dem.loc["RUSH-C", "qty_target"] == 8000
    assert set(dem.index) == {"A-W0", "B-W0", "C-W0", "RUSH-C", "A-W1", "B-W1", "D-W1"}
    co = pd.read_csv(FIXTURE / "changeovers.csv").set_index(["from_sku", "to_sku"])
    assert co.loc[("A", "C"), "setup_hours"] == 1.25          # -> 2 h in the model (SA-4)
    assert co.loc[("B", "C"), "cip_req_after"] == 1           # -> 6 h floor
    caps = pd.read_csv(FIXTURE / "capabilities_rates.csv")
    assert caps[(caps.line_name == "L3") & (caps.sku == "A")]["capable"].item() == 0
    dt = pd.read_csv(FIXTURE / "downtimes.csv")
    assert [(r.line_name, r.start_hour, r.end_hour) for r in dt.itertuples()] == \
        [("L2", 40, 60), ("L3", 100, 106)]


def test_t1_independent_validator_is_clean_on_the_fixture_solve(solver_tiny_work):
    """The pandas-only validator (24 checks) finds no ERROR on the artifact."""
    from independent_validator import validate_work_dir
    rep = validate_work_dir(solver_tiny_work)
    errs = [v.as_line() for v in rep.errors()]
    assert not errs, "\n".join(errs)
    assert rep.stats["orders_scheduled"] == 8          # 7 demand orders + MO1|CUR


def test_t1_c61_current_mo_bracket_in_the_fixture(solver_tiny_solution):
    """C61 (exact-quantity trap): MO1 has 12,500 kg remaining on L1 @ 1000
    kg/h. The model makes produced = 1000 x whole hours, so 12,500 is
    unreachable; the loader brackets it at floor/ceil hours:
    qty_min = 1000 x 12 = 12,000, qty_max = 1000 x 13 = 13,000, and the solve
    is FEASIBLE at level 0 with produced in {12,000, 13,000}."""
    data = solver_tiny_solution["data"]
    mo = [o for o in data.orders if o.get("is_current_mo")]
    assert len(mo) == 1
    assert (mo[0]["qty_min"], mo[0]["qty_max"], mo[0]["qty_remaining"]) == (12000, 13000, 12500)
    produced = {b["order_id"]: b["produced"] for b in solver_tiny_solution["bounds"]}
    assert produced["MO1|CUR"] in (12000, 13000)


# ===========================================================================
# T-2  _solution_to_rows / write_mo_changes (tests-2)
# ===========================================================================
def test_t2_rows_carry_rate_times_hours_and_sum_to_produced(solver_tiny_solution):
    """Per row qty_kg == round(rate) x run_hours (L1 1000, L2 800, L3 500) and
    the rows of one order sum to the solver's produced value; every row is a
    whole-hour block with end - start == run_hours (no CIP inside a row)."""
    sol = solver_tiny_solution
    rate = {"L1": 1000, "L2": 800, "L3": 500}
    by_order: dict[str, int] = {}
    for r in sol["rows"]:
        assert r["qty_kg"] == rate[r["line_name"]] * r["run_hours"], r
        assert r["end_hour"] - r["start_hour"] == r["run_hours"], r
        by_order[r["order_id"]] = by_order.get(r["order_id"], 0) + r["qty_kg"]
    for b in sol["bounds"]:
        assert by_order.get(b["order_id"], 0) == b["produced"], b
    # the two C orders on one line back to back need no gap (same SKU)
    assert {r["sku"] for r in sol["rows"]} == {"A", "B", "C", "D"}


def test_t2_mo_changes_semantics_on_the_fixture(solver_tiny_solution):
    """write_mo_changes on the real solution: one row per produced piece of
    MO1, orig_qty_kg = the raw remaining 12,500 (qty_remaining, SA-5), new =
    produced (12,000 or 13,000), delta = new - 12,500, wall-clock columns from
    the anchor 2026-09-07 00:00 (start_h 0 -> '2026-09-07 00:00:00'),
    scenario_id = the work-dir name, 19 columns in the documented order."""
    import phase2_scheduler as p2
    sol = solver_tiny_solution
    df = pd.read_csv(sol["work"] / "mo_changes.csv", dtype=str, keep_default_na=False)
    assert list(df.columns) == p2.MO_CHANGES_COLUMNS
    rows = df[df["mo"] == "MO1"]
    assert len(rows) >= 1
    pieces = [r for r in sol["rows"] if r["order_id"] == "MO1|CUR"]
    assert len(rows) == len(pieces)
    r0 = rows.iloc[0]
    assert r0["line_name"] == "L1" and r0["sku"] == "A"
    assert int(r0["orig_qty_kg"]) == 12500
    new_kg = int(r0["new_qty_kg"])
    assert new_kg in (12000, 13000) and int(r0["delta_kg"]) == new_kg - 12500
    assert r0["planning_anchor"] == "2026-09-07 00:00:00"
    assert r0["scenario_id"] == sol["work"].name
    assert r0["orig_start_src"] == "due_start"          # no manprg_start_h column yet
    anchor = datetime(2026, 9, 7)
    first = min(pieces, key=lambda r: r["start_hour"])
    assert r0["new_start_dt"] == (anchor + timedelta(hours=first["start_hour"])).strftime("%Y-%m-%d %H:%M:%S")
    assert r0["new_end_dt"] == (anchor + timedelta(hours=first["end_hour"])).strftime("%Y-%m-%d %H:%M:%S")
    assert int(r0["piece"]) == 1 and int(r0["split_count"]) == len(pieces)
    assert r0["reason"] != "dropped"


# ===========================================================================
# T-3  the orchestrator's own findings (tests-7)
# ===========================================================================
def test_t3_c01_reforecast_rate_is_measured_up_to_the_observation_time():
    """C01: the running-MO re-forecast divided 'Qty made' by (render clock -
    start) although the counter was read at the manprg observation time.
    Hand: start 2027-03-01 00:00, as_of 05:00, made 25 of 100 cases (left 75)
    -> actual 5 cas/h; end = as_of + 75/5 h = 20:00 -- whether the board is
    rendered at 05:00 or at 09:00. The legacy call (no as_of) at 09:00 reads
    25/9 = 2.78 cas/h and lands at 09:00 + 27 h = 03-02 12:00 (the defect)."""
    from helpers.current_state import _reforecast
    row = {"start_dt": pd.Timestamp("2027-03-01 00:00"), "fct_cas": 100.0,
           "made_cas": 25.0, "left_cas": 75.0, "completion_pct": 25.0, "qty_kg": 1000.0}
    as_of = datetime(2027, 3, 1, 5, 0)
    rf_a = _reforecast(row, datetime(2027, 3, 1, 5, 0), 10.0, as_of=as_of)
    rf_b = _reforecast(row, datetime(2027, 3, 1, 9, 0), 10.0, as_of=as_of)
    assert rf_a.actual_cph == rf_b.actual_cph == 5.0
    assert rf_a.end == rf_b.end == datetime(2027, 3, 1, 20, 0)
    legacy = _reforecast(row, datetime(2027, 3, 1, 9, 0), 10.0)
    assert abs(legacy.actual_cph - 25 / 9) < 1e-9
    assert legacy.end == datetime(2027, 3, 2, 12, 0)


def test_t3_c01_stale_content_is_refused_by_the_wrapper():
    """Guard: content older than REFORECAST_MAX_STALE_H (8 h) must not be
    extrapolated -- _reforecast_end returns (None, why) naming the age.
    as_of 05:00, now 14:00 -> 9.0 h old -> refused; now 12:00 -> 7 h -> used."""
    from helpers.current_state import _reforecast_end
    row = {"start_dt": pd.Timestamp("2027-03-01 00:00"), "fct_cas": 100.0,
           "made_cas": 25.0, "left_cas": 75.0, "completion_pct": 25.0, "qty_kg": 1000.0}
    as_of = datetime(2027, 3, 1, 5, 0)
    end, why = _reforecast_end(row, datetime(2027, 3, 1, 14, 0), 10.0, as_of=as_of)
    assert end is None and "9.0h old" in why
    end, why = _reforecast_end(row, datetime(2027, 3, 1, 12, 0), 10.0, as_of=as_of)
    assert end == datetime(2027, 3, 1, 20, 0)


def test_t3_c61_exact_quantity_trap_23000_at_737(tmp_path):
    """C61: 23,000 kg remaining @ 737 kg/h. produced = 737 x n; 23,000 / 737 =
    31.21 -> bracket 737 x 31 = 22,847 .. 737 x 32 = 23,584 (qty_remaining
    23,000). The old exact bounds made level 0 INFEASIBLE by arithmetic."""
    from data_loader import Data, Files, Params
    d = tmp_path
    (d / "capabilities_rates.csv").write_text(
        "line_id,sku,line_name,capable,calc_rate_kgph\n0,X,P09,1,737\n", encoding="utf-8")
    (d / "changeovers.csv").write_text("from_sku,to_sku,setup_hours\n", encoding="utf-8")
    (d / "initial_states.csv").write_text("line_id,line_name,initial_sku,available_from_hour\n0,P09,X,0\n",
                                          encoding="utf-8")
    (d / "demand_plan.csv").write_text(
        "order_id,sku,qty_target,lower_pct,upper_pct,due_start_hour,due_end_hour\n", encoding="utf-8")
    (d / "current_mo.csv").write_text(
        "mo,line_name,sku,remaining_kg,due_start_h,due_end_h,locked_line,source\n"
        "M9,P09,X,23000,0,167,1,manprg\n", encoding="utf-8")
    P = Params(horizon_h=168, use_sku_rates=True)
    data = Data(P, Files(d))
    data.load()
    mo = data.orders[0]
    assert (mo["qty_min"], mo["qty_max"], mo["qty_remaining"]) == (22847, 23584, 23000)
    assert mo["qty_min"] <= 23000 <= mo["qty_max"]
    assert mo["qty_min"] % 737 == 0 and mo["qty_max"] % 737 == 0


def test_t3_c84_two_phase_sub_params_keep_every_field():
    """C84: the two-phase driver rebuilt Params by hand and dropped
    use_sku_rates / soft_demand / objective_shortfall_weight /
    over_target_reward_pct / solver_random_seed. Set every non-default value
    imaginable and require identity except the two documented overrides."""
    import phase2_scheduler as p2
    from data_loader import Params
    P = Params(horizon_h=504, use_sku_rates=True, soft_demand=True,
               objective_shortfall_weight=7, over_target_reward_pct=2.5,
               solver_random_seed=99, min_run_hours=3, max_lines_per_order=1,
               co_ffs_weight=601, objective_idle_weight=11,
               planning_start_date="2026-09-07 00:00:00", allow_week1_in_week0=True)
    P0 = p2._sub_phase_params(P, horizon_h=168)
    for f in dataclasses.fields(Params):
        if f.name in ("horizon_h", "allow_week1_in_week0"):
            continue
        assert getattr(P0, f.name) == getattr(P, f.name), f.name
    assert P0.horizon_h == 168 and P0.allow_week1_in_week0 is False
    assert P is not P0


# ===========================================================================
# T-4  the live-data guard (tests-6)
# ===========================================================================
def test_t4_guard_allowlist_names_existing_modules_and_tests_only():
    """Every allowlisted module / test must exist -- a renamed file would
    silently lose its exemption AND the debt record would go stale."""
    for name in cft.LIVE_READ_ALLOWED_MODULES:
        assert (ROOT / "tests" / name).exists(), name
    for nodeid in cft.LIVE_READ_ALLOWED_TESTS:
        mod, func = nodeid.split("::")
        src = (ROOT / "tests" / mod).read_text(encoding="utf-8")
        assert f"def {func}(" in src, nodeid


def test_t4_pinned_fixtures_are_not_live_data():
    live = cft.LIVE_REFERENCE_DIR          # the guard's own notion of "live"
    assert not cft._is_live_read(str(FIXTURE / "demand_plan.csv"), "r")
    assert not cft._is_live_read(str(ROOT / "data" / "test_fixtures" / "live_2026-08-13" / "manprg.txt"), "r")
    assert cft._is_live_read(str(live / "downtimes.csv"), "r")
    assert cft._is_live_read(live / "manprg.txt", "rb")
    # writes are not reads; file descriptors are ignored
    assert not cft._is_live_read(str(live / "x.csv"), "w")
    assert not cft._is_live_read(3, "r")


# ===========================================================================
# T-5  TypeScript parity toolchain and dist freshness (tests-9)
# ===========================================================================
def test_t5_node_toolchain_is_present_for_the_ts_parity_tests():
    """55 parity tests skip when node or frontend/node_modules is missing.
    On this box both exist, so a skip would be a silent loss -- fail loudly
    (FLOWSTATE_ALLOW_NODE_SKIPS=1 documents a box without the toolchain)."""
    import os
    if os.environ.get("FLOWSTATE_ALLOW_NODE_SKIPS"):
        pytest.skip("FLOWSTATE_ALLOW_NODE_SKIPS set: toolchain absence accepted on this box")
    assert shutil.which("node") is not None, "node not on PATH -- TS parity tests would skip"
    assert (FRONTEND / "node_modules" / "typescript" / "bin" / "tsc").exists(), \
        "frontend node_modules missing (npm install) -- TS parity tests would skip"


def test_t5_dist_bundle_is_not_older_than_any_src_file():
    """The Streamlit component is declared against frontend/dist (gantt
    __init__.py); the parity tests compile frontend/src. A dist older than a
    src file ships numbers the suite never proved."""
    dist = FRONTEND / "dist" / "index.js"
    assert dist.exists(), "frontend/dist/index.js missing -- run npm run build"
    dist_m = dist.stat().st_mtime
    newer = sorted(str(p.relative_to(FRONTEND)) for p in (FRONTEND / "src").rglob("*")
                   if p.suffix in (".ts", ".tsx", ".css") and p.stat().st_mtime > dist_m + 1.0)
    assert not newer, "src files newer than dist/index.js (rebuild the bundle): " + ", ".join(newer)


def test_t5_dist_bundle_carries_the_src_string_literals():
    """The bundle is a browser build (React + Streamlit), not executable under
    node, so behavioural parity is compiled from src. What CAN be checked
    against dist: every plain string literal (>= 6 chars, no template
    substitution) that appears in frontend/src/utils/*.ts must appear verbatim
    in the bundle -- minification keeps literals. A missing one means the
    bundle was built from different source."""
    dist = (FRONTEND / "dist" / "index.js").read_text(encoding="utf-8", errors="ignore")
    # Plain literals only: no ':' ',' '(' ')' '?' so the text BETWEEN two
    # adjacent literals in a template-free expression cannot match.
    lit = re.compile(r"""(?<![\w$])(?:"([A-Za-z0-9 _./%<>=!+*-]{6,})"|'([A-Za-z0-9 _./%<>=!+*-]{6,})')""")
    checked, missing = 0, []
    for f in sorted((FRONTEND / "src" / "utils").glob("*.ts")):
        t = f.read_text(encoding="utf-8")
        t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
        t = re.sub(r"//[^\n]*", "", t)
        for a, b in lit.findall(t):
            s = a or b
            if s.startswith(("./", "../")):
                continue          # import specifiers are resolved away
            checked += 1
            if s not in dist:
                missing.append(f"{f.name}: {s!r}")
    assert checked >= 20, f"only {checked} literals found -- regex too strict?"
    assert not missing, "src string literals absent from dist/index.js:\n  " + "\n  ".join(missing)


# ===========================================================================
# T-6  pure-Python properties (tests-11)
# ===========================================================================
@pytest.mark.parametrize("dt, key", [
    # 2026-12-31 is a Thursday of ISO week 53 (2026 has 53 ISO weeks: it
    # starts on a Thursday); 2027-01-03 (Sunday) still belongs to 2026-W53;
    # 2027-01-04 (Monday) opens 2027-W01; 2026-01-01 (Thursday) is 2026-W01;
    # 2025-12-29 (Monday) is already 2026-W01.
    (datetime(2026, 12, 31), 202653),
    (datetime(2027, 1, 3, 23, 59), 202653),
    (datetime(2027, 1, 4), 202701),
    (datetime(2026, 1, 1), 202601),
    (datetime(2025, 12, 29), 202601),
    (datetime(2026, 8, 31), 202636),       # the Monday of ISO 2026-W36
])
def test_t6_iso_week_keys_match_the_calendar(dt, key):
    from helpers.demand_coverage import iso_week_key
    assert iso_week_key(dt) == key
    y, w, _ = dt.isocalendar()
    assert key == y * 100 + w


def test_t6_week_index_to_iso_from_a_monday_anchor():
    """week_index 0 = the anchor week: anchor Mon 2026-12-28 is ISO 2026-W53,
    +1 week -> 2027-W01, +2 -> W02."""
    from helpers.timefmt import week_index_to_iso, week_label
    anchor = datetime(2026, 12, 28)
    assert [week_index_to_iso(i, anchor) for i in (0, 1, 2)] == [53, 1, 2]
    assert week_label(1, anchor) == "WW01"


def test_t6_netting_invariant_gross_equals_applied_plus_net():
    """build_ledger / apply_ledger on a hand-built frame (Monday anchor
    2026-08-31 = ISO W36, hours [0,168) = W36, [168,336) = W37):
      X-W36 gross 10,000; committed block 3,000 kg in [0,10) -> applied 3,000, net 7,000
      X-W37 gross 4,000; block 6,000 kg in [168,180) -> applied 4,000, net 0,
            surplus 2,000 (over-committed, nothing to absorb it)
      Y-W36 gross 5,000; no supply -> applied 0, net 5,000
    Invariants: gross == applied + net on every row; applied <= gross;
    apply_ledger target == gross - applied, never below 0."""
    from helpers.demand_coverage import apply_ledger, build_ledger
    anchor = datetime(2026, 8, 31)
    dem = pd.DataFrame([
        {"order_id": "X-W36", "sku": "X", "week_index": 0, "qty_target": 10000, "lower_pct": 0.9,
         "upper_pct": 1.1, "due_start_hour": 0, "due_end_hour": 167, "priority": 3},
        {"order_id": "X-W37", "sku": "X", "week_index": 1, "qty_target": 4000, "lower_pct": 0.9,
         "upper_pct": 1.1, "due_start_hour": 168, "due_end_hour": 335, "priority": 3},
        {"order_id": "Y-W36", "sku": "Y", "week_index": 0, "qty_target": 5000, "lower_pct": 0.9,
         "upper_pct": 1.1, "due_start_hour": 0, "due_end_hour": 167, "priority": 3},
    ])
    blocks = pd.DataFrame([
        {"block_id": "b1", "block_type": "production", "line_id": 0, "line_name": "P09",
         "start_h": 0.0, "end_h": 10.0, "label": "X", "order_id": "30001", "sku": "X",
         "sku_description": "", "qty_kg": 3000.0, "locked": True, "attrs": "current_state:queued"},
        {"block_id": "b2", "block_type": "production", "line_id": 0, "line_name": "P09",
         "start_h": 168.0, "end_h": 180.0, "label": "X", "order_id": "30002", "sku": "X",
         "sku_description": "", "qty_kg": 6000.0, "locked": True, "attrs": "current_state:queued"},
    ])
    ledger = build_ledger(dem, blocks, anchor=anchor)
    rows = {r.order_id: r for r in ledger.rows}
    assert set(rows) == {"X-W36", "X-W37", "Y-W36"}
    for r in ledger.rows:
        assert abs(r.gross_kg - (r.applied_kg + r.net_kg)) < 1e-6, r
        assert 0 <= r.applied_kg <= r.gross_kg + 1e-6, r
    assert (rows["X-W36"].applied_kg, rows["X-W36"].net_kg) == (3000.0, 7000.0)
    assert (rows["X-W37"].applied_kg, rows["X-W37"].net_kg) == (4000.0, 0.0)
    assert (rows["Y-W36"].applied_kg, rows["Y-W36"].net_kg) == (0.0, 5000.0)
    assert ledger.overcommitted["X"]["surplus_kg"] == 2000.0
    dem2, _notes = apply_ledger(dem, ledger)
    tgt = dem2.set_index("order_id")["qty_target"]
    assert (tgt["X-W36"], tgt["X-W37"], tgt["Y-W36"]) == (7000.0, 0.0, 5000.0)
    assert (dem2["qty_target"] >= 0).all()


def test_t6_reforecast_guards():
    """Guards of the running-MO re-forecast, each with its hand case:
      * a rate under 0.25x catalog is CLAMPED to the floor (C02), never a cliff:
        made 5 in 50 h = 0.1 cas/h vs catalog 10 -> used 2.5 cas/h,
        end = as_of + 95/2.5 = +38 h
      * a rate over 2x catalog is unusable (counter jump): 960 in 40 h = 24 > 20
      * start after the observation -> unusable
      * an extrapolated end behind now is floored at now + 0.25 h
    """
    from helpers.current_state import _reforecast
    t0 = datetime(2027, 3, 1, 0, 0)
    slow = {"start_dt": pd.Timestamp(t0), "fct_cas": 100.0, "made_cas": 5.0, "left_cas": 95.0,
            "completion_pct": 5.0, "qty_kg": 1000.0}
    rf = _reforecast(slow, t0 + timedelta(hours=50), 10.0, as_of=t0 + timedelta(hours=50))
    assert rf.clamped and rf.used_cph == 2.5 and rf.end == t0 + timedelta(hours=88)
    fast = {"start_dt": pd.Timestamp(t0), "fct_cas": 1000.0, "made_cas": 960.0, "left_cas": 40.0,
            "completion_pct": 96.0, "qty_kg": 1000.0}
    rf = _reforecast(fast, t0 + timedelta(hours=40), 10.0)
    assert rf.end is None and "outside" in rf.why
    future = dict(slow, start_dt=pd.Timestamp(t0 + timedelta(hours=60)))
    rf = _reforecast(future, t0 + timedelta(hours=50), 10.0)
    assert rf.end is None and "not before" in rf.why
    # made 90 of 100 in 10 h (9 cas/h), left 10 -> end = as_of + 1.11 h;
    # rendered 5 h later the MO should be done -> floored at now + 0.25 h
    nearly = {"start_dt": pd.Timestamp(t0), "fct_cas": 100.0, "made_cas": 90.0, "left_cas": 10.0,
              "completion_pct": 90.0, "qty_kg": 1000.0}
    now = t0 + timedelta(hours=15)
    rf = _reforecast(nearly, now, 10.0, as_of=t0 + timedelta(hours=10))
    assert rf.end == now + timedelta(minutes=15) and "behind now" in rf.why


def test_t6_cip_split_conserves_hours_and_kg():
    """The Python side of the split rule (current_state._clip_prod_around_cips,
    CA-4): a 30 h / 6,000 kg block with a 6 h clean at hour 12 becomes
    [0,12) + [18,36): 12 + 18 = 30 h kept, kg pro-rated by duration
    12/30 x 6,000 = 2,400 and 18/30 x 6,000 = 3,600 -> 6,000 conserved."""
    from helpers.current_state import _clip_prod_around_cips
    blk = {"block_id": "b_A", "block_type": "production", "line_id": 0, "line_name": "P09",
           "start_h": 0.0, "end_h": 30.0, "label": "A", "order_id": "A", "sku": "280581",
           "sku_description": "", "qty_kg": 6000.0, "locked": False,
           "attrs": "current_state:queued"}
    cip = {"block_id": "c1", "block_type": "cip", "line_id": 0, "line_name": "P09",
           "start_h": 12.0, "end_h": 18.0, "label": "CIP", "order_id": "", "sku": "",
           "sku_description": "", "qty_kg": 0.0, "locked": True,
           "attrs": "current_state:cip_scheduled"}
    out, kept = _clip_prod_around_cips([blk], [cip], warnings=[], line="P09")
    assert [(b["start_h"], b["end_h"]) for b in out] == [(0.0, 12.0), (18.0, 36.0)]
    assert sum(b["end_h"] - b["start_h"] for b in out) == 30.0
    assert [b["qty_kg"] for b in out] == [2400.0, 3600.0]
    assert sum(b["qty_kg"] for b in out) == 6000.0
    assert len(kept) == 1


# ===========================================================================
# T-7  the probes' early-fill window is the PLANT rule, not the model's
#      Monday constants (tests-7 / modelcore-2 / time-2)
# ===========================================================================
def test_t7_probe_window_rule_by_hand_on_a_wednesday_frame():
    """Wednesday-anchored F frame: W0 [0,119], W1 [120,287], W2 [288,455].
    RE-DERIVED 2026-09-04 for the plant decision "early fill can go as far
    into the previous weeks as needed, except cannot be placed before an
    already scheduled MO" (this test's own docstring asked for exactly that
    when the solver's rule changes). With allow_week1_in_week0 on and no
    early_fill_hours key (unbounded) every demand order's due floor is 0;
    a current MO stays at its own due_start (plant fact). With
    early_fill_hours = 48 EVERY later week opens 48 h early: W1 at 72, W2 at
    240 (fix SB-1 had W1 at 72 and W2 at 288 -- second week only). With the
    flag off every order opens at its due_start. The old mirror
    (due_start > 167 -> 120) would have put W1 at 120 and W2 at 120."""
    from test_constraint_probes import _second_week_start, _window_open
    orders = {
        "A-W0": dict(sku="A", due_start=0, due_end=119, current_mo=False),
        "B-W1": dict(sku="B", due_start=120, due_end=287, current_mo=False),
        "C-W2": dict(sku="C", due_start=288, due_end=455, current_mo=False),
        "MO1|CUR": dict(sku="A", due_start=130, due_end=200, current_mo=True),
    }
    assert _second_week_start(orders) == 120
    on = {"allow_week1_in_week0": True}                       # unbounded
    assert [_window_open(o, orders, on) for o in orders.values()] == [0, 0, 0, 130]
    on48 = {"allow_week1_in_week0": True, "early_fill_hours": 48}
    assert _window_open(orders["A-W0"], orders, on48) == 0
    assert _window_open(orders["B-W1"], orders, on48) == 72
    assert _window_open(orders["C-W2"], orders, on48) == 240
    assert _window_open(orders["MO1|CUR"], orders, on48) == 130
    off = {"allow_week1_in_week0": False}
    assert [_window_open(o, orders, off) for o in orders.values()] == [0, 120, 288, 130]
    # a single demand week: floor 0 unbounded, its own start when bounded
    one = {"A-W0": orders["A-W0"], "MO1|CUR": orders["MO1|CUR"]}
    assert _second_week_start(one) is None
    assert _window_open(one["A-W0"], one, on) == 0 == _window_open(one["A-W0"], one, on48)
    # the floor never goes negative (a week starting inside the first 48 h)
    tight = {"X": dict(sku="X", due_start=0, due_end=23, current_mo=False),
             "Y": dict(sku="Y", due_start=24, due_end=191, current_mo=False)}
    assert _window_open(tight["Y"], tight, on48) == 0


def test_t7_probe_window_rule_agrees_with_the_solver_on_the_hand_cases():
    """The bridge between the independent statement and the model: on the
    same Wednesday frame model_builder.effective_due_start gives the same
    hours -- unbounded (Params default, 2026-09-04): 0 / 0 / 0 / 130;
    early_fill_hours 48: 0 / 72 / 240 / 130; flag off: 0 / 120 / 288 / 130.
    If the solver's rule changes, THIS test goes red and the probes must be
    re-derived from the plant rule -- never re-copied from the model."""
    from data_loader import Params
    from model_builder import effective_due_start, week_frame
    from test_constraint_probes import _window_open
    solver_orders = [
        dict(order_id="A-W0", sku="A", due_start=0, due_end=119),
        dict(order_id="B-W1", sku="B", due_start=120, due_end=287),
        dict(order_id="C-W2", sku="C", due_start=288, due_end=455),
        dict(order_id="MO1|CUR", sku="A", due_start=130, due_end=200, is_current_mo=True),
    ]
    probe_orders = {o["order_id"]: dict(sku=o["sku"], due_start=o["due_start"], due_end=o["due_end"],
                                        current_mo=bool(o.get("is_current_mo")))
                    for o in solver_orders}
    frame = week_frame(solver_orders)
    assert Params().early_fill_hours is None   # unbounded is the default
    for flag in (True, False):
        for efh in (None, 48, 0):
            P = Params(horizon_h=504, allow_week1_in_week0=flag, early_fill_hours=efh)
            cfg = {"allow_week1_in_week0": flag, "early_fill_hours": efh}
            for o in solver_orders:
                assert effective_due_start(P, o, frame) == _window_open(
                    probe_orders[o["order_id"]], probe_orders, cfg), (flag, efh, o["order_id"])
