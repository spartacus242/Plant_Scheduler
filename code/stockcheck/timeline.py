# code/stockcheck/timeline.py — time-phased component supply verdicts.
#
# The flat stock check compares every block against the whole on-hand pile,
# so it cannot say "on hand covers 39% of this run and the truck lands 1.3 d
# before start". This module nets consumption across blocks on ONE curve per
# item, applies gated PO receipts as steps, and grades every (block,
# requirement group) OK / DEPENDENT / SHORT / NO_DATA.
#
# Contract: .hermes/plans/2026-09-01-po-stock-contracts.md §3. The TypeScript
# port (frontend/src/utils/stockRisk.ts) copies this file 1:1 and replays
# data/test_fixtures/stock_risk/cases.json, so: stdlib only, plain loops,
# exact comparisons (no epsilon games beyond EPS), sorted deterministic
# order everywhere. Hours are floats in the board's storage frame; dates
# become hours only inside gate_receipts via helpers.timefmt.

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta

from helpers.timefmt import datetime_to_hour, hour_to_stamp, parse_datetime
# Same package: the PO importer owns the batch-date and item-key rules.
from stockcheck.po_import import decode_batch_date, resolve_item_key

# Rule keys mirror helpers.config.stock_config (§1) so a rules dict from the
# toml can be passed straight through.
DEFAULT_RULES: dict = {
    "min_days_after_delivery": 4,
    "lead_measured_from": "block_start",   # or "depletion"
    "receipt_ready_hour": 16,
    "raw_qc_offset_h": 72,
    "appt_ready_offset_h": 2,
    "po_ignore_after_h": 168,
    "landed_match_frac": 0.95,
    "dependent_frac_floor": 0.05,
    "hard_block": False,
    "use_board_qty_kg": True,
    "raw_areas": ["RB1", "AMB", "RC1"],
    "offsite_areas": ["SL3"],
}

# Balances within EPS of zero count as covered: draws that exactly exhaust
# the opening must not flip a block on float noise.
EPS = 1e-6

VERDICT_OK = "OK"
VERDICT_DEPENDENT = "DEPENDENT"
VERDICT_SHORT = "SHORT"
VERDICT_NO_DATA = "NO_DATA"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _rules(rules: dict | None) -> dict:
    out = dict(DEFAULT_RULES)
    out.update(rules or {})
    return out


def _is_num(x) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def _num(v) -> float | None:
    """Lenient number: ints/floats pass through, strings may carry
    commas; bools, blanks, garbage and non-finite values -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    try:
        f = float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _item(x) -> str:
    """Canonical item key: stripped str, so '754751 ' and 754751 pool as
    one item. Only trims — never reformats a code."""
    return "" if x is None else str(x).strip()


def _batch_key(x) -> str:
    """Lot / receipt-slip batch as a join key (2026-09-16): stripped and
    upper-cased; None, NaN and blanks -> '' (never matches anything)."""
    if x is None or (isinstance(x, float) and x != x):
        return ""
    s = str(x).strip().upper()
    return "" if s in ("NAN", "NONE", "NAT") else s


def _js_num(x: float) -> str:
    """JS `String(number)` for the hour range: integral values print without
    a fraction ("10", never "10.0"), anything else in the shortest
    round-trip form (Python repr == JS for 1e-6 <= |x| < 1e21). Keys built
    here must equal the Gantt's blockIdentity.blockKey (`${id}|${start}`)."""
    f = float(x)
    if f == 0.0:
        return "0"
    if f == math.floor(f) and abs(f) < 1e15:
        return str(int(f))
    # JS writes exponents without a zero pad ("1e-7", Python "1e-07")
    return repr(f).replace("e-0", "e-").replace("e+0", "e+")


def block_key(block: dict) -> str:
    """Board key `block_id|start_h` — the Gantt's blockIdentity.blockKey, at
    full precision. Split MOs share block_id, so the start is part of it;
    the old `@{start:.2f}` form collapsed two rows less than 0.005 h apart
    into one key, and add_block then silently dropped the second row's
    draw (audit stock-14). An explicit `key` on the block always wins."""
    k = block.get("key")
    if k:
        return str(k)
    return f"{block.get('block_id', '')}|{_js_num(float(block.get('start_h', 0.0)))}"


def supply_rank(supply: dict) -> int:
    """SHORT 4 > DEPENDENT 3 > NO_DATA 2 > OK backed 1 > OK 0; minor
    DEPENDENT ranks with backed (grey chip)."""
    v = supply.get("verdict")
    if v == VERDICT_SHORT:
        return 4
    if v == VERDICT_DEPENDENT:
        return 1 if supply.get("minor") else 3
    if v == VERDICT_NO_DATA:
        return 2
    return 1 if supply.get("backed") else 0


# --------------------------------------------------------------------------
# §3.1–3.2  timelines and the draw model
# --------------------------------------------------------------------------

def _block_draw(block: dict, per_case: float, snapshot_h: float
                ) -> tuple[float, float, float] | None:
    """(a, e, qty) the block draws of one item, or None when it draws nothing.

    Stock counted at S already reflects consumption up to S, so a running
    block only draws what is left: cases_left when manprg knows it, else the
    uniform tail of the board quantity."""
    start = float(block["start_h"])
    end = float(block["end_h"])
    cases = block.get("cases")
    if not _is_num(cases) or cases <= 0 or per_case <= 0:
        return None
    need = float(cases) * per_case
    if end <= snapshot_h or end <= start:
        return None
    if start < snapshot_h:
        cl = block.get("cases_left")
        if _is_num(cl):
            remaining = float(cl) * per_case
        else:
            remaining = need * (end - snapshot_h) / (end - start)
        a = snapshot_h
    else:
        remaining = need
        a = start
    if remaining <= 0:
        return None
    return (a, end, remaining)


def _capacity(it: dict, a: float) -> float:
    """Quantity of one item still unallocated for a draw starting at a:
    opening + receipts landed by a - every draw already registered
    (whatever its hours: sequential netting counts a registered block in
    full)."""
    cap = float(it["opening"])
    for r in it["receipts"]:
        if float(r["ready_h"]) <= a:
            cap += float(r["qty"])
    for d in it["draws"]:
        cap -= float(d["qty"])
    return cap


def _allocate(items: dict, g: dict, a: float, qty: float) -> list:
    """Sequential netting of one block's draw over a requirement group
    (audit stock-5): [(member, qty)] in members order, primary first.

    Each member gives what it still has unallocated at a (_capacity); an
    alternate is touched only for what the earlier members could not
    give; whatever no member can give stays on the PRIMARY, so the
    shortage shows on the curve the group is named after. A single-member
    group therefore books exactly qty on its primary (the old rule). One
    physical lot is now promised to at most one run: a group that lists
    Y as an alternate and a group whose only member is Y both see the
    same draws on Y."""
    members = g["members"]
    primary = g["primary"]
    if len(members) == 1:
        return [(primary, qty)]
    left = qty
    takes: list = []
    for m in members:
        cap = _capacity(items[m], a)
        if cap <= EPS:
            continue
        take = left if cap >= left else cap
        takes.append((m, take))
        left -= take
        if left <= EPS:
            break
    # the primary carries its own take plus the shortfall: computed as a
    # difference so the pieces always sum to qty exactly
    alt_total = 0.0
    out: list = []
    for m, take in takes:
        if m != primary:
            alt_total += take
    prim = qty - alt_total
    if prim > EPS:
        out.append((primary, prim))
    for m, take in takes:
        if m != primary and take > EPS:
            out.append((m, take))
    return out


def add_block(timelines: dict, block: dict) -> dict:
    """Register a block and append its draws to its recipe items' curves.

    A key already registered is left untouched and its entry returned:
    the key is block_id|start, so a repeat is the same board row sent
    twice, and drawing it again would fake a shortage. Build fresh
    timelines to change a block.

    Draws are netted sequentially in registration order (_allocate):
    build_timelines registers the board in (start, end, key) order, so an
    earlier run takes the shared stock first; a block added afterwards
    (a preview, a virtual block) is allocated what is left, which is the
    conservative answer for a NEW run."""
    b = dict(block)
    key = block_key(b)
    existing = timelines["blocks"].get(key)
    if existing is not None:
        return existing
    b["key"] = key
    timelines["blocks"][key] = b
    items = timelines["items"]
    for g in timelines["groups"].get(str(b.get("sku", "")), []):
        d = _block_draw(b, g["per_case"], items[g["primary"]]["snapshot_h"])
        if d is None:
            continue
        a, e, qty = d
        for m, q in _allocate(items, g, a, qty):
            items[m]["draws"].append({"key": key, "a": a, "e": e, "qty": q})
    return b


def build_timelines(blocks, sku_needs, opening, receipts, snapshot_h, *,
                    tracked, in_house) -> dict:
    """Per-item curves + per-SKU requirement groups, JSON-able (§3.6).

    Item keys from every input (recipe items and alts, opening, receipts,
    snapshot_h, tracked, in_house) go through _item first, so a stray
    space or an int code still lands on the same curve. Keys that
    collide after trimming pool: openings add, receipts concatenate, the
    first snapshot hour wins. Duplicate block keys keep the first (see
    add_block)."""
    tracked_set = {_item(x) for x in (tracked or ())} - {""}
    in_house_set = {_item(x) for x in (in_house or ())} - {""}
    opening_by: dict = {}
    for k, v in (opening or {}).items():
        k = _item(k)
        opening_by[k] = opening_by.get(k, 0.0) + (_num(v) or 0.0)
    receipts_by: dict = {}
    for k, v in (receipts or {}).items():
        receipts_by.setdefault(_item(k), []).extend(v or [])
    snapshot_by: dict = {}
    for k, v in (snapshot_h or {}).items():
        snapshot_by.setdefault(_item(k), _num(v) or 0.0)
    items: dict = {}
    groups: dict = {}

    def ensure_item(i: str) -> None:
        if i in items:
            return
        rs = [dict(r) for r in receipts_by.get(i, [])]
        rs.sort(key=lambda r: (float(r["ready_h"]), str(r.get("po8", ""))))
        items[i] = {
            "opening": opening_by.get(i, 0.0),
            "draws": [],
            "receipts": rs,
            "snapshot_h": snapshot_by.get(i, 0.0),
        }

    for sku, need in (sku_needs or {}).items():
        gl = []
        for ent in (need or {}).get("items", []):
            primary = _item(ent.get("item"))
            per_case = float(ent.get("per_case") or 0.0)
            members = [primary]
            for a in ent.get("alts", []) or []:
                a = _item(a)
                if a and a not in members:
                    members.append(a)
            # In-house intermediates (BT001/BT002) are made on demand: a
            # group touching one never gates (coverage.py precedent).
            if per_case <= 0 or any(m in in_house_set for m in members):
                continue
            gl.append({
                "primary": primary,
                "members": members,
                "per_case": per_case,
                "unit": str(ent.get("unit") or ""),
                "tracked": any(m in tracked_set for m in members),
            })
            for m in members:
                ensure_item(m)
        groups[str(sku)] = gl

    tl = {"items": items, "groups": groups, "blocks": {},
          "tracked": sorted(tracked_set), "in_house": sorted(in_house_set)}
    # Board order for the sequential netting = time order: the run that
    # starts first takes the shared stock first (ties: shorter run, then
    # key). Stable, so the input order only matters between exact twins.
    ordered = sorted(list(blocks or []),
                     key=lambda b: (_num(b.get("start_h")) or 0.0,
                                    _num(b.get("end_h")) or 0.0,
                                    block_key(b)))
    for b in ordered:
        add_block(tl, b)
    return tl


def copy_timelines(timelines: dict) -> dict:
    """Cheap copy: only draw lists and the block map are ever mutated."""
    return {
        "items": {i: {**v, "draws": list(v["draws"])}
                  for i, v in timelines["items"].items()},
        "groups": timelines["groups"],
        "blocks": dict(timelines["blocks"]),
        "tracked": timelines.get("tracked", []),
        "in_house": timelines.get("in_house", []),
    }


# --------------------------------------------------------------------------
# curve evaluation
# --------------------------------------------------------------------------

def _consumed(d: dict, t: float) -> float:
    a, e, q = d["a"], d["e"], d["qty"]
    if t <= a or e <= a:
        return 0.0
    if t >= e:
        return q
    return q * (t - a) / (e - a)


def _hard_at(opening: float, draws: list, t: float) -> float:
    v = opening
    for d in draws:
        v -= _consumed(d, t)
    return v


def _points(ws: float, we: float, draws: list, receipts: list) -> list:
    """Evaluation points (t, side): side 0 = just before t (receipts with
    ready_h < t), side 1 = at t (ready_h <= t). H is continuous so both share
    one H(t); only the receipt step differs. Draw breakpoints keep every
    segment linear; receipt hours catch the dip right before a truck."""
    ts = {ws, we}
    for d in draws:
        for x in (d["a"], d["e"]):
            if ws < x < we:
                ts.add(x)
    for r in receipts:
        rh = float(r["ready_h"])
        if ws < rh <= we:
            ts.add(rh)
    out = []
    for t in sorted(ts):
        if t > ws:
            out.append((t, 0))
        out.append((t, 1))
    return out


def _curve_min(hard_points: list, receipts: list) -> tuple[float, float | None]:
    """(min value, first hour the curve is < 0 or None) for H + steps.

    hard_points: [(t, side, H(t))] ascending. The first crossing is linearly
    interpolated inside the segment where it happens (P is linear between
    consecutive points; the before/at pair at one t is a zero-length step)."""
    best = None
    first_neg = None
    prev_t = None
    prev_v = None
    for t, side, h in hard_points:
        added = 0.0
        for r in receipts:
            rh = float(r["ready_h"])
            if rh < t or (side == 1 and rh == t):
                added += float(r["qty"])
        v = h + added
        if best is None or v < best:
            best = v
        if first_neg is None and v < -EPS:
            if prev_t is None or prev_t == t:
                first_neg = t
            elif prev_v <= 0.0:
                first_neg = prev_t
            else:
                first_neg = prev_t + (t - prev_t) * prev_v / (prev_v - v)
        prev_t, prev_v = t, v
    if best is None:
        best = 0.0
    return best, first_neg


def balance_at(timelines: dict, item: str, t: float, *, planned: bool = True
               ) -> float:
    """Item balance at hour t (with receipts landed by t when planned)."""
    it = timelines["items"][item]
    v = _hard_at(it["opening"], it["draws"], t)
    if planned:
        for r in it["receipts"]:
            if float(r["ready_h"]) <= t:
                v += float(r["qty"])
    return v


# --------------------------------------------------------------------------
# §3.3–3.4  verdict per (block, group)
# --------------------------------------------------------------------------

def _binding_dict(r: dict | None) -> dict | None:
    if r is None:
        return None
    # (2026-09-17) the buyers' PO-line comments ride on the binding so every
    # surface that names the truck can show the note beside it; "" when the
    # receipt came from the legacy workbook loader (no comment columns).
    # Never folded into `label` / verdict_text — those strings are pinned.
    return {"po8": str(r.get("po8", "")), "qty": float(r["qty"]),
            "ready_h": float(r["ready_h"]),
            "receipt_date": r.get("receipt_date"),
            "label": str(r.get("label", "")),
            "comment": str(r.get("comment") or ""),
            "comment_external": str(r.get("comment_external") or "")}


def _evaluate_group(block: dict, key: str, g: dict, timelines: dict, R: dict,
                    L: float, feed_state: str, window_end_h: float) -> dict:
    items = timelines["items"]
    start = float(block["start_h"])
    end = float(block["end_h"])
    primary = g["primary"]
    members = g["members"]
    S = items[primary]["snapshot_h"]
    ws = max(start, S)
    we = end
    opening = 0.0
    draws: list = []
    receipts: list = []
    for m in members:
        it = items[m]
        opening += it["opening"]
        draws.extend(it["draws"])
        receipts.extend(it["receipts"])
    receipts.sort(key=lambda r: (float(r["ready_h"]), str(r.get("po8", "")),
                                 float(r["qty"])))
    # The block's own draw may sit in pieces on several members after the
    # sequential netting (_allocate); its need is their sum.
    need = 0.0
    for m in members:
        for d in items[m]["draws"]:
            if d["key"] == key:
                need += float(d["qty"])

    entry = {"item": primary, "unit": g["unit"], "need": need,
             "opening_at_start": _hard_at(opening, draws, ws),
             "minH": 0.0, "minP": 0.0, "minR": 0.0,
             "verdict": VERDICT_OK, "backed": False,
             "covered_frac": 1.0, "depletion_h": None,
             "covered_frac_planned": 1.0, "depletion_planned_h": None,
             "binding": None, "lead_h": None, "safe_from_h": None,
             "dependent_frac": 0.0, "minor": False, "mid_run": False}
    if need <= 0 or we <= ws:
        # Nothing drawn (finished before the count, zero length) — the
        # group cannot fail this block whatever other blocks do to it.
        h = entry["opening_at_start"]
        entry["minH"] = entry["minP"] = entry["minR"] = h
        return entry

    pts = _points(ws, we, draws, receipts)
    hard = [(t, side, _hard_at(opening, draws, t)) for t, side in pts]
    minH, dH = _curve_min(hard, [])
    minP, dP = _curve_min(hard, receipts)
    if R.get("lead_measured_from") == "depletion":
        ref = dH if dH is not None else start
    else:
        ref = start
    reliable = [r for r in receipts if float(r["ready_h"]) <= ref - L]
    minR, _ = _curve_min(hard, reliable)

    if minH >= -EPS:
        verdict, backed = VERDICT_OK, False
    elif feed_state != "ok":
        verdict, backed = VERDICT_NO_DATA, False
    elif minR >= -EPS:
        verdict, backed = VERDICT_OK, True
    elif minP >= -EPS:
        verdict, backed = VERDICT_DEPENDENT, False
    else:
        # Audit stock-1: SHORT must be reachable. The feed's horizon
        # (window_end_h) can only excuse a crossing when the feed shows
        # inbound for this group at all (a truck counted before the block
        # ends) AND the horizon reaches into the run yet ends before the
        # crossing — "the next truck may not be in the extract yet". No
        # inbound at all, or a horizon that ends before the run even
        # starts, is a proven shortage on the data we have.
        inbound = False
        for r in receipts:
            if float(r["ready_h"]) < we:
                inbound = True
                break
        if (inbound and dP is not None and dP > window_end_h
                and window_end_h >= ws):
            verdict = VERDICT_NO_DATA
        else:
            verdict = VERDICT_SHORT
        backed = False

    # Binding receipt: latest-first, drop while the planned curve stays
    # >= 0; the first one that cannot be dropped is the truck this block
    # actually waits for (SHORT keeps the latest counted one for the text).
    counted = [r for r in receipts if float(r["ready_h"]) < we]
    kept = list(counted)
    binding = None
    for r in reversed(counted):
        trial = [x for x in kept if x is not r]
        m, _ = _curve_min(hard, trial)
        if m >= -EPS:
            kept = trial
        else:
            binding = r
            break

    # Share of the block's REMAINING draw that on-hand covers (audit
    # stock-11): the draw is linear on [ws, we], so (dH - ws) / (we - ws) is
    # both the time and the quantity fraction of what this block still has
    # to draw when the curve first dips. Measuring from `start` counted the
    # hours a running block had already produced before the stock count.
    span = we - ws
    covered = 1.0 if dH is None else _clamp01((dH - ws) / span)
    covered_p = 1.0 if dP is None else _clamp01((dP - ws) / span)
    dep_frac = _clamp01(-minH / need) if minH < 0 else 0.0
    entry.update({
        "minH": minH, "minP": minP, "minR": minR,
        "verdict": verdict, "backed": backed,
        "covered_frac": covered, "depletion_h": dH,
        "covered_frac_planned": covered_p, "depletion_planned_h": dP,
        "binding": _binding_dict(binding),
        "lead_h": (ref - float(binding["ready_h"])) if binding else None,
        "safe_from_h": (float(binding["ready_h"]) + L) if binding else None,
        "mid_run": bool(binding) and float(binding["ready_h"]) > start,
        "dependent_frac": dep_frac,
        "minor": (verdict == VERDICT_DEPENDENT
                  and dep_frac < float(R["dependent_frac_floor"])),
    })
    return entry


# --------------------------------------------------------------------------
# §3.5  block verdict
# --------------------------------------------------------------------------

def _empty_supply(key: str, verdict: str, lead_from: str) -> dict:
    return {"key": key, "verdict": verdict, "backed": False, "minor": False,
            "mid_run": False, "item": None, "covered_frac": None,
            "depletion_h": None, "lead_h": None, "safe_from_h": None,
            "binding": None, "action": "none", "lead_from": lead_from,
            "items": [], "untracked": [], "co_consumers": {}, "text": ""}


def evaluate_block(block, timelines, rules, feed_state, receipts_window_end_h
                   ) -> dict:
    """Supply verdict for one block (§3.5): the worst tracked group wins.

    action: "none" for OK; "chase_po" when the block is locked/running
    (it cannot move, someone must chase the truck); "move" otherwise. A
    block with no recipe or no numeric quantity is NO_DATA with action
    "none" ON PURPOSE: the gap is in the data, not the stock, so there is
    nothing to move or chase (grey "?"). A tracked group graded NO_DATA
    (feed missing/stale, or the crossing beyond the PO window) keeps
    move/chase_po — on-hand provably runs out, only the receipts are
    unknown."""
    R = _rules(rules)
    L = 24.0 * float(R["min_days_after_delivery"])
    # lead_from rides along so verdict_text can word the lead correctly
    # ("before start" vs "before it runs out") without the rules.
    lead_from = str(R.get("lead_measured_from") or "block_start")
    key = block_key(block)
    sku = str(block.get("sku", ""))
    groups = timelines["groups"].get(sku)
    # No recipe / unknown quantity: grey "?", never green (plan §8).
    if groups is None or not _is_num(block.get("cases")):
        sup = _empty_supply(key, VERDICT_NO_DATA, lead_from)
        sup["text"] = verdict_text(sup)
        return sup
    start = float(block["start_h"])
    end = float(block["end_h"])
    items = timelines["items"]
    entries = []
    untracked = []
    co: dict = {}
    for g in groups:
        if not g["tracked"]:
            untracked.append(g["primary"])
            continue
        entries.append(_evaluate_group(block, key, g, timelines, R, L,
                                       feed_state, receipts_window_end_h))
        ws = max(start, items[g["primary"]]["snapshot_h"])
        for m in g["members"]:
            others = [d for d in items[m]["draws"]
                      if d["key"] != key and d["a"] < end and d["e"] > ws]
            if others:
                others.sort(key=lambda d: (d["a"], d["key"]))
                co[m] = [d["key"] for d in others]

    if not entries:
        sup = _empty_supply(key, VERDICT_OK, lead_from)
        sup["untracked"] = untracked
        sup["text"] = verdict_text(sup)
        return sup

    worst = entries[0]
    for e in entries[1:]:
        if supply_rank(e) > supply_rank(worst):
            worst = e
    verdict = worst["verdict"]
    locked = bool(block.get("locked")) or bool(block.get("running"))
    if verdict == VERDICT_OK:
        action = "none"
    else:
        action = "chase_po" if locked else "move"
    sup = {
        "key": key,
        "verdict": verdict,
        "backed": worst["backed"],
        "minor": worst["minor"],
        "mid_run": worst["mid_run"],
        "item": worst["item"],
        "covered_frac": worst["covered_frac"],
        "depletion_h": (worst["depletion_planned_h"] if verdict == VERDICT_SHORT
                        else worst["depletion_h"]),
        "lead_h": worst["lead_h"],
        "safe_from_h": worst["safe_from_h"],
        "binding": worst["binding"],
        "action": action,
        "lead_from": lead_from,
        "items": entries,
        "untracked": untracked,
        "co_consumers": co,
        "text": "",
    }
    sup["text"] = verdict_text(sup)
    return sup


def evaluate_board(blocks, timelines, rules, feed_state, receipts_window_end_h
                   ) -> dict:
    out = {}
    for b in blocks or []:
        sup = evaluate_block(b, timelines, rules, feed_state,
                             receipts_window_end_h)
        out[sup["key"]] = sup
    return out


def earliest_clear_start(sku, cases, duration_h, timelines, rules, feed_state,
                         receipts_window_end_h, from_h, *, line_name="") -> dict:
    """First start >= from_h at which a virtual block reads OK (plain or
    backed). Candidates: from_h and every receipt + L of the SKU's recipe
    items — the only hours where a verdict can improve."""
    R = _rules(rules)
    L = 24.0 * float(R["min_days_after_delivery"])
    sku = str(sku)
    from_h = float(from_h)
    cands = {from_h}
    for g in timelines["groups"].get(sku, []):
        for m in g["members"]:
            for r in timelines["items"][m]["receipts"]:
                rh = float(r["ready_h"])
                if rh >= from_h - L:
                    cands.add(rh + L)
    verdict_now = None
    for c in sorted(cands):
        tl = copy_timelines(timelines)
        vb = {"key": f"virtual:{sku}@{c:.2f}", "block_id": "virtual",
              "sku": sku, "line_name": line_name, "start_h": c,
              "end_h": c + float(duration_h), "cases": cases,
              "locked": False, "running": False, "cases_left": None}
        add_block(tl, vb)
        sup = evaluate_block(vb, tl, R, feed_state, receipts_window_end_h)
        if verdict_now is None:
            verdict_now = sup
        if sup["verdict"] == VERDICT_OK:
            return {"safe_from_h": c, "verdict_now": verdict_now,
                    "verdict_at_safe": sup}
    return {"safe_from_h": None, "verdict_now": verdict_now,
            "verdict_at_safe": None}


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

def _r1(x: float) -> float:
    # floor(x*10+0.5): identical in Python and JS (round() is half-even here,
    # Math.round half-up — the text must match across the port).
    return math.floor(float(x) * 10.0 + 0.5) / 10.0


def _days(h: float) -> str:
    return f"{_r1(abs(h) / 24.0):.1f} d"


def _lead(h: float) -> str:
    # Under a day a planner thinks in hours: a truck 1 h before start must
    # not read "0.0 d". Whole hours (half-up, like _r1) below 24 h, tenths
    # of a day from there. Mirrored by stockRisk.lead / supplyGlue.leadText.
    a = abs(float(h))
    if a < 24.0:
        return f"{int(math.floor(a + 0.5))} h"
    return _days(h)


def _shown_hours(h: float) -> float:
    """The hours _lead's rounding actually displays (24 h -> 24.0,
    95 h -> 4.0 d -> 96.0)."""
    a = abs(float(h))
    if a < 24.0:
        return float(math.floor(a + 0.5))
    return _r1(a / 24.0) * 24.0


def _lead_vs_buffer(lead: float, buffer_h: float | None) -> str:
    """_lead, unless its rounding would read AT OR ABOVE the buffer while
    the lead is really under it (audit stock-13: 95 h printed "4.0 d
    before start (min 4 d)" on a DEPENDENT block). Then whole hours,
    floored, so the sentence explains the verdict: "95 h before start
    (min 4 d)"."""
    if (buffer_h is not None and 0.0 <= lead < buffer_h
            and _shown_hours(lead) >= buffer_h):
        return f"{int(math.floor(lead))} h"
    return _lead(lead)


def _pct(frac: float) -> int:
    return int(math.floor(float(frac) * 100.0 + 0.5))


def _gnum(x: float) -> str:
    r = _r1(x)
    return str(int(r)) if r == int(r) else f"{r:.1f}"


def verdict_text(supply: dict, anchor=None) -> str:
    """One-line planner sentence; real stamps when an anchor is given."""
    def stamp(h):
        if anchor is not None:
            return hour_to_stamp(h, anchor)
        return f"h{_r1(h):.1f}"

    v = supply.get("verdict")
    item = supply.get("item")
    if item is None:
        if v == VERDICT_OK:
            return "✅ supply: no tracked components to check"
        return "? supply: no recipe or quantity data"
    entry = {}
    for e in supply.get("items", []) or []:
        if e.get("item") == item:
            entry = e
            break
    d_h = entry.get("depletion_h", supply.get("depletion_h"))
    covered = supply.get("covered_frac")
    covered = 1.0 if covered is None else float(covered)
    cover = f"on hand covers {_pct(covered)}%"
    if d_h is not None:
        cover += f" (runs out {stamp(d_h)})"

    b = supply.get("binding")
    lead = supply.get("lead_h")
    safe = supply.get("safe_from_h")
    po = None
    if b and lead is not None:
        dep_mode = supply.get("lead_from") == "depletion"
        ref_word = "it runs out" if dep_mode else "start"
        mid = " (mid-run)" if supply.get("mid_run") else ""
        buffer_h = (float(safe) - float(b["ready_h"])) if safe is not None else None
        if lead >= 0:
            lead_txt = f"{_lead_vs_buffer(lead, buffer_h)} before {ref_word}{mid}"
        else:
            lead_txt = f"{_lead(lead)} after {ref_word}{mid}"
        po = f"PO {b['po8']} lands {stamp(b['ready_h'])} — {lead_txt}"
        if safe is not None:
            po += f" (min {_gnum((safe - b['ready_h']) / 24.0)} d)"

    if v == VERDICT_OK:
        if not supply.get("backed"):
            return f"✅ {item}: on hand covers the run"
        return f"🚚 {item}: {cover} · {po} · backed"
    if v == VERDICT_DEPENDENT:
        parts = [f"🚚 {item}: {cover}"]
        if po:
            parts.append(po)
        if safe is not None:
            parts.append(f"safe from {stamp(safe)}")
        if supply.get("minor"):
            parts.append("minor share")
        return " · ".join(parts)
    if v == VERDICT_SHORT:
        parts = [f"⛔ {item}: {cover}"]
        dp = supply.get("depletion_h")
        if b:
            stop = f" the line stops {stamp(dp)}" if dp is not None else ""
            parts.append(f"even with PO {b['po8']} ({stamp(b['ready_h'])})"
                         + stop)
        else:
            parts.append("no inbound counted")
        return " · ".join(parts)
    return f"? {item}: {cover} · no inbound data covering the gap"


# --------------------------------------------------------------------------
# §3.6  gate_receipts — PO lines -> Receipts
# --------------------------------------------------------------------------

# Dock-sheet time cells as receiving_import._norm_time sees them: '8AM',
# '11am', '1PM', '07:00' — plus '8:30 PM' and time/datetime objects.
_TIME_AMPM = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)$")
_TIME_24H = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")

# Every fate row carries the PoLine keys (contract §2), so a table built
# from line_fates never misses a column when a row was not even a dict.
_EMPTY_LINE = {"po": "", "po8": "", "item": "", "designation": "", "qty": None,
               "unit": "", "receipt_date": None, "initial_receipt_date": None,
               "slip_days": None, "arrival_area": "", "supplier": "",
               "supplier_id": "", "received": False, "order_date": None,
               "row": None, "cancelled": False, "status_text": "",
               # ERP line comments (order_npa.csv, 2026-09-17): the buyers'
               # internal note and the supplier-facing one, per LINE, cut at
               # 50 characters by the ERP; blank on legacy-workbook lines
               "comment": "", "comment_external": ""}


def _to_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, date):
        d = v
    else:
        dt = parse_datetime(v)
        d = dt.date() if dt else None
    # pandas NaT passes both isinstance checks and .date() hands NaT back;
    # it is the one date that is not equal to itself (receipt frames of the
    # 2026-09-15 drop carry NaT for an unparseable slip date)
    if d is not None and d != d:
        return None
    return d


def _appt_time(v) -> time | None:
    """Appointment time, or None when blank/unreadable. None sends the line
    back to the ERP rule entirely — never an appointment date with a
    guessed hour. Hours outside 1-12 (AM/PM) or 0-23, minutes > 59 are
    unreadable rather than wrapped."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        v = v.time()
    if isinstance(v, time):
        return v.replace(second=0, microsecond=0)
    s = str(v).strip().upper().replace(".", "")
    m = _TIME_AMPM.match(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= h <= 12 or mi > 59:
            return None
        return time(h % 12 + (12 if m.group(3) == "PM" else 0), mi)
    m = _TIME_24H.match(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        return time(h, mi)
    return None


def _rule_num(R: dict, key: str) -> float:
    """Numeric rule; the default covers a toml typo instead of a crash."""
    v = _num(R.get(key))
    return v if v is not None else float(DEFAULT_RULES[key])


def _area_set(v) -> set:
    if isinstance(v, str):
        v = [v]
    try:
        return {str(a).strip().upper() for a in (v or [])}
    except TypeError:
        return set()


def _match_appt(appts, po8: str, rd: date, need_cat: str | None = None
                ) -> dict | None:
    """Closest appointment for po8 within ±1 d of the receipt date; on the
    same day a timed slot beats a blank one, earlier beats later."""
    if not isinstance(appts, dict) or not po8:
        return None
    cands = appts.get(po8) or []
    if not isinstance(cands, (list, tuple)):
        return None
    best = None
    best_key = None
    for a in cands:
        if not isinstance(a, dict):
            continue
        ad = _to_date(a.get("date"))
        if ad is None or abs((ad - rd).days) > 1:
            continue
        if need_cat and need_cat not in str(a.get("category", "")).upper():
            continue
        t = _appt_time(a.get("time"))
        k = (abs((ad - rd).days), t is None,
             (t.hour, t.minute) if t is not None else (0, 0))
        if best_key is None or k < best_key:
            best, best_key = a, k
    return best


def _to_datetime(v) -> datetime | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, time(0))
    return parse_datetime(v)


def gate_receipts(lines, *, bom_items, unit_by_item, snapshot_date, today,
                  anchor, rules, stock_lots=None, appts=None,
                  source_mtime=None, as_of=None, now=None,
                  receipt_slips=None, daily_lots=None) -> tuple[dict, list, dict]:
    """(receipts_by_item, line_fates, feed_info) — see contract §3.6.

    Receipt-slip rule (drop of 2026-09-15, PKG-REC.csv): `receipt_slips` is
    {po8: [{"item", "qty", "date", "slip", "batch"}]} — the ERP's own
    packaging receipt slips for a rolling week, one entry per (slip, batch).
    A PO line whose (po8, item) has slips dated on/before `today` totalling
    >= landed_match_frac x its qty is LANDED (fate "landed", reason naming
    the slip number(s) and date(s), e.g. "receipt slip 30119536 on
    2026-09-15: 35,200 of 35,200" — plural only when several DISTINCT slip
    numbers were consumed). A slip is stronger evidence than a batch-dated
    lot, so this check runs BEFORE the lot rule below — and before the
    overdue rule (2026-09-16: a line the dock already booked is landed,
    not overdue) — and, like lots, slip qty is CONSUMED by the lines it
    lands (two lines of one item on one PO share the PO's slips). A slip
    matches the line when its item equals the line's resolved item key or
    its raw code. Slips dated after `today` are ignored (not evidence yet).
    Only a slip dated BEFORE `snapshot_date` is evidence by its date alone;
    one dated ON or after it is evidence only when its batch is among the
    item's lots (stock_lots / daily_lots) — the lot export runs mid-day
    (14:05), and on the real drop 25 of the 34 slips of the snapshot day
    had batches in no lot (2026-09-16). A slip that is not evidence is a
    delivery booked AFTER the stock count: when such "late" slips, with
    any evidence slips of the line, total >= landed_match_frac x qty — or
    the line's receipt date is before the snapshot (else overdue, never
    counted) — the line is counted as a receipt (fate "used", tier "erp")
    ready on the latest late slip's date @ receipt_ready_hour (+ the raw
    QC offset on raw areas), of qty - the evidence part when covered, else
    of the late qty only (the unbooked rest stays overdue); the reason
    names the slip(s) "booked after the stock export". Late slips consume
    their qty but no lots (their goods are in none); evidence slips of a
    counted line consume theirs as when landing. A partial late booking
    on a line still due (receipt date on/after the snapshot) and any
    offsite-area line take the normal path. feed_info["n_counted_by_slip"]
    counts the lines counted this way.
    Landing by slip also CONSUMES the lots that truck became (2026-09-16):
    the landed qty is taken from the item's lot pool, the slip's own batch
    first, then lots dated slip date - 3 d .. slip date + 1 d, earliest
    first — one physical lot never lands a second PO line by the lot rule.
    Malformed slips (not a dict, unreadable date, non-numeric qty), a
    non-list PO entry or a non-dict `receipt_slips` are noted in
    feed_info["errors"] and skipped — never raised.
    feed_info["n_landed_by_slip"] counts the lines the rule landed.

    Freshness (audit stock-6): TWO rules, and feed_info["stale_reason"]
    lists which fired. "content": the latest receipt date in the extract
    is before `today` (nothing in it still looks ahead). "file_age": the
    extract itself is older than rules["po_ignore_after_h"] — measured
    from its own as-of date (`as_of`, 00:00 of that day) when it carries
    one, else from the file's `source_mtime`; feed_info["age_basis"] says
    which ("as_of" / "mtime" / None when neither is known), and
    feed_info["source_age_h"] the age. Ages are measured to `now`
    (datetime), defaulting to 00:00 of `today`.

    Landed rule (audit stock-7): a lot proves at most one delivery. Lots
    are pooled once per item and CONSUMED by each PO line they land, and
    only lots dated receipt_date - 3 d .. receipt_date + 1 d count as
    "that truck came early".

    Daily lots (2026-09-16): `daily_lots` has stock_lots' shape and holds
    the lots of a daily-delivery export (fresh apples, jestksav.csv). A
    lot dated near a FUTURE line is another day's truck, so a daily lot
    lands only a line whose receipt date is on/before `snapshot_date`
    (with the overdue rule: the snapshot day itself), and only when the
    lot is dated that same receipt date. It counts toward the slip rule's
    batch check like any lot. A non-dict `daily_lots` is noted and
    skipped.

    Appointment tier (audit stock-9): a dock appointment dated BEFORE the
    ERP receipt date cannot pull the delivery forward — the ready hour is
    clamped to the ERP rule (receipt date @ receipt_ready_hour); same-day
    and later appointments time the receipt as before.

    A line flagged `cancelled` (deleted in the ERP, line status 70 on the
    order_npa.csv export) gets fate "cancelled" before any other check:
    it is never a receipt, whatever quantity still "remains" on it. A
    `received` line's reason says "archived in the ERP" when the export
    marked it archived (status 60), else "nothing left to receive".

    Never raises. A line that is not a dict gets fate "bad_row"; a stock
    lot whose qty is not numeric counts as 0 (None/blank silently, garbage
    noted); malformed lots, non-dict stock_lots and unusable rule values
    are noted in feed_info["errors"] (unique messages) and skipped. Every
    input line gets exactly one fate; only "used" (incl. the
    landed_unverifiable variant) lines become receipts.

    ready_h: a joined appointment with a readable time gives tier "appt"
    (appointment time + appt_ready_offset_h). An appointment with a blank
    or unreadable time falls back to the ERP rule entirely (tier "erp",
    receipt date @ receipt_ready_hour) — never a hybrid. Raw areas add
    raw_qc_offset_h to either.

    feed_info["receipts_window_end_h"] = max(latest used ready_h, 23:59 of
    the latest parseable receipt date across ALL lines): a fresh extract
    with no usable line still bounds the NO_DATA window by its last date.
    0.0 when nothing is parseable."""
    R = _rules(rules)
    feed = {"state": "ok", "receipts_window_end_h": 0.0, "n_used": 0,
            "n_lines": 0, "max_receipt_date": None, "fates": {}, "errors": [],
            "stale_reason": [], "source_age_h": None, "age_basis": None,
            "n_landed_by_slip": 0, "n_counted_by_slip": 0}
    errors: list = feed["errors"]

    def note(msg: str) -> None:
        if msg not in errors:
            errors.append(msg)

    if lines is None:
        feed["state"] = "missing"
        return {}, [], feed
    if isinstance(lines, (str, bytes)):
        lines = None
    else:
        try:
            lines = list(lines)
        except TypeError:
            lines = None
    if lines is None:
        note("lines is not a list of PO line dicts")
        feed["state"] = "missing"
        return {}, [], feed
    feed["n_lines"] = len(lines)
    if not lines:
        feed["state"] = "empty"
        return {}, [], feed

    try:
        bom = {str(x) for x in (bom_items or ())}
    except TypeError:
        note("bom_items is not iterable; every line is unjoinable")
        bom = set()
    if not isinstance(unit_by_item, dict):
        unit_by_item = {}
    if stock_lots is not None and not isinstance(stock_lots, dict):
        note(f"stock_lots is {type(stock_lots).__name__}, not a dict; "
             f"landed rule skipped")
        stock_lots = None
    if daily_lots is not None and not isinstance(daily_lots, dict):
        note(f"daily_lots is {type(daily_lots).__name__}, not a dict; "
             f"daily lots skipped")
        daily_lots = None
    have_lots = stock_lots is not None or daily_lots is not None
    if receipt_slips is not None and not isinstance(receipt_slips, dict):
        note(f"receipt_slips is {type(receipt_slips).__name__}, not a dict; "
             f"receipt-slip rule skipped")
        receipt_slips = None
    snap_d = _to_date(snapshot_date)
    today_d = _to_date(today)
    raw_areas = _area_set(R.get("raw_areas"))
    offsite = _area_set(R.get("offsite_areas"))
    frac = _rule_num(R, "landed_match_frac")
    ready_hour = int(_rule_num(R, "receipt_ready_hour")) % 24
    raw_off = _rule_num(R, "raw_qc_offset_h")
    appt_off = _rule_num(R, "appt_ready_offset_h")
    receipts: dict = {}
    fates: list = []
    max_date = None
    window_end = None

    # Landed-rule lot pools, built once per item: [[batch date, qty left,
    # batch, daily]] in date order (mutable: a landed line consumes its
    # qty; daily = from daily_lots, 2026-09-16), plus flags: whether the
    # item had regular / daily lots at all and whether any of them carried
    # a decodable date. lot_batches[key] = every batch of the item's lots,
    # dated or not (the slip rule's "already counted" check, 2026-09-16).
    pools: dict = {}
    lot_batches: dict = {}

    def lot_pool(key: str) -> tuple[list, dict]:
        if key in pools:
            return pools[key]
        dated = []
        batches: set = set()
        flags = {"reg": False, "reg_dated": False,
                 "daily": False, "daily_dated": False}
        for src, is_daily in ((stock_lots, False), (daily_lots, True)):
            if src is None:
                continue
            name = "daily_lots" if is_daily else "stock_lots"
            lots = src.get(key) or []
            if not isinstance(lots, (list, tuple)):
                note(f"{key}: {name} entry is {type(lots).__name__}, "
                     f"not a list; landed rule skipped")
                lots = []
            tag = "daily" if is_daily else "reg"
            flags[tag] = flags[tag] or bool(lots)
            for lot in lots:
                try:
                    batch, q = lot
                except (TypeError, ValueError):
                    note(f"{key}: lot {lot!r} is not (batch, qty); ignored")
                    continue
                qn = _num(q)
                if qn is None:
                    if q is not None and str(q).strip():
                        note(f"{key}: lot {batch!r} qty {q!r} not numeric; "
                             f"treated as 0")
                    qn = 0.0
                bk = _batch_key(batch)
                if bk:
                    batches.add(bk)
                d = decode_batch_date(batch)
                if d is not None:
                    dated.append([d, qn, bk, is_daily])
                    flags[tag + "_dated"] = True
        dated.sort(key=lambda t: t[0])
        pools[key] = (dated, flags)
        lot_batches[key] = batches
        return pools[key]

    def slip_is_evidence(s: list, key: str) -> bool:
        """(2026-09-16) A slip dated BEFORE the stock snapshot date is taken
        as in the counted on-hand; one dated ON or after it only when its
        batch already sits in the item's lots. The lot export runs mid-day
        (14:05 on the drop of 2026-09-15), so a slip of the snapshot day
        may be booked after it — 25 of that day's 34 slips were in no lot —
        and must then stay a receipt."""
        if snap_d is None or s[0] < snap_d:
            return True
        if not have_lots or not s[4]:
            return False
        lot_pool(key)
        return s[4] in lot_batches.get(key, set())

    def consume_lots_for_slip(key: str, s: list, take: float) -> None:
        """(2026-09-16) The slip's truck IS a lot now: take the landed qty
        from the item's lot pool — the slip's batch first, then lots dated
        slip date - 3 d .. + 1 d, earliest first — so the same physical
        lot cannot land a second PO line by the lot rule."""
        if not have_lots or take <= 0:
            return
        pool, _flags = lot_pool(key)
        left = take
        if s[4]:
            for lot in pool:
                if left <= 0:
                    return
                if lot[2] == s[4] and lot[1] > 0:
                    t = lot[1] if lot[1] < left else left
                    lot[1] -= t
                    left -= t
        lo = s[0] - timedelta(days=3)
        hi = s[0] + timedelta(days=1)
        for lot in pool:                  # date-sorted: earliest first
            if left <= 0:
                return
            if lo <= lot[0] <= hi and lot[1] > 0:
                t = lot[1] if lot[1] < left else left
                lot[1] -= t
                left -= t

    # Receipt-slip pools per PO, built once: [[date, qty left, slip no,
    # item, batch]] in date order (mutable: a landed line consumes its
    # qty). Only slips dated on/before today are evidence; malformed ones
    # are noted.
    slip_pools: dict = {}

    def slip_pool(po8: str) -> list:
        if po8 in slip_pools:
            return slip_pools[po8]
        raw = receipt_slips.get(po8) or []
        if not isinstance(raw, (list, tuple)):
            note(f"PO {po8}: receipt_slips entry is {type(raw).__name__}, "
                 f"not a list; ignored")
            raw = []
        out: list = []
        for s in raw:
            if not isinstance(s, dict):
                note(f"PO {po8}: receipt slip {s!r} is not a dict; ignored")
                continue
            slip_no = str(s.get("slip") or "").strip() or "?"
            d = _to_date(s.get("date"))
            if d is None:
                note(f"PO {po8}: receipt slip {slip_no} date "
                     f"{s.get('date')!r} unreadable; ignored")
                continue
            if today_d is not None and d > today_d:
                continue            # dated in the future: not received yet
            q = _num(s.get("qty"))
            if q is None:
                if s.get("qty") is not None and str(s.get("qty")).strip():
                    note(f"PO {po8}: receipt slip {slip_no} qty "
                         f"{s.get('qty')!r} not numeric; ignored")
                continue
            if q <= 0:
                continue
            out.append([d, q, slip_no, _item(s.get("item")),
                        _batch_key(s.get("batch"))])
        out.sort(key=lambda t: (t[0], t[2]))
        slip_pools[po8] = out
        return out

    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            kind = type(line).__name__
            fates.append({**_EMPTY_LINE, "fate": "bad_row",
                          "reason": f"lines[{i}] is {kind}, not a dict",
                          "ready_h": None, "tier": None, "item_key": None})
            note(f"lines[{i}]: {kind} instead of a PO line dict")
            continue
        out = {**line, "fate": None, "reason": "", "ready_h": None,
               "tier": None, "item_key": None}
        fates.append(out)
        rd = _to_date(line.get("receipt_date"))
        if rd is not None and (max_date is None or rd > max_date):
            max_date = rd
        code = str(line.get("item") or "").strip()
        if line.get("cancelled"):
            out.update(fate="cancelled", reason="deleted in the ERP (line status 70)")
            continue
        if line.get("received"):
            out.update(fate="received",
                       reason="archived in the ERP"
                       if str(line.get("status_text") or "") == "archived"
                       else "nothing left to receive")
            continue
        key = resolve_item_key(code, bom) if code else None
        if key is None:
            out.update(fate="unjoinable", reason=f"item {code or '?'} not in BOM")
            continue
        out["item_key"] = key
        unit = str(line.get("unit") or "").strip()
        bom_unit = str(unit_by_item.get(key) or "").strip()
        if bom_unit and unit.upper() != bom_unit.upper():
            out.update(fate="unit_mismatch",
                       reason=f"PO unit {unit or '?'} vs BOM {bom_unit}")
            continue
        qty = _num(line.get("qty"))
        if qty is None or qty <= 0:
            out.update(fate="bad_qty", reason=f"qty {line.get('qty')!r}")
            continue
        if rd is None:
            out.update(fate="bad_date",
                       reason=f"receipt date {line.get('receipt_date')!r}")
            continue
        po8 = str(line.get("po8") or "").strip()
        # Receipt-slip rule (2026-09-15): the ERP booked the receipt itself —
        # stronger than a batch-dated lot, so it is checked first. Since
        # 2026-09-16 it also runs BEFORE the overdue rule (14 slip-covered
        # lines read "overdue" on the real drop), a slip after the stock
        # snapshot (or ON its day, 2026-09-16 second pass) counts only when
        # its batch is already in the lots, and the landed qty is consumed
        # from the lots too (one physical lot never lands a second line).
        # Slips booked after the stock count ("late") make the line a
        # receipt on the slip date instead of landed / overdue.
        area = str(line.get("arrival_area") or "").strip().upper()
        if receipt_slips is not None and po8:
            cands = [s for s in slip_pool(po8)
                     if s[1] > 0 and s[3] in (key, code)]
            evid = [s for s in cands if slip_is_evidence(s, key)]
            late = [s for s in cands if not slip_is_evidence(s, key)]
            m_evid = sum(s[1] for s in evid)
            m_late = sum(s[1] for s in late)

            def take_slips(pool_: list, left_: float, lots_too: bool):
                taken_: list = []
                for s in pool_:          # consume, earliest slip first
                    if left_ <= 0:
                        break
                    take = s[1] if s[1] < left_ else left_
                    s[1] -= take
                    left_ -= take
                    taken_.append(s)
                    if lots_too:
                        consume_lots_for_slip(key, s, take)
                return taken_, left_

            def slip_words(taken_: list) -> str:
                # plural only for several DISTINCT slip numbers: PKG-REC
                # prints one row per (slip, batch) (2026-09-16)
                slip_nos = sorted({s[2] for s in taken_})
                days = sorted({s[0] for s in taken_})
                when = (days[0].isoformat() if len(days) == 1
                        else f"{days[0].isoformat()}..{days[-1].isoformat()}")
                return (f"receipt slip{'s' if len(slip_nos) > 1 else ''} "
                        f"{', '.join(slip_nos)} on {when}")

            if evid and m_evid >= frac * qty:
                taken, left = take_slips(evid, qty, True)
                got = qty - left if left > 0 else qty
                out.update(fate="landed",
                           reason=f"{slip_words(taken)}: {got:,.0f} of {qty:,.0f}")
                feed["n_landed_by_slip"] += 1
                continue
            covered = m_evid + m_late >= frac * qty
            if (late and area not in offsite
                    and (covered or (snap_d is not None and rd < snap_d))):
                # booked after the stock count (2026-09-16): the goods are
                # in no lot, so count them — on the slip date, not the
                # (often earlier) ERP date, and never as overdue
                _e_taken, e_left = take_slips(evid, qty, True)
                e_got = qty - e_left
                l_taken, l_left = take_slips(late, qty - e_got, False)
                l_got = (qty - e_got) - l_left
                full = e_got + l_got >= frac * qty
                n_qty = qty - e_got if full else l_got
                last = max(s[0] for s in l_taken)
                ready_dt = datetime.combine(last, time(ready_hour))
                if area in raw_areas:
                    ready_dt += timedelta(hours=raw_off)
                ready_h = float(datetime_to_hour(ready_dt, anchor))
                why = (f"counted; {slip_words(l_taken)} booked after the stock "
                       f"export: {l_got:,.0f} of {qty:,.0f}")
                if e_got > 0:
                    why += f" ({e_got:,.0f} already on hand)"
                if not full:
                    why += "; the rest is overdue"
                out.update(fate="used", reason=why, ready_h=ready_h, tier="erp")
                receipts.setdefault(key, []).append({
                    "ready_h": ready_h, "qty": n_qty, "po8": po8, "tier": "erp",
                    "receipt_date": last.isoformat(),
                    "label": (f"PO {po8} · {key} · {n_qty:,.0f} {unit} · "
                              f"{last.isoformat()} · receipt slip"),
                    "comment": str(line.get("comment") or ""),
                    "comment_external": str(line.get("comment_external") or "")})
                window_end = (ready_h if window_end is None
                              else max(window_end, ready_h))
                feed["n_counted_by_slip"] += 1
                continue
        if snap_d is not None and rd < snap_d:
            out.update(fate="overdue",
                       reason=f"receipt date {rd.isoformat()} before stock "
                              f"export {snap_d.isoformat()}")
            continue
        # Landed rule: lots dated near the receipt date already on hand mean
        # the truck came early — counting it again is the false-comfort
        # direction. Undecodable batches can only be flagged, not proven.
        unverifiable = False
        if have_lots:
            pool, fl = lot_pool(key)
            # daily lots (fresh apples) prove only a truck of the snapshot
            # day or earlier (2026-09-16)
            daily_ok = snap_d is not None and rd <= snap_d
            had_lots = fl["reg"] or (daily_ok and fl["daily"])
            had_dated = fl["reg_dated"] or (daily_ok and fl["daily_dated"])
            if had_lots and not had_dated:
                unverifiable = True
            elif had_lots:
                # a lot dated in the window AROUND the receipt date is this
                # truck; one dated months later is another delivery. A daily
                # lot must be dated the receipt date itself.
                lo = rd - timedelta(days=3)
                hi = rd + timedelta(days=1)
                cands = [lot for lot in pool if lot[1] > 0 and (
                    (daily_ok and lot[0] == rd) if lot[3]
                    else lo <= lot[0] <= hi)]
                matched = sum(lot[1] for lot in cands)
                if matched >= frac * qty:
                    left = qty
                    for lot in cands:        # consume, earliest lot first
                        take = lot[1] if lot[1] < left else left
                        lot[1] -= take
                        left -= take
                        if left <= 0:
                            break
                    span = (f"{lo.isoformat()}..{hi.isoformat()}"
                            if any(not lot[3] for lot in cands)
                            else rd.isoformat())
                    out.update(fate="landed",
                               reason=f"{matched:g} of {qty:g} already in "
                                      f"lots dated {span}")
                    continue
        if area in offsite:
            appt = _match_appt(appts, po8, rd, need_cat="SL3")
            if appt is None:
                out.update(fate="offsite_no_transfer",
                           reason=f"{area} line without a joined SL3 TRANSFER "
                                  f"appointment")
                continue
        else:
            appt = _match_appt(appts, po8, rd)
        appt_t = _appt_time(appt.get("time")) if appt is not None else None
        if appt is not None and appt_t is not None:
            ad = _to_date(appt.get("date"))
            ready_dt = datetime.combine(ad, appt_t) + timedelta(hours=appt_off)
            tier = "appt"
            why = "counted"
            erp_dt = datetime.combine(rd, time(ready_hour))
            if ad < rd and ready_dt < erp_dt:
                # the ERP date is the contractual one: a booking on the day
                # before cannot credit the material a day early
                ready_dt = erp_dt
                why = (f"counted; appointment {ad.isoformat()} precedes the "
                       f"ERP receipt date, clamped to {rd.isoformat()} "
                       f"{ready_hour:02d}:00")
        else:
            ready_dt = datetime.combine(rd, time(ready_hour))
            tier = "erp"
            why = ("counted; appointment time unreadable, ERP date used"
                   if appt is not None else "counted")
        if area in raw_areas:
            ready_dt += timedelta(hours=raw_off)
        ready_h = float(datetime_to_hour(ready_dt, anchor))
        label = (f"PO {po8} · {key} · {qty:,.0f} {unit} · {rd.isoformat()} · "
                 f"{'appt' if tier == 'appt' else 'ERP date'}")
        out.update(fate="landed_unverifiable" if unverifiable else "used",
                   reason=("lots have no decodable batch date"
                           if unverifiable else why),
                   ready_h=ready_h, tier=tier)
        receipts.setdefault(key, []).append({
            "ready_h": ready_h, "qty": qty, "po8": po8, "tier": tier,
            "receipt_date": rd.isoformat(), "label": label,
            # the buyers' line comments travel with the receipt (2026-09-17)
            # so the Gantt and the binding can show them; never in `label`
            "comment": str(line.get("comment") or ""),
            "comment_external": str(line.get("comment_external") or "")})
        window_end = ready_h if window_end is None else max(window_end, ready_h)

    for k in receipts:
        receipts[k].sort(key=lambda r: (r["ready_h"], r["po8"]))
    counts: dict = {}
    for f in fates:
        counts[f["fate"]] = counts.get(f["fate"], 0) + 1
    feed["fates"] = counts
    feed["n_used"] = counts.get("used", 0) + counts.get("landed_unverifiable", 0)
    # The extract's last receipt date vouches for that whole day even when
    # no line survived the gate: nothing is booked after it.
    if max_date is not None:
        eod = float(datetime_to_hour(datetime.combine(max_date, time(23, 59)),
                                     anchor))
        window_end = eod if window_end is None else max(window_end, eod)
    feed["receipts_window_end_h"] = window_end if window_end is not None else 0.0
    feed["max_receipt_date"] = max_date.isoformat() if max_date else None
    # Freshness rule 1 — content: does the extract still look ahead?
    reasons: list = feed["stale_reason"]
    if max_date is None or (today_d is not None and max_date < today_d):
        reasons.append("content")
    # Freshness rule 2 — file age (po_ignore_after_h): an extract older than
    # a week is not trusted whatever dates it happens to contain. The
    # bridge copies only on a content change (copy2 keeps the source mtime),
    # so the mtime is the extract's age when no as-of cell exists.
    ref_now = _to_datetime(now)
    if ref_now is None and today_d is not None:
        ref_now = datetime.combine(today_d, time(0))
    as_of_d = _to_date(as_of)
    basis_dt = None
    if as_of_d is not None:
        basis_dt = datetime.combine(as_of_d, time(0))
        feed["age_basis"] = "as_of"
    else:
        basis_dt = _to_datetime(source_mtime)
        if basis_dt is not None:
            feed["age_basis"] = "mtime"
    if basis_dt is not None and ref_now is not None:
        age_h = (ref_now - basis_dt).total_seconds() / 3600.0
        feed["source_age_h"] = age_h
        if age_h > _rule_num(R, "po_ignore_after_h"):
            reasons.append("file_age")
    if reasons:
        feed["state"] = "stale"
    return receipts, fates, feed
