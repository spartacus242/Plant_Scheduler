# tests/test_reconcile.py — Reconcile engine (P4 slice 2).
#
# Every rule is tested PURE (frames/objects in, findings out). assess_plan is
# smoke-tested against a self-contained tmp_path data dir — never against
# data/reference (the no_live_data_reads guard enforces that repo-wide).

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.reconcile_engine import (  # noqa: E402
    BLOCKING,
    CIP,
    COVERAGE,
    DATA,
    FIT,
    INFO,
    STOCK,
    WARN,
    capability_findings,
    cip_findings,
    coverage_findings,
    fit_findings,
    sort_findings,
    stock_findings,
    summary,
)

ANCHOR = datetime(2026, 8, 10, 0, 0, 0)


def _cal(rows: list[dict]) -> pd.DataFrame:
    base = {
        "block_id": "b", "block_type": "production", "line_id": 0,
        "line_name": "P09", "start_h": 0.0, "end_h": 10.0, "label": "",
        "order_id": "", "sku": "", "sku_description": "", "qty_kg": None,
        "locked": False, "attrs": "",
    }
    out = []
    for i, r in enumerate(rows):
        row = dict(base)
        row.update(r)
        row["block_id"] = row["block_id"] if row["block_id"] != "b" else f"b{i}"
        out.append(row)
    df = pd.DataFrame(out, columns=list(base.keys()))
    df["qty_kg"] = pd.to_numeric(df["qty_kg"], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# COVERAGE
# ---------------------------------------------------------------------------

def _demand(rows: list[dict]) -> pd.DataFrame:
    base = {"order_id": "X-W0", "sku": "X", "week_index": 0,
            "qty_target": 10000, "lower_pct": 0.9, "upper_pct": 1.1,
            "due_start_hour": 0, "due_end_hour": 167, "priority": 3}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_coverage_zero_scheduled_week1_is_blocking():
    cal = _cal([])
    dem = _demand([{"order_id": "111-W0", "sku": "111", "qty_target": 50000,
                    "due_start_hour": 0}])
    f = coverage_findings(cal, dem)
    assert any(x.severity == BLOCKING and x.category == COVERAGE for x in f)
    blk = next(x for x in f if x.severity == BLOCKING)
    assert "111-W0" in blk.detail


def test_coverage_under_minimum_is_one_summary_warning():
    cal = _cal([
        {"order_id": "111-W1", "sku": "111", "qty_kg": 40000.0},
        {"order_id": "222-W1", "sku": "222", "qty_kg": 1000.0,
         "line_name": "P10", "start_h": 20, "end_h": 30},
    ])
    dem = _demand([
        {"order_id": "111-W1", "sku": "111", "qty_target": 80000,
         "due_start_hour": 168},
        {"order_id": "222-W1", "sku": "222", "qty_target": 9000,
         "due_start_hour": 168},
    ])
    f = coverage_findings(cal, dem)
    warns = [x for x in f if x.severity == WARN and x.category == COVERAGE]
    assert len(warns) == 1  # one summary, not one per order
    assert "2 order(s)" in warns[0].title
    total = warns[0].context["total_missing_kg"]
    assert total == pytest.approx((80000 * 0.9 - 40000) + (9000 * 0.9 - 1000))


def test_coverage_met_orders_are_silent():
    cal = _cal([{"order_id": "111-W0", "sku": "111", "qty_kg": 45000.0}])
    dem = _demand([{"order_id": "111-W0", "sku": "111", "qty_target": 50000}])
    assert coverage_findings(cal, dem) == []


def test_coverage_excludes_past_iso_weeks_and_labels_survivors():
    # Demand file self-anchored to Monday of ISO W33 (2026-08-10); "now" is
    # Tuesday of W34. The W33 order is a miss for the netting summary, not a
    # "plan me now" item (walkthrough 2026-08-17: W32 orders flagged urgent
    # in W34); survivors carry their TRUE ISO week, not the raw -W<k> suffix.
    anchor = datetime(2026, 8, 10)          # ISO W33 Monday
    now = datetime(2026, 8, 18, 9, 0)       # ISO W34 Tuesday
    dem = _demand([
        {"order_id": "111-W0", "sku": "111", "qty_target": 50000,
         "due_start_hour": 0, "due_end_hour": 167},      # W33 — past
        {"order_id": "222-W1", "sku": "222", "qty_target": 9000,
         "due_start_hour": 168, "due_end_hour": 335},    # W34 — this week
        {"order_id": "333-W2", "sku": "333", "qty_target": 9000,
         "due_start_hour": 336, "due_end_hour": 503},    # W35 — next week
    ])
    f = coverage_findings(_cal([]), dem, demand_anchor=anchor, now=now)
    blk = [x for x in f if x.severity == BLOCKING]
    assert len(blk) == 1
    assert "this week (W34)" in blk[0].title
    assert "222-W34" in blk[0].detail        # ISO label, not "222-W1"
    assert "111" not in blk[0].detail        # past order excluded
    warns = [x for x in f if x.severity == WARN]
    assert len(warns) == 1 and "333-W35" in warns[0].detail
    past = [x for x in f if x.key == "coverage_past_weeks"]
    assert len(past) == 1 and past[0].severity == INFO
    assert "1 unmet" in past[0].title


def test_coverage_week_key_falls_back_to_week_index():
    anchor = datetime(2026, 8, 10)          # ISO W33 Monday
    now = datetime(2026, 8, 18, 9, 0)       # ISO W34
    dem = pd.DataFrame([  # no due window columns at all
        {"order_id": "111-W0", "sku": "111", "week_index": 0,
         "qty_target": 50000, "lower_pct": 0.9},
        {"order_id": "222-W1", "sku": "222", "week_index": 1,
         "qty_target": 9000, "lower_pct": 0.9},
    ])
    f = coverage_findings(_cal([]), dem, demand_anchor=anchor, now=now)
    blk = [x for x in f if x.severity == BLOCKING]
    assert len(blk) == 1 and "222-W34" in blk[0].detail
    assert not any("111" in x.detail for x in blk)


def test_coverage_without_anchor_keeps_legacy_hour_window_behavior():
    # Pure-frame callers (staging tests) pass no anchor: nothing is excluded
    # and the raw order ids stay untouched.
    dem = _demand([{"order_id": "111-W0", "sku": "111", "qty_target": 50000,
                    "due_start_hour": 0}])
    f = coverage_findings(_cal([]), dem)
    blk = [x for x in f if x.severity == BLOCKING]
    assert len(blk) == 1 and "111-W0" in blk[0].detail
    assert "week 1" in blk[0].title
    assert not any(x.key == "coverage_past_weeks" for x in f)


def test_coverage_unknown_kg_is_reported_not_counted():
    cal = _cal([{"order_id": "111-W0", "sku": "111", "qty_kg": None}])
    dem = _demand([{"order_id": "111-W0", "sku": "111", "qty_target": 50000}])
    f = coverage_findings(cal, dem)
    assert any(x.severity == INFO and "unknown kg" in x.title for x in f)
    # the NaN block contributed 0 -> the order reads as uncovered (blocking)
    assert any(x.severity == BLOCKING for x in f)


# ---------------------------------------------------------------------------
# CIP
# ---------------------------------------------------------------------------

def _cip_line(prev_h_ago: float, max_h: float, sched_in_h: float | None,
              now: datetime):
    prev = now - timedelta(hours=prev_h_ago)
    return SimpleNamespace(
        previous_cip=pd.Timestamp(prev),
        scheduled_cip=(pd.Timestamp(now + timedelta(hours=sched_in_h))
                       if sched_in_h is not None else None),
        max_hours_between=max_h,
    )


def test_cip_overdue_now_is_blocking():
    now = ANCHOR + timedelta(hours=100)
    by_line = {"P10": _cip_line(150, 120, None, now)}
    f = cip_findings(by_line, _cal([]), anchor=ANCHOR, now=now, horizon_h=504)
    assert [x.severity for x in f] == [BLOCKING]
    assert "PAST its CIP interval" in f[0].title


def test_cip_due_soon_unscheduled_warns():
    now = ANCHOR + timedelta(hours=100)
    by_line = {"P09": _cip_line(100, 120, None, now)}  # due in 20h
    f = cip_findings(by_line, _cal([]), anchor=ANCHOR, now=now, horizon_h=504)
    assert [x.severity for x in f] == [WARN]
    assert "P09" in f[0].title


def test_cip_due_soon_with_calendar_cip_is_silent():
    now = ANCHOR + timedelta(hours=100)
    by_line = {"P09": _cip_line(100, 120, None, now)}
    # calendar CIP block before the due moment (prev at h0 + 120h max -> h120)
    cal = _cal([{"block_type": "cip", "sku": "CIP", "start_h": 110.0,
                 "end_h": 116.0}])
    assert cip_findings(by_line, cal, anchor=ANCHOR, now=now, horizon_h=504) == []


def test_cip_due_soon_with_scheduled_cip_is_silent():
    now = ANCHOR + timedelta(hours=100)
    by_line = {"P09": _cip_line(100, 120, 10, now)}  # scheduled within limit
    assert cip_findings(by_line, _cal([]), anchor=ANCHOR, now=now,
                        horizon_h=504) == []


def test_cip_healthy_line_is_silent():
    now = ANCHOR + timedelta(hours=100)
    by_line = {"P09": _cip_line(10, 120, None, now)}  # due in 110h
    assert cip_findings(by_line, _cal([]), anchor=ANCHOR, now=now,
                        horizon_h=504) == []


# ---------------------------------------------------------------------------
# CAPABILITY
# ---------------------------------------------------------------------------

def _caps(rows: list[tuple[str, str, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"sku": s, "line_name": ln, "capable": c, "calc_rate_kgph": 700}
         for s, ln, c in rows])


def test_scheduled_incapable_pair_is_blocking():
    caps = _caps([("111", "P09", 1)])
    cal = _cal([{"sku": "222", "line_name": "P09"}])
    f = capability_findings(caps, cal)
    assert [x.severity for x in f] == [BLOCKING]
    assert "222" in f[0].title and "P09" in f[0].title


def test_scheduled_capable_pair_is_silent():
    caps = _caps([("111", "P09", 1)])
    cal = _cal([{"sku": "111", "line_name": "P09"}])
    assert capability_findings(caps, cal) == []


def test_side_block_inherits_group_capability():
    caps = _caps([("111", "P17", 1)])
    cal = _cal([{"sku": "111", "line_name": "P17A"}])
    assert capability_findings(caps, cal) == []


def test_manprg_conflicts_surface_as_warning():
    from helpers.capability_check import (CapabilityCheckResult,
                                          CapabilityConflict)
    res = CapabilityCheckResult(conflicts=[
        CapabilityConflict(sku="333", line_name="P11", mo="M1",
                           kind="SKU_MISSING")])
    f = capability_findings(_caps([("111", "P09", 1)]), _cal([]), res)
    assert [x.severity for x in f] == [WARN]
    assert "ground truth" in f[0].detail


# ---------------------------------------------------------------------------
# FIT
# ---------------------------------------------------------------------------

def test_overlapping_blocks_are_blocking():
    cal = _cal([
        {"sku": "111", "start_h": 0, "end_h": 10},
        {"sku": "222", "start_h": 8, "end_h": 20},
    ])
    f = fit_findings(cal)
    assert any(x.key == "fit_overlaps" and x.severity == BLOCKING for x in f)


def test_production_over_downtime_is_blocking():
    cal = _cal([{"sku": "111", "line_name": "P11", "start_h": 5, "end_h": 15}])
    downtimes = pd.DataFrame([{"line_id": 2, "line_name": "P11",
                               "start_hour": 0, "end_hour": 504,
                               "reason": "Down"}])
    f = fit_findings(cal, downtimes)
    assert any(x.key == "fit_downtime" and x.severity == BLOCKING for x in f)


def test_block_past_horizon_warns():
    cal = _cal([{"sku": "111", "start_h": 500, "end_h": 520}])
    f = fit_findings(cal, None, horizon_h=504)
    assert any(x.key == "fit_horizon" and x.severity == WARN for x in f)


def test_clean_calendar_is_silent():
    cal = _cal([
        {"sku": "111", "start_h": 0, "end_h": 10},
        {"sku": "222", "start_h": 10, "end_h": 20},
    ])
    assert fit_findings(cal, None, horizon_h=504) == []


# ---------------------------------------------------------------------------
# STOCK (pure transform of a stock_check_report dict)
# ---------------------------------------------------------------------------

def test_stock_do_not_schedule_is_blocking():
    report = {"schedule_view": [{
        "block_id": "b1", "sku": "111", "line_name": "P09",
        "start_h": 0, "qty_kg": 5000, "status": "DO_NOT_SCHEDULE",
        "items": [{"primary_item": "RM1", "need": 100, "ratio": 0.2,
                   "status": "DO_NOT_SCHEDULE"}],
    }]}
    f = stock_findings(report)
    assert [x.severity for x in f] == [BLOCKING]
    assert x.category == STOCK if (x := f[0]) else False


def test_stock_at_risk_is_blocking_tight_warns_ok_silent():
    # Live vocabulary (coverage.item_status): AT_RISK < 0.95 -> blocking;
    # TIGHT 0.95-1.10 -> warn; OK silent. ('SHORT' never existed — the first
    # mapping guessed it and live AT_RISK blocks produced no findings.)
    report = {"schedule_view": [
        {"block_id": "b1", "sku": "111", "line_name": "P09", "start_h": 0,
         "qty_kg": 1, "status": "AT_RISK", "items": []},
        {"block_id": "b2", "sku": "222", "line_name": "P10", "start_h": 0,
         "qty_kg": 1, "status": "TIGHT", "items": []},
        {"block_id": "b3", "sku": "333", "line_name": "P11", "start_h": 0,
         "qty_kg": 1, "status": "OK", "items": []},
    ]}
    f = stock_findings(report)
    assert len(f) == 2
    assert {x.severity for x in f} == {BLOCKING, WARN}


def test_stock_dns_demand_skus_roll_into_one_warning():
    report = {"schedule_view": [], "demand_view": [
        {"sku": "111", "status": "DO_NOT_SCHEDULE", "achievable_ratio": 0.2},
        {"sku": "222", "status": "DO_NOT_SCHEDULE", "achievable_ratio": 0.5},
        {"sku": "333", "status": "OK", "achievable_ratio": 2.0},
    ]}
    f = stock_findings(report)
    assert len(f) == 1 and f[0].severity == WARN
    assert "2 demand SKU(s)" in f[0].title
    assert f[0].context["skus"] == ["111", "222"]


def test_stock_report_error_becomes_data_finding():
    f = stock_findings({"error": "ediact missing"})
    assert [x.category for x in f] == [DATA]


# ---------------------------------------------------------------------------
# Ordering / summary / assess_plan smoke
# ---------------------------------------------------------------------------

def test_sort_blocking_first_then_category_order():
    from helpers.reconcile_engine import Finding
    f = sort_findings([
        Finding("a", COVERAGE, WARN, "w", "", ""),
        Finding("b", FIT, BLOCKING, "b", "", ""),
        Finding("c", STOCK, BLOCKING, "s", "", ""),
    ])
    assert [x.key for x in f] == ["c", "b", "a"]


def test_summary_counts():
    from helpers.reconcile_engine import Finding
    s = summary([
        Finding("a", FIT, BLOCKING, "", "", ""),
        Finding("b", CIP, WARN, "", "", ""),
        Finding("c", COVERAGE, INFO, "", "", ""),
        Finding("d", CIP, WARN, "", "", ""),
    ])
    assert s == {BLOCKING: 1, WARN: 2, INFO: 1}


def test_assess_plan_runs_on_a_self_contained_dir(tmp_path):
    """Smoke: broken/missing inputs never raise — they become findings."""
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    _cal([
        {"sku": "111", "order_id": "111-W0", "qty_kg": 1000.0,
         "start_h": 0, "end_h": 10},
        {"sku": "111", "order_id": "111-W0", "qty_kg": None,
         "start_h": 8, "end_h": 12},  # overlap + unknown kg
    ]).to_csv(dd / "calendar_blocks.csv", index=False)
    _demand([{"order_id": "111-W0", "sku": "111", "qty_target": 50000},
             {"order_id": "222-W0", "sku": "222", "qty_target": 9000}]) \
        .to_csv(dd / "reference" / "demand_plan.csv", index=False)
    _caps([("999", "P09", 1)]).to_csv(
        dd / "reference" / "capabilities_rates.csv", index=False)
    # deliberately corrupt downtimes to prove the guard reports, not raises
    (dd / "reference" / "downtimes.csv").write_text("not,a,valid\x00csv")

    from helpers.reconcile_engine import assess_plan
    cfg = {"scheduler": {"horizon_weeks": 3,
                         "planning_start_date": "2026-08-10 00:00:00"}}
    findings = assess_plan(dd, cfg)

    keys = {f.key for f in findings}
    assert "fit_overlaps" in keys                      # overlap detected
    assert any(k.startswith("cap_sched:") for k in keys)  # 111@P09 not capable
    assert any(f.category == COVERAGE for f in findings)
    # sorted: first finding is blocking
    assert findings[0].severity == BLOCKING


def test_assess_plan_excludes_past_demand_weeks_via_source_json(tmp_path):
    """The engine learns the demand file's own anchor from source.json."""
    import json as _json

    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    _cal([]).to_csv(dd / "calendar_blocks.csv", index=False)
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    anchor = monday - timedelta(weeks=2)   # file anchored 2 ISO weeks back
    _demand([
        {"order_id": "111-W0", "sku": "111", "qty_target": 50000,
         "due_start_hour": 0, "due_end_hour": 167},      # 2 weeks ago
        {"order_id": "222-W2", "sku": "222", "qty_target": 9000,
         "due_start_hour": 336, "due_end_hour": 503},    # this week
    ]).to_csv(dd / "reference" / "demand_plan.csv", index=False)
    (dd / "reference" / "demand_plan.source.json").write_text(_json.dumps({
        "anchor": anchor.strftime("%Y-%m-%d %H:%M:%S"),
        "anchor_iso_week": anchor.isocalendar()[1],
    }), encoding="utf-8")

    from helpers.reconcile_engine import assess_plan
    findings = assess_plan(dd, {"scheduler": {"horizon_weeks": 3}})

    blk = [f for f in findings if f.key == "coverage_zero_week1"]
    assert len(blk) == 1
    iso_now = now.isocalendar()[1]
    assert f"222-W{iso_now:02d}" in blk[0].detail   # relabelled to ISO week
    assert "111" not in blk[0].detail               # past week excluded
    assert any(f.key == "coverage_past_weeks" for f in findings)
