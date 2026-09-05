# tests/test_fix_V.py -- fix V (audit 2026-09-03): solve orchestration,
# validation surfacing, two-pass adoption, VIF write-back record.
#
# Findings covered (see scratchpad/fixes/V/CHANGES.md):
#   C84/config-5   _run_two_phase rebuilt Params by hand and dropped fields
#   C83/orch-7     pass 2 adopted blindly whenever FEASIBLE
#   C81/V-1        independent validator after every solve -> report
#   C63/orch-1     level-3 (ignore_co) outputs labelled UNSAFE
#   C85/orch-9     relax level 1/2 must keep committed MO bounds
#   C80/orch-4     pass-1 outputs written before pass 2; --deterministic
#   C47/config-7   validate_schedule CIP check: tolerance 0, original limits,
#                  stand-down reported as NOT CHECKED
#   orch-11/12     mode banner survives reset_err; two-phase W0 warm-start gates
#   writeback-5/6/7 mo_changes.csv: dropped / manprg start / one row per piece
#
# Every expected value is derived by hand in the test body. CP-SAT solves are
# tiny (<= 2 lines, <= 4 orders, 2 workers, <= 10 s).
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ortools.sat.python import cp_model  # noqa: E402

import phase2_scheduler as p2  # noqa: E402
from data_loader import Data, Files, Params  # noqa: E402
from model_builder import build_model  # noqa: E402
from validate_schedule import (  # noqa: E402
    CIP_STANDDOWN_MIN, check_cip_spacing, resolve_cip_check_inputs, validate_all,
)

PY = ROOT / ".venv" / "Scripts" / "python.exe"
SOLVER_SRC = ROOT / "code" / "solver" / "phase2_scheduler.py"


# ═══════════════════════════════════════════════════════════════════════════
# C84 / config-5: two-phase sub-params keep EVERY field
# ═══════════════════════════════════════════════════════════════════════════
def test_sub_phase_params_keep_every_field_but_the_two_overrides():
    P = Params()
    P.horizon_h = 504
    P.allow_week1_in_week0 = True
    P.use_sku_rates = True              # the field config-5 saw dropped
    P.soft_demand = True
    P.objective_shortfall_weight = 3
    P.over_target_reward_pct = 2.5
    P.solver_random_seed = 7
    P.min_run_hours = 4
    P0 = p2._sub_phase_params(P, horizon_h=168)
    assert P0.horizon_h == 168
    assert P0.allow_week1_in_week0 is False
    # hand list of the five fields the old constructor silently dropped
    assert P0.use_sku_rates is True
    assert P0.soft_demand is True
    assert P0.objective_shortfall_weight == 3
    assert P0.over_target_reward_pct == 2.5
    assert P0.solver_random_seed == 7
    # and by construction: every other field is identical to P
    for f in dataclasses.fields(Params):
        if f.name in ("horizon_h", "allow_week1_in_week0"):
            continue
        assert getattr(P0, f.name) == getattr(P, f.name), f.name
    # the CLI --min-run-hours override still wins, as before
    assert p2._sub_phase_params(P, horizon_h=168, min_run_override=2).min_run_hours == 2
    assert P.min_run_hours == 4  # the parent is untouched (replace copies)


# ═══════════════════════════════════════════════════════════════════════════
# C83 / orchestration-7: pass-2 adoption rule
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("co1,co2,err1,err2,expect", [
    (100, 90, 0, 0, True),       # fewer changeovers, same errors -> adopt
    (100, 100, 0, 0, True),      # equal load is "not worse" -> adopt
    (100, 101, 0, 0, False),     # ONE unit more load -> keep pass 1
    (100, 50, 0, 1, False),      # better load but a NEW validator error -> keep
    (100, 50, 2, 2, True),       # same error count (not worse) -> adopt
    (100, 50, None, 0, True),    # unknown pass-1 errors cannot veto
    (100, 50, 0, None, True),    # unknown pass-2 errors cannot veto
    (None, 50, 0, 0, False),     # load not evaluable -> never adopt blindly
    (100, None, 0, 0, False),
    (0, 0, 0, 0, True),          # no CO term at all (ints 0) -> adopt
])
def test_adopt_pass2_rule(co1, co2, err1, err2, expect):
    ok, why = p2._adopt_pass2(co1, co2, err1, err2)
    assert ok is expect, why
    assert why  # the decision is always explained in the log


# ═══════════════════════════════════════════════════════════════════════════
# C81 / V-1: validation summary counts PHYSICAL errors only
# ═══════════════════════════════════════════════════════════════════════════
def test_validation_summary_physical_count_by_hand():
    rep = {
        "ok": False, "n_errors": 6, "n_warnings": 2,
        "counts": {
            "OVERLAP": {"ERROR": 2, "WARN": 0},         # physical
            "CIP_INTERVAL": {"ERROR": 1, "WARN": 0},    # physical
            "DEMAND_BOUNDS": {"ERROR": 3, "WARN": 0},   # NOT physical (tonnage)
            "MIN_RUN": {"ERROR": 0, "WARN": 2},         # warnings only
        },
        "violations": [
            {"code": "OVERLAP", "severity": "ERROR", "line": "P09", "order": "A", "hours": 2.0, "detail": "x"},
            {"code": "MIN_RUN", "severity": "WARN", "line": "P09", "order": "B", "hours": 1.0, "detail": "y"},
        ],
        "checks_run": ["a", "b", "c"],
    }
    summ = p2._validation_summary(rep)
    # 2 (OVERLAP) + 1 (CIP_INTERVAL) = 3 physical; DEMAND_BOUNDS is excluded
    assert summ["physical_errors"] == 3
    assert summ["n_errors"] == 6 and summ["n_warnings"] == 2
    assert summ["checks_run"] == 3
    # only ERROR-severity violations are listed as "top"; the WARN is not
    assert len(summ["top_violations"]) == 1
    assert summ["top_violations"][0].startswith("OVERLAP line=P09 order=A [2h]")
    assert set(p2.PHYSICAL_ERROR_CODES) == {
        "OVERLAP", "IN_DOWNTIME", "CIP_INTERVAL", "CHANGEOVER_GAP", "BEFORE_GATE"}


# ═══════════════════════════════════════════════════════════════════════════
# C63 / orchestration-1: level-3 labels
# ═══════════════════════════════════════════════════════════════════════════
def test_level_report_fields_label_ignore_co():
    assert p2._level_report_fields(0) == {"ignore_co": False, "setup_times_enforced": True}
    assert p2._level_report_fields(2) == {"ignore_co": False, "setup_times_enforced": True}
    assert p2._level_report_fields(3) == {"ignore_co": True, "setup_times_enforced": False}
    # a model that keeps setup TIME at level 3 (agent SA) announces it
    assert p2._level_report_fields(3, {"setup_times_enforced": True}) == {
        "ignore_co": True, "setup_times_enforced": True}


# ═══════════════════════════════════════════════════════════════════════════
# C85 / orchestration-9: committed MO bounds survive relax level 1
# ═══════════════════════════════════════════════════════════════════════════
def _tiny_data_with_mo(P: Params) -> Data:
    """One line P09 @1000 kg/h, one demand order (SKU 111) and one committed
    MO (SKU 222, 4000 kg remaining -> exactly 4 h at 1000 kg/h)."""
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = [0]
    d.line_names = {0: "P09"}
    for sku in ("111", "222"):
        d.capable[(0, sku)] = 1
        d.rate[(0, sku)] = 1000.0
    d.init_map = {0: {"available_from": 0, "initial_sku": "222", "carryover_run_hours": 0}}
    d.downtimes = []
    d.cip_interval_map = {0: 100000}
    d.orders = [
        dict(order_id="A-W0", sku="111", due_start=0, due_end=167,
             qty_min=9000, qty_max=11000, qty_target=10000, priority=3),
        dict(order_id="M1|CUR", sku="222", due_start=0, due_end=167,
             qty_min=4000, qty_max=4000, qty_remaining=4000, priority=0,
             is_current_mo=True, mo_id="M1", locked_line=0, source="manprg"),
    ]
    return d


def test_keep_committed_mo_bounds_states_and_effect():
    P = Params(horizon_h=168, min_run_hours=1, min_run_pct_of_qty=0.0,
               max_lines_per_order=1, allow_week1_in_week0=False,
               objective_idle_weight=0)
    data = _tiny_data_with_mo(P)
    # level 0: bounds are hard already
    m0, v0 = build_model(P, data, "full", False, False, maximize_production=False)
    assert p2._keep_committed_mo_bounds(m0, v0, data, 0) == ("n/a", 0)
    # level 3: released (last resort), nothing added
    m3, v3 = build_model(P, data, "full", True, True, maximize_production=False)
    assert p2._keep_committed_mo_bounds(m3, v3, data, 3) == ("released", 1)
    # level 1 (relax_demand): model_builder zeroes qty_min for EVERY order;
    # the orchestrator re-imposes produced >= 4000 for the MO only.
    m1, v1 = build_model(P, data, "full", True, False, maximize_production=False)
    n_before = len(m1.Proto().constraints)
    assert p2._keep_committed_mo_bounds(m1, v1, data, 1) == ("kept", 1)
    assert len(m1.Proto().constraints) == n_before + 1
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 2
    s.parameters.max_time_in_seconds = 10
    st = s.Solve(m1)
    assert st in (cp_model.FEASIBLE, cp_model.OPTIMAL)
    mo_idx = next(i for i, o in enumerate(data.orders) if o.get("is_current_mo"))
    # relax_demand alone would let the objective (min makespan/idle) drop the
    # MO to 0 kg; with the bound kept it makes at least its 4000 kg.
    assert s.Value(v1["produced"][mo_idx]) >= 4000


# ═══════════════════════════════════════════════════════════════════════════
# writeback-5/6/7: mo_changes.csv record
# ═══════════════════════════════════════════════════════════════════════════
def _mo_data(P: Params, **order_extra) -> Data:
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = [0]
    d.line_names = {0: "P09"}
    d.capable[(0, "280581")] = 1
    d.rate[(0, "280581")] = 1000.0
    d.orders = [{
        "order_id": "29901|CUR", "sku": "280581", "due_start": 10,
        "due_end": 300, "qty_min": 10000, "qty_max": 10000,
        "priority": 0, "is_current_mo": True, "mo_id": "29901",
        "locked_line": 0, "source": "manprg", **order_extra,
    }]
    return d


def _bounds(produced: int) -> list[dict]:
    return [{"order_id": "29901|CUR", "sku": "280581", "qty_min": 10000,
             "qty_max": 10000, "produced": produced, "in_bounds": True}]


def test_mo_changes_dropped_mo_has_empty_hours_and_dropped_reason(tmp_path):
    P = Params()
    P.planning_start_date = "2026-09-07 00:00:00"
    d = _mo_data(P)
    # writeback-5: no block for the MO -> ONE row, reason 'dropped', hours EMPTY
    p2.write_mo_changes(tmp_path, d, [], _bounds(0), P=P)
    out = pd.read_csv(tmp_path / "mo_changes.csv", dtype={"mo": str},
                      keep_default_na=False)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["reason"] == "dropped"
    assert row["new_start_h"] == "" and row["new_end_h"] == ""   # not "0"
    assert row["new_start_dt"] == "" and row["new_end_dt"] == ""
    assert int(row["split_count"]) == 0 and row["piece"] == ""
    assert int(row["delta_kg"]) == -10000
    assert row["planning_anchor"] == "2026-09-07 00:00:00"
    assert row["scenario_id"] == tmp_path.name


def test_mo_changes_split_mo_is_written_as_pieces_with_wallclock(tmp_path):
    P = Params()
    P.planning_start_date = "2026-09-07 00:00:00"
    d = _mo_data(P)
    schedule = [
        {"line_id": 0, "line_name": "P09", "order_id": "29901|CUR", "sku": "280581",
         "start_hour": 40, "end_hour": 45, "run_hours": 5},
        {"line_id": 0, "line_name": "P09", "order_id": "29901|CUR", "sku": "280581",
         "start_hour": 51, "end_hour": 56, "run_hours": 5},
    ]
    p2.write_mo_changes(tmp_path, d, schedule, _bounds(10000), P=P)
    out = pd.read_csv(tmp_path / "mo_changes.csv", dtype={"mo": str})
    # writeback-7: two pieces, each with ITS OWN window (the 45-51 CIP gap is
    # not reported as booked)
    assert len(out) == 2
    assert list(out["piece"]) == [1, 2]
    assert list(out["new_start_h"]) == [40, 51]
    assert list(out["new_end_h"]) == [45, 56]
    assert list(out["split_count"]) == [2, 2]
    # hand: anchor Mon 2026-09-07 00:00; 40 h = 1 d 16 h -> Tue 08 16:00;
    # 45 h -> Tue 08 21:00; 51 h = 2 d 3 h -> Wed 09 03:00; 56 h -> Wed 09 08:00
    assert list(out["new_start_dt"]) == ["2026-09-08 16:00:00", "2026-09-09 03:00:00"]
    assert list(out["new_end_dt"]) == ["2026-09-08 21:00:00", "2026-09-09 08:00:00"]
    # orig start 10 (due_start projection) != 40 -> reordered; tonnage equal
    assert out.iloc[0]["reason"] == "split+reordered"
    assert list(out["orig_start_src"]) == ["due_start", "due_start"]
    assert out.iloc[0]["planning_anchor"] == "2026-09-07 00:00:00"
    # historical twelve columns come first, unchanged
    assert list(out.columns)[:12] == p2.MO_CHANGES_COLUMNS[:12]


def test_mo_changes_orig_start_prefers_manprg_start(tmp_path):
    P = Params()
    P.planning_start_date = "2026-09-07 00:00:00"
    # writeback-6: due_start is the solver bound (0 for a running MO); the
    # plant's committed start is manprg_start_h = 30 (HANDOFF: CB writes the
    # column into current_mo.csv, SA passes it through data_loader).
    d = _mo_data(P, due_start=0, manprg_start_h=30)
    schedule = [{"line_id": 0, "line_name": "P09", "order_id": "29901|CUR",
                 "sku": "280581", "start_hour": 30, "end_hour": 40, "run_hours": 10}]
    p2.write_mo_changes(tmp_path, d, schedule, _bounds(10000), P=P)
    out = pd.read_csv(tmp_path / "mo_changes.csv", dtype={"mo": str})
    assert int(out.iloc[0]["orig_start_h"]) == 30
    assert out.iloc[0]["orig_start_src"] == "manprg"
    # planned at 30 == manprg 30 -> NOT a reorder (the old code compared with
    # due_start 0 and flagged every running MO 'reordered')
    assert out.iloc[0]["reason"] == "unmoved"


def test_mo_changes_without_params_keeps_working(tmp_path):
    """Backward compatibility: callers that do not pass P get blank datetimes."""
    d = _mo_data(Params())
    p2.write_mo_changes(tmp_path, d, [], _bounds(0))
    out = pd.read_csv(tmp_path / "mo_changes.csv", keep_default_na=False)
    assert list(out.columns) == p2.MO_CHANGES_COLUMNS
    assert out.iloc[0]["planning_anchor"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# C47 / config-7: validate_schedule CIP check
# ═══════════════════════════════════════════════════════════════════════════
def _write_sched(d: Path, segs: list[tuple[int, int, int, str]]) -> None:
    pd.DataFrame(
        [{"line_id": lid, "line_name": f"L{lid + 1}", "order_id": f"O{i}",
          "sku": "A", "start_hour": s, "end_hour": e, "run_hours": e - s,
          "qty_kg": 1000 * (e - s)}
         for i, (lid, s, e, _) in enumerate(segs)]
    ).to_csv(d / "schedule_phase2.csv", index=False)


def test_cip_spacing_tolerance_is_zero_not_twelve(tmp_path):
    # one line, production 0-121 h, no CIP, carry 0: 121 h since the (t0)
    # reference. Old check: 121 > 120 + 12 -> silent OK. New: 121 > 120 + 0.
    _write_sched(tmp_path, [(0, 0, 121, "A")])
    issues = check_cip_spacing(tmp_path / "schedule_phase2.csv", tmp_path / "cip_windows.csv",
                               tmp_path / "initial_states.csv", interval_h=120)
    assert issues == ["CIP: Line L1 -- 121h clock since last CIP (limit 120h, tolerance 0h) at h121"]
    # the explicit tolerance is honoured (and named)
    assert check_cip_spacing(tmp_path / "schedule_phase2.csv", tmp_path / "cip_windows.csv",
                             tmp_path / "initial_states.csv", interval_h=120,
                             tolerance_h=12)[0].endswith("OK.")


def test_cip_spacing_standdown_is_not_checked_never_ok(tmp_path):
    _write_sched(tmp_path, [(0, 0, 300, "A"), (1, 0, 50, "A")])
    lc = tmp_path / "line_cip_hrs.csv"
    # L1 stood down (fill-mode sentinel), L2 real 120 h limit
    lc.write_text("line_id,line_name,max_cip_hrs\n0,L1,100000\n1,L2,120\n", encoding="utf-8")
    issues = check_cip_spacing(tmp_path / "schedule_phase2.csv", tmp_path / "cip_windows.csv",
                               tmp_path / "initial_states.csv", interval_h=120,
                               line_cip_hrs_path=lc)
    # L1: 300 h without a clean is NOT reported OK - it was not checked
    assert any(i.startswith("CIP: NOT CHECKED on 1 of 2 line(s)") and "L1" in i for i in issues)
    assert any(i.endswith("OK.") for i in issues)  # L2 (50 h) is fine
    assert not any("Line L1" in i for i in issues)
    # every line stood down -> no OK line at all
    lc.write_text("line_id,line_name,max_cip_hrs\n0,L1,100000\n1,L2,100000\n", encoding="utf-8")
    issues = check_cip_spacing(tmp_path / "schedule_phase2.csv", tmp_path / "cip_windows.csv",
                               tmp_path / "initial_states.csv", interval_h=120,
                               line_cip_hrs_path=lc)
    assert len(issues) == 1 and issues[0].startswith("CIP: NOT CHECKED on 2 of 2")
    assert CIP_STANDDOWN_MIN == 10000


def test_validate_all_reads_original_limits_and_cip_interval(tmp_path):
    # data/ layout: reference/line_cip_hrs.csv (the ORIGINAL, L1=110) and the
    # work dir data/_scenario_work/E with the stood-down copy + a toml whose
    # [cip] interval_h = 100 (used for lines the reference does not list).
    ref = tmp_path / "reference"
    ref.mkdir()
    ref.joinpath("line_cip_hrs.csv").write_text(
        "line_id,line_name,max_cip_hrs\n0,L1,110\n", encoding="utf-8")
    work = tmp_path / "_scenario_work" / "E"
    work.mkdir(parents=True)
    work.joinpath("flowstate.toml").write_text(
        "[scheduler]\nhorizon_hours = 336\n\n[cip]\ninterval_h = 100\nduration_h = 6\n",
        encoding="utf-8")
    # L1 (id 0) runs 0-115: over the reference 110 h limit; L2 (id 1, not in
    # the reference file) runs 0-105: over the toml's 100 h
    _write_sched(work, [(0, 0, 115, "A"), (1, 0, 105, "A")])
    pd.DataFrame(columns=["order_id", "sku", "qty_min", "qty_max", "produced", "in_bounds"]).to_csv(
        work / "produced_vs_bounds.csv", index=False)
    pd.DataFrame(columns=["order_id", "sku"]).to_csv(work / "demand_plan.csv", index=False)
    pd.DataFrame(columns=["from_sku", "to_sku", "setup_hours"]).to_csv(work / "changeovers.csv", index=False)
    inputs = resolve_cip_check_inputs(work)
    assert inputs["interval_h"] == 100 and inputs["interval_src"] == "[cip] interval_h"
    assert inputs["line_cip_hrs_path"] == ref / "line_cip_hrs.csv"
    assert inputs["line_cip_hrs_src"] == "reference (original)"
    assert inputs["tolerance_h"] == 0.0
    rep = validate_all(work, verbose=False)
    text = "\n".join(rep)
    assert "CIP: Line L1 -- 115h clock since last CIP (limit 110h, tolerance 0h) at h115" in text
    assert "CIP: Line L2 -- 105h clock since last CIP (limit 100h, tolerance 0h) at h105" in text
    assert "per-line limits from" in text and "reference (original)" in text
    # now the work dir carries the fill-mode stand-down: limits still come
    # from the reference, but the stood-down line is reported NOT CHECKED
    work.joinpath("line_cip_hrs.csv").write_text(
        "line_id,line_name,max_cip_hrs\n0,L1,100000\n1,L2,120\n", encoding="utf-8")
    text = "\n".join(validate_all(work, verbose=False))
    assert "NOT CHECKED on 1 of 2 line(s)" in text and "L1 (reference limit 110h)" in text
    assert "Line L1 --" not in text
    assert "Line L2 -- 105h" in text
    # [validator] cip_tolerance_h is honoured when given explicitly via cfg
    text = "\n".join(validate_all(
        work, verbose=False,
        cfg={"cip": {"interval_h": 100}, "validator": {"cip_tolerance_h": 6}}))
    assert "tolerance 6h" in text and "Line L2 -- 105h" not in text  # 105 <= 100 + 6


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end (subprocess): tiny data dir through phase2_scheduler.py
# ═══════════════════════════════════════════════════════════════════════════
SKUS = ["A", "B", "C"]


def _build_dir(out: Path, *, soft: bool, two_pass: bool, seed: int | None = 7,
               tl: int = 5, use_sku_rates: bool = False, horizon: int = 336,
               extra_sched: str = "") -> Path:
    """2 lines (L1 1000 kg/h, L2 800 kg/h), 3 orders: A-W0 B-W0 (week 0),
    C-W1 (week 1). Tiny enough for OPTIMAL in well under a second."""
    out.mkdir(parents=True, exist_ok=True)
    cap = ["line_id,sku,line_name,capable,calc_rate_kgph"]
    for lid, ln in ((0, "L1"), (1, "L2")):
        for s in SKUS:
            cap.append(f"{lid},{s},{ln},1,500.0")
    (out / "capabilities_rates.csv").write_text("\n".join(cap) + "\n", encoding="utf-8")
    (out / "line_rates.csv").write_text("line_id,Line,rate_kgph\n0,L1,1000\n1,L2,800\n", encoding="utf-8")
    chg = ["from_sku,to_sku,setup_hours,ttp_change,ffs_change,tpld_change,"
           "cspkr_change,conv_to_org,cinn_to_non_cinn,added_flavors,cip_req_after"]
    for a in SKUS:
        for b in SKUS:
            if a != b:
                chg.append(f"{a},{b},5,1,0,0,0,0,0,0,0")
    (out / "changeovers.csv").write_text("\n".join(chg) + "\n", encoding="utf-8")
    (out / "sku_info.csv").write_text(
        "sku,designation,format,casepacker_format,topload_format,pouch_format,is_organic,has_cinnamon\n"
        + "\n".join(f"{s},SKU {s},4x12x90,12,4,90,0,0" for s in SKUS) + "\n", encoding="utf-8")
    (out / "demand_plan.csv").write_text(
        "order_id,sku,week_index,qty_target,lower_pct,upper_pct,due_start_hour,due_end_hour,priority\n"
        "A-W0,A,0,10000,0.9,1.1,0,167,3\n"
        "B-W0,B,0,8000,0.9,1.1,0,167,3\n"
        "C-W1,C,1,8000,0.9,1.1,168,335,3\n", encoding="utf-8")
    (out / "initial_states.csv").write_text(
        "line_id,line_name,initial_sku,available_from_hour,long_shutdown_flag,"
        "long_shutdown_extra_setup_hours,carryover_run_hours_since_last_cip_at_t0,"
        "last_cip_end_datetime,comment\n0,L1,A,0,0,0,0,,\n1,L2,CLEAN,0,0,0,0,,\n", encoding="utf-8")
    (out / "line_cip_hrs.csv").write_text("line_id,line_name,max_cip_hrs\n0,L1,120\n1,L2,120\n", encoding="utf-8")
    (out / "downtimes.csv").write_text("line_id,line_name,start_hour,end_hour,reason\n", encoding="utf-8")
    (out / "flowstate.toml").write_text(f'''planning_start_date = "2026-09-07 00:00:00"

[scheduler]
planning_start_date = "2026-09-07 00:00:00"
horizon_hours = {horizon}
time_limit = {tl}
min_run_hours = 4
max_lines_per_order = 2
validate = true
use_sku_rates = {'true' if use_sku_rates else 'false'}
use_current_mo = false
soft_demand = {'true' if soft else 'false'}
two_pass_co = {'true' if two_pass else 'false'}
{f'solver_random_seed = {seed}' if seed is not None else ''}
{extra_sched}

[cip]
interval_h = 120
duration_h = 6

[objective]
makespan_weight = 6
changeover_weight = 120
cip_defer_weight = 5
late_weight = 200
idle_weight = 3

[changeover]
base_changeover_weight = 5
topload_weight = 50
ttp_weight = 5
ffs_weight = 75
casepacker_weight = 20
cip_req_weight = 150
''', encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def solver_w2(tmp_path_factory) -> Path:
    """A verbatim copy of phase2_scheduler.py with the 8-worker portfolio cut
    to 2 workers (CPU discipline while other agents share the box)."""
    src = SOLVER_SRC.read_text(encoding="utf-8")
    needle = "SEARCH_WORKERS = 1 if DETERMINISTIC else 8\n"
    assert src.count(needle) == 1
    dst = tmp_path_factory.mktemp("solver") / "phase2_scheduler_w2.py"
    dst.write_text(src.replace(needle, "SEARCH_WORKERS = 1 if DETERMINISTIC else 2\n"),
                   encoding="utf-8")
    return dst


def _run(solver: Path, d: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "code" / "solver"), str(ROOT / "code")])
    cmd = [str(PY), str(solver), "--data-dir", str(d), "--config", str(d / "flowstate.toml"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=240, env=env)


def _log(d: Path) -> str:
    return (d / "solver_error.txt").read_text(encoding="utf-8")


def _report(d: Path) -> dict:
    return json.loads((d / "feasibility_report.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(not PY.exists(), reason="repo venv python not found")
def test_two_pass_run_saves_pass1_first_validates_and_records_decision(tmp_path, solver_w2):
    d = _build_dir(tmp_path / "d", soft=True, two_pass=True)
    proc = _run(solver_w2, d)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    log = _log(d)
    lines = log.splitlines()
    # orchestration-11: the mode banners are the FIRST lines (they survive
    # the log reset), before START
    assert "[soft-demand]" in lines[0]
    assert "[two-pass] enabled" in lines[1]
    assert any(ln.split("] ", 1)[1].startswith("START") for ln in lines[2:4])
    # V-9: budget honesty
    assert "[budget] single-phase: 5s per solve, 2 workers" in log
    assert "two-pass adds anchor <= 5s + pass 2 5s" in log
    # C80: pass 1 is on disk BEFORE the anchor/pass 2 start
    i_saved = log.index("[two-pass] pass 1 outputs written")
    i_anchor = log.index("[two-pass] anchor ")
    assert i_saved < i_anchor
    # SA-3 floors + exchange rate went into the pass-2 build
    assert re.search(r"\[two-pass\] \d+ per-order floors .* fill exchange rate K=\d+", log)
    # C83: the decision is logged with BOTH loads
    m = re.search(r"co_load (\S+) vs pass 1 (\S+) - (ADOPTING pass 2|KEEPING pass 1)", log)
    assert m, log
    rep = _report(d)
    tp = rep["two_pass"]
    assert tp["adopted"] in ("pass 1", "pass 2")
    assert "co_load_pass1" in tp and "decision" in tp
    if tp["adopted"] == "pass 2":
        assert tp["co_load_pass2"] <= tp["co_load_pass1"]
    # C81: the independent validator ran and is in the report
    val = rep["validation"]
    assert isinstance(val["n_errors"], int) and isinstance(val["physical_errors"], int)
    assert val["physical_error_codes"] == list(p2.PHYSICAL_ERROR_CODES)
    assert (d / "validation_independent.txt").exists()
    # C63: a level-0 plan is labelled as enforcing setup times
    assert rep["setup_times_enforced"] is True and rep["ignore_co"] is False
    assert rep["deterministic"] is False and rep["search_workers"] == 2
    assert (d / "schedule_phase2.csv").exists()
    # the classic validator ran with the ORIGINAL config (config-7 plumbing)
    vr = (d / "validation_report.txt").read_text(encoding="utf-8")
    assert "CIP check inputs: interval_h=120 ([cip] interval_h)" in vr


@pytest.mark.skipif(not PY.exists(), reason="repo venv python not found")
def test_ignore_changeovers_run_is_labelled_unsafe(tmp_path, solver_w2):
    """UPDATED 2026-09-03 (INTEGRATE closing V's NOT DONE): fix SA-6 keeps
    setup TIME at relax level 3 (ignore_co drops only the PRICE) and the
    model now announces it (`vars_dict["setup_times_enforced"] = True`), so
    the level-3 report reads ignore_co True / setup_times_enforced True and
    the UNSAFE label — which was the conservative interim reading while the
    model did not expose the flag — no longer appears. The label path itself
    stays covered by test_level_report_fields_label_ignore_co."""
    d = _build_dir(tmp_path / "d", soft=False, two_pass=False)
    proc = _run(solver_w2, d, "--ignore-changeovers")
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    rep = _report(d)
    assert rep["relax_level"] == 3
    assert rep["ignore_co"] is True
    assert rep["setup_times_enforced"] is True
    assert "UNSAFE: changeover times not enforced" not in _log(d)
    assert "UNSAFE" not in (d / "solver_kpis.txt").read_text(encoding="utf-8")
    assert "validation" in rep
    assert rep["model_warnings"] == []      # INTEGRATE: key always present


@pytest.mark.skipif(not PY.exists(), reason="repo venv python not found")
def test_deterministic_flag_is_reproducible_and_recorded(tmp_path, solver_w2):
    d1 = _build_dir(tmp_path / "d1", soft=True, two_pass=False, seed=7)
    d2 = _build_dir(tmp_path / "d2", soft=True, two_pass=False, seed=7)
    for d in (d1, d2):
        proc = _run(solver_w2, d, "--deterministic")
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
        rep = _report(d)
        assert rep["deterministic"] is True and rep["search_workers"] == 1
        assert "[seed] --deterministic: every solve runs with num_search_workers=1" in _log(d)
        assert "1 workers" in _log(d)  # the solve banner names the worker count
    # same inputs + same seed + one worker -> the same plan (OPTIMAL both times)
    s1 = pd.read_csv(d1 / "schedule_phase2.csv").drop(columns=["start_dt", "end_dt"], errors="ignore")
    s2 = pd.read_csv(d2 / "schedule_phase2.csv").drop(columns=["start_dt", "end_dt"], errors="ignore")
    pd.testing.assert_frame_equal(s1, s2)


@pytest.mark.skipif(not PY.exists(), reason="repo venv python not found")
def test_two_phase_keeps_params_and_validates(tmp_path, solver_w2):
    d = _build_dir(tmp_path / "d", soft=False, two_pass=False, use_sku_rates=True)
    proc = _run(solver_w2, d, "--two-phase")
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    log = _log(d)
    # C84: the sub-phase Params carry use_sku_rates / seed from the toml
    assert "[two-phase] sub-phase params: use_sku_rates=True soft_demand=False seed=7" in log
    assert "[budget] two-phase: 5s per week solve, 2 workers" in log
    # orchestration-12: the W0 warm start went through the trust gates (this
    # is a cold dir -> "skipped: solve inputs changed" is the gate speaking)
    assert "[warm-start] W0 skipped" in log or "[warm-start] hinted" in log
    rep = _report(d)
    assert "validation" in rep and isinstance(rep["validation"]["n_errors"], int)
    assert rep["setup_times_enforced"] is True
    assert rep["committed_mo_bounds"] == {"week0": "n/a", "week1": "n/a"}
    assert (d / "schedule_phase2.csv").exists()


# ═══════════════════════════════════════════════════════════════════════════
# C80 / orchestration-4: the pass-2 crash mitigations are pinned in the source
# ═══════════════════════════════════════════════════════════════════════════
def test_c80_pass2_crash_mitigations_are_present_in_source():
    """Evidence (scratchpad/bench/runs/B7_free): with the pre-fix two-pass
    block, phase2_scheduler.py aborted with exit 0xC0000409 'Check failed:
    heuristics.fixed_search != nullptr' in 4 of 9 subprocess runs over seeds
    1-3 (base_small_s2, repro2_s1, repro2_s2, vrepro_s3), always right after
    'anchor OPTIMAL: complete hint installed', i.e. inside the pass-2 solve
    (8 workers, repair_hint=True, complete hint, soft-demand decision
    strategy). With the four mitigations below: 0 of 6 runs (vfix_s1..s6).
    A structural pin so a refactor cannot silently drop one of them."""
    src = SOLVER_SRC.read_text(encoding="utf-8")
    # (b) anchor solve: no search strategy (every strategy var is fixed by
    #     the hint -> an EMPTY strategy is what trips the CHECK)
    assert "_proto2.search_strategy.clear()" in src
    assert "_proto2.search_strategy.extend(_strategy_backup)" in src
    # (c) anchor = fixed-assignment check, ONE worker
    assert "ANCHOR_WORKERS = 1" in src
    assert "_s2a.parameters.num_search_workers = ANCHOR_WORKERS" in src
    # (b') pass 2: the 'fixed' subsolver is excluded and repair_hint is off
    assert '_s2.parameters.ignore_subsolvers.append("fixed")' in src
    assert "_s2.parameters.repair_hint = False" in src
    # (a) pass-1 outputs are on disk BEFORE the anchor / pass-2 solves
    i_write = src.index("_pass1_report = _write_single_phase_outputs(")
    i_anchor = src.index("_s2a = cp_model.CpSolver()")
    assert i_write < i_anchor
    # --deterministic exists and drives the worker count
    assert '"--deterministic"' in src
    assert "SEARCH_WORKERS = 1 if DETERMINISTIC else 8" in src


# ═══════════════════════════════════════════════════════════════════════════
# Page helpers (generate.py / compare.py) - pure functions extracted by ast so
# the Streamlit page body is not executed
# ═══════════════════════════════════════════════════════════════════════════
def _page_funcs(page: str, *names: str) -> dict:
    import ast
    src = (ROOT / "code" / "pages" / page).read_text(encoding="utf-8")
    tree = ast.parse(src)
    keep = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) in names for t in node.targets):
            keep.append(node)
    assert len(keep) == len(names), f"{page}: found {[getattr(k, 'name', None) for k in keep]}"
    ns: dict = {"pd": pd, "Path": Path}
    exec(compile(ast.Module(body=keep, type_ignores=[]), page, "exec"), ns)
    return ns


def test_generate_expected_wall_time_by_hand():
    """Fix V-9 (time-limit honesty): the slider is ONE solve's budget."""
    g = _page_funcs("generate.py", "_expected_wall_s", "_fmt_wall")
    # two-pass fill, 600 s: pass 1 600 + anchor min(120, 600)=120 + pass 2 600
    # + ~60 staging = 1380 s (23 min); ceiling: 4 ladder levels x 600 + 120 +
    # 600 + 60 = 3180 s
    assert g["_expected_wall_s"](600, two_pass=True, two_phase=False) == (1380, 3180)
    # two-phase, 60 s: W0 + W1 = 120 + 60 = 180; ceiling 2 weeks x 4 levels x
    # 60 + 60 = 540
    assert g["_expected_wall_s"](60, two_pass=False, two_phase=True) == (180, 540)
    # single-phase, 60 s: 120 typical, 4 x 60 + 60 = 300 ceiling
    assert g["_expected_wall_s"](60, two_pass=False, two_phase=False) == (120, 300)
    assert g["_fmt_wall"](1380) == "23 min 00 s"
    assert g["_fmt_wall"](45) == "45 s"
    assert g["_fmt_wall"](3180) == "53 min 00 s"


def test_generate_staging_warnings_are_extracted_from_the_log():
    """Agent CB handoff (C54): ledger WARNINGs and staging checks surface."""
    g = _page_funcs("generate.py", "_staging_warnings", "_STAGING_WARN_PREFIXES")
    log = "\n".join([
        "[current state] 3 MOs; WARNING: made kg of MO 29901 exceeds Fct; ok",
        "[12:00:01] [stage] committed windows -> downtimes",
        "[12:00:02] CIP CHECK — P09: 131 h since last clean at h40 (limit 120)",
        "[12:00:02] CIP CHECK — P09: 131 h since last clean at h40 (limit 120)",
        "GATE CHECK — P12 gated at h500 of 504: no fill capacity",
        "[12:00:03] SOLVER level=0 status=OPTIMAL",
        "WARNING: history_demand covers 2 of 3 lookback weeks",
    ])
    got = g["_staging_warnings"](log)
    # hand: the "[current state] " tag is stripped and its "; "-joined notes
    # split -> the embedded WARNING (not "3 MOs" / "ok") is extracted; the
    # duplicated CIP CHECK line appears once; timestamps are stripped; the
    # [stage] / SOLVER lines carry no prefix; order preserved.
    assert got == [
        "WARNING: made kg of MO 29901 exceeds Fct",
        "CIP CHECK — P09: 131 h since last clean at h40 (limit 120)",
        "GATE CHECK — P12 gated at h500 of 504: no fill capacity",
        "WARNING: history_demand covers 2 of 3 lookback weeks",
    ]
    # a line joining several notes splits; unprefixed parts are dropped
    assert g["_staging_warnings"]("WARNING: a; WARNING: b; plain") == ["WARNING: a", "WARNING: b"]
    assert g["_staging_warnings"](None) == []
    assert g["_staging_warnings"]("WARNING: x\n" * 30, limit=3) == ["WARNING: x"]


def test_compare_version_validation_line_by_hand():
    c = _page_funcs("compare.py", "_version_validation")
    f = c["_version_validation"]
    line, phys = f(None)
    assert phys is None and line.startswith("validation: not recorded")
    line, phys = f({"feasibility": {
        "relax_level": 3, "relax_mode": "ignore_co", "setup_times_enforced": False,
        "validation": {"n_errors": 5, "physical_errors": 2, "n_warnings": 1},
        "two_pass": {"adopted": "pass 1"}}})
    assert phys == 2
    assert "relax level 3 (ignore_co)" in line
    assert "UNSAFE: changeover times NOT enforced" in line
    assert "validator: 5 error(s) (2 physical), 1 warning(s)" in line
    assert "two-pass: pass 1 adopted" in line
    # validated clean -> 0 physical (an int, so the page uses caption not error)
    line, phys = f({"feasibility": {"relax_level": 0, "relax_mode": "hard",
                                    "validation": {"n_errors": 0, "physical_errors": 0,
                                                   "n_warnings": 0}}})
    assert phys == 0
    # feasibility present but the validator did not run -> None (unchecked)
    line, phys = f({"feasibility": {"relax_level": 0, "relax_mode": "hard",
                                    "validation": {"n_errors": None}}})
    assert phys is None and "independent validator: not run" in line


def test_compare_fill_ledger_inputs_roundtrip():
    """Agent Q handoff 3 (C57): the JSON-safe metadata record becomes the
    typed ledger_inputs score_calendar expects."""
    from datetime import datetime
    c = _page_funcs("compare.py", "_fill_ledger_inputs")
    f = c["_fill_ledger_inputs"]
    assert f(None) is None
    assert f({"fill_ledger": {}}) is None
    assert f({"fill_ledger": {"anchor": "not a date"}}) is None   # never raises
    out = f({"fill_ledger": {
        "anchor": "2026-09-07 00:00:00",
        "history_demand": {"280581|36": 1000.0, "280582|37": 250},
        "completed": [{"mo": "1", "made_kg": 5, "start_dt": "2026-09-01 08:00:00", "end_dt": ""}],
        "lookback_weeks": 3}})
    assert out["anchor"] == datetime(2026, 9, 7)
    assert out["history_demand"] == {("280581", 36): 1000.0, ("280582", 37): 250.0}
    assert out["lookback_weeks"] == 3
    assert out["completed"][0]["start_dt"] == pd.Timestamp("2026-09-01 08:00:00")
    assert out["completed"][0]["end_dt"] == ""          # blanks stay blank
    assert out["completed"][0]["made_kg"] == 5
