# tests/test_overnight_batch.py — the overnight batch engine's pure helpers
# plus the fixed-slug publish logic against a temp versions dir.

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "overnight_batch", ROOT / "scripts" / "overnight_batch.py")
ob = importlib.util.module_from_spec(_spec)
# dataclass resolution needs the module registered before exec
sys.modules["overnight_batch"] = ob
_spec.loader.exec_module(ob)


# ── generation id: stable for a stable signature ───────────────────────────

def test_gen_id_stability():
    sig = (1.0, 2.0, 3.5)
    now = datetime(2026, 8, 20, 19, 0)
    a = ob.gen_id_for(sig, now)
    b = ob.gen_id_for(tuple(sig), now)
    assert a == b
    assert a.startswith("20260820-1900-")
    assert len(a.split("-")[-1]) == 8
    # a different signature changes ONLY the hash suffix
    c = ob.gen_id_for((1.0, 2.0, 999.0), now)
    assert c != a
    assert c.split("-")[:2] == a.split("-")[:2]


# ── generation rotation signature ──────────────────────────────────────────

def test_generation_signature_covers_board_and_lock(tmp_path, monkeypatch):
    """The fill staging nets the planner's board out of demand and the pin
    guard compares against it, so a board (or week-lock) edit MUST rotate the
    generation — scoring_inputs_signature alone watches only reference/+toml."""
    data = tmp_path / "data"
    (data / "reference").mkdir(parents=True)
    board = data / "calendar_blocks.csv"
    board.write_text("block_id\n", encoding="utf-8")
    lock = data / "lock_state.json"
    lock.write_text("{}", encoding="utf-8")
    # hold the shared part constant so only our two files can move the result
    monkeypatch.setattr(ob, "scoring_inputs_signature", lambda d: (1.0, 2.0))

    base = ob.generation_signature(data)
    assert base[:2] == (1.0, 2.0)
    assert len(base) == 2 + len(ob._EXTRA_SIGNATURE_FILES)
    assert ob.generation_signature(data) == base  # stable while nothing moves

    import os
    os.utime(board, (base[2] + 60, base[2] + 60))
    moved = ob.generation_signature(data)
    assert moved != base
    assert ob.gen_id_for(moved, datetime(2026, 8, 20, 19, 0)) != \
        ob.gen_id_for(base, datetime(2026, 8, 20, 19, 0))

    os.utime(lock, (base[3] + 60, base[3] + 60))
    assert ob.generation_signature(data) != moved

    # a missing file reads 0.0 rather than exploding
    board.unlink()
    lock.unlink()
    assert ob.generation_signature(data) == (1.0, 2.0, 0.0, 0.0)


# ── work-dir isolation ─────────────────────────────────────────────────────

def test_batch_solves_outside_the_shared_scenario_work_root(monkeypatch):
    """An overnight arm must never land in data/_scenario_work: the UI's
    plant write-back picker and the constraint probes both select the dir
    that solved LAST, and a 3am sandbox solve would win that race."""
    from helpers import scenario_runner as sr

    assert ob.WORK_ROOT != "_scenario_work"

    seen: dict[str, Path] = {}

    class _Stop(Exception):
        pass

    def _capture(data_dir, work):
        seen["work"] = work
        raise _Stop

    monkeypatch.setattr(sr, "_prepare_work_dir", _capture)
    scenario = {"id": "F_overnight_champion_n1", "fill_mode": True}
    with pytest.raises(_Stop):
        sr.run_scenario(scenario, Path("data"), work_root=ob.WORK_ROOT)
    assert seen["work"].parent.name == ob.WORK_ROOT
    assert "_scenario_work" not in seen["work"].parts

    # the default is unchanged for every existing caller
    with pytest.raises(_Stop):
        sr.run_scenario(scenario, Path("data"))
    assert seen["work"].parent.name == "_scenario_work"


# ── steering.json parsing ──────────────────────────────────────────────────

def _steering(at: datetime, rows: list[dict]) -> dict:
    return {"generated_by": "test", "at": at.isoformat(timespec="seconds"),
            "portfolio_overrides": rows, "notes": ""}


def test_steering_fresh_replaces_exploration_arms():
    now = datetime(2026, 8, 20, 19, 0)
    raw = _steering(now - timedelta(hours=2), [
        {"label": "hot_idea", "params": {"ffs_weight": 900},
         "budgets": {"pass1_s": 120, "pass2_s": 240}},
    ])
    arms, notes = ob.parse_steering(raw, now)
    assert len(arms) == 1
    assert arms[0]["label"] == "steer_hot_idea"
    assert arms[0]["kind"] == "exploration"
    assert arms[0]["budgets"] == {"pass1_s": 120, "pass2_s": 240}
    assert any("replaced" in n for n in notes)

    base = ob.default_portfolio({}, dry_run=False)
    merged = ob.apply_steering(base, arms)
    kinds = [a["kind"] for a in merged]
    # every diversity arm survives; the steering exploration arm is appended
    # (the default portfolio carries no exploration arms since 2026-08-21)
    assert kinds.count("noise") == 2
    assert kinds.count("seed") == 2
    assert kinds.count("cold") == 1
    assert kinds.count("chained") == 1
    assert [a["label"] for a in merged if a["kind"] == "exploration"] == \
        ["steer_hot_idea"]


def test_steering_stale_or_malformed_is_ignored():
    now = datetime(2026, 8, 20, 19, 0)
    stale = _steering(now - timedelta(hours=30), [
        {"label": "old", "params": {}}])
    arms, notes = ob.parse_steering(stale, now)
    assert arms == []
    assert any("stale" in n for n in notes)

    arms2, notes2 = ob.parse_steering({"at": "not-a-date"}, now)
    assert arms2 == []
    arms3, _ = ob.parse_steering(None, now)
    assert arms3 == []

    # rows without label / dict params are skipped, valid rows survive
    mixed = _steering(now, [
        {"params": {}},                      # no label
        {"label": "bad", "params": "nope"},  # params not a dict
        {"label": "ok", "params": {"topload_weight": 100}},
    ])
    arms4, notes4 = ob.parse_steering(mixed, now)
    assert [a["label"] for a in arms4] == ["steer_ok"]
    # budgets default to the champion's
    assert arms4[0]["budgets"] == ob.CHAMPION_BUDGETS


def test_default_portfolio_shape():
    """Portfolio redesign 2026-08-21: diversity of STARTING POINTS.
    Ladder arms are retired (600/3600 vs 300/1800 measured within noise on
    2026-08-20); exploration arms come only from steering.json."""
    full = ob.default_portfolio({}, dry_run=False)
    assert [a["label"] for a in full] == [
        "champion_n1", "champion_n2", "seed_2", "seed_3",
        "cold_start", "chained_best"]
    assert [a["kind"] for a in full] == [
        "noise", "noise", "seed", "seed", "cold", "chained"]
    assert all(a["budgets"] == ob.CHAMPION_BUDGETS for a in full)
    assert not any(a["kind"] in ("ladder", "exploration") for a in full)
    # seed arms: champion params + ONLY the CP-SAT seed differs
    seeds = [a for a in full if a["kind"] == "seed"]
    assert [a["params"] for a in seeds] == [
        {"solver_random_seed": 2}, {"solver_random_seed": 3}]
    # cold arm skips every warm start; chained plants the donor schedule
    assert next(a for a in full if a["kind"] == "cold")["warm_start"] == \
        "none"
    assert next(a for a in full if a["kind"] == "chained")["warm_start"] == \
        "chained"
    dry = ob.default_portfolio({}, dry_run=True)
    assert [a["label"] for a in dry] == [
        "champion_n1", "champion_n2", "seed_2", "cold_start",
        "chained_best"]
    assert all(a["budgets"] == ob.DRY_BUDGETS for a in dry)
    # F's built-in overrides remain the champion baseline the effective-
    # weight helper reports (steering authors scale from these)
    eff = ob.effective_co_weights({})
    assert eff["ffs_weight"] == 600
    assert eff["topload_weight"] == 450


def test_chained_arm_runs_after_every_possible_donor():
    """The chained arm warm-starts from the best candidate SO FAR, so it
    must be ordered after every other arm in both portfolios."""
    for dry in (False, True):
        arms = ob.default_portfolio({}, dry_run=dry)
        assert arms[-1]["kind"] == "chained"


def test_budget_override_flags_hit_every_arm():
    arms = ob.default_portfolio({}, dry_run=False)
    out = ob.apply_budget_overrides(arms, 120, 300)
    assert all(a["budgets"] == {"pass1_s": 120, "pass2_s": 300}
               for a in out)
    # one-sided override keeps the other pass per-arm
    arms2 = ob.default_portfolio({}, dry_run=False)
    ob.apply_budget_overrides(arms2, None, 300)
    assert all(a["budgets"]["pass1_s"] == ob.CHAMPION_BUDGETS["pass1_s"]
               for a in arms2)
    assert all(a["budgets"]["pass2_s"] == 300 for a in arms2)
    # no flags = byte-identical budgets (the Task Scheduler path)
    arms3 = ob.default_portfolio({}, dry_run=False)
    before = [dict(a["budgets"]) for a in arms3]
    ob.apply_budget_overrides(arms3, None, None)
    assert [a["budgets"] for a in arms3] == before
    # steering arms are arms too
    steer = [{"label": "steer_x", "kind": "exploration", "params": {},
              "budgets": {"pass1_s": 600, "pass2_s": 2400}}]
    merged = ob.apply_budget_overrides(
        ob.apply_steering(ob.default_portfolio({}, dry_run=False), steer),
        60, 90)
    assert all(a["budgets"] == {"pass1_s": 60, "pass2_s": 90}
               for a in merged)


def test_best_donor_picks_top_scored_candidate():
    cands = [
        {"run_id": "a", "label": "champion_n1",
         "overnight_score": {"composite": 64.2}},
        {"run_id": "b", "label": "seed_2", "overnight_score": None},  # failed
        {"run_id": "c", "label": "seed_3",
         "overnight_score": {"composite": 66.1}},
    ]
    assert ob.best_donor(cands)["run_id"] == "c"
    assert ob.best_donor([]) is None
    assert ob.best_donor([{"run_id": "b", "overnight_score": None}]) is None


def test_plant_chain_seed_copies_the_donor_problem(tmp_path):
    """The chained arm re-solves the donor's EXACT staged problem: measured
    2026-08-21, two same-generation arms staged 14 min apart differ (the
    running-MO tail estimates tick with the wall clock — gates +1h on 3
    lines), so carrying only the signature is not enough. The donor's
    staged inputs are copied byte-for-byte; the trust gate stays intact."""
    donor = tmp_path / "F_overnight_seed_3"
    work = tmp_path / "F_overnight_chained_best"
    donor.mkdir()
    work.mkdir()
    (donor / "schedule_phase2.csv").write_text("line_id,order_id\n",
                                               encoding="utf-8")
    (donor / "feasibility_report.json").write_text(
        json.dumps({"relax_level": 0, "input_sig": "abc123"}),
        encoding="utf-8")
    # donor problem files vs the chained arm's own (drifted) staging
    (donor / "demand_plan.csv").write_text("donor-demand", encoding="utf-8")
    (donor / "initial_states.csv").write_text("donor-init", encoding="utf-8")
    (work / "demand_plan.csv").write_text("drifted-demand", encoding="utf-8")
    (work / "initial_states.csv").write_text("drifted-init", encoding="utf-8")
    # staged for the arm but absent from the donor -> must be REMOVED so
    # both dirs hash identically (a half-shared problem is no problem)
    (work / "current_mo.csv").write_text("stray", encoding="utf-8")

    notes = ob.plant_chain_seed(work, donor)
    assert (work / "prev_schedule.csv").read_text(encoding="utf-8") == \
        "line_id,order_id\n"
    assert (work / "demand_plan.csv").read_text(encoding="utf-8") == \
        "donor-demand"
    assert (work / "initial_states.csv").read_text(encoding="utf-8") == \
        "donor-init"
    assert not (work / "current_mo.csv").exists()
    # the donor's signature + relax level are CARRIED verbatim — the
    # solver's trust gates decide, the batch never fakes a signature
    planted = json.loads(
        (work / "prev_feasibility.json").read_text(encoding="utf-8"))
    assert planted == {"relax_level": 0, "input_sig": "abc123"}
    assert any("input_sig carried" in n for n in notes)

    # donor without a report: problem + schedule planted, gate consequence
    # reported honestly
    (donor / "feasibility_report.json").unlink()
    notes2 = ob.plant_chain_seed(work, donor)
    assert any("WITHOUT a" in n for n in notes2)

    # donor without a schedule: nothing planted, cold reported
    (donor / "schedule_phase2.csv").unlink()
    notes3 = ob.plant_chain_seed(work, donor)
    assert any("solving cold" in n for n in notes3)


def test_chain_problem_files_cover_the_solver_signature():
    """Every file phase2_scheduler.input_signature hashes must be copied by
    plant_chain_seed — one missed file and the trust gate honestly rejects
    the whole chain (that is how the 2026-08-21 drift was caught)."""
    sys.path.insert(
        0, str(Path(__file__).resolve().parent.parent / "code" / "solver"))
    import phase2_scheduler
    import inspect
    src = inspect.getsource(phase2_scheduler.input_signature)
    sig_files = {n for n in ob.CHAIN_PROBLEM_FILES if f'"{n}"' in src}
    # the nine signature inputs are all covered
    assert len(sig_files) == 9, (
        f"CHAIN_PROBLEM_FILES covers only {sorted(sig_files)} of the "
        "solver's input_signature files")


# ── cold / chained warm-start staging (scenario_runner seam) ───────────────

def test_stage_warm_start_modes(tmp_path, monkeypatch):
    from helpers import scenario_runner as sr

    work = tmp_path
    carried = work / "prev_schedule.csv"
    carried_feas = work / "prev_feasibility.json"

    # "none" (cold arm): carried warm-start files are REMOVED — the solver
    # must log a true cold start with zero hints
    carried.write_text("stale", encoding="utf-8")
    carried_feas.write_text("{}", encoding="utf-8")
    notes = sr._stage_warm_start(work, {"warm_start": "none"})
    assert not carried.exists() and not carried_feas.exists()
    assert any("cold start" in n for n in notes)

    # "prev" (chained arm): the planted donor schedule is left untouched
    carried.write_text("donor", encoding="utf-8")
    notes2 = sr._stage_warm_start(work, {"warm_start": "prev"})
    assert carried.read_text(encoding="utf-8") == "donor"
    assert any("chained" in n for n in notes2)
    # "prev" without a planted file is reported, not invented
    carried.unlink()
    notes3 = sr._stage_warm_start(work, {"warm_start": "prev"})
    assert any("no prev_schedule.csv" in n for n in notes3)

    # default / absent: the historical greedy seed runs
    called = {}
    monkeypatch.setattr(sr, "_greedy_seed",
                        lambda w: called.setdefault("work", w) and []
                        or ["greedy ran"])
    assert sr._stage_warm_start(work, {}) == ["greedy ran"]
    assert called["work"] == work
    assert sr._stage_warm_start(work, {"warm_start": "greedy"}) == \
        ["greedy ran"]


# ── guards: overlaps + pins ────────────────────────────────────────────────

def _cal(rows):
    base = {"block_id": "b", "block_type": "production", "line_id": "1",
            "line_name": "P09", "start_h": 0.0, "end_h": 1.0, "label": "",
            "order_id": "", "sku": "S", "sku_description": "",
            "qty_kg": None, "locked": False, "attrs": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_count_overlaps():
    ok = _cal([{"start_h": 0, "end_h": 10},
               {"start_h": 10, "end_h": 20},
               {"start_h": 5, "end_h": 15, "line_name": "P10"}])
    assert ob.count_overlaps(ok) == 0
    bad = _cal([{"start_h": 0, "end_h": 10},
                {"start_h": 9, "end_h": 20},
                {"start_h": 19, "end_h": 25, "block_type": "cip"}])
    assert ob.count_overlaps(bad) == 2


def test_pins_match():
    pinned = _cal([{"start_h": 50, "end_h": 60, "sku": "111",
                    "order_id": "O1", "attrs": "pinned"}])
    cand_ok = _cal([{"start_h": 50, "end_h": 60, "sku": "111",
                     "order_id": "O1"}])
    cand_moved = _cal([{"start_h": 70, "end_h": 80, "sku": "111",
                        "order_id": "O1"}])
    assert ob.pins_match(cand_ok, pinned)
    assert not ob.pins_match(cand_moved, pinned)
    assert ob.pins_match(cand_moved, pinned.iloc[0:0])  # nothing pinned


# ── noise floor + ladder verdict ───────────────────────────────────────────

def _cand(label, comp, kind="exploration"):
    return {"run_id": label, "label": label, "kind": kind,
            "overnight_score": {"composite": comp}}


def test_noise_floor_and_ladder_verdict():
    cands = [_cand("champion_n1", 70.0, "noise"),
             _cand("champion_n2", 71.2, "noise"),
             _cand("champion_n3", 70.6, "noise"),
             _cand("ladder_300_1800", 70.9, "ladder"),
             _cand("ladder_600_3600", 72.5, "ladder")]
    floor = ob.noise_floor(cands)
    assert floor == {"runs": 3, "spread_composite": 1.2}
    verdict = ob.ladder_verdict(cands, floor)
    assert "YES" in verdict  # +1.6 > 1.2 spread
    verdict2 = ob.ladder_verdict(
        [c for c in cands if c["label"] != "ladder_600_3600"], floor)
    assert "incomplete" in verdict2
    assert ob.noise_floor([_cand("champion_n1", 70.0, "noise")]) is None


def test_next_occurrence():
    start = datetime(2026, 8, 20, 19, 0)
    assert ob.next_occurrence("05:00", start) == datetime(2026, 8, 21, 5, 0)
    late = datetime(2026, 8, 21, 5, 30)
    # computed ONCE at start — an overrun does not wait another day
    assert ob.next_occurrence("05:00", start) < late


# ── publish: fixed slugs against a temp versions dir ───────────────────────

@pytest.fixture()
def temp_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "versions").mkdir(parents=True)
    opt = data / "optimizer"
    opt.mkdir()
    monkeypatch.setattr(ob, "DATA", data)
    monkeypatch.setattr(ob, "OPT_DIR", opt)
    return data


def _mk_candidate(data: Path, gen: "ob.Generation", run_id: str,
                  comp: float) -> dict:
    from helpers.calendar_io import save_calendar
    cdir = gen.dir / "candidates"
    cdir.mkdir(parents=True, exist_ok=True)
    cal = _cal([{"order_id": "O1", "sku": "111", "start_h": 10,
                 "end_h": 20, "qty_kg": 500}])
    save_calendar(cal, cdir / f"{run_id}.calendar.csv")
    (cdir / f"{run_id}.scorecard.json").write_text(
        json.dumps({"composite": 55.0}), encoding="utf-8")
    return {
        "run_id": run_id, "label": run_id, "kind": "noise",
        "params": {}, "budgets": {"pass1_s": 60, "pass2_s": 120},
        "overnight_score": {"version": "v1", "composite": comp,
                            "fill": comp, "changeovers": comp,
                            "campaign": comp, "on_time": comp},
        "guards": {"overlaps": 0, "pins_ok": True, "lock_ok": True,
                   "cip_ok": True},
        "gap_pct_end": None, "wall_s": 1.0,
        "version_slug": None, "published": None,
        "_calendar_path": str(cdir / f"{run_id}.calendar.csv"),
        "_scorecard_path": str(cdir / f"{run_id}.scorecard.json"),
        "_fill_gates": {"P09": 10.0},
        "_gen_id": gen.gen_id,
    }


def _mk_gen(data: Path, gen_id: str = "20260820-1900-abcd1234",
            created: str = "2026-08-20T19:00:00",
            baseline: float | None = None) -> "ob.Generation":
    from helpers.horizon import resolve as _hr
    from helpers.config import load_toml as _lt
    anchor = _hr(_lt()).anchor  # publish shifts vs the CURRENT anchor -> 0
    gen = ob.Generation(
        gen_id=gen_id, sig=(1.0,),
        created=created, anchor=anchor, horizon_h=504.0,
        week_marks=[(0.0, 34)], gates={"P09": 10.0},
        demand=pd.DataFrame([{"order_id": "O1", "sku": "111",
                              "qty_target": 1000.0, "lower_pct": 0.9,
                              "upper_pct": 1.1, "due_start_hour": 0,
                              "due_end_hour": 167}]),
        net_demand_kg=1000.0, capacity_bound=5000.0, capacity_detail={},
        co_map={},
        board_baseline=({"overnight_score": {"composite": baseline}}
                        if baseline is not None else None),
        board_note="",
    )
    gen.dir.mkdir(parents=True, exist_ok=True)
    return gen


def test_publish_creates_best_and_runner_up(temp_env):
    from helpers.version_manager import list_versions
    gen = _mk_gen(temp_env)
    gen.candidates = [_mk_candidate(temp_env, gen, "a", 60.0),
                      _mk_candidate(temp_env, gen, "b", 70.0),
                      _mk_candidate(temp_env, gen, "c", 65.0)]
    notes: list[str] = []
    ob.publish_top2([gen], ob.Log(temp_env / "optimizer" / "batch.log"),
                    notes)
    vs = {v["slug"]: v for v in list_versions(temp_env)}
    assert set(vs) == {"overnight_best", "overnight_runner_up"}
    # best = composite 70 (run b), runner-up = 65 (run c)
    assert "score 70.0" in vs["overnight_best"]["name"]
    assert "score 65.0" in vs["overnight_runner_up"]["name"]
    assert vs["overnight_best"]["source"] == "agent:overnight"
    assert vs["overnight_best"]["fill_gates"] == {"P09": 10.0}
    best = next(c for c in gen.candidates if c["run_id"] == "b")
    assert best["published"] == "best"
    assert best["version_slug"] == "overnight_best"
    # leaderboard on disk reflects the publish and hides plumbing keys
    lb = json.loads((gen.dir / "leaderboard.json").read_text())
    pub = {c["run_id"]: c.get("published") for c in lb["candidates"]}
    assert pub == {"a": None, "b": "best", "c": "runner_up"}
    assert not any(k.startswith("_")
                   for c in lb["candidates"] for k in c)


def test_publish_skips_guard_failures(temp_env):
    from helpers.version_manager import list_versions
    gen = _mk_gen(temp_env)
    good = _mk_candidate(temp_env, gen, "good", 50.0)
    bad = _mk_candidate(temp_env, gen, "bad", 90.0)
    bad["guards"]["overlaps"] = 2  # better score, but disqualified
    gen.candidates = [good, bad]
    notes: list[str] = []
    ob.publish_top2([gen], ob.Log(temp_env / "optimizer" / "batch.log"),
                    notes)
    vs = {v["slug"] for v in list_versions(temp_env)}
    assert vs == {"overnight_best"}
    assert good["published"] == "best"
    assert bad["published"] is None


def test_publish_updates_in_place_at_full_capacity(temp_env):
    """5/5 slots with overnight_best among them: the update succeeds without
    eviction; the runner-up (a NEW slug at capacity) is caught + noted."""
    from helpers.version_manager import MAX_VERSIONS, list_versions
    vd = temp_env / "versions"
    for i in range(MAX_VERSIONS - 1):
        d = vd / f"user_version_{i}"
        d.mkdir()
        (d / "metadata.json").write_text(json.dumps(
            {"name": f"user {i}", "timestamp": f"2026-08-1{i}T00:00:00",
             "source": "manual", "scorecard": {}}), encoding="utf-8")
    d = vd / "overnight_best"
    d.mkdir()
    (d / "metadata.json").write_text(json.dumps(
        {"name": "Overnight best (old)", "timestamp": "2026-08-19T05:00:00",
         "source": "agent:overnight", "scorecard": {}}), encoding="utf-8")

    gen = _mk_gen(temp_env)
    gen.candidates = [_mk_candidate(temp_env, gen, "a", 60.0),
                      _mk_candidate(temp_env, gen, "b", 70.0)]
    notes: list[str] = []
    ob.publish_top2([gen], ob.Log(temp_env / "optimizer" / "batch.log"),
                    notes)
    vs = {v["slug"]: v for v in list_versions(temp_env)}
    assert len(vs) == MAX_VERSIONS  # nothing evicted, nothing added
    assert "score 70.0" in vs["overnight_best"]["name"]  # updated in place
    assert "overnight_runner_up" not in vs
    assert any("runner_up" in n and "skipped" in n for n in notes)
    # the leaderboard still records the best as published
    b = next(c for c in gen.candidates if c["run_id"] == "b")
    assert b["published"] == "best"


def test_publish_first_creation_at_capacity_is_caught(temp_env):
    from helpers.version_manager import MAX_VERSIONS, list_versions
    vd = temp_env / "versions"
    for i in range(MAX_VERSIONS):
        d = vd / f"user_version_{i}"
        d.mkdir()
        (d / "metadata.json").write_text(json.dumps(
            {"name": f"user {i}", "timestamp": f"2026-08-1{i}T00:00:00",
             "source": "manual", "scorecard": {}}), encoding="utf-8")
    gen = _mk_gen(temp_env)
    gen.candidates = [_mk_candidate(temp_env, gen, "a", 60.0)]
    notes: list[str] = []
    ob.publish_top2([gen], ob.Log(temp_env / "optimizer" / "batch.log"),
                    notes)
    assert len(list_versions(temp_env)) == MAX_VERSIONS
    assert any("overnight_best" in n and "skipped" in n for n in notes)
    assert gen.candidates[0]["published"] is None


# ---------------------------------------------------------------------------
# Publish fill floor — the frozen v1 composite gives 45% of its weight to
# per-placed-hour ratios, so an arm that places almost nothing can outrank a
# real schedule. It may sit on the leaderboard; it may NOT reach the
# planner's version slots (2026-08-19).
# ---------------------------------------------------------------------------

def _pub_cand(run_id: str, fill: float, composite: float) -> dict:
    return {
        "run_id": run_id, "label": run_id,
        "overnight_score": {"version": "v1", "composite": composite,
                            "fill": fill, "changeovers": 90.0,
                            "campaign": 100.0, "on_time": 20.0},
        "guards": {"overlaps": 0, "pins_ok": True, "lock_ok": True,
                   "cip_ok": True},
    }


def test_underfilled_candidate_is_barred_from_publishing():
    from overnight_batch import publishable

    real = _pub_cand("champion", fill=86.0, composite=63.0)
    lazy = _pub_cand("lazy", fill=31.0, composite=70.0)   # wins on ratios alone
    eligible, barred = publishable([lazy, real])
    assert [c["run_id"] for c in eligible] == ["champion"]
    assert [c["run_id"] for c in barred] == ["lazy"]


def test_close_fills_all_stay_eligible():
    from overnight_batch import publishable

    a = _pub_cand("a", fill=86.0, composite=63.0)
    b = _pub_cand("b", fill=80.0, composite=65.0)   # 93% of best — fine
    eligible, barred = publishable([a, b])
    assert {c["run_id"] for c in eligible} == {"a", "b"}
    assert barred == []


def test_publishable_handles_an_empty_pool():
    from overnight_batch import publishable

    assert publishable([]) == ([], [])


# ---------------------------------------------------------------------------
# Same-generation deltas — the 2026-08-21 brief compared a 20260820-1505
# candidate (71.19) against the 20260821-0500 board (66.65) and headlined
# +4.54 when the honest same-generation delta was +0.44, inside the ±1.64
# noise floor. Every delta a planner sees must stay inside one generation.
# ---------------------------------------------------------------------------

OLD_GEN = "20260820-1505-aaaa1111"
NEW_GEN = "20260821-0500-bbbb2222"


def test_published_block_uses_own_generation_baseline():
    boards = [
        {"generation": OLD_GEN,
         "board_baseline": {"overnight_score": {"composite": 70.75}},
         "candidates": [
             {"run_id": "champ_a", "published": "best",
              "version_slug": "overnight_best",
              "overnight_score": {"composite": 71.19}},
             {"run_id": "champ_b", "published": None,
              "overnight_score": {"composite": 70.0}},
         ]},
        {"generation": NEW_GEN,
         "board_baseline": {"overnight_score": {"composite": 66.65}},
         "candidates": [
             {"run_id": "champ_5am", "published": "runner_up",
              "version_slug": "overnight_runner_up",
              "overnight_score": {"composite": 67.01}},
         ]},
    ]
    rows = ob.published_block(boards)
    assert [r["run_id"] for r in rows] == ["champ_a", "champ_5am"]
    best, runner = rows
    assert best["tag"] == "best"
    assert best["generation"] == OLD_GEN
    assert best["board_baseline_composite"] == pytest.approx(70.75)
    assert best["delta_same_gen"] == pytest.approx(0.44)
    # the runner-up's delta comes from ITS generation, not the best's
    assert runner["generation"] == NEW_GEN
    assert runner["board_baseline_composite"] == pytest.approx(66.65)
    assert runner["delta_same_gen"] == pytest.approx(0.36)
    # nobody's delta is the cross-generation lie
    assert all(abs(r["delta_same_gen"] - 4.54) > 1.0 for r in rows)

    # a published candidate whose generation had no baseline reports None —
    # it must never borrow another generation's board
    boards[0]["board_baseline"] = None
    rows2 = ob.published_block(boards)
    assert rows2[0]["delta_same_gen"] is None
    assert rows2[0]["board_baseline_composite"] is None


def test_same_generation_deltas_end_to_end(temp_env):
    """Two generations; the best candidate lives in the OLDER one whose
    baseline is HIGHER. Publish notes, brief, latest.json published block,
    summarize() and the UI table must all report the same-generation delta
    (+0.44 / +0.36) and never the cross-generation +4.54."""
    from helpers import overnight_results as onr
    from helpers.overnight_ui import _leaderboard_frame

    old = _mk_gen(temp_env, gen_id=OLD_GEN,
                  created="2026-08-20T15:05:00", baseline=70.75)
    new = _mk_gen(temp_env, gen_id=NEW_GEN,
                  created="2026-08-21T05:00:00", baseline=66.65)
    best = _mk_candidate(temp_env, old, "champion_n1_150543", 71.19)
    n2 = _mk_candidate(temp_env, old, "champion_n2_155622", 69.55)
    old.candidates = [best, n2]                    # noise arms -> floor 1.64
    champ5 = _mk_candidate(temp_env, new, "champion_5am_050020", 67.01)
    champ5["kind"] = "champion"
    new.candidates = [champ5]
    gens = [old, new]
    log = ob.Log(temp_env / "optimizer" / "batch.log")

    notes: list[str] = []
    ob.publish_top2(gens, log, notes)
    assert best["published"] == "best"
    assert n2["published"] == "runner_up"
    pub_notes = [n for n in notes if n.startswith("published")]
    assert any("vs its generation's board 70.75 -> +0.44" in n
               for n in pub_notes)
    assert any("-> -1.20" in n for n in pub_notes)
    assert not any("4.54" in n for n in notes)

    ob.write_brief(gens, notes, [], datetime(2026, 8, 20, 15, 5), log)
    brief = (temp_env / "optimizer" / "brief.md").read_text(encoding="utf-8")
    assert "4.54" not in brief
    assert "Δ vs board" in brief
    for delta in ("+0.44", "-1.20", "+0.36"):      # each vs its OWN board
        assert delta in brief
    vs_board = brief.split("## vs your board", 1)[1].split("##", 1)[0]
    assert "vs its generation's board 70.75 -> +0.44" in vs_board
    assert "(noise ±1.64) — within noise" in vs_board
    assert OLD_GEN in vs_board                     # names the generation

    for g in gens:
        ob.write_leaderboard(g)
    block = ob.published_block([ob.leaderboard_dict(g) for g in gens])
    ob.write_latest(new, block)
    latest = json.loads(
        (temp_env / "optimizer" / "latest.json").read_text(encoding="utf-8"))
    assert latest["generation"] == NEW_GEN         # pointer semantics kept
    assert [(r["tag"], r["generation"], r["delta_same_gen"])
            for r in latest["published"]] == \
        [("best", OLD_GEN, 0.44), ("runner_up", OLD_GEN, -1.2)]

    ov = onr.summarize(temp_env, now=datetime(2026, 8, 21, 5, 0))
    assert ov.best_composite == pytest.approx(71.19)
    assert ov.board_composite == pytest.approx(70.75)
    assert ov.delta_vs_board == pytest.approx(0.44)
    assert ov.published_best["run_id"] == "champion_n1_150543"
    assert "+0.4 vs its board, 14h ago" in ov.detail
    assert "4.5" not in ov.detail

    frame_old = _leaderboard_frame(ob.leaderboard_dict(old))
    assert list(frame_old["Δ vs board"]) == pytest.approx([0.44, -1.2])
    frame_new = _leaderboard_frame(ob.leaderboard_dict(new))
    assert list(frame_new["Δ vs board"]) == pytest.approx([0.36])
    # both generations sit within the ±1.64 floor — the UI says "tie"
    assert onr.all_within_noise(ob.leaderboard_dict(old), 1.64)


# ── regression on the real 2026-08-20/21 night (gitignored fixtures) ───────

REAL_OPT = ROOT / "data" / "optimizer"
REAL_GENS = ["20260819-1810-486dcb88", "20260820-1505-486dcb88",
             "20260821-0500-486dcb88"]


@pytest.mark.skipif(
    not all((REAL_OPT / g / "leaderboard.json").exists() for g in REAL_GENS),
    reason="real overnight generations not present (gitignored data)")
def test_real_generations_regression(tmp_path):
    """The night that exposed the bug: published best champion_n1_150543
    (gen 1505, 71.19) must read +0.44 against ITS generation's board 70.75 —
    never +4.54 against the 0500 generation's 66.65."""
    from helpers import overnight_results as onr

    boards = [json.loads((REAL_OPT / g / "leaderboard.json")
                         .read_text(encoding="utf-8")) for g in REAL_GENS]
    block = ob.published_block(boards)
    best = next(r for r in block if r["tag"] == "best")
    assert best["run_id"] == "champion_n1_150543"
    assert best["generation"] == "20260820-1505-486dcb88"
    assert best["board_baseline_composite"] == pytest.approx(70.75)
    assert best["delta_same_gen"] == pytest.approx(0.44, abs=0.01)

    # stage a copy carrying the published block, as the fixed batch writes it
    opt = tmp_path / "optimizer"
    for g in REAL_GENS:
        (opt / g).mkdir(parents=True)
        (opt / g / "leaderboard.json").write_text(
            (REAL_OPT / g / "leaderboard.json").read_text(encoding="utf-8"),
            encoding="utf-8")
    # The pointer is frozen AS IT WAS that night — the live latest.json moves
    # with every later batch (a supervised daytime run re-pointed it hours
    # after this test was written, breaking the replay). The generation dirs
    # themselves are immutable history; only the pointer needed pinning.
    latest = {"generation": REAL_GENS[-1],
              "leaderboard": f"{REAL_GENS[-1]}/leaderboard.json",
              "brief": "brief.md"}
    (opt / "latest.json").write_text(
        json.dumps({**latest, "published": block}), encoding="utf-8")

    ov = onr.summarize(tmp_path, now=datetime(2026, 8, 21, 5, 50))
    assert ov.best_composite == pytest.approx(71.19)
    assert ov.board_composite == pytest.approx(70.75)
    assert ov.delta_vs_board == pytest.approx(0.44, abs=0.01)
    assert "vs its board" in ov.detail and "ago" in ov.detail
    assert "4.5" not in ov.detail

    # an old-writer latest.json (no block) falls back to the LATEST
    # generation's own delta (+0.36) — still same-generation, never +4.54
    (opt / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
    ov2 = onr.summarize(tmp_path, now=datetime(2026, 8, 21, 5, 50))
    assert ov2.delta_vs_board == pytest.approx(0.36, abs=0.01)
    assert ov2.published_best is None


# ---------------------------------------------------------------------------
# Resume — the 2026-08-22 reboot: all six arms of 20260821-1900-5d421b01
# finished, the batch died in the "waiting until 05:00" sleep, nothing was
# published and latest.json stayed on the previous generation. --resume
# rebuilds the night from disk and finishes it; the phase ledger
# (state.json) is how the Home chip tells that night from a quiet one.
# ---------------------------------------------------------------------------

import argparse  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
from types import SimpleNamespace  # noqa: E402

CRASHED_GEN = "20260821-1900-5d421b01"
PRIOR_GEN = "20260821-1047-486dcb88"


def _resume_args(gen_id: str, **over) -> argparse.Namespace:
    base = dict(resume=gen_id, dry_run=False, publish=False,
                no_publish=False, skip_pull=True, pass1_s=None,
                pass2_s=None, consolidate_at="05:00")
    base.update(over)
    return argparse.Namespace(**base)


def _log(data: Path) -> "ob.Log":
    return ob.Log(data / "optimizer" / "batch.log")


def _crashed_night(data: Path, *, state: bool = True,
                   heartbeat: str = "2026-08-22T00:11:26") -> "ob.Generation":
    """A generation dir exactly as the reboot left it: leaderboard +
    candidates + frame snapshot, ledger in arms_done with a 16h-old
    heartbeat, latest.json still pointing at the previous generation."""
    gen = _mk_gen(data, gen_id=CRASHED_GEN, created="2026-08-21T19:00:07")
    gen.candidates = [_mk_candidate(data, gen, "champion_n1_190030", 65.54),
                      _mk_candidate(data, gen, "seed_2_204549", 69.38),
                      _mk_candidate(data, gen, "cold_start_222906", 66.54)]
    ob.write_leaderboard(gen)
    ob.write_frame(gen)
    if state:
        ob.write_state(gen, ob.PHASE_ARMS_DONE, batch=[gen.gen_id],
                       arms_planned=3)
        st = json.loads((gen.dir / "state.json").read_text(encoding="utf-8"))
        st["updated"] = heartbeat
        (gen.dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
    opt = data / "optimizer"
    (opt / PRIOR_GEN).mkdir(parents=True, exist_ok=True)
    (opt / "latest.json").write_text(json.dumps({
        "generation": PRIOR_GEN,
        "leaderboard": f"{PRIOR_GEN}/leaderboard.json",
        "brief": "brief.md", "published": []}), encoding="utf-8")
    return gen


def test_state_ledger_merges_and_heartbeats(temp_env):
    gen = _mk_gen(temp_env)
    s1 = ob.write_state(gen, ob.PHASE_STAGED, batch=[gen.gen_id])
    assert s1["phase"] == "staged"
    assert s1["started"] == gen.created
    assert s1["arms_done"] == 0 and s1["arms_scored"] == 0
    gen.candidates.append(_mk_candidate(temp_env, gen, "a", 60.0))
    gen.candidates.append({"run_id": "crashed", "overnight_score": None})
    s2 = ob.write_state(gen, ob.PHASE_CONSOLIDATED,
                        consolidation={"status": "done", "note": "x"})
    assert s2["arms_done"] == 2 and s2["arms_scored"] == 1
    assert s2["updated"] >= s1["updated"]
    # later phase writes MERGE: the consolidation record and the batch list
    # survive the terminal write
    s3 = ob.write_state(gen, ob.PHASE_PUBLISHED, published=[])
    assert s3["phase"] == ob.PHASE_PUBLISHED
    assert s3["consolidation"] == {"status": "done", "note": "x"}
    assert s3["batch"] == [gen.gen_id]
    assert ob.read_state(gen.dir) == s3
    assert ob.read_state(temp_env / "nowhere") is None
    (gen.dir / "state.json").write_text("{bad", encoding="utf-8")
    assert ob.read_state(gen.dir) is None
    assert ob.gen_stamp(CRASHED_GEN) == datetime(2026, 8, 21, 19, 0)
    assert ob.gen_stamp("garbage") is None


def test_load_generation_rebuilds_candidate_plumbing(temp_env):
    from helpers.version_manager import list_versions
    data = temp_env
    gen = _mk_gen(data, baseline=64.0)
    a = _mk_candidate(data, gen, "a", 60.0)
    b = _mk_candidate(data, gen, "b", 70.0)
    gen.candidates = [a, b]
    ob.write_leaderboard(gen)
    ob.write_frame(gen)
    # b carries the gates sidecar run_arm writes; a's calendar file is gone
    (gen.dir / "candidates" / "b.gates.json").write_text(
        json.dumps({"gates": {"p09": 33.0}}), encoding="utf-8")
    (gen.dir / "candidates" / "a.calendar.csv").unlink()

    loaded = ob.load_generation(gen.gen_id, _log(data))
    assert loaded.gen_id == gen.gen_id
    assert loaded.sig == (1.0,)
    assert loaded.anchor == gen.anchor
    assert loaded.horizon_h == 504.0
    assert loaded.capacity_bound == 5000.0
    assert loaded.net_demand_kg == 1000.0
    assert loaded.frame_ok
    assert loaded.gates == {"P09": 10.0}
    assert list(loaded.demand["order_id"]) == ["O1"]
    assert ob.baseline_composite(loaded.board_baseline) == 64.0
    by = {c["run_id"]: c for c in loaded.candidates}
    assert by["b"]["_gen_id"] == gen.gen_id
    assert by["b"]["_calendar_path"].endswith("b.calendar.csv")
    assert by["b"]["_fill_gates"] == {"P09": 33.0}  # sidecar beats the frame
    assert by["a"]["_calendar_path"] is None        # gone -> cannot publish
    notes: list[str] = []
    ob.publish_top2([loaded], _log(data), notes)
    assert {v["slug"] for v in list_versions(data)} == {"overnight_best"}
    assert by["b"]["published"] == "best"
    assert by["a"]["published"] is None
    lb = json.loads((gen.dir / "leaderboard.json").read_text(encoding="utf-8"))
    assert not any(k.startswith("_") for c in lb["candidates"] for k in c)

    # a leaderboard written before the frame contract cannot be resumed
    fixture = ROOT / "tests" / "fixtures" / "optimizer" / "20260819-0230-9f3a7c21"
    shutil.copytree(fixture, data / "optimizer" / fixture.name)
    with pytest.raises(ValueError, match="no frame"):
        ob.load_generation(fixture.name, _log(data))


def test_candidate_gates_fall_back_to_a_surviving_work_dir(temp_env):
    """No sidecar (pre-contract candidate): the arm's work dir is trusted
    only when its gates were written inside this run's window — a later
    night re-uses the same F_overnight_<label> dir."""
    data = temp_env
    gen = _mk_gen(data, created="2026-08-21T19:00:07")
    cand = _mk_candidate(data, gen, "seed_2_204549", 69.38)
    created = datetime.fromisoformat(gen.created).timestamp()
    cal = gen.dir / "candidates" / "seed_2_204549.calendar.csv"
    os.utime(cal, (created + 3600, created + 3600))
    work = data / ob.WORK_ROOT / "F_overnight_seed_2_204549" / "fill_gates.json"
    work.parent.mkdir(parents=True)
    work.write_text(json.dumps({"gates": {"P09": 42.0}}), encoding="utf-8")

    os.utime(work, (created + 600, created + 600))       # inside the window
    assert ob.candidate_gates(cand, gen) == {"P09": 42.0}
    os.utime(work, (created + 7200, created + 7200))     # after the save: a later night's
    assert ob.candidate_gates(cand, gen) == {"P09": 10.0}  # generation gates
    os.utime(work, (created - 3600, created - 3600))     # before staging
    assert ob.candidate_gates(cand, gen) == {"P09": 10.0}
    # the staging dir is the second fallback, same window rule
    stage = data / ob.WORK_ROOT / ob.STAGING_ID / "fill_gates.json"
    stage.parent.mkdir(parents=True)
    stage.write_text(json.dumps({"gates": {"P09": 7.0}}), encoding="utf-8")
    os.utime(stage, (created + 5, created + 5))
    assert ob.candidate_gates(cand, gen) == {"P09": 7.0}
    # nothing on disk and no frame gates -> honest empty gates
    gen.gates = {}
    os.utime(stage, (created + 9999, created + 9999))
    assert ob.candidate_gates(cand, gen) == {}


def test_resume_finishes_an_arms_done_night(temp_env, monkeypatch):
    """The reboot case, inputs + anchor still matching: the late champion
    runs on the generation's own frame, the night publishes, the brief
    names the interruption, latest.json moves to the generation and the
    ledger reaches `published`."""
    from helpers.version_manager import list_versions
    data = temp_env
    gen = _crashed_night(data)
    monkeypatch.setattr(ob, "generation_signature", lambda d: (1.0,))
    monkeypatch.setattr(ob, "fresh_stock_check", lambda log: ({}, []))
    monkeypatch.setattr(ob, "_load_changeovers", lambda ref: pd.DataFrame())
    ran: list[str] = []

    def fake_run_arm(arm, g, dns, log):
        ran.append(arm["label"])
        assert arm["budgets"] == ob.CHAMPION_BUDGETS
        assert g.gen_id == CRASHED_GEN and g.frame_ok
        cand = _mk_candidate(data, g, "champion_5am_120000", 71.0)
        cand["kind"] = "champion"
        g.candidates.append(cand)
        ob.write_leaderboard(g)
        return cand

    monkeypatch.setattr(ob, "run_arm", fake_run_arm)
    assert ob.run_resume(_resume_args(CRASHED_GEN), _log(data)) == 0
    assert ran == ["champion_5am"]

    vs = {v["slug"]: v for v in list_versions(data)}
    assert set(vs) == {"overnight_best", "overnight_runner_up"}
    assert "score 71.0" in vs["overnight_best"]["name"]
    assert "score 69.38" in vs["overnight_runner_up"]["name"]
    assert vs["overnight_best"]["run_id"] == "champion_5am_120000"
    assert vs["overnight_best"]["generation"] == CRASHED_GEN
    assert vs["overnight_runner_up"]["run_id"] == "seed_2_204549"

    latest = json.loads((data / "optimizer" / "latest.json")
                        .read_text(encoding="utf-8"))
    assert latest["generation"] == CRASHED_GEN
    assert [(r["tag"], r["run_id"]) for r in latest["published"]] == \
        [("best", "champion_5am_120000"), ("runner_up", "seed_2_204549")]

    brief = (data / "optimizer" / "brief.md").read_text(encoding="utf-8")
    assert "Interrupted night, finished by `--resume`" in brief
    assert f"Generation {CRASHED_GEN} stopped in phase 'arms_done'" in brief
    assert "last heartbeat 2026-08-22T00:11:26" in brief
    assert "3 arm(s) finished, 3 scored, nothing published" in brief
    assert "Batch started 2026-08-21 19:00" in brief   # the night's, not now
    assert "4 arm(s) tried, 4 scored" in brief

    state = ob.read_state(gen.dir)
    assert state["phase"] == ob.PHASE_PUBLISHED
    assert state["consolidation"] == {
        "status": "done", "note": "late champion via --resume",
        "run_id": "champion_5am_120000"}
    assert state["resumed"]["from_phase"] == ob.PHASE_ARMS_DONE
    assert state["resumed"]["last_heartbeat"] == "2026-08-22T00:11:26"
    assert [(r["tag"], r["slug"], r["run_id"]) for r in state["published"]] \
        == [("best", "overnight_best", "champion_5am_120000"),
            ("runner_up", "overnight_runner_up", "seed_2_204549")]
    assert state["batch"] == [CRASHED_GEN]
    lb = json.loads((gen.dir / "leaderboard.json").read_text(encoding="utf-8"))
    assert {c["run_id"]: c.get("published") for c in lb["candidates"]} == {
        "champion_n1_190030": None, "seed_2_204549": "runner_up",
        "cold_start_222906": None, "champion_5am_120000": "best"}


@pytest.mark.parametrize("why", ["signature", "anchor", "no_frame"])
def test_resume_skips_the_late_champion_when_the_problem_moved(
        temp_env, monkeypatch, why):
    """A changed board / reference / toml, a rolled anchor, or a generation
    without a frame snapshot: the late champion would solve a DIFFERENT
    problem than the leaderboard's candidates, so it is skipped and the
    brief says why. The candidates themselves still publish — they are
    exactly the problem their generation staged."""
    from helpers.version_manager import list_versions
    data = temp_env
    gen = _crashed_night(data)
    monkeypatch.setattr(ob, "generation_signature",
                        lambda d: (2.0,) if why == "signature" else (1.0,))
    if why == "anchor":
        rolled = SimpleNamespace(anchor=gen.anchor + timedelta(days=1))
        monkeypatch.setattr(ob, "resolve_horizon", lambda cfg=None: rolled)
    if why == "no_frame":
        shutil.rmtree(gen.dir / "frame")
    monkeypatch.setattr(
        ob, "run_arm",
        lambda *a, **k: pytest.fail("the late champion must not run"))

    assert ob.run_resume(_resume_args(CRASHED_GEN), _log(data)) == 0
    assert {v["slug"] for v in list_versions(data)} == \
        {"overnight_best", "overnight_runner_up"}
    expected = {"signature": "the scoring inputs changed",
                "anchor": "the planning anchor rolled",
                "no_frame": "no frame/ snapshot"}[why]
    brief = (data / "optimizer" / "brief.md").read_text(encoding="utf-8")
    assert "late champion consolidation skipped: " + expected in brief
    assert "Interrupted night, finished by `--resume`" in brief
    state = ob.read_state(gen.dir)
    assert state["phase"] == ob.PHASE_PUBLISHED
    assert state["consolidation"]["status"] == "skipped"
    assert expected in state["consolidation"]["note"]


def test_resume_twice_never_republishes(temp_env, monkeypatch):
    """Idempotency: the second invocation for the same generation upserts
    nothing — the slugs already hold these runs — so a planner's rename on
    the published version survives and the timestamps do not move."""
    from helpers.version_manager import list_versions, rename_version
    data = temp_env
    gen = _crashed_night(data)
    monkeypatch.setattr(ob, "generation_signature", lambda d: (2.0,))
    upserts: list[str] = []
    real = ob.upsert_version

    def counting(slug, *a, **k):
        upserts.append(slug)
        return real(slug, *a, **k)

    monkeypatch.setattr(ob, "upsert_version", counting)
    assert ob.run_resume(_resume_args(CRASHED_GEN), _log(data)) == 0
    assert upserts == ["overnight_best", "overnight_runner_up"]
    vs1 = {v["slug"]: v for v in list_versions(data)}
    rename_version("overnight_best", "Seed 2 — looks right", data)

    assert ob.run_resume(_resume_args(CRASHED_GEN), _log(data)) == 0
    assert upserts == ["overnight_best", "overnight_runner_up"]  # no new ones
    vs2 = {v["slug"]: v for v in list_versions(data)}
    assert set(vs2) == {"overnight_best", "overnight_runner_up"}
    assert vs2["overnight_best"]["name"] == "Seed 2 — looks right"
    assert vs2["overnight_runner_up"]["timestamp"] == \
        vs1["overnight_runner_up"]["timestamp"]
    brief = (data / "optimizer" / "brief.md").read_text(encoding="utf-8")
    assert "had already been finalized" in brief
    assert "overnight_best already holds seed_2_204549" in brief
    assert "overnight_runner_up already holds cold_start_222906" in brief
    lb = json.loads((gen.dir / "leaderboard.json").read_text(encoding="utf-8"))
    assert {c["run_id"]: c.get("published") for c in lb["candidates"]} == {
        "champion_n1_190030": None, "seed_2_204549": "best",
        "cold_start_222906": "runner_up"}
    state = ob.read_state(gen.dir)
    assert state["phase"] == ob.PHASE_PUBLISHED
    # the first run's consolidation record survives the re-run verbatim
    assert state["consolidation"]["status"] == "skipped"
    assert "the scoring inputs changed" in state["consolidation"]["note"]
    latest = json.loads((data / "optimizer" / "latest.json")
                        .read_text(encoding="utf-8"))
    assert latest["generation"] == CRASHED_GEN


def test_main_path_publish_is_idempotent_too(temp_env):
    """publish_top2 itself skips a slug that already holds the same run —
    and re-assigns when the ranking moved (a late champion displacing the
    old best overwrites overnight_best and demotes it to runner-up)."""
    from helpers.version_manager import list_versions
    data = temp_env
    gen = _mk_gen(data)
    b = _mk_candidate(data, gen, "b", 70.0)
    c = _mk_candidate(data, gen, "c", 65.0)
    gen.candidates = [b, c]
    notes: list[str] = []
    ob.publish_top2([gen], _log(data), notes)
    first = {v["slug"]: v for v in list_versions(data)}
    notes.clear()
    ob.publish_top2([gen], _log(data), notes)
    again = {v["slug"]: v for v in list_versions(data)}
    assert again == first
    assert sum("already holds" in n for n in notes) == 2
    # ranking moves: a new best arrives
    d = _mk_candidate(data, gen, "d", 80.0)
    gen.candidates.append(d)
    notes.clear()
    ob.publish_top2([gen], _log(data), notes)
    moved = {v["slug"]: v for v in list_versions(data)}
    assert moved["overnight_best"]["run_id"] == "d"
    assert moved["overnight_runner_up"]["run_id"] == "b"
    assert (d["published"], b["published"], c["published"]) == \
        ("best", "runner_up", None)
    assert c["version_slug"] is None


def test_resume_refuses_to_overwrite_a_newer_night(temp_env):
    from helpers.version_manager import list_versions
    data = temp_env
    gen = _crashed_night(data)
    newer = "20260822-1900-ffffffff"
    (data / "optimizer" / newer).mkdir()
    pointer = {"generation": newer, "leaderboard": f"{newer}/leaderboard.json",
               "brief": "brief.md"}
    (data / "optimizer" / "latest.json").write_text(json.dumps(pointer),
                                                    encoding="utf-8")
    assert ob.run_resume(_resume_args(CRASHED_GEN), _log(data)) == 3
    assert list_versions(data) == []
    assert json.loads((data / "optimizer" / "latest.json")
                      .read_text(encoding="utf-8")) == pointer
    assert not (data / "optimizer" / "brief.md").exists()
    assert ob.read_state(gen.dir)["phase"] == ob.PHASE_ARMS_DONE  # untouched
    # an unknown generation is a clean "nothing to resume"
    assert ob.run_resume(_resume_args("20260801-0000-00000000"),
                         _log(data)) == 2


def test_resume_a_generation_without_state_json(temp_env, monkeypatch):
    """The real 2026-08-21 shape: leaderboard + candidates, no state.json,
    no frame/. Publishable; no late champion; the brief says the
    generation predates phase tracking."""
    from helpers.version_manager import list_versions
    data = temp_env
    gen = _crashed_night(data, state=False)
    shutil.rmtree(gen.dir / "frame")
    monkeypatch.setattr(
        ob, "run_arm", lambda *a, **k: pytest.fail("no frame -> no champion"))
    assert ob.run_resume(_resume_args(CRASHED_GEN), _log(data)) == 0
    vs = {v["slug"]: v for v in list_versions(data)}
    assert vs["overnight_best"]["run_id"] == "seed_2_204549"
    assert vs["overnight_runner_up"]["run_id"] == "cold_start_222906"
    brief = (data / "optimizer" / "brief.md").read_text(encoding="utf-8")
    assert "carries no state.json (it predates phase tracking)" in brief
    assert "3 arm(s), 3 scored, none published" in brief
    assert "no frame/ snapshot" in brief
    state = ob.read_state(gen.dir)
    assert state["phase"] == ob.PHASE_PUBLISHED
    assert state["resumed"]["from_phase"] == ob.PHASE_UNKNOWN
    assert state["resumed"]["last_heartbeat"] is None
    assert state["started"] == "2026-08-21T19:00:07"


def test_resume_reloads_every_generation_of_the_batch(temp_env, monkeypatch):
    """A 5am rotation closed generation A into B and the crash came after
    B's champion: resuming EITHER id reloads both, publishes across them
    (same-generation deltas intact), points latest.json at B and keeps
    the consolidation record B already made."""
    from helpers.version_manager import list_versions
    data = temp_env
    a = _mk_gen(data, gen_id="20260821-1900-aaaa1111",
                created="2026-08-21T19:00:07", baseline=66.0)
    a.candidates = [_mk_candidate(data, a, "seed_2_204549", 69.38),
                    _mk_candidate(data, a, "champion_n1_190030", 65.54)]
    b = _mk_gen(data, gen_id="20260822-0500-bbbb2222",
                created="2026-08-22T05:00:10", baseline=67.0)
    b.candidates = [_mk_candidate(data, b, "champion_5am_050010", 68.0)]
    for g in (a, b):
        ob.write_leaderboard(g)
        ob.write_frame(g)
    batch = [a.gen_id, b.gen_id]
    ob.write_state(a, ob.PHASE_CLOSED, batch=batch, closed_into=b.gen_id)
    ob.write_state(b, ob.PHASE_CONSOLIDATED, batch=batch,
                   consolidation={"status": "done", "note": "5am champion",
                                  "run_id": "champion_5am_050010"})
    monkeypatch.setattr(
        ob, "run_arm",
        lambda *a_, **k: pytest.fail("consolidation already recorded"))

    assert ob.run_resume(_resume_args(a.gen_id), _log(data)) == 0
    vs = {v["slug"]: v for v in list_versions(data)}
    assert vs["overnight_best"]["generation"] == a.gen_id
    assert vs["overnight_runner_up"]["generation"] == b.gen_id
    latest = json.loads((data / "optimizer" / "latest.json")
                        .read_text(encoding="utf-8"))
    assert latest["generation"] == b.gen_id
    assert [(r["generation"], r["delta_same_gen"])
            for r in latest["published"]] == [(a.gen_id, 3.38), (b.gen_id, 1.0)]
    for g in (a, b):
        st = ob.read_state(g.dir)
        assert st["phase"] == ob.PHASE_PUBLISHED
        assert st["batch"] == batch
        assert st["resumed"]["at"]
    assert ob.read_state(b.dir)["consolidation"]["run_id"] == \
        "champion_5am_050010"
    assert "consolidation" not in ob.read_state(a.dir)
    brief = (data / "optimizer" / "brief.md").read_text(encoding="utf-8")
    assert f"Generation {b.gen_id} stopped in phase 'consolidated'" in brief
    assert "already recorded (done: 5am champion)" in brief
    assert "Batch started 2026-08-21 19:00" in brief
    assert "2 generation(s), 3 arm(s) tried" in brief


def test_resume_honours_no_publish_and_dry_run(temp_env, monkeypatch):
    from helpers.version_manager import list_versions
    data = temp_env
    gen = _crashed_night(data)
    monkeypatch.setattr(ob, "generation_signature", lambda d: (1.0,))
    monkeypatch.setattr(
        ob, "run_arm", lambda *a, **k: pytest.fail("dry run: no champion"))
    assert ob.run_resume(_resume_args(CRASHED_GEN, dry_run=True),
                         _log(data)) == 0
    assert list_versions(data) == []
    brief = (data / "optimizer" / "brief.md").read_text(encoding="utf-8")
    assert "dry run: publish skipped" in brief
    assert "late champion consolidation skipped: dry run" in brief
    # the dry run finalized the ledger: nothing to resume twice, but a
    # --no-publish re-run is still safe
    assert ob.read_state(gen.dir)["phase"] == ob.PHASE_PUBLISHED
    assert ob.run_resume(_resume_args(CRASHED_GEN, no_publish=True),
                         _log(data)) == 0
    assert list_versions(data) == []
