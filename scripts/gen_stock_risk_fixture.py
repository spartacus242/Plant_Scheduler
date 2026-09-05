"""Golden cases for the supply timeline engine (stockcheck/timeline.py).

Usage (repo root):
    PYTHONPATH=code .venv/Scripts/python.exe scripts/gen_stock_risk_fixture.py

Writes data/test_fixtures/stock_risk/cases.json — the SAME cases
tests/test_timeline.py asserts on (it imports CASES from here) and the
fixture the TypeScript parity test replays against utils/stockRisk.ts.
Everything is synthetic (plan §7 decision 12): the 280351/754751 numbers
are the plan's worked table with a hypothetical sleeves stock, nothing
here is read from data/reference.

Fixture shape (contract §3.6):
    {"rules": DEFAULT_RULES,
     "cases": [{"name", "rules" (effective, per case),
                "inputs": {blocks, sku_needs, opening, tracked, in_house,
                           receipts, snapshot_h, feed_state,
                           receipts_window_end_h},
                "expected": {block key: Supply},
                "expected_draws": {item: [{key, a, e, qty}]},
                "earliest_clear_start": {"args", "expected"} | null}]}
Floats are rounded to 6 dp; compare with a tolerance of 1e-6.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from stockcheck import timeline as tl  # noqa: E402

OUT = ROOT / "data" / "test_fixtures" / "stock_risk" / "cases.json"

SKU = "280351"
ITEM = "754751"
SNAPSHOT_H = 14.8
WINDOW_END = 800.0
IN_HOUSE = ["BT001", "BT002"]


def block(key, block_id, sku, line, start, end, cases, *, locked=False,
          running=False, cases_left=None) -> dict:
    return {"key": key, "block_id": block_id, "sku": sku, "line_name": line,
            "start_h": start, "end_h": end, "cases": cases, "locked": locked,
            "running": running, "cases_left": cases_left}


def receipt(ready_h, qty, po8, receipt_date, tier="erp") -> dict:
    return {"ready_h": ready_h, "qty": qty, "po8": po8, "tier": tier,
            "receipt_date": receipt_date,
            "label": f"PO {po8} · {ITEM} · {qty:,.0f} EA · {receipt_date} · "
                     f"{'appt' if tier == 'appt' else 'ERP date'}"}


def needs(sku, item, per_case=1.0, unit="EA", alts=(), kg_per_case=2.16) -> dict:
    return {sku: {"kg_per_case": kg_per_case,
                  "items": [{"item": item, "per_case": per_case, "unit": unit,
                             "alts": list(alts)}]}}


# The plan's worked table: running P10 MO, queued P19 MO, P10 split tail.
P10_RUN = block("cs_87c620df4a@0.02", "cs_87c620df4a", SKU, "P10", 0.017,
                119.733, 29283.0, locked=True, running=True)
P19 = block("cs_44c4bde023@71.85", "cs_44c4bde023", SKU, "P19", 71.85, 131.5,
            41435.0)
P10_TAIL = block("cs_87c620df4a@125.73", "cs_87c620df4a", SKU, "P10", 125.733,
                 188.112, 15258.0)
R40 = receipt(40.0, 67200.0, "30043543", "2026-09-02")
R112 = receipt(112.0, 67200.0, "30043543", "2026-09-05")

# A second tracked item with plenty of stock: a block that stays OK whatever
# the feed says (verdict rule 1 runs before the feed check).
OK_SKU = "280900"
OK_ITEM = "752420"
OK_BLOCK = block("ok_blk@30.00", "ok_blk", OK_SKU, "P21", 30.0, 50.0, 1000.0)


def base_inputs(**over) -> dict:
    inp = {
        "blocks": [P10_RUN, P19, P10_TAIL],
        "sku_needs": needs(SKU, ITEM),
        "opening": {ITEM: 36000.0},
        "tracked": [ITEM],
        "in_house": list(IN_HOUSE),
        "receipts": {ITEM: [R40]},
        "snapshot_h": {ITEM: SNAPSHOT_H},
        "feed_state": "ok",
        "receipts_window_end_h": WINDOW_END,
    }
    inp.update(over)
    return inp


def with_ok_block(inp: dict) -> dict:
    inp = dict(inp)
    inp["blocks"] = inp["blocks"] + [OK_BLOCK]
    inp["sku_needs"] = {**inp["sku_needs"], **needs(OK_SKU, OK_ITEM, unit="M2")}
    inp["opening"] = {**inp["opening"], OK_ITEM: 50000.0}
    inp["tracked"] = inp["tracked"] + [OK_ITEM]
    inp["snapshot_h"] = {**inp["snapshot_h"], OK_ITEM: SNAPSHOT_H}
    return inp


def case(name, inputs, rules=None, ecs=None) -> dict:
    return {"name": name, "inputs": inputs, "rules": dict(rules or {}),
            "earliest_clear_start": ecs}


# --- alternate pooling: 752700 primary with alt 752701; another SKU has
# 752701 as its own primary and drains the shared pool.
ALT_SKU, ALT_PRIMARY, ALT_ITEM = "280700", "752700", "752701"
ALT_OTHER_SKU = "280701"
ALT_BLOCK = block("h1@20.00", "h1", ALT_SKU, "P19", 20.0, 40.0, 10000.0)
ALT_OTHER_BLOCK = block("h2@20.00", "h2", ALT_OTHER_SKU, "P21", 20.0, 40.0,
                        45000.0)
ALT_BASE = {
    "sku_needs": needs(ALT_SKU, ALT_PRIMARY, alts=[ALT_ITEM]),
    "opening": {ALT_PRIMARY: 0.0, ALT_ITEM: 50000.0},
    "tracked": [ALT_ITEM],           # primary untracked, alt tracked -> group tracked
    "receipts": {},
    "snapshot_h": {ALT_PRIMARY: SNAPSHOT_H, ALT_ITEM: SNAPSHOT_H},
}

ECS_P19 = {"sku": SKU, "cases": 41435.0, "duration_h": 59.65, "from_h": 71.85,
           "line_name": "P19"}

CASES: list[dict] = [
    case("A_worked_table_L96", base_inputs()),
    case("B_zero_buffer", base_inputs(), rules={"min_days_after_delivery": 0}),
    case("C_receipt_after_crossing", base_inputs(receipts={ITEM: [R112]})),
    case("D1_window_exhausted",
         with_ok_block(base_inputs(receipts={}, receipts_window_end_h=20.0))),
    case("D2_feed_missing",
         with_ok_block(base_inputs(receipts={}, feed_state="missing"))),
    # Stacked receipts: opening 36,000 vs 41,435 needed by P19 alone.
    case("E1_stacked_both_needed", base_inputs(
        blocks=[P19],
        receipts={ITEM: [receipt(30.0, 3000.0, "30043601", "2026-09-02"),
                         receipt(50.0, 3000.0, "30043602", "2026-09-03")]})),
    case("E2_stacked_later_not_needed", base_inputs(
        blocks=[P19],
        receipts={ITEM: [receipt(30.0, 10000.0, "30043601", "2026-09-02"),
                         receipt(50.0, 1000.0, "30043602", "2026-09-03")]})),
    # Two lines drawing one item across the same crossing.
    case("F1_concurrent_lines_dependent", base_inputs(
        blocks=[P19, block("ln2@80.00", "ln2", SKU, "P21", 80.0, 130.0,
                           20000.0)])),
    case("F2_concurrent_lines_short", base_inputs(
        blocks=[P19, block("ln2@80.00", "ln2", SKU, "P21", 80.0, 130.0,
                           20000.0)],
        receipts={})),
    # Running block clipped at S; a block finished before S draws nothing.
    case("G1_running_cases_left", base_inputs(
        blocks=[block("run_cl@0.02", "run_cl", SKU, "P10", 0.017, 119.733,
                      29283.0, locked=True, running=True, cases_left=20000.0),
                block("done@0.00", "done", SKU, "P10", 0.0, 10.0, 5000.0,
                      locked=True, running=True)],
        receipts={})),
    case("G2_running_fallback", base_inputs(
        blocks=[block("run_fb@0.02", "run_fb", SKU, "P10", 0.017, 119.733,
                      29283.0, locked=True, running=True),
                block("done@0.00", "done", SKU, "P10", 0.0, 10.0, 5000.0,
                      locked=True, running=True)],
        receipts={})),
    case("H1_alt_pool_covers", base_inputs(blocks=[ALT_BLOCK], **ALT_BASE)),
    case("H2_alt_pool_drained", base_inputs(
        blocks=[ALT_BLOCK, ALT_OTHER_BLOCK],
        **{**ALT_BASE,
           "sku_needs": {**ALT_BASE["sku_needs"],
                         **needs(ALT_OTHER_SKU, ALT_ITEM)}})),
    # Dependent share 1,435 / 41,435 = 3.5% < 5% floor -> minor.
    case("I_minor_dependent", base_inputs(blocks=[P19],
                                          opening={ITEM: 40000.0})),
    # Holding-card question: P19 is NOT on the board; where can it start?
    case("J1_earliest_clear_start", base_inputs(blocks=[P10_RUN, P10_TAIL]),
         ecs=ECS_P19),
    case("J2_earliest_clear_start_no_receipts",
         base_inputs(blocks=[P10_RUN, P10_TAIL], receipts={}), ecs=ECS_P19),
    case("K_lead_from_depletion", base_inputs(),
         rules={"lead_measured_from": "depletion"}),
    # Unknown SKU / unknown cases -> NO_DATA; untracked consumable -> listed.
    case("L_unknown_and_untracked", base_inputs(
        blocks=[block("unk@10.00", "unk", "999999", "P19", 10.0, 20.0, 100.0),
                block("nocases@10.00", "nocases", SKU, "P19", 10.0, 20.0, None),
                block("ink@10.00", "ink", "280800", "P19", 10.0, 20.0, 100.0)],
        sku_needs={**needs(SKU, ITEM),
                   **needs("280800", "758001", per_case=0.01, unit="KG")},
        receipts={})),
]


def run_case(c: dict) -> dict:
    """Build + evaluate one case; shared by the tests and the writer."""
    inp = c["inputs"]
    rules = {**tl.DEFAULT_RULES, **c.get("rules", {})}
    timelines = tl.build_timelines(
        inp["blocks"], inp["sku_needs"], inp["opening"], inp["receipts"],
        inp["snapshot_h"], tracked=inp["tracked"], in_house=inp["in_house"])
    board = tl.evaluate_board(inp["blocks"], timelines, rules,
                              inp["feed_state"], inp["receipts_window_end_h"])
    ecs = None
    a = c.get("earliest_clear_start")
    if a:
        ecs = tl.earliest_clear_start(
            a["sku"], a["cases"], a["duration_h"], timelines, rules,
            inp["feed_state"], inp["receipts_window_end_h"], a["from_h"],
            line_name=a.get("line_name", ""))
    return {"timelines": timelines, "board": board, "ecs": ecs, "rules": rules}


def _round(o):
    if isinstance(o, float):
        return round(o, 6)
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v) for v in o]
    return o


def build_fixture() -> dict:
    cases = []
    for c in CASES:
        r = run_case(c)
        entry = {
            "name": c["name"],
            "rules": r["rules"],
            "inputs": c["inputs"],
            "expected": r["board"],
            "expected_draws": {i: v["draws"]
                               for i, v in r["timelines"]["items"].items()},
            "earliest_clear_start": (
                {"args": c["earliest_clear_start"], "expected": r["ecs"]}
                if c.get("earliest_clear_start") else None),
        }
        cases.append(entry)
    return _round({"rules": dict(tl.DEFAULT_RULES), "cases": cases})


def main() -> int:
    fix = build_fixture()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(fix, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    n_blocks = sum(len(c["expected"]) for c in fix["cases"])
    print(f"wrote {OUT.relative_to(ROOT)}: {len(fix['cases'])} cases, "
          f"{n_blocks} block verdicts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
