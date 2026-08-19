# tests/test_scorecard.py -- scorecard honesty (P3 dispatch 1, charter M4).
#
# Three silent-lie defects are pinned here, plus the loader change that made
# the first of them visible:
#
#   M4a  Service ignored produced kg. score_service only populated `produced`
#        when the calendar carried a non-null qty_kg column, so the excess-kg
#        component silently dropped out of the Service average and the score
#        was late/at-risk only. It now ESTIMATES kg from run_hours x the
#        line's average rate and flags the estimate.
#   M4b  Trials scored 100 on absent data. trial_hours=0 with no trial blocks
#        AND no reference/trials.csv is "no trial data", not "a perfectly
#        trial-free week". The category is now None (n/a) in that case.
#   M4c  load_calendar coerced a missing qty_kg to 0.0, which is
#        indistinguishable from "produced nothing" -- so M4a's null path could
#        never fire from the app. The loader now keeps NaN.
#
# Plus a regression on the CIP compliance gate (0 overdue -> 100, 1 -> 0),
# which this dispatch must NOT change.
#
# Design notes
#   * Synthetic calendars only -- no live inputs, no solve. Config is
#     scorecard_config({}), i.e. the v0 defaults, so a cap edit in
#     flowstate.toml cannot make these flaky.
#   * The solver's row writer is covered at the level that is separable:
#     _reconcile_row_qty_kg (pure). A real solve is 60-600 s and never runs
#     inside a test -- the written CSV is verified out of band.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "code") not in sys.path:
    sys.path.insert(0, str(ROOT / "code"))

from helpers.calendar_io import (  # noqa: E402
    CALENDAR_COLUMNS,
    calendar_to_gantt_payload,
    gantt_payload_to_calendar,
    import_solver_schedule,
    load_calendar,
    save_calendar,
)
from helpers.config import scorecard_config  # noqa: E402
from helpers.scorecard_engine import (  # noqa: E402
    category_scores,
    score_calendar,
    score_campaigns,
    score_changeovers,
    score_cip,
    score_service,
    score_trials,
)

CFG = scorecard_config({})


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def _block(**kw) -> dict:
    row = {
        "block_id": kw.get("block_id", "b1"),
        "block_type": kw.get("block_type", "production"),
        "line_id": kw.get("line_id", 9),
        "line_name": kw.get("line_name", "P09"),
        "start_h": float(kw.get("start_h", 0.0)),
        "end_h": float(kw.get("end_h", 10.0)),
        "label": kw.get("label", "S1"),
        "order_id": kw.get("order_id", "O1"),
        "sku": kw.get("sku", "S1"),
        "sku_description": kw.get("sku_description", ""),
        "qty_kg": kw.get("qty_kg", None),
        "locked": False,
        "attrs": "",
    }
    return row


def _calendar(blocks: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(blocks, columns=CALENDAR_COLUMNS)
    df["qty_kg"] = pd.to_numeric(df["qty_kg"], errors="coerce").astype(float)
    return df


def _demand(qty_max: float = 8000.0, due_end_hour: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame([{
        "order_id": "O1",
        "sku": "S1",
        "week_index": 0,
        "due_end_hour": due_end_hour,
        "qty_max": qty_max,
    }])


def _two_prod_blocks(qty_a=None, qty_b=None) -> list[dict]:
    """20 production hours for order O1 on P09, split in two 10 h blocks."""
    return [
        _block(block_id="b1", start_h=0, end_h=10, qty_kg=qty_a),
        _block(block_id="b2", start_h=20, end_h=30, qty_kg=qty_b),
    ]


def _raw(calendar: pd.DataFrame, *, intervals=None, rates=None,
         demand=None, data_dir=None) -> dict:
    """The five raw metric blocks category_scores() consumes."""
    return {
        "changeovers": score_changeovers(calendar, CFG, {}),
        "cip": score_cip(calendar, CFG, intervals or {}, rates or {}),
        "trials": score_trials(calendar, {}),
        "campaigns": score_campaigns(calendar, CFG),
        "service": score_service(calendar, CFG, demand, data_dir, rates),
    }


# --------------------------------------------------------------------------
# M4a -- Service excess kg: real, estimated, or honestly None
# --------------------------------------------------------------------------
def test_service_uses_real_qty_kg_when_present():
    """6000 + 4000 kg produced against a qty_max of 8000 -> 2000 kg excess,
    and the estimated flag is False because these are measured kg."""
    cal = _calendar(_two_prod_blocks(6000.0, 4000.0))
    svc = score_service(cal, CFG, _demand(qty_max=8000.0))

    assert svc["available"] is True
    assert svc["excess_inventory_kg"] == 2000.0
    assert svc["excess_inventory_kg_estimated"] is False


def test_service_estimates_kg_when_qty_is_absent():
    """No qty_kg at all -> estimate 20 h x 1000 kg/h = 20000 kg produced, so
    excess = 20000 - 8000 = 12000 kg, flagged as an estimate."""
    cal = _calendar(_two_prod_blocks(None, None))
    svc = score_service(cal, CFG, _demand(qty_max=8000.0), None, {"P09": 1000.0})

    assert svc["excess_inventory_kg_estimated"] is True
    assert svc["excess_inventory_kg"] == pytest.approx(12000.0, rel=0.01)


def test_service_estimates_kg_when_qty_is_all_zero():
    """All-zero qty_kg is the shape a fillna(0) loader used to produce. It is
    NOT evidence of zero production, so it takes the estimate path too."""
    cal = _calendar(_two_prod_blocks(0.0, 0.0))
    svc = score_service(cal, CFG, _demand(qty_max=8000.0), None, {"P09": 1000.0})

    assert svc["excess_inventory_kg_estimated"] is True
    assert svc["excess_inventory_kg"] == pytest.approx(12000.0, rel=0.01)


def test_service_excess_is_none_when_no_kg_and_no_rates():
    """With neither measured kg nor a rate table there is nothing to report --
    None (dropped from the average), never a flattering 0."""
    cal = _calendar(_two_prod_blocks(None, None))
    svc = score_service(cal, CFG, _demand(qty_max=8000.0))

    assert svc["excess_inventory_kg"] is None
    assert svc["excess_inventory_kg_estimated"] is False


def test_service_kg_component_is_actually_scored():
    """The point of the estimate: the kg part must reach the Service average.
    Excess 12000 kg against cap_excess_kg=50000 scores 76, so the category is
    (100 + 100 + 76) / 3 = 92, not the 100 it used to get by dropping kg."""
    cal = _calendar(_two_prod_blocks(None, None))
    rates = {"P09": 1000.0}
    cats = category_scores(_raw(cal, rates=rates, demand=_demand()), CFG)

    assert cats["service"] == pytest.approx(92.0, abs=0.05)


def test_service_is_na_without_demand():
    cal = _calendar(_two_prod_blocks(6000.0, 4000.0))
    svc = score_service(cal, CFG, None)

    assert svc["available"] is False
    assert svc["excess_inventory_kg"] is None
    assert category_scores(_raw(cal), CFG)["service"] is None


# --------------------------------------------------------------------------
# M4b -- Trials availability
# --------------------------------------------------------------------------
def test_trials_category_is_none_when_there_is_no_trial_input(tmp_path):
    """No trial blocks in the calendar -> the category is n/a.
    The old behaviour scored this 100 and fed it into the composite."""
    cal = _calendar([_block(start_h=0, end_h=10, qty_kg=1000.0)])
    result = score_calendar(cal, week_label="t", data_dir=tmp_path, cfg=CFG)

    assert result.trials["available"] is False
    assert result.category_scores["trials"] is None
    # and it survives the JSON round trip as a real null
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["category_scores"]["trials"] is None
    assert "trials" not in {k for k, v in payload["category_scores"].items()
                            if v == 100.0}


def test_trials_category_is_scored_when_the_calendar_has_trial_blocks():
    """A trial block in the calendar is trial data — the only source since
    reference/trials.csv was retired (manprg TRIALS pseudo-MOs became
    blocked line time, 2026-08-14)."""
    cal = _calendar([
        _block(block_id="p1", start_h=0, end_h=10, qty_kg=1000.0),
        _block(block_id="t1", block_type="trial", start_h=12, end_h=36,
               order_id="", sku="TRIAL"),
    ])
    tr = score_trials(cal, {})

    assert tr["available"] is True
    assert tr["trial_hours"] == 24.0
    assert category_scores(_raw(cal), CFG)["trials"] is not None


# --------------------------------------------------------------------------
# CIP compliance gate -- regression, this dispatch must not move it
# --------------------------------------------------------------------------
def test_cip_category_is_100_when_no_line_is_overdue():
    """100 h of production against the 120 h fallback interval (+12 h grace)
    is compliant, so the CIP category is 100."""
    cal = _calendar([_block(start_h=0, end_h=100, qty_kg=1000.0)])
    raw = _raw(cal)

    assert raw["cip"]["cip_overdue"] == 0
    assert category_scores(raw, CFG)["cip"] == 100.0


def test_cip_category_collapses_to_0_on_a_single_overdue_line():
    """200 h without a clean blows the 120 h interval -- cap_cip_overdue is 1,
    so ONE overdue event drops the whole category to 0."""
    cal = _calendar([_block(start_h=0, end_h=200, qty_kg=1000.0)])
    raw = _raw(cal)

    assert raw["cip"]["cip_overdue"] == 1
    assert category_scores(raw, CFG)["cip"] == 0.0


# --------------------------------------------------------------------------
# cip_count is REPORTED ONLY (2026-08-19): every CIP is mandated by cip_info,
# so the count is the same on any compliant schedule and scores nothing.
# --------------------------------------------------------------------------
def test_cip_count_does_not_move_the_cip_score():
    cal = _calendar([_block(start_h=0, end_h=100, qty_kg=1000.0)])
    raw = _raw(cal)
    raw["cip"]["cip_count"] = 999  # would zero the old count-scored category

    assert category_scores(raw, CFG)["cip"] == 100.0


def test_cip_count_has_no_cap_wiring():
    from helpers.scorecard_engine import contribution_breakdown, metric_docs

    assert "cap_cip_count" not in CFG
    docs = metric_docs(CFG)
    assert docs["cip_count"]["cap_key"] is None
    assert docs["cip_count"]["cap_label"] == "n/a"

    data = {
        "category_scores": {"cip": 100.0},
        "cip": {"cip_count": 999, "cip_hours": 6.0, "cip_forfeited_kg": 0.0},
    }
    rows = contribution_breakdown(data, CFG)
    cip_row = next(r for r in rows if r["category"] == "cip")
    assert "cip_count" not in cip_row["cap_saturation"]


def test_cip_no_data_gate_keys_off_hours_and_kg_not_count():
    """Zero CIP blocks must still gray the category out on the scorecard —
    the gate now reads cip_hours / cip_forfeited_kg, not the retired count."""
    from helpers.scorecard_ui import _category_has_data

    assert not _category_has_data(
        "cip", {"cip": {"cip_count": 0, "cip_hours": 0,
                        "cip_forfeited_kg": 0}})
    assert _category_has_data("cip", {"cip": {"cip_hours": 6.0}})
    assert _category_has_data(
        "cip", {"cip": {"cip_hours": 0, "cip_forfeited_kg": 120.0}})


# --------------------------------------------------------------------------
# M4c -- the loader must not invent kg
# --------------------------------------------------------------------------
def test_load_calendar_keeps_missing_qty_kg_as_nan(tmp_path):
    path = tmp_path / "calendar_blocks.csv"
    path.write_text(
        "block_id,block_type,line_id,line_name,start_h,end_h,label,order_id,"
        "sku,sku_description,qty_kg,locked,attrs\n"
        "b1,production,9,P09,0,10,S1,O1,S1,,,False,\n"
        "b2,production,9,P09,20,30,S1,O1,S1,,4000,False,\n",
        encoding="utf-8",
    )
    cal = load_calendar(path)

    assert cal["qty_kg"].dtype == float
    assert pd.isna(cal["qty_kg"].iloc[0]), "missing kg must stay NaN, never 0.0"
    assert cal["qty_kg"].iloc[1] == 4000.0
    assert not (cal["qty_kg"].fillna(-1) == 0).any()


def test_missing_qty_kg_survives_a_save_load_round_trip(tmp_path):
    path = tmp_path / "calendar_blocks.csv"
    save_calendar(_calendar(_two_prod_blocks(None, 4000.0)), path)
    cal = load_calendar(path)

    assert pd.isna(cal["qty_kg"].iloc[0])
    assert cal["qty_kg"].iloc[1] == 4000.0


def test_loader_nan_reaches_the_service_estimate_path(tmp_path):
    """End to end for the browser repro: a schedule with no kg, loaded through
    load_calendar, must be ESTIMATED (not read as zero excess)."""
    ref = tmp_path / "reference"
    ref.mkdir(parents=True)
    (ref / "capabilities_rates.csv").write_text(
        "line_id,line_name,sku,capable,calc_rate_kgph\n9,P09,S1,1,1000\n",
        encoding="utf-8")
    (ref / "demand_plan.csv").write_text(
        "order_id,sku,week_index,due_end_hour,qty_max\nO1,S1,0,100,8000\n",
        encoding="utf-8")

    path = tmp_path / "calendar_blocks.csv"
    save_calendar(_calendar(_two_prod_blocks(None, None)), path)
    result = score_calendar(load_calendar(path), week_label="t",
                            data_dir=tmp_path, cfg=CFG)

    assert result.service["excess_inventory_kg_estimated"] is True
    assert result.service["excess_inventory_kg"] == pytest.approx(12000.0, rel=0.01)
    assert result.category_scores["service"] is not None


# --------------------------------------------------------------------------
# importer -- carry the solver's kg into the calendar
# --------------------------------------------------------------------------
def _write_schedule(path: Path, with_qty: bool) -> None:
    cols = ["line_id", "line_name", "order_id", "sku", "sku_description",
            "start_hour", "end_hour", "run_hours", "is_trial"]
    rows = [
        [9, "P09", "O1", "S1", "", 0, 10, 10, False],
        [9, "P09", "O1", "S1", "", 20, 30, 10, False],
    ]
    if with_qty:
        cols.insert(8, "qty_kg")
        rows[0].insert(8, 6000)
        rows[1].insert(8, 4000)
    pd.DataFrame(rows, columns=cols).to_csv(path, index=False)


def test_import_solver_schedule_carries_qty_kg(tmp_path):
    sched = tmp_path / "schedule_phase2.csv"
    _write_schedule(sched, with_qty=True)

    cal = import_solver_schedule(sched)

    assert list(cal["qty_kg"]) == [6000.0, 4000.0]


def test_import_solver_schedule_keeps_none_when_the_column_is_absent(tmp_path):
    """Pre-P3 schedules have no qty column; the importer must not invent one."""
    sched = tmp_path / "schedule_phase2.csv"
    _write_schedule(sched, with_qty=False)

    cal = import_solver_schedule(sched)

    assert cal["qty_kg"].isna().all()


# --------------------------------------------------------------------------
# Gantt round trip -- the other place kg can be silently invented or lost
# --------------------------------------------------------------------------
def test_gantt_round_trip_keeps_real_kg_and_never_writes_zero():
    """Drag/drop rebuilds the calendar from the component payload. Real kg must
    survive it, and the component's 0 placeholder must not land in the data
    model as 'produced nothing'."""
    cal = _calendar(_two_prod_blocks(6000.0, None))
    schedule, windows = calendar_to_gantt_payload(cal)

    assert [b["qty_kg"] for b in schedule] == [6000.0, None]

    schedule[1]["qty_kg"] = 0  # what useScheduleState.addToHolding defaults to
    back = gantt_payload_to_calendar(schedule, windows)

    assert back["qty_kg"].iloc[0] == 6000.0
    assert pd.isna(back["qty_kg"].iloc[1]), "a 0 placeholder must not become 0 kg"


# --------------------------------------------------------------------------
# solver row writer -- the separable half
# --------------------------------------------------------------------------
def _reconcile():
    # phase2_scheduler pulls in ortools; skip rather than fail if it is absent.
    pytest.importorskip("ortools")
    for _p in (str(ROOT / "code"), str(ROOT / "code" / "solver")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from phase2_scheduler import _reconcile_row_qty_kg

    return _reconcile_row_qty_kg


def test_row_qty_splits_the_solver_total_by_run_hours_when_the_rate_is_missing():
    reconcile = _reconcile()
    rows = [
        {"order_id": "A", "run_hours": 10, "qty_kg": 0},
        {"order_id": "A", "run_hours": 30, "qty_kg": 0},
    ]
    reconcile(rows, {"A": 4000.0})

    assert [r["qty_kg"] for r in rows] == [1000.0, 3000.0]


def test_row_qty_totals_match_the_solver_produced_value():
    reconcile = _reconcile()
    rows = [
        {"order_id": "A", "run_hours": 10, "qty_kg": 1000},
        {"order_id": "A", "run_hours": 30, "qty_kg": 3000},
        {"order_id": "B", "run_hours": 5, "qty_kg": 7},
    ]
    reconcile(rows, {"A": 4001.0, "B": 700.0})

    a = [r["qty_kg"] for r in rows if r["order_id"] == "A"]
    b = [r["qty_kg"] for r in rows if r["order_id"] == "B"]
    assert sum(a) == pytest.approx(4001.0, abs=0.11)
    assert sum(b) == pytest.approx(700.0, abs=0.11)


def test_reconcile_is_a_noop_without_a_produced_map():
    reconcile = _reconcile()
    rows = [{"order_id": "A", "run_hours": 10, "qty_kg": 123}]
    reconcile(rows, {})

    assert rows[0]["qty_kg"] == 123
