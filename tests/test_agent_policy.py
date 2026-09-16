# tests/test_agent_policy.py — agent input policies: the flat DNS trim of
# the solver's work-dir demand copy (user-approved 2026-08-14) and the
# projected per-order policy with earliest-start floors (slice 3,
# 2026-09-15).

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.agent_policy import (  # noqa: E402
    EARLIEST_COL, apply_stock_policy, dns_ratios, policy_signature,
    projected_caps, trim_dns_demand, trim_projected_demand)


def _demand(rows):
    base = {"order_id": "X-W0", "sku": "X", "week_index": 0,
            "qty_target": 10000, "lower_pct": 0.9, "upper_pct": 1.1,
            "due_start_hour": 0, "due_end_hour": 167, "priority": 3}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_dns_ratios_takes_worst_week_and_guards_none():
    rep = {"demand_view": [
        {"sku": "111", "status": "DO_NOT_SCHEDULE", "achievable_ratio": 0.6},
        {"sku": "111", "status": "DO_NOT_SCHEDULE", "achievable_ratio": 0.2},
        {"sku": "222", "status": "DO_NOT_SCHEDULE", "achievable_ratio": None},
        {"sku": "333", "status": "OK", "achievable_ratio": 2.0},
    ]}
    r = dns_ratios(rep)
    assert r == {"111": 0.2, "222": 0.0}


def test_trim_zeroes_qmin_and_caps_qmax_at_achievable():
    dem = _demand([
        {"order_id": "111-W0", "sku": "111"},
        {"order_id": "444-W0", "sku": "444"},  # not DNS — untouched
    ])
    out, notes = trim_dns_demand(dem, {"111": 0.4})
    trimmed = out[out["order_id"] == "111-W0"].iloc[0]
    assert trimmed["lower_pct"] == 0.0
    assert trimmed["upper_pct"] == 0.4          # capped at achievable
    untouched = out[out["order_id"] == "444-W0"].iloc[0]
    assert untouched["lower_pct"] == 0.9 and untouched["upper_pct"] == 1.1
    assert len(notes) == 1 and "111-W0" in notes[0]
    assert "9,000→0" in notes[0]                 # qty_min 10000*0.9 -> 0


def test_trim_never_raises_qmax():
    # achievable 1.5 > upper 1.1: cap keeps the ORIGINAL upper bound
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    out, _ = trim_dns_demand(dem, {"111": 1.5})
    assert out.iloc[0]["upper_pct"] == 1.1


def test_trim_noop_without_dns():
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    out, notes = trim_dns_demand(dem, {})
    assert notes == [] and out.iloc[0]["lower_pct"] == 0.9


# ---------------------------------------------------------------------------
# slice 3 — projected per-order policy
# ---------------------------------------------------------------------------

def _proj(status, cap=None, floor=None, board=0.0, text="why"):
    return {"status": status, "cap_kg": cap, "earliest_start_h": floor,
            "board_kg": board, "text": text}


def _report(rows, anchor="2026-09-14 00:00:00"):
    return {"anchor": anchor, "demand_view": rows}


def test_projected_caps_keys_by_order_with_week_alias():
    rep = _report([
        {"order_id": "111-W0", "sku": "111", "week_index": 0,
         "projected": _proj("DNS", cap=1200.0)},
        {"order_id": "222-W1", "sku": "222", "week_index": 1},      # no projection
    ])
    caps = projected_caps(rep)
    assert set(caps) == {"111-W0", "_by_week"}
    assert caps["111-W0"]["cap_kg"] == 1200.0 and caps["111-W0"]["sku"] == "111"
    assert caps["_by_week"] == {("111", 0): "111-W0"}
    assert projected_caps({"demand_view": [{"order_id": "a", "sku": "1"}]}) == {}


def test_projected_trim_caps_pct_row_on_board_plus_cap():
    """Gross 10,000 kg row, board makes 2,000 in the week, components support
    3,000 kg more: upper 1.1 → 0.5, lower → 0."""
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    out, notes = trim_projected_demand(
        dem, projected_caps(_report([{"order_id": "111-W0", "sku": "111", "week_index": 0,
                                      "projected": _proj("DNS", cap=3000.0, board=2000.0)}])))
    r = out.iloc[0]
    assert (r["lower_pct"], r["upper_pct"]) == (0.0, 0.5)
    assert EARLIEST_COL not in out.columns            # no floor anywhere
    assert len(notes) == 1 and "11,000→5,000" in notes[0]


def test_projected_trim_caps_netted_row_on_its_net_target():
    dem = pd.DataFrame([{
        "order_id": "120437-W0", "sku": "120437", "week_index": 0,
        "qty_target": 6926.7, "lower_pct": float("nan"), "upper_pct": float("nan"),
        "qty_min": 4606.9, "qty_max": 9246.5, "credit_kg": 16271.3,
        "due_start_hour": 0, "due_end_hour": 119, "priority": 3,
    }])
    caps = projected_caps(_report([{"order_id": "120437-W0", "sku": "120437", "week_index": 0,
                                    "projected": _proj("LIFTED", cap=1127.2, floor=112.0)}]))
    out, notes = trim_projected_demand(dem, caps)
    r = out.iloc[0]
    assert r["qty_min"] == 0.0 and r["qty_max"] == 1127.2
    assert pd.isna(r["lower_pct"])
    assert r[EARLIEST_COL] == 112.0
    assert "earliest start h112" in notes[0]


def test_projected_trim_leaves_cap_above_qmax_but_writes_floor():
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    caps = projected_caps(_report([{"order_id": "111-W0", "sku": "111", "week_index": 0,
                                    "projected": _proj("LIFTED", cap=50000.0, floor=40.0)}]))
    out, notes = trim_projected_demand(dem, caps, shift_h=-24.0)
    r = out.iloc[0]
    assert (r["lower_pct"], r["upper_pct"]) == (0.9, 1.1)      # untouched band
    assert r[EARLIEST_COL] == 16.0                            # shifted into the work frame
    assert len(notes) == 1 and "earliest start h16" in notes[0]


def test_projected_trim_floor_clamped_and_flagged_past_window():
    dem = _demand([{"order_id": "111-W0", "sku": "111", "due_end_hour": 119}])
    caps = projected_caps(_report([{"order_id": "111-W0", "sku": "111", "week_index": 0,
                                    "projected": _proj("LIFTED", cap=50000.0, floor=10.0)}]))
    out, notes = trim_projected_demand(dem, caps, shift_h=-50.0)
    assert out.iloc[0][EARLIEST_COL] == 0.0                   # never negative
    out, notes = trim_projected_demand(dem, caps, shift_h=+150.0)
    assert out.iloc[0][EARLIEST_COL] == 160.0
    assert "after the due window" in notes[0]


def test_projected_trim_floors_can_be_switched_off():
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    caps = projected_caps(_report([{"order_id": "111-W0", "sku": "111", "week_index": 0,
                                    "projected": _proj("LIFTED", cap=50000.0, floor=40.0)}]))
    out, notes = trim_projected_demand(dem, caps, earliest_start=False)
    assert EARLIEST_COL not in out.columns and notes == []


def test_projected_trim_ok_and_no_data_rows_untouched():
    dem = _demand([{"order_id": "111-W0", "sku": "111"},
                   {"order_id": "222-W0", "sku": "222"}])
    caps = projected_caps(_report([
        {"order_id": "111-W0", "sku": "111", "week_index": 0, "projected": _proj("OK")},
        {"order_id": "222-W0", "sku": "222", "week_index": 0,
         "projected": _proj("NO_DATA")}]))
    out, notes = trim_projected_demand(dem, caps)
    assert notes == []
    assert list(out["upper_pct"]) == [1.1, 1.1]


def test_projected_trim_matches_renamed_rows_by_sku_week():
    dem = _demand([{"order_id": "W0-111", "sku": "111"}])
    caps = projected_caps(_report([{"order_id": "111-W0", "sku": "111", "week_index": 0,
                                    "projected": _proj("DNS", cap=1000.0)}]))
    out, notes = trim_projected_demand(dem, caps)
    assert out.iloc[0]["upper_pct"] == 0.1 and "W0-111" in notes[0]


def test_policy_signature_projected_vs_flat():
    rep = _report([
        {"order_id": "111-W0", "sku": "111", "week_index": 0, "status": "DO_NOT_SCHEDULE",
         "achievable_ratio": 0.2, "projected": _proj("LIFTED", cap=9000.0, floor=100.0)},
        {"order_id": "111-W1", "sku": "111", "week_index": 1, "status": "DO_NOT_SCHEDULE",
         "achievable_ratio": 0.2, "projected": _proj("OK")},
        {"order_id": "222-W0", "sku": "222", "week_index": 0, "status": "DO_NOT_SCHEDULE",
         "achievable_ratio": 0.5, "projected": _proj("DNS", cap=500.0)},
    ])
    assert policy_signature(rep) == {"111-W0": 9000.0, "222-W0": 500.0}
    for d in rep["demand_view"]:
        d.pop("projected")
    assert policy_signature(rep) == {"111": 0.2, "222": 0.5}


# ---------------------------------------------------------------------------
# apply_stock_policy — the one patch every solver caller runs
# ---------------------------------------------------------------------------

TOML = """
[scheduler]
planning_start_date = "2026-09-15 00:00:00"
"""


def _work(tmp_path, dem: pd.DataFrame, toml: str = TOML) -> Path:
    work = tmp_path / "work"
    work.mkdir(parents=True)
    (work / "flowstate.toml").write_text(toml, encoding="utf-8")
    dem.to_csv(work / "demand_plan.csv", index=False)
    return work


def test_apply_stock_policy_projected_shifts_floors_into_the_work_frame(tmp_path):
    dem = _demand([{"order_id": "111-W0", "sku": "111"},
                   {"order_id": "333-W0", "sku": "333"}])
    work = _work(tmp_path, dem)
    rep = _report([
        {"order_id": "111-W0", "sku": "111", "week_index": 0,
         "projected": _proj("LIFTED", cap=4000.0, floor=112.0)},
        {"order_id": "333-W0", "sku": "333", "week_index": 0, "projected": _proj("OK")},
    ], anchor="2026-09-14 00:00:00")            # report frame is one day EARLIER
    res = apply_stock_policy(work, rep, cfg={})
    assert res.mode == "projected"
    assert res.shift_h == pytest.approx(-24.0)
    assert res.capped_orders == {"111-W0": 4000.0} and res.capped_skus == {"111"}
    assert res.n_floors == 1
    out = pd.read_csv(work / "demand_plan.csv", dtype={"sku": str})
    r = out[out["order_id"] == "111-W0"].iloc[0]
    assert (r["lower_pct"], r["upper_pct"]) == (0.0, 0.4)
    assert r[EARLIEST_COL] == 88.0                              # 112 - 24
    assert pd.isna(out[out["order_id"] == "333-W0"].iloc[0][EARLIEST_COL])
    assert res.notes[0].startswith("stock policy (projected): 1 order(s) capped, 1 earliest-start floor(s)")
    assert any("shifted -24h" in n for n in res.notes)


def test_apply_stock_policy_falls_back_to_flat_without_projection(tmp_path):
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    work = _work(tmp_path, dem)
    rep = {"demand_view": [{"order_id": "111-W0", "sku": "111", "week_index": 0,
                            "status": "DO_NOT_SCHEDULE", "achievable_ratio": 0.4}]}
    res = apply_stock_policy(work, rep, cfg={})
    assert res.mode == "flat" and res.capped_skus == {"111"}
    assert "no per-order projection" in res.notes[0]
    out = pd.read_csv(work / "demand_plan.csv")
    assert out.iloc[0]["upper_pct"] == 0.4 and EARLIEST_COL not in out.columns


def test_apply_stock_policy_config_knobs(tmp_path):
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    rep = _report([{"order_id": "111-W0", "sku": "111", "week_index": 0,
                    "status": "DO_NOT_SCHEDULE", "achievable_ratio": 0.4,
                    "projected": _proj("LIFTED", cap=4000.0, floor=112.0)}],
                  anchor="2026-09-15 00:00:00")
    # off: nothing written
    work = _work(tmp_path / "a", dem)
    res = apply_stock_policy(work, rep, cfg={"stock": {"solver_policy": "off"}})
    assert res.mode == "off"
    assert pd.read_csv(work / "demand_plan.csv").iloc[0]["upper_pct"] == 1.1
    # flat forced: the worst-week ratio, no floor column
    work = _work(tmp_path / "b", dem)
    res = apply_stock_policy(work, rep, cfg={"stock": {"solver_policy": "flat"}})
    assert res.mode == "flat"
    out = pd.read_csv(work / "demand_plan.csv")
    assert out.iloc[0]["upper_pct"] == 0.4 and EARLIEST_COL not in out.columns
    # projected with floors off
    work = _work(tmp_path / "c", dem)
    res = apply_stock_policy(work, rep, cfg={"stock": {"solver_earliest_start": False}})
    assert res.mode == "projected" and res.n_floors == 0
    out = pd.read_csv(work / "demand_plan.csv")
    assert out.iloc[0]["upper_pct"] == 0.4 and EARLIEST_COL not in out.columns


def test_apply_stock_policy_errored_report_solves_without_it(tmp_path):
    dem = _demand([{"order_id": "111-W0", "sku": "111"}])
    work = _work(tmp_path, dem)
    res = apply_stock_policy(work, {"error": "ediact missing"}, cfg={})
    assert res.mode == "off" and "unavailable" in res.notes[0]
    assert pd.read_csv(work / "demand_plan.csv").iloc[0]["upper_pct"] == 1.1
