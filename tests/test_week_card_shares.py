# tests/test_week_card_shares.py — the week card's two numbers.
#
# Each card above the Gantt reads "X% on board + Y% already made". Y used to
# be the DIFFERENCE between a credited adherence pass and a board-only pass,
# so any kg the made credit displaced into a neighbouring week's order
# through the waterfall was reported as "already made" there. Live
# 2026-09-15: the whole of W39's "+1.7% already made" (28,753 kg) and all of
# W40's (8,103 kg) was displaced BOARD kg, with no made kg behind it.
#
# utils/kpi.weekCardShares computes the two shares directly instead — board
# kg capped at the order's target, made kg capped at what is left under it —
# so board kg can never be reported as already-made and the two shares never
# double-count the same kg. Compiled with the frontend's own tsc and pinned
# under node, the same harness as test_kpi_parity / test_adherence_week_filter;
# skipped (not passed) when node or the frontend's node_modules are missing.

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
KPI_TS = FRONTEND / "src" / "utils" / "kpi.ts"

RUNNER_JS = """
const path = require("path");
const out = process.argv[2];
const kpi = require(path.join(out, "utils", "kpi.js"));

// qty_min 9,000 / qty_max 11,000 -> target (the 100% point) = 10,000 kg
const row = (scheduled) => ({
  order_id: "280104-W0", sku: "280104", qty_min: 9000, qty_max: 11000,
  scheduled_qty: scheduled, pct_adherence: 0, status: "UNDER",
});

const r = {
  target: kpi.orderTarget(9000, 11000),
  boardOnly: kpi.weekCardShares(row(4000), 0),
  madeFillsTheGap: kpi.weekCardShares(row(4000), 6000),
  madeOvershoots: kpi.weekCardShares(row(4000), 9000),
  boardAtTarget: kpi.weekCardShares(row(10000), 5000),
  boardOverTarget: kpi.weekCardShares(row(12000), 5000),
  negativeMade: kpi.weekCardShares(row(4000), -500),
  nothingAtAll: kpi.weekCardShares(row(0), 0),
  madeOnly: kpi.weekCardShares(row(0), 7000),
};
console.log(JSON.stringify(r));
"""


@pytest.fixture(scope="module")
def shares(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node not available")
    tsc = FRONTEND / "node_modules" / ".bin" / ("tsc.cmd" if Path(
        FRONTEND / "node_modules" / ".bin" / "tsc.cmd").exists() else "tsc")
    if not Path(tsc).exists():
        pytest.skip("frontend node_modules not installed")
    out = tmp_path_factory.mktemp("tsc_out")
    proc = subprocess.run(
        [str(tsc), str(KPI_TS), "--outDir", str(out), "--module", "commonjs",
         "--target", "es2020", "--moduleResolution", "node",
         "--esModuleInterop", "--skipLibCheck"],
        capture_output=True, text=True, cwd=str(FRONTEND))
    if not (out / "utils" / "kpi.js").exists():
        pytest.skip(f"tsc could not emit kpi.js: {proc.stdout}\n{proc.stderr}")
    runner = out / "runner.js"
    runner.write_text(RUNNER_JS, encoding="utf-8")
    res = subprocess.run(["node", str(runner), str(out)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_target_is_the_band_midpoint(shares):
    assert shares["target"] == 10000


def test_board_only_reports_no_made_share(shares):
    s = shares["boardOnly"]
    assert (s["board"], s["made"], s["target"]) == (4000, 0, 10000)


def test_made_credit_fills_the_gap_above_the_board(shares):
    s = shares["madeFillsTheGap"]
    assert (s["board"], s["made"]) == (4000, 6000)
    assert s["board"] + s["made"] == s["target"]      # exactly 100%, no more


def test_made_credit_is_clipped_at_the_target(shares):
    """9,000 kg of made production on top of 4,000 kg of board kg cannot make
    the week 130% — the card caps at the week's own target."""
    s = shares["madeOvershoots"]
    assert (s["board"], s["made"]) == (4000, 6000)


def test_a_week_the_board_already_covers_shows_no_made_share(shares):
    assert shares["boardAtTarget"]["made"] == 0
    assert shares["boardAtTarget"]["board"] == 10000


def test_board_over_target_is_capped_and_still_shows_no_made_share(shares):
    """The headline is fulfillment, not production: 12,000 kg on a 10,000 kg
    week is 100% covered, and the made credit adds nothing on top."""
    s = shares["boardOverTarget"]
    assert (s["board"], s["made"]) == (10000, 0)


def test_negative_credit_cannot_subtract_from_the_board(shares):
    s = shares["negativeMade"]
    assert (s["board"], s["made"]) == (4000, 0)


def test_empty_week(shares):
    s = shares["nothingAtAll"]
    assert (s["board"], s["made"], s["target"]) == (0, 0, 10000)


def test_a_week_covered_entirely_by_production_already_made(shares):
    """The planner-visible case: nothing on the board, but the plant made it
    on Sunday — the card must read 0% on board + 70% already made."""
    s = shares["madeOnly"]
    assert (s["board"], s["made"]) == (0, 7000)
