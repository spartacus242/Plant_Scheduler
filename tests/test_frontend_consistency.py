# tests/test_frontend_consistency.py — Supply Timeline cleanup pass (review
# findings of 2026-09-01, frontend_consistency track).
#
# Pins the fixes the pure-utils harnesses cannot see:
#   1. a split MO piece is never dropped over its own sibling: the drag
#      preview and the insert plan exclude the dragged PIECE (blockIdentity.
#      sameBlock), not every block carrying its id;
#   5. the insert plan names the displaced neighbour by piece key (nextKey),
#      so a split MO's SECOND piece is the one found, drawn and slid;
#   3. the holding-card pill judges from max(now, lock) — the Place
#      popover's base — never from a lock boundary already in the past;
#   4. holding draggables register `holding_<piece key>`, so two parked
#      pieces of one split MO do not collide in dnd-kit's registry;
#   6. the popover's kg-basis caption stays silent when the engine had no
#      cases at all (NO_DATA with no worst item);
#   2/7/8. the sandbox's banner discipline (stock-gated clears, no-op-proof
#      pending windows) and the acknowledge reading — source contracts, since
#      GanttSandbox needs a DOM to drive.
# dragPreview.ts is compiled with the frontend's own tsc and pinned under
# node (the test_sku_picker_math harness). BlockPopover / HoldingArea compile
# the same way and render with react-dom/server out of the frontend's own
# node_modules (NODE_PATH): static markup is enough to read a caption or a
# pill's title, and a stub over @dnd-kit/core records the draggable ids.
# Skipped (not passed) when node or the frontend's node_modules are missing.
# Synthetic inputs plus the golden stock fixture; nothing live is read.

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.timefmt import hour_to_stamp  # noqa: E402

FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
SRC = FRONTEND / "src"
ENTRIES = [
    SRC / "utils" / "dragPreview.ts",
    SRC / "components" / "HoldingArea.tsx",
    SRC / "components" / "BlockPopover.tsx",
]
FIXTURE = ROOT / "data" / "test_fixtures" / "stock_risk" / "cases.json"

ANCHOR = "2026-08-31 00:00:00"
RULES4 = {"min_days_after_delivery": 4, "lead_measured_from": "block_start",
          "dependent_frac_floor": 0.05, "hard_block": False}

RUNNER_JS = r"""
const fs = require("fs");
const path = require("path");
const out = process.argv[2];
const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

// Spy over @dnd-kit/core BEFORE the components load: HoldingArea's compiled
// require() hits the same cached module and sees the patched exports.
const dndPath = require.resolve("@dnd-kit/core");
const realDnd = require(dndPath);
const draggableIds = [];
require.cache[dndPath].exports = {
  ...realDnd,
  useDraggable: (opts) => { draggableIds.push(String(opts.id)); return realDnd.useDraggable(opts); },
};

const dp = require(path.join(out, "utils", "dragPreview.js"));
const bi = require(path.join(out, "utils", "blockIdentity.js"));
const sg = require(path.join(out, "utils", "supplyGlue.js"));
const { HoldingArea } = require(path.join(out, "components", "HoldingArea.js"));
const { BlockPopover } = require(path.join(out, "components", "BlockPopover.js"));

const r = {};
const [Y, M0, D] = job.anchor_parts;
const anchor = new Date(Y, M0, D, 0, 0, 0);
const stamp = sg.stampFor(anchor);

// --- F1 / F5: a split MO (two pieces, ONE id) and a plain block on P10 ---
const mk = (id, s, e) => ({ id, line_id: 1, line_name: "P10", order_id: `O-${id}`, sku: "S",
  start_hour: s, end_hour: e, run_hours: e - s, is_trial: false, block_type: "sku", qty_kg: 0 });
const A = mk("cs", 0, 10);
const B = mk("cs", 12, 20);
const C = mk("c", 30, 40);
const all = [A, B, C];
const lines = [{ line_id: 1, line_name: "P10", line_group: "P10", is_double: false }];
const caps = { P10: { S: 100 } };
const ctx = { setupBetween: () => 0, horizonH: 336, lockedThroughH: null, isBlockLocked: () => false };
const geom = (start, dur) => ({ rowIdx: 0, lineName: "P10", snappedStartHour: start,
  endHour: start + dur, valid: true, reason: null });
const preview = (block, start, insertCtx) => dp.computeDragPreview({
  block, activeId: bi.blockKey(block), overId: "line_P10", plan: geom(start, block.run_hours),
  lines, caps, allBlocks: all, anchor, downtime: {}, insertCtx });
const slim = (p) => p && ({ valid: p.valid, reason: p.reason, startHour: p.startHour, endHour: p.endHour,
  insert: p.insert ? { nextId: p.insert.nextId, nextKey: p.insert.nextKey, deltaH: p.insert.deltaH,
                       shiftedCount: p.insert.shiftedCount, blockedReason: p.insert.blockedReason } : p.insert });
r.drag = {
  sibling_plain: slim(preview(B, 4)),          // B over A, no insert context -> refused
  sibling_insert: slim(preview(B, 4, ctx)),    // B over A with insert -> A is the displaced piece
  self_plain: slim(preview(C, 35)),            // C over itself only -> free move
  self_split: slim(preview(B, 14)),            // B inside its own span -> free move
};
const planC = dp.computeInsertPlan(C, "P10", 15, 10, all, ctx);   // C dropped inside B
r.plan_second_piece = {
  nextId: planC.nextId, nextKey: planC.nextKey,
  by_id_start: all.find((b) => b.id === planC.nextId).start_hour,
  by_key_start: all.find((b) => bi.blockKey(b) === planC.nextKey).start_hour,
  deltaH: planC.deltaH, shiftedCount: planC.shiftedCount,
};
const planB = dp.computeInsertPlan(B, "P10", 4, 8, all, ctx);      // B dropped onto its sibling
r.plan_sibling = planB && { nextKey: planB.nextKey, deltaH: planB.deltaH, shiftedCount: planB.shiftedCount };
const pv = preview(B, 4, ctx);
r.same_preview = {
  identical: dp.samePreview(pv, { ...pv, insert: { ...pv.insert } }),
  other_piece: dp.samePreview(pv, { ...pv, insert: { ...pv.insert, nextKey: "cs|12" } }),
};

// --- F3 / F4 / F6: the golden fixture's case A as a StockArgs payload -----
const fix = JSON.parse(fs.readFileSync(job.fixture, "utf8"));
const inp = fix.cases.find((c) => c.name === "A_worked_table_L96").inputs;
const kpc = inp.sku_needs["280351"].kg_per_case;
const stock = {
  feed_state: inp.feed_state,
  as_of: { stock_rm: "9/1 14:46", stock_pkg: "9/1 14:46", po: "9/1 06:10" },
  receipts_window_end_h: inp.receipts_window_end_h,
  rules: job.rules4,
  opening: inp.opening, tracked: inp.tracked, in_house: inp.in_house,
  units: { "754751": "EA" }, designations: { "754751": "SLEEVE 1X24X90" },
  snapshot_h: inp.snapshot_h, receipts: inp.receipts, sku_needs: inp.sku_needs,
  cases_left: {},
};
const toBoard = (b) => ({
  id: b.block_id, line_id: 1, line_name: b.line_name, order_id: `MO-${b.block_id}`,
  sku: b.sku, start_hour: b.start_h, end_hour: b.end_h, run_hours: b.end_h - b.start_h,
  is_trial: false, block_type: "sku", qty_kg: b.cases * kpc,
  locked: b.locked, attrs: b.running ? "current_state:running" : "",
});
const board = inp.blocks.map(toBoard);
const p19 = board.find((b) => b.line_name === "P19");
const rest = board.filter((b) => b !== p19);
const rate19 = p19.qty_kg / p19.run_hours;
const capsHold = { P19: { "280351": rate19 } };
const timelines = sg.evaluateSchedule(rest, stock, capsHold, null).timelines;
// The P19 run held back as a card (its board hours, its kg).
const card = { ...p19, line_name: "", line_id: 0 };

const PILL = /data-testid="safe-start-pill" data-tone="(\w+)" title="([^"]*)"[^>]*>([^<]*)</;
const pillOf = (props) => {
  const h = renderToStaticMarkup(React.createElement(HoldingArea, {
    blocks: [card], anchor, skuFormats: {}, stock, supplyTimelines: timelines, caps: capsHold,
    demandTargets: [], supplyStamp: stamp, ...props }));
  const m = PILL.exec(h);
  return m ? { tone: m[1], title: m[2], text: m[3] } : null;
};
r.pill = {
  base0: pillOf({ lockedThroughH: null, nowH: null }),
  now50: pillOf({ lockedThroughH: null, nowH: 50 }),
  lock40_now10: pillOf({ lockedThroughH: 40, nowH: 10 }),
  lock40_now50: pillOf({ lockedThroughH: 40, nowH: 50 }),
};
r.stamps = { h0: stamp(0), h40: stamp(40), h50: stamp(50) };

// Two parked pieces of ONE split MO: the ids dnd-kit is handed.
draggableIds.length = 0;
const pieceA = { ...A, id: "cs_split", start_hour: 0.017, end_hour: 119.733, run_hours: 119.716, line_name: "" };
const pieceB = { ...B, id: "cs_split", start_hour: 125.733, end_hour: 188.112, run_hours: 62.379, line_name: "" };
renderToStaticMarkup(React.createElement(HoldingArea, { blocks: [pieceA, pieceB], anchor, skuFormats: {} }));
r.draggable_ids = [...draggableIds];

// Popover caption per basis.
const sup = (over) => ({ key: "k", verdict: "OK", backed: false, minor: false, mid_run: false,
  item: null, covered_frac: null, depletion_h: null, lead_h: null, safe_from_h: null, binding: null,
  action: "none", lead_from: "block_start", items: [], untracked: [], co_consumers: {}, text: "", ...over });
const blk = (over) => ({ id: "b1", line_id: 1, line_name: "P19", order_id: "X-W36", sku: "280351",
  start_hour: 10, end_hour: 20, run_hours: 10, is_trial: false, block_type: "sku", qty_kg: 0, ...over });
const popHtml = (block, supply) => renderToStaticMarkup(React.createElement(BlockPopover, {
  block, x: 0, y: 0, rate: 0, anchor, onClose: () => {}, supply, stock }));
r.caption = {
  no_recipe: popHtml(blk({ sku: "999999" }), sup({ verdict: "NO_DATA" })),
  no_kg_no_rate: popHtml(blk({}), sup({ verdict: "NO_DATA" })),
  kg: popHtml(blk({ qty_kg: 1000 }), sup({ item: "754751" })),
  rate_hours: popHtml(blk({}), sup({ item: "754751" })),
  nodata_with_item: popHtml(blk({}), sup({ verdict: "NO_DATA", item: "754751" })),
};

process.stdout.write(JSON.stringify(r));
"""


def _compile(tmp: Path) -> Path:
    tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        pytest.skip("frontend node_modules not installed (npm install in frontend dir)")
    out = tmp / "compiled"
    proc = subprocess.run(
        [
            "node", str(tsc), *[str(p) for p in ENTRIES],
            "--outDir", str(out),
            "--rootDir", str(SRC),
            "--module", "commonjs",
            "--target", "es2020",
            "--moduleResolution", "node",
            "--jsx", "react-jsx",
            "--skipLibCheck",
            "--esModuleInterop",
            "--strict",
        ],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert proc.returncode == 0, f"tsc failed:\n{proc.stdout}\n{proc.stderr}"
    for rel in ("utils/dragPreview.js", "components/HoldingArea.js", "components/BlockPopover.js"):
        assert (out / rel).exists(), f"expected {out / rel}"
    return out


@pytest.fixture(scope="module")
def fe(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node not available")
    tmp = tmp_path_factory.mktemp("frontend_consistency")
    out = _compile(tmp)
    job_path = tmp / "job.json"
    job_path.write_text(json.dumps({
        "fixture": str(FIXTURE), "anchor_parts": [2026, 7, 31], "rules4": RULES4,
    }), encoding="utf-8")
    runner_path = tmp / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    env = {**os.environ, "NODE_PATH": str(FRONTEND / "node_modules")}
    proc = subprocess.run(
        ["node", str(runner_path), str(out), str(job_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# 1 + 5: split MO pieces in the drag preview / insert plan
# --------------------------------------------------------------------------

def test_split_piece_cannot_drop_over_its_sibling(fe):
    """Piece B (cs, 12-20) dragged onto piece A (cs, 0-10): before the fix
    the id-based exclusion dropped A from the obstacle list and the ghost
    read valid. Now the plain drop is refused and the insert gesture names A
    (by key) as the displaced piece. A block over only itself still moves."""
    d = fe["drag"]
    assert d["sibling_plain"]["valid"] is False
    assert d["sibling_plain"]["reason"].startswith("Overlap on P10 at ")
    ins = d["sibling_insert"]
    assert ins["valid"] is True and ins["reason"] is None
    assert ins["insert"] == {"nextId": "cs", "nextKey": "cs|0", "deltaH": 12.0,
                             "shiftedCount": 2, "blockedReason": None}
    assert d["self_plain"]["valid"] is True and d["self_plain"]["insert"] is None
    assert d["self_split"]["valid"] is True and d["self_split"]["insert"] is None


def test_insert_plan_names_the_displaced_piece_by_key(fe):
    """C dropped inside the SECOND piece of the split MO: nextId ("cs")
    resolves to the first piece, nextKey to the one actually hit — the
    sandbox and the chart ghost resolve by key. Dropping a piece onto its
    sibling displaces the sibling (and the block after it)."""
    p = fe["plan_second_piece"]
    assert (p["nextId"], p["nextKey"]) == ("cs", "cs|12")
    assert p["by_id_start"] == 0 and p["by_key_start"] == 12
    assert p["shiftedCount"] == 1          # only B slides: A precedes the drop, C is the dragged block
    assert fe["plan_sibling"] == {"nextKey": "cs|0", "deltaH": 12.0, "shiftedCount": 2}
    assert fe["same_preview"] == {"identical": True, "other_piece": False}


# --------------------------------------------------------------------------
# 3: holding-card pill base
# --------------------------------------------------------------------------

def test_holding_pill_judges_from_now_or_lock(fe):
    """The card's earliest clear start is measured from max(now, lock): the
    title's first line quotes the hour judged. Case A's P19 run clears at
    136 h whichever base applies, so the pill text is stable and only the
    judged-at stamp moves — 0 h, now 50 h, the 40 h lock over a 10 h now."""
    pills, st = fe["pill"], fe["stamps"]
    assert st == {"h0": hour_to_stamp(0, ANCHOR), "h40": hour_to_stamp(40, ANCHOR),
                  "h50": hour_to_stamp(50, ANCHOR)}
    for k in ("base0", "now50", "lock40_now10", "lock40_now50"):
        assert pills[k] is not None, k
        assert pills[k]["tone"] == "warn" and pills[k]["text"].startswith("🚚 from "), k
    # first title line is "at <stamp>: <sentence>" — the stamp has its own ":"
    at = {k: html.unescape(v["title"]).split(": ", 1)[0] for k, v in pills.items()}
    assert at["base0"] == f"at {st['h0']}"
    assert at["now50"] == f"at {st['h50']}"
    assert at["lock40_now10"] == f"at {st['h40']}"
    assert at["lock40_now50"] == f"at {st['h50']}"
    assert pills["base0"]["text"] == pills["now50"]["text"] == pills["lock40_now50"]["text"]


# --------------------------------------------------------------------------
# 4: holding draggable ids
# --------------------------------------------------------------------------

def test_holding_draggables_are_keyed_by_piece(fe):
    """Two parked pieces of one split MO register distinct dnd-kit ids —
    `holding_` + blockKey — instead of colliding on `holding_<id>`."""
    assert fe["draggable_ids"] == ["holding_cs_split|0.017", "holding_cs_split|125.733"]
    sandbox = (SRC / "GanttSandbox.tsx").read_text(encoding="utf-8")
    # The drop resolves the card from the carried object; the key is only
    # the fallback (never the bare id).
    assert "findBlock(holdingArea, carried)" in sandbox
    assert "holdingArea.find((b) => blockKey(b) === activeKey)" in sandbox
    assert "holdingArea.find((b) => b.id === blockId)" not in sandbox


# --------------------------------------------------------------------------
# 6: popover kg-basis caption
# --------------------------------------------------------------------------

def test_popover_caption_has_no_basis_for_null_cases(fe):
    """NO_DATA with no worst item (no recipe, or no kg and no rate) prints
    'No quantity basis' — never 'rate × hours'. Every other verdict keeps
    the kg / rate wording, including a NO_DATA that still names an item."""
    cap = {k: html.unescape(v) for k, v in fe["caption"].items()}
    for k in ("no_recipe", "no_kg_no_rate"):
        assert "No quantity basis for this block." in cap[k], k
        assert "Need is based on" not in cap[k] and "rate × hours" not in cap[k], k
    assert "Need is based on this block's kg (qty ÷ kg/case)" in cap["kg"]
    assert "Need is based on rate × hours (no kg on the block)" in cap["rate_hours"]
    assert "Need is based on rate × hours (no kg on the block)" in cap["nodata_with_item"]
    for k in ("kg", "rate_hours", "nodata_with_item"):
        assert "No quantity basis" not in cap[k], k


# --------------------------------------------------------------------------
# 2 / 7 / 8 (+ the sandbox halves of 1 and 5): source contracts
# --------------------------------------------------------------------------

def test_sandbox_banner_and_piece_contracts():
    """GanttSandbox needs a DOM to drive, so its halves of the findings are
    pinned at source level: (2) every commit-path banner clear goes through
    the stock-gated helper, leaving only the helper body and the banner's
    own dismiss as raw clears; (7) queued supply windows carry the schedule
    they were queued against and a no-op commit drops them; (8) the
    acknowledge reading is stated where supplyFor hides the verdict; (1/5)
    no overlap check excludes by bare id and the displaced neighbour is
    resolved by piece key in both the sandbox and the chart."""
    sandbox = (SRC / "GanttSandbox.tsx").read_text(encoding="utf-8")
    chart = (SRC / "components" / "GanttChart.tsx").read_text(encoding="utf-8")
    drag = (SRC / "utils" / "dragPreview.ts").read_text(encoding="utf-8")
    # 2 — stock-gated clears
    assert "if (stockEnabled) setWarnMsg(null);" in sandbox
    assert sandbox.count("setWarnMsg(null)") == 2, "raw clears: helper body + banner dismiss only"
    assert sandbox.count("clearSupplyBanner();") == 10
    # 7 — no-op-proof pending windows
    assert "pendingSupply.current = { schedule, windows };" in sandbox
    assert "if (pend.schedule === schedule) return;" in sandbox
    # 8 — acknowledge hides chip + hatch + tick, said where it happens
    m = re.search(r"((?:\s*//[^\n]*\n)+)\s*const supplyFor = useCallback", sandbox)
    assert m and "hatch" in m.group(1) and "tick" in m.group(1)
    # 1 — piece-only exclusion everywhere an overlap is checked
    for name, src in (("GanttSandbox", sandbox), ("dragPreview", drag)):
        assert re.search(r"findOverlapsOnLine\([^)]*\bblock\.id\b", src) is None, name
        assert "b.id !== dragged.id" not in src, name
    # 5 — displaced neighbour by piece key
    assert sandbox.count("blockKey(n) === plan.nextKey") == 2
    assert "blockKey(b) === insertPreview.nextKey" in chart
    assert "b.id === insertPreview.nextId" not in chart
