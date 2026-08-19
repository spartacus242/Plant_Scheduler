# tests/test_drop_plan_math.py — drag-and-drop landing geometry.
#
# utils/dropPlan.ts is the ONE source of truth for where a dragged block
# lands: the ghost's current rect against the chart svg's current rect, read
# at the same instant, so every scroll source cancels out by construction
# (dnd-kit's event.delta carries no scroll adjustment for SVG draggables —
# its scrollable-ancestor walk bails on SVG nodes — and the parent-page
# auto-scroll is invisible to the iframe entirely). Compiled with the
# frontend's own tsc and pinned under node — the same harness pattern as
# test_kpi_parity / test_sku_picker_math. Skipped (not failed) when node or
# the frontend's node_modules are missing.

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
PLAN_TS = FRONTEND / "src" / "utils" / "dropPlan.ts"

# Chart geometry constants mirrored from utils/layout.ts:
#   LINE_LABEL_WIDTH=50, HEADER_HEIGHT=48, LINE_HEIGHT=40.
RUNNER_JS = """
const path = require("path");
const out = process.argv[2];
const dp = require(path.join(out, "dropPlan.js"));

const HW = 10;                        // hourWidth px/h
const SVG = { left: 100, top: 60, width: 3410, height: 640 };
const rowNames = ["P09", "P10", "P17", "P22"];
// ghost rect helpers: hour h -> viewport left; row i center -> viewport top
const leftAt = (h) => SVG.left + 50 + h * HW;
const topAtRow = (i, hgt) => SVG.top + 48 + i * 40 + 20 - hgt / 2;
const ghost = (h, row, w, hgt) => ({
  left: leftAt(h), top: topAtRow(row, hgt || 32), width: w || 110, height: hgt || 32 });
const base = {
  svgRect: SVG, viewStart: 0, viewEnd: 336, hourWidth: HW,
  rowNames, durationH: 11, sourceLineName: "P09",
};
const r = {};
r.exact = dp.planDrop({ ...base, translated: ghost(10, 1) });
r.snapUp = dp.planDrop({ ...base, translated: ghost(9.6, 1) });
r.snapDown = dp.planDrop({ ...base, translated: ghost(9.4, 1) });
r.sameRowKeepsSide = dp.planDrop({
  ...base, sourceLineName: "P17A", translated: ghost(10, 2) });
r.crossRowUsesGroup = dp.planDrop({
  ...base, sourceLineName: "P17A", translated: ghost(10, 0) });
r.clampRight = dp.planDrop({ ...base, translated: ghost(500, 1) });
r.clampLeft = dp.planDrop({ ...base, translated: ghost(-25, 1) });
r.rowClampAbove = dp.planDrop({
  ...base, translated: { ...ghost(10, 0), top: SVG.top - 30 } });
r.rowClampBelow = dp.planDrop({ ...base, translated: ghost(10, 9) });
r.holdingInside = dp.planDrop({
  ...base, sourceLineName: undefined, requireInsideRows: true,
  translated: ghost(10, 3) });
r.holdingOutside = dp.planDrop({
  ...base, sourceLineName: undefined, requireInsideRows: true,
  translated: ghost(10, 9) });

// Scroll invariance.
// (a) Parent-page scroll moves the iframe, not anything inside it: both
//     rects unchanged -> trivially same. Simulate the equivalent whole-frame
//     shift anyway: BOTH rects offset by the same vector -> identical plan.
const shift = (rc, dx, dy) => ({ ...rc, left: rc.left + dx, top: rc.top + dy });
r.pageShift = dp.planDrop({
  ...base,
  translated: shift(ghost(10, 1), -37, 121),
  svgRect: shift(SVG, -37, 121),
});
// (b) Container scroll (.gantt-scroll): content slides left under the
//     pointer-frozen ghost -> svgRect moves, translated does not. The
//     landing must advance by exactly the scrolled amount.
r.containerScroll = dp.planDrop({
  ...base, translated: ghost(10, 1), svgRect: shift(SVG, -200, 0) });
process.stdout.write(JSON.stringify(r));
"""


def _compile_plan_ts(tmp: Path) -> Path:
    tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        pytest.skip("frontend node_modules not installed (npm install in frontend dir)")
    out = tmp / "compiled"
    proc = subprocess.run(
        [
            "node", str(tsc), str(PLAN_TS),
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
    # dropPlan.ts pulls in only layout/abLines, so the common source root is
    # src/utils and dropPlan.js lands at the outDir top level.
    js = out / "dropPlan.js"
    assert js.exists(), f"expected {js}"
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_drop_plan_math(tmp_path):
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    out = _compile_plan_ts(tmp_path)
    proc = subprocess.run(
        ["node", str(runner_path), str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    r = json.loads(proc.stdout)

    # Left edge on hour 10, center on row 1: lands exactly there.
    p = r["exact"]
    assert p["snappedStartHour"] == 10 and p["rowIdx"] == 1
    assert p["lineName"] == "P10" and p["valid"] and p["reason"] is None
    assert p["endHour"] == 21  # start + durationH

    # Whole-hour snap of the ghost's left edge, both directions.
    assert r["snapUp"]["snappedStartHour"] == 10
    assert r["snapDown"]["snappedStartHour"] == 9

    # A side-named block dropped back on its own row keeps its side name;
    # dropped on another row it takes that row's (group) name.
    assert r["sameRowKeepsSide"]["lineName"] == "P17A"
    assert r["crossRowUsesGroup"]["lineName"] == "P09"

    # Start clamps into [viewStart, viewEnd - duration].
    assert r["clampRight"]["snappedStartHour"] == 336 - 11
    assert r["clampLeft"]["snappedStartHour"] == 0

    # Row clamps to the band for chart drags (still valid: the ghost shows
    # the clamped row, and that is where the drop lands).
    assert r["rowClampAbove"]["rowIdx"] == 0 and r["rowClampAbove"]["valid"]
    assert r["rowClampBelow"]["rowIdx"] == 3 and r["rowClampBelow"]["valid"]

    # Holding drags require the ghost's center on a real row.
    assert r["holdingInside"]["valid"] and r["holdingInside"]["lineName"] == "P22"
    p = r["holdingOutside"]
    assert not p["valid"] and "production line" in p["reason"]

    # Scroll invariance: a whole-frame shift (parent-page scroll analogue)
    # changes nothing; a container scroll advances the landing by exactly
    # the scrolled amount (200px / 10px-per-h = 20h).
    assert r["pageShift"] == r["exact"]
    assert r["containerScroll"]["snappedStartHour"] == 30
    assert r["containerScroll"]["rowIdx"] == 1
