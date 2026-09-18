# code/stockcheck/projection.py — time-phased, PO-aware demand projection
# (Supply Timeline slice 3, 2026-09-15).
#
# The flat demand view compares every demand order against the whole on-hand
# pile: no netting across weeks or orders, no board consumption, no inbound.
# The solver's stock policy (helpers/agent_policy) read that view, so a SKU
# short on hand was capped even when its truck landed on Tuesday, and one
# short week capped every week of the SKU. This module grades each demand
# order on the SAME per-item curves the Gantt uses (stockcheck.timeline):
#
#   * the board's committed blocks draw first (they are already in the
#     curves), so a week the board covers has no residual to plan;
#   * orders are netted SEQUENTIALLY in (week, priority, order) order —
#     an earlier order takes the shared component first, the way MRP nets;
#   * gated receipts lift an order that on-hand cannot support, and every
#     counted receipt pins the order's EARLIEST START at ready + L (the
#     plant's "minimum 4 days after a delivery" rule), so the solver never
#     plans a run inside the delivery buffer;
#   * receipts only ever LIFT a trim: an order on-hand supports to >= the
#     DNS ratio keeps today's behaviour (no cap, no floor).
#
# Pure: stdlib only, JSON-able output, deterministic order. Hours are floats
# in the board's storage frame; the caller converts demand weeks into that
# frame (never mix the demand anchor and the board anchor — time-frame
# invariant).

from __future__ import annotations

import math

from stockcheck.timeline import EPS, _hard_at, _rules

STATUS_OK = "OK"             # on hand supports >= dns_ratio of the residual
STATUS_COVERED = "COVERED"   # the board already covers the week's target
STATUS_LIFTED = "LIFTED"     # inbound receipts lift it to >= dns_ratio
STATUS_DNS = "DNS"           # below dns_ratio even with usable inbound
STATUS_NO_DATA = "NO_DATA"   # no recipe / unknown quantity — never gates

DNS_RATIO = 0.90             # mirrors stockcheck.coverage.DNS_RATIO
# A residual at or below this is the board covering the week: the ledger
# zeroes such residuals too (demand_coverage.apply_ledger, netting-7), and a
# 0.3 kg "need" against an overdrawn pile read as -11,563 % on live data.
RESIDUAL_FLOOR_KG = 0.5


def _fin(x) -> float | None:
    """JSON-safe float: None for None/NaN/inf."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _overlap_frac(a: float, e: float, ws: float, we: float) -> float:
    """Share of [a, e) inside [ws, we) — the ledger's week-overlap rule."""
    if e <= a:
        return 0.0
    lo, hi = max(a, ws), min(e, we)
    if hi <= lo:
        return 0.0
    return (hi - lo) / (e - a)


def board_kg_in_window(timelines: dict, sku: str, ws: float, we: float,
                       kg_per_case: float) -> float:
    """kg the board's registered blocks of `sku` make inside [ws, we),
    pro-rated by time overlap (split pieces count each on their own)."""
    total_cases = 0.0
    for b in timelines.get("blocks", {}).values():
        if str(b.get("sku", "")) != str(sku):
            continue
        cases = b.get("cases")
        if not isinstance(cases, (int, float)) or isinstance(cases, bool):
            continue
        if not math.isfinite(cases) or cases <= 0:
            continue
        total_cases += float(cases) * _overlap_frac(
            float(b.get("start_h", 0.0)), float(b.get("end_h", 0.0)), ws, we)
    return total_cases * float(kg_per_case or 0.0)


class _Ledger:
    """Allocations booked by earlier orders, per funding source: on-hand
    per item, and each receipt (item, index) separately — so an order that
    counts fewer receipts than an earlier one never sees the earlier
    order's receipt-funded take as missing on-hand."""

    def __init__(self, timelines: dict):
        self.items = timelines["items"]
        self.onhand: dict[str, float] = {}
        self.rcpt: dict[tuple[str, int], float] = {}

    def onhand_rem(self, m: str, we: float) -> float:
        it = self.items[m]
        return _hard_at(float(it["opening"]), it["draws"], we) \
            - self.onhand.get(m, 0.0)

    def receipts(self, m: str, floor_h: float | None, L: float) -> list:
        """[(idx, receipt, remaining qty)] counted at `floor_h`: a receipt
        counts when ready + L <= floor (None counts nothing)."""
        if floor_h is None:
            return []
        out = []
        for i, r in enumerate(self.items[m]["receipts"]):
            rh = float(r["ready_h"])
            if rh + L <= floor_h + EPS:
                rem = float(r["qty"]) - self.rcpt.get((m, i), 0.0)
                out.append((i, r, rem))
        return out

    def member_avail(self, m: str, we: float, floor_h: float | None,
                     L: float) -> float:
        v = self.onhand_rem(m, we)
        for _, _, rem in self.receipts(m, floor_h, L):
            v += rem
        return v

    def book(self, m: str, we: float, floor_h: float | None, L: float,
             qty: float) -> float:
        """Book `qty` of item m: on-hand first, then counted receipts in
        ready order. Returns what could be booked (<= qty)."""
        left = qty
        oh = self.onhand_rem(m, we)
        if oh > EPS:
            take = min(left, oh)
            self.onhand[m] = self.onhand.get(m, 0.0) + take
            left -= take
        for i, _, rem in self.receipts(m, floor_h, L):
            if left <= EPS:
                break
            if rem <= EPS:
                continue
            take = min(left, rem)
            self.rcpt[(m, i)] = self.rcpt.get((m, i), 0.0) + take
            left -= take
        return qty - left


def _group_avail(led: _Ledger, g: dict, we: float, floor_h, L: float
                 ) -> float:
    return sum(led.member_avail(m, we, floor_h, L) for m in g["members"])


def _counted_receipts(led: _Ledger, groups: list, floor_h, L: float) -> list:
    out = []
    if floor_h is None:
        return out
    seen: set = set()
    for g in groups:
        for m in g["members"]:
            for i, r, rem in led.receipts(m, floor_h, L):
                if (m, i) in seen:
                    continue
                seen.add((m, i))
                out.append({"item": m, "po8": str(r.get("po8", "")),
                            "ready_h": float(r["ready_h"]),
                            "qty": float(r["qty"]),
                            "remaining": max(0.0, rem),
                            "receipt_date": r.get("receipt_date"),
                            "label": r.get("label", ""),
                            # buyers' PO-line comments (2026-09-17), "" when
                            # the receipt came without them
                            "comment": str(r.get("comment") or ""),
                            "comment_external": str(r.get("comment_external") or "")})
    out.sort(key=lambda x: (x["ready_h"], x["po8"], x["item"]))
    return out


def project_demand(orders: list, timelines: dict, *, rules=None,
                   feed_state: str = "ok", kg_per_case: dict | None = None,
                   dns_ratio: float = DNS_RATIO, stamp=None,
                   week_label=None) -> dict:
    """Per-order projection: {order_id: record}.

    orders: [{"order_id", "sku", "week_index", "target_kg", "cases",
              "priority", "ws_h", "we_h"}] — cases = gross target cases,
    [ws_h, we_h) the demand week in board hours. timelines: the board's
    timelines (timeline.build_timelines) — blocks already registered.
    kg_per_case: {sku: kg}. stamp(h) / week_label(week_index) only feed the
    planner sentence.

    Record keys (all JSON-safe):
      status, window_h [ws, we], gross_kg, board_kg, residual_kg,
      onhand_ratio, ratio (both vs the residual; None when no residual),
      ratio_gross ((board + supportable residual) / gross — comparable
      with the flat achievable_ratio), cap_kg (new-production kg the
      components support; None = no cap), earliest_start_h (None = no
      floor), frac_window, constraining, receipts (counted at the floor),
      allocated_kg, text.
    """
    R = _rules(rules)
    L = 24.0 * float(R["min_days_after_delivery"])
    kpc = kg_per_case or {}
    stamp = stamp or (lambda h: f"h{float(h):.0f}")
    week_label = week_label or (lambda w: f"W{int(w)}")
    led = _Ledger(timelines)
    groups_by_sku = timelines.get("groups", {})
    feed_ok = str(feed_state) == "ok"

    def sort_key(o):
        pr = o.get("priority")
        try:
            pr = float(pr)
        except (TypeError, ValueError):
            pr = 999.0
        if not math.isfinite(pr):
            pr = 999.0
        # week, priority, then LARGEST first — the same order the greedy
        # seed places orders in, so the caps and the seed agree on who
        # takes a shared component first inside one week.
        return (float(o.get("ws_h", 0.0)), pr,
                -(_fin(o.get("target_kg")) or 0.0), str(o.get("order_id", "")))

    out: dict = {}
    for o in sorted(orders or [], key=sort_key):
        oid = str(o.get("order_id", ""))
        sku = str(o.get("sku", ""))
        ws = float(o.get("ws_h", 0.0))
        we = float(o.get("we_h", ws + 168.0))
        gross_kg = _fin(o.get("target_kg")) or 0.0
        cases = _fin(o.get("cases")) or 0.0
        wl = week_label(o.get("week_index", 0))
        kg_pc = _fin(kpc.get(sku)) or (gross_kg / cases if cases > 0 else 0.0)
        rec = {
            "status": STATUS_NO_DATA, "window_h": [ws, we],
            "gross_kg": gross_kg, "board_kg": 0.0, "residual_kg": gross_kg,
            "onhand_ratio": None, "ratio": None, "ratio_gross": None,
            "cap_kg": None, "earliest_start_h": None, "frac_window": 1.0,
            "constraining": None, "receipts": [], "groups": [],
            "allocated_kg": 0.0, "feed_state": str(feed_state), "text": "",
        }
        out[oid] = rec
        groups = [g for g in groups_by_sku.get(sku, []) if g.get("tracked", True)]
        if sku not in groups_by_sku:
            rec["text"] = f"no recipe for {sku} — not graded"
            continue
        if cases <= 0 or gross_kg <= 0:
            rec["text"] = "unknown quantity (no kg per case) — not graded"
            continue

        board_kg = board_kg_in_window(timelines, sku, ws, we, kg_pc)
        residual_kg = max(0.0, gross_kg - board_kg)
        if residual_kg <= RESIDUAL_FLOOR_KG:
            residual_kg = 0.0
        residual_cases = residual_kg / kg_pc if kg_pc > 0 else 0.0
        rec.update(board_kg=board_kg, residual_kg=residual_kg)

        if not groups:
            rec.update(status=STATUS_OK, onhand_ratio=None, ratio=None,
                       ratio_gross=None,
                       text="no tracked component gates this SKU")
            continue

        def ratio_at(floor_h) -> tuple[float, dict | None]:
            worst, worst_g = math.inf, None
            for g in groups:
                need = residual_cases * float(g["per_case"])
                if need <= EPS:
                    continue
                r = _group_avail(led, g, we, floor_h, L) / need
                if r < worst:
                    worst, worst_g = r, g
            return worst, worst_g

        def group_detail(floor_h) -> list:
            """Per tracked group, the balance sheet behind the ratio at
            `floor_h` (planner drill-down + the overnight brief)."""
            out_g = []
            for g in groups:
                need = residual_cases * float(g["per_case"])
                opening = drawn = booked = inbound = 0.0
                for m in g["members"]:
                    it = led.items[m]
                    opening += float(it["opening"])
                    drawn += float(it["opening"]) - _hard_at(float(it["opening"]),
                                                             it["draws"], we)
                    booked += led.onhand.get(m, 0.0)
                    for _, _, rem in led.receipts(m, floor_h, L):
                        inbound += max(0.0, rem)
                avail = _group_avail(led, g, we, floor_h, L)
                out_g.append({
                    "item": g["primary"], "unit": g.get("unit", ""),
                    "need": _fin(need), "opening": _fin(opening),
                    "board_drawn": _fin(drawn), "booked_earlier": _fin(booked),
                    "inbound_counted": _fin(inbound), "avail": _fin(avail),
                    "ratio": _fin(min(avail / need, 9.99)) if need > EPS else None,
                })
            out_g.sort(key=lambda x: (x["ratio"] is None, x["ratio"] or 0.0))
            return out_g

        def supportable_kg(floor_h) -> float:
            best = math.inf
            for g in groups:
                pc = float(g["per_case"])
                if pc <= 0:
                    continue
                best = min(best, _group_avail(led, g, we, floor_h, L) / pc)
            if not math.isfinite(best):
                return math.inf
            return max(0.0, best) * kg_pc

        if residual_cases <= EPS:
            head = supportable_kg(None)
            rec.update(status=STATUS_COVERED, cap_kg=_fin(head),
                       ratio_gross=_fin(board_kg / gross_kg) if gross_kg else None,
                       groups=group_detail(None),
                       text=(f"board covers {wl}: {board_kg:,.0f} kg on the "
                             f"board vs {gross_kg:,.0f} kg target"))
            continue

        r0, g0 = ratio_at(None)
        rec["onhand_ratio"] = _fin(min(r0, 9.99)) if math.isfinite(r0) else None
        if r0 >= dns_ratio:
            alloc = min(residual_cases, supportable_kg(None) / kg_pc
                        if kg_pc > 0 else residual_cases)
            _book(led, groups, we, None, L, alloc)
            rec.update(status=STATUS_OK, ratio=rec["onhand_ratio"],
                       ratio_gross=_fin(min((board_kg + residual_kg) / gross_kg, 9.99)),
                       allocated_kg=alloc * kg_pc, groups=group_detail(None),
                       text=(f"on hand supports {min(r0, 9.99):.0%} of the "
                             f"{residual_kg:,.0f} kg to plan in {wl}"))
            continue

        # On hand falls short: which receipts, if any, are worth waiting for?
        # Candidate floors = ready + L of every receipt of the SKU's tracked
        # groups that lands with time to spare inside the week. Score = the
        # supportable share (clipped at 100%) x the share of the week left
        # after the floor; the on-hand-only option scores at the full week.
        cands: set = set()
        if feed_ok:
            for g in groups:
                for m in g["members"]:
                    for r in led.items[m]["receipts"]:
                        f = float(r["ready_h"]) + L
                        if f < we - EPS:
                            cands.add(f)
        # Scores are clipped to [0, 1]: a board that already overdraws the
        # pile reads as 0 on hand, and a truck only wins when it leaves a
        # POSITIVE supportable share — never a floor for nothing.
        best_f, best_score, best_r, best_g = None, max(0.0, min(r0, 1.0)), r0, g0
        for f in sorted(cands):
            rf, gf = ratio_at(f)
            frac = 1.0 if f <= ws else max(0.0, (we - f) / (we - ws)) \
                if we > ws else 1.0
            score = max(0.0, min(rf, 1.0)) * frac
            if score > best_score + EPS:
                best_f, best_score, best_r, best_g = f, score, rf, gf
        cap = supportable_kg(best_f)
        cap = cap if math.isfinite(cap) else None
        frac = 1.0
        if best_f is not None and best_f > ws and we > ws:
            frac = max(0.0, (we - best_f) / (we - ws))
        alloc_cases = residual_cases if cap is None else min(residual_cases, cap / kg_pc)
        # receipts as this order finds them (remaining BEFORE its own take)
        counted = _counted_receipts(led, groups, best_f, L)
        detail = group_detail(best_f)
        _book(led, groups, we, best_f, L, alloc_cases)
        lifted = best_f is not None and best_r >= dns_ratio
        constraining = (best_g or g0 or {}).get("primary")
        ratio_gross = (board_kg + min(residual_kg, cap if cap is not None else residual_kg)) / gross_kg
        rec.update(
            status=STATUS_LIFTED if lifted else STATUS_DNS,
            ratio=_fin(min(best_r, 9.99)) if math.isfinite(best_r) else None,
            ratio_gross=_fin(min(ratio_gross, 9.99)),
            cap_kg=_fin(cap), earliest_start_h=best_f, frac_window=frac,
            constraining=constraining, receipts=counted, groups=detail,
            allocated_kg=alloc_cases * kg_pc,
        )
        base = (f"on hand supports {min(r0, 9.99):.0%} of the "
                f"{residual_kg:,.0f} kg to plan in {wl}"
                + (f" ({constraining})" if constraining else ""))
        if best_f is None:
            if not feed_ok:
                why = f"PO feed {feed_state}: inbound not counted"
            elif cands:
                why = "waiting for a truck would leave too little of the week"
            else:
                why = "no inbound lands in time"
            rec["text"] = f"{base} · {why} · cap {cap:,.0f} kg" if cap is not None \
                else f"{base} · {why}"
        else:
            trucks = ", ".join(f"PO {r['po8']} ({r['item']}) {stamp(r['ready_h'])}"
                               for r in counted[:3])
            rec["text"] = (f"{base} · {trucks} → {min(best_r, 9.99):.0%} from "
                           f"{stamp(best_f)} (buffer {L / 24:g} d)"
                           + (f" · cap {cap:,.0f} kg" if cap is not None else ""))
    return out


def _book(led: _Ledger, groups: list, we: float, floor_h, L: float,
          alloc_cases: float) -> None:
    """Book an order's draw on every group, primary member first."""
    if alloc_cases <= EPS:
        return
    for g in groups:
        left = alloc_cases * float(g["per_case"])
        if left <= EPS:
            continue
        for m in g["members"]:
            if left <= EPS:
                break
            avail = led.member_avail(m, we, floor_h, L)
            if avail <= EPS:
                continue
            left -= led.book(m, we, floor_h, L, min(left, avail))


def summarize(projected: dict) -> dict:
    """Counts for the report header / brief."""
    n = {STATUS_OK: 0, STATUS_COVERED: 0, STATUS_LIFTED: 0, STATUS_DNS: 0,
         STATUS_NO_DATA: 0}
    floors = 0
    capped = 0
    for rec in (projected or {}).values():
        n[rec.get("status", STATUS_NO_DATA)] = n.get(rec.get("status"), 0) + 1
        if rec.get("earliest_start_h") is not None:
            floors += 1
        if rec.get("cap_kg") is not None and rec.get("status") in (
                STATUS_LIFTED, STATUS_DNS):
            capped += 1
    return {"by_status": n, "n_floors": floors, "n_capped": capped}
