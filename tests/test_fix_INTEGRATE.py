# tests/test_fix_INTEGRATE.py — regression tests for the cross-file HANDOFF
# items the fix agents could not apply themselves (integrator, 2026-09-03).
# Every expected value is derived by hand in the test's docstring/comments.
#
#   I-1  independent_validator DUE_WINDOW early-start rule (agent SB handoff ->
#        validator owner). Re-derived 2026-09-04 for the plant's early-fill
#        policy: [scheduler] early_fill_hours absent/"unbounded" -> an early
#        start is an informational WARN, never an ERROR; an integer -> WARN
#        inside the allowance, ERROR beyond it (every week, not only the
#        second); allow_week1_in_week0 = false -> ERROR.
#   I-2  phase2_scheduler two-phase week boundary from the demand frame
#        (SB handoff V-3) + Params.legacy_week_stitch (SB handoff SA-1 / V-2)
#   I-3  model_builder announces setup_times_enforced; level-3 label follows
#        (V NOT DONE / SA handoff)
#   I-4  model warnings surfaced (SB handoff V-1)
#   I-5  demand_coverage made_part credit rule (CA handoff -> CB)
#   I-6  calendar_io: duplicate block ids refused (FE handoff W-3),
#        cip_req_after flag column (FE W-1), board_supply_summary no-recipe /
#        unknown-kg rules (FE W-2)
#   I-7  holding_builder mean capable rate ignores 0 rates (FE handoff)
#   I-8  data_loader manprg_start_h passthrough (V handoff SA) and
#        scenario_runner writing it (V handoff CB)
#   I-9  scenario_runner fill_ledger.json + save_scenario_version metadata
#        (V handoff C-1/C-2, Q handoff)
#   I-10 source-level guards: pages use build_demand_targets (Q handoff),
#        demand_anchor payload key (FE W-4), caps passed to the caption
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from dataclasses import fields
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "code", ROOT / "code" / "solver", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from data_loader import Data, Files, Params  # noqa: E402
import model_builder as mb  # noqa: E402
import phase2_scheduler as p2  # noqa: E402
import independent_validator as iv  # noqa: E402
from independent_validator import ERROR, WARN, validate_work_dir  # noqa: E402
from helpers import calendar_io as cio  # noqa: E402
from helpers import scenario_runner as sr  # noqa: E402
from helpers.demand_coverage import keep_completed_row  # noqa: E402
from helpers.holding_builder import average_rate_per_sku  # noqa: E402

CODE = ROOT / "code"


def _load_test_module(name: str):
    """Import a sibling test module by path (tests/ is not a package)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════════════════════════
# I-1  validator DUE_WINDOW rule on a Wednesday-anchored (live F) frame
# ═══════════════════════════════════════════════════════════════════════════
def _wed_world(tmp_path: Path, sched_rows) -> Path:
    """Live-F shaped frame: W0 [0,119], W1 [120,287], W2 [288,455], 504 h.
    Distinct starts [0, 120, 288] -> second_start 120 -> W1 may open at 72."""
    tiv = _load_test_module("test_independent_validator")
    toml = tiv.TOML.replace("horizon_hours = 336", "horizon_hours = 504")
    d = tiv.build_world(tmp_path / "w", toml=toml, demand=[
        ("W0-A", "A", 0, 20000, 0.9, 1.1, 0, 119, 3),
        ("W1-A", "A", 1, 20000, 0.9, 1.1, 120, 287, 3),
        ("W2-A", "A", 2, 20000, 0.9, 1.1, 288, 455, 3),
    ], downtimes=[(1, "L2", 0, 10, "Down")], line_cip=[(0, "L1", 100000), (1, "L2", 100000)])
    sched = pd.DataFrame([tiv._row(*r) for r in sched_rows], columns=tiv.SCHED_COLS)
    cips = pd.DataFrame(columns=["line_id", "line_name", "start_hour", "end_hour"])
    pvb = pd.DataFrame(columns=["order_id", "sku", "qty_min", "qty_max", "produced", "in_bounds"])
    tiv.write_outputs(d, sched=sched, cips=cips, pvb=pvb)
    return d


def _due(rep, order):
    return [v for v in rep.by_code("DUE_WINDOW") if v.order == order]


def _set_early_fill(d: Path, value: str) -> None:
    """Write [scheduler] early_fill_hours = <value>, replacing an earlier one."""
    import re
    toml = (d / "flowstate.toml").read_text(encoding="utf-8")
    toml = re.sub(r"^early_fill_hours = .*$" + chr(10), "", toml, flags=re.M)
    (d / "flowstate.toml").write_text(
        toml.replace("[scheduler]", "[scheduler]" + chr(10) + f"early_fill_hours = {value}"),
        encoding="utf-8")


def test_second_demand_week_start_by_hand():
    """[0,120,288] -> 120; Monday frame [0,168,336] -> 168; one week -> None.
    LEGACY helper since the plant decision of 2026-09-04 (the DUE_WINDOW
    check no longer privileges the second week) — kept, with the documented
    legacy constant 48 in both modules."""
    O = iv.Order
    mk = lambda starts: {f"o{i}": O(f"o{i}", "A", s, s + 119, 0, 1, 1)  # noqa: E731
                         for i, s in enumerate(starts)}
    assert iv.second_demand_week_start(mk([0, 120, 288, 120])) == 120.0
    assert iv.second_demand_week_start(mk([0, 168, 336])) == 168.0
    assert iv.second_demand_week_start(mk([0, 0])) is None
    assert iv.second_demand_week_start({}) is None
    assert iv.EARLY_FILL_H == 48.0 == mb.EARLY_FILL_HOURS


def test_validator_due_window_unbounded_policy_never_errors_on_an_early_start(tmp_path):
    """Plant decision 2026-09-04: the toml has no early_fill_hours key ->
    unbounded. Hand:
      W1-A at 72  -> early 48  -> WARN "early fill (policy: unbounded)"
      W2-A at 130 -> early 158 -> WARN too (under fix SB-1 this was an ERROR:
                     only the second week could start early)
      W0-A at 0   -> nothing.
    Both blocks start after their line's gate (L1 0, L2 10) and over no
    committed block, so the report carries no DUE_WINDOW ERROR at all."""
    d = _wed_world(tmp_path, [
        (0, "L1", "W0-A", "A", 0, 20, 20000),
        (0, "L1", "W1-A", "A", 72, 92, 20000),
        (1, "L2", "W2-A", "A", 130, 170, 20000),
    ])
    rep = validate_work_dir(d)
    assert rep.stats["early_fill_hours"] == "unbounded"
    w1 = _due(rep, "W1-A")
    assert len(w1) == 1 and w1[0].severity == WARN and w1[0].hours == 48.0
    assert "early fill (policy: unbounded)" in w1[0].detail
    w2 = _due(rep, "W2-A")
    assert len(w2) == 1 and w2[0].severity == WARN and w2[0].hours == 158.0
    assert "early fill (policy: unbounded)" in w2[0].detail
    assert _due(rep, "W0-A") == []
    assert not [v for v in rep.by_code("DUE_WINDOW") if v.severity == ERROR]
    assert any(c.startswith("DUE_WINDOW") and "unbounded" in c for c in rep.checks_run)


def test_validator_due_window_bounded_allowance_applies_to_every_week(tmp_path):
    """early_fill_hours = 48 in the toml (the pre-decision value, now for
    EVERY later week). Hand:
      W1-A at 72  -> early 48 = the edge -> WARN (inside the allowance, from 72h)
      W1-A at 71  -> early 49 > 48        -> ERROR (legal start >= 72)
      W2-A at 240 -> early 48             -> WARN (SB-1 called the third week an ERROR)
      W2-A at 130 -> early 158 > 48       -> ERROR."""
    d = _wed_world(tmp_path, [
        (0, "L1", "W0-A", "A", 0, 20, 20000),
        (0, "L1", "W1-A", "A", 72, 92, 20000),
        (1, "L2", "W2-A", "A", 240, 280, 20000),
    ])
    _set_early_fill(d, "48")
    rep = validate_work_dir(d)
    assert rep.stats["early_fill_hours"] == 48.0
    w1 = _due(rep, "W1-A")
    assert len(w1) == 1 and w1[0].severity == WARN and w1[0].hours == 48.0
    assert "early_fill_hours = 48h" in w1[0].detail and "from 72h" in w1[0].detail
    w2 = _due(rep, "W2-A")
    assert len(w2) == 1 and w2[0].severity == WARN and w2[0].hours == 48.0

    d = _wed_world(tmp_path / "b", [
        (0, "L1", "W0-A", "A", 0, 20, 20000),
        (0, "L1", "W1-A", "A", 71, 91, 20000),
        (1, "L2", "W2-A", "A", 130, 170, 20000),
    ])
    _set_early_fill(d, "48")
    rep = validate_work_dir(d)
    w1 = _due(rep, "W1-A")
    assert len(w1) == 1 and w1[0].severity == ERROR and w1[0].hours == 49.0
    assert "beyond the early-fill allowance" in w1[0].detail
    w2 = _due(rep, "W2-A")
    assert len(w2) == 1 and w2[0].severity == ERROR and w2[0].hours == 158.0


def test_validator_early_fill_key_parses_like_the_solver(tmp_path):
    """"unbounded" / "none" -> None; 48 / "48" -> 48.0; a typo raises."""
    assert iv.parse_early_fill_hours("unbounded") is None
    assert iv.parse_early_fill_hours("none") is None
    assert iv.parse_early_fill_hours(None) is None
    assert iv.parse_early_fill_hours(48) == 48.0 == iv.parse_early_fill_hours("48")
    assert iv.parse_early_fill_hours(0) == 0.0
    with pytest.raises(ValueError):
        iv.parse_early_fill_hours("soon")
    with pytest.raises(ValueError):
        iv.parse_early_fill_hours(-1)
    d = _wed_world(tmp_path, [(0, "L1", "W1-A", "A", 72, 92, 20000)])
    _set_early_fill(d, '"unbounded"')
    assert iv.load_cfg(None, d).early_fill_hours is None
    _set_early_fill(d, "0")
    rep = validate_work_dir(d)   # 0 h allowance: any early start is an ERROR
    w1 = _due(rep, "W1-A")
    assert len(w1) == 1 and w1[0].severity == ERROR and w1[0].hours == 48.0


def test_validator_due_window_no_waiver_when_early_fill_is_off(tmp_path):
    """allow_week1_in_week0 = false -> the 72 h start of W1-A is an ERROR
    whatever early_fill_hours says (the master switch, unchanged 2026-09-04)."""
    d = _wed_world(tmp_path, [
        (0, "L1", "W1-A", "A", 72, 92, 20000),
    ])
    toml = (d / "flowstate.toml").read_text(encoding="utf-8")
    (d / "flowstate.toml").write_text(
        toml.replace("[scheduler]", "[scheduler]\nallow_week1_in_week0 = false"), encoding="utf-8")
    rep = validate_work_dir(d)
    w1 = _due(rep, "W1-A")
    assert len(w1) == 1 and w1[0].severity == ERROR


# ═══════════════════════════════════════════════════════════════════════════
# I-2  two-phase week boundary + Params.legacy_week_stitch
# ═══════════════════════════════════════════════════════════════════════════
def _o(oid, ds, de, **kw):
    return dict(order_id=oid, sku="A", due_start=ds, due_end=de, qty_min=1,
                qty_max=2, qty_target=2, **kw)


def test_two_phase_boundary_is_the_second_distinct_demand_start():
    """Wednesday frame [0,120,288] -> 120 (the OLD constant 168 put a W1 order
    due 120-287 in NEITHER week: due_end 287 > 167 and due_start 120 < 168);
    Monday frame [0,168] -> 168; a single week -> the legacy 168; a current
    MO (due_start 50) and a trial are not week starts."""
    wed = [_o("W0", 0, 119), _o("W1", 120, 287), _o("W2", 288, 455),
           _o("MO", 50, 100, is_current_mo=True), _o("T", 30, 40, is_trial=True)]
    assert p2._two_phase_boundary(wed) == 120
    assert p2._two_phase_boundary([_o("W0", 0, 167), _o("W1", 168, 335)]) == 168
    assert p2._two_phase_boundary([_o("W0", 0, 167)]) == 168
    assert p2._two_phase_boundary([]) == 168
    # the split rule the driver applies with that boundary (hand):
    b = p2._two_phase_boundary(wed)
    week0 = [o["order_id"] for o in wed
             if o.get("is_current_mo") or (o.get("is_trial", False) and o["due_end"] < b)
             or (not o.get("is_trial", False) and o["due_start"] < b)]
    week1 = [o["order_id"] for o in wed if not o.get("is_current_mo")
             and not o.get("is_trial", False) and o["due_start"] >= b]
    assert week0 == ["W0", "MO", "T"] and week1 == ["W1", "W2"]
    assert set(week0) | set(week1) == {o["order_id"] for o in wed}   # nothing dropped


def test_params_legacy_week_stitch_field_and_config_read():
    """Default False; [scheduler] legacy_week_stitch = true -> True; it is a
    real dataclass field, so _sub_phase_params (dataclasses.replace) carries
    it into the two-phase sub-solves."""
    assert Params().legacy_week_stitch is False
    assert "legacy_week_stitch" in {f.name for f in fields(Params)}
    assert p2.params_from_config({}).legacy_week_stitch is False
    P = p2.params_from_config({"scheduler": {"legacy_week_stitch": True}})
    assert P.legacy_week_stitch is True
    assert p2._sub_phase_params(P, horizon_h=168).legacy_week_stitch is True


# ═══════════════════════════════════════════════════════════════════════════
# I-3 / I-4  model flags: setup_times_enforced, warnings surfaced
# ═══════════════════════════════════════════════════════════════════════════
def _tiny(P: Params, carry: int = 0, interval: int = 100000, avail: int = 0) -> Data:
    d = Data(P, Files(Path("/nonexistent")))
    d.lines = [0]
    d.line_names = {0: "L0"}
    for s in ("A", "B"):
        d.capable[(0, s)] = 1
        d.rate[(0, s)] = 1000.0
    d.init_map = {0: {"available_from": avail, "initial_sku": "A",
                      "carryover_run_hours": carry, "long_shutdown_flag": 0,
                      "long_shutdown_extra": 0}}
    d.downtimes = []
    d.cip_interval_map = {0: interval}
    d.setup = {("A", "B"): 2}
    d.machine_changes = {}
    d.orders = [dict(order_id="B-W0", sku="B", due_start=0, due_end=167, qty_min=9000,
                     qty_max=11000, qty_target=10000, priority=3)]
    return d


def test_model_announces_setup_times_enforced_and_level3_label_follows():
    """ignore_co (level 3) keeps setup TIME (fix SA-6): the model says so and
    the report label is ignore_co True / setup_times_enforced True (was the
    conservative False while the flag was not exposed — V's NOT DONE)."""
    P = Params(horizon_h=168)
    _, v = mb.build_model(P, _tiny(P), "full", False, True, objective_mode="balanced")
    assert v["setup_times_enforced"] is True
    assert p2._level_report_fields(3, v) == {"ignore_co": True, "setup_times_enforced": True}
    _, v0 = mb.build_model(P, _tiny(P), "full", False, False, objective_mode="balanced")
    assert v0["setup_times_enforced"] is True
    assert p2._level_report_fields(0, v0) == {"ignore_co": False, "setup_times_enforced": True}


def test_model_warnings_helper_and_the_overdue_gate_warning():
    """_model_warnings is a pure read of vars_dict["warnings"]; a line with
    carry 100 h on a 120 h interval gated at 60 (SB-3: CIP due at 120 - 100
    = 20 < gate 60 -> clean pinned at the gate) yields exactly one warning
    naming the line."""
    assert p2._model_warnings(None) == []
    assert p2._model_warnings({}) == []
    assert p2._model_warnings({"warnings": ["x", "y"]}) == ["x", "y"]
    P = Params(horizon_h=168, cip_interval_h=120, cip_duration_h=6)
    _, v = mb.build_model(P, _tiny(P, carry=100, interval=120, avail=60), "full", False, False,
                          objective_mode="balanced")
    ws = p2._model_warnings(v)
    assert len(ws) == 1 and "L0" in ws[0] and "CIP" in ws[0] and "gate" in ws[0]
    # the log helper returns the same list (and never raises without a log)
    assert p2._log_model_warnings(v, " (test)") == ws


def test_generate_summary_and_page_show_model_warnings():
    src = (CODE / "pages" / "generate.py").read_text(encoding="utf-8")
    assert 'feas.get("model_warnings")' in src
    assert 'st.warning(f"Model: {_mw}")' in src
    assert "model warning(s)" in src


# ═══════════════════════════════════════════════════════════════════════════
# I-4b  pass-2 adoption on pass 2's own objective (V Fix 3 x SA Fix 3)
# ═══════════════════════════════════════════════════════════════════════════
def test_adopt_pass2_uses_the_pass2_objective_when_known():
    """live_F t60 case by hand: pass 1 co_load 17,520, pass 2 19,820 (+13 %)
    but pass 2's objective fell (it bought +870 t of fill at K = 33): pass 1's
    plan scores 1,000 on the pass-2 objective, pass 2 scores 900 -> ADOPT
    (the co_load-only rule refused). obj2 > anchor -> keep pass 1 (hint not
    honoured). Physical validator errors still veto (1 > 0). Without the
    objective pair the legacy co_load rule applies unchanged."""
    ok, why = p2._adopt_pass2(17520, 19820, 0, 0, obj_anchor=1000.0, obj2=900.0)
    assert ok and "objective" in why and "co_load 19,820 vs 17,520" in why
    ok, why = p2._adopt_pass2(17520, 19820, 0, 0, obj_anchor=1000.0, obj2=1000.0)
    assert ok                                             # equal = not worse
    ok, why = p2._adopt_pass2(17520, 15000, 0, 0, obj_anchor=1000.0, obj2=1001.0)
    assert not ok and "hint not honoured" in why           # objective rules, not co_load
    ok, why = p2._adopt_pass2(17520, 19820, 0, 1, obj_anchor=1000.0, obj2=900.0)
    assert not ok and "physical validator errors 1 > pass 1's 0" in why
    ok, why = p2._adopt_pass2(17520, 19820, None, 3, obj_anchor=1000.0, obj2=900.0)
    assert ok and "unknown" in why                         # unknown counts cannot veto
    # legacy path (anchor failed -> no objective pair)
    assert p2._adopt_pass2(17520, 19820, 0, 0)[0] is False
    assert p2._adopt_pass2(17520, 17520, 0, 0)[0] is True
    assert p2._adopt_pass2(17520, 17000, 0, 0, obj_anchor=None, obj2=900.0)[0] is True
    src = (CODE / "solver" / "phase2_scheduler.py").read_text(encoding="utf-8")
    assert '_err1 = _val1.get("physical_errors")' in src    # physical, not n_errors
    assert "obj_anchor=_obj_anchor, obj2=_obj2" in src


def test_pass2_order_floors_are_opt_in():
    """INTEGRATE decision: the per-order floors are behind
    [scheduler] pass2_order_floors (default false); the exchange rate K is
    always passed. One full changeover (1080 x 100 x K=33 pass-2 units) at
    ~1,010 fill units/kg is 1080*100*33/1010 = 3,528.7 kg -> the log says
    '~ 3.5 t of fill'."""
    src = (CODE / "solver" / "phase2_scheduler.py").read_text(encoding="utf-8")
    assert 'PASS2_ORDER_FLOORS = bool(_CFG_SCHED.get("pass2_order_floors", False))' in src
    assert "if PASS2_ORDER_FLOORS:" in src
    assert "order_floors=_floors" in src and "fill_exchange_rate=_K" in src
    assert round(1080 * 100 * 33 / 1010 / 1000, 1) == 3.5


# ═══════════════════════════════════════════════════════════════════════════
# I-5  made_part credit rule
# ═══════════════════════════════════════════════════════════════════════════
def test_keep_completed_row_rule_by_hand():
    """MO 'R' is on the board (exclude_mos = {'R'}).
      completed row of R           -> dropped (its block carries the kg)
      made_part row of R, no attrs -> kept (post-fix board assumed)
      made_part row of R, board block post-fix (`made_kg=`) -> kept
      made_part row of R, legacy block (`pct=`, full Fct kg) -> dropped
      any row of MO 'Q' not on the board -> kept."""
    made = {"mo": "R", "kind": "made_part", "made_kg": 2500.0}
    done = {"mo": "R", "kind": "completed", "made_kg": 5000.0}
    other = {"mo": "Q", "kind": "completed", "made_kg": 100.0}
    ex = {"R"}
    assert keep_completed_row(done, ex, None) is False
    assert keep_completed_row(made, ex, None) is True
    assert keep_completed_row(made, ex, {"R": "current_state:running;fct_kg=5000;made_kg=2500"}) is True
    assert keep_completed_row(made, ex, {"R": "current_state:running;pct=50.0"}) is False
    assert keep_completed_row(made, ex, {}) is False           # attrs known, R has none -> legacy
    assert keep_completed_row(other, ex, {}) is True
    assert keep_completed_row(other, ex, None) is True
    src = (CODE / "pages" / "calendar.py").read_text(encoding="utf-8")
    assert "board_attrs=_board_attrs" in src


# ═══════════════════════════════════════════════════════════════════════════
# I-6  calendar_io
# ═══════════════════════════════════════════════════════════════════════════
def _cal(rows):
    cols = ["block_id", "block_type", "line_id", "line_name", "start_h", "end_h", "label",
            "order_id", "sku", "sku_description", "qty_kg", "locked", "attrs"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


def test_duplicate_block_ids_by_hand():
    """blk_1 on O1/S1 and O2/S1 -> unrelated -> reported; cs_9 twice with the
    same (MO9, S, production) = split pieces -> not reported; 'x' as a CIP and
    as production -> reported; blank / NaN ids ignored."""
    df = _cal([
        ("blk_1", "production", 0, "P09", 0, 10, "S1", "O1", "S1", "", 1000, False, ""),
        ("blk_1", "production", 0, "P09", 20, 30, "S1", "O2", "S1", "", 1000, False, ""),
        ("cs_9", "production", 0, "P09", 40, 50, "S", "MO9", "S", "", 500, True, "current_state:queued;split"),
        ("cs_9", "production", 0, "P09", 56, 60, "S", "MO9", "S", "", 200, True, "current_state:queued;split"),
        ("x", "cip", 1, "P10", 0, 6, "CIP", "", "", "", None, False, ""),
        ("x", "production", 1, "P10", 10, 20, "S", "O3", "S", "", 1000, False, ""),
        (None, "production", 1, "P10", 30, 40, "S", "O4", "S", "", 1000, False, ""),
        (float("nan"), "production", 1, "P10", 50, 60, "S", "O5", "S", "", 1000, False, ""),
    ])
    assert cio.duplicate_block_ids(df) == ["blk_1", "x"]
    assert cio.duplicate_block_ids(df.iloc[2:4]) == []
    assert cio.duplicate_block_ids(pd.DataFrame(columns=df.columns)) == []


def test_save_calendar_refuses_unrelated_blocks_under_one_id(tmp_path):
    bad = _cal([
        ("blk_1", "production", 0, "P09", 0, 10, "S1", "O1", "S1", "", 1000, False, ""),
        ("blk_1", "production", 0, "P09", 20, 30, "S1", "O2", "S1", "", 1000, False, ""),
    ])
    target = tmp_path / "calendar_blocks.csv"
    with pytest.raises(ValueError, match="blk_1"):
        cio.save_calendar(bad, target)
    assert not target.exists()                       # refused before any write
    res = cio.save_calendar(bad, target, allow_duplicate_ids=True)
    assert target.exists() and any("shared by unrelated" in w for w in res["warnings"])
    # split pieces of one MO are fine
    ok = _cal([
        ("cs_9", "production", 0, "P09", 40, 50, "S", "MO9", "S", "", 500, True, "split"),
        ("cs_9", "production", 0, "P09", 56, 60, "S", "MO9", "S", "", 200, True, "split"),
    ])
    assert cio.save_calendar(ok, tmp_path / "ok.csv")["warnings"] == []


def test_co_flag_columns_carry_cip_req_after_as_bit_64(tmp_path):
    """Seventh column -> bit 1 << 6 = 64 (the client's CO_FLAG_BITS label
    "CIP req"). A pair with only cip_req_after set has mask 64; ttp + cip_req
    -> 1 + 64 = 65."""
    assert cio.CO_FLAG_COLUMNS[6] == "cip_req_after" and len(cio.CO_FLAG_COLUMNS) == 7
    p = tmp_path / "changeovers.csv"
    pd.DataFrame([
        ("A", "B", 1.0, 0, 0, 0, 0, 0, 0, 0, 1),
        ("B", "A", 1.0, 1, 0, 0, 0, 0, 0, 0, 1),
        ("A", "C", 1.0, 0, 1, 0, 0, 0, 0, 0, 0),
    ], columns=["from_sku", "to_sku", "setup_hours", "ttp_change", "ffs_change", "tpld_change",
                "cspkr_change", "conv_to_org", "cinn_to_non_cinn", "added_flavors",
                "cip_req_after"]).to_csv(p, index=False)
    flags = cio.build_co_flags(p, {"A", "B", "C"})
    assert flags["A|B"] == 64 and flags["B|A"] == 65 and flags["A|C"] == 2
    ts = (CODE / "components/gantt/frontend/src/utils/skuPicker.ts").read_text(encoding="utf-8")
    assert "bit: 64" in ts


def test_board_supply_summary_rules_aligned_with_the_client():
    """no recipe -> NO_DATA (kept, counted); unknown kg + caps -> rate x hours
    (same verdict as the auto-kg record); unknown kg, no caps -> NO_DATA;
    `current_state:completed` counts as locked (source-level, the engine does
    not echo the flag)."""
    tsp = _load_test_module("test_stock_payload")
    gen = tsp.gen
    pay = tsp._payload(gen.base_inputs())
    P19 = gen.P19
    ref = cio.board_supply_summary([tsp.record(P19)], pay, {})
    nodata = cio.board_supply_summary([tsp.record({**P19, "block_id": "u", "sku": "424242"})], pay, {})
    assert list(nodata["verdicts"].values()) == ["NO_DATA"] and nodata["no_data"] == 1
    hours = float(P19["end_h"]) - float(P19["start_h"])
    rate = float(P19["cases"]) * tsp.KG_PER_CASE / hours
    with_caps = cio.board_supply_summary([tsp.record(P19, qty_kg=None)], pay, {},
                                         caps={P19["line_name"]: {tsp.SKU: rate}})
    assert with_caps["verdicts"] == ref["verdicts"]
    no_caps = cio.board_supply_summary([tsp.record(P19, qty_kg=None)], pay, {})
    assert list(no_caps["verdicts"].values()) == ["NO_DATA"]
    src = (CODE / "helpers" / "calendar_io.py").read_text(encoding="utf-8")
    assert '"current_state:completed" in _tokens' in src
    page = (CODE / "pages" / "calendar.py").read_text(encoding="utf-8")
    assert "locked_through_h=_lock_h, caps=caps)" in page


# ═══════════════════════════════════════════════════════════════════════════
# I-7  holding_builder
# ═══════════════════════════════════════════════════════════════════════════
def test_average_rate_per_sku_ignores_capable_rows_with_zero_rate():
    """X: capable rows 1000, 0, 500 and a non-capable 2000 -> (1000 + 500) / 2
    = 750 (the old mean counted the 0: 500). Y: only a 0-rate capable row ->
    no entry."""
    caps = pd.DataFrame([
        {"sku": "X", "capable": 1, "calc_rate_kgph": 1000.0},
        {"sku": "X", "capable": 1, "calc_rate_kgph": 0.0},
        {"sku": "X", "capable": 1, "calc_rate_kgph": 500.0},
        {"sku": "X", "capable": 0, "calc_rate_kgph": 2000.0},
        {"sku": "Y", "capable": 1, "calc_rate_kgph": 0.0},
    ])
    out = average_rate_per_sku(caps)
    assert out == {"X": 750.0}


# ═══════════════════════════════════════════════════════════════════════════
# I-8  manprg_start_h: data_loader passthrough + current_mo.csv writer
# ═══════════════════════════════════════════════════════════════════════════
def test_parse_current_mo_passes_manprg_start_h_through():
    """2,000 kg @ 500 kg/h -> ir 500, n_lo = n_hi = 4 -> bounds 2000/2000;
    manprg_start_h -12.4 -> -12 (rounded), NaN -> None, absent column -> None."""
    P = Params()
    d = Data(P, Files(Path("/nonexistent")))
    d.line_names = {0: "P09"}
    d.rate[(0, "S")] = 500.0
    base = {"mo": "30001", "line_name": "P09", "sku": "S", "remaining_kg": 2000.0,
            "due_start_h": 0, "due_end_h": 100, "locked_line": 1, "source": "manprg"}
    o = d._parse_current_mo(pd.DataFrame([{**base, "manprg_start_h": -12.4}]))[0]
    assert (o["qty_min"], o["qty_max"], o["manprg_start_h"]) == (2000, 2000, -12)
    o2 = d._parse_current_mo(pd.DataFrame([{**base, "manprg_start_h": float("nan")}]))[0]
    assert o2["manprg_start_h"] is None
    o3 = d._parse_current_mo(pd.DataFrame([base]))[0]
    assert o3["manprg_start_h"] is None


def test_overlay_current_state_writes_manprg_start_h(tmp_path, monkeypatch):
    """CB's sandbox (frozen clock 2027-03-02 03:00, anchor 2027-03-02 00:00):
    queued 30003 starts 03/02/2027 17:00 -> 17.0 h; 30002 and 30004 start
    03/02/2027 00:00 -> 0.0 h. The rows are the same three MOs C64 leaves in
    current_mo.csv."""
    tcb = _load_test_module("test_fix_CB")
    from helpers import horizon as hzmod
    real = hzmod.resolve
    monkeypatch.setattr(hzmod, "resolve", lambda cfg=None, now=None: real(cfg, now=tcb.NOW))
    dd = tcb._make_sandbox(tmp_path)
    work = dd / "_scenario_work" / "E"
    sr._prepare_work_dir(dd, work)
    sr._overlay_current_state(work, dd, lock_current_mo=True)
    cmo = pd.read_csv(work / "current_mo.csv", dtype={"mo": str}).set_index("mo")
    assert "manprg_start_h" in cmo.columns
    assert cmo.loc["30003", "manprg_start_h"] == 17.0
    assert cmo.loc["30002", "manprg_start_h"] == 0.0
    assert cmo.loc["30004", "manprg_start_h"] == 0.0
    # and the loader hands it to write_mo_changes as an int
    P = Params()
    d = Data(P, Files(work))
    d.line_names = {0: "P09", 1: "P10", 3: "P12", 4: "P13"}
    for lid in d.line_names:
        for s in ("111", "222", "888"):
            d.rate[(lid, s)] = 500.0
    parsed = {o["mo_id"]: o for o in d._parse_current_mo(pd.read_csv(work / "current_mo.csv", dtype={"sku": str}))}
    assert parsed["30003"]["manprg_start_h"] == 17


# ═══════════════════════════════════════════════════════════════════════════
# I-9  fill_ledger.json + save_scenario_version metadata
# ═══════════════════════════════════════════════════════════════════════════
def _compare_func(name: str):
    src = (CODE / "pages" / "compare.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(keep) == 1
    ns: dict = {"pd": pd, "Path": Path}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "compare.py", "exec"), ns)
    return ns[name]


def test_fill_ledger_json_roundtrips_into_compare_ledger_inputs(tmp_path):
    """Hand: anchor 2026-09-07 -> "2026-09-07 00:00:00"; hist {("S", 36): 1000}
    -> {"S|36": 1000.0}; a completed row with a Timestamp, a NaT, a NaN and a
    numpy int -> string / None / None / int; compare._fill_ledger_inputs gives
    back the typed legs (anchor datetime, tuple-keyed hist, Timestamp start)."""
    import numpy as np
    row = {"mo": "1", "item": "S", "made_kg": np.float64(5.0), "hours": np.int64(3),
           "start_dt": pd.Timestamp("2026-09-01 08:00:00"), "end_dt": pd.NaT,
           "left_cas": float("nan"), "kind": "completed"}
    p = sr._write_fill_ledger_json(tmp_path, datetime(2026, 9, 7), {("S", 36): 1000},
                                   [row], lookback_weeks=4)
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert payload["anchor"] == "2026-09-07 00:00:00"
    assert payload["history_demand"] == {"S|36": 1000.0}
    c = payload["completed"][0]
    assert c["start_dt"] == "2026-09-01 08:00:00" and c["end_dt"] is None
    assert c["left_cas"] is None and c["made_kg"] == 5.0 and c["hours"] == 3 and c["kind"] == "completed"
    assert payload["lookback_weeks"] == 4
    out = _compare_func("_fill_ledger_inputs")({"fill_ledger": payload})
    assert out["anchor"] == datetime(2026, 9, 7)
    assert out["history_demand"] == {("S", 36): 1000.0}
    assert out["completed"][0]["start_dt"] == pd.Timestamp("2026-09-01 08:00:00")
    assert out["lookback_weeks"] == 4


def test_save_scenario_version_persists_feasibility_scenario_id_and_fill_ledger(tmp_path, monkeypatch):
    """metadata.json carries feasibility (the listed keys only — 'junk' is
    dropped), scenario_id 'F-1' and the fill_ledger dict verbatim."""
    from helpers import version_manager as vm
    dd = tmp_path / "data"
    (dd / "versions").mkdir(parents=True)
    board = _cal([("b1", "production", 0, "P09", 0, 10, "S", "O1", "S", "", 1000, False, "")])
    cio.save_calendar(board, dd / "calendar_blocks.csv")
    monkeypatch.setattr(vm, "planning_anchor", lambda *a, **k: datetime(2026, 9, 2))
    fl = {"anchor": "2026-09-02 00:00:00", "history_demand": {"S|36": 1.0},
          "completed": [], "lookback_weeks": 4}
    result = {"ok": True, "calendar": board, "scorecard": {"composite": 1.0},
              "feasibility": {"relax_level": 0, "status": "OPTIMAL", "junk": 1,
                              "validation": {"physical_errors": 0}, "model_warnings": []},
              "fill_gates": {"P09": 10.0}, "fill_ledger": fl, "started_at": "2026-09-03T10:00:00"}
    scenario = {"id": "F-1", "name": "F", "objective": "balanced", "intent": "t",
                "fill_mode": True, "two_pass": True}
    res = sr.save_scenario_version(scenario, result, dd)
    meta = json.loads((dd / "versions" / res["slug"] / "metadata.json").read_text(encoding="utf-8"))
    assert meta["scenario_id"] == "F-1"
    assert meta["feasibility"] == {"relax_level": 0, "status": "OPTIMAL",
                                   "validation": {"physical_errors": 0}, "model_warnings": []}
    assert meta["fill_ledger"] == fl and meta["fill_gates"] == {"P09": 10.0}


def test_collect_result_reads_fill_ledger_json_and_overlay_writes_it():
    src = (CODE / "helpers" / "scenario_runner.py").read_text(encoding="utf-8")
    assert "_write_fill_ledger_json(work, hz.anchor, hist, cs.completed)" in src
    assert '"fill_ledger": fill_ledger,' in src


# ═══════════════════════════════════════════════════════════════════════════
# I-10  source-level guards for the page handoffs
# ═══════════════════════════════════════════════════════════════════════════
def test_pages_use_the_one_demand_targets_rule_and_payload_keys():
    cal = (CODE / "pages" / "calendar.py").read_text(encoding="utf-8")
    gen = (CODE / "pages" / "generate.py").read_text(encoding="utf-8")
    assert "demand_targets = _bdt(dd, anchor=_anchor)" in cal
    assert "for _, r in ddf.iterrows()" not in cal          # the inline loop is gone
    assert '"demand_anchor": (f"{_dem_anchor0:%Y-%m-%d %H:%M:%S}"' in cal
    assert "manprg content observed" in cal                 # CA-1 caption
    assert 'getattr(live, "rankable", True)' in cal          # Q rankable check
    assert "demand_targets = _bdt(dd)" in gen
    assert "retired" in gen and "cip_defer_weight (retired" in gen
    for f in ("phase2_scheduler.py",):
        s = (CODE / "solver" / f).read_text(encoding="utf-8")
        assert "_two_phase_boundary(data0.orders)" in s and "_two_phase_boundary(data1.orders)" in s
        assert "RETIRED (no effect since 2026-09-03" in s
