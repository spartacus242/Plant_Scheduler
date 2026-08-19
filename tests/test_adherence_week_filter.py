# tests/test_adherence_week_filter.py — past demand weeks stay off the board.
#
# Finding 11 (walkthrough 2026-08-18): the adherence table under the Gantt
# still listed past-week orders ("570560-W32 ... 0% UNDER" with today in W34).
# Past-week misses live on the Reconcile page; the planning board plans
# current/future ISO weeks only — the same rule the holding area
# (_holding_is_current in pages/calendar.py) and the popup demand list apply.
# The filter is client-side (utils/layout.ts isPastDemandWeek, applied to the
# table rows in GanttSandbox), so this test compiles layout.ts with the
# frontend's own tsc and pins the predicate under node. Skipped (not passed)
# when node or the frontend's node_modules are unavailable.

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
LAYOUT_TS = FRONTEND / "src" / "utils" / "layout.ts"

RUNNER_JS = """
const layout = require(process.argv[2]);
const anchor = new Date("2026-08-17T00:00:00"); // Monday, ISO W34
const now = new Date("2026-08-19T10:00:00");    // Wednesday of W34

// Production path: order -W<k> indexes count from the DEMAND anchor's ISO
// week (demand_plan.source.json), here W32 — so -W0 is W32, -W2 is W34.
layout.setDemandBaseWeek(32);
const based = {
  w0_past: layout.isPastDemandWeek("570560-W0", anchor, now),
  w1_past: layout.isPastDemandWeek("570560-W1", anchor, now),
  w2_current: layout.isPastDemandWeek("570560-W2", anchor, now),
  w3_future: layout.isPastDemandWeek("570560-W3", anchor, now),
  no_suffix: layout.isPastDemandWeek("MO-12345", anchor, now),
};

// Legacy fallback (no demand base week): week index converts via
// anchor + k*168h. Anchor one ISO week before `now` makes -W0 a past week.
layout.setDemandBaseWeek(null);
const prevMonday = new Date("2026-08-10T00:00:00"); // Monday, ISO W33
const legacy = {
  w0_past: layout.isPastDemandWeek("X-W0", prevMonday, now),
  w1_current: layout.isPastDemandWeek("X-W1", prevMonday, now),
};
process.stdout.write(JSON.stringify({ based, legacy }));
"""


def _compile_layout_ts(tmp: Path) -> Path:
    tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        pytest.skip("frontend node_modules not installed (npm install in frontend dir)")
    out = tmp / "compiled"
    proc = subprocess.run(
        [
            "node", str(tsc), str(LAYOUT_TS),
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
    # layout.ts imports nothing, so it is the whole program and compiles flat.
    js = out / "layout.js"
    assert js.exists(), f"expected {js}"
    return js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_past_demand_weeks_are_filtered(tmp_path):
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    layout_js = _compile_layout_ts(tmp_path)
    proc = subprocess.run(
        ["node", str(runner_path), str(layout_js)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    res = json.loads(proc.stdout)

    # W32/W33 orders are past (today W34): off the planning board.
    assert res["based"]["w0_past"] is True
    assert res["based"]["w1_past"] is True
    # The current and future weeks stay.
    assert res["based"]["w2_current"] is False
    assert res["based"]["w3_future"] is False
    # Un-dated ids (committed MOs) are never "past".
    assert res["based"]["no_suffix"] is False

    assert res["legacy"]["w0_past"] is True
    assert res["legacy"]["w1_current"] is False
