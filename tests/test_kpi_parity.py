# tests/test_kpi_parity.py — golden-fixture parity: Python scorecard_engine
# vs the TypeScript Gantt KPI port (kpi.ts).
#
# helpers/scorecard_engine.gantt_kpis is the single source of truth for the
# Gantt KPI bar; code/components/gantt/frontend/src/utils/kpi.ts re-implements
# the same rules for live what-if recompute. This test feeds ONE fixture
# (calendar + demand + caps + changeover standards) through both and asserts
# the numbers are identical. The TS side runs via node on kpi.ts compiled with
# the frontend's own tsc; it is skipped (not passed) when node or the
# frontend's node_modules are unavailable.
#
# Run from repo root:  python -m pytest tests/test_kpi_parity.py -x -q

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.calendar_io import CALENDAR_COLUMNS, calendar_to_gantt_payload  # noqa: E402
from helpers.config import scorecard_config  # noqa: E402
from helpers.scorecard_engine import gantt_kpis  # noqa: E402

FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
KPI_TS = FRONTEND / "src" / "utils" / "kpi.ts"


# ---------------------------------------------------------------------------
# Fixture: covers met/under/over/unscheduled/no-min orders, a trial block
# (excluded from qty and changeovers), a CIP, a same-SKU adjacency (no
# transition), standards rows with setup_hours>0 and setup_hours==0, a
# SKU pair with no standards row (default classification), a transition
# fully waived by a CIP in its gap (2026-08-26 rule), and a cip_req_after
# pair with no CIP in the gap (hygiene violation).
# ---------------------------------------------------------------------------


def _block(btype, line_id, line_name, start, end, sku="", order_id=""):
    return {
        "block_id": f"{btype}_{line_name}_{start}",
        "block_type": btype,
        "line_id": line_id,
        "line_name": line_name,
        "start_h": float(start),
        "end_h": float(end),
        "label": sku or btype,
        "order_id": order_id,
        "sku": sku,
        "sku_description": "",
        "qty_kg": None,
        "locked": False,
        "attrs": "",
    }


def fixture_calendar() -> pd.DataFrame:
    rows = [
        _block("production", 1, "P10", 0, 10, "SKU_A", "O1"),
        _block("production", 1, "P10", 10, 20, "SKU_B", "O2"),
        _block("production", 1, "P10", 20, 25, "SKU_A", "O1"),
        _block("cip", 1, "P10", 25, 31, "CIP"),
        _block("trial", 1, "P10", 31, 35, "SKU_B", "O2"),  # excluded everywhere
        _block("production", 2, "P11", 0, 8, "SKU_C", "O3"),
        _block("production", 2, "P11", 8, 12, "SKU_C", "O3"),  # same SKU: no CO
        _block("production", 3, "P12", 0, 5, "SKU_D", "O5"),
        _block("production", 3, "P12", 5, 9, "SKU_E", "O6"),  # pair not in standards
        # P13 exercises the CIP rules (no demand orders — adherence untouched):
        # F→G has the CIP fully in its gap → transition WAIVED everywhere and
        # the cip_req_after flag is satisfied; G→F is flagged with no CIP →
        # counted as a normal (recipe) transition PLUS a cip_req violation.
        _block("production", 4, "P13", 0, 6, "SKU_F"),
        _block("cip", 4, "P13", 6, 12, "CIP"),
        _block("production", 4, "P13", 12, 18, "SKU_G"),
        _block("production", 4, "P13", 18, 22, "SKU_F"),
    ]
    return pd.DataFrame(rows)[CALENDAR_COLUMNS]


CAPS = {
    "P10": {"SKU_A": 100.0, "SKU_B": 80.0},
    "P11": {"SKU_C": 50.0},
    "P12": {"SKU_D": 40.0, "SKU_E": 60.0},
}

DEMAND = [
    {"order_id": "O1", "sku": "SKU_A", "qty_min": 1200.0, "qty_max": 1600.0},  # MET
    {"order_id": "O2", "sku": "SKU_B", "qty_min": 900.0, "qty_max": 1000.0},   # UNDER
    {"order_id": "O3", "sku": "SKU_C", "qty_min": 500.0, "qty_max": 550.0},    # OVER
    {"order_id": "O4", "sku": "SKU_A", "qty_min": 300.0, "qty_max": 400.0},    # unscheduled
    {"order_id": "O5", "sku": "SKU_D", "qty_min": 0.0, "qty_max": 0.0},        # no min
    {"order_id": "O6", "sku": "SKU_E", "qty_min": 240.0, "qty_max": 0.0},      # exact min
]

CO_MAP = {
    # setup_hours > 0 wins outright; ffs flag makes it a format change too.
    ("SKU_A", "SKU_B"): {"setup_hours": 3.0, "ffs_change": 1},
    # setup_hours == 0 falls back to per-type defaults: format 2.0 + recipe 1.0.
    ("SKU_B", "SKU_A"): {"setup_hours": 0.0, "ffs_change": 1},
    # (SKU_D, SKU_E) has no row: default = recipe, base 0.5 + recipe 1.0.
    # cip_req_after pairs (2026-08-26): F→G is satisfied by the CIP in its
    # gap (and waived); G→F has no CIP → violation, priced as plain recipe.
    ("SKU_F", "SKU_G"): {"cip_req_after": 1},
    ("SKU_G", "SKU_F"): {"cip_req_after": 1},
}


# ---------------------------------------------------------------------------
# Covered-mode fixture — the master-file rule (user mandate 2026-08-21):
# board kg ALWAYS counts from the board (the committed-MO waterfall runs no
# matter what credit map arrives) and covered_by_order only ADDS non-board
# kg (made kg from hidden completed MOs). The old covered mode disabled the
# waterfall entirely — the number was the ledger, unfalsifiable against the
# calendar it sat on.
# ---------------------------------------------------------------------------

def covered_fixture_calendar() -> pd.DataFrame:
    rows = [
        # demand-matched block: credits O1 directly (rate 100 x 10h = 1000)
        _block("production", 1, "P10", 0, 10, "SKU_A", "O1"),
        # committed MO, order id matches no demand order: 700 kg waterfalls
        _block("production", 1, "P10", 10, 17, "SKU_A", "MO9"),
    ]
    return pd.DataFrame(rows)[CALENDAR_COLUMNS]


COVERED_DEMAND = [
    {"order_id": "O1", "sku": "SKU_A", "qty_min": 1200.0, "qty_max": 1600.0},
    {"order_id": "O4", "sku": "SKU_A", "qty_min": 300.0, "qty_max": 400.0},
]
COVERED_MAP = {"O4": 100.0}   # made kg from a hidden completed MO


def test_covered_map_adds_to_board_never_replaces_it():
    """Hand-computed: O1 board 1000; made credit puts O4 at 100; waterfall
    (ALWAYS on) spreads MO9's 700 kg earliest-due first — O1 takes 600 (to
    its 1600 cap), the remaining 100 tops O4 up to 200. The old covered
    mode would have frozen O1 at 1000 and O4 at the ledger's 100."""
    from helpers.scorecard_engine import compute_adherence

    rows = compute_adherence(covered_fixture_calendar(), COVERED_DEMAND,
                             CAPS, covered_by_order=COVERED_MAP)
    by = {r["order_id"]: r for r in rows}
    assert by["O1"]["scheduled_qty"] == 1600      # 1000 board + 600 waterfall
    assert by["O1"]["status"] == "MET"
    assert by["O1"]["pct_adherence"] == 114.3     # 1600 / 1400 target
    assert by["O4"]["scheduled_qty"] == 200       # 100 made + 100 waterfall
    assert by["O4"]["status"] == "UNDER"
    assert by["O4"]["pct_adherence"] == 57.1      # 200 / 350 target

    # board-only pass (no credit map): the waterfall still credits MO9's kg
    board = compute_adherence(covered_fixture_calendar(), COVERED_DEMAND, CAPS)
    by_b = {r["order_id"]: r for r in board}
    assert by_b["O1"]["scheduled_qty"] == 1600
    assert by_b["O4"]["scheduled_qty"] == 100     # waterfall remainder only


@pytest.fixture(scope="module")
def py_result():
    return gantt_kpis(
        fixture_calendar(), DEMAND, CAPS, cfg=scorecard_config({}), co_map=CO_MAP
    )


# ---------------------------------------------------------------------------
# Golden values — canonical behavior of the Python engine (runs without node).
# ---------------------------------------------------------------------------


def test_python_golden_values(py_result):
    assert py_result["orders_total"] == 6
    assert py_result["orders_met"] == 3          # O1, O5, O6
    assert py_result["pct_adherence"] == 50.0

    co = py_result["changeovers"]
    assert co["sku_transitions"] == 4            # A→B, B→A, D→E, G→F (F→G waived at CIP)
    assert co["recipe_changes"] == 4
    assert co["format_changes"] == 2             # both standards rows flag ffs
    assert co["total_co_hours"] == 9.0           # 3.0 + (2.0 + 1.0) + (0.5 + 1.0) + (0.5 + 1.0)
    assert py_result["per_line_changeovers"] == {"P10": 2, "P11": 0, "P12": 1, "P13": 1}

    # CIP rules on this fixture (score_changeovers, the engine gantt_kpis
    # wraps): F→G swallowed by the P13 CIP; flagged G→F with no CIP in the
    # gap is the one violation. Pinned node-free so the TS parity below has
    # canonical numbers to match.
    from helpers.scorecard_engine import score_changeovers

    co_full = score_changeovers(fixture_calendar(), scorecard_config({}), CO_MAP)
    assert co_full["transitions_at_cip"] == 1
    assert co_full["cip_req_violations"] == 1
    assert co_full["cip_req_detail"] == [
        {"line": "P13", "from_sku": "SKU_G", "to_sku": "SKU_F", "at_h": 18.0}]

    by_order = {r["order_id"]: r for r in py_result["adherence"]}
    assert by_order["O1"]["status"] == "MET" and by_order["O1"]["scheduled_qty"] == 1500
    # pct is % of TARGET, the (qty_min+qty_max)/2 midpoint (user rule
    # 2026-08-14: the planner's band is 90-110 of target): 800/950 = 84.2
    assert by_order["O2"]["status"] == "UNDER" and by_order["O2"]["pct_adherence"] == 84.2
    assert by_order["O3"]["status"] == "OVER" and by_order["O3"]["scheduled_qty"] == 600
    assert by_order["O4"]["status"] == "UNDER" and by_order["O4"]["scheduled_qty"] == 0
    assert by_order["O5"]["status"] == "MET" and by_order["O5"]["pct_adherence"] == 999.0
    assert by_order["O6"]["status"] == "MET" and by_order["O6"]["pct_adherence"] == 100.0

    # co_pairs classification map the frontend consumes
    assert py_result["co_pairs"]["SKU_A|SKU_B"] == {
        "recipe": 1, "format": 1, "hours": 3.0,
        "tl": 0, "ffs": 1, "cp": 0, "ttp": 0, "cip_req": 0}
    assert py_result["co_pairs"]["SKU_B|SKU_A"] == {
        "recipe": 1, "format": 1, "hours": 3.0,
        "tl": 0, "ffs": 1, "cp": 0, "ttp": 0, "cip_req": 0}
    assert py_result["co_pairs"]["SKU_F|SKU_G"] == {
        "recipe": 1, "format": 0, "hours": 1.5,
        "tl": 0, "ffs": 0, "cp": 0, "ttp": 0, "cip_req": 1}
    assert py_result["co_pairs"]["SKU_G|SKU_F"] == {
        "recipe": 1, "format": 0, "hours": 1.5,
        "tl": 0, "ffs": 0, "cp": 0, "ttp": 0, "cip_req": 1}
    assert py_result["co_default"] == {
        "recipe": 1, "format": 0, "hours": 1.5,
        "tl": 0, "ffs": 0, "cp": 0, "ttp": 0, "cip_req": 0}

    # trial qty must NOT count: O2 scheduled is 800 (10h * 80), not 800 + trial
    assert by_order["O2"]["scheduled_qty"] == 800

    # Week-fulfillment credit cap (finding 7): min(scheduled, target), the
    # arithmetic weekly_breakdown sums per week. Pinned here node-free so the
    # goldens hold even when the TS half is skipped.
    for oid, expected in EXPECTED_WEEK_CREDITS.items():
        r = by_order[oid]
        tgt = ((r["qty_min"] + r["qty_max"]) / 2
               if r["qty_min"] > 0 and r["qty_max"] >= r["qty_min"]
               else max(r["qty_min"], r["qty_max"]))
        assert min(float(r["scheduled_qty"]), tgt) == expected


# ---------------------------------------------------------------------------
# TS parity — compile kpi.ts with the frontend's tsc, run under node, compare.
# ---------------------------------------------------------------------------

RUNNER_JS = """
const fs = require("fs");
const kpi = require(process.argv[2]);
const fx = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const kpis = kpi.computeKpis(
  fx.schedule, fx.cipWindows, fx.demand, fx.caps, fx.co_pairs, fx.co_default);
const adherence = kpi.computeAdherence(fx.schedule, fx.demand, fx.caps);
const weekCredits = Object.fromEntries(
  adherence.map((r) => [r.order_id, kpi.weekFulfillmentCredit(r)]));
const changeovers = kpi.countChangeovers(
  fx.schedule, fx.co_pairs, fx.co_default, fx.cipWindows);
const out = { kpis, adherence, weekCredits, changeovers };
if (fx.covered) {
  out.coveredAdherence = kpi.computeAdherence(
    fx.covered.schedule, fx.covered.demand, fx.caps, fx.covered.map);
}
process.stdout.write(JSON.stringify(out));
"""

# Hand-verified: credit = min(scheduled_qty, target) where target is the
# min/max midpoint (max(min, max) when the band is degenerate) — the cap
# weekly fulfillment sums (weekly_breakdown / weekStats, finding 7).
#   O1 sched 1500, band 1200-1600 -> target 1400 -> 1400 (capped)
#   O2 sched  800, band  900-1000 -> target  950 ->  800
#   O3 sched  600, band  500-550  -> target  525 ->  525 (capped)
#   O4 sched    0, band  300-400  -> target  350 ->    0
#   O5 sched  200, band    0-0    -> target    0 ->    0 (no target, no credit)
#   O6 sched  240, band  240-0    -> target  240 ->  240
EXPECTED_WEEK_CREDITS = {
    "O1": 1400.0, "O2": 800.0, "O3": 525.0, "O4": 0.0, "O5": 0.0, "O6": 240.0,
}


def _compile_kpi_ts(tmp: Path) -> Path:
    tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        pytest.skip("frontend node_modules not installed (npm install in frontend dir)")
    out = tmp / "compiled"
    proc = subprocess.run(
        [
            "node", str(tsc), str(KPI_TS),
            "--outDir", str(out),
            "--module", "commonjs",
            "--target", "es2020",
            "--moduleResolution", "node",
            "--skipLibCheck",
            "--esModuleInterop",
        ],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"tsc failed:\n{proc.stdout}\n{proc.stderr}"
    # types.ts is pulled into the program (referenced from kpi.ts), so the
    # common source root is src/ and kpi.js lands under utils/.
    js = out / "utils" / "kpi.js"
    assert js.exists(), f"expected {js}"
    return js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_ts_matches_python(py_result, tmp_path):
    schedule, windows = calendar_to_gantt_payload(fixture_calendar())
    cov_schedule, _ = calendar_to_gantt_payload(covered_fixture_calendar())
    fixture = {
        "schedule": schedule,
        "cipWindows": windows,
        "demand": DEMAND,
        "caps": CAPS,
        "co_pairs": py_result["co_pairs"],
        "co_default": py_result["co_default"],
        "covered": {
            "schedule": cov_schedule,
            "demand": COVERED_DEMAND,
            "map": COVERED_MAP,
        },
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")

    kpi_js = _compile_kpi_ts(tmp_path)
    proc = subprocess.run(
        ["node", str(runner_path), str(kpi_js), str(fixture_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    ts = json.loads(proc.stdout)

    # Summary numbers must match the Python payload exactly.
    assert ts["kpis"]["pctAdherence"] == pytest.approx(py_result["pct_adherence"], abs=1e-9)
    assert ts["kpis"]["ordersMet"] == py_result["orders_met"]
    assert ts["kpis"]["ordersTotal"] == py_result["orders_total"]
    co = py_result["changeovers"]
    assert ts["kpis"]["totalChangeovers"] == co["sku_transitions"]
    assert ts["kpis"]["recipeChanges"] == co["recipe_changes"]
    assert ts["kpis"]["formatChanges"] == co["format_changes"]
    assert ts["kpis"]["totalCoHours"] == pytest.approx(co["total_co_hours"], abs=1e-9)
    assert ts["kpis"]["perLineChangeovers"] == py_result["per_line_changeovers"]

    # CIP rules (2026-08-26): the TS port must waive the F→G transition at
    # the P13 CIP and flag G→F as the one cip_req violation — the exact
    # score_changeovers numbers.
    from helpers.scorecard_engine import score_changeovers
    py_co = score_changeovers(fixture_calendar(), scorecard_config({}), CO_MAP)
    assert ts["changeovers"]["transitionsAtCip"] == py_co["transitions_at_cip"]
    assert ts["changeovers"]["cipReqViolations"] == py_co["cip_req_violations"]
    assert ts["changeovers"]["total"] == py_co["sku_transitions"]

    # Adherence rows: same order (codepoint SKU sort), same values.
    py_rows = py_result["adherence"]
    ts_rows = ts["adherence"]
    assert [r["order_id"] for r in ts_rows] == [r["order_id"] for r in py_rows]
    for tr, pr in zip(ts_rows, py_rows):
        assert tr["sku"] == pr["sku"]
        assert tr["status"] == pr["status"]
        assert tr["scheduled_qty"] == pr["scheduled_qty"]
        assert tr["pct_adherence"] == pytest.approx(pr["pct_adherence"], abs=1e-9)
        assert tr["qty_min"] == pytest.approx(pr["qty_min"], abs=1e-9)
        assert tr["qty_max"] == pytest.approx(pr["qty_max"], abs=1e-9)

    # Week-fulfillment credit: capped at each order's target (finding 7) —
    # the same arithmetic weekly_breakdown applies server-side.
    assert ts["weekCredits"] == pytest.approx(EXPECTED_WEEK_CREDITS)

    # Covered mode: board waterfall ALWAYS on, credit map only ADDS non-board
    # kg — TS must agree with the Python goldens hand-computed above.
    from helpers.scorecard_engine import compute_adherence
    py_cov = compute_adherence(covered_fixture_calendar(), COVERED_DEMAND,
                               CAPS, covered_by_order=COVERED_MAP)
    ts_cov = ts["coveredAdherence"]
    assert [r["order_id"] for r in ts_cov] == [r["order_id"] for r in py_cov]
    for tr, pr in zip(ts_cov, py_cov):
        assert tr["scheduled_qty"] == pr["scheduled_qty"]
        assert tr["status"] == pr["status"]
        assert tr["pct_adherence"] == pytest.approx(pr["pct_adherence"], abs=1e-9)


# ---------------------------------------------------------------------------
# Weekly breakdown — ISO-week bucketing of the same fixture rules.
# ---------------------------------------------------------------------------


def test_iso_week_bounds_midweek_anchor():
    """A Friday anchor's first bucket is its own partial ISO week; the next
    Monday mark starts the following ISO week."""
    from datetime import datetime

    from helpers.scorecard_engine import _iso_week_bounds

    bounds = _iso_week_bounds(datetime(2026, 8, 14), 504.0)  # Fri W33
    assert bounds[0] == (0.0, 33)
    assert bounds[1] == (72.0, 34)      # Mon Aug 17, 72h after Fri 00:00
    assert bounds[2] == (240.0, 35)


def test_iso_week_bounds_monday_anchor():
    from datetime import datetime

    from helpers.scorecard_engine import _iso_week_bounds

    bounds = _iso_week_bounds(datetime(2026, 8, 17), 504.0)  # Mon W34
    assert bounds[0] == (0.0, 34)
    assert bounds[1] == (168.0, 35)
    assert bounds[2] == (336.0, 36)
