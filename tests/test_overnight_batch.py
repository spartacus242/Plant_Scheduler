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
    # champion noise x3 + ladder x2 survive; both default exploration arms gone
    assert kinds.count("noise") == 3
    assert kinds.count("ladder") == 2
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
    full = ob.default_portfolio({}, dry_run=False)
    assert [a["label"] for a in full] == [
        "champion_n1", "champion_n2", "champion_n3",
        "ladder_300_1800", "ladder_600_3600",
        "explore_co_x1_5", "explore_ffs_topload_x2"]
    dry = ob.default_portfolio({}, dry_run=True)
    assert [a["label"] for a in dry] == [
        "champion_n1", "champion_n2", "explore_co_x1_5"]
    assert all(a["budgets"] == ob.DRY_BUDGETS for a in dry)
    # exploration params scale the EFFECTIVE weights and stay ints
    x15 = next(a for a in full if a["label"] == "explore_co_x1_5")
    eff = ob.effective_co_weights({})
    assert x15["params"]["ffs_weight"] == int(round(eff["ffs_weight"] * 1.5))
    assert all(isinstance(v, int) for v in x15["params"].values())
    # F's built-in overrides are the champion baseline (ffs 600, topload 450)
    assert eff["ffs_weight"] == 600
    assert eff["topload_weight"] == 450


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
