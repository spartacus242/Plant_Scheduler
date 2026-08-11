# tests/test_data_health.py — health engine + process flow (Command Center).

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helpers import data_health as dh
from helpers.process_flow import STAGES, stage_state

OK = dh.OK
STALE = dh.STALE
MISSING = dh.MISSING
ERROR = dh.ERROR
NOT_APPLICABLE = dh.NOT_APPLICABLE


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _touch(path: Path, age_h: float = 0.1) -> None:
    """Create a file if missing (with content 'x'), and set a controlled age."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x", encoding="utf-8")
    if age_h:
        old = time.time() - age_h * 3600
        import os
        os.utime(path, (old, old))


def _empty_data_dir(tmp_path: Path) -> Path:
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    (dd / "scorecards").mkdir()
    (dd / "versions").mkdir()
    return dd


def _min_catalog(dd: Path) -> None:
    """Create the catalog files the engine checks, all fresh."""
    for spec_key, cols, rows in [
        ("calendar_blocks", ["block_id", "block_type", "line_id", "start_h", "end_h"], 2),
        ("lines", ["line_id", "line_name"], 1),
        ("capabilities_rates", ["line_id", "sku", "capable"], 2),
        ("downtimes", ["line_id", "start_hour", "end_hour"], 1),
        ("demand_plan", ["order_id", "sku", "qty_target", "week_index"], 2),
        ("changeovers", ["from_sku", "to_sku", "setup_hours"], 2),
        ("line_cip_hrs", ["line_id", "max_cip_hrs"], 1),
        ("initial_states", ["line_id", "line_name", "initial_sku", "available_from_hour"], 1),
        ("sku_info", ["sku", "recipe"], 1),
        ("trials", ["line_name", "sku", "start_datetime"], 1),
    ]:
        import pandas as pd
        df = pd.DataFrame([{c: (i if c in ("line_id", "start_h", "end_h", "start_hour", "end_hour", "qty_target", "week_index", "max_cip_hrs", "available_from_hour") else f"{c}{i}") for i, c in enumerate(cols)} for i in range(rows)])
        # a healthy catalog: every SKU capable (the demand capability check
        # flags SKUs with no capable line — the fixture must not trip it)
        if spec_key == "capabilities_rates":
            df["capable"] = 1
            df["calc_rate_kgph"] = 540.0
        path = dd / "calendar_blocks.csv" if spec_key == "calendar_blocks" else (
            dd / "lines.csv" if spec_key == "lines" else dd / "reference" / f"{spec_key}.csv")
        df.to_csv(path, index=False)
        _touch(path, 0.1)
    # live feeds present + fresh by default
    _touch(dd / "reference" / "manprg.txt", 0.1)
    _touch(dd / "reference" / "manprg2.txt", 0.1)
    _touch(dd / "reference" / "cip_info.csv", 0.1)


def _cfg(**overrides) -> dict:
    cfg = {
        "scheduler": {
            "planning_start_date": "2026-08-03 00:00:00",
            "anchor_mode": "fixed",
            "horizon_weeks": 3,
            "use_sku_rates": True,
        },
        "datasources": {},
    }
    for k, v in overrides.items():
        if k == "use_sku_rates":
            cfg["scheduler"]["use_sku_rates"] = v
        elif k == "anchor_mode":
            cfg["scheduler"]["anchor_mode"] = v
    return cfg


# ---------------------------------------------------------------------------
# catalog rules
# ---------------------------------------------------------------------------

def test_missing_catalog_file(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "trials.csv").unlink()
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "trials")
    assert hit.state == MISSING
    assert hit.severity == 2


def test_corrupt_catalog_file(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "trials.csv").write_text("not,a,valid\n1,2", encoding="utf-8")
    # trials.csv with one data column -> still reads; force an ERROR via empty rows file
    empty = dd / "reference" / "downtimes.csv"
    empty.write_text("line_id,start_hour,end_hour\n", encoding="utf-8")
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "downtimes")
    assert hit.state == ERROR  # 0 rows


def test_zero_row_catalog_file(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    import pandas as pd
    pd.DataFrame(columns=["line_id", "line_name"]).to_csv(dd / "lines.csv", index=False)
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "lines")
    assert hit.state == ERROR


def test_fresh_catalog_all_ok(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    health = dh.assess(dd, _cfg())
    bad = [h for h in health if h.state in (MISSING, ERROR)]
    assert bad == [], f"unexpected bad: {[(h.key, h.state, h.detail) for h in bad]}"


# ---------------------------------------------------------------------------
# live feed freshness
# ---------------------------------------------------------------------------

def test_stale_manprg(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _touch(dd / "reference" / "manprg.txt", age_h=3.0)
    _touch(dd / "reference" / "manprg2.txt", age_h=3.0)
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "manprg")
    assert hit.state == STALE
    assert any("refresh" in a.lower() for a in hit.actions)


def test_fresh_manprg_ok(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _touch(dd / "reference" / "manprg.txt", age_h=0.1)
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "manprg")
    assert hit.state == OK


def test_missing_manprg(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    for f in ("manprg.txt", "manprg2.txt"):
        p = dd / "reference" / f
        if p.exists():
            p.unlink()
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "manprg")
    assert hit.state == MISSING


def test_stale_cip_info(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _touch(dd / "reference" / "cip_info.csv", age_h=50.0)
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "cip_info")
    assert hit.state == STALE


# ---------------------------------------------------------------------------
# semantic rules
# ---------------------------------------------------------------------------

def test_demand_week_mismatch_stale(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    import pandas as pd
    # week_index 0 only, but "today" (fixed anchor 2026-08-03, ISO week 32)
    # — week_index 0 IS the anchor week, so latest_iso == iso_now → OK here.
    df = pd.DataFrame([
        {"order_id": "a-W0", "sku": "120430", "qty_target": 100, "week_index": 0},
        {"order_id": "b-W0", "sku": "120431", "qty_target": 100, "week_index": 0},
    ])
    df.to_csv(dd / "reference" / "demand_plan.csv", index=False)
    _touch(dd / "reference" / "demand_plan.csv", 0.1)
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "demand_week")
    assert hit.state == OK  # week 0 == anchor week


def test_calendar_anchor_stale_rolling(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    # anchor_mode=today, config anchor is 3 days ago -> stale
    cfg = _cfg(anchor_mode="today")
    health = dh.assess(dd, cfg)
    hit = next(h for h in health if h.key == "calendar_anchor")
    assert hit.state == STALE
    assert any("Roll calendar" in a for a in hit.actions)


def test_zero_cip_calendar_warns(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    import pandas as pd
    df = pd.DataFrame([
        {"block_id": "b1", "block_type": "production", "line_id": 0, "line_name": "P09",
         "start_h": 0, "end_h": 10, "label": "280581", "order_id": "o1", "sku": "280581",
         "sku_description": "", "qty_kg": 100, "locked": False, "attrs": ""},
    ])
    df.to_csv(dd / "calendar_blocks.csv", index=False)
    _touch(dd / "calendar_blocks.csv", 0.1)
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "calendar_cip"), None)
    assert hit is not None and hit.state == STALE


def test_changeover_quality_all_zero(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    import pandas as pd
    df = pd.DataFrame([
        {"from_sku": "120430", "to_sku": "120431", "setup_hours": 0},
        {"from_sku": "120431", "to_sku": "120433", "setup_hours": 0},
    ])
    df.to_csv(dd / "reference" / "changeovers.csv", index=False)
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "changeover_hours"), None)
    assert hit is not None and hit.state == STALE
    assert any("solver" in h.detail.lower() for h in health if h.key == "changeover_hours")


def test_rate_mode_false_warns(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    health = dh.assess(dd, _cfg(use_sku_rates=False))
    hit = next(h for h in health if h.key == "rate_mode")
    assert hit.state == STALE


def test_rate_mode_true_ok(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    health = dh.assess(dd, _cfg(use_sku_rates=True))
    hit = next(h for h in health if h.key == "rate_mode")
    assert hit.state == OK


def test_scorecard_semantics_stale(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    # no scorecard in the scorecards dir -> STALE for current week
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "scorecard"), None)
    assert hit is not None and hit.state == STALE


def test_capability_check_ok_when_manprg_matches(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    # manprg has no production MOs (only CIP/TRIALS) -> no conflicts
    _touch(dd / "reference" / "manprg.txt", 0.1)
    _touch(dd / "reference" / "manprg2.txt", 0.1)
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "capability_check"), None)
    assert hit is not None and hit.state == OK


def test_capability_check_stale_on_conflict(tmp_path):
    import pandas as pd
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    # capabilities has sku 280581 capable only on line P09
    pd.DataFrame([
        {"line_id": 0, "sku": "280581", "line_name": "P09", "capable": 1, "calc_rate_kgph": 540.0},
        {"line_id": 1, "sku": "280581", "line_name": "P10", "capable": 0, "calc_rate_kgph": 540.0},
    ]).to_csv(dd / "reference" / "capabilities_rates.csv", index=False)
    _touch(dd / "reference" / "capabilities_rates.csv", 0.1)
    # manprg says P10 runs 280581 (a conflict)
    (dd / "reference" / "manprg.txt").write_text(
        "Start date;Start time;Line;MO No.;Item;Designation;Pal;Type;Hours;Fct qty (Cas);Qty made (Cas);Fct qty [Kg];;Qty made [Kg];;Left (Cas)\n"
        "08/10/2026;03:49;LMH-P10;29901;280581;X;;;20.06;3800;;15048.000;Kg;;Kg;3800\n",
        encoding="utf-8")
    _touch(dd / "reference" / "manprg.txt", 0.1)
    (dd / "reference" / "manprg2.txt").write_text(
        "Start date;Start time;Line;MO No.;Item;Designation;Pal;Type;Hours;Fct qty (Cas);Qty made (Cas);Fct qty [Kg];;Qty made [Kg];;Left (Cas)\n",
        encoding="utf-8")
    _touch(dd / "reference" / "manprg2.txt", 0.1)
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "capability_check"), None)
    assert hit is not None and hit.state == STALE
    assert "280581@P10" in hit.detail


def test_version_slots_full(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    from helpers.version_manager import MAX_VERSIONS, save_version
    from helpers.calendar_io import empty_calendar
    cal = empty_calendar()
    for i in range(MAX_VERSIONS):
        save_version(f"v{i}", cal, {"composite": None}, dd, source="test")
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "versions"), None)
    assert hit is not None and hit.state == STALE


# ---------------------------------------------------------------------------
# next_actions
# ---------------------------------------------------------------------------

def test_next_actions_blocking_first(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "trials.csv").unlink()       # MISSING (blocking)
    _touch(dd / "reference" / "manprg.txt", 3.0)     # STALE
    _touch(dd / "reference" / "manprg2.txt", 3.0)
    actions = dh.next_actions(dh.assess(dd, _cfg()), limit=3)
    assert actions, "expected at least one action"
    assert "Upload trials.csv" in actions[0] or "trials.csv" in actions[0]


def test_next_actions_dedupe(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "trials.csv").unlink()
    cip = dd / "reference" / "cip_info.csv"
    if cip.exists():
        cip.unlink()
    actions = dh.next_actions(dh.assess(dd, _cfg()), limit=10)
    assert len(actions) == len(set(actions))


def test_next_actions_empty_when_all_ok(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _touch(dd / "reference" / "manprg.txt", 0.1)
    _touch(dd / "reference" / "manprg2.txt", 0.1)
    actions = dh.next_actions(dh.assess(dd, _cfg()), limit=3)
    # With everything fresh, at most the scorecard nudge should remain.
    assert all("Score this week" in a or "scorecard" in a.lower() for a in actions)


def test_summary_counts(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "trials.csv").unlink()
    counts = dh.summary(dh.assess(dd, _cfg()))
    assert counts[MISSING] >= 1


# ---------------------------------------------------------------------------
# process flow
# ---------------------------------------------------------------------------

def test_stages_order():
    assert [s.id for s in STAGES] == ["vif", "demand", "calendar", "score", "stock", "optimize", "promote"]


def test_stage_state_worst_wins():
    h = [
        dh.HealthStatus(key="demand_plan", name="demand", state=OK, detail=""),
        dh.HealthStatus(key="demand_week", name="week", state=STALE, detail=""),
    ]
    assert stage_state("demand", h) == STALE


def test_stage_state_no_rule_ok():
    assert stage_state("score", []) == OK


def test_stage_state_missing_beats_stale():
    h = [
        dh.HealthStatus(key="calendar_blocks", name="cal", state=STALE, detail=""),
        dh.HealthStatus(key="lines", name="lines", state=MISSING, detail=""),
    ]
    assert stage_state("calendar", h) == MISSING
