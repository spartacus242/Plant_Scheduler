# tests/test_supply_glue.py — board block -> supply verdict glue (client).
#
# utils/supplyGlue.ts is the pure seam between the Gantt's ScheduleBlock
# state and the timeline engine port (utils/stockRisk.ts): the block ->
# TimelineBlock mapping of contract 2026-09-01 §5 (cases from board kg, the
# rate x hours fallback, null cases -> NO_DATA, locked / running /
# cases_left), one evaluateSchedule per edit, previewSupply for the dragged
# block, the chip / banner wording of §9, and the before-placement helpers
# of slice 2b (earliestSafeStartFor / safeStartPill for holding cards and
# picker rows, receiptsByDay for the axis trucks). Compiled with the frontend's
# own tsc (stockRisk + blockIdentity ride along; both import-free) and
# pinned under node — the harness pattern of test_stock_risk_parity /
# test_sku_picker_math. The whole-board run replays case A of the golden
# fixture (data/test_fixtures/stock_risk/cases.json) through ScheduleBlock
# inputs and must land on the Python engine's verdicts. Skipped (not
# passed) when node or the frontend's node_modules are missing. Synthetic
# inputs only; nothing live is read.

from __future__ import annotations

import json
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
GLUE_TS = SRC / "utils" / "supplyGlue.ts"
FIXTURE = ROOT / "data" / "test_fixtures" / "stock_risk" / "cases.json"

TOL = 1e-6
ANCHOR = "2026-08-31 00:00:00"
RULES4 = {"min_days_after_delivery": 4, "lead_measured_from": "block_start",
          "dependent_frac_floor": 0.05, "hard_block": False}

RUNNER_JS = """
const fs = require("fs");
const path = require("path");
const out = process.argv[2];
const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const sg = require(path.join(out, "utils", "supplyGlue.js"));
const sr = require(path.join(out, "utils", "stockRisk.js"));

const fix = JSON.parse(fs.readFileSync(job.fixture, "utf8"));
const caseA = fix.cases.find((c) => c.name === "A_worked_table_L96");
const inp = caseA.inputs;
const kpc = inp.sku_needs["280351"].kg_per_case;

// StockArgs shape (contract §5) from the fixture case's engine inputs.
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

// Board rows: the fixture's three 280351 blocks as ScheduleBlocks (kg =
// cases x kg_per_case; the running MO carries the manprg attr).
const toBoard = (b) => ({
  id: b.block_id, line_id: 1, line_name: b.line_name, order_id: `MO-${b.block_id}`,
  sku: b.sku, start_hour: b.start_h, end_hour: b.end_h, run_hours: b.end_h - b.start_h,
  is_trial: false, block_type: "sku", qty_kg: b.cases * kpc,
  locked: b.locked, attrs: b.running ? "current_state:running" : "",
});
const schedule = inp.blocks.map(toBoard);
const caps = { P10: { "280351": 530 }, P19: { "280351": 800 } };

const r = {};

// --- toTimelineBlock -------------------------------------------------------
const base = schedule[1];
r.tb_kg = sg.toTimelineBlock(base, stock, caps, null);
r.tb_rate = sg.toTimelineBlock({ ...base, qty_kg: undefined }, stock, caps, null);
r.tb_rate_side = sg.toTimelineBlock({ ...base, qty_kg: 0, line_name: "P19A" }, stock, caps, null);
r.tb_norate = sg.toTimelineBlock({ ...base, qty_kg: 0, line_name: "P11" }, stock, caps, null);
r.tb_norecipe = sg.toTimelineBlock({ ...base, sku: "999999" }, stock, caps, null);
r.tb_window = sg.toTimelineBlock({ ...base, block_type: "cip" }, stock, caps, null);
r.tb_trial = sg.toTimelineBlock({ ...base, block_type: "trial" }, stock, caps, null);
r.tb_flags = {
  locked: sg.toTimelineBlock({ ...base, locked: true }, stock, caps, null).locked,
  completed: sg.toTimelineBlock({ ...base, completed: true }, stock, caps, null).locked,
  pinned: sg.toTimelineBlock({ ...base, pinned: true }, stock, caps, null).locked,
  inLock: sg.toTimelineBlock(base, stock, caps, 100).locked,
  atLock: sg.toTimelineBlock(base, stock, caps, base.start_hour).locked,
  free: sg.toTimelineBlock(base, stock, caps, 10).locked,
  running: sg.toTimelineBlock({ ...base, attrs: "current_state:running;x" }, stock, caps, null).running,
  notRunning: sg.toTimelineBlock({ ...base, attrs: "current_state:completed" }, stock, caps, null).running,
  casesLeft: sg.toTimelineBlock(base, { ...stock, cases_left: { [base.order_id]: 1234 } }, caps, null).cases_left,
  casesLeftMissing: sg.toTimelineBlock(base, stock, caps, null).cases_left,
};

// --- evaluateSchedule ------------------------------------------------------
const ev = sg.evaluateSchedule(schedule, stock, caps, null);
r.keys = [...ev.verdicts.keys()];
r.counts = ev.counts;
r.board = {};
for (const [k, v] of ev.verdicts) r.board[k] = v;
r.verdictFor = sg.verdictFor(ev.verdicts, schedule[1]) === ev.verdicts.get("cs_44c4bde023|71.85");
r.verdictForPair = sg.verdictFor(ev.verdicts, { id: "cs_87c620df4a", start_hour: 125.733 }).covered_frac;
r.verdictForMiss = sg.verdictFor(ev.verdicts, { id: "nope", start_hour: 0 });
r.verdictForNull = sg.verdictFor(null, schedule[0]);

// --- previewSupply ---------------------------------------------------------
const p19 = schedule[1];
const dur = p19.end_hour - p19.start_hour;
// Plan §2: start >= 136 h reads OK backed; the base copy is untouched.
const before = JSON.stringify(ev.timelines);
r.prev_safe = sg.previewSupply(p19, 136, 136 + dur, "P19", schedule, stock, caps, null, ev.timelines);
r.prev_same = sg.previewSupply(p19, p19.start_hour, p19.end_hour, "P19", schedule, stock, caps, null, ev.timelines);
r.prev_nobase = sg.previewSupply(p19, 136, 136 + dur, "P19", schedule, stock, caps, null);
r.prev_window = sg.previewSupply({ ...p19, block_type: "cip" }, 136, 136 + dur, "P19", schedule, stock, caps, null, ev.timelines);
// A card from holding (not on the board) previews against every board block.
r.prev_holding = sg.previewSupply({ ...p19, id: "hold_x", line_name: "" }, 136, 136 + dur, "P19", schedule, stock, caps, null, ev.timelines);
r.base_untouched = JSON.stringify(ev.timelines) === before;

// --- chipFor / needsBanner -----------------------------------------------
const sup = (over) => ({ key: "k", verdict: "OK", backed: false, minor: false, mid_run: false,
  item: "754751", covered_frac: 0.393487, depletion_h: 95.321493, lead_h: 31.85,
  safe_from_h: 136, binding: null, action: "move", lead_from: "block_start",
  items: [], untracked: [], co_consumers: {}, text: "", ...over });
r.chips = {
  ok: sg.chipFor(sup({})),
  backed: sg.chipFor(sup({ backed: true })),
  dep: sg.chipFor(sup({ verdict: "DEPENDENT" })),
  depRound: sg.chipFor(sup({ verdict: "DEPENDENT", lead_h: 85.733 })),
  depHours: sg.chipFor(sup({ verdict: "DEPENDENT", lead_h: 13.733 })),
  depOneHour: sg.chipFor(sup({ verdict: "DEPENDENT", lead_h: 1.0 })),
  depDayEdge: sg.chipFor(sup({ verdict: "DEPENDENT", lead_h: 24.0 })),
  depMid: sg.chipFor(sup({ verdict: "DEPENDENT", lead_h: -39.983, mid_run: true })),
  depNoLead: sg.chipFor(sup({ verdict: "DEPENDENT", lead_h: null })),
  minor: sg.chipFor(sup({ verdict: "DEPENDENT", minor: true })),
  short: sg.chipFor(sup({ verdict: "SHORT" })),
  shortZero: sg.chipFor(sup({ verdict: "SHORT", covered_frac: 0 })),
  nodata: sg.chipFor(sup({ verdict: "NO_DATA" })),
  none: sg.chipFor(null),
};
r.banner = {
  ok: sg.needsBanner(sup({})), backed: sg.needsBanner(sup({ backed: true })),
  dep: sg.needsBanner(sup({ verdict: "DEPENDENT" })),
  minor: sg.needsBanner(sup({ verdict: "DEPENDENT", minor: true })),
  short: sg.needsBanner(sup({ verdict: "SHORT" })),
  nodata: sg.needsBanner(sup({ verdict: "NO_DATA" })), none: sg.needsBanner(null),
};

// --- isHardBlock ------------------------------------------------------------
const hb = { ...stock, rules: { ...job.rules4, hard_block: true }, opening: { "754751": 0 }, receipts: {} };
const shortSup = sup({ verdict: "SHORT" });
r.hard = {
  on: sg.isHardBlock(shortSup, hb),
  ruleOff: sg.isHardBlock(shortSup, { ...hb, rules: job.rules4 }),
  notShort: sg.isHardBlock(sup({ verdict: "DEPENDENT" }), hb),
  hasOpening: sg.isHardBlock(shortSup, { ...hb, opening: { "754751": 5 } }),
  hasReceipt: sg.isHardBlock(shortSup, { ...hb, receipts: inp.receipts }),
  feedStale: sg.isHardBlock(shortSup, { ...hb, feed_state: "stale" }),
  noItem: sg.isHardBlock(sup({ verdict: "SHORT", item: null }), hb),
  nullSup: sg.isHardBlock(null, hb),
  openingMissing: sg.isHardBlock(shortSup, { ...hb, opening: {} }),
};
// The worst item's group is pooled like the engine's (primary + recipe
// alternates, trimmed): stock or a truck on the alternate rescues the run.
const altNeeds = { "280351": { kg_per_case: kpc,
  items: [{ item: "754751", per_case: 1, unit: "EA", alts: [" 754752 ", ""] }] } };
const hbAlt = { ...hb, sku_needs: altNeeds };
r.hard.altStock = sg.isHardBlock(shortSup, { ...hbAlt, opening: { "754751": 0, "754752": 5 } });
r.hard.altReceipt = sg.isHardBlock(shortSup, { ...hbAlt, receipts: { "754752": inp.receipts["754751"] } });
r.hard.altEmpty = sg.isHardBlock(shortSup, { ...hbAlt, opening: { "754751": 0, "754752": 0 } });
r.hard.altSku = sg.isHardBlock(shortSup, { ...hbAlt, opening: { "754751": 0, "754752": 5 } }, "280351");
r.hard.otherSku = sg.isHardBlock(shortSup, { ...hbAlt, opening: { "754751": 0, "754752": 5 } }, "999999");

// --- humanText --------------------------------------------------------------
const stamp = sg.stampFor(job.anchor);
r.text = {};
for (const [k, v] of ev.verdicts) {
  r.text[k] = { glue: sg.humanText(v, stamp), engine: sr.verdictText(v, job.anchor) };
}
const binding = { po8: "30043543", qty: 67200, ready_h: 40, receipt_date: "2026-09-02", label: "" };
const withItem = (over) => sup({ items: [{ item: "754751", unit: "EA", need: 41435,
  opening_at_start: 22045, minH: -1, minP: 1, minR: -1, verdict: "DEPENDENT", backed: false,
  covered_frac: 0.393487, depletion_h: 95.321493, covered_frac_planned: 1,
  depletion_planned_h: null, binding, lead_h: 31.85, safe_from_h: 136, dependent_frac: 0.5,
  minor: false, mid_run: false }], binding, ...over });
r.text_forms = {
  short_nobinding: sg.humanText(withItem({ verdict: "SHORT", binding: null, depletion_h: 110 }), stamp),
  short_binding: sg.humanText(withItem({ verdict: "SHORT", depletion_h: 110 }), stamp),
  nodata: sg.humanText(withItem({ verdict: "NO_DATA" }), stamp),
  nodata_noitem: sg.humanText(sup({ verdict: "NO_DATA", item: null }), stamp),
  ok_noitem: sg.humanText(sup({ item: null }), stamp),
  ok_plain: sg.humanText(withItem({}), stamp),
  backed: sg.humanText(withItem({ backed: true, lead_h: 100.8 }), stamp),
  backed_hours: sg.humanText(withItem({ backed: true, lead_h: 1 }), stamp),
  dep_minor: sg.humanText(withItem({ verdict: "DEPENDENT", minor: true }), stamp),
  empty: sg.humanText(null, stamp),
};
// Sub-day leads through the real engine: P19 moved to start 1 h after the
// truck lands (41 h) and 3 h before it lands (37 h, mid-run).
const prev41 = sg.previewSupply(p19, 41, 41 + dur, "P19", schedule, stock, caps, null, ev.timelines);
const prev37 = sg.previewSupply(p19, 37, 37 + dur, "P19", schedule, stock, caps, null, ev.timelines);
const hoursProbe = (s) => ({ glue: sg.humanText(s, stamp), engine: sr.verdictText(s, job.anchor),
  verdict: s.verdict, lead_h: s.lead_h, mid_run: s.mid_run, chip: sg.chipFor(s) });
r.text_hours = { lead1: hoursProbe(prev41), mid3: hoursProbe(prev37) };
r.stamps = job.stamp_hours.map((h) => stamp(h));
r.days = [sg.days(31.85), sg.days(-39.983), sg.days(85.733), sg.days(0), sg.days(96)];
r.leads = [31.85, -39.983, 1, 13.733, -3, 0.4, 23.6, 24, 0, 96]
  .map((h) => [sg.leadText(h), sg.leadText(h, true)]);

// --- popover helpers -------------------------------------------------------
const p19sup = ev.verdicts.get("cs_44c4bde023|71.85");
r.sorted = sg.sortedItems({ ...p19sup, items: [
  { ...p19sup.items[0], item: "a", verdict: "OK", backed: false },
  { ...p19sup.items[0], item: "b", verdict: "SHORT" },
  { ...p19sup.items[0], item: "c", verdict: "OK", backed: true },
  { ...p19sup.items[0], item: "d", verdict: "DEPENDENT", minor: false },
  { ...p19sup.items[0], item: "e", verdict: "DEPENDENT", minor: true },
] }).map((e) => e.item);
r.counted = sg.countedReceipts(stock, "280351", "754751", 131.5).map((x) => x.po8);
r.counted_before = sg.countedReceipts(stock, "280351", "754751", 30).length;
r.counted_alt = sg.countedReceipts(
  { ...stock, sku_needs: { S: { kg_per_case: 1, items: [{ item: "X", per_case: 1, unit: "EA", alts: ["754751"] }] } } },
  "S", "X", 200).map((x) => x.po8);
r.casesFromKg = [sg.casesFromKg(base, stock), sg.casesFromKg({ ...base, qty_kg: 0 }, stock),
  sg.casesFromKg({ ...base, sku: "nope" }, stock)];
r.rules = sg.rulesOf(stock);
r.rules_none = sg.rulesOf(null).min_days_after_delivery;

// --- before placement (slice 2b) ------------------------------------------
// The P19 run as a HOLDING card: the board without it, the card's kg at
// its own rate, judged alone. Plan §2: nothing clears before 136 h.
const restNoP19 = schedule.filter((b) => b.id !== "cs_44c4bde023");
const evNo = sg.evaluateSchedule(restNoP19, stock, caps, null);
const p19kg = p19.qty_kg;
const p19rate = p19kg / dur;
const noBefore = JSON.stringify(evNo.timelines);
r.safe = sg.earliestSafeStartFor("280351", p19kg, p19rate, 0, evNo.timelines, stock, { lineName: "P19" });
r.safe_day = sg.dayIndexOf(r.safe.safe_from_h);
r.safe_from136 = sg.earliestSafeStartFor("280351", p19kg, p19rate, 136, evNo.timelines, stock);
r.safe_from_late = sg.earliestSafeStartFor("280351", p19kg, p19rate, 100, evNo.timelines, stock);
r.safe_norecipe = sg.earliestSafeStartFor("999999", 1000, 500, 0, evNo.timelines, stock);
r.safe_nokg = sg.earliestSafeStartFor("280351", 0, 0, 0, evNo.timelines, stock, { fallbackHours: 10 });
r.safe_norate = sg.earliestSafeStartFor("280351", p19kg, 0, 0, evNo.timelines, stock, { fallbackHours: dur });
r.safe_norate_nohours = sg.earliestSafeStartFor("280351", p19kg, 0, 0, evNo.timelines, stock);
r.safe_stale = sg.earliestSafeStartFor("280351", p19kg, p19rate, 0, evNo.timelines, { ...stock, feed_state: "stale" });
// On top of the FULL board (P19 already placed) the same run never clears.
r.safe_full = sg.earliestSafeStartFor("280351", p19kg, p19rate, 0, ev.timelines, stock);
// A 1,000 kg run at 20 h that on hand alone covers: plain OK, no truck.
r.safe_plain = sg.earliestSafeStartFor("280351", 1000, 500, 20, evNo.timelines, stock);
r.safe_untouched = JSON.stringify(evNo.timelines) === noBefore && JSON.stringify(ev.timelines) === before;
r.pills = {
  from: sg.safeStartPill(r.safe, stamp, null),
  fromDue: sg.safeStartPill(r.safe, stamp, 100),
  fromDueEdge: sg.safeStartPill(r.safe, stamp, 136),
  fromDueOk: sg.safeStartPill(r.safe, stamp, 200),
  clear: sg.safeStartPill(r.safe_from136, stamp, null),
  clearDue: sg.safeStartPill(r.safe_from136, stamp, 10),
  clearPlain: sg.safeStartPill(r.safe_plain, stamp, null),
  clearPlainDue: sg.safeStartPill(r.safe_plain, stamp, 10),
  norecipe: sg.safeStartPill(r.safe_norecipe, stamp, null),
  nokg: sg.safeStartPill(r.safe_nokg, stamp, null),
  norate: sg.safeStartPill(r.safe_norate, stamp, null),
  norateNohours: sg.safeStartPill(r.safe_norate_nohours, stamp, null),
  stale: sg.safeStartPill(r.safe_stale, stamp, null),
  none: sg.safeStartPill(r.safe_full, stamp, null),
};
r.byDay = [...sg.receiptsByDay(stock).entries()].map(([d, rs]) => [d, rs.map((x) => x.po8)]);
const multi = { ...stock, receipts: {
  "754751": [...inp.receipts["754751"],
    { ready_h: 47.9, qty: 1, po8: "30000001", tier: "appt", receipt_date: "2026-09-02", label: "" },
    { ready_h: -3, qty: 1, po8: "30000002", tier: "erp", receipt_date: "", label: "" }],
  "999": [{ ready_h: 40.5, qty: 2, po8: "30000000", tier: "erp", receipt_date: "", label: "" },
          { ready_h: "x", qty: 2, po8: "bad", tier: "erp", receipt_date: "", label: "" }],
} };
r.byDayMulti = [...sg.receiptsByDay(multi).entries()].sort((a, b) => a[0] - b[0])
  .map(([d, rs]) => [d, rs.map((x) => x.po8)]);
r.byDayEmpty = sg.receiptsByDay({ ...stock, receipts: {} }).size;
r.byDayShift = [...sg.receiptsByDay(stock, 12).keys()];
r.dayIdx = [sg.dayIndexOf(136), sg.dayIndexOf(40), sg.dayIndexOf(-0.5), sg.dayIndexOf(24), sg.dayIndexOf(23.999)];

process.stdout.write(JSON.stringify(r));
"""


def _compile(tmp: Path) -> Path:
    tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        pytest.skip("frontend node_modules not installed (npm install in frontend dir)")
    out = tmp / "compiled"
    proc = subprocess.run(
        [
            "node", str(tsc), str(GLUE_TS),
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
        timeout=120,
    )
    assert proc.returncode == 0, f"tsc failed:\n{proc.stdout}\n{proc.stderr}"
    js = out / "utils" / "supplyGlue.js"
    assert js.exists(), f"expected {js}"
    return out


STAMP_HOURS = [0.0, 40.0, 95.321493, 125.733, 136.0, -0.25, 719.999999]


@pytest.fixture(scope="module")
def glue(tmp_path_factory) -> tuple[dict, dict]:
    """(fixture case A, node result)."""
    if shutil.which("node") is None:
        pytest.skip("node not available")
    tmp = tmp_path_factory.mktemp("supply_glue")
    out = _compile(tmp)
    job_path = tmp / "job.json"
    job_path.write_text(json.dumps({
        "fixture": str(FIXTURE), "anchor": ANCHOR, "rules4": RULES4,
        "stamp_hours": STAMP_HOURS,
    }), encoding="utf-8")
    runner_path = tmp / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(runner_path), str(out), str(job_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case_a = next(c for c in fixture["cases"] if c["name"] == "A_worked_table_L96")
    return case_a, json.loads(proc.stdout)


# --------------------------------------------------------------------------
# toTimelineBlock (§5)
# --------------------------------------------------------------------------

def test_to_timeline_block_from_board_kg(glue):
    """key = id|start_hour, cases = qty_kg / kg_per_case, engine field names."""
    case_a, got = glue
    tb = got["tb_kg"]
    src = case_a["inputs"]["blocks"][1]
    assert tb["key"] == "cs_44c4bde023|71.85"
    assert tb["block_id"] == "cs_44c4bde023"
    assert (tb["sku"], tb["line_name"]) == ("280351", "P19")
    assert (tb["start_h"], tb["end_h"]) == (71.85, 131.5)
    assert abs(tb["cases"] - src["cases"]) <= TOL
    assert tb["locked"] is False and tb["running"] is False
    assert tb["cases_left"] is None
    assert set(tb) == {"key", "block_id", "sku", "line_name", "start_h", "end_h",
                       "cases", "locked", "running", "cases_left"}


def test_to_timeline_block_rate_fallback(glue):
    """No kg on the block: cases = rate x hours / kg_per_case from the
    line's capability row (a side name falls back to its group); no rate
    or no recipe -> null cases (NO_DATA, never green)."""
    _, got = glue
    hours = 131.5 - 71.85
    assert abs(got["tb_rate"]["cases"] - (800 * hours) / 2.16) <= TOL
    assert abs(got["tb_rate_side"]["cases"] - (800 * hours) / 2.16) <= TOL
    assert got["tb_norate"]["cases"] is None
    assert got["tb_norecipe"]["cases"] is None
    # windows and trials are not production draws
    assert got["tb_window"] is None and got["tb_trial"] is None


def test_to_timeline_block_flags(glue):
    f = glue[1]["tb_flags"]
    assert f["locked"] is True and f["completed"] is True and f["pinned"] is True
    assert f["inLock"] is True      # start 71.85 < locked_through 100
    assert f["atLock"] is False     # start == boundary is not inside
    assert f["free"] is False
    assert f["running"] is True and f["notRunning"] is False
    assert f["casesLeft"] == 1234 and f["casesLeftMissing"] is None


# --------------------------------------------------------------------------
# evaluateSchedule against the golden case
# --------------------------------------------------------------------------

def test_evaluate_schedule_matches_golden_case_a(glue):
    """The three 280351 blocks read exactly as the Python engine graded
    them (fixture keys use block_id@start; the client keys id|start_hour)."""
    case_a, got = glue
    exp = case_a["expected"]
    keymap = {"cs_87c620df4a@0.02": "cs_87c620df4a|0.017",
              "cs_44c4bde023@71.85": "cs_44c4bde023|71.85",
              "cs_87c620df4a@125.73": "cs_87c620df4a|125.733"}
    assert sorted(got["keys"]) == sorted(keymap.values())
    for fk, ck in keymap.items():
        e, g = exp[fk], got["board"][ck]
        assert g["key"] == ck
        for field in ("verdict", "item", "backed", "minor", "mid_run", "action"):
            assert g[field] == e[field], f"{ck}.{field}"
        for field in ("covered_frac", "depletion_h", "lead_h", "safe_from_h"):
            assert abs(g[field] - e[field]) <= TOL, f"{ck}.{field}"
        assert g["binding"]["po8"] == e["binding"]["po8"]
        assert len(g["items"]) == len(e["items"]) == 1
        assert abs(g["items"][0]["need"] - e["items"][0]["need"]) <= TOL
        # co-consumers name the other pieces by the CLIENT key
        co = {k: sorted(keymap[x] for x in v) for k, v in e["co_consumers"].items()}
        assert {k: sorted(v) for k, v in g["co_consumers"].items()} == co
    assert got["counts"] == {"short": 0, "dependent": 3, "no_data": 0, "backed": 0}


def test_verdict_for(glue):
    _, got = glue
    assert got["verdictFor"] is True
    assert abs(got["verdictForPair"] - 0.0) <= TOL
    assert got["verdictForMiss"] is None and got["verdictForNull"] is None


# --------------------------------------------------------------------------
# previewSupply
# --------------------------------------------------------------------------

def test_preview_supply(glue):
    """P19 moved to start >= 136 h reads OK backed (plan §2); at its own
    position the preview equals the board verdict; a base copy is never
    mutated; a rebuild without a base agrees; windows preview nothing."""
    _, got = glue
    safe = got["prev_safe"]
    assert (safe["verdict"], safe["backed"]) == ("OK", True)
    assert safe["key"] == "cs_44c4bde023|136"
    same = got["prev_same"]
    board = got["board"]["cs_44c4bde023|71.85"]
    assert (same["verdict"], same["covered_frac"], same["lead_h"]) == (
        board["verdict"], board["covered_frac"], board["lead_h"])
    nb = got["prev_nobase"]
    assert (nb["verdict"], nb["backed"], nb["lead_h"]) == (safe["verdict"], safe["backed"], safe["lead_h"])
    assert got["prev_window"] is None
    hold = got["prev_holding"]
    # A holding card is NOT on the board, so nothing is removed: it draws
    # on top of all three blocks (36,000 + 67,200 in, 123,795 out) and even
    # the truck cannot cover it — SHORT, where the moved P19 read OK.
    assert hold["key"] == "hold_x|136" and hold["verdict"] == "SHORT"
    assert got["base_untouched"] is True


# --------------------------------------------------------------------------
# chips, banners, hard block (§9)
# --------------------------------------------------------------------------

def test_chip_for(glue):
    c = glue[1]["chips"]
    assert c["ok"] is None and c["none"] is None
    assert c["backed"] == {"text": "🚚", "tone": "muted"}
    assert c["dep"] == {"text": "🚚 1.3d", "tone": "warn"}
    assert c["depRound"] == {"text": "🚚 3.6d", "tone": "warn"}
    # under a day the chip quotes whole hours (live: a truck 1 h before start)
    assert c["depHours"] == {"text": "🚚 14h", "tone": "warn"}
    assert c["depOneHour"] == {"text": "🚚 1h", "tone": "warn"}
    assert c["depDayEdge"] == {"text": "🚚 1.0d", "tone": "warn"}
    assert c["depMid"] == {"text": "🚚 mid-run", "tone": "warn"}
    assert c["depNoLead"] == {"text": "🚚", "tone": "warn"}
    assert c["minor"] == {"text": "🚚", "tone": "muted"}
    assert c["short"] == {"text": "⛔ 39%", "tone": "crit"}
    assert c["shortZero"] == {"text": "⛔ 0%", "tone": "crit"}
    assert c["nodata"] == {"text": "?", "tone": "muted"}


def test_needs_banner(glue):
    b = glue[1]["banner"]
    assert b == {"ok": False, "backed": False, "dep": True, "minor": False,
                 "short": True, "nodata": False, "none": False}


def test_is_hard_block(glue):
    h = glue[1]["hard"]
    assert h["on"] is True
    assert h["openingMissing"] is True
    for k in ("ruleOff", "notShort", "hasOpening", "hasReceipt", "feedStale", "noItem", "nullSup"):
        assert h[k] is False, k
    # the worst item's alternates pool like the engine's group: stock or a
    # truck on the alternate is a rescue; an empty alternate is not; naming
    # a SKU whose recipe has no alternate keeps the gate strict
    for k in ("altStock", "altReceipt", "altSku"):
        assert h[k] is False, k
    assert h["altEmpty"] is True and h["otherSku"] is True


# --------------------------------------------------------------------------
# wording
# --------------------------------------------------------------------------

def test_human_text_dependent_matches_engine_sentence(glue):
    """DEPENDENT reads byte-for-byte like verdict_text with the anchor — the
    same sentence Reconcile prints — with real minutes in the stamps."""
    _, got = glue
    for key, t in got["text"].items():
        assert t["glue"] == t["engine"], key
    p19 = got["text"]["cs_44c4bde023|71.85"]["glue"]
    assert p19 == ("🚚 754751: on hand covers 39% (runs out Thu 9/3 23:19) · "
                   "PO 30043543 lands Tue 9/1 16:00 — 1.3 d before start (min 4 d) · "
                   "safe from Sat 9/5 16:00")
    running = got["text"]["cs_87c620df4a|0.017"]["glue"]
    assert "1.7 d after start (mid-run)" in running


def test_human_text_other_forms(glue):
    f = glue[1]["text_forms"]
    assert f["short_nobinding"] == "⛔ 754751: runs out at 39% (Thu 9/3 23:19) — no receipt covers the rest"
    assert f["short_binding"] == ("⛔ 754751: runs out at 39% (Thu 9/3 23:19) — even with PO 30043543 "
                                  "(lands Tue 9/1 16:00) the line stops Fri 9/4 14:00")
    assert f["nodata"] == "? 754751: on hand covers 39% — no inbound data covering the gap"
    assert f["nodata_noitem"] == "? supply: no recipe or quantity data"
    assert f["ok_noitem"] == "✅ supply: no tracked components to check"
    assert f["ok_plain"] == "✅ 754751: on hand covers the run"
    assert f["backed"] == "🚚 754751 backed by PO 30043543 (lands Tue 9/1 16:00, 4.2 d before start)"
    assert f["backed_hours"] == "🚚 754751 backed by PO 30043543 (lands Tue 9/1 16:00, 1 h before start)"
    assert f["dep_minor"].endswith(" · minor share")
    assert f["empty"] == ""


def test_human_text_sub_day_lead_reads_in_hours(glue):
    """Live finding: a truck 1 h before start read "0.0 d before start".
    Under a day the sentence quotes whole hours, byte-identical between the
    glue and the engine port; the chip follows ("🚚 1h", "🚚 mid-run")."""
    t = glue[1]["text_hours"]
    for k in ("lead1", "mid3"):
        assert t[k]["verdict"] == "DEPENDENT", k
        assert t[k]["glue"] == t[k]["engine"], k
        assert "0.0 d" not in t[k]["glue"], k
    assert abs(t["lead1"]["lead_h"] - 1.0) <= TOL and t["lead1"]["mid_run"] is False
    assert "PO 30043543 lands Tue 9/1 16:00 — 1 h before start (min 4 d)" in t["lead1"]["glue"]
    assert t["lead1"]["chip"] == {"text": "🚚 1h", "tone": "warn"}
    assert abs(t["mid3"]["lead_h"] + 3.0) <= TOL and t["mid3"]["mid_run"] is True
    assert "PO 30043543 lands Tue 9/1 16:00 — 3 h after start (mid-run) (min 4 d)" in t["mid3"]["glue"]
    assert t["mid3"]["chip"] == {"text": "🚚 mid-run", "tone": "warn"}


def test_stamps_and_days(glue):
    """stampFor == helpers.timefmt.hour_to_stamp (real minutes, naive);
    leadText is timeline._lead (hours under a day, tenths of a day after),
    compact for the chip."""
    _, got = glue
    assert got["stamps"] == [hour_to_stamp(h, ANCHOR) for h in STAMP_HOURS]
    assert got["days"] == ["1.3", "1.7", "3.6", "0.0", "4.0"]
    assert got["leads"] == [["1.3 d", "1.3d"], ["1.7 d", "1.7d"], ["1 h", "1h"],
                            ["14 h", "14h"], ["3 h", "3h"], ["0 h", "0h"],
                            ["24 h", "24h"], ["1.0 d", "1.0d"], ["0 h", "0h"],
                            ["4.0 d", "4.0d"]]


# --------------------------------------------------------------------------
# popover helpers
# --------------------------------------------------------------------------

def test_popover_helpers(glue):
    _, got = glue
    # worst-first: SHORT, DEPENDENT, then the grey ones (backed / minor) in
    # recipe order, plain OK last
    assert got["sorted"] == ["b", "d", "c", "e", "a"]
    assert got["counted"] == ["30043543"]
    assert got["counted_before"] == 0          # ready 40 h is not before 30 h
    assert got["counted_alt"] == ["30043543"]  # alternates pool receipts
    assert got["casesFromKg"] == [True, False, False]
    assert got["rules"]["min_days_after_delivery"] == 4
    assert got["rules"]["receipt_ready_hour"] == 16   # defaults fill the 4-key subset
    assert got["rules_none"] == 4


# --------------------------------------------------------------------------
# before placement (slice 2b)
# --------------------------------------------------------------------------

def test_earliest_safe_start_for_holding_card(glue):
    """The P19 run held back as a card: cases = kg / kg_per_case, duration =
    kg / rate, judged alone against the rest of the board — not clear at
    0 h, clear (backed) from 136 h (plan §2), which is axis day 5. The
    board timelines are never mutated; on top of the FULL board (the run
    already placed) the copy never clears."""
    case_a, got = glue
    s = got["safe"]
    src = case_a["inputs"]["blocks"][1]
    assert abs(s["cases"] - src["cases"]) <= 1e-6
    assert abs(s["duration_h"] - (131.5 - 71.85)) <= TOL
    assert s["from_h"] == 0
    assert abs(s["safe_from_h"] - 136.0) <= TOL
    assert s["verdict_now"]["verdict"] in ("DEPENDENT", "SHORT")
    assert (s["verdict_at_safe"]["verdict"], s["verdict_at_safe"]["backed"]) == ("OK", True)
    assert got["safe_day"] == 5
    # asked from the safe hour itself: clear at from_h
    at = got["safe_from136"]
    assert abs(at["safe_from_h"] - 136.0) <= TOL and at["verdict_now"]["verdict"] == "OK"
    # asked from 100 h: still the receipt + 4 d candidate
    assert abs(got["safe_from_late"]["safe_from_h"] - 136.0) <= TOL
    assert got["safe_full"]["safe_from_h"] is None
    assert got["safe_full"]["verdict_now"]["verdict"] in ("DEPENDENT", "SHORT")
    assert got["safe_untouched"] is True


def test_earliest_safe_start_no_data_paths(glue):
    """No recipe, no kg, no rate and no hours, or a stale feed: null cases
    or an unjudgeable curve -> NO_DATA, never a green pill. An unknown rate
    with the card's own hours still judges (fallbackHours)."""
    _, got = glue
    for k in ("safe_norecipe", "safe_nokg", "safe_norate_nohours"):
        assert got[k]["cases"] is None, k
        assert got[k]["safe_from_h"] is None, k
        assert got[k]["verdict_now"]["verdict"] == "NO_DATA", k
    nr = got["safe_norate"]
    assert abs(nr["duration_h"] - (131.5 - 71.85)) <= TOL
    assert abs(nr["safe_from_h"] - 136.0) <= TOL
    st = got["safe_stale"]
    assert st["safe_from_h"] is None and st["verdict_now"]["verdict"] == "NO_DATA"


def test_safe_start_pill_wording(glue):
    p = glue[1]["pills"]
    assert (p["from"]["text"], p["from"]["tone"]) == ("🚚 from Sat 9/5 16:00", "warn")
    assert p["fromDue"]["text"] == "🚚 from Sat 9/5 16:00 · after due window"
    assert p["fromDueEdge"]["text"] == "🚚 from Sat 9/5 16:00"      # == due end is not after
    assert p["fromDueOk"]["text"] == "🚚 from Sat 9/5 16:00"
    # OK only because the truck counts inside the buffer: grey "🚚 backed",
    # like the block chip; on hand alone covering the run stays "✓ clear"
    assert (p["clear"]["text"], p["clear"]["tone"]) == ("🚚 backed", "muted")
    assert p["clearDue"]["text"] == "🚚 backed"                      # due window never taints a clear run
    assert (p["clearPlain"]["text"], p["clearPlain"]["tone"]) == ("✓ clear", "ok")
    assert p["clearPlainDue"]["text"] == "✓ clear"
    assert p["clear"]["title"].startswith("at Sat 9/5 16:00: 🚚 754751 backed by PO 30043543 (lands Tue 9/1 16:00, 4.0 d before start)")
    assert p["clearPlain"]["title"].startswith("at Mon 8/31 20:00: ✅ 754751: on hand covers the run")
    for k in ("norecipe", "nokg", "norateNohours", "stale"):
        assert (p[k]["text"], p[k]["tone"]) == ("? no data", "muted"), k
    assert (p["none"]["text"], p["none"]["tone"]) == ("⛔ no clear start", "crit")
    assert (p["norate"]["text"], p["norate"]["tone"]) == ("🚚 from Sat 9/5 16:00", "warn")
    # tooltips: the planner sentence at from_h, the safe-hour sentence, the
    # due note, and always the "judged alone" caveat
    for k, v in p.items():
        assert v["title"].splitlines()[-1] == "judged alone against the placed board", k
    assert p["from"]["title"].startswith("at Mon 8/31 00:00: 🚚 754751: on hand covers ")
    assert "from Sat 9/5 16:00: 🚚 754751 backed by PO 30043543" in p["from"]["title"]
    assert "due window ends Fri 9/4 04:00" in p["fromDue"]["title"]
    assert "due window" not in p["from"]["title"]
    assert "no start inside the receipts window reads OK" in p["none"]["title"]


def test_receipts_by_day(glue):
    """One bucket per calendar day (floor(ready_h / 24)) over ALL items,
    soonest first inside a day; a negative day is kept (the axis just never
    asks for it), a non-numeric ready_h is dropped."""
    _, got = glue
    assert got["byDay"] == [[1, ["30043543"]]]
    assert got["byDayMulti"] == [[-1, ["30000002"]], [1, ["30043543", "30000000", "30000001"]]]
    assert got["byDayEmpty"] == 0
    assert got["byDayShift"] == [3]                 # 40 h on 12-hour days
    assert got["dayIdx"] == [5, 1, -1, 1, 0]
