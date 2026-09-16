# tests/test_projection.py — slice 3: per-order demand projection on the
# supply-timeline curves (stockcheck/projection.py).
#
# Synthetic sleeves data in the plan's 280351/754751 shape (1 EA per case,
# 2.16 kg per case). Windows are board-frame hours; W0 = [0, 168),
# W1 = [168, 336). Buffer L = 96 h (4 d, the default rule).

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from stockcheck import projection as pj  # noqa: E402
from stockcheck import timeline as tl  # noqa: E402

SKU, ITEM = "280351", "754751"
KPC = 2.16
L = 96.0


def needs(sku=SKU, item=ITEM, per_case=1.0, alts=()):
    return {sku: {"kg_per_case": KPC,
                  "items": [{"item": item, "per_case": per_case, "unit": "EA",
                             "alts": list(alts)}]}}


def receipt(ready_h, qty, po8="30043543"):
    return {"ready_h": ready_h, "qty": qty, "po8": po8, "tier": "erp",
            "receipt_date": "2026-09-02", "label": f"PO {po8}"}


def block(key, sku, start, end, cases):
    return {"key": key, "block_id": key, "sku": sku, "line_name": "P10",
            "start_h": start, "end_h": end, "cases": cases, "locked": False,
            "running": False, "cases_left": None}


def timelines(opening=100000.0, receipts=None, blocks=(), sku_needs=None,
              tracked=None, snapshot_h=0.0):
    sn = sku_needs or needs()
    items = {e["item"] for n in sn.values() for e in n["items"]}
    items |= {a for n in sn.values() for e in n["items"] for a in e["alts"]}
    op = opening if isinstance(opening, dict) else {i: opening for i in items}
    return tl.build_timelines(
        list(blocks), sn, op, receipts or {}, {i: snapshot_h for i in items},
        tracked=tracked if tracked is not None else sorted(items),
        in_house=["BT001", "BT002"])


def order(oid, week, target_kg, sku=SKU, priority=3):
    return {"order_id": oid, "sku": sku, "week_index": week,
            "target_kg": target_kg, "cases": target_kg / KPC,
            "priority": priority, "ws_h": 168.0 * week,
            "we_h": 168.0 * (week + 1)}


def project(orders, tls, feed_state="ok", kpc=None):
    return pj.project_demand(orders, tls, rules={}, feed_state=feed_state,
                             kg_per_case=kpc or {SKU: KPC})


# 10,000 cases = 21,600 kg
KG10K = 10000 * KPC


def test_on_hand_supports_the_order_no_cap_no_floor():
    out = project([order("A-W0", 0, KG10K)], timelines(opening=100000.0))
    r = out["A-W0"]
    assert r["status"] == "OK"
    assert r["cap_kg"] is None and r["earliest_start_h"] is None
    assert r["onhand_ratio"] == pytest.approx(9.99)      # clipped for display
    assert r["ratio_gross"] == pytest.approx(1.0)
    assert r["residual_kg"] == pytest.approx(KG10K)
    assert "on hand supports" in r["text"]


def test_receipt_lifts_a_short_order_and_pins_its_start():
    """3,000 on hand vs 10,000 needed (30%); the truck lands at h10 →
    usable from h106 (buffer 4 d), leaving 62 of 168 week hours: score
    1.0 x 0.37 beats the on-hand 0.30 → LIFTED, cap = everything the pool
    supports, floor = ready + L."""
    tls = timelines(opening=3000.0, receipts={ITEM: [receipt(10.0, 67200.0)]})
    r = project([order("A-W0", 0, KG10K)], tls)["A-W0"]
    assert r["status"] == "LIFTED"
    assert r["earliest_start_h"] == pytest.approx(106.0)
    assert r["cap_kg"] == pytest.approx(70200 * KPC)
    assert r["onhand_ratio"] == pytest.approx(0.3)
    assert r["ratio"] == pytest.approx(7.02)
    assert r["frac_window"] == pytest.approx(62 / 168)
    assert [x["po8"] for x in r["receipts"]] == ["30043543"]
    assert r["constraining"] == ITEM
    assert "buffer 4 d" in r["text"] and "h106" in r["text"]


def test_truck_too_late_in_the_week_keeps_the_on_hand_cap():
    """Same order, truck at h40 → usable h136: only 32 h of the week left
    (score 0.19) — waiting loses more than the 30 % on hand makes, so the
    order stays capped at on-hand with NO floor."""
    tls = timelines(opening=3000.0, receipts={ITEM: [receipt(40.0, 67200.0)]})
    r = project([order("A-W0", 0, KG10K)], tls)["A-W0"]
    assert r["status"] == "DNS"
    assert r["earliest_start_h"] is None
    assert r["cap_kg"] == pytest.approx(3000 * KPC)
    assert r["ratio"] == pytest.approx(0.3)
    assert "too little of the week" in r["text"]


def test_receipt_before_the_week_lifts_fully_with_floor_before_window():
    tls = timelines(opening=3000.0, receipts={ITEM: [receipt(40.0, 67200.0)]})
    r = project([order("A-W1", 1, KG10K)], tls)["A-W1"]
    assert r["status"] == "LIFTED"
    assert r["earliest_start_h"] == pytest.approx(136.0)   # < ws 168: frac 1
    assert r["frac_window"] == pytest.approx(1.0)
    assert r["cap_kg"] == pytest.approx(70200 * KPC)


def test_sequential_netting_across_weeks_of_one_sku():
    """15,000 on hand: W0 takes its 10,000, W1 sees 5,000 → 50 % → capped.
    The flat view read 150 % for BOTH weeks."""
    tls = timelines(opening=15000.0)
    out = project([order("A-W1", 1, KG10K), order("A-W0", 0, KG10K)], tls)
    assert out["A-W0"]["status"] == "OK"
    assert out["A-W0"]["allocated_kg"] == pytest.approx(KG10K)
    w1 = out["A-W1"]
    assert w1["status"] == "DNS"
    assert w1["ratio"] == pytest.approx(0.5)
    assert w1["cap_kg"] == pytest.approx(5000 * KPC)
    assert w1["earliest_start_h"] is None


def test_board_draws_reduce_availability_and_credit_the_week():
    """A board block of the SKU inside W0 draws 8,000 and MAKES 17,280 kg
    of the 21,600 kg target: residual 4,320 kg (2,000 cases) against
    15,000 - 8,000 = 7,000 on hand → OK; gross projection 100 %."""
    tls = timelines(opening=15000.0, blocks=[block("b1", SKU, 20.0, 60.0, 8000.0)])
    r = project([order("A-W0", 0, KG10K)], tls)["A-W0"]
    assert r["status"] == "OK"
    assert r["board_kg"] == pytest.approx(8000 * KPC)
    assert r["residual_kg"] == pytest.approx(2000 * KPC)
    assert r["onhand_ratio"] == pytest.approx(3.5)
    assert r["ratio_gross"] == pytest.approx(1.0)


def test_board_block_straddling_the_week_boundary_is_pro_rated():
    """[150, 190) sits 18/40 in W0: 1,800 of 4,000 cases credit W0 and the
    curve at h168 has drawn exactly those 1,800."""
    tls = timelines(opening=15000.0, blocks=[block("b1", SKU, 150.0, 190.0, 4000.0)])
    r = project([order("A-W0", 0, KG10K)], tls)["A-W0"]
    assert r["board_kg"] == pytest.approx(1800 * KPC)
    assert r["residual_kg"] == pytest.approx(8200 * KPC)
    assert r["onhand_ratio"] == pytest.approx((15000 - 1800) / 8200)


def test_board_covering_the_week_reads_covered_with_headroom_cap():
    tls = timelines(opening=15000.0, blocks=[block("b1", SKU, 0.0, 100.0, 12000.0)])
    r = project([order("A-W0", 0, KG10K)], tls)["A-W0"]
    assert r["status"] == "COVERED"
    assert r["residual_kg"] == 0.0
    assert r["cap_kg"] == pytest.approx(3000 * KPC)       # what is left beyond the board
    assert r["ratio_gross"] == pytest.approx(1.2)
    assert r["earliest_start_h"] is None


def test_stale_feed_counts_no_receipts():
    tls = timelines(opening=3000.0, receipts={ITEM: [receipt(10.0, 67200.0)]})
    r = project([order("A-W0", 0, KG10K)], tls, feed_state="stale")["A-W0"]
    assert r["status"] == "DNS"
    assert r["earliest_start_h"] is None
    assert r["cap_kg"] == pytest.approx(3000 * KPC)
    assert "PO feed stale" in r["text"]


def test_priority_then_size_order_the_netting_inside_a_week():
    """Two SKUs share the sleeve, 12,000 on hand, both need 10,000 in W0:
    the priority-1 order is netted first whatever its id says; at equal
    priority the LARGER order goes first (the greedy seed's order)."""
    sn = {**needs(), **needs(sku="280900")}
    tls = timelines(opening=12000.0, sku_needs=sn)
    kpc = {SKU: KPC, "280900": KPC}
    out = project([order("A-W0", 0, KG10K, priority=3),
                   order("B-W0", 0, KG10K, sku="280900", priority=1)], tls, kpc=kpc)
    assert out["B-W0"]["status"] == "OK"
    assert out["A-W0"]["status"] == "DNS"
    assert out["A-W0"]["ratio"] == pytest.approx(0.2)
    # B needs 12,000 (all of the pile) and is larger → netted first → OK;
    # A finds nothing → capped at 0
    out = project([order("A-W0", 0, KG10K), order("B-W0", 0, 1.2 * KG10K, sku="280900")],
                  timelines(opening=12000.0, sku_needs=sn), kpc=kpc)
    assert out["B-W0"]["status"] == "OK" and out["A-W0"]["status"] == "DNS"
    assert out["A-W0"]["cap_kg"] == 0.0


def test_sub_kilogram_residual_reads_covered():
    """gross 21,600 kg, board 21,599.7 kg → 0.3 kg residual is the board
    covering the week (the ledger drops it too), never a -11,000 % ratio
    with a floor."""
    cases_board = (KG10K - 0.3) / KPC
    tls = timelines(opening=0.0, receipts={ITEM: [receipt(10.0, 500.0)]},
                    blocks=[block("b1", SKU, 0.0, 100.0, cases_board)])
    r = project([order("A-W0", 0, KG10K)], tls)["A-W0"]
    assert r["status"] == "COVERED" and r["residual_kg"] == 0.0
    assert r["earliest_start_h"] is None


def test_alternates_pool_and_book_primary_first():
    sn = needs(alts=("754752",))
    tls = timelines(opening={ITEM: 2000.0, "754752": 9000.0}, sku_needs=sn)
    out = project([order("A-W0", 0, KG10K)], tls)
    r = out["A-W0"]
    assert r["status"] == "OK"
    assert r["onhand_ratio"] == pytest.approx(1.1)
    # a second order of the same SKU finds 1,000 left across the pool
    out2 = pj.project_demand([order("A-W0", 0, KG10K), order("A-W1", 1, KG10K)],
                             tls, rules={}, feed_state="ok",
                             kg_per_case={SKU: KPC})
    assert out2["A-W1"]["ratio"] == pytest.approx(0.1)


def test_receipt_funded_take_does_not_read_as_missing_on_hand():
    """W0 is lifted by the truck (3,000 on hand + 7,000 from the receipt);
    W1 must see the receipt's remaining 60,200, not a 7,000 hole in on-hand."""
    tls = timelines(opening=3000.0, receipts={ITEM: [receipt(10.0, 67200.0)]})
    out = project([order("A-W0", 0, KG10K), order("A-W1", 1, KG10K)], tls)
    assert out["A-W0"]["status"] == "LIFTED"
    assert out["A-W0"]["allocated_kg"] == pytest.approx(KG10K)
    w1 = out["A-W1"]
    assert w1["status"] == "LIFTED"
    assert w1["earliest_start_h"] == pytest.approx(106.0)
    assert w1["onhand_ratio"] == pytest.approx(0.0)
    assert w1["receipts"][0]["remaining"] == pytest.approx(60200.0)
    assert w1["cap_kg"] == pytest.approx(60200 * KPC)


def test_board_overdraw_not_repaid_by_the_truck_gets_no_floor():
    """The board draws 20,000 of 3,000 on hand (its own SHORT blocks); a
    12,000 truck does not even repay that hole → nothing supportable, cap 0
    and NO floor (a floor for zero kg would be noise)."""
    tls = timelines(opening=3000.0, receipts={ITEM: [receipt(10.0, 12000.0)]},
                    blocks=[block("b1", "280900", 0.0, 50.0, 20000.0)],
                    sku_needs={**needs(), **needs(sku="280900")})
    r = project([order("A-W0", 0, KG10K)], tls)["A-W0"]
    assert r["status"] == "DNS"
    assert r["cap_kg"] == 0.0 and r["earliest_start_h"] is None
    assert r["onhand_ratio"] < 0
    g = r["groups"][0]
    assert g["item"] == ITEM and g["opening"] == 3000.0
    assert g["board_drawn"] == pytest.approx(20000.0)
    assert g["inbound_counted"] == 0.0 and g["avail"] == pytest.approx(-17000.0)


def test_group_detail_explains_the_ratio():
    tls = timelines(opening=15000.0, blocks=[block("b1", SKU, 20.0, 60.0, 8000.0)])
    out = project([order("A-W0", 0, KG10K), order("A-W1", 1, KG10K)], tls)
    g1 = out["A-W1"]["groups"][0]
    assert g1["need"] == pytest.approx(10000.0)
    assert g1["board_drawn"] == pytest.approx(8000.0)
    assert g1["booked_earlier"] == pytest.approx(2000.0)     # W0's residual take
    assert g1["avail"] == pytest.approx(5000.0)
    assert g1["ratio"] == pytest.approx(0.5)


def test_no_recipe_or_unknown_quantity_never_gates():
    tls = timelines(opening=0.0)
    out = project([order("X-W0", 0, 5000.0, sku="999999"),
                   {**order("A-W0", 0, 0.0), "cases": 0.0}], tls,
                  kpc={SKU: KPC, "999999": 1.0})
    assert out["X-W0"]["status"] == "NO_DATA" and out["X-W0"]["cap_kg"] is None
    assert out["A-W0"]["status"] == "NO_DATA"


def test_untracked_only_recipe_is_ok():
    tls = timelines(opening=0.0, tracked=[])
    r = project([order("A-W0", 0, KG10K)], tls)["A-W0"]
    assert r["status"] == "OK" and r["cap_kg"] is None


def test_output_is_json_safe_and_summarised():
    tls = timelines(opening=3000.0, receipts={ITEM: [receipt(10.0, 67200.0)]})
    out = project([order("A-W0", 0, KG10K), order("A-W1", 1, KG10K),
                   order("A-W2", 2, KG10K)], tls)
    json.dumps(out, allow_nan=False)
    s = pj.summarize(out)
    assert s["by_status"]["LIFTED"] == 3 and s["n_floors"] == 3
    assert s["n_capped"] == 3
