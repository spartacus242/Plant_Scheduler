# tests/test_sku_picker_math.py — blank-space SKU picker placement math.
#
# utils/skuPicker.ts is the pure half of the picker: snap-left placement
# (start at prev.end + setup, floored at now/lock; duration capped by the
# next block minus ITS setup and the remaining demand), the changeover-flag
# bitmask decode (bit order mirrors helpers/calendar_io.CO_FLAG_COLUMNS),
# and the target-order pick that makes the placed kg credit a real demand
# order. Compiled with the frontend's own tsc and pinned under node — the
# same harness pattern as test_kpi_parity / test_adherence_week_filter.
# Skipped (not passed) when node or the frontend's node_modules are missing.

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
PICKER_TS = FRONTEND / "src" / "utils" / "skuPicker.ts"

RUNNER_JS = """
const path = require("path");
const out = process.argv[2];
const sp = require(path.join(out, "utils", "skuPicker.js"));
const layout = require(path.join(out, "utils", "layout.js"));

const co = { A: { N: 2 }, N: { B: 3 } };
const mk = (sku, s, e, t) => ({ sku, start_hour: s, end_hour: e, block_type: t || "sku" });
const base = {
  clickHour: 20, sku: "N", rate: 100, remainingKg: 1500,
  blocksOnLine: [mk("A", 0, 10), mk("B", 40, 60)],
  changeovers: co, lockedThroughH: null, nowH: 0, horizonH: 336,
  minRunH: 4, lineGroup: "P09", downtime: {},
};
const r = {};
r.snapLeft = sp.planPlacement(base);
r.roomClamp = sp.planPlacement({ ...base, remainingKg: 5000 });
r.minRun = sp.planPlacement({ ...base, remainingKg: 100 });
r.noRoom = sp.planPlacement({ ...base, clickHour: 12,
  blocksOnLine: [mk("A", 0, 10), mk("B", 17, 30)] });
r.lockFloor = sp.planPlacement({ ...base, lockedThroughH: 20, clickHour: 25 });
r.nowFloor = sp.planPlacement({ ...base, nowH: 22.4, clickHour: 30 });
r.emptyLine = sp.planPlacement({ ...base, blocksOnLine: [], clickHour: 5 });
r.insideBlock = sp.planPlacement({ ...base, clickHour: 5 });
r.afterCip = sp.planPlacement({ ...base,
  blocksOnLine: [mk("CIP", 0, 10, "cip"), mk("B", 40, 60)] });
r.flags = {
  f5: sp.coFlagLabels(5), f32: sp.coFlagLabels(32),
  f0: sp.coFlagLabels(0), fu: sp.coFlagLabels(undefined),
};

// Demand-side helpers: order-id week suffixes count from the demand anchor's
// ISO week (here W32); today W34 makes -W0/-W1 past, -W2 the current week.
layout.setDemandBaseWeek(32);
const anchor = new Date("2026-08-17T00:00:00");
const now = new Date("2026-08-19T10:00:00");
const adhRow = (oid, min, max, sched) => ({
  order_id: oid, sku: "S", qty_min: min, qty_max: max, scheduled_qty: sched,
  pct_adherence: 0, status: "UNDER", avg_rate_kgph: 0 });
const adh = [
  adhRow("S-W0", 900, 1100, 0),    // past week: excluded everywhere
  adhRow("S-W2", 900, 1000, 950),  // met (target 950)
  adhRow("S-W3", 400, 600, 100),   // 400 left of target 500
];
const demand = [
  { order_id: "S-W0", sku: "S", qty_min: 900, qty_max: 1100, due_start_hour: -336 },
  { order_id: "S-W2", sku: "S", qty_min: 900, qty_max: 1000, due_start_hour: 0 },
  { order_id: "S-W3", sku: "S", qty_min: 400, qty_max: 600, due_start_hour: 168 },
];
r.remaining = sp.remainingDemandBySku(adh, anchor, now);
// Per-order split: 15h/1500kg -> W3 has 400kg room, W4 (added) 2000kg room.
const demand4 = demand.concat([
  { order_id: "S-W4", sku: "S", qty_min: 1800, qty_max: 2200, due_start_hour: 336 },
]);
r.split = sp.splitPlacementByOrders(
  "S", { startHour: 12, durationH: 15, qtyKg: 1500 }, adh, demand4, anchor, now);
// Overrun: only W3's 400kg room is open -> the rest stays on W3 (one block).
r.splitOverrun = sp.splitPlacementByOrders(
  "S", { startHour: 12, durationH: 15, qtyKg: 1500 }, adh, demand, anchor, now);
// All orders met: whole run lands on the LAST open order, never a past week.
r.splitAllMet = sp.splitPlacementByOrders(
  "S", { startHour: 12, durationH: 15, qtyKg: 1500 },
  adh.map((x) => ({ ...x, scheduled_qty: 5000 })), demand, anchor, now);
process.stdout.write(JSON.stringify(r));
"""


def _compile_picker_ts(tmp: Path) -> Path:
    tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        pytest.skip("frontend node_modules not installed (npm install in frontend dir)")
    out = tmp / "compiled"
    proc = subprocess.run(
        [
            "node", str(tsc), str(PICKER_TS),
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
    # skuPicker.ts pulls in types/abLines/layout/kpi, so the common source
    # root is src/ and skuPicker.js lands under utils/.
    js = out / "utils" / "skuPicker.js"
    assert js.exists(), f"expected {js}"
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_sku_picker_math(tmp_path):
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    out = _compile_picker_ts(tmp_path)
    proc = subprocess.run(
        ["node", str(runner_path), str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    r = json.loads(proc.stdout)

    # Snap left: start = prev.end (10) + setup A->N (2); duration = the
    # 1500kg @ 100kg/h; room to B (40) minus setup N->B (3) is 25h — no clamp.
    p = r["snapLeft"]
    assert p["reason"] is None
    assert p["startHour"] == 12 and p["durationH"] == 15 and p["qtyKg"] == 1500
    assert p["prevSku"] == "A" and p["nextSku"] == "B"
    assert p["setupBeforeH"] == 2 and p["setupAfterH"] == 3

    # Gap capacity clamps: 5000kg wants 50h but only 37-12=25h fit.
    p = r["roomClamp"]
    assert p["durationH"] == 25 and p["qtyKg"] == 2500

    # Tiny remainder still runs the plant minimum (min_run_hours = 4).
    p = r["minRun"]
    assert p["durationH"] == 4 and p["qtyKg"] == 400

    # 2h free after both setups < 4h min run: not placeable, reason set.
    assert r["noRoom"]["reason"] is not None

    # Lock boundary floors the start even when prev.end+setup is earlier.
    p = r["lockFloor"]
    assert p["startHour"] == 20 and p["durationH"] == 15 and p["qtyKg"] == 1500

    # Wall clock floors the start; room shrinks accordingly (rounded to 0.1h).
    p = r["nowFloor"]
    assert p["startHour"] == pytest.approx(22.4)
    assert p["durationH"] == pytest.approx(14.6)
    assert p["qtyKg"] == pytest.approx(1460)

    # Empty line: starts at hour 0, no setups, open to the horizon.
    p = r["emptyLine"]
    assert p["startHour"] == 0 and p["durationH"] == 15
    assert p["prevSku"] is None and p["nextSku"] is None

    # Click landed on a block: the picker refuses (defensive — blocks stop
    # propagation, so this should not happen from the UI).
    assert "Not empty space" in (r["insideBlock"]["reason"] or "")

    # A CIP neighbour absorbs the changeover: no setup, no chips — and the
    # plan says WHAT sits there (prevType) so the UI can label "after CIP".
    p = r["afterCip"]
    assert p["startHour"] == 10 and p["setupBeforeH"] == 0
    assert p["prevSku"] is None and p["nextSku"] == "B"
    assert p["prevType"] == "cip" and p["nextType"] == "sku"

    # Bitmask decode (bit order = helpers/calendar_io.CO_FLAG_COLUMNS).
    assert r["flags"]["f5"] == ["ttp", "tpld"]
    assert r["flags"]["f32"] == ["cin-non"]
    assert r["flags"]["f0"] == [] and r["flags"]["fu"] == []

    # Remaining demand: past weeks excluded, met orders contribute 0.
    assert r["remaining"] == {"S": 400}

    # Per-order split, earliest due first: W3 takes its 400kg room (4h of
    # the 15h run), W4 takes the remaining 1100kg (11h), contiguous.
    assert r["split"] == [
        {"orderId": "S-W3", "startHour": 12, "durationH": 4, "qtyKg": 400},
        {"orderId": "S-W4", "startHour": 16, "durationH": 11, "qtyKg": 1100},
    ]
    # Overrun beyond every open order's room stays on the last touched order.
    assert r["splitOverrun"] == [
        {"orderId": "S-W3", "startHour": 12, "durationH": 15, "qtyKg": 1500},
    ]
    # All met: the whole run lands on the LAST open order, never a past week.
    assert r["splitAllMet"] == [
        {"orderId": "S-W3", "startHour": 12, "durationH": 15, "qtyKg": 1500},
    ]
