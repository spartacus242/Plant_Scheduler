# tests/test_data_health.py — health engine (Command Center).

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helpers import data_health as dh
from helpers.config import datasources_config, load_toml

OK = dh.OK
STALE = dh.STALE
MISSING = dh.MISSING
ERROR = dh.ERROR
NOT_APPLICABLE = dh.NOT_APPLICABLE


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

RECV_XLSM = "Shipping Receiving Schedule NPA - 2024.xlsm"


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
    _touch(dd / 'reference' / 'demand_plan_summary.csv', 0.1)
    # VIF exports for the stock check (P1 live link) — anchored by ediact 3
    _touch(dd / "reference" / "ediact 3.csv", 0.1)
    # supply timeline feeds: IT's open-PO extract + the weekly receiving xlsm
    _touch(dd / "reference" / "open_pos.xlsx", 0.1)
    _touch(dd / "reference" / RECV_XLSM, 0.1)


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
    (dd / "reference" / "line_cip_hrs.csv").unlink()
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "line_cip_hrs")
    assert hit.state == MISSING
    assert hit.severity == 2


def test_corrupt_catalog_file(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    # force an ERROR via an empty-rows file
    empty = dd / "reference" / "downtimes.csv"
    empty.write_text("line_id,start_hour,end_hour\n", encoding="utf-8")
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "downtimes")
    assert hit.state == ERROR  # 0 rows


def test_cip_info_is_in_catalog_for_upload():
    """Data Files renders one upload slot per catalog entry — cip_info must be
    one of them (it was a live-feed-only file with no upload path)."""
    from helpers.data_catalog import by_key, missing_columns
    import pandas as pd
    spec = by_key("cip_info")
    assert spec is not None, "cip_info missing from CATALOG -> no upload slot"
    assert spec.filename == "cip_info.csv"
    df = pd.DataFrame(columns=["ID", "LineEquipment", "PreviousCIP",
                               "MaxHoursBetweenCIP", "ScheduledCIP", "Notes"])
    assert missing_columns(df, spec) == []  # upload validation passes


def test_cip_info_health_row_not_duplicated(tmp_path):
    """cip_info has a dedicated live-feed rule; the catalog loop must not emit
    a second row with the same key."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    health = dh.assess(dd, _cfg())
    rows = [h for h in health if h.key == "cip_info"]
    assert len(rows) == 1, f"expected exactly 1 cip_info row, got {len(rows)}"
    assert rows[0].source == "live_feed"


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
# demand baseline path config (P1 Dispatch 1 part C)
# ---------------------------------------------------------------------------

def test_datasources_config_demand_summary_csv(tmp_path):
    """demand_summary_csv defaults to '' and is honored when set in flowstate.toml."""
    ds_default = datasources_config({})
    assert ds_default["demand_summary_csv"] == ""

    toml_path = tmp_path / "flowstate.toml"
    toml_path.write_text(
        '[datasources]\ndemand_summary_csv = "C:/carsten/demand_plan_summary.csv"\n',
        encoding="utf-8")
    cfg = load_toml(toml_path)
    ds = datasources_config(cfg)
    assert ds["demand_summary_csv"] == "C:/carsten/demand_plan_summary.csv"


def test_demand_summary_health_row_cadence_and_configured_path(tmp_path):
    """Demand baseline health row uses the 168h weekly cadence and honors a
    configured datasources.demand_summary_csv path override."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    custom = tmp_path / "carsten_drop" / "demand_plan_summary.csv"
    custom.parent.mkdir(parents=True, exist_ok=True)
    _touch(custom, 0.1)
    cfg = _cfg()
    cfg["datasources"]["demand_summary_csv"] = str(custom)
    health = dh.assess(dd, cfg)
    hit = next(h for h in health if h.key == "demand_summary")
    assert hit.cadence_h == 168.0
    assert hit.state == OK  # uses the configured path, not the missing default


# ---------------------------------------------------------------------------
# supply timeline feeds: open-PO report + receiving xlsm (2026-09-01)
# ---------------------------------------------------------------------------

def test_default_cadences_include_supply_feeds():
    assert dh.DEFAULT_CADENCE_H["open_pos"] == 26.0
    assert dh.DEFAULT_CADENCE_H["receiving"] == 168.0


def test_open_pos_fresh_ok(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    health = dh.assess(dd, _cfg())
    rows = [h for h in health if h.key == "open_pos"]
    assert len(rows) == 1
    assert rows[0].state == OK
    assert rows[0].cadence_h == 26.0
    assert rows[0].actions == ()


def test_open_pos_stale_by_mtime(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _touch(dd / "reference" / "open_pos.xlsx", age_h=30.0)
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "open_pos")
    assert hit.state == STALE
    assert any("open_pos.xlsx" in a for a in hit.actions)


def test_open_pos_missing_names_bridge_file(tmp_path):
    """No dev fixture exists for this feed: absence is MISSING (blocking),
    never the STALE downgrade the VIF rule gives, and the action names the
    bridge delivery file so the planner knows what to ask IT for."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "open_pos.xlsx").unlink()
    (dd / "stockcheck").mkdir()  # a stockcheck dir alone must not downgrade
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "open_pos")
    assert hit.state == MISSING
    assert hit.severity == 2
    assert any("open_pos.xlsx" in a for a in hit.actions)


def test_open_pos_csv_fallback_ok(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "open_pos.xlsx").unlink()
    _touch(dd / "reference" / "open_pos.csv", 0.1)
    health = dh.assess(dd, _cfg())
    hit = next(h for h in health if h.key == "open_pos")
    assert hit.state == OK
    assert "open_pos.csv" in hit.detail


def test_open_pos_configured_path_override(tmp_path):
    """[datasources] po_report_path is authoritative: honored when it exists,
    reported MISSING (naming the configured path) when it does not — even
    though the bridge copy is present."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    custom = tmp_path / "it_drop" / "NPA Open POs -8.24.xlsx"
    _touch(custom, 0.1)
    cfg = _cfg()
    cfg["datasources"]["po_report_path"] = str(custom)
    hit = next(h for h in dh.assess(dd, cfg) if h.key == "open_pos")
    assert hit.state == OK
    assert custom.name in hit.detail

    cfg["datasources"]["po_report_path"] = str(tmp_path / "nowhere.xlsx")
    hit = next(h for h in dh.assess(dd, cfg) if h.key == "open_pos")
    assert hit.state == MISSING
    assert "nowhere.xlsx" in hit.detail


def test_open_pos_cadence_override_via_health_section(tmp_path):
    """[health] cadence_h overrides the code default, same mechanism as the
    other feeds."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _touch(dd / "reference" / "open_pos.xlsx", age_h=30.0)
    cfg = _cfg()
    cfg["health"] = {"cadence_h": {"open_pos": 48}}
    hit = next(h for h in dh.assess(dd, cfg) if h.key == "open_pos")
    assert hit.state == OK
    assert hit.cadence_h == 48


def test_receiving_fresh_ok(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    health = dh.assess(dd, _cfg())
    rows = [h for h in health if h.key == "receiving"]
    assert len(rows) == 1
    assert rows[0].state == OK
    assert rows[0].cadence_h == 168.0


def test_receiving_stale_weekly_cadence(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _touch(dd / "reference" / RECV_XLSM, age_h=200.0)
    hit = next(h for h in dh.assess(dd, _cfg()) if h.key == "receiving")
    assert hit.state == STALE
    # 30 h old is fine for a weekly file (must not inherit the VIF cadence)
    _touch(dd / "reference" / RECV_XLSM, age_h=30.0)
    hit = next(h for h in dh.assess(dd, _cfg()) if h.key == "receiving")
    assert hit.state == OK


def test_receiving_missing_vs_dev_fixture_downgrade(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / RECV_XLSM).unlink()
    hit = next(h for h in dh.assess(dd, _cfg()) if h.key == "receiving")
    assert hit.state == MISSING
    # the bundled dev fixture keeps the Receiving tab usable -> warn, not block
    _touch(dd / "stockcheck" / "dev_receiving_schedule.xlsm", 0.1)
    hit = next(h for h in dh.assess(dd, _cfg()) if h.key == "receiving")
    assert hit.state == STALE
    assert "dev fixture" in hit.detail


def test_datasources_config_po_report_path(tmp_path):
    assert datasources_config({})["po_report_path"] == ""
    toml_path = tmp_path / "flowstate.toml"
    toml_path.write_text(
        '[datasources]\npo_report_path = "C:/it/NPA Open POs.xlsx"\n',
        encoding="utf-8")
    ds = datasources_config(load_toml(toml_path))
    assert ds["po_report_path"] == "C:/it/NPA Open POs.xlsx"


def test_stock_config_defaults_and_override(tmp_path):
    from helpers.config import stock_config
    sc = stock_config({})
    assert sc["min_days_after_delivery"] == 4
    assert sc["lead_measured_from"] == "block_start"
    assert sc["receipt_ready_hour"] == 16
    assert sc["raw_qc_offset_h"] == 72
    assert sc["appt_ready_offset_h"] == 2
    assert sc["po_ignore_after_h"] == 168
    assert sc["landed_match_frac"] == 0.95
    assert sc["dependent_frac_floor"] == 0.05
    assert sc["hard_block"] is False
    assert sc["use_board_qty_kg"] is True
    assert sc["raw_areas"] == ["RB1", "AMB", "RC1"]
    assert sc["offsite_areas"] == ["SL3"]
    # list defaults are copies: mutating one result never leaks into the next
    sc["raw_areas"].append("XX")
    assert stock_config({})["raw_areas"] == ["RB1", "AMB", "RC1"]
    toml_path = tmp_path / "flowstate.toml"
    toml_path.write_text(
        '[stock]\nmin_days_after_delivery = 6\nhard_block = true\n'
        'offsite_areas = ["SL3", "SL4"]\n', encoding="utf-8")
    sc = stock_config(load_toml(toml_path))
    assert sc["min_days_after_delivery"] == 6
    assert sc["hard_block"] is True
    assert sc["offsite_areas"] == ["SL3", "SL4"]
    assert sc["receipt_ready_hour"] == 16  # untouched keys keep defaults


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
    """use_sku_rates=false with no data/reference/line_rates.csv -> STALE
    (solver falls back to per-SKU rates, disagreeing with the intended flat mode)."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    health = dh.assess(dd, _cfg(use_sku_rates=False))
    hit = next(h for h in health if h.key == "rate_mode")
    assert hit.state == STALE


def test_rate_mode_false_with_line_rates_ok(tmp_path):
    """use_sku_rates=false with data/reference/line_rates.csv present -> OK
    (the intended flat-rate mode: solver and scorecard both read line_rates.csv)."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    import pandas as pd
    pd.DataFrame([{"line_id": 0, "Line": "P09", "rate_kgph": 737}]).to_csv(
        dd / "reference" / "line_rates.csv", index=False)
    health = dh.assess(dd, _cfg(use_sku_rates=False))
    hit = next(h for h in health if h.key == "rate_mode")
    assert hit.state == OK


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


def _write_scorecard(dd: Path, scored_at: str, week_label: str = "Week-X",
                     composite: float = 50.0) -> None:
    name = f"{week_label}_{scored_at.replace(':', '').replace('-', '')}.json"
    (dd / "scorecards" / name).write_text(json.dumps({
        "week_label": week_label, "scored_at": scored_at,
        "composite": composite,
    }), encoding="utf-8")


def test_scorecard_scored_this_week_is_ok(tmp_path):
    """Scored this ISO week -> an explicit OK entry. The Home Track step read
    an ABSENT entry as 'no scorecard history yet' (walkthrough finding 9)."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    # anchor fixed at 2026-08-03 (ISO week 32 of 2026)
    _write_scorecard(dd, "2026-08-04T09:00:00", week_label="Week-2026-08-04")
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "scorecard"), None)
    assert hit is not None and hit.state == OK
    assert "Week-2026-08-04" in hit.detail


def test_scorecard_other_week_is_stale(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _write_scorecard(dd, "2026-07-20T09:00:00")
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "scorecard"), None)
    assert hit is not None and hit.state == STALE


def test_scorecard_same_week_number_prior_year_is_stale(tmp_path):
    """ISO week 32 of 2025 must not satisfy week 32 of 2026."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    _write_scorecard(dd, "2025-08-05T09:00:00")
    health = dh.assess(dd, _cfg())
    hit = next((h for h in health if h.key == "scorecard"), None)
    assert hit is not None and hit.state == STALE


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
    (dd / "reference" / "line_cip_hrs.csv").unlink()  # MISSING (blocking)
    _touch(dd / "reference" / "manprg.txt", 3.0)     # STALE
    _touch(dd / "reference" / "manprg2.txt", 3.0)
    actions = dh.next_actions(dh.assess(dd, _cfg()), limit=3)
    assert actions, "expected at least one action"
    assert "line_cip_hrs.csv" in actions[0]


def test_next_actions_dedupe(tmp_path):
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "line_cip_hrs.csv").unlink()
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
    (dd / "reference" / "line_cip_hrs.csv").unlink()
    counts = dh.summary(dh.assess(dd, _cfg()))
    assert counts[MISSING] >= 1


def test_open_pos_configured_directory_is_missing(tmp_path):
    """po_report_path pointing at IT's drop FOLDER (instead of the file)
    exists but is no report: MISSING, naming the path and the reason —
    never OK on the strength of the bridge copy sitting in data/reference."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    folder = tmp_path / "it_drop"
    folder.mkdir()
    cfg = _cfg()
    cfg["datasources"]["po_report_path"] = str(folder)
    hit = next(h for h in dh.assess(dd, cfg) if h.key == "open_pos")
    assert hit.state == MISSING and hit.severity == 2
    assert "it_drop" in hit.detail and "directory" in hit.detail


def test_open_pos_bridge_directory_is_missing(tmp_path):
    """A folder named open_pos.xlsx in data/reference is skipped by the
    resolver (is_file), so the row reads MISSING like an absent file."""
    dd = _empty_data_dir(tmp_path)
    _min_catalog(dd)
    (dd / "reference" / "open_pos.xlsx").unlink()
    (dd / "reference" / "open_pos.xlsx").mkdir()
    hit = next(h for h in dh.assess(dd, _cfg()) if h.key == "open_pos")
    assert hit.state == MISSING
    assert "open_pos.xlsx / open_pos.csv not found" in hit.detail
