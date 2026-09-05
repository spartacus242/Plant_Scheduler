# tests/test_supply_detail.py — the trimmed popover Supply section and the
# "Supply details" modal (planner feedback 2026-09-02).
#
# The planner asked the popover to show only the items that will be
# depleted, keep the OK items behind the collapsed toggle, drop the untracked
# line and the "Also drawing ..." lists, and to open a detailed view of the
# at-risk items: which lines also pull them (with the running balance) and
# the open POs with dates and quantities.
#
# Pinned here:
#   * supplyGlue.supplyDetail — the pure rows behind the modal, on case A of
#     the golden stock fixture (data/test_fixtures/stock_risk/cases.json)
#     through a synthetic 3-block ScheduleBlock board: the DEPENDENT P19
#     block's detail lists the P10 running MO and the P10 tail as co-drawing
#     rows with pooled balances (recomputed here from the fixture's curve),
#     the 67,200 EA PO as a counted binding receipt with lead 31.85 h, OK
#     items in ok_items and the untracked passthrough; an all-OK block yields
#     items=[] and ok_items populated;
#   * BlockPopover / SupplyDetailPanel static markup via react-dom/server out
#     of the frontend's own node_modules (the test_frontend_consistency
#     harness): the popover keeps the flagged item, the button, "N more OK"
#     and loses "Also drawing" / "untracked (never gate)"; the panel carries
#     the two tables, the co-drawing rows, the PO row and the no-PO fallback.
# Skipped (not passed) when node or the frontend's node_modules are missing.
# Synthetic inputs plus the golden fixture; nothing live is read.

from __future__ import annotations

import html
import json
import os
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
    SRC / "utils" / "supplyGlue.ts",
    SRC / "components" / "BlockPopover.tsx",
    SRC / "components" / "SupplyDetailPanel.tsx",
]
FIXTURE = ROOT / "data" / "test_fixtures" / "stock_risk" / "cases.json"

TOL = 1e-6
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
const bi = require(path.join(out, "utils", "blockIdentity.js"));
const sg = require(path.join(out, "utils", "supplyGlue.js"));
const { BlockPopover } = require(path.join(out, "components", "BlockPopover.js"));
const { SupplyDetailPanel } = require(path.join(out, "components", "SupplyDetailPanel.js"));

const [Y, M0, D] = job.anchor_parts;
const anchor = new Date(Y, M0, D, 0, 0, 0);
const stamp = sg.stampFor(anchor);

// Case A as a StockArgs payload, plus one tracked item with a huge opening
// (always OK) and one untracked consumable, so one block's detail has all
// three buckets. 754751 keeps the fixture's verdicts (groups are independent).
const fix = JSON.parse(fs.readFileSync(job.fixture, "utf8"));
const inp = fix.cases.find((c) => c.name === "A_worked_table_L96").inputs;
const kpc = inp.sku_needs["280351"].kg_per_case;
const skuNeeds = { "280351": { kg_per_case: kpc, items: [
  ...inp.sku_needs["280351"].items,
  { item: "CAP1", per_case: 1, unit: "EA", alts: [] },
  { item: "CARTON1", per_case: 0.1, unit: "EA", alts: [] },
] } };
const stock = {
  feed_state: inp.feed_state,
  as_of: { stock_rm: "9/1 14:46", stock_pkg: "9/1 14:46", po: "9/1 06:10" },
  receipts_window_end_h: inp.receipts_window_end_h,
  rules: job.rules4,
  opening: { ...inp.opening, CAP1: 1e9 }, tracked: [...inp.tracked, "CAP1"], in_house: inp.in_house,
  units: { "754751": "EA", CAP1: "EA" },
  designations: { "754751": "SLEEVE 1X24X90", CAP1: "CAP 38MM" },
  snapshot_h: { ...inp.snapshot_h, CAP1: inp.snapshot_h["754751"] },
  receipts: inp.receipts, sku_needs: skuNeeds, cases_left: {},
};
const toBoard = (b) => ({
  id: b.block_id, line_id: 1, line_name: b.line_name, order_id: `MO-${b.block_id}`,
  sku: b.sku, sku_description: "PIZZA 1X24X90", start_hour: b.start_h, end_hour: b.end_h,
  run_hours: b.end_h - b.start_h, is_trial: false, block_type: "sku", qty_kg: b.cases * kpc,
  locked: b.locked, attrs: b.running ? "current_state:running" : "",
});
const schedule = inp.blocks.map(toBoard);
const caps = { P10: { "280351": 530 }, P19: { "280351": 800 }, P11: { "280351": 500 } };
const rules = sg.rulesOf(stock);
const r = {};

// --- supplyDetail goldens ----------------------------------------------------
const ev = sg.evaluateSchedule(schedule, stock, caps, null);
const p19 = schedule[1];
const sup19 = ev.verdicts.get(bi.blockKey(p19));
r.sup19 = { verdict: sup19.verdict, item: sup19.item, untracked: sup19.untracked };
r.detail_p19 = sg.supplyDetail(p19, sup19, ev.timelines, stock, schedule, rules, stamp);
r.sentence_nostamp = sg.supplyDetail(p19, sup19, ev.timelines, stock, schedule, null).items[0].sentence;

// A 1,000 kg run at 20 h that on hand alone covers: every item OK.
const okBlock = { id: "d1", line_id: 3, line_name: "P11", order_id: "D-W36", sku: "280351",
  start_hour: 20, end_hour: 22, run_hours: 2, is_trial: false, block_type: "sku", qty_kg: 1000 };
const sched4 = [...schedule, okBlock];
const ev4 = sg.evaluateSchedule(sched4, stock, caps, null);
const supOk = ev4.verdicts.get(bi.blockKey(okBlock));
r.ok_verdict = supOk.verdict;
r.detail_ok = sg.supplyDetail(okBlock, supOk, ev4.timelines, stock, sched4, rules, stamp);

// No PO at all: the same P19 run reads SHORT and has no receipt rows.
const stockNoPo = { ...stock, receipts: {} };
const evN = sg.evaluateSchedule(schedule, stockNoPo, caps, null);
const supN = evN.verdicts.get(bi.blockKey(p19));
r.nopo_verdict = supN.verdict;
r.detail_nopo = sg.supplyDetail(p19, supN, evN.timelines, stockNoPo, schedule, rules, stamp);

r.at_risk = {
  dep: sg.atRiskCount(sup19), ok: sg.atRiskCount(supOk), none: sg.atRiskCount(null),
  backed: sg.atRiskCount({ items: [{ ...sup19.items[0], verdict: "OK", backed: true }] }),
  minor: sg.atRiskCount({ items: [{ ...sup19.items[0], minor: true }] }),
  nodata: sg.atRiskCount({ items: [{ ...sup19.items[0], verdict: "NO_DATA" }] }),
  two: sg.atRiskCount({ items: [sup19.items[0], { ...sup19.items[0], verdict: "SHORT" }] }),
};
r.fmt = [sg.fmtQty(67200), sg.fmtQty(1234567.6), sg.fmtQty(-5000), sg.fmtQty(0), sg.fmtQty(999)];
r.sentence = sg.itemSentence(sup19.items[0], stamp);

// --- popover renders ---------------------------------------------------------
const okItem = (code) => ({ ...sup19.items[0], item: code, verdict: "OK", backed: false, minor: false,
  covered_frac: 1, depletion_h: null, binding: null, lead_h: null, safe_from_h: null, dependent_frac: 0 });
const synth = {
  ...sup19,
  items: [sup19.items[0], okItem("OK1"), okItem("OK2"), okItem("OK3")],
  untracked: ["CARTON1", "TAPE9"],
  co_consumers: { "754751": ["cs_87c620df4a|0.017", "cs_87c620df4a|125.733"] },
};
const stockSynth = { ...stock, designations: { ...stock.designations, OK1: "LID A", OK2: "LID B", OK3: "LID C" } };
const popHtml = (block, supply, extra) => renderToStaticMarkup(React.createElement(BlockPopover, {
  block, x: 0, y: 0, rate: 0, anchor, onClose: () => {}, supply, stock: stockSynth, supplyStamp: stamp, ...extra }));
const allOk = { ...synth, verdict: "OK", backed: false, covered_frac: 1, depletion_h: null,
  items: [okItem("754751"), okItem("OK1")], co_consumers: {} };
r.pop = {
  dep: popHtml(p19, synth, { onOpenSupplyDetail: () => {} }),
  one_other: popHtml(p19, { ...synth, co_consumers: { "754751": ["k1"] } }, { onOpenSupplyDetail: () => {} }),
  ok: popHtml(p19, allOk, { onOpenSupplyDetail: () => {} }),
  no_handler: popHtml(p19, synth, {}),
  no_stock: renderToStaticMarkup(React.createElement(BlockPopover, { block: p19, x: 0, y: 0, rate: 0, anchor,
    onClose: () => {}, supply: synth, stock: null, onOpenSupplyDetail: () => {} })),
};

// --- panel renders -----------------------------------------------------------
const panelHtml = (block, supply, tl, st, sched) => renderToStaticMarkup(React.createElement(SupplyDetailPanel, {
  block, supply, timelines: tl, stock: st, schedule: sched, anchorStamp: stamp, rules, anchor, onClose: () => {} }));
r.panel = {
  p19: panelHtml(p19, sup19, ev.timelines, stock, schedule),
  nopo: panelHtml(p19, supN, evN.timelines, stockNoPo, schedule),
  ok: panelHtml(okBlock, supOk, ev4.timelines, stock, sched4),
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
    for rel in ("utils/supplyGlue.js", "components/BlockPopover.js", "components/SupplyDetailPanel.js"):
        assert (out / rel).exists(), f"expected {out / rel}"
    return out


@pytest.fixture(scope="module")
def fe(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node not available")
    tmp = tmp_path_factory.mktemp("supply_detail")
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
# the fixture's curve, recomputed here (timeline.py §3.1-3.2)
# --------------------------------------------------------------------------

def _case_a() -> dict:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return next(c for c in fixture["cases"] if c["name"] == "A_worked_table_L96")


def _draws(inp: dict) -> list[tuple[str, float, float, float]]:
    """(key, a, e, qty) per block as the engine draws them: a running block
    draws the uniform tail of its need from the snapshot hour on."""
    snap = inp["snapshot_h"]["754751"]
    out = []
    for b in inp["blocks"]:
        need = b["cases"] * 1.0
        s, e = b["start_h"], b["end_h"]
        if s < snap:
            out.append((f"{b['block_id']}|{s}", snap, e, need * (e - snap) / (e - s)))
        else:
            out.append((f"{b['block_id']}|{s}", s, e, need))
    return out


def _consumed(a: float, e: float, q: float, t: float) -> float:
    if t <= a or e <= a:
        return 0.0
    if t >= e:
        return q
    return q * (t - a) / (e - a)


def _balance(inp: dict, draws, t: float) -> float:
    v = inp["opening"]["754751"]
    for _k, a, e, q in draws:
        v -= _consumed(a, e, q, t)
    for rcpt in inp["receipts"].get("754751", []):
        if rcpt["ready_h"] <= t:
            v += rcpt["qty"]
    return v


def _fmt(v: float) -> str:
    return f"{round(v):,}"


# --------------------------------------------------------------------------
# supplyDetail goldens
# --------------------------------------------------------------------------

def test_detail_dependent_block_lists_co_drawers_with_balances(fe):
    """P19's detail: one DEPENDENT section for 754751; its draws are the P10
    running MO (tag running), P19 itself (this block) and the P10 tail
    (planner — the fixture leaves it unlocked), start-sorted, each with the
    pooled planned balance before/after the draw."""
    inp = _case_a()["inputs"]
    exp = _case_a()["expected"]["cs_44c4bde023@71.85"]["items"][0]
    d = fe["detail_p19"]
    assert fe["sup19"] == {"verdict": "DEPENDENT", "item": "754751", "untracked": ["CARTON1"]}
    assert [it["item"] for it in d["items"]] == ["754751"]
    it = d["items"][0]
    assert (it["designation"], it["unit"], it["verdict"], it["backed"], it["rank"]) == \
        ("SLEEVE 1X24X90", "EA", "DEPENDENT", False, 3)
    assert it["members"] == ["754751"]
    for k in ("need", "opening_at_start", "covered_frac", "depletion_h", "dependent_frac"):
        assert abs(it[k] - exp[k]) <= TOL, k
    # draws
    draws = _draws(inp)
    by_key = {k: (a, e, q) for k, a, e, q in draws}
    assert [(x["key"], x["tag"], x["line_name"], x["is_alt"]) for x in it["draws"]] == [
        ("cs_87c620df4a|0.017", "running", "P10", False),
        ("cs_44c4bde023|71.85", "this block", "P19", False),
        ("cs_87c620df4a|125.733", "planner", "P10", False),
    ]
    for x in it["draws"]:
        a, e, q = by_key[x["key"]]
        assert (x["sku"], x["order_id"]) == ("280351", f"MO-{x['key'].split('|')[0]}")
        assert abs(x["a"] - a) <= TOL and abs(x["e"] - e) <= TOL and abs(x["qty"] - q) <= TOL, x["key"]
        assert abs(x["balance_before"] - _balance(inp, draws, a)) <= 1e-5, x["key"]
        assert abs(x["balance_after"] - _balance(inp, draws, e)) <= 1e-5, x["key"]
    assert abs(it["draws"][0]["balance_before"] - 36000.0) <= TOL
    # the P19 draw's own need is the block's need
    assert abs(it["draws"][1]["qty"] - exp["need"]) <= TOL


def test_detail_dependent_block_lists_the_binding_po(fe):
    """One receipt row: the 67,200 EA PO, ERP tier, counted (lands before
    the block ends), binding, lead 31.85 h before start (reads 1.3 d)."""
    it = fe["detail_p19"]["items"][0]
    assert len(it["receipts"]) == 1
    rc = it["receipts"][0]
    assert (rc["po8"], rc["qty"], rc["unit"], rc["receipt_date"], rc["ready_h"], rc["tier"]) == \
        ("30043543", 67200.0, "EA", "2026-09-02", 40.0, "erp")
    assert abs(rc["lead_h"] - 31.85) <= TOL
    assert rc["counted"] is True and rc["binding"] is True
    assert (rc["item"], rc["is_alt"]) == ("754751", False)


def test_detail_buckets_ok_and_untracked(fe):
    d = fe["detail_p19"]
    assert d["ok_items"] == [{"item": "CAP1", "designation": "CAP 38MM"}]
    assert d["untracked"] == ["CARTON1"]
    assert fe["sentence"] == (
        f"on hand at start 22,045 EA → covers 39%, runs out {hour_to_stamp(95.321493, ANCHOR)}"
        " · dependent share 78%")
    assert d["items"][0]["sentence"] == fe["sentence"]
    assert fe["sentence_nostamp"] == "on hand at start 22,045 EA → covers 39%, runs out h95.3 · dependent share 78%"


def test_detail_all_ok_block(fe):
    """A run on hand covers: no sections, both tracked items in ok_items
    (recipe order), untracked still passed through."""
    assert fe["ok_verdict"] == "OK"
    d = fe["detail_ok"]
    assert d["items"] == []
    assert d["ok_items"] == [{"item": "754751", "designation": "SLEEVE 1X24X90"},
                             {"item": "CAP1", "designation": "CAP 38MM"}]
    assert d["untracked"] == ["CARTON1"]


def test_detail_short_without_po(fe):
    assert fe["nopo_verdict"] == "SHORT"
    it = fe["detail_nopo"]["items"][0]
    assert it["verdict"] == "SHORT" and it["receipts"] == []
    assert [x["tag"] for x in it["draws"]] == ["running", "this block", "planner"]


def test_at_risk_count_and_qty_format(fe):
    """The button counts SHORT / DEPENDENT / NO_DATA; backed and minor rows
    are shown but not 'at risk'. fmtQty groups thousands whatever the host
    locale says."""
    assert fe["at_risk"] == {"dep": 1, "ok": 0, "none": 0, "backed": 0, "minor": 0, "nodata": 1, "two": 2}
    assert fe["fmt"] == ["67,200", "1,234,568", "-5,000", "0", "999"]


# --------------------------------------------------------------------------
# BlockPopover markup
# --------------------------------------------------------------------------

def test_popover_shows_only_flagged_items_and_the_details_button(fe):
    """One DEPENDENT + three OK + untracked: the DEPENDENT row, the button
    with the at-risk count and the collapsed '3 more OK' render; the OK
    codes, the untracked list and the 'Also drawing' lists do not."""
    h = html.unescape(fe["pop"]["dep"])
    assert 'data-testid="supply-section"' in h
    assert "754751" in h and "SLEEVE 1X24X90" in h
    assert "Supply details (1 item at risk)" in h
    assert "3 more OK" in h
    assert "2 other blocks draw this" in h
    for gone in ("Also drawing", "untracked (never gate)", "CARTON1", "TAPE9", "OK1", "LID A"):
        assert gone not in h, gone
    assert "1 other block draws this" in html.unescape(fe["pop"]["one_other"])


def test_popover_all_ok_still_offers_details(fe):
    h = html.unescape(fe["pop"]["ok"])
    assert "Supply details…" in h and "at risk" not in h
    assert "2 more OK" in h
    assert "other block" not in h
    # no handler -> no button; no stock -> no section at all
    assert "Supply details" not in fe["pop"]["no_handler"]
    ns = fe["pop"]["no_stock"]
    assert 'data-testid="supply-section"' not in ns and "Supply details" not in ns


# --------------------------------------------------------------------------
# SupplyDetailPanel markup
# --------------------------------------------------------------------------

def test_panel_tables_for_the_dependent_block(fe):
    inp = _case_a()["inputs"]
    draws = _draws(inp)
    h = html.unescape(fe["panel"]["p19"])
    assert 'role="dialog"' in h and 'aria-modal="true"' in h
    assert "280351" in h and "PIZZA 1X24X90" in h and "P19" in h
    assert f"{hour_to_stamp(71.85, ANCHOR)} → {hour_to_stamp(131.5, ANCHOR)}" in h
    # table A
    assert f"Blocks drawing 754751 from the stock snapshot ({hour_to_stamp(14.8, ANCHOR)}) through this block's end" in h
    assert "Balance before → after" in h
    i_run, i_this, i_plan = h.index(">running<"), h.index(">this block<"), h.index(">planner<")
    assert i_run < i_this < i_plan
    assert h.count("MO-cs_87c620df4a 280351") == 2 and "MO-cs_44c4bde023 280351" in h
    assert f"{hour_to_stamp(14.8, ANCHOR)} → {hour_to_stamp(119.733, ANCHOR)}" in h
    assert f"{_fmt(_balance(inp, draws, 14.8))}</span> → <span" in h
    assert f"{_fmt(_balance(inp, draws, 131.5))}" in h
    # table B
    assert "Open POs for 754751" in h
    assert "PO 30043543" in h and "+67,200 EA" in h and "2026-09-02" in h
    assert hour_to_stamp(40.0, ANCHOR) in h and "ERP date" in h
    assert "1.3 d < 4 d before start" in h
    assert ">counted<" in h and "◀ binding" in h
    # footer + buckets
    assert "Untracked consumables are never graded: CARTON1" in h
    assert "1 item covered by on hand" in h and "CAP 38MM" in h
    assert "Stock as of 9/1 14:46" in h and "POs as of 9/1 06:10" in h
    assert "Need is based on this block's kg (qty ÷ kg/case)" in h
    assert "All items covered" not in h and "No open PO" not in h


def test_panel_no_po_fallback_and_negative_balance(fe):
    inp = _case_a()["inputs"]
    inp_nopo = {**inp, "receipts": {}}
    draws = _draws(inp)
    h = html.unescape(fe["panel"]["nopo"])
    assert f"No open PO for this item in the feed (window to {hour_to_stamp(800.0, ANCHOR)})." in h
    assert ">SHORT<" in h
    neg = _balance(inp_nopo, draws, 131.5)
    assert neg < 0
    assert f'color:#b71c1c;font-weight:700">{_fmt(neg)}</span>' in h
    assert "PO 30043543" not in h


def test_panel_all_ok_block(fe):
    h = html.unescape(fe["panel"]["ok"])
    assert "All items covered by on-hand stock for this block." in h
    assert "✅ 754751" in h and "✅ CAP1" in h
    assert "Blocks drawing" not in h and "Open POs" not in h
    assert "Untracked consumables are never graded: CARTON1" in h


# --------------------------------------------------------------------------
# source contracts
# --------------------------------------------------------------------------

def test_source_contracts():
    """The popover code path no longer prints the co-consumer or untracked
    lines; the sandbox mounts the panel behind the stock gate; the panel
    closes on Escape in the capture phase (the sandbox's window handler
    must not also close the popover)."""
    pop = (SRC / "components" / "BlockPopover.tsx").read_text(encoding="utf-8")
    sandbox = (SRC / "GanttSandbox.tsx").read_text(encoding="utf-8")
    panel = (SRC / "components" / "SupplyDetailPanel.tsx").read_text(encoding="utf-8")
    for gone in ("Also drawing", "untracked (never gate)", "coLabel"):
        assert gone not in pop, gone
    assert "onOpenSupplyDetail" in pop
    assert "describeKey" not in sandbox
    assert "<SupplyDetailPanel" in sandbox
    assert "onOpenSupplyDetail={stockEnabled ? (b) => setSupplyDetailFor(b) : undefined}" in sandbox
    assert "stockEnabled && stock && supply && supplyDetailFor" in sandbox
    assert 'window.addEventListener("keydown", h, true)' in panel
    assert "e.stopPropagation();" in panel
    assert "Also drawing" not in panel
