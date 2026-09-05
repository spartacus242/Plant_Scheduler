# tests/test_fix_FE.py — regression tests for fix round FE (frontend parity
# and identity, 2026-09-03). Every expected value below is derived BY HAND
# in the comment next to it; the TypeScript under test is compiled with the
# frontend's own tsc and driven under node (the test_sku_picker_math
# harness). Skipped (not passed) when node or the frontend's node_modules
# are missing. Nothing live is read.
#
# Findings covered (audit round 2 / round 1):
#   writeback-8  collision-free block ids + save guard      (blockIdentity)
#   ui-1 / ui-14 holding card re-priced on the target line  (validation)
#   ui-4         mean capable rate over DISTINCT lines      (rates)
#   ui-8         '+' button sizes from tonnage / rate       (AdherenceTable)
#   ui-5         one weekly changeover rule                 (kpi)
#   C23/time-7   naive TS clock (no DST hour)               (layout)
#   C24/time-8   real ISO week labels, (year, week) keys    (layout)
#   C37/changeover-9  cip_req 6 h floor + ceil setup        (skuPicker)
#   C53/cip-14   CIP re-forecast on the wall-clock grid     (cipReforecast)

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
SRC = FRONTEND / "src"
ENTRIES = [
    SRC / "utils" / "blockIdentity.ts",
    SRC / "utils" / "layout.ts",
    SRC / "utils" / "validation.ts",
    SRC / "utils" / "holdingDerive.ts",
    SRC / "utils" / "skuPicker.ts",
    SRC / "utils" / "cipReforecast.ts",
    SRC / "utils" / "kpi.ts",
]

RUNNER_JS = r"""
const path = require("path");
const out = process.argv[2];
const U = (n) => path.join(out, "utils", n + ".js");
const bi = require(U("blockIdentity"));
const L = require(U("layout"));
const V = require(U("validation"));
const H = require(U("holdingDerive"));
const R = require(U("rates"));
const SP = require(U("skuPicker"));
const CR = require(U("cipReforecast"));
const K = require(U("kpi"));
const r = {};

// ── writeback-8: ids ───────────────────────────────────────────────────────
const ids = new Set();
for (let i = 0; i < 5000; i++) ids.add(bi.mintBlockId("blk"));
// "remount": a fresh module instance must not re-mint any earlier id
delete require.cache[require.resolve(U("blockIdentity"))];
const bi2 = require(U("blockIdentity"));
const ids2 = new Set();
for (let i = 0; i < 5000; i++) ids2.add(bi2.mintBlockId("blk"));
let calls = 0;
const taken = { has: () => { calls += 1; return calls === 1; } };
bi.mintBlockId("blk", taken);
r.mint = {
  unique: ids.size === 5000 && ids2.size === 5000,
  shape: [...ids].every((s) => /^blk_[0-9a-z]+_[0-9a-z]{13}$/.test(s)),
  acrossMounts: [...ids2].every((s) => !ids.has(s)),
  retriedOnTaken: calls === 2,
  prefix: bi.mintBlockId("cip").startsWith("cip_"),
};
r.collisions = bi.findIdCollisions([
  { id: "blk_1", order_id: "O1", sku: "111", block_type: "sku" },
  { id: "blk_1", order_id: "O9", sku: "999", block_type: "sku" },   // two runs, one id
  { id: "cs_split", order_id: "MO7", sku: "222", block_type: "sku" },
  { id: "cs_split", order_id: "MO7", sku: "222", block_type: "sku" }, // split pieces: fine
  { id: "blk_2", order_id: "O2", sku: "111", block_type: "sku" },
]);

// ── ui-1 / ui-14 / ui-4: the 280103 card of the audit ─────────────────────
// capabilities_rates keyed by GROUP, then expand_caps_with_groups adds the
// sides at half rate (pages/calendar.py:388).
const caps = {
  P09: { "280103": 716 }, P15: { "280103": 630 },
  P17: { "280103": 1454 }, P17A: { "280103": 727 }, P17B: { "280103": 727 },
  P18: { "280103": 1486 }, P18A: { "280103": 743 }, P18B: { "280103": 743 },
  P20: { "280103": 1475 }, P20A: { "280103": 737.5 }, P20B: { "280103": 737.5 },
};
const card = { id: "hold_280103-W4", line_id: 0, line_name: "", order_id: "280103-W4", sku: "280103",
  start_hour: 0, end_hour: 170.6, run_hours: 170.6, is_trial: false, block_type: "sku", qty_kg: 196512 };
r.dur = {
  P09: V.recalcDuration(card, "P09", caps, {}, 0),
  P15: V.recalcDuration(card, "P15", caps, {}, 0),
  P17: V.recalcDuration(card, "P17", caps, {}, 0),
  P18: V.recalcDuration(card, "P18", caps, {}, 0),
  P17_sideDown: V.recalcDuration(card, "P17", caps, { P17A: [[0, 1000]] }, 0),
  noKg: V.recalcDuration({ ...card, qty_kg: 0 }, "P15", caps, {}, 0),
  noRate: V.recalcDuration(card, "P11", caps, {}, 0),
};
r.kg = {
  full: V.placedQtyKg(card, "P15", 0, 312, caps, {}),
  clamped: V.placedQtyKg(card, "P15", 0, 100, caps, {}),
  sideDown: V.placedQtyKg(card, "P17", 0, 271, caps, { P17A: [[0, 1000]] }),
  unknownStaysUnknown: V.placedQtyKg({ ...card, qty_kg: undefined }, "P15", 0, 100, caps, {}) === undefined,
};
r.rate = {
  expanded: R.meanCapableRate(caps, "280103"),
  groupsOnly: R.meanCapableRate({ P09: { "280103": 716 }, P15: { "280103": 630 }, P17: { "280103": 1454 },
                                  P18: { "280103": 1486 }, P20: { "280103": 1475 } }, "280103"),
  sidesOnly: R.meanCapableRate({ P17A: { "280103": 727 }, P17B: { "280103": 727 }, P09: { "280103": 716 } }, "280103"),
  unknown: R.meanCapableRate(caps, "nope"),
  viaHoldingDerive: H.meanCapableRate(caps, "280103"),
};
// The derived card: same kg / same mean rate / same two-step rounding as
// holding_builder -> to_payload, so the first edit is a pricing no-op.
L.setDemandBaseWeek(null);
const cards = H.deriveAutoHolding([], [
  { order_id: "280103-W4", sku: "280103", qty_min: 196512, qty_max: 240000, due_start_hour: 672, due_end_hour: 839 },
], caps, undefined, new Date(), {});
r.card = cards.map((c) => ({ id: c.id, run_hours: c.run_hours, qty_kg: c.qty_kg, line_name: c.line_name }));

// ── C23 / time-7: naive clock (node runs with TZ=America/New_York) ────────
const oct26 = new Date(2026, 9, 26);          // Mon 2026-10-26 00:00 local, DST ends 11-01
r.time = {
  mondays: L.mondayBoundaries(oct26, 0, 336),
  stamp168: L.hourToStamp(168, oct26),
  label167: L.hourToDateLabel(167, oct26),
  time167: L.hourToTimeLabel(167, oct26),
  short168: L.hourToShortStamp(168, oct26),
  isoAt169: L.isoWeekAtHour(oct26, 169),
  isoKeyAt169: L.isoWeekKeyAtHour(oct26, 169),
  nowH: L.nowHour(oct26, new Date(2026, 10, 2, 0, 0)),
  nowHHalf: L.nowHour(oct26, new Date(2026, 10, 2, 6, 30)),
  elapsedMsHours: (new Date(2026, 10, 2, 0, 0).getTime() - oct26.getTime()) / 3600000, // the REAL elapsed hours (169 in a DST zone)
  tzOffsetsDiffer: oct26.getTimezoneOffset() !== new Date(2026, 10, 2).getTimezoneOffset(),
};

// ── C24 / time-8: ISO week labels across a 53-week year ───────────────────
const dec21 = new Date(2026, 11, 21);          // Mon 2026-12-21 = W52-2026
const dec30 = new Date(2026, 11, 30);          // Wed of W53-2026
L.setDemandBaseWeek(52);
r.iso = {
  labels: [0, 1, 2, 3].map((k) => L.isoWeekLabel(k, dec21)),
  keys: [0, 1, 2, 3].map((k) => L.demandWeekKey(k, dec21)),
  pastW0: L.isPastDemandWeek("X-W0", dec21, dec30),
  pastW1: L.isPastDemandWeek("X-W1", dec21, dec30),
  pastW2: L.isPastDemandWeek("X-W2", dec21, dec30),
  display: L.displayOrderId("X-W1", dec21),
  nowKey: L.isoWeekKey(dec30),
};
L.setDemandBaseWeek(52, "2026-12-23 00:00:00");   // payload date pins the Monday
r.iso.labelsPinned = [0, 1, 2].map((k) => L.isoWeekLabel(k, dec21));
L.setDemandBaseWeek(53);                            // base week 53 seen from W1-2027
r.iso.from2027 = [L.isoWeekLabel(0, new Date(2027, 0, 4)), L.isoWeekLabel(1, new Date(2027, 0, 4)),
                  L.demandWeekKey(0, new Date(2027, 0, 4)), L.demandWeekKey(1, new Date(2027, 0, 4))];
L.setDemandBaseWeek(null);                          // legacy hour math
r.iso.legacy = [L.isoWeekLabel(0, dec21), L.isoWeekLabel(1, dec21), L.isoWeekLabel(2, dec21)];

// ── C37 / changeover-9: setup floor + ceil, chips ─────────────────────────
r.setup = {
  q125: SP.effectiveSetupHours(1.25, false, 6),
  q025: SP.effectiveSetupHours(0.25, false, 6),
  whole2: SP.effectiveSetupHours(2, false, 6),
  cipReq1: SP.effectiveSetupHours(1, true, 6),
  cipReq8: SP.effectiveSetupHours(8, true, 6),
  unknown: SP.effectiveSetupHours(undefined, false, 6),
  cipReq0: SP.effectiveSetupHours(0, true, 6),
  isReq: SP.isCipReqPair({ "A|B": { recipe: 1, format: 0, hours: 1.5, cip_req: 1 } }, "A", "B"),
  isReqRev: SP.isCipReqPair({ "A|B": { recipe: 1, format: 0, hours: 1.5, cip_req: 1 } }, "B", "A"),
  isReqSelf: SP.isCipReqPair({ "A|A": { recipe: 0, format: 0, hours: 0, cip_req: 1 } }, "A", "A"),
};
r.chips = {
  flagsPlusReq: SP.coFlagsForPair({ "A|B": 5 }, "A", "B", { "A|B": { recipe: 1, format: 0, hours: 1.5, cip_req: 1 } }),
  fromPairsOnly: SP.coFlagsForPair(undefined, "A", "B", { "A|B": { recipe: 0, format: 1, hours: 2, tl: 1, cip_req: 0 } }),
  unknownStill: SP.coFlagsForPair({}, "A", "B", undefined),
  bit64: SP.coFlagLabels(64),
  self: SP.coFlagsForPair({}, "A", "A", { "A|A": { recipe: 0, format: 0, hours: 0, cip_req: 1 } }),
};
const mk = (sku, s, e, t) => ({ sku, start_hour: s, end_hour: e, block_type: t || "sku" });
const base = {
  clickHour: 20, sku: "N", rate: 100, remainingKg: 1500,
  blocksOnLine: [mk("A", 0, 10), mk("B", 40, 60)],
  changeovers: { A: { N: 1.25 }, N: { B: 3 } }, lockedThroughH: null, nowH: 0, horizonH: 336,
  minRunH: 4, lineGroup: "P09", downtime: {},
};
r.plan = {
  ceilDefault: (({ startHour, setupBeforeH, durationH }) => ({ startHour, setupBeforeH, durationH }))(SP.planPlacement(base)),
  withFloor: (({ startHour, setupBeforeH, setupAfterH, durationH }) => ({ startHour, setupBeforeH, setupAfterH, durationH }))(
    SP.planPlacement({ ...base, setupFor: (f, t) => (f === "A" && t === "N" ? 6 : 3) })),
};

// ── C53 / cip-14: re-forecast on the wall-clock grid ──────────────────────
let n = 0;
const mint = () => `new_${++n}`;
const blk = (id, s, e, kg, extra) => ({ id, line_id: 1, line_name: "P10", order_id: `O-${id}`, sku: "S" + id,
  start_hour: s, end_hour: e, run_hours: e - s, is_trial: false, block_type: "sku", qty_kg: kg, ...(extra || {}) });
const A = blk("A", 0, 50, 5000), B = blk("B", 60, 100, 4000), C = blk("C", 100, 140, 8000);
const line = (res) => res.schedule.filter((b) => b.line_name === "P10")
  .sort((x, y) => x.start_hour - y.start_hour).map((b) => [b.order_id, b.start_hour, b.end_hour, b.qty_kg]);
const starts = (res) => res.added.map((c) => [c.start_hour, c.end_hour, c.attrs]);
const sched0 = [A, B, C];
const opts = (over) => ({ lineName: "P10", lineId: 1, startHour: 50, duration: 6, intervalH: 100, horizonH: 400,
  schedule: sched0, windows: [], mintId: mint, ...(over || {}) });
const free = CR.reforecastCips(opts());
r.cip = {
  free: { starts: starts(free), sameArray: free.schedule === sched0, line: line(free), skipped: free.skipped,
          nWindows: free.windows.length },
  split: (() => { const res = CR.reforecastCips(opts({ intervalH: 40 }));
    return { starts: starts(res), line: line(res), skipped: res.skipped }; })(),
  immovable: (() => { const res = CR.reforecastCips(opts({ intervalH: 40, immovable: (b) => b.order_id === "O-B" }));
    return { starts: starts(res).map((x) => x[0]), line: line(res), skipped: res.skipped }; })(),
  nearExisting: (() => {
    const committed = { id: "cs_cip", line_id: 1, line_name: "P10", order_id: "CIP", sku: "CIP", start_hour: 150.5,
      end_hour: 156.5, run_hours: 6, is_trial: false, block_type: "cip", attrs: "current_state:cip" };
    const res = CR.reforecastCips(opts({ windows: [committed] }));
    return { starts: starts(res).map((x) => x[0]), windows: res.windows.map((w) => w.start_hour), skipped: res.skipped }; })(),
  replaceOld: (() => {
    const proj = (ln, s) => ({ id: `p_${ln}_${s}`, line_id: ln === "P10" ? 1 : 2, line_name: ln, order_id: "CIP", sku: "CIP",
      start_hour: s, end_hour: s + 6, run_hours: 6, is_trial: false, block_type: "cip", attrs: "planner:cip_projected" });
    const res = CR.reforecastCips(opts({ windows: [proj("P10", 200), proj("P11", 200), proj("P10", 20)] }));
    return { windows: res.windows.map((w) => `${w.line_name}@${w.start_hour}`), starts: starts(res).map((x) => x[0]) }; })(),
  pushStart: (() => {
    const B2 = blk("B", 152, 180, 4000);
    const res = CR.reforecastCips(opts({ schedule: [A, B2] }));
    return { starts: starts(res).map((x) => x[0]), line: line(res) }; })(),
  splitOnly: (() => {
    const res = CR.insertCleanAt([A, B, C], "P10", 90, 6, () => false, mint);
    return { line: res.map((b) => [b.order_id, b.start_hour, b.end_hour, b.qty_kg]), newIds: res.filter((b) => b.id.startsWith("new_")).length }; })(),
  freeSlotSameArray: (() => { const s = [A, B, C]; return CR.insertCleanAt(s, "P10", 300, 6, () => false, mint) === s; })(),
};

// ── ui-5: weekly changeover rows (hand case) ──────────────────────────────
const p = (sku, s, e) => ({ id: `${sku}${s}`, line_id: 1, line_name: "P10", order_id: "", sku, start_hour: s, end_hour: e,
  run_hours: e - s, is_trial: false, block_type: "sku" });
const cipW = { id: "cip20", line_id: 1, line_name: "P10", order_id: "CIP", sku: "CIP", start_hour: 20, end_hour: 26,
  run_hours: 6, is_trial: false, block_type: "cip" };
const pairs = { "A|B": { recipe: 0, format: 1, hours: 3, tl: 1, ffs: 0, cp: 0, ttp: 0, cip_req: 0 },
                "C|A": { recipe: 1, format: 0, hours: 1.5, tl: 0, ffs: 0, cp: 0, ttp: 0, cip_req: 1 } };
const dflt = { recipe: 1, format: 0, hours: 1.5, tl: 0, ffs: 0, cp: 0, ttp: 0, cip_req: 0 };
r.weekly = K.countChangeoversByWeek(
  [p("A", 0, 10), p("B", 10, 20), p("C", 26, 30), p("A", 30, 40)], pairs, dflt, [cipW], [0, 24], 48);

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
    return out


@pytest.fixture(scope="module")
def fe(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node not available")
    tmp = tmp_path_factory.mktemp("fix_fe")
    out = _compile(tmp)
    runner = tmp / "runner.js"
    runner.write_text(RUNNER_JS, encoding="utf-8")
    # A DST zone, so the naive-clock assertions bite: 2026-11-01 falls back.
    env = {**os.environ, "TZ": "America/New_York"}
    proc = subprocess.run(
        ["node", str(runner), str(out)],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


# ── writeback-8 ─────────────────────────────────────────────────────────────

def test_block_ids_are_collision_free_across_mounts(fe):
    """5,000 ids per "mount" and two mounts (a fresh module instance, the
    way a page reload resets module scope) share nothing: the old
    `blk_${_nextId++}` re-minted blk_1, blk_2 ... on every mount. A taken
    id is retried; the prefix is caller-chosen."""
    m = fe["mint"]
    assert m == {"unique": True, "shape": True, "acrossMounts": True,
                 "retriedOnTaken": True, "prefix": True}


def test_save_guard_names_the_colliding_ids_only(fe):
    """blk_1 on two different runs (O1/111 and O9/999) is a collision; the
    two cs_split pieces of MO7 are one run and legitimately share an id."""
    assert fe["collisions"] == ["blk_1"]


def test_frontend_source_no_longer_has_a_module_counter():
    src = (SRC / "hooks" / "useScheduleState.ts").read_text(encoding="utf-8")
    # the counter as CODE (a comment may still name it as history)
    assert "let _nextId" not in src and "_nextId++" not in src
    assert "mintBlockId(" in src
    sandbox = (SRC / "GanttSandbox.tsx").read_text(encoding="utf-8")
    assert "findIdCollisions(" in sandbox, "push must refuse duplicate ids"


# ── ui-1 / ui-14 ───────────────────────────────────────────────────────────

def test_holding_card_is_priced_at_the_target_line_rate(fe):
    """196,512 kg (the audit's hold_280103-W4). hoursForQty integrates the
    line's rate hour by hour and rounds UP to whole hours:
      P09 716 kg/h : 196512 / 716  = 274.46 -> 275 h
      P15 630 kg/h : 196512 / 630  = 311.92 -> 312 h
      P17 1454 kg/h: 196512 / 1454 = 135.15 -> 136 h
      P18 1486 kg/h: 196512 / 1486 = 132.24 -> 133 h
    The pre-fix code returned the card's 170.6 h on every one of them
    (implied 1,152 kg/h on P15 = 83% above its physical rate). With side A
    of P17 down for the whole window the line runs at 727 kg/h:
    196512 / 727 = 270.3 -> 271 h. A card with no kg and no source rate
    keeps its hours (nothing to re-price from); an incapable line is null."""
    d = fe["dur"]
    assert d == {"P09": 275, "P15": 312, "P17": 136, "P18": 133,
                 "P17_sideDown": 271, "noKg": 170.6, "noRate": None}


def test_placed_kg_never_exceeds_line_capacity_or_card_demand(fe):
    """placedQtyKg = min(card kg, rate x placed hours):
      full  312 h on P15: 630 x 312 = 196,560 >= 196,512 -> the card's 196,512
      clamped to 100 h : 630 x 100 = 63,000 -> 63,000 (what the line makes)
      P17 side down 271 h: 727 x 271 = 197,017 -> 196,512
      unknown kg stays unknown (never invented)."""
    assert fe["kg"] == {"full": 196512, "clamped": 63000, "sideDown": 196512,
                        "unknownStaysUnknown": True}


# ── ui-4 ───────────────────────────────────────────────────────────────────

def test_mean_capable_rate_counts_each_line_once(fe):
    """SKU 280103 is capable on P09 716, P15 630, P17 1454, P18 1486, P20
    1475: mean = 5761 / 5 = 1152.2 kg/h (holding_builder.average_rate_per_sku
    over the raw rows). The group-expanded map (sides at half rate) must
    give the SAME number — the pre-fix loop over every key read 925.1. A
    sides-only map (P17A 727 + P17B 727, P09 716) collapses to two lines:
    (1454 + 716) / 2 = 1085."""
    r = fe["rate"]
    assert r["expanded"] == pytest.approx(1152.2)
    assert r["groupsOnly"] == pytest.approx(1152.2)
    assert r["viaHoldingDerive"] == pytest.approx(1152.2)
    assert r["sidesOnly"] == pytest.approx(1085.0)
    assert r["unknown"] == 0


def test_derived_card_equals_the_server_card(fe):
    """Server (holding_builder): run_hours = round(196512 / 1152.2, 2) =
    round(170.5537.., 2) = 170.55; to_payload rounds to 1 dp -> 170.6;
    qty_kg 196512. The client card must carry the same numbers so the first
    edit is a pricing no-op (sameAutoCards compares run_hours); the
    pre-fix client read 212.4 h for the same kg."""
    assert fe["card"] == [{"id": "hold_280103-W4", "run_hours": 170.6,
                           "qty_kg": 196512, "line_name": ""}]


# ── ui-8 ───────────────────────────────────────────────────────────────────

def test_adherence_plus_button_sizes_from_tonnage_and_rate():
    """Source contract (the table needs a DOM to click): the '+' handler
    prices missing kg at the SKU's mean capable rate handed in by the
    sandbox, with no 24 h cap and no `|| 1` divisor."""
    table = (SRC / "components" / "AdherenceTable.tsx").read_text(encoding="utf-8")
    assert "Math.min(24" not in table
    assert "avg_rate_kgph || 1" not in table
    assert "rateFor" in table
    sandbox = (SRC / "GanttSandbox.tsx").read_text(encoding="utf-8")
    assert re.search(r"rateFor=\{\(sku\) => meanCapableRate\(caps, sku\)\}", sandbox)


# ── C23 / time-7 ───────────────────────────────────────────────────────────

def test_ts_clock_is_naive_like_python(fe):
    """Anchor Mon 2026-10-26 00:00 in America/New_York; DST ends 11-01 so
    Mon 11-02 00:00 is 169 REAL hours later. Python (naive datetime) calls
    it h168 and every week 168 h; so must the board:
      mondayBoundaries(0..336) -> [0, 168]      (local math gave [0, 169])
      hourToStamp(168)         -> "Mon 11/2 00:00" (local: "Sun 11/1 23:00")
      h167                     -> "Sun 11/1" "23:00"
      isoWeekAtHour(169)       -> 45 (Nov 2 2026 starts ISO W45), key 202645
      nowHour(anchor, 11-02 00:00) -> 168, 06:30 -> 174.5
    The runner also proves the zone really switched (elapsed 169 h, offsets
    differ) so a UTC node would not pass this vacuously."""
    t = fe["time"]
    assert t["tzOffsetsDiffer"] is True and t["elapsedMsHours"] == 169
    assert t["mondays"] == [0, 168]
    assert t["stamp168"] == "Mon 11/2 00:00"
    assert t["label167"] == "Sun 11/1" and t["time167"] == "23:00"
    assert t["short168"] == "11/2 00:00"
    assert t["isoAt169"] == 45 and t["isoKeyAt169"] == 202645
    assert t["nowH"] == 168 and t["nowHHalf"] == 174.5


# ── C24 / time-8 ───────────────────────────────────────────────────────────

def test_iso_week_labels_survive_a_53_week_year(fe):
    """Demand anchored W52-2026 (Mon 12-21; 2026 has 53 ISO weeks, Dec 31
    is a Thursday). Index k = the ISO week k weeks after Mon 12-21:
      k=0 W52-2026, k=1 W53-2026, k=2 W1-2027, k=3 W2-2027
    keys year*100+week: 202652, 202653, 202701, 202702. The old
    `((52 + k - 1) % 52) + 1` read [52, 1, 2, 3] and, with now in W53,
    judged the W53 order (label 1 < 53) "past". Now = Wed 12-30 (W53):
    only W0 is past. displayOrderId("X-W1") -> "X-W53". A payload demand
    anchor date (12-23 -> Monday 12-21) pins the same labels; base week 53
    seen from Mon 2027-01-04 (W1-2027) resolves to W53-2026 (2027 has no
    week 53) -> [53, 1] / [202653, 202701]. Legacy (no base week): Mon
    12-21 + k*168 h + 1 h -> [52, 53, 1]."""
    i = fe["iso"]
    assert i["labels"] == [52, 53, 1, 2]
    assert i["keys"] == [202652, 202653, 202701, 202702]
    assert i["nowKey"] == 202653
    assert (i["pastW0"], i["pastW1"], i["pastW2"]) == (True, False, False)
    assert i["display"] == "X-W53"
    assert i["labelsPinned"] == [52, 53, 1]
    assert i["from2027"] == [53, 1, 202653, 202701]
    assert i["legacy"] == [52, 53, 1]


# ── C37 / changeover-9 ─────────────────────────────────────────────────────

def test_effective_setup_hours_ceil_and_cip_req_floor(fe):
    """Solver rule (changeover_cache.round_setup_hours + apply_cip_req_setup_floor):
    1.25 -> 2, 0.25 -> 1, 2 -> 2, unknown -> 0; a cip_req pair floors at the
    6 h clean (1 -> 6, 0 -> 6) but a longer standard stays (8 -> 8)."""
    s = fe["setup"]
    assert (s["q125"], s["q025"], s["whole2"], s["unknown"]) == (2, 1, 2, 0)
    assert (s["cipReq1"], s["cipReq8"], s["cipReq0"]) == (6, 8, 6)
    assert (s["isReq"], s["isReqRev"], s["isReqSelf"]) == (True, False, False)


def test_picker_chips_show_cip_req(fe):
    """coFlags mask 5 = ttp + tpld; co_pairs says cip_req -> the "CIP req"
    chip is appended. A pair the coFlags table lacks but co_pairs knows
    reads its machine flags (tl -> tpld) instead of "?"; a pair neither
    knows stays unknown (null); same-SKU is never a changeover; bit 64 is
    reserved for the server's future cip_req_after column."""
    c = fe["chips"]
    assert c["flagsPlusReq"] == ["ttp", "tpld", "CIP req"]
    assert c["fromPairsOnly"] == ["tpld"]
    assert c["unknownStill"] is None
    assert c["bit64"] == ["CIP req"]
    assert c["self"] == []


def test_picker_snap_uses_the_effective_setup(fe):
    """prev A ends h10, click h20, N at 100 kg/h for 1500 kg:
      default lookup A->N 1.25 h -> ceil 2 h -> start 12, 15 h run
      sandbox rule A->N cip_req -> 6 h -> start 16; N->B 3 h -> room
      40 - 3 - 16 = 21 h >= 15 h, so still a 15 h run."""
    p = fe["plan"]
    assert p["ceilDefault"] == {"startHour": 12, "setupBeforeH": 2, "durationH": 15}
    assert p["withFloor"] == {"startHour": 16, "setupBeforeH": 6, "setupAfterH": 3, "durationH": 15}


# ── C53 / cip-14 ───────────────────────────────────────────────────────────

def test_reforecast_marches_on_the_wall_clock_grid(fe):
    """Planner clean at h50 (6 h), interval 100, horizon 400, blocks A[0,50]
    B[60,100] C[100,140]: grid slots 150, 250, 350 are all free -> the
    schedule is untouched (same array), 4 cleans (planner + 3 projected),
    nothing skipped."""
    f = fe["cip"]["free"]
    assert f["starts"] == [[50, 56, "planner:cip"], [150, 156, "planner:cip_projected"],
                           [250, 256, "planner:cip_projected"], [350, 356, "planner:cip_projected"]]
    assert f["sameArray"] is True
    assert f["line"] == [["O-A", 0, 50, 5000], ["O-B", 60, 100, 4000], ["O-C", 100, 140, 8000]]
    assert f["skipped"] == 0 and f["nWindows"] == 4


def test_reforecast_splits_production_around_a_slot_and_pushes_the_queue(fe):
    """Interval 40 from h50 (_clip_prod_around_cips rule):
      t=90  inside B[60,100] (4000 kg): B -> [60,90] 3000 kg (30/40) +
            [96,106] 1000 kg (the 10 h tail pushed by the 6 h clean);
            C[100,140] starts before the cursor 106 -> pushed to [106,146]
      t=130 inside C[106,146] (8000 kg): -> [106,130] 4800 (24/40) +
            [136,152] 3200 (16 h tail)
      t=170, 210, 250, 290, 330, 370 free; 410 >= horizon stops.
    Kg always sums back (3000+1000 = 4000; 4800+3200 = 8000). The old
    frontend would have moved the h90 slot to B's end (h100) and re-phased
    the grid from there (140, 180, ...)."""
    s = fe["cip"]["split"]
    assert [x[0] for x in s["starts"]] == [50, 90, 130, 170, 210, 250, 290, 330, 370]
    assert s["line"] == [["O-A", 0, 50, 5000], ["O-B", 60, 90, 3000], ["O-B", 96, 106, 1000],
                         ["O-C", 106, 130, 4800], ["O-C", 136, 152, 3200]]
    assert s["skipped"] == 0


def test_reforecast_skips_a_slot_that_would_move_a_committed_block(fe):
    """Same grid, B immovable: the h90 slot would split B -> skipped (counted)
    and B stays [60,100]; the grid marches on: h130 lands inside C[100,140]
    (movable) -> C[100,130] 6000 (30/40) + [136,146] 2000; then 170 ... 370
    free. (Python would split the committed MO; the board cannot move it —
    stated deviation, see cipReforecast.ts.)"""
    s = fe["cip"]["immovable"]
    assert s["starts"] == [50, 130, 170, 210, 250, 290, 330, 370]
    assert s["skipped"] == 1
    assert s["line"] == [["O-A", 0, 50, 5000], ["O-B", 60, 100, 4000],
                         ["O-C", 100, 130, 6000], ["O-C", 136, 146, 2000]]


def test_reforecast_respects_cleans_already_on_the_line(fe):
    """A committed CIP at h150.5 is within 1 h of the h150 slot: the slot is
    skipped without counting (project_cips' `taken`), 250 and 350 land; the
    committed window survives. Old projected cleans AFTER the planner clean
    on THIS line are replaced (P10@200 gone), other lines' and earlier ones
    kept (P11@200, P10@20)."""
    n = fe["cip"]["nearExisting"]
    assert n["starts"] == [50, 250, 350] and n["skipped"] == 0
    assert sorted(n["windows"]) == [50, 150.5, 250, 350]
    o = fe["cip"]["replaceOld"]
    assert sorted(o["windows"]) == sorted(["P10@20", "P11@200", "P10@50", "P10@150", "P10@250", "P10@350"])
    assert o["starts"] == [50, 150, 250, 350]


def test_reforecast_pushes_a_block_whose_start_the_slot_overlaps(fe):
    """B[152,180] starts inside the h150 slot [150,156): pushed (no piece cut
    off) to [156,184], kg unchanged — Python's "overlapped start: pushed,
    no piece". insertCleanAt alone at h90 splits B and pushes C, minting
    two new ids for the pieces; a free slot returns the input array."""
    p = fe["cip"]["pushStart"]
    assert p["starts"] == [50, 150, 250, 350]
    assert p["line"] == [["O-A", 0, 50, 5000], ["O-B", 156, 184, 4000]]
    so = fe["cip"]["splitOnly"]
    assert so["line"] == [["O-A", 0, 50, 5000], ["O-B", 60, 90, 3000], ["O-B", 96, 106, 1000], ["O-C", 106, 146, 8000]]
    assert so["newIds"] == 2
    assert fe["cip"]["freeSlotSameArray"] is True


def test_cip_interval_comes_from_config_not_from_drawn_spacing():
    """Source contract: cipIntervalFor reads config.cip_interval_h[line] else
    the configured default — never infers the interval from the spacing of
    projected cleans already drawn (that spacing is an OUTPUT)."""
    sandbox = (SRC / "GanttSandbox.tsx").read_text(encoding="utf-8")
    m = re.search(r"const cipIntervalFor = useCallback\(\(lineName: string\): number => \{(.*?)\}, \[", sandbox, re.S)
    assert m, "cipIntervalFor not found"
    body = m.group(1)
    assert "cip_projected" not in body and "cip_interval_default_h" in body
    assert "immovable: isBlockLocked" in sandbox


# ── ui-5 ───────────────────────────────────────────────────────────────────

def test_weekly_changeover_rows_hand_case(fe):
    """P10: A[0,10] B[10,20] | CIP[20,26] | C[26,30] A[30,40]; marks [0, 24],
    horizon 48.
      A->B at h10 -> week 0: pair tl=1 format=1 recipe=0 hours 3
      B->C at h26: the CIP [20,26] fills the gap -> WAIVED (counted nowhere)
      C->A at h30 -> week 1: recipe=1, no machine -> recipe_only, hours 1.5,
                     cip_req with no CIP in the gap -> violation, gap at h20?
                     no: the gap is C.end=30 -> cip_gap start 30
    prod_h: week 0 = 10 + 10 = 20 (C and the second A lie past h24);
            week 1 = 4 + 10 = 14."""
    w = fe["weekly"]
    assert len(w) == 2
    w0, w1 = w
    assert (w0["idx"], w0["start_h"], w0["span_h"], w0["prod_h"]) == (0, 0, 24, 20)
    assert (w0["sku_transitions"], w0["recipe_changes"], w0["format_changes"]) == (1, 0, 1)
    assert (w0["topload_changes"], w0["recipe_only_changes"], w0["co_hours"], w0["cip_req_violations"]) == (1, 0, 3, 0)
    assert w0["cip_gap"] is None
    assert (w1["idx"], w1["start_h"], w1["span_h"], w1["prod_h"]) == (1, 24, 24, 14)
    assert (w1["sku_transitions"], w1["recipe_changes"], w1["format_changes"]) == (1, 1, 0)
    assert (w1["topload_changes"], w1["recipe_only_changes"], w1["co_hours"], w1["cip_req_violations"]) == (0, 1, 1.5, 1)
    assert w1["cip_gap"] == {"lineName": "P10", "lineId": 1, "start": 30}


def test_sandbox_week_chips_use_the_one_weekly_rule():
    """GanttSandbox.weekStats no longer carries its own transition loop: it
    consumes kpi.countChangeoversByWeek (the score_changeovers port)."""
    sandbox = (SRC / "GanttSandbox.tsx").read_text(encoding="utf-8")
    assert "countChangeoversByWeek(" in sandbox
    m = re.search(r"const weekStats = useMemo\(\(\) => \{(.*?)\n  \}, \[", sandbox, re.S)
    assert m, "weekStats not found"
    assert "cipsByLineId" not in m.group(1), "private CO loop must be gone"
