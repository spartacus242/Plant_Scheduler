# tests/test_block_identity.py — one-piece targeting for Gantt edits.
#
# utils/blockIdentity.ts is the pure half of the 2026-09-01 split-piece fix:
# split MO pieces share one id (calendar_blocks.csv `;split`), so
# useScheduleState's edits resolve their target to ONE list index — object
# identity, else (id, start_hour), else (bare id, legacy callers) the FIRST
# piece — instead of patching every block with the id. Compiled with the
# frontend's own tsc and pinned under node, the same harness as
# test_sku_picker_math. Skipped (not passed) when node or the frontend's
# node_modules are missing. Synthetic blocks only.

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "code" / "components" / "gantt" / "frontend"
SRC = FRONTEND / "src"
IDENTITY_TS = SRC / "utils" / "blockIdentity.ts"

RUNNER_JS = """
const path = require("path");
const out = process.argv[2];
const bi = require(path.join(out, "utils", "blockIdentity.js"));

// Live-board shape: a running split MO (two pieces, ONE id) plus a unique
// block. Hours mirror the real cs_ split so the float keys are realistic.
const A = { id: "cs_split", start_hour: 0.017, end_hour: 119.733, line_name: "P10", qty_kg: 63251.1 };
const B = { id: "cs_split", start_hour: 125.733, end_hour: 188.112, line_name: "P10", qty_kg: 32957.5 };
const C = { id: "blk_1", start_hour: 200, end_hour: 210, line_name: "P10", qty_kg: 100 };
const list = [A, B, C];
const asc = (a, b) => a - b;
const r = {};

r.key = bi.blockKey(B);
r.refId = [bi.refId("x"), bi.refId({ id: "y", start_hour: 1 })];
r.same = {
  identity: bi.sameBlock(B, B),
  copy: bi.sameBlock(B, { ...B }),
  sibling: bi.sameBlock(A, B),
  otherId: bi.sameBlock(C, { ...C, id: "blk_2" }),
};

r.idx = {
  bareId: bi.findBlockIndex(list, "cs_split"),
  pair: bi.findBlockIndex(list, { id: "cs_split", start_hour: 125.733 }),
  object: bi.findBlockIndex(list, B),
  copy: bi.findBlockIndex(list, { ...B }),
  unique: bi.findBlockIndex(list, "blk_1"),
  unknownId: bi.findBlockIndex(list, "nope"),
  unknownStart: bi.findBlockIndex(list, { id: "cs_split", start_hour: 50 }),
  // Resize hint: a bare id shared by two pieces -> the piece keeping an edge.
  edgeEnd: bi.findBlockIndex(list, "cs_split", { edges: [122, 188.112] }),
  edgeStart: bi.findBlockIndex(list, "cs_split", { edges: [0.017, 121] }),
  edgeNone: bi.findBlockIndex(list, "cs_split", { edges: [10, 20] }),
  edgeUnique: bi.findBlockIndex(list, "blk_1", { edges: [10, 20] }),
  edgePair: bi.findBlockIndex(list, B, { edges: [0.017, 121] }),
};
r.found = bi.findBlock(list, { id: "cs_split", start_hour: 125.733 });
r.foundMissing = bi.findBlock(list, "nope") === undefined;

// Move piece B: only B changes; A and C are the SAME objects; input untouched.
const moved = bi.patchOne(list, B, { start_hour: 130, end_hour: 192.379, line_name: "P19" });
r.patch = {
  newArray: moved !== list,
  len: moved.length,
  a: moved[0], b: moved[1], c: moved[2],
  aSameObj: moved[0] === A, cSameObj: moved[2] === C,
  inputUntouched: B.start_hour === 125.733 && list[1] === B && list.length === 3,
};
r.patchFn = bi.patchOne(list, "blk_1", (b) => ({ ...b, qty_kg: b.qty_kg * 2 }))[2].qty_kg;
r.patchMiss = bi.patchOne(list, "nope", { qty_kg: 0 }) === list;
// Bare id: FIRST piece only (documented legacy fallback), never both.
const bare = bi.patchOne(list, "cs_split", { qty_kg: 1 });
r.patchBare = [bare[0].qty_kg, bare[1].qty_kg];
// Resize by bare id with the end edge fixed lands on B; A untouched.
const rs = bi.patchOne(list, "cs_split", { start_hour: 122 }, { edges: [122, 188.112] });
r.patchEdge = [rs[0].start_hour, rs[1].start_hour];

const rm = bi.removeOne(list, B);
r.remove = {
  len: rm.length, keys: rm.map(bi.blockKey), aSameObj: rm[0] === A,
  miss: bi.removeOne(list, "nope") === list, inputLen: list.length,
};
r.removeBare = bi.removeOne(list, "cs_split").map(bi.blockKey);

r.indices = {
  mixed: [...bi.resolveIndices(list, ["cs_split", B, "blk_1"])].sort(asc),
  dedupe: [...bi.resolveIndices(list, ["blk_1", { id: "blk_1", start_hour: 200 }, C])],
  unknown: [...bi.resolveIndices(list, ["nope", { id: "blk_1", start_hour: 5 }])],
  bareTwice: [...bi.resolveIndices(list, ["cs_split", "cs_split"])],
  empty: [...bi.resolveIndices(list, [])],
};
process.stdout.write(JSON.stringify(r));
"""


def _compile_identity_ts(tmp: Path) -> Path:
    tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        pytest.skip("frontend node_modules not installed (npm install in frontend dir)")
    out = tmp / "compiled"
    proc = subprocess.run(
        [
            "node", str(tsc), str(IDENTITY_TS),
            "--outDir", str(out),
            # blockIdentity.ts imports nothing, so pin the src/ root
            # explicitly to keep the utils/ layout of the other harnesses.
            "--rootDir", str(SRC),
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
    js = out / "utils" / "blockIdentity.js"
    assert js.exists(), f"expected {js}"
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_block_identity(tmp_path):
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    out = _compile_identity_ts(tmp_path)
    proc = subprocess.run(
        ["node", str(runner_path), str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node runner failed:\n{proc.stdout}\n{proc.stderr}"
    r = json.loads(proc.stdout)

    # Key format is the supply-verdict key (contract §5): `${id}|${start_hour}`.
    assert r["key"] == "cs_split|125.733"
    assert r["refId"] == ["x", "y"]

    # Same piece = identical object or same (id, start_hour); siblings differ.
    assert r["same"] == {"identity": True, "copy": True, "sibling": False, "otherId": False}

    # Resolution order: identity -> (id, start_hour) -> bare id = FIRST piece.
    i = r["idx"]
    assert i["bareId"] == 0
    assert i["pair"] == 1 and i["object"] == 1 and i["copy"] == 1
    assert i["unique"] == 2
    assert i["unknownId"] == -1 and i["unknownStart"] == -1
    # Resize hint only disambiguates a BARE id: the fixed edge picks the
    # piece, no edge -> first piece, and an explicit ref ignores the hint.
    assert i["edgeEnd"] == 1 and i["edgeStart"] == 0
    assert i["edgeNone"] == 0 and i["edgeUnique"] == 2
    assert i["edgePair"] == 1
    assert r["found"]["start_hour"] == 125.733 and r["foundMissing"] is True

    # patchOne touches exactly one slot, keeps the other objects by
    # reference (React bail-outs), and never mutates the input list.
    p = r["patch"]
    assert p["newArray"] is True and p["len"] == 3
    assert p["a"]["start_hour"] == 0.017 and p["a"]["line_name"] == "P10"
    assert p["b"]["start_hour"] == 130 and p["b"]["end_hour"] == 192.379
    assert p["b"]["line_name"] == "P19" and p["b"]["qty_kg"] == 32957.5
    assert p["c"]["start_hour"] == 200
    assert p["aSameObj"] is True and p["cSameObj"] is True
    assert p["inputUntouched"] is True
    assert r["patchFn"] == 200
    assert r["patchMiss"] is True  # no match -> same reference, no re-render
    # The pre-fix bug in one line: a bare id must NOT hit both pieces.
    assert r["patchBare"] == [1, 32957.5]
    assert r["patchEdge"] == [0.017, 122]

    # removeOne parks exactly the named piece; the sibling stays on the board.
    rm = r["remove"]
    assert rm["len"] == 2 and rm["keys"] == ["cs_split|0.017", "blk_1|200"]
    assert rm["aSameObj"] is True and rm["miss"] is True and rm["inputLen"] == 3
    assert r["removeBare"] == ["cs_split|125.733", "blk_1|200"]

    # Shift sets: one index per ref, deduped, unresolved refs dropped; two
    # bare copies of a shared id both mean the first piece (documented).
    ix = r["indices"]
    assert ix["mixed"] == [0, 1, 2]
    assert ix["dedupe"] == [2]
    assert ix["unknown"] == []
    assert ix["bareTwice"] == [0]
    assert ix["empty"] == []
