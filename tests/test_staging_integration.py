# tests/test_staging_integration.py -- Scenario F staging and the two-phase
# solver driver, end to end on synthetic dirs (fix agent T, 2026-09-03; audit
# tests-2: _overlay_fill, _prepare_work_dir, _run_two_phase and
# _solution_to_rows had 0 % coverage while carrying a P0).
#
# Part 1  scenario_runner._prepare_work_dir + _overlay_fill on a sandbox with
#         a synthetic manprg / cip_info / board, asserting the WORK-DIR files
#         field by field:
#           committed windows -> downtimes.csv     (C29 pure CIP rows, C11 outward rounding)
#           gates -> initial_states.csv + fill_gates.json
#           netted demand -> demand_plan.csv       (C56 bounds, C20 pro-rate, C54 hold-back)
#           projected CIP grid kept with a solver:cip_req block on the board (C41)
#           trials are blocked line time in the F path                     (staging-2)
#           pinned block re-based from a stale board                       (C55)
#           partial-week (horizon-clipped) target pro-rated                (C20)
# Part 2  phase2_scheduler.py --two-phase as a SUBPROCESS on the committed
#         solver_tiny fixture (tl 5 s, 2 workers): outputs exist, the
#         sub-phase Params are not dropped (C84), mo_changes.csv semantics.
#
# Every expected number is derived by hand in the comment next to it. The
# sandbox clock is frozen in 2027 so the overlay's real-clock now-floor stays
# inactive (same device as tests/test_fix_CB.py, whose sandbox geometry this
# reuses so the two files cross-check each other).

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from helpers import scenario_runner as sr  # noqa: E402
from helpers.calendar_io import CALENDAR_COLUMNS  # noqa: E402

PY = ROOT / ".venv" / "Scripts" / "python.exe"
SOLVER_SRC = ROOT / "code" / "solver" / "phase2_scheduler.py"
SOLVER_TINY = ROOT / "data" / "test_fixtures" / "solver_tiny"

# Clock: config anchor Mon 2027-03-01 (ISO 2027-W09); anchor_mode "today" and
# now = Tue 2027-03-02 03:00 -> staging anchor Tue 03-02 00:00, shift +24 h.
NOW = datetime(2027, 3, 2, 3, 0)

_TOML = textwrap.dedent("""\
    [scheduler]
    planning_start_date = "2027-03-01 00:00:00"
    anchor_mode = "today"
    horizon_weeks = 3
    horizon_hours = 504
    time_limit = 60
    min_run_hours = 4
    max_lines_per_order = 2
    use_current_mo = true
    reforecast_running_mo_ends = false

    [cip]
    interval_h = 120
    duration_h = 6
    """)

_MANPRG_HEADER = ("Start date;Start time;Line;MO No.;Item;Designation;Pal;Type;"
                  "Hours;Fct qty (Cas);Qty made (Cas);Fct qty [Kg];;Qty made [Kg];;Left (Cas)")


def _mp(date, time, line, mo, item, hours, fct, made, fct_kg, made_kg, left):
    return (f"{date};{time};LMH-{line};{mo};{item};{item} desc;640;GGS;{hours};"
            f"{fct};{made};{fct_kg};Kg;{made_kg};Kg;{left}")


def _blk(bid, line, s, e, sku, btype="production", attrs="", line_id=1,
         order_id="", qty=None):
    return {"block_id": bid, "block_type": btype, "line_id": line_id,
            "line_name": line, "start_h": float(s), "end_h": float(e),
            "label": sku, "order_id": order_id, "sku": sku, "sku_description": "",
            "qty_kg": qty, "locked": False, "attrs": attrs}


def make_sandbox(tmp_path: Path) -> Path:
    """<root>/flowstate.toml + <root>/plant/{lines.csv, calendar_blocks.csv,
    reference/*}. Returns the data dir (deliberately not named 'data')."""
    root = tmp_path / "sandbox"
    dd = root / "plant"
    ref = dd / "reference"
    ref.mkdir(parents=True)
    (root / "flowstate.toml").write_text(_TOML, encoding="utf-8")
    pd.DataFrame([{"line_id": 0, "line_name": "P09", "active": True},
                  {"line_id": 1, "line_name": "P10", "active": True},
                  {"line_id": 3, "line_name": "P12", "active": True}]
                 ).to_csv(dd / "lines.csv", index=False)
    rows = [
        # P09 running MO 110: start Mon 20:00, 20.5 h nominal -> ends Tue
        # 16:30 = staging hour 16.5 (reforecast off)
        _mp("03/01/2027", "20:00", "P09", "30001", "110", 20.5, 1000, 500, 8000.0, 4000.0, 500),
        # P09 queued 111: Tue 17:00 for 20 h -> [17, 37], 10,000 kg
        _mp("03/02/2027", "17:00", "P09", "30003", "111", 20.0, 1000, "", 10000.0, "", 1000),
        # P12 queued 222: nominal Tue 00:00, cursor = now 03:00 -> [3, 23], 5,000 kg
        _mp("03/02/2027", "00:00", "P12", "30002", "222", 20.0, 1000, "", 5000.0, "", 1000),
        # P10 TRIAL Tue 08:00 for 4 h -> [8, 12]: blocked line time, no kg (staging-2)
        _mp("03/02/2027", "08:00", "P10", "30077", "TRIALS", 4.0, 1, "", 0.0, "", 1),
        # P09 completed 777 in W08 (Wed 02-24): 7,000 kg made, NO W08 demand row (C54)
        _mp("02/24/2027", "10:00", "P09", "30000", "777", 10.0, 500, 500, 7000.0, 7000.0, 0),
    ]
    (ref / "manprg.txt").write_text("\n".join([_MANPRG_HEADER, *rows]) + "\n", encoding="cp1252")
    # PreviousCIP: P09 Sun 02-28 00:00 = staging hour -48; P12 Mon 03-01 = -24
    (ref / "cip_info.csv").write_text(textwrap.dedent("""\
        ID,LineEquipment,PreviousCIP,MaxHoursBetweenCIP,ScheduledCIP,Notes
        1,P09,2027-02-28 00:00:00,120,,
        2,P10,,144,,
        3,P12,2027-03-01 00:00:00,120,,
        """), encoding="utf-8")
    dem = pd.DataFrame([{
        "order_id": o, "sku": s, "week_index": w, "qty_target": t,
        "lower_pct": 0.9, "upper_pct": 1.1,
        "due_start_hour": 168 * w, "due_end_hour": 168 * w + 167, "priority": 3,
    } for o, s, w, t in [("111-W0", "111", 0, 20000), ("222-W0", "222", 0, 5000),
                         ("333-W1", "333", 1, 8000), ("444-W3", "444", 3, 1_715_000),
                         ("555-W0", "555", 0, 3000), ("777-W0", "777", 0, 9000)]])
    dem.to_csv(ref / "demand_plan.csv", index=False)
    (ref / "demand_plan.source.json").write_text(json.dumps(
        {"source": "demand_plan_summary.csv", "anchor": "2027-03-01 00:00:00"}), encoding="utf-8")
    pd.DataFrame([{"line_id": 1, "line_name": "P10", "start_datetime": "2027-03-02 10:30",
                   "end_datetime": "2027-03-02 20:30", "reason": "Down"}]
                 ).to_csv(ref / "downtimes.csv", index=False)
    skus = ["110", "111", "222", "333", "444", "555", "777"]
    pd.DataFrame([{"line_id": lid, "sku": s, "line_name": ln, "capable": 1, "calc_rate_kgph": 500.0}
                  for lid, ln in ((0, "P09"), (1, "P10"), (3, "P12")) for s in skus]
                 ).to_csv(ref / "capabilities_rates.csv", index=False)
    pd.DataFrame([{"line_id": lid, "Line": ln, "rate_kgph": 500}
                  for lid, ln in ((0, "P09"), (1, "P10"), (3, "P12"))]
                 ).to_csv(ref / "line_rates.csv", index=False)
    pd.DataFrame([{"from_sku": "111", "to_sku": "222", "setup_hours": 1, "ttp_change": 0,
                   "ffs_change": 0, "tpld_change": 0, "cspkr_change": 0, "conv_to_org": 0,
                   "cinn_to_non_cinn": 0, "added_flavors": 0, "cip_req_after": 0}]
                 ).to_csv(ref / "changeovers.csv", index=False)
    pd.DataFrame([{"sku": s, "designation": s} for s in skus]).to_csv(ref / "sku_info.csv", index=False)
    pd.DataFrame([{"line_id": 0, "line_name": "P09", "max_cip_hrs": 120},
                  {"line_id": 1, "line_name": "P10", "max_cip_hrs": 144},
                  {"line_id": 3, "line_name": "P12", "max_cip_hrs": 120}]
                 ).to_csv(ref / "line_cip_hrs.csv", index=False)
    pd.DataFrame([{"line_id": lid, "line_name": ln, "initial_sku": "999", "available_from_hour": 0,
                   "long_shutdown_flag": 1, "long_shutdown_extra_setup_hours": 2,
                   "carryover_run_hours_since_last_cip_at_t0": 50}
                  for lid, ln in ((0, "P09"), (1, "P10"), (3, "P12"))]
                 ).to_csv(ref / "initial_states.csv", index=False)
    # Board stored in the CONFIG frame (Mon 03-01): every hour is 24 h "late"
    board = pd.DataFrame([
        # the clean this pipeline drew LAST run -- not the planner's (C41)
        _blk("cip_solver", "P12", 236, 242, "CIP", btype="cip", attrs="solver:cip_req", line_id=3),
        # a genuine planner CIP on P10 -> replaces P10's projected grid
        _blk("cip_planner", "P10", 200, 206, "CIP", btype="cip", attrs="planner:cip", line_id=1),
        # planner-pinned demand block, 5,000 kg of 333-W1 (C55 re-base -24 h)
        _blk("pin_1", "P09", 160, 180, "333", attrs="pinned", line_id=0, order_id="333-W1", qty=5000.0),
        # a pin that is fully in the past after the re-base -> dropped
        _blk("pin_old", "P09", -10, 10, "333", attrs="pinned", line_id=0, order_id="333-W1", qty=5000.0),
    ], columns=CALENDAR_COLUMNS)
    board.to_csv(dd / "calendar_blocks.csv", index=False)
    return dd


@pytest.fixture
def frozen_clock(monkeypatch):
    from helpers import horizon as hzmod
    real = hzmod.resolve

    def _resolve(cfg=None, now=None):
        return real(cfg, now=NOW)

    monkeypatch.setattr(hzmod, "resolve", _resolve)


@pytest.fixture
def staged(tmp_path, frozen_clock):
    dd = make_sandbox(tmp_path)
    work = dd / "_scenario_work" / "F"
    sr._prepare_work_dir(dd, work)
    notes = sr._overlay_fill(work, dd)
    return {"dd": dd, "work": work, "notes": notes, "log": "\n".join(notes)}


def _spans(dt: pd.DataFrame, line: str) -> list[tuple[int, int, str]]:
    return sorted((int(r.start_hour), int(r.end_hour), str(r.reason))
                  for r in dt.itertuples() if r.line_name == line)


def _covers(spans, lo, hi) -> bool:
    """True when the union of [s,e) spans covers [lo,hi)."""
    cur = lo
    for s, e, *_r in sorted(spans):
        if s <= cur < e:
            cur = max(cur, e)
    return cur >= hi


# ---------------------------------------------------------------------------
# Part 1 -- Scenario F staging
# ---------------------------------------------------------------------------
def test_prepare_work_dir_copies_the_solver_inputs_from_reference(staged):
    work = staged["work"]
    for name in ("capabilities_rates.csv", "changeovers.csv", "demand_plan.csv",
                 "downtimes.csv", "initial_states.csv", "line_cip_hrs.csv",
                 "sku_info.csv", "flowstate.toml"):
        assert (work / name).exists(), name
    # F never hands the plant's MOs to the solver as orders
    assert not (work / "current_mo.csv").exists()


def test_committed_windows_become_fixed_downtime_rows(staged):
    """P09: running 30001 ends 16.5 -> the queued 30003 [17,37] follows;
    both are committed production -> one blocked window [0, 37]. Projected
    cleans (current_state.project_cips) step one interval START-to-start
    from the last known clean: PreviousCIP Sun 02-28 00:00 = staging hour -48
    + k x 120 -> 72, 192, 312, 432 (each 6 h; the plant's grid is therefore
    one clean-duration conservative against an end-to-start reading), as PURE
    'Committed CIP' rows (C29). No two rows overlap."""
    dt = pd.read_csv(staged["work"] / "downtimes.csv")
    p09 = _spans(dt, "P09")
    assert _covers(p09, 0, 37), p09
    cips = [(s, e) for s, e, r in p09 if r == "Committed CIP"]
    assert cips == [(72, 78), (192, 198), (312, 318), (432, 438)], cips
    assert all(e - s == 6 for s, e in cips)
    for ln in ("P09", "P10", "P12"):
        sp = _spans(dt, ln)
        assert all(b[0] >= a[1] for a, b in zip(sp, sp[1:])), (ln, sp)
    # real downtimes are kept aside for the proposal calendar
    assert (staged["work"] / "real_downtimes.csv").exists()


def test_downtime_edges_round_outward_and_trial_is_blocked_time(staged):
    """C11: P10 line-down 10:30-20:30 -> [10, 21] (floor / ceil, never int()).
    staging-2: the P10 TRIAL [8,12] is COMMITTED line time in the F path --
    it appears in committed_blocks.csv as block_type 'trial' and the blocked
    union on P10 covers [8, 21] (trial + line-down)."""
    work = staged["work"]
    dt = pd.read_csv(work / "downtimes.csv")
    p10 = _spans(dt, "P10")
    assert _covers(p10, 8, 21), p10
    assert not _covers(p10, 7, 8) and not _covers(p10, 21, 22), p10
    committed = pd.read_csv(work / "committed_blocks.csv", dtype=str, keep_default_na=False)
    trial = committed[(committed["line_name"] == "P10") & (committed["block_type"] == "trial")]
    assert len(trial) == 1, committed[committed["line_name"] == "P10"]
    assert (float(trial.iloc[0]["start_h"]), float(trial.iloc[0]["end_h"])) == (8.0, 12.0)
    # the planner CIP re-based 200-24 = 176 -> [176, 182] as a pure CIP row
    assert (176, 182, "Committed CIP") in p10, p10


def test_gates_are_the_committed_tails(staged):
    """P09 tail 37 (queued MO end); P10 12 (trial end -- the line-down is not
    committed work, gates read committed blocks only); P12 23 (queued MO
    [3,23]). initial_sku = last committed SKU (P09 111, P12 222); P10 has no
    committed PRODUCTION -> CLEAN with the fixture's long-shutdown fields
    zeroed (staging-4). Carry-over 0 everywhere (layer-1 CIPs rule)."""
    work = staged["work"]
    init = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str}).set_index("line_name")
    assert int(init.loc["P09", "available_from_hour"]) == 37
    assert int(init.loc["P10", "available_from_hour"]) == 12
    assert int(init.loc["P12", "available_from_hour"]) == 23
    assert init.loc["P09", "initial_sku"] == "111"
    assert init.loc["P12", "initial_sku"] == "222"
    assert init.loc["P10", "initial_sku"] == "CLEAN"
    assert int(init.loc["P10", "long_shutdown_flag"]) == 0
    assert int(init.loc["P10", "long_shutdown_extra_setup_hours"]) == 0
    assert (init["carryover_run_hours_since_last_cip_at_t0"] == 0).all()
    gates = json.loads((work / "fill_gates.json").read_text(encoding="utf-8"))["gates"]
    assert gates == {"P09": 37.0, "P10": 12.0, "P12": 23.0}


def test_solver_cip_generation_stands_down_in_f(staged):
    ch = pd.read_csv(staged["work"] / "line_cip_hrs.csv")
    assert (ch["max_cip_hrs"] == 100000).all()
    assert "stood down" in staged["log"]


def test_c41_solver_drawn_clean_does_not_delete_the_projected_grid(staged):
    """The board carries the clean THIS pipeline drew last run on P12
    (attrs solver:cip_req). It is not the planner's re-forecast, so P12 keeps
    its projected grid: PreviousCIP Mon 03-01 00:00 = staging hour -24, 120 h
    interval start-to-start -> cleans at 96, 216, 336, 456. Only P10
    (planner:cip) has its grid replaced. No solver:* token survives into the
    committed blocks and the CIP CHECK stays silent."""
    work, log = staged["work"], staged["log"]
    committed = pd.read_csv(work / "committed_blocks.csv", dtype=str, keep_default_na=False)
    p12 = committed[(committed["line_name"] == "P12") & (committed["block_type"] == "cip")]
    starts = sorted(float(x) for x in p12["start_h"])
    assert starts == [96.0, 216.0, 336.0, 456.0], starts
    assert p12["attrs"].str.contains("current_state:cip_projected").all()
    assert not committed["attrs"].str.contains("solver:").any()
    assert "1 planner CIP(s) held FIXED" in log and "P10" in log
    assert "CIP CHECK" not in log


def test_c55_stale_board_is_rebased_and_past_pins_dropped(staged):
    """Board frame Mon 03-01, staging frame Tue 03-02 -> shift +24 h: pin_1
    160-180 -> 136-156 (same wall clock, Sun 03-07 16:00 .. Mon 03-08 12:00);
    pin_old -10..10 -> -34..-14 is fully past -> dropped."""
    work, log = staged["work"], staged["log"]
    committed = pd.read_csv(work / "committed_blocks.csv", dtype=str, keep_default_na=False)
    pin = committed[committed["block_id"] == "pin_1"]
    assert len(pin) == 1
    assert (float(pin.iloc[0]["start_h"]), float(pin.iloc[0]["end_h"])) == (136.0, 156.0)
    assert not (committed["block_id"] == "pin_old").any()
    assert "board re-based +24h" in log and "1 fully-past block(s) dropped" in log
    dt = pd.read_csv(work / "downtimes.csv")
    assert (136, 156, "Committed PRODUCTION 333-W1") in _spans(dt, "P09")


def test_netted_demand_bounds_prorate_and_holdback(staged):
    """Netting (staging frame, ISO weeks: W09 = hours [0,144), W10 = [144,312)):
      111-W0 gross 20,000; queued 30003 makes 10,000 in W09 -> residual
        10,000; C56 explicit bounds = 0.9x20,000-10,000 = 8,000 /
        1.1x20,000-10,000 = 12,000; pct blanked, credit_kg 10,000.
      222-W0 gross 5,000; 30002 makes 5,000 -> residual 0 -> DROPPED.
      333-W1 gross 8,000; pin_1 [136,156] = 8 h in W09 (2,000 kg) + 12 h in
        W10 (3,000 kg); W09 has no 333 demand so the 2,000 carries -> 5,000
        applied -> residual 3,000; bounds 7,200-5,000 = 2,200 / 8,800-5,000 = 3,800.
      444-W3 raw window [504,671] -> re-based [480,647] -> clipped to the 504 h
        horizon: 24 of 168 h -> 1,715,000 x 24/168 = 245,000 (C20), 1,470,000
        deferred, window [480,503], pct kept (no credit).
      555-W0 3,000 untouched (no supply).
      777-W0 9,000 stays 9,000: the 7,000 kg made on Wed 02-24 (W08) has no
        W08 demand row and is NOT carried forward (C54) -- WARNING in the log."""
    work, log = staged["work"], staged["log"]
    dem = pd.read_csv(work / "demand_plan.csv", dtype={"sku": str}).set_index("order_id")
    assert set(dem.index) == {"111-W0", "333-W1", "444-W3", "555-W0", "777-W0"}
    r = dem.loc["111-W0"]
    assert r["qty_target"] == 10000.0
    assert (r["qty_min"], r["qty_max"]) == (8000.0, 12000.0)
    assert pd.isna(r["lower_pct"]) and r["credit_kg"] == 10000.0
    r = dem.loc["333-W1"]
    assert r["qty_target"] == 3000.0
    assert (r["qty_min"], r["qty_max"]) == (2200.0, 3800.0)
    r = dem.loc["444-W3"]
    assert r["qty_target"] == 245000.0
    assert (r["due_start_hour"], r["due_end_hour"]) == (480.0, 503.0)
    assert (r["lower_pct"], r["upper_pct"]) == (0.9, 1.1)
    assert dem.loc["555-W0", "qty_target"] == 3000.0
    assert dem.loc["777-W0", "qty_target"] == 9000.0
    assert "WARNING: 7,000 kg of past production" in log
    assert "1 fully-covered order(s) dropped" in log and "222-W0" in log
    assert "1,470,000 kg DEFERRED" in log
    # gross = applied + net on every ledger row (netting invariant)
    ledger = pd.read_csv(work / "coverage_ledger.csv")
    assert ((ledger["gross_kg"] - ledger["applied_kg"] - ledger["net_kg"]).abs() < 0.11).all()


def test_staging_anchor_is_written_where_the_solver_reads_it(staged):
    """C21: [scheduler].planning_start_date in the WORK toml is the staging
    anchor (Tue 2027-03-02), which phase2_scheduler.params_from_config reads."""
    import tomllib

    import phase2_scheduler as p2
    cfg = tomllib.loads((staged["work"] / "flowstate.toml").read_text(encoding="utf-8"))
    assert cfg["scheduler"]["planning_start_date"] == "2027-03-02 00:00:00"
    assert p2.params_from_config(cfg).planning_start_date == "2027-03-02 00:00:00"


def test_staged_work_dir_loads_and_builds_in_the_solver(staged):
    """The staged dir must be a valid solver input: data_loader loads it and
    build_model builds (no solve -- CPU discipline). Every committed window
    reaches the model as a downtime; the fill orders are the netted ones."""
    import tomllib

    import phase2_scheduler as p2
    from data_loader import Data, Files
    from model_builder import build_model

    work = staged["work"]
    cfg = tomllib.loads((work / "flowstate.toml").read_text(encoding="utf-8"))
    P = p2.params_from_config(cfg)
    data = Data(P, Files(work))
    data.load()
    assert {o["order_id"] for o in data.orders} == {"111-W0", "333-W1", "444-W3", "555-W0", "777-W0"}
    assert not any(o.get("is_current_mo") for o in data.orders)
    assert data.init_map[0]["available_from"] == 37 and data.init_map[0]["initial_sku"] == "111"
    assert len(data.downtimes) == len(pd.read_csv(work / "downtimes.csv"))
    model, _v = build_model(P, data, "full", False, False, maximize_production=True)
    assert len(model.Proto().variables) > 0


# ---------------------------------------------------------------------------
# Part 2 -- the two-phase driver as a subprocess on the committed fixture
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def solver_w2(tmp_path_factory) -> Path:
    """phase2_scheduler.py with the 8-worker portfolio cut to 2 (CPU discipline)."""
    src = SOLVER_SRC.read_text(encoding="utf-8")
    needle = "SEARCH_WORKERS = 1 if DETERMINISTIC else 8\n"
    assert src.count(needle) == 1, "worker constant moved -- update the test"
    dst = tmp_path_factory.mktemp("solver") / "phase2_scheduler_w2.py"
    dst.write_text(src.replace(needle, "SEARCH_WORKERS = 1 if DETERMINISTIC else 2\n"),
                   encoding="utf-8")
    return dst


@pytest.fixture(scope="module")
def two_phase_run(tmp_path_factory, solver_w2) -> dict:
    """ONE subprocess run of --two-phase on a copy of solver_tiny with unusual
    Params so the C84 check can see them travel: min_run_hours 3, seed 11,
    use_sku_rates true (already), time_limit 5."""
    if not PY.exists():
        pytest.fail(f"repo venv python not found at {PY}")
    d = tmp_path_factory.mktemp("two_phase") / "d"
    d.mkdir()
    for p in SOLVER_TINY.iterdir():
        if p.suffix in (".csv", ".toml"):
            (d / p.name).write_bytes(p.read_bytes())
    toml = (d / "flowstate.toml").read_text(encoding="utf-8")
    toml = toml.replace("time_limit = 15", "time_limit = 5")
    toml = toml.replace("min_run_hours = 4", "min_run_hours = 3")
    toml = toml.replace("solver_random_seed = 7", "solver_random_seed = 11")
    (d / "flowstate.toml").write_text(toml, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "code" / "solver"), str(ROOT / "code")])
    cmd = [str(PY), str(solver_w2), "--data-dir", str(d), "--config", str(d / "flowstate.toml"),
           "--two-phase"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    return {"dir": d, "proc": proc,
            "log": (d / "solver_error.txt").read_text(encoding="utf-8")
            if (d / "solver_error.txt").exists() else ""}


def test_two_phase_produces_every_output(two_phase_run):
    d, proc = two_phase_run["dir"], two_phase_run["proc"]
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:] + two_phase_run["log"][-2000:]
    for name in ("schedule_phase2.csv", "feasibility_report.json", "produced_vs_bounds.csv",
                 "mo_changes.csv", "week1_initial_states.csv"):
        assert (d / name).exists(), f"{name} missing after --two-phase"
    rep = json.loads((d / "feasibility_report.json").read_text(encoding="utf-8"))
    assert rep["status"] in ("FEASIBLE", "OPTIMAL"), rep
    # cip_windows.csv is written only when a clean was placed. Without one,
    # no line may have run past its wall-clock deadline: L1 carry 60 of 120
    # -> production must end by h60; L2 100; L3 120.
    if not (d / "cip_windows.csv").exists():
        sched = pd.read_csv(d / "schedule_phase2.csv")
        deadline = {"L1": 60, "L2": 100, "L3": 120}
        for ln, grp in sched.groupby("line_name"):
            assert float(grp["end_hour"].max()) <= deadline[ln], (ln, grp["end_hour"].max())


def test_two_phase_params_are_not_dropped_c84(two_phase_run):
    """C84: the sub-phase Params must carry every toml field, not just the
    horizon. use_sku_rates=True, seed 11 and min_run_hours 3 come from the
    run's toml; soft_demand False is the fixture's setting."""
    log = two_phase_run["log"]
    assert ("[two-phase] sub-phase params: use_sku_rates=True soft_demand=False "
            "seed=11 min_run_hours=3") in log, log[:3000]


def test_two_phase_week1_orders_follow_the_week0_tail_and_the_mo_stays_locked(two_phase_run):
    """The two-phase driver's DOCUMENTED contract (phase2_scheduler._run_two_phase
    docstring + line ~1866 `o["due_start"] = 0`): week-1 orders run in the
    second phase and may start as soon as each line finishes its week-0 work
    -- NOT at hour 168. So per line: every W1 block starts at or after the
    last W0/MO block end on that line. (Observation for the report, not a
    verified finding here: this pulls every week-1 order into week 0 whenever
    lines are free, and the independent validator then reports one
    DUE_WINDOW error per W1 block on every two-phase run.) The current MO
    (A, locked to L1) is placed on L1 within its bracket 12,000..13,000 kg."""
    sched = pd.read_csv(two_phase_run["dir"] / "schedule_phase2.csv")
    w1_ids = {"A-W1", "B-W1", "D-W1"}
    w1 = sched[sched["order_id"].isin(w1_ids)]
    w0 = sched[~sched["order_id"].isin(w1_ids)]
    assert len(w1) > 0 and len(w0) > 0
    for ln, grp in w1.groupby("line_name"):
        tail = float(w0[w0["line_name"] == ln]["end_hour"].max()) if (w0["line_name"] == ln).any() else 0.0
        assert float(grp["start_hour"].min()) >= tail, (ln, tail, grp[["order_id", "start_hour"]])
    mo = sched[sched["order_id"] == "MO1|CUR"]
    assert len(mo) >= 1 and (mo["line_name"] == "L1").all()
    assert int(mo["qty_kg"].sum()) in (12000, 13000)
    log = two_phase_run["log"]
    assert "[two-phase] Phase 1: Week-0 only (5 orders)" in log      # 4 W0 demand orders + MO1
    assert "[two-phase] Phase 2: Week-1 (3 orders)" in log
    rates = {"L1": 1000, "L2": 800, "L3": 500}
    for r in sched.itertuples():
        assert abs(float(r.qty_kg) - rates[r.line_name] * float(r.run_hours)) < 0.5 + float(r.run_hours), r


def test_two_phase_mo_changes_has_the_full_column_contract(two_phase_run):
    """mo_changes.csv carries the 19-column contract (V fix 10 / writeback-5..7)
    in order, and every non-dropped row has wall-clock start/end plus the
    planning anchor the hours refer to. (orchestration-10 -- not a verified
    item here -- the two-phase writer receives the week-1 order list, so the
    MO row is ABSENT on this run (header-only file); reported in
    scratchpad/fixes/T/CHANGES.md, not asserted. The single-phase semantics
    are pinned in tests/test_fix_T.py::test_t2_mo_changes_semantics_on_the_fixture.)"""
    import phase2_scheduler as p2
    df = pd.read_csv(two_phase_run["dir"] / "mo_changes.csv", dtype=str, keep_default_na=False)
    assert list(df.columns) == p2.MO_CHANGES_COLUMNS
    for r in df.itertuples():
        if r.reason == "dropped":
            assert r.new_start_h == "" and r.new_end_h == "" and r.piece == ""
        else:
            assert r.new_start_dt and r.new_end_dt and r.planning_anchor == "2026-09-07 00:00:00"
            assert r.reason in ("tonnage_trim", "tonnage_fill", "split", "reordered", "unmoved") or \
                "+" in r.reason, r.reason
