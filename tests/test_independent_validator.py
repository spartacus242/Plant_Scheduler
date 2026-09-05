# tests/test_independent_validator.py -- golden, hand-derived cases for the
# INDEPENDENT schedule validator (code/solver/independent_validator.py).
#
# A tiny 2-line / 4-order world is built in tmp_path; one schedule is fully
# valid (report.ok True, zero violations) and every check has a case that
# breaks exactly one rule, asserting code / line / order / hours by hand.
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "code", ROOT / "code" / "solver"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from solver import independent_validator as iv  # noqa: E402
from solver.independent_validator import (  # noqa: E402
    ERROR, WARN, Violation, validate_work_dir, validate_calendar, main,
)

SNAPSHOT_F = Path(
    "C:/Users/jbdil/AppData/Local/Temp/claude/C--Users-jbdil-Flowstate-Plant-Scheduler/"
    "4676946a-cd87-4d76-9139-b1ba1df3896a/scratchpad/snapshot/F_workdir")

TOML = """
[scheduler]
planning_start_date = "2026-09-07 00:00:00"
horizon_hours = 336
min_run_hours = 4
max_lines_per_order = 2
use_sku_rates = false
soft_demand = false

[cip]
interval_h = 120
duration_h = 6
"""


# ---------------------------------------------------------------------------
# World: L1 (id 0, 1000 kg/h flat) and L2 (id 1, 500 kg/h flat); SKUs A, B, C.
# ---------------------------------------------------------------------------
def _write(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False)


def build_world(d: Path, *, changeover_extra=None, line_cip=None, init=None,
                downtimes=None, demand=None, toml: str = TOML) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "flowstate.toml").write_text(toml, encoding="utf-8")
    _write(d / "capabilities_rates.csv", pd.DataFrame([
        (0, "A", "L1", 1, 900.0), (0, "B", "L1", 1, 900.0), (0, "C", "L1", 1, 900.0),
        (1, "A", "L2", 1, 400.0), (1, "B", "L2", 1, 400.0), (1, "C", "L2", 0, 400.0),
    ], columns=["line_id", "sku", "line_name", "capable", "calc_rate_kgph"]))
    _write(d / "line_rates.csv", pd.DataFrame([(0, "L1", 1000), (1, "L2", 500)],
                                              columns=["line_id", "Line", "rate_kgph"]))
    _write(d / "sku_info.csv", pd.DataFrame([("A", "sku A"), ("B", "sku B"), ("C", "sku C")],
                                            columns=["sku", "designation"]))
    co_rows = [
        ("A", "B", 1.5, 0), ("B", "A", 2.0, 0),
        ("A", "C", 0.5, 1), ("C", "A", 0.5, 0),
        ("B", "C", 1.0, 0), ("C", "B", 1.0, 0),
        ("B", "C", 1.0, 0),  # exact duplicate (agrees) -> dedup, no conflict
    ] + list(changeover_extra or [])
    _write(d / "changeovers.csv", pd.DataFrame(
        [(f, t, s, 1, 0, 0, 0, 0, 0, 0, cr) for (f, t, s, cr) in co_rows],
        columns=["from_sku", "to_sku", "setup_hours", "ttp_change", "ffs_change", "tpld_change",
                 "cspkr_change", "conv_to_org", "cinn_to_non_cinn", "added_flavors", "cip_req_after"]))
    _write(d / "initial_states.csv", pd.DataFrame(init or [
        (0, "L1", "A", 0, 0, 0, 0, "", ""),
        (1, "L2", "CLEAN", 10, 0, 0, 0, "", ""),
    ], columns=["line_id", "line_name", "initial_sku", "available_from_hour", "long_shutdown_flag",
                "long_shutdown_extra_setup_hours", "carryover_run_hours_since_last_cip_at_t0",
                "last_cip_end_datetime", "comment"]))
    _write(d / "downtimes.csv", pd.DataFrame(downtimes or [
        (1, "L2", 0, 10, "Down"),
        (0, "L1", 200, 206, "Committed CIP"),
    ], columns=["line_id", "line_name", "start_hour", "end_hour", "reason"]))
    _write(d / "demand_plan.csv", pd.DataFrame(demand or [
        ("O1", "A", 0, 40000, 0.9, 1.1, 0, 119, 3),
        ("O2", "B", 0, 20000, 0.9, 1.1, 0, 119, 3),
        ("O3", "C", 1, 10000, 0.9, 1.1, 120, 287, 3),
        ("O4", "A", 1, 30000, 0.9, 1.1, 120, 287, 3),
    ], columns=["order_id", "sku", "week_index", "qty_target", "lower_pct", "upper_pct",
                "due_start_hour", "due_end_hour", "priority"]))
    _write(d / "line_cip_hrs.csv", pd.DataFrame(line_cip or [(0, "L1", 120), (1, "L2", 120)],
                                                columns=["line_id", "line_name", "max_cip_hrs"]))
    return d


SCHED_COLS = ["line_id", "line_name", "order_id", "sku", "sku_description", "start_hour",
              "end_hour", "run_hours", "qty_kg", "start_dt", "end_dt", "is_trial"]


def _row(lid, name, oid, sku, s, e, kg, trial=False):
    s, e, kg = float(s), float(e), float(kg)   # float columns: pandas 3 refuses float->int upcasts
    return (lid, name, oid, sku, f"sku {sku}", s, e, e - s, kg, "", "", trial)


def valid_schedule() -> pd.DataFrame:
    return pd.DataFrame([
        _row(0, "L1", "O1", "A", 0, 20, 20000),
        _row(0, "L1", "O2", "B", 22, 42, 20000),      # A->B 1.5h, gap 2h
        _row(0, "L1", "O4", "A", 120, 150, 30000),    # B->A 2h, CIP [110,116) in gap
        _row(0, "L1", "O3", "C", 157, 167, 10000),    # A->C 0.5h + cip_req: CIP [150,156), 1h free
        _row(1, "L2", "O1", "A", 10, 42, 16000),      # gate 10, 500 kg/h
    ], columns=SCHED_COLS)


def valid_cips() -> pd.DataFrame:
    return pd.DataFrame([(0, "L1", 110.0, 116.0), (0, "L1", 150.0, 156.0)],
                        columns=["line_id", "line_name", "start_hour", "end_hour"])


def valid_pvb() -> pd.DataFrame:
    return pd.DataFrame([
        ("O1", "A", 36000.0, 44000.0, 36000.0, True),
        ("O2", "B", 18000.0, 22000.0, 20000.0, True),
        ("O3", "C", 9000.0, 11000.0, 10000.0, True),
        ("O4", "A", 27000.0, 33000.0, 30000.0, True),
    ], columns=["order_id", "sku", "qty_min", "qty_max", "produced", "in_bounds"])


def write_outputs(d: Path, sched=None, cips=None, pvb=None) -> None:
    _write(d / "schedule_phase2.csv", valid_schedule() if sched is None else sched)
    _write(d / "cip_windows.csv", valid_cips() if cips is None else cips)
    _write(d / "produced_vs_bounds.csv", valid_pvb() if pvb is None else pvb)


@pytest.fixture
def world(tmp_path):
    d = build_world(tmp_path / "w")
    write_outputs(d)
    return d


def codes(rep):
    return sorted({v.code for v in rep.violations})


def one(rep, code, **match):
    hits = [v for v in rep.by_code(code)
            if all(getattr(v, k) == val for k, val in match.items())]
    assert hits, f"no {code} with {match}; got {[v.as_line() for v in rep.violations]}"
    return hits


# ---------------------------------------------------------------------------
# Golden valid case
# ---------------------------------------------------------------------------
def test_valid_world_is_clean(world):
    rep = validate_work_dir(world)
    assert rep.violations == [], [v.as_line() for v in rep.violations]
    assert rep.ok is True
    assert rep.stats["blocks_by_kind"] == {"production": 5, "cip": 2}
    assert rep.stats["cleans_total"] == 3          # 2 cip_windows + 1 'Committed CIP' downtime
    assert rep.stats["cleans_from_downtime_rows"] == 1
    assert rep.stats["changeover_duplicate_rows"] == 1
    assert rep.stats["changeover_duplicate_conflicts"] == 0
    assert rep.stats["changeover_pairs_loaded"] == 6
    assert rep.stats["rate_dialect"] == "flat line_rates.csv"
    assert rep.stats["production_kg"] == 96000.0
    for name in iv.ALL_CHECKS:
        assert any(c.startswith(name) for c in rep.checks_run), name
    assert any(c.startswith("TRIAL_MOVED (skipped") for c in rep.checks_run)


def test_cli_exit_codes_and_json(world, tmp_path, capsys):
    js = tmp_path / "rep.json"
    assert main(["--data-dir", str(world), "--json", str(js)]) == 0
    assert js.exists() and '"ok": true' in js.read_text()
    out = capsys.readouterr().out
    assert "ok=True" in out and "violations by code" in out
    # break it -> exit 1
    s = valid_schedule()
    s.loc[1, "start_hour"] = 15
    write_outputs(world, sched=s)
    assert main(["--data-dir", str(world)]) == 1


# ---------------------------------------------------------------------------
# One case per check
# ---------------------------------------------------------------------------
def test_overlap(world):
    s = valid_schedule()
    s.loc[1, "start_hour"] = 15          # O2 [15,42) vs O1 [0,20): 5h overlap
    s.loc[1, "run_hours"] = 27
    s.loc[1, "qty_kg"] = 27000
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = one(rep, "OVERLAP", line="L1", order="O1", severity=ERROR)[0]
    assert v.hours == 5.0
    assert rep.ok is False


def test_in_downtime(world):
    s = valid_schedule()
    s.loc[4, "start_hour"] = 5           # L2 O1 [5,42): 5h into 'Down' [0,10)
    s.loc[4, "run_hours"] = 37
    s.loc[4, "qty_kg"] = 18500
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    assert one(rep, "IN_DOWNTIME", line="L2", order="O1", severity=ERROR)[0].hours == 5.0
    assert one(rep, "BEFORE_GATE", line="L2", order="O1", severity=ERROR)[0].hours == 5.0


def test_line_not_capable(world):
    s = valid_schedule()
    s.loc[3, ["line_id", "line_name", "qty_kg"]] = [1, "L2", 5000]   # C on L2 (capable=0), 500 kg/h x 10h
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = one(rep, "LINE_NOT_CAPABLE", line="L2", order="O3", severity=ERROR)[0]
    assert v.hours == 10.0 and "capable=0" in v.detail


def test_qty_rate_mismatch_reports_both_deltas(world):
    s = valid_schedule()
    s.loc[1, "qty_kg"] = 25000           # 20h x 1000 = 20000 -> +5000 over-claim
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = one(rep, "QTY_RATE_MISMATCH", line="L1", order="O2", severity=ERROR)[0]
    assert v.hours == 20.0
    assert "delta +5000.0 kg" in v.detail                  # flat line_rates (config dialect)
    assert "900x20h (delta +7000.0 kg)" in v.detail        # capabilities calc_rate_kgph
    # under-claim is bookkeeping only -> WARN
    s.loc[1, "qty_kg"] = 15000
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    assert one(rep, "QTY_RATE_MISMATCH", order="O2", severity=WARN)


def test_qty_rate_uses_sku_rates_when_configured(world):
    (world / "flowstate.toml").write_text(TOML.replace("use_sku_rates = false", "use_sku_rates = true"),
                                          encoding="utf-8")
    rep = validate_work_dir(world)   # every block was sized at the flat rate -> all mismatch
    assert rep.stats["rate_dialect"] == "capabilities calc_rate_kgph"
    assert len(rep.by_code("QTY_RATE_MISMATCH")) == 5
    v = one(rep, "QTY_RATE_MISMATCH", line="L1", order="O1")[0]
    assert "900x20h=18000" in v.detail and "delta +2000.0 kg" in v.detail


def test_run_hours_mismatch(world):
    s = valid_schedule()
    s.loc[1, "run_hours"] = 19
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    assert one(rep, "RUN_HOURS_MISMATCH", line="L1", order="O2", severity=ERROR)[0].hours == 1.0


def test_min_run_per_block(world):
    s = valid_schedule()
    s.loc[3, ["end_hour", "run_hours", "qty_kg"]] = [160, 3, 3000]   # O3 C 3h
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = one(rep, "MIN_RUN", line="L1", order="O3", severity=ERROR)
    assert len(v) == 1 and v[0].hours == 3.0
    assert one(rep, "DEMAND_BOUNDS", order="O3", severity=ERROR)   # 3000 < 9000, soft_demand off


def test_min_run_per_line_order_total(world):
    s = valid_schedule()
    # split O3 into two 2h pieces on L1 -> each block < 4 AND the (line, order) total 4? no: make total 3.5
    s = s.drop(index=3)
    extra = pd.DataFrame([_row(0, "L1", "O3", "C", 157, 159, 2000),
                          _row(0, "L1", "O3", "C", 160, 161.5, 1500)], columns=SCHED_COLS)
    write_outputs(world, sched=pd.concat([s, extra], ignore_index=True))
    rep = validate_work_dir(world)
    per_block = [v for v in rep.by_code("MIN_RUN") if "block run" in v.detail]
    total = [v for v in rep.by_code("MIN_RUN") if "total" in v.detail]
    assert len(per_block) == 2 and len(total) == 1
    assert total[0].line == "L1" and total[0].order == "O3" and total[0].hours == 3.5


def test_max_lines_per_order(world):
    rep = validate_work_dir(world, config={"scheduler": {"max_lines_per_order": 1, "horizon_hours": 336},
                                           "cip": {"interval_h": 120, "duration_h": 6}})
    v = one(rep, "MAX_LINES_PER_ORDER", order="O1", severity=ERROR)[0]
    assert "2 lines > max_lines_per_order 1" in v.detail
    assert set(codes(rep)) == {"MAX_LINES_PER_ORDER"}


def test_before_gate(world):
    s = valid_schedule()
    s.loc[4, ["start_hour", "end_hour", "run_hours", "qty_kg"]] = [8, 40, 32, 16000]   # gate 10
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    assert one(rep, "BEFORE_GATE", line="L2", order="O1", severity=ERROR)[0].hours == 2.0
    assert one(rep, "IN_DOWNTIME", line="L2", order="O1")[0].hours == 2.0


def test_initial_setup_from_initial_sku(world):
    # L1 initial_sku B: first block A at 0 needs B->A 2.0h -> shortfall 2h
    build_world(world, init=[(0, "L1", "B", 0, 0, 0, 0, "", ""), (1, "L2", "CLEAN", 10, 0, 0, 0, "", "")])
    rep = validate_work_dir(world)
    v = one(rep, "INITIAL_SETUP", line="L1", order="O1", severity=ERROR)[0]
    assert v.hours == 2.0 and "setup(B->A)=2" in v.detail
    # long shutdown flag adds the extra on top of the gate (L2: gate 10 + 3 = 13 > 10)
    build_world(world, init=[(0, "L1", "A", 0, 0, 0, 0, "", ""), (1, "L2", "CLEAN", 10, 1, 3, 0, "", "")])
    rep = validate_work_dir(world)
    assert one(rep, "INITIAL_SETUP", line="L2", order="O1")[0].hours == 3.0


def test_trial_moved(world):
    _write(world / "trials.csv", pd.DataFrame([("T1", 0, 170, 180)],
                                              columns=["order_id", "line_id", "start_hour", "end_hour"]))
    s = pd.concat([valid_schedule(),
                   pd.DataFrame([_row(0, "L1", "T1", "C", 172, 182, 10000, trial=True)], columns=SCHED_COLS)],
                  ignore_index=True)
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = one(rep, "TRIAL_MOVED", line="L1", order="T1", severity=ERROR)[0]
    assert v.hours == 4.0 and "pinned to L1 [170,180)" in v.detail
    assert one(rep, "UNKNOWN_ORDER", order="T1", severity=WARN)


def test_cip_interval_no_tolerance(world):
    # drop the CIP at [110,116): L1 runs from the t0 reference (carry 0) to 150 -> 150h > 120, excess 30
    write_outputs(world, cips=pd.DataFrame([(0, "L1", 150, 156)],
                                           columns=["line_id", "line_name", "start_hour", "end_hour"]))
    rep = validate_work_dir(world)
    v = one(rep, "CIP_INTERVAL", line="L1", severity=ERROR)
    assert len(v) == 1 and v[0].hours == 30.0 and "150h wall-clock" in v[0].detail
    # a 1h breach is still a breach (validate_schedule.py would let +12h pass)
    write_outputs(world, cips=pd.DataFrame([(0, "L1", 121, 127), (0, "L1", 150, 156)],
                                           columns=["line_id", "line_name", "start_hour", "end_hour"]))
    # production O4 [120,150) overlaps the clean at 121 -> move O4 to [127,150) instead
    s = valid_schedule()
    s.loc[2, ["start_hour", "run_hours", "qty_kg"]] = [127, 23, 23000]
    pvb = valid_pvb(); pvb.loc[3, "produced"] = 23000
    write_outputs(world, sched=s, cips=pd.DataFrame([(0, "L1", 121, 127), (0, "L1", 150, 156)],
                                                    columns=["line_id", "line_name", "start_hour", "end_hour"]),
                  pvb=pvb)
    rep = validate_work_dir(world)
    # span t0(0) -> clean start 121 contains production O1/O2 ending 42: fine (42h);
    # but does the clean itself start late? Production ended at 42, so the wall-clock
    # rule is only breached when PRODUCTION runs past 120h since the last clean.
    assert rep.by_code("CIP_INTERVAL") == []


def test_cip_interval_carryover_and_tail(world):
    # L2 carry 100 at t0 -> last clean ended at -100; production to 42 -> 142h > 120 (excess 22)
    build_world(world, init=[(0, "L1", "A", 0, 0, 0, 0, "", ""), (1, "L2", "CLEAN", 10, 0, 0, 100, "", "")])
    rep = validate_work_dir(world)
    v = one(rep, "CIP_INTERVAL", line="L2", severity=ERROR)[0]
    assert v.hours == 22.0 and "t0 - carry" in v.detail


def test_cip_interval_disabled_by_standdown_is_reported(world):
    build_world(world, line_cip=[(0, "L1", 100000), (1, "L2", 120)])
    write_outputs(world, cips=pd.DataFrame(columns=["line_id", "line_name", "start_hour", "end_hour"]))
    # no cleans on L1 except the committed one at 200; production runs to 167 -> would breach 120
    rep = validate_work_dir(world)
    assert rep.by_code("CIP_INTERVAL") == []
    note = next(c for c in rep.checks_run if c.startswith("CIP_INTERVAL"))
    assert "DISABLED" in note and "L1(max_cip_hrs=100000)" in note
    assert rep.stats["cip_interval_disabled_lines"] == ["L1(max_cip_hrs=100000)"]
    # ... but CIP_REQ_MISSING still fires (A->C needs a clean, none left)
    assert one(rep, "CIP_REQ_MISSING", line="L1", order="O3", severity=ERROR)[0].hours == 7.0


def test_cip_interval_falls_back_to_toml_interval(world):
    (world / "line_cip_hrs.csv").unlink()
    (world / "flowstate.toml").write_text(TOML.replace("interval_h = 120", "interval_h = 100"), encoding="utf-8")
    rep = validate_work_dir(world)
    # L1: t0 -> clean at 110 contains production ending 42 (fine); 116 -> 150 (34h) fine.
    # L1 first clean starts 110 but production ended 42 -> no breach. Nothing else.
    assert rep.by_code("CIP_INTERVAL") == []
    note = next(c for c in rep.checks_run if c.startswith("CIP_INTERVAL"))
    assert "interval_h=100 fallback" in note


def test_cip_duration(world):
    write_outputs(world, cips=pd.DataFrame([(0, "L1", 110, 117), (0, "L1", 150, 156)],
                                           columns=["line_id", "line_name", "start_hour", "end_hour"]))
    rep = validate_work_dir(world)
    v = one(rep, "CIP_DURATION", line="L1", severity=ERROR)[0]
    assert v.hours == 1.0 and "lasts 7h" in v.detail
    # a committed (downtime-row) clean of the wrong length is only a WARN
    build_world(world, downtimes=[(1, "L2", 0, 10, "Down"), (0, "L1", 200, 207, "Committed CIP")])
    write_outputs(world)
    rep = validate_work_dir(world)
    assert one(rep, "CIP_DURATION", line="L1", severity=WARN)[0].hours == 1.0


def test_cip_req_missing(world):
    s = valid_schedule()
    s.loc[3, ["start_hour", "end_hour"]] = [151, 161]       # A->C gap 1h, no clean inside
    write_outputs(world, sched=s, cips=pd.DataFrame([(0, "L1", 110, 116)],
                                                    columns=["line_id", "line_name", "start_hour", "end_hour"]))
    rep = validate_work_dir(world)
    v = one(rep, "CIP_REQ_MISSING", line="L1", order="O3", severity=ERROR)[0]
    assert v.hours == 1.0 and "A->C" in v.detail
    # direction-aware: C->A is NOT flagged (cip_req_after=0 for that direction)
    s2 = pd.DataFrame([_row(0, "L1", "O3", "C", 0, 10, 10000), _row(0, "L1", "O1", "A", 11, 47, 36000)],
                      columns=SCHED_COLS)
    pvb = valid_pvb(); pvb.loc[1, "produced"] = 0; pvb.loc[3, "produced"] = 0
    write_outputs(world, sched=s2, cips=pd.DataFrame(columns=["line_id", "line_name", "start_hour", "end_hour"]),
                  pvb=pvb)
    rep = validate_work_dir(world)
    assert rep.by_code("CIP_REQ_MISSING") == []


def test_changeover_gap_exact_float_and_rounding_note(world):
    s = valid_schedule()
    s.loc[1, ["start_hour", "run_hours", "qty_kg"]] = [21, 21, 21000]   # A->B needs 1.5h, gap 1h
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = one(rep, "CHANGEOVER_GAP", line="L1", order="O2", severity=ERROR)[0]
    assert v.hours == 0.5
    assert "setup 1.5h (exact; solver rounds half-up to 2)" in v.detail
    assert "gap not covered by any CIP" in v.detail


def test_changeover_gap_absorbed_by_cip_is_a_warning(world):
    s = valid_schedule()
    s.loc[3, ["start_hour", "run_hours", "qty_kg"]] = [156, 11, 11000]   # A->C 0.5h, gap 6h == the CIP
    pvb = valid_pvb(); pvb.loc[2, "produced"] = 11000
    write_outputs(world, sched=s, pvb=pvb)
    rep = validate_work_dir(world)
    v = one(rep, "CHANGEOVER_GAP", line="L1", order="O3", severity=WARN)[0]
    assert v.hours == 0.5 and "overlapping a CIP" in v.detail
    assert rep.ok is True


def test_changeover_duplicate_pair_conflict_is_reported_and_strictest_wins(world):
    build_world(world, changeover_extra=[("B", "C", 2.0, 0)])   # disagrees with the 1.0 rows
    rep = validate_work_dir(world)
    assert rep.stats["changeover_duplicate_conflicts"] == 1
    assert rep.stats["changeover_duplicate_rows"] == 2
    w = one(rep, "CHANGEOVER_GAP", severity=WARN)[0]
    assert "duplicate pair B->C disagrees" in w.detail and "strictest kept" in w.detail
    # the strictest value (2.0) is what the gap check uses
    s = pd.DataFrame([_row(0, "L1", "O2", "B", 0, 20, 20000), _row(0, "L1", "O3", "C", 21.5, 131.5, 110000)],
                     columns=SCHED_COLS)
    _write(world / "schedule_phase2.csv", s)
    rep = validate_work_dir(world)
    assert one(rep, "CHANGEOVER_GAP", order="O3", severity=ERROR)[0].hours == 0.5


def test_unknown_pair_is_a_warning_with_setup_zero(world):
    build_world(world, changeover_extra=[])
    (world / "changeovers.csv").write_text(
        "from_sku,to_sku,setup_hours,cip_req_after\nA,B,1.5,0\n", encoding="utf-8")
    rep = validate_work_dir(world)
    assert one(rep, "CHANGEOVER_GAP", line="L1", order="O4", severity=WARN)[0].detail.startswith(
        "no changeovers.csv row for B->A")


def test_horizon(world):
    """The world's toml has no due_week_policy key -> "hard" (the shipped
    default since 2026-09-04 night, matching the solver's Params), so a
    block finishing past due_end + 1 is a DUE_WINDOW ERROR and the horizon
    overrun is an ERROR too. O3: 10,000 kg over [330, 340), due_end 287 ->
    52 h past 288. Spelling the key out as "hard" gives the same finding.
    Opting in to the soft policy (plant decision 2026-09-04 #2) turns the
    late finish into a PRICED TRADE-OFF -> DUE_WINDOW WARN carrying the late
    kg / kg-weeks (whole block late -> ~10,000 kg; hours [288, 456) are one
    week late -> 10,000 kg-weeks); the horizon overrun stays an ERROR."""
    s = valid_schedule()
    s.loc[3, ["start_hour", "end_hour"]] = [330, 340]
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    assert one(rep, "HORIZON", line="L1", order="O3", severity=ERROR)[0].hours == 4.0
    v = one(rep, "DUE_WINDOW", line="L1", order="O3", severity=ERROR)[0]
    assert v.hours == 52.0 and "due_week_policy = hard" in v.detail   # 340 - 288
    assert rep.stats["due_week_policy"] == "hard"
    (world / "flowstate.toml").write_text(
        TOML.replace("[scheduler]", "[scheduler]\ndue_week_policy = \"hard\""), encoding="utf-8")
    rep = validate_work_dir(world)
    v = one(rep, "DUE_WINDOW", line="L1", order="O3", severity=ERROR)[0]
    assert v.hours == 52.0 and "due_week_policy = hard" in v.detail
    assert rep.stats["due_week_policy"] == "hard"
    (world / "flowstate.toml").write_text(
        TOML.replace("[scheduler]", "[scheduler]\ndue_week_policy = \"soft\""), encoding="utf-8")
    rep = validate_work_dir(world)
    assert one(rep, "HORIZON", line="L1", order="O3", severity=ERROR)[0].hours == 4.0
    v = one(rep, "DUE_WINDOW", line="L1", order="O3", severity=WARN)[0]
    assert v.hours == 52.0
    assert "priced trade-off" in v.detail and "10,000 kg late = 10,000 kg-weeks" in v.detail
    assert rep.stats["due_week_policy"] == "soft"
    assert rep.stats["late_kg"] == 10000.0 and rep.stats["late_kg_weeks"] == 10000.0
    assert rep.stats["late_orders"] == 1
    assert any(c.startswith("DUE_WINDOW") and "soft due weeks" in c for c in rep.checks_run)


def test_due_window_early(world):
    """UPDATED 2026-09-04 for the plant's early-fill policy ("early fill can
    go as far into the previous weeks as needed, except cannot be placed
    before an already scheduled MO"; supersedes fix SB-1's second-week-only
    48 h rule pinned here on 2026-09-03). The world's toml has no
    early_fill_hours key -> UNBOUNDED: an early start is an informational
    WARN "early fill (policy: unbounded)", never an ERROR. O2 (due from 30)
    at 22 is 8 h early -> WARN; O4 (due 120) at 60 is 60 h early -> WARN too
    (SB-1 called it an ERROR as the third week). What still invalidates an
    early start is the line's gate / a committed block / a committed MO —
    their own checks (BEFORE_GATE, OVERLAP, IN_DOWNTIME,
    EARLY_BEFORE_COMMITTED). With early_fill_hours = 48 the allowance is
    bounded for EVERY order: O2 (8 h) stays a WARN, O4 (60 h > 48) is an
    ERROR whose legal start is 72."""
    build_world(world, demand=[
        ("O1", "A", 0, 40000, 0.9, 1.1, 0, 119, 3),
        ("O2", "B", 0, 20000, 0.9, 1.1, 30, 119, 3),     # O2 due from 30; scheduled at 22
        ("O3", "C", 1, 10000, 0.9, 1.1, 120, 287, 3),
        ("O4", "A", 1, 30000, 0.9, 1.1, 120, 287, 3),
    ])
    s = valid_schedule()
    s.loc[2, ["start_hour", "end_hour"]] = [60.0, 90.0]      # O4 A: 30 h, 60 h early
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = one(rep, "DUE_WINDOW", line="L1", order="O2", severity=WARN)[0]
    assert v.hours == 8.0 and "early by 8h" in v.detail
    assert "early fill (policy: unbounded)" in v.detail
    v4 = one(rep, "DUE_WINDOW", line="L1", order="O4", severity=WARN)[0]
    assert v4.hours == 60.0 and "early by 60h" in v4.detail
    assert codes(rep) == ["DUE_WINDOW"]
    assert rep.ok is True and rep.stats["early_fill_hours"] == "unbounded"
    # bounded allowance: 48 h for every order
    (world / "flowstate.toml").write_text(
        TOML.replace("[scheduler]", "[scheduler]\nearly_fill_hours = 48"), encoding="utf-8")
    rep = validate_work_dir(world)
    v = one(rep, "DUE_WINDOW", line="L1", order="O2", severity=WARN)[0]
    assert v.hours == 8.0 and "inside the early-fill allowance" in v.detail and "from 0h" in v.detail
    v4 = one(rep, "DUE_WINDOW", line="L1", order="O4", severity=ERROR)[0]
    assert v4.hours == 60.0 and "legal start >= 72h" in v4.detail
    assert rep.ok is False and rep.stats["early_fill_hours"] == 48.0


def test_early_before_committed(world):
    """Plant decision 2026-09-04: nothing may be placed before an already
    scheduled MO. E-style work dir: current_mo.csv holds M1 (L1, A, 13,000
    kg remaining) and the solver wrote it as the row "M1|CUR" [60,73) on L1
    (13 h x 1,000). O1 [0,20) and O2 [22,42) on L1 start BEFORE that
    committed block -> two ERRORs with hours = committed end - start: O1 73,
    O2 51. O4 [120,150) and O3 [157,167) start after it -> nothing; L2 has no
    committed MO -> nothing. The |CUR row itself is not in demand_plan.csv
    (UNKNOWN_ORDER WARN, as before) and takes no due/demand check."""
    pd.DataFrame([("M1", "L1", "A", 13000, 0, 119, 1, "manprg")],
                 columns=["mo", "line_name", "sku", "remaining_kg", "due_start_h",
                          "due_end_h", "locked_line", "source"]
                 ).to_csv(world / "current_mo.csv", index=False)
    s = valid_schedule()
    s = pd.concat([s, pd.DataFrame([_row(0, "L1", "M1|CUR", "A", 60, 73, 13000)],
                                   columns=SCHED_COLS)], ignore_index=True)
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v1 = one(rep, "EARLY_BEFORE_COMMITTED", line="L1", order="O1", severity=ERROR)[0]
    assert v1.hours == 73.0 and "committed MO M1|CUR [60,73)" in v1.detail
    v2 = one(rep, "EARLY_BEFORE_COMMITTED", line="L1", order="O2", severity=ERROR)[0]
    assert v2.hours == 51.0
    assert {v.order for v in rep.by_code("EARLY_BEFORE_COMMITTED")} == {"O1", "O2"}
    assert rep.ok is False
    assert any(c.startswith("EARLY_BEFORE_COMMITTED (1 committed MO(s)") for c in rep.checks_run)
    assert one(rep, "UNKNOWN_ORDER", order="M1|CUR", severity=WARN)
    # the same schedule with the committed MO FIRST on its line is clean of it
    s2 = valid_schedule()
    s2.loc[0, ["start_hour", "end_hour"]] = [13.0, 33.0]     # O1 A after M1
    s2.loc[1, ["start_hour", "end_hour"]] = [35.0, 55.0]     # O2 B (A->B 1.5h)
    s2 = pd.concat([s2, pd.DataFrame([_row(0, "L1", "M1|CUR", "A", 0, 13, 13000)],
                                     columns=SCHED_COLS)], ignore_index=True)
    write_outputs(world, sched=s2)
    rep = validate_work_dir(world)
    assert rep.by_code("EARLY_BEFORE_COMMITTED") == []


def test_demand_bounds_from_schedule_and_pvb_and_demand_sum(world):
    s = valid_schedule()
    s.loc[4, ["end_hour", "run_hours", "qty_kg"]] = [30, 20, 10000]   # O1 total 30000 < 36000
    write_outputs(world, sched=s)                                     # pvb still says 36000
    rep = validate_work_dir(world)
    under = one(rep, "DEMAND_BOUNDS", order="O1", severity=ERROR)
    assert len(under) == 1 and under[0].detail.startswith("schedule: produced 30000 kg < qty_min 36000 (short 6000)")
    dsum = one(rep, "DEMAND_SUM", order="O1", severity=ERROR)[0]
    assert "sum(schedule qty_kg)=30000 vs produced_vs_bounds produced=36000" in dsum.detail
    # over qty_max is always an ERROR, even with soft_demand
    (world / "flowstate.toml").write_text(TOML.replace("soft_demand = false", "soft_demand = true"), encoding="utf-8")
    s = valid_schedule()
    s.loc[1, ["end_hour", "run_hours", "qty_kg"]] = [47, 25, 25000]   # O2 25000 > 22000
    pvb = valid_pvb(); pvb.loc[1, "produced"] = 25000
    write_outputs(world, sched=s, pvb=pvb)
    rep = validate_work_dir(world)
    over = one(rep, "DEMAND_BOUNDS", order="O2", severity=ERROR)[0]
    assert "schedule = produced_vs_bounds: produced 25000 kg > qty_max 22000 (excess 3000)" in over.detail
    # under qty_min with soft_demand is only a WARN
    s = valid_schedule()
    s.loc[4, ["end_hour", "run_hours", "qty_kg"]] = [30, 20, 10000]
    pvb = valid_pvb(); pvb.loc[0, "produced"] = 30000
    write_outputs(world, sched=s, pvb=pvb)
    rep = validate_work_dir(world)
    assert one(rep, "DEMAND_BOUNDS", order="O1", severity=WARN)
    assert rep.by_code("DEMAND_SUM") == []


def test_demand_sum_half_kilo_tolerance(world):
    pvb = valid_pvb(); pvb.loc[1, "produced"] = 20000.4
    write_outputs(world, pvb=pvb)
    assert validate_work_dir(world).by_code("DEMAND_SUM") == []
    pvb.loc[1, "produced"] = 20001
    write_outputs(world, pvb=pvb)
    rep = validate_work_dir(world)
    assert one(rep, "DEMAND_SUM", order="O2", severity=ERROR)[0].detail.endswith("(delta -1.0 kg > 0.5 kg)")
    assert codes(rep) == ["DEMAND_SUM"]


def test_bounds_definition_mismatch_is_reported(world):
    pvb = valid_pvb(); pvb.loc[0, "qty_min"] = 35000     # floor(40000*0.9) = 36000
    write_outputs(world, pvb=pvb)
    rep = validate_work_dir(world)
    assert one(rep, "DEMAND_BOUNDS", order="O1", severity=WARN)[0].detail.startswith("bounds definition differs")


def test_unknown_sku_and_line(world):
    s = valid_schedule()
    s.loc[3, "sku"] = "Z"
    s.loc[4, ["line_id", "line_name"]] = [7, "L9"]
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    assert one(rep, "UNKNOWN_SKU", line="L1", order="O3", severity=ERROR)
    assert one(rep, "UNKNOWN_LINE", line="L9", order="O1", severity=ERROR)[0].detail.startswith("line_id=7")


def test_duplicate_rows(world):
    s = valid_schedule()
    s = pd.concat([s, s.iloc[[1]]], ignore_index=True)
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = one(rep, "DUPLICATE_ROWS", line="L1", order="O2", severity=ERROR)[0]
    assert v.hours == 20.0
    assert one(rep, "OVERLAP", line="L1", order="O2")[0].hours == 20.0


def test_negative_or_zero(world):
    s = valid_schedule()
    s.loc[3, "qty_kg"] = 0
    s.loc[1, ["end_hour", "run_hours", "qty_kg"]] = [22, 0, 0]
    write_outputs(world, sched=s)
    rep = validate_work_dir(world)
    v = rep.by_code("NEGATIVE_OR_ZERO")
    assert {x.order for x in v} == {"O2", "O3"}
    assert any("end<=start" in x.detail for x in v) and any("run_hours=0" in x.detail for x in v)
    assert any(x.order == "O3" and "qty_kg=0" in x.detail for x in v)


def test_material_hook(world):
    def hook(block):
        if block.sku == "C":
            return {"severity": WARN, "hours": 2.5, "detail": "film short for C"}
        if block.order_id == "O2":
            return Violation("MATERIAL", ERROR, block.line_name, block.order_id, None, "no caps")
        return None
    rep = validate_work_dir(world, material=hook)
    assert one(rep, "MATERIAL", line="L1", order="O3", severity=WARN)[0].hours == 2.5
    assert one(rep, "MATERIAL", line="L1", order="O2", severity=ERROR)
    assert rep.ok is False
    assert any(c == "MATERIAL (hook returned 2 finding(s))" for c in rep.checks_run)


def test_mixed_cip_downtime_row_is_not_a_clean(world):
    # Scenario-F staging merges committed windows: 'Committed PRODUCTION 30142 + Committed CIP'
    # ending at the gate is NOT a clean ending there.
    build_world(world, downtimes=[(1, "L2", 0, 10, "Down"),
                                  (0, "L1", 200, 206, "Committed PRODUCTION 999 + Committed CIP")])
    rep = validate_work_dir(world)
    assert rep.stats["cleans_from_downtime_rows"] == 0
    assert rep.stats["downtime_rows_mixed_with_cip"] == 1
    assert rep.stats["cleans_total"] == 2


# ---------------------------------------------------------------------------
# Calendar mode (data/reference-shaped inputs)
# ---------------------------------------------------------------------------
CAL_COLS = ["block_id", "block_type", "line_id", "line_name", "start_h", "end_h", "label", "order_id",
            "sku", "sku_description", "qty_kg", "locked", "attrs"]
ANCHOR = datetime(2026, 9, 2, 0, 0, 0)


def build_reference(d: Path, *, prev_cip_l1="2026-08-31 00:00:00", carry_l1=0) -> Path:
    build_world(d, init=[(0, "L1", "A", 0, 0, 0, carry_l1, "", ""), (1, "L2", "CLEAN", 0, 0, 0, 0, "", "")],
                demand=[("O1", "A", 0, 40000, 0.9, 1.1, 0, 335, 3),
                        ("O2", "B", 0, 20000, 0.9, 1.1, 0, 335, 3)])
    (d / "downtimes.csv").write_text(
        "line_id,line_name,start_datetime,end_datetime,reason\n"
        "1,L2,2026-09-02 00:00,2026-09-02 10:00,Down\n", encoding="utf-8")
    (d / "cip_info.csv").write_text(
        "ID,LineEquipment,PreviousCIP,MaxHoursBetweenCIP,ScheduledCIP,Notes\n"
        f"1,L1,{prev_cip_l1},120,,\n"
        "2,L2,,144,,\n", encoding="utf-8")
    return d


def cal_block(bid, btype, lid, name, s, e, oid=None, sku=None, kg=None, locked=False, attrs=""):
    return (bid, btype, lid, name, s, e, sku or btype, oid, sku, "", kg, locked, attrs)


def valid_calendar() -> pd.DataFrame:
    return pd.DataFrame([
        cal_block("p1", "production", 0, "L1", 0.0, 26.0, "O1", "A", 26000.0),
        cal_block("c1", "cip", 0, "L1", 60.0, 66.0),                       # -48 -> 60: 108h ok
        cal_block("p2", "production", 0, "L1", 68.0, 88.0, "O2", "B", 20000.0),   # A->B 1.5 (gap 2 free)
        cal_block("p3", "production", 1, "L2", 22.0, 42.0, "O1", "A", 10000.0),   # O1 total 36000 = qty_min
        cal_block("d1", "line_down", 1, "L2", 0.0, 10.0, attrs="Down"),
    ], columns=CAL_COLS)


def test_calendar_valid(tmp_path):
    ref = build_reference(tmp_path / "ref")
    rep = validate_calendar(valid_calendar(), ref, ANCHOR, ref / "flowstate.toml")
    assert rep.violations == [], [v.as_line() for v in rep.violations]
    assert rep.stats["mode"] == "calendar" and rep.stats["tolerance_h"] == 1e-3
    assert rep.stats["blocks_by_kind"] == {"production": 3, "cip": 1, "line_down": 1}


def test_calendar_cip_interval_from_cip_info_previous_cip(tmp_path):
    ref = build_reference(tmp_path / "ref")
    cal = valid_calendar()
    cal = cal[cal.block_id != "c1"]          # no clean: -48 -> 88 = 136h > 120 (excess 16)
    rep = validate_calendar(cal, ref, ANCHOR, ref / "flowstate.toml")
    v = one(rep, "CIP_INTERVAL", line="L1", severity=ERROR)[0]
    assert v.hours == 16.0 and "cip_info.PreviousCIP" in v.detail
    # MaxHoursBetweenCIP comes from cip_info (L2 = 144) not line_cip_hrs (120)
    cal2 = pd.DataFrame([cal_block("p", "production", 1, "L2", 10.0, 140.0, "O1", "A", 65000.0)], columns=CAL_COLS)
    rep = validate_calendar(cal2, ref, ANCHOR, ref / "flowstate.toml")
    assert rep.by_code("CIP_INTERVAL") == []
    cal3 = pd.DataFrame([cal_block("p", "production", 1, "L2", 10.0, 146.0, "O1", "A", 68000.0)], columns=CAL_COLS)
    rep = validate_calendar(cal3, ref, ANCHOR, ref / "flowstate.toml")
    assert one(rep, "CIP_INTERVAL", line="L2")[0].hours == 2.0


def test_calendar_precision_does_not_fake_a_breach(tmp_path):
    # PreviousCIP 2026-08-31 19:55 = -28.0833..h; production end stored as 91.917 -> 120.0003h
    ref = build_reference(tmp_path / "ref", prev_cip_l1="2026-08-31 19:55:00")
    cal = pd.DataFrame([cal_block("p", "production", 0, "L1", 0.0, 91.917, "O1", "A", 91917.0)], columns=CAL_COLS)
    rep = validate_calendar(cal, ref, ANCHOR, ref / "flowstate.toml")
    assert rep.by_code("CIP_INTERVAL") == []


def test_calendar_downtime_wallclock_and_committed_rows(tmp_path):
    ref = build_reference(tmp_path / "ref")
    cal = valid_calendar()
    cal.loc[cal.block_id == "p3", "start_h"] = 5.0            # into 'Down' 00:00-10:00 -> 5h
    rep = validate_calendar(cal, ref, ANCHOR, ref / "flowstate.toml")
    v = one(rep, "IN_DOWNTIME", line="L2", order="O1", severity=ERROR)
    assert len(v) == 1 and v[0].hours == 5.0                    # csv + line_down block dedup to ONE window
    # committed / current_state rows: not capable -> reported as WARN only; qty mismatch -> WARN
    cal = valid_calendar()
    # committed C on L2 [10,20) ahead of O1 A [22,42): C->A 0.5h (no cip_req in that direction)
    cal = pd.concat([cal, pd.DataFrame([cal_block("cs", "production", 1, "L2", 10.0, 20.0, "30001", "C", 999.0,
                                                  locked=True, attrs="current_state:running;pct=50")],
                                       columns=CAL_COLS)], ignore_index=True)
    rep = validate_calendar(cal, ref, ANCHOR, ref / "flowstate.toml")
    assert one(rep, "LINE_NOT_CAPABLE", line="L2", order="30001", severity=WARN)
    assert one(rep, "QTY_RATE_MISMATCH", line="L2", order="30001", severity=WARN)
    assert rep.by_code("UNKNOWN_ORDER") == []                   # committed MO ids are not demand orders
    assert rep.ok is True


def test_calendar_cli(tmp_path, capsys):
    ref = build_reference(tmp_path / "ref")
    cal = valid_calendar()
    cal.to_csv(tmp_path / "cal.csv", index=False)
    rc = main(["--calendar", str(tmp_path / "cal.csv"), "--reference", str(ref),
               "--anchor", "2026-09-02 00:00:00", "--config", str(ref / "flowstate.toml")])
    assert rc == 0
    assert "mode: calendar" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The live Scenario-F solve (frozen snapshot): must RUN; findings are recorded.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not SNAPSHOT_F.exists(), reason="frozen F_workdir snapshot not present on this machine")
def test_live_scenario_f_snapshot_runs(tmp_path):
    work = tmp_path / "F"
    shutil.copytree(SNAPSHOT_F, work)
    rep = validate_work_dir(work)
    print(rep.summary(max_lines=200))
    assert rep.stats["blocks_by_kind"]["production"] == 84
    for name in iv.ALL_CHECKS:
        assert any(c.startswith(name) for c in rep.checks_run), name
    # stand-down staging: every line at 100000 -> interval check disabled but REPORTED
    note = next(c for c in rep.checks_run if c.startswith("CIP_INTERVAL"))
    assert "DISABLED" in note
    # soft_demand -> under-production is WARN, never ERROR
    assert all(v.severity == WARN for v in rep.by_code("DEMAND_BOUNDS"))
    # what this validator finds that validate_schedule.py does not (recorded, not judged here)
    counts = rep.counts()
    assert set(counts) >= {"CHANGEOVER_GAP", "DUE_WINDOW", "DEMAND_BOUNDS"}
    assert "OVERLAP" not in counts and "IN_DOWNTIME" not in counts and "QTY_RATE_MISMATCH" not in counts
