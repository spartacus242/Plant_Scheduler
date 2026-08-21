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


def test_plant_chain_seed_carries_signature(tmp_path):
    donor = tmp_path / "F_overnight_seed_3"
    work = tmp_path / "F_overnight_chained_best"
    donor.mkdir()
    work.mkdir()
    (donor / "schedule_phase2.csv").write_text("line_id,order_id\n",
                                               encoding="utf-8")
    (donor / "feasibility_report.json").write_text(
        json.dumps({"relax_level": 0, "input_sig": "abc123"}),
        encoding="utf-8")
    notes = ob.plant_chain_seed(work, donor)
    assert (work / "prev_schedule.csv").read_text(encoding="utf-8") == \
        "line_id,order_id\n"
    # the donor's signature + relax level are CARRIED verbatim — the
    # solver's trust gates decide, the batch never fakes a signature
    planted = json.loads(
        (work / "prev_feasibility.json").read_text(encoding="utf-8"))
    assert planted == {"relax_level": 0, "input_sig": "abc123"}
    assert any("chain seed planted" in n for n in notes)

    # donor without a report: schedule planted, absence reported honestly
    (donor / "feasibility_report.json").unlink()
    notes2 = ob.plant_chain_seed(work, donor)
    assert any("WITHOUT a" in n for n in notes2)

    # donor without a schedule: nothing planted, cold reported
    (donor / "schedule_phase2.csv").unlink()
    notes3 = ob.plant_chain_seed(work, donor)
    assert any("solving cold" in n for n in notes3)


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


def _mk_gen(data: Path) -> "ob.Generation":
    from helpers.horizon import resolve as _hr
    from helpers.config import load_toml as _lt
    anchor = _hr(_lt()).anchor  # publish shifts vs the CURRENT anchor -> 0
    gen = ob.Generation(
        gen_id="20260820-1900-abcd1234", sig=(1.0,),
        created="2026-08-20T19:00:00", anchor=anchor, horizon_h=504.0,
        week_marks=[(0.0, 34)], gates={"P09": 10.0},
        demand=pd.DataFrame([{"order_id": "O1", "sku": "111",
                              "qty_target": 1000.0, "lower_pct": 0.9,
                              "upper_pct": 1.1, "due_start_hour": 0,
                              "due_end_hour": 167}]),
        net_demand_kg=1000.0, capacity_bound=5000.0, capacity_detail={},
        co_map={}, board_baseline=None, board_note="",
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
