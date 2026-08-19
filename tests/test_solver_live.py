# tests/test_solver_live.py — live-narration helpers (user request 2026-08-19).
#
# Two layers under test, both pure:
#  1. the WRITER's pass-aware solution labels (solver/solver_progress.py) —
#     percentages must be against a baseline that makes sense for the pass
#     and direction (the old vs-first-feasible labels read "+2302.3%" noise
#     because the fill pass's first incumbent scores ~0);
#  2. the PAGE's formatting helpers (helpers/solver_live.py) — stage banner,
#     data chips, solution feed, trust indicators, journal filter.

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.solver_live import (  # noqa: E402
    data_chips,
    fmt_dur,
    journal_from_dir,
    journal_lines,
    live_caption,
    solution_feed,
    stage_banner,
    trust_lines,
)
from solver.solver_progress import (  # noqa: E402
    add_solution,
    co_solution_label,
    fill_solution_label,
    fmt_kg,
    generic_solution_label,
    init_progress,
    reset_solver_stats,
    update_solver_stats,
    STAGES_SINGLE,
)


# ── Writer: kg formatting ─────────────────────────────────────────────────

def test_fmt_kg_millions_and_thousands():
    assert fmt_kg(3_327_132) == "3.33M kg"
    assert fmt_kg(58_340) == "58,340 kg"
    assert fmt_kg(0) == "0 kg"


# ── Writer: fill-pass labels (maximize placed kg) ─────────────────────────

def test_fill_label_first_solution_states_kg_and_pct():
    lbl = fill_solution_label(1, 2_410_000, 3_327_132, None, pass_name="pass 1")
    assert "pass 1" in lbl
    assert "first plan on the board" in lbl
    assert "2.41M kg placed" in lbl
    assert "(72% of netted demand)" in lbl


def test_fill_label_improvement_shows_kg_delta():
    lbl = fill_solution_label(5, 3_210_000, 3_327_132, 3_151_660,
                              pass_name="pass 1")
    assert "more demand placed" in lbl
    assert "+58,340 kg" in lbl
    assert "(96% of netted demand)" in lbl


def test_fill_label_fewer_raw_kg_is_named_placement_not_growth():
    # The weighted score improved but raw kg dropped — the label must not
    # pretend tonnage went up.
    lbl = fill_solution_label(6, 3_190_000, 3_327_132, 3_202_000,
                              pass_name="pass 1")
    assert "better week/target placement" in lbl
    assert "-12,000 kg" in lbl
    assert "more demand placed" not in lbl


def test_fill_label_same_kg():
    lbl = fill_solution_label(7, 3_190_000, 3_327_132, 3_190_000)
    assert "same tonnage, better placed" in lbl


def test_fill_label_no_demand_basis_skips_pct():
    lbl = fill_solution_label(1, 1000, 0, None)
    assert "%" not in lbl
    assert "1,000 kg placed" in lbl


# ── Writer: pass-2 changeover labels (minimize, baseline = pass-2 start) ──

def test_co_label_first_solution_is_pass1_handover():
    lbl = co_solution_label(1, 4_432_000.0, 4_432_000.0, 34_120, 34_120)
    assert "starting from pass 1's plan" in lbl
    assert "34,120" in lbl


def test_co_label_improvement_pct_against_pass2_start():
    lbl = co_solution_label(50, 3_616_682.0, 4_432_000.0, 27_842, 34_120)
    # (34120-27842)/34120 = 18.4%
    assert "changeover load down 18.4% since pass 2 started" in lbl
    assert "27,842" in lbl


def test_co_label_raw_load_up_is_honest():
    lbl = co_solution_label(9, 100.0, 200.0, 35_000, 34_120)
    assert "up" not in lbl.split("·")[0]  # pass tag clean
    assert "above pass-2 start" in lbl


def test_co_label_fallback_uses_objective_and_says_so():
    lbl = co_solution_label(12, 3_616_682.0, 4_432_000.0, None, None)
    assert "changeover objective down 18.4%" in lbl


# ── Writer: generic labels keep pct only for meaningful baselines ─────────

def test_generic_label_first():
    assert generic_solution_label(1, -0.0, -0.0) == "first feasible plan"


def test_generic_label_minimize_down_pct():
    lbl = generic_solution_label(26, 8_770.0, 10_000.0, direction="min",
                                 prefix="W0 L1: ")
    assert lbl == "W0 L1: plan #26 — objective down 12.3% vs first"


def test_generic_label_absurd_baseline_drops_pct():
    # Old behavior printed "+24132986976500.0%" here.
    lbl = generic_solution_label(2, 241_329_869_765.0, -0.0, direction="max")
    assert "%" not in lbl
    assert lbl == "improved plan #2"


# ── Writer: solution entries carry ts + structured extras ─────────────────

def test_add_solution_stamps_ts_and_extras(tmp_path):
    init_progress(tmp_path, STAGES_SINGLE)
    add_solution(tmp_path, 12.5, 100.0, "x", pass_id="fill",
                 placed_kg=500, co_load=None)
    state = json.loads((tmp_path / "solver_progress.json").read_text())
    (sol,) = state["solutions"]
    assert sol["pass_id"] == "fill"
    assert sol["placed_kg"] == 500
    assert "co_load" not in sol  # None extras are dropped, never invented
    datetime.fromisoformat(sol["ts"])  # parseable wall-clock stamp


def test_reset_solver_stats_replaces_not_merges(tmp_path):
    init_progress(tmp_path, STAGES_SINGLE)
    update_solver_stats(tmp_path, status="SOLVING", gap_pct=100.0,
                        best_objective=5.0, direction="max")
    reset_solver_stats(tmp_path, status="STARTING", direction="min",
                       pass_id="co")
    stats = json.loads(
        (tmp_path / "solver_progress.json").read_text())["solver_stats"]
    assert stats == {"status": "STARTING", "direction": "min",
                     "pass_id": "co"}
    assert "gap_pct" not in stats  # pass-1 numbers must not describe pass 2


# ── Page: durations ────────────────────────────────────────────────────────

def test_fmt_dur():
    assert fmt_dur(45) == "45s"
    assert fmt_dur(993) == "16m 33s"
    assert fmt_dur(4085) == "1h 08m"
    assert fmt_dur(-3) == "0s"


# ── Page: stage banner ─────────────────────────────────────────────────────

def _prog(stage_id, status="active", detail="", extra_stages=()):
    stages = [{"id": stage_id, "label": stage_id.replace("_", " ").title(),
               "status": status, "detail": detail, "ts": ""}]
    stages.extend(extra_stages)
    return {"stages": stages, "solutions": [], "solver_stats": {},
            "data_summary": {}}


def test_banner_fill_pass1():
    p = _prog("solving", detail="600s time limit, 8 workers (level 0: hard)")
    b = stage_banner(p, fill_mode=True)
    assert "Pass 1" in b
    assert "netted demand" in b
    assert "nearest weeks first" in b


def test_banner_fill_anchor():
    p = _prog("solving",
              detail="pass 2 anchor: locking pass 1's plan in as the starting point")
    b = stage_banner(p, fill_mode=True)
    assert "Locking pass 1's plan in" in b


def test_banner_fill_pass2_reads_floor_from_detail():
    p = _prog("solving", detail="pass 2: min changeovers, fill floored at 99% (600s)")
    b = stage_banner(p, fill_mode=True)
    assert "floored at 99%" in b
    assert "FFS" in b and "topload" in b


def test_banner_fill_pass2_without_floor_detail_does_not_invent_one():
    p = _prog("solving", detail="pass 2: min changeovers, fill floored (600s)")
    b = stage_banner(p, fill_mode=True)
    assert "99%" not in b
    assert "pass 1's level" in b


def test_banner_non_fill_uses_stage_label_and_detail():
    p = _prog("solving_week0", detail="60s limit, 8 workers (level 0: hard)")
    b = stage_banner(p, fill_mode=False)
    assert "Solving Week0" in b
    assert "60s limit" in b


def test_banner_building_loading_validating():
    assert "Building the optimization model — 122,431 variables" in stage_banner(
        _prog("building_model", detail="122,431 variables"), True)
    assert "staged inputs" in stage_banner(_prog("loading_data"), True)
    assert "hard rules" in stage_banner(_prog("validating"), True)


def test_banner_all_done_and_error_and_empty():
    done = {"stages": [{"id": "solving", "label": "Solving", "status": "done",
                        "detail": "", "ts": ""}],
            "solutions": [], "solver_stats": {}, "data_summary": {}}
    assert "finished" in stage_banner(done, True)
    err = _prog("solving", status="error", detail="INFEASIBLE")
    assert "INFEASIBLE" in stage_banner(err, True)
    assert "Waiting" in stage_banner(None, True)
    assert "Waiting" in stage_banner({}, True)


# ── Page: data chips ───────────────────────────────────────────────────────

def test_data_chips_full_summary():
    p = {"data_summary": {"lines": 14, "orders": 144, "skus": 72,
                          "total_demand_kg": 3_327_132,
                          "changeover_pairs": 37_830, "horizon_h": 504}}
    chips = data_chips(p, fill_mode=True)
    assert "14 lines" in chips
    assert "144 orders" in chips
    assert "72 SKUs" in chips
    assert "3.33M kg netted demand" in chips
    assert "3-week horizon (504h)" in chips
    assert "37,830 changeover pairs" in chips


def test_data_chips_empty():
    assert data_chips(None) == ""
    assert data_chips({"data_summary": {}}) == ""


# ── Page: live caption keeps the original composition ─────────────────────

def test_live_caption_composition():
    p = _prog("solving", detail="pass 2: min changeovers, fill floored at 99% (600s)")
    p["solver_stats"] = {"gap_pct": 100.0}
    p["solutions"] = [{"wall_time": 355.5, "objective": 1.0,
                       "label": "pass 2 · changeover load down 18.4% since pass 2 started"}]
    cap = live_caption(p, 993)
    assert cap.startswith("16m 33s elapsed")
    assert "gap 100.0%" in cap
    assert "changeover load down 18.4%" in cap


# ── Page: solution feed ────────────────────────────────────────────────────

def test_solution_feed_newest_first_with_clock_and_walltime():
    sols = [{"wall_time": 10.0, "objective": 1.0, "label": f"plan {i}",
             "ts": f"2026-08-19T11:00:{i:02d}"} for i in range(12)]
    p = {"solutions": sols}
    feed = solution_feed(p, limit=8)
    assert len(feed) == 8
    assert feed[0].endswith("plan 11")  # newest first
    assert feed[0].startswith("11:00:11 · t+10s — ")
    assert feed[-1].endswith("plan 4")


def test_solution_feed_tolerates_old_entries_without_ts():
    p = {"solutions": [{"wall_time": 104.02, "objective": -0.0,
                        "label": "L0: First feasible solution"}]}
    assert solution_feed(p) == ["t+1m 44s — L0: First feasible solution"]


# ── Page: trust indicators ─────────────────────────────────────────────────

def test_trust_stale_search_wording():
    now = datetime(2026, 8, 19, 11, 10, 0)
    p = {"solver_stats": {"status": "SOLVING"},
         "solutions": [{"label": "x", "ts": "2026-08-19T11:05:48"}]}
    (line,) = trust_lines(p, now=now)
    assert line == "still searching — best plan unchanged for 4m 12s"


def test_trust_recent_improvement_wording():
    now = datetime(2026, 8, 19, 11, 6, 0)
    p = {"solver_stats": {"status": "SOLVING"},
         "solutions": [{"label": "x", "ts": "2026-08-19T11:05:48"}]}
    (line,) = trust_lines(p, now=now)
    assert line.startswith("improving — better plan found 12s ago")


def test_trust_max_loose_ceiling_defends_the_plan():
    p = {"solver_stats": {"status": "SOLVING", "direction": "max",
                          "gap_pct": 100.0, "best_objective": 3.9e9},
         "solutions": []}
    (line,) = trust_lines(p)
    assert "loose" in line
    assert "not the plan" in line


def test_trust_max_tight_ceiling():
    p = {"solver_stats": {"status": "SOLVING", "direction": "max",
                          "gap_pct": 3.2, "best_objective": 100.0},
         "solutions": []}
    (line,) = trust_lines(p)
    assert "no plan more than 3% better" in line


def test_trust_min_pass2_names_changeovers():
    p = {"solver_stats": {"status": "SOLVING", "direction": "min",
                          "gap_pct": 12.0, "best_objective": 100.0,
                          "pass_id": "co"},
         "solutions": []}
    (line,) = trust_lines(p)
    assert "cheaper on changeovers" in line


def test_trust_min_loose_floor():
    p = {"solver_stats": {"status": "SOLVING", "direction": "min",
                          "gap_pct": 100.0, "best_objective": 3_460_128.0},
         "solutions": []}
    (line,) = trust_lines(p)
    assert "floor still loose" in line
    assert "only ever improves" in line


def test_trust_optimal():
    p = {"solver_stats": {"status": "OPTIMAL", "gap_pct": 0.0,
                          "best_objective": 1.0, "direction": "min"},
         "solutions": []}
    assert trust_lines(p) == [
        "proven optimal — no better plan exists under these rules and inputs"]


def test_trust_silent_when_not_solving():
    p = {"solver_stats": {"status": "STARTING"}, "solutions": []}
    assert trust_lines(p) == []
    assert trust_lines(None) == []


# ── Page: journal filter ───────────────────────────────────────────────────

_LOG = """[10:58:33] START 2026-08-19 phase=full relax=False tl=600 mlpo=2
[10:58:34] [data] 14 lines, 144 orders, 72 SKUs
[10:58:37] [model] level 0 (hard): 122431 vars, 442172 constraints
[10:58:37] [warm-start] hinted 20160 vars from the previous schedule: 96 assignments
[10:58:37] some noisy internal line that planners should not see
[11:08:37] SOLVER level=0 status=FEASIBLE
[11:08:38] [two-pass] pass 1 fill score 3,958,964,950 -> pass 2 floor 3,919,375,300 (1.0% give), objective = weighted changeover load
[11:08:40] [two-pass] anchor OPTIMAL: complete 122,431-var hint installed (pass 1's plan as incumbent)
[11:18:45] [validate] running post-solve validation
"""


def test_journal_filters_to_machinery_lines():
    lines = journal_lines(_LOG)
    assert len(lines) == 8
    assert not any("noisy internal" in ln for ln in lines)
    assert any("[warm-start] hinted" in ln for ln in lines)
    assert any("[two-pass] anchor OPTIMAL" in ln for ln in lines)
    assert lines[0].startswith("[10:58:33] START")


def test_journal_limit_keeps_the_tail():
    lines = journal_lines(_LOG, limit=2)
    assert len(lines) == 2
    assert "[validate]" in lines[-1]


def test_journal_from_dir_missing_file(tmp_path):
    assert journal_from_dir(tmp_path) == []


def test_journal_from_dir_reads_solver_error_txt(tmp_path):
    (tmp_path / "solver_error.txt").write_text(_LOG, encoding="utf-8")
    assert len(journal_from_dir(tmp_path)) == 8
