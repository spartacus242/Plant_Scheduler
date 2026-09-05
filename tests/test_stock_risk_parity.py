# tests/test_stock_risk_parity.py — TS port of the supply timeline engine.
#
# utils/stockRisk.ts is a 1:1 port of stockcheck/timeline.py (contract
# 2026-09-01 §3/§5): the client grades a dragged block exactly the way the
# report grades the pushed board, so the chip a planner sees agrees with the
# Reconcile finding. This harness compiles the TS file alone with the
# frontend's own tsc (precedent test_sku_picker_math / test_block_identity),
# replays data/test_fixtures/stock_risk/cases.json under node and compares
# every Supply field with the golden Python output: floats within 1e-6,
# strings/bools/None exact, the planner sentence separately so a drift
# shows both strings side by side. The anchor-stamp path is pinned against
# helpers.timefmt.hour_to_stamp directly.
#
# EXTRA_CASES below are regression inputs the golden fixture does not
# carry (a board row sent twice, item keys that only agree after trimming,
# lenient number strings, a preview removing a block); Python's answer is
# computed here at test time and the TS answer must match it field for
# field. Skipped (not passed) when node or the frontend's node_modules are
# missing. Everything is synthetic; nothing live is read.

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
from stockcheck import timeline as tl  # noqa: E402

FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
SRC = FRONTEND / "src"
RISK_TS = SRC / "utils" / "stockRisk.ts"
FIXTURE = ROOT / "data" / "test_fixtures" / "stock_risk" / "cases.json"

TOL = 1e-6
ANCHOR = "2026-08-31 00:00:00"
# Stamp probes: the fixture's own hours, exact minute boundaries the way
# datetime_to_hour produces them (seconds / 3600), hours before the anchor,
# and hours past the US DST switch (2026-11-01) — a local-time port would
# drift an hour there, Python's naive datetime does not.
STAMP_HOURS = (
    [0.0, 40.0, 95.321493, 125.733, 136.0, 168.0, 144.3, 0.017, 123.7,
     -0.25, -3.5, -48.983, 1487.5, 1500.5, 1512.25, 2000.75, 719.999999,
     0.0166666, 0.0166667, 10.116666666666667]
    + [k * 7 * 60 / 3600 for k in range(300)]
    + [k * 11 * 60 / 3600 + 0.5 / 3600 for k in range(0, 200, 3)]
)

# --------------------------------------------------------------------------
# regression cases (Python computes the expectation at test time)
# --------------------------------------------------------------------------

# The 4-key subset the page ships in StockArgs.rules — the port must take
# the other defaults itself.
RULES4 = {"min_days_after_delivery": 4, "lead_measured_from": "block_start",
          "dependent_frac_floor": 0.05, "hard_block": False}


def _blk(key, block_id, sku, start, end, cases, **kw) -> dict:
    b = {"key": key, "block_id": block_id, "sku": sku, "line_name": "P10",
         "start_h": start, "end_h": end, "cases": cases, "locked": False,
         "running": False, "cases_left": None}
    b.update(kw)
    return b


def _rcpt(ready_h, qty, po8) -> dict:
    return {"ready_h": ready_h, "qty": qty, "po8": po8, "tier": "erp",
            "receipt_date": "2026-09-02", "label": f"PO {po8}"}


def _needs(item, alts=()) -> dict:
    return {"kg_per_case": 1.0,
            "items": [{"item": item, "per_case": 1.0, "unit": "EA",
                       "alts": list(alts)}]}


def _inputs(blocks, sku_needs, opening, tracked, **kw) -> dict:
    inp = {"blocks": blocks, "sku_needs": sku_needs, "opening": opening,
           "tracked": tracked, "in_house": [], "receipts": {},
           "snapshot_h": {}, "feed_state": "ok",
           "receipts_window_end_h": 800.0}
    inp.update(kw)
    return inp


_DUP = _blk("b1|10", "b1", "S", 10.0, 20.0, 100.0)
# No key: both sides must fall back to block_id|start and still dedupe.
_DUP_NOKEY = {"block_id": "b2", "sku": "S", "line_name": "P10",
              "start_h": 30.0, "end_h": 40.0, "cases": 100.0}

EXTRA_CASES = [
    # add_block keeps the first registration: the same row twice draws
    # once (opening 250 covers 100 + 100; a double draw would flip b2).
    {"name": "dup_key_same_row_twice", "rules": RULES4,
     "inputs": _inputs(
         [_DUP, dict(_DUP), _DUP_NOKEY, dict(_DUP_NOKEY)],
         {"S": _needs("754751")}, {"754751": 250.0}, ["754751"]),
     "dup_block": _DUP},
    # _item: keys agree only after trimming — openings pool (60 + 60),
    # receipts concatenate, '' alts vanish, an int code is the same code,
    # a trimmed in-house code still silences its group.
    {"name": "item_keys_trim_and_pool", "rules": RULES4,
     "inputs": _inputs(
         [_blk("s|10", "s", "S", 10.0, 20.0, 200.0),
          _blk("t|10", "t", "T", 10.0, 20.0, 50.0),
          _blk("u|30", "u", "U", 30.0, 40.0, 10.0)],
         {"S": _needs("754751 ", alts=["", " 754751", "754751 "]),
          "T": _needs("BT001"),
          "U": _needs(754751)},
         {"754751": 60.0, " 754751": 60.0}, [" 754751"],
         in_house=["BT001 ", ""],
         receipts={"754751 ": [_rcpt(12.0, 100.0, "30000002")],
                   " 754751": [_rcpt(11.0, 50.0, "30000001")]},
         snapshot_h={"754751 ": 0.0})},
    # _num: commas, Python digit underscores, exponents, a bool and garbage
    # (-> 0) — the verdicts are designed so a NaN-as-0 port flips a, b, c.
    {"name": "lenient_numbers", "rules": RULES4,
     "inputs": _inputs(
         [_blk("a|10", "a", "S1", 10.0, 20.0, 1000.0),
          _blk("b|10", "b", "S2", 10.0, 20.0, 1000.0),
          _blk("c|10", "c", "S3", 10.0, 20.0, 500.0),
          _blk("d|10", "d", "S4", 10.0, 20.0, 10.0),
          _blk("e|10", "e", "S5", 10.0, 20.0, 10.0)],
         {"S1": _needs("754751"), "S2": _needs("754752"),
          "S3": _needs("754753"), "S4": _needs("754754"),
          "S5": _needs("754755")},
         {"754751": "1,200", "754752": " 1_000 ", "754753": "5e2",
          "754754": True, "754755": "abc"},
         ["754751", "754752", "754753", "754754", "754755"],
         snapshot_h={"754751": "0", "754752": None, "754753": " 2.5 "})},
    # removeBlock (preview recipe) must equal a rebuild without the block:
    # b3 is SHORT with b2 on the board and OK once b2 is gone.
    {"name": "remove_block_flips_neighbour", "rules": RULES4,
     "inputs": _inputs(
         [_blk("b1|10", "b1", "S", 10.0, 20.0, 100.0),
          _blk("b2|30", "b2", "S", 30.0, 40.0, 100.0),
          _blk("b3|50", "b3", "S", 50.0, 60.0, 100.0)],
         {"S": _needs("754751")}, {"754751": 250.0}, ["754751"]),
     "remove_key": "b2|30"},
    # _lead: under a day the sentence quotes whole hours ("1 h before
    # start", "3 h after start (mid-run)"), from 24 h on tenths of a day —
    # a port still printing "0.0 d" drifts on every block here.
    {"name": "sub_day_leads", "rules": RULES4,
     "inputs": _inputs(
         [_blk("h1|30", "h1", "S1", 30.0, 40.0, 100.0),     # lead 1 h
          _blk("h14|40", "h14", "S2", 40.0, 50.0, 100.0),   # 13.7 h -> 14 h
          _blk("mid|10", "mid", "S3", 10.0, 20.0, 100.0),   # lands 3 h after start
          _blk("d1|60", "d1", "S4", 60.0, 70.0, 100.0),     # 24 h -> 1.0 d
          _blk("h0|30", "h0", "S5", 30.0, 40.0, 100.0)],    # 0.4 h -> 0 h
         {"S1": _needs("A1"), "S2": _needs("A2"), "S3": _needs("A3"),
          "S4": _needs("A4"), "S5": _needs("A5")},
         {"A1": 50.0, "A2": 50.0, "A3": 50.0, "A4": 50.0, "A5": 50.0},
         ["A1", "A2", "A3", "A4", "A5"],
         receipts={"A1": [_rcpt(29.0, 100.0, "30000011")],
                   "A2": [_rcpt(26.3, 100.0, "30000012")],
                   "A3": [_rcpt(13.0, 100.0, "30000013")],
                   "A4": [_rcpt(36.0, 100.0, "30000014")],
                   "A5": [_rcpt(29.6, 100.0, "30000015")]})},
]

RUNNER_JS = """
const fs = require("fs");
const path = require("path");
const out = process.argv[2];
const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const sr = require(path.join(out, "utils", "stockRisk.js"));
const fix = JSON.parse(fs.readFileSync(job.fixture, "utf8"));
const extra = JSON.parse(fs.readFileSync(job.extra, "utf8"));

const drawsOf = (tl) => {
  const d = {};
  for (const i of Object.keys(tl.items)) d[i] = tl.items[i].draws;
  return d;
};
const nDraws = (tl) => Object.values(tl.items).reduce((n, it) => n + it.draws.length, 0);

function runCase(c) {
  const inp = c.inputs;
  const evalBoard = (blocks, t) => sr.evaluateBoard(blocks, t, c.rules, inp.feed_state,
    inp.receipts_window_end_h);
  const tl = sr.buildTimelines(inp.blocks, inp.sku_needs, inp.opening, inp.receipts,
    inp.snapshot_h, { tracked: inp.tracked, in_house: inp.in_house });
  const board = evalBoard(inp.blocks, tl);
  const textAnchor = {};
  const ranks = {};
  for (const k of Object.keys(board)) {
    textAnchor[k] = sr.verdictText(board[k], job.anchor);
    ranks[k] = sr.supplyRank(board[k]);
  }
  let ecs = null;
  let ecsText = null;
  const a = c.earliest_clear_start && c.earliest_clear_start.args;
  if (a) {
    ecs = sr.earliestClearStart(a.sku, a.cases, a.duration_h, tl, c.rules,
      inp.feed_state, inp.receipts_window_end_h, a.from_h, a.line_name || "");
    ecsText = {
      now: ecs.verdict_now && sr.verdictText(ecs.verdict_now, job.anchor),
      at_safe: ecs.verdict_at_safe && sr.verdictText(ecs.verdict_at_safe, job.anchor),
    };
  }
  const r = { name: c.name, board, draws: drawsOf(tl), ecs, text_anchor: textAnchor,
    ecs_text_anchor: ecsText, ranks, groups: tl.groups, tracked: tl.tracked,
    in_house: tl.in_house, n_blocks_registered: Object.keys(tl.blocks).length };
  // Registering a key again (on a copy): the stored entry back, no new draw.
  if (c.dup_block) {
    const cp = sr.copyTimelines(tl);
    const before = nDraws(cp);
    const ret = sr.addBlock(cp, c.dup_block);
    r.dup_probe = { same_entry: ret === cp.blocks[sr.blockKey(c.dup_block)],
      draws_added: nDraws(cp) - before, n_blocks: Object.keys(cp.blocks).length };
  }
  // removeBlock on a copy == a rebuild without that block.
  if (c.remove_key) {
    const cp = sr.copyTimelines(tl);
    const rest = inp.blocks.filter((b) => sr.blockKey(b) !== c.remove_key);
    r.remove_probe = { removed: sr.removeBlock(cp, c.remove_key),
      missing: sr.removeBlock(cp, "no-such-key"),
      board: evalBoard(rest, cp), draws: drawsOf(cp),
      n_blocks: Object.keys(cp.blocks).length };
  }
  // Probes and earliestClearStart work on copies: the board reads the same after.
  r.stable = JSON.stringify(evalBoard(inp.blocks, tl)) === JSON.stringify(board);
  return r;
}

const res = { default_rules: sr.DEFAULT_RULES, eps: sr.EPS,
  cases: fix.cases.map(runCase), extra: extra.cases.map(runCase) };
res.stamps = job.stamp_hours.map((h) => sr.hourStamp(h, job.anchor));
// A Date anchor (the frontend's shape) must stamp like the string anchor.
res.stamps_date = job.stamp_hours.map(
  (h) => sr.hourStamp(h, new Date(job.anchor.replace(" ", "T"))));
res.keys = [
  sr.blockKey({ block_id: "b", start_h: 0.125 }),
  sr.blockKey({ block_id: "b", start_h: 0.375 }),
  sr.blockKey({ block_id: "b", start_h: 71.85 }),
  sr.blockKey({ block_id: "b", start_h: 2.675 }),
  sr.blockKey({ key: "given|1", block_id: "b", start_h: 1 }),
];
process.stdout.write(JSON.stringify(res));
"""


def _compile_risk_ts(tmp: Path) -> Path:
    tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        pytest.skip("frontend node_modules not installed (npm install in frontend dir)")
    out = tmp / "compiled"
    proc = subprocess.run(
        [
            "node", str(tsc), str(RISK_TS),
            "--outDir", str(out),
            # stockRisk.ts imports nothing, so pin the src/ root explicitly
            # to keep the utils/ layout of the other harnesses.
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
    js = out / "utils" / "stockRisk.js"
    assert js.exists(), f"expected {js}"
    return out


@pytest.fixture(scope="module")
def parity(tmp_path_factory) -> tuple[dict, dict]:
    """(fixture json, node result) — compiled and replayed once per module."""
    if shutil.which("node") is None:
        pytest.skip("node not available")
    tmp = tmp_path_factory.mktemp("stock_risk")
    out = _compile_risk_ts(tmp)
    extra_path = tmp / "extra.json"
    extra_path.write_text(json.dumps({"cases": EXTRA_CASES}), encoding="utf-8")
    job_path = tmp / "job.json"
    job_path.write_text(json.dumps({
        "fixture": str(FIXTURE), "extra": str(extra_path), "anchor": ANCHOR,
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
    got = json.loads(proc.stdout)
    assert [c["name"] for c in got["cases"]] == [c["name"] for c in fixture["cases"]]
    assert [c["name"] for c in got["extra"]] == [c["name"] for c in EXTRA_CASES]
    return fixture, got


# --------------------------------------------------------------------------
# structural comparison
# --------------------------------------------------------------------------

def _diff(exp, got, path: str, diffs: list, texts: list) -> None:
    """Recursive compare; numeric leaves within TOL, everything else exact.
    "text" leaves go to `texts` so the sentence drift reads side by side.
    Key sets must match exactly: a TS `undefined` vanishes from JSON, which
    is how a None-vs-missing bug would show."""
    if isinstance(exp, dict):
        if not isinstance(got, dict):
            diffs.append(f"{path}: expected dict, got {type(got).__name__}")
            return
        ek, gk = set(exp), set(got)
        if ek != gk:
            diffs.append(f"{path}: keys differ — missing {sorted(ek - gk)}, "
                         f"extra {sorted(gk - ek)}")
        for k in exp:
            if k not in got:
                continue
            if k == "text":
                if exp[k] != got[k]:
                    texts.append(f"{path}.text\n"
                                 f"    expected: {exp[k]!r}\n"
                                 f"    got:      {got[k]!r}")
            else:
                _diff(exp[k], got[k], f"{path}.{k}", diffs, texts)
        return
    if isinstance(exp, list):
        if not isinstance(got, list):
            diffs.append(f"{path}: expected list, got {type(got).__name__}")
            return
        if len(exp) != len(got):
            diffs.append(f"{path}: length {len(exp)} vs {len(got)}")
        for i, (a, b) in enumerate(zip(exp, got)):
            _diff(a, b, f"{path}[{i}]", diffs, texts)
        return
    # bool is an int subclass in Python: keep it exact and typed.
    if isinstance(exp, bool) or isinstance(got, bool):
        if not (isinstance(exp, bool) and isinstance(got, bool) and exp == got):
            diffs.append(f"{path}: expected {exp!r}, got {got!r}")
        return
    if exp is None or got is None:
        if exp is not got:
            diffs.append(f"{path}: expected {exp!r}, got {got!r}")
        return
    if isinstance(exp, (int, float)) and isinstance(got, (int, float)):
        if not abs(exp - got) <= TOL:
            diffs.append(f"{path}: expected {exp!r}, got {got!r} "
                         f"(delta {got - exp:.3g})")
        return
    if exp != got:
        diffs.append(f"{path}: expected {exp!r}, got {got!r}")


def _board_diffs(parity) -> tuple[list, list]:
    fixture, got = parity
    diffs: list = []
    texts: list = []
    for c, g in zip(fixture["cases"], got["cases"]):
        _diff(c["expected"], g["board"], c["name"], diffs, texts)
    return diffs, texts


def _assert_same(exp, got, label: str) -> None:
    diffs: list = []
    texts: list = []
    _diff(exp, got, label, diffs, texts)
    assert not diffs and not texts, (
        "TS/Python drift:\n  " + "\n  ".join(diffs + texts))


def _py_case(c: dict) -> tuple[dict, dict]:
    """Python's (timelines, board) for a regression case's inputs."""
    inp = c["inputs"]
    t = tl.build_timelines(inp["blocks"], inp["sku_needs"], inp["opening"],
                           inp["receipts"], inp["snapshot_h"],
                           tracked=inp["tracked"], in_house=inp["in_house"])
    board = tl.evaluate_board(inp["blocks"], t, c["rules"], inp["feed_state"],
                              inp["receipts_window_end_h"])
    return t, board


def _extra(parity, name: str) -> tuple[dict, dict]:
    """(case, node result) for one regression case."""
    _, got = parity
    c = next(c for c in EXTRA_CASES if c["name"] == name)
    g = next(g for g in got["extra"] if g["name"] == name)
    return c, g


def _draws(t: dict) -> dict:
    return {i: v["draws"] for i, v in t["items"].items()}


# --------------------------------------------------------------------------
# tests — golden fixture
# --------------------------------------------------------------------------

def test_default_rules_and_eps(parity):
    fixture, got = parity
    assert got["default_rules"] == fixture["rules"] == tl.DEFAULT_RULES
    assert got["eps"] == tl.EPS


def test_board_parity(parity):
    """Every Supply field of every block, every case (text aside)."""
    diffs, _ = _board_diffs(parity)
    assert not diffs, "TS/Python supply drift:\n  " + "\n  ".join(diffs)


def test_text_parity(parity):
    """verdict_text byte-for-byte: emoji, ' · ' separators, h-stamps, days."""
    _, texts = _board_diffs(parity)
    assert not texts, "TS/Python sentence drift:\n  " + "\n  ".join(texts)


def test_draws_parity(parity):
    """build_timelines: the draw model (running clip, cases_left, before-S)."""
    fixture, got = parity
    diffs: list = []
    for c, g in zip(fixture["cases"], got["cases"]):
        _diff(c["expected_draws"], g["draws"], f"{c['name']}.draws", diffs, [])
        assert g["n_blocks_registered"] == len(c["inputs"]["blocks"]), c["name"]
    assert not diffs, "TS/Python draw drift:\n  " + "\n  ".join(diffs)


def test_earliest_clear_start_parity(parity):
    fixture, got = parity
    diffs: list = []
    texts: list = []
    n = 0
    for c, g in zip(fixture["cases"], got["cases"]):
        ecs = c.get("earliest_clear_start")
        if not ecs:
            assert g["ecs"] is None
            continue
        n += 1
        _diff(ecs["expected"], g["ecs"], f"{c['name']}.ecs", diffs, texts)
        # Copies only: the board evaluates identically after the probe.
        assert g["stable"], f"{c['name']}: earliestClearStart mutated the timelines"
    assert n >= 2, "fixture lost its earliest_clear_start cases"
    assert not texts, "TS/Python sentence drift:\n  " + "\n  ".join(texts)
    assert not diffs, "TS/Python earliest_clear_start drift:\n  " + "\n  ".join(diffs)


def test_supply_rank_parity(parity):
    fixture, got = parity
    for c, g in zip(fixture["cases"], got["cases"]):
        for key, sup in c["expected"].items():
            assert g["ranks"][key] == tl.supply_rank(sup), f"{c['name']}/{key}"


def test_verdict_text_with_anchor(parity):
    """Same Supply, same anchor: the TS sentence equals Python's (real
    stamps, minutes included). Fed the TS supply so both sides format the
    identical floats."""
    fixture, got = parity
    bad = []
    for c, g in zip(fixture["cases"], got["cases"]):
        for key, sup in g["board"].items():
            exp = tl.verdict_text(sup, ANCHOR)
            if exp != g["text_anchor"][key]:
                bad.append(f"{c['name']}/{key}\n    expected: {exp!r}\n"
                           f"    got:      {g['text_anchor'][key]!r}")
        if g["ecs"]:
            for which, sup in (("now", g["ecs"]["verdict_now"]),
                               ("at_safe", g["ecs"]["verdict_at_safe"])):
                exp = tl.verdict_text(sup, ANCHOR) if sup else None
                if exp != g["ecs_text_anchor"][which]:
                    bad.append(f"{c['name']}/ecs.{which}\n    expected: {exp!r}\n"
                               f"    got:      {g['ecs_text_anchor'][which]!r}")
    assert not bad, "anchored sentence drift:\n  " + "\n  ".join(bad)
    # At least one anchored sentence must carry a real stamp.
    sample = got["cases"][0]["text_anchor"]
    assert any("Thu 9/3" in t for t in sample.values()), sample


def test_hour_stamp_parity(parity):
    """hourStamp == helpers.timefmt.hour_to_stamp (naive, real minutes)."""
    _, got = parity
    exp = [hour_to_stamp(h, ANCHOR) for h in STAMP_HOURS]
    bad = [f"h={h!r}: expected {e!r}, got {g!r}"
           for h, e, g in zip(STAMP_HOURS, exp, got["stamps"]) if e != g]
    assert not bad, "stamp drift:\n  " + "\n  ".join(bad)
    assert got["stamps_date"] == got["stamps"]


def test_block_key_python_rounding(parity):
    """Key = `block_id|start_h` at FULL precision in JS `String(number)`
    form — the Gantt's blockIdentity.blockKey (fix K-8 / FE, audit
    stock-14: the old `@start.toFixed(2)` collapsed two rows of one MO less
    than 0.005 h apart into one key and silently dropped the second draw).
    Python prints the same spelling through timeline._js_num, so ties and
    exponents cannot drift: 0.125 stays "0.125", 30.0 prints "30" (JS
    never writes "30.0")."""
    _, got = parity
    assert got["keys"] == [
        tl.block_key({"block_id": "b", "start_h": 0.125}),
        tl.block_key({"block_id": "b", "start_h": 0.375}),
        tl.block_key({"block_id": "b", "start_h": 71.85}),
        tl.block_key({"block_id": "b", "start_h": 2.675}),
        "given|1",
    ]
    assert got["keys"][:4] == ["b|0.125", "b|0.375", "b|71.85", "b|2.675"]
    assert tl.block_key({"block_id": "b", "start_h": 30.0}) == "b|30"


# --------------------------------------------------------------------------
# tests — regression cases
# --------------------------------------------------------------------------

def test_extra_cases_parity(parity):
    """Every regression case: board, draws, groups, tracked/in_house sets
    equal Python's, and the probes left the shared timelines untouched."""
    for c in EXTRA_CASES:
        _, g = _extra(parity, c["name"])
        t, board = _py_case(c)
        _assert_same(board, g["board"], c["name"])
        _assert_same(_draws(t), g["draws"], f"{c['name']}.draws")
        _assert_same(t["groups"], g["groups"], f"{c['name']}.groups")
        assert g["tracked"] == t["tracked"], c["name"]
        assert g["in_house"] == t["in_house"], c["name"]
        assert g["n_blocks_registered"] == len(t["blocks"]), c["name"]
        assert g["stable"], f"{c['name']}: a probe mutated the timelines"
        for key, sup in g["board"].items():
            assert g["text_anchor"][key] == tl.verdict_text(sup, ANCHOR), key
            assert g["ranks"][key] == tl.supply_rank(sup), key


def test_duplicate_block_key_draws_once(parity):
    """A repeated key is the same board row twice: one draw, first entry
    kept (add_block) — a second draw would fake a shortage on b2."""
    c, g = _extra(parity, "dup_key_same_row_twice")
    t, board = _py_case(c)
    # The scenario itself: Python draws once per key and both blocks pass.
    # The key-less row falls back to `block_id|start` ("b2|30", fix K-8 /
    # FE — was "b2@30.00").
    assert sorted(board) == ["b1|10", "b2|30"]
    assert {s["verdict"] for s in board.values()} == {"OK"}
    assert len(t["items"]["754751"]["draws"]) == 2
    assert g["n_blocks_registered"] == 2
    assert [d["key"] for d in g["draws"]["754751"]] == ["b1|10", "b2|30"]
    assert g["dup_probe"] == {"same_entry": True, "draws_added": 0, "n_blocks": 2}


def test_item_keys_trim_and_pool(parity):
    """_item parity: the group is keyed by the trimmed code, both openings
    pool, both receipt lists merge, the in-house code trims too."""
    c, g = _extra(parity, "item_keys_trim_and_pool")
    t, board = _py_case(c)
    # Python's view of the scenario, so the parity assertion is meaningful.
    assert t["groups"]["S"] == [{"primary": "754751", "members": ["754751"],
                                 "per_case": 1.0, "unit": "EA", "tracked": True}]
    assert t["groups"]["T"] == [] and t["groups"]["U"][0]["primary"] == "754751"
    assert t["tracked"] == ["754751"] and t["in_house"] == ["BT001"]
    assert t["items"]["754751"]["opening"] == 120.0
    assert [r["po8"] for r in t["items"]["754751"]["receipts"]] == ["30000001", "30000002"]
    s = board["s|10"]
    assert (s["verdict"], s["item"], s["binding"]["po8"]) == ("DEPENDENT", "754751", "30000002")
    assert (board["t|10"]["verdict"], board["t|10"]["item"]) == ("OK", None)
    assert board["u|30"]["verdict"] == "DEPENDENT"
    # And the port agrees on every field (the general check, named here).
    _assert_same(board, g["board"], c["name"])
    assert g["groups"]["S"][0]["members"] == ["754751"]


def test_lenient_numbers(parity):
    """_num parity for opening/snapshot strings: '1,200', ' 1_000 ', '5e2'
    parse; a bool and garbage read as 0 (SHORT, not NaN-as-0 everywhere)."""
    c, g = _extra(parity, "lenient_numbers")
    t, board = _py_case(c)
    assert {k: s["verdict"] for k, s in board.items()} == {
        "a|10": "OK", "b|10": "OK", "c|10": "OK", "d|10": "SHORT", "e|10": "SHORT"}
    assert t["items"]["754753"]["snapshot_h"] == 2.5
    _assert_same(board, g["board"], c["name"])


def test_sub_day_leads_read_in_hours(parity):
    """verdict_text / verdictText: a lead under a day is whole hours (half
    up), never "0.0 d"; 24 h and up stays tenths of a day — byte-identical
    on both sides, engine stamps and anchored stamps alike."""
    c, g = _extra(parity, "sub_day_leads")
    _, board = _py_case(c)
    want = {"h1|30": "— 1 h before start (min 4 d)",
            "h14|40": "— 14 h before start (min 4 d)",
            "mid|10": "— 3 h after start (mid-run) (min 4 d)",
            "d1|60": "— 1.0 d before start (min 4 d)",
            "h0|30": "— 0 h before start (min 4 d)"}
    assert {k: s["verdict"] for k, s in board.items()} == dict.fromkeys(want, "DEPENDENT")
    for key, frag in want.items():
        assert frag in board[key]["text"], (key, board[key]["text"])
        assert "0.0 d" not in board[key]["text"]
        assert g["board"][key]["text"] == board[key]["text"], key
        assert g["text_anchor"][key] == tl.verdict_text(g["board"][key], ANCHOR), key
    assert "— 1 h before start" in g["text_anchor"]["h1|30"]
    _assert_same(board, g["board"], c["name"])


def test_remove_block_matches_rebuild(parity):
    """removeBlock on a copy == build_timelines without that block; the
    shared timelines stay intact; an unknown key is a no-op."""
    c, g = _extra(parity, "remove_block_flips_neighbour")
    inp = c["inputs"]
    _, board = _py_case(c)
    assert board["b3|50"]["verdict"] == "SHORT"
    rest = [b for b in inp["blocks"] if tl.block_key(b) != c["remove_key"]]
    t2 = tl.build_timelines(rest, inp["sku_needs"], inp["opening"], inp["receipts"],
                            inp["snapshot_h"], tracked=inp["tracked"],
                            in_house=inp["in_house"])
    board2 = tl.evaluate_board(rest, t2, c["rules"], inp["feed_state"],
                               inp["receipts_window_end_h"])
    assert board2["b3|50"]["verdict"] == "OK"
    p = g["remove_probe"]
    assert (p["removed"], p["missing"], p["n_blocks"]) == (True, False, 2)
    _assert_same(board2, p["board"], "after removeBlock")
    _assert_same(_draws(t2), p["draws"], "draws after removeBlock")
    assert g["stable"], "removeBlock on the copy touched the shared timelines"
