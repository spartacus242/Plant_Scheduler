# tests/test_time_axis_math.py — ISO week boundaries on the Gantt time axis.
#
# utils/layout.mondayBoundaries is the one source of truth for where the
# dashed week lines (and their W-labels) land. The rolling anchor is TODAY
# at midnight — any weekday — so boundaries must be true ISO Monday-00:00
# wall-clock positions, never `floor(viewStart/168)*168 + k*168` (hour-0
# stepping put every "week" line on a Friday after a Friday roll). Compiled
# with the frontend's own tsc and pinned under node — the same harness
# pattern as test_drop_plan_math. Skipped (not failed) when node or the
# frontend's node_modules are missing.

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
const path = require("path");
const out = process.argv[2];
const L = require(path.join(out, "layout.js"));

// Local-midnight anchors (the roll writes "YYYY-MM-DD 00:00:00").
const fri = new Date(2026, 7, 21);   // Friday 2026-08-21, ISO W34
const mon = new Date(2026, 7, 24);   // Monday 2026-08-24, ISO W35

const labels = (anchor, hours) =>
  hours.map((h) => L.isoWeekAtHour(anchor, h + 1));

const r = {};
// Friday anchor: last Monday at/before viewStart(0) is Aug 17 (-96h) — kept
// so the partial week's label clamps at the viewport edge — then true
// Mondays Aug 24 (+72h), Aug 31 (+240h), Sep 7 (+408h).
r.friBounds = L.mondayBoundaries(fri, 0, 504);
r.friWeeks = labels(fri, r.friBounds);
// Monday anchor: boundaries at 0h, 168h.
r.monBounds = L.mondayBoundaries(mon, 0, 336);
r.monWeeks = labels(mon, r.monBounds);
// Mid-week viewStart still backfills to the covering week's Monday.
r.friMidView = L.mondayBoundaries(fri, 100, 336);
r.monMidView = L.mondayBoundaries(mon, 100, 336);
process.stdout.write(JSON.stringify(r));
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
    js = out / "layout.js"
    assert js.exists(), f"expected {js}"
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_monday_boundaries(tmp_path):
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    out = _compile_layout_ts(tmp_path)
    proc = subprocess.run(
        ["node", str(runner_path), str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    r = json.loads(proc.stdout)

    # Friday anchor: -96 (clamped-label Monday of the anchor's own week),
    # then +72, +240, +408 — Mondays Aug 17 / 24 / 31 / Sep 7.
    assert r["friBounds"] == [-96, 72, 240, 408]
    assert r["friWeeks"] == [34, 35, 36, 37]

    # Monday anchor: hour 0 IS a boundary; plain 168h stepping.
    assert r["monBounds"] == [0, 168]
    assert r["monWeeks"] == [35, 36]

    # A mid-week viewStart backfills to the Monday covering it, so the
    # current week's label can clamp at the viewport edge.
    assert r["friMidView"] == [72, 240]   # viewStart h100 = Tue Aug 25
    assert r["monMidView"] == [0, 168]
