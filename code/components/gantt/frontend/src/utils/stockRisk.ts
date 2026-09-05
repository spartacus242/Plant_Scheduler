// stockRisk.ts — time-phased component supply verdicts (client twin).
//
// 1:1 port of code/stockcheck/timeline.py (contract 2026-09-01 §3/§5): one
// consumption curve per item, gated PO receipts as steps, every (block,
// requirement group) graded OK / DEPENDENT / SHORT / NO_DATA. Python is the
// source of truth: same names, same snake_case fields inside the returned
// objects, same EPS, and the SAME operation order in every float sum so
// both sides land on the same bits. No imports — tests/test_stock_risk_parity
// compiles this file alone with the frontend's tsc and replays
// data/test_fixtures/stock_risk/cases.json under node.
//
// Hours are floats in the page's anchor frame. verdictText() stamps them as
// `h95.3` unless an anchor is given, in which case it formats like
// helpers.timefmt.hour_to_stamp ("Wed 9/2 16:00", naive arithmetic, real
// minutes) — deliberately NOT layout.hourToStamp, which prints ":00" and
// walks local time across DST.
//
// Item codes go through itemKey (timeline._item: str + trim) so a stray
// space in a payload key lands on the same curve on both sides, and a
// block key already registered is never drawn twice (timeline.add_block).

export type Verdict = "OK" | "DEPENDENT" | "SHORT" | "NO_DATA";
export type FeedState = "ok" | "missing" | "stale" | "empty" | string;

/** Rule keys mirror helpers.config.stock_config (§1): a rules dict from the
 * toml (or StockArgs.rules, a 4-key subset) can be passed straight through;
 * missing keys take the defaults. */
export interface Rules {
  min_days_after_delivery: number;
  lead_measured_from: "block_start" | "depletion" | string;
  receipt_ready_hour: number;
  raw_qc_offset_h: number;
  appt_ready_offset_h: number;
  po_ignore_after_h: number;
  landed_match_frac: number;
  dependent_frac_floor: number;
  hard_block: boolean;
  use_board_qty_kg: boolean;
  raw_areas: string[];
  offsite_areas: string[];
}

export const DEFAULT_RULES: Rules = {
  min_days_after_delivery: 4,
  lead_measured_from: "block_start",
  receipt_ready_hour: 16,
  raw_qc_offset_h: 72,
  appt_ready_offset_h: 2,
  po_ignore_after_h: 168,
  landed_match_frac: 0.95,
  dependent_frac_floor: 0.05,
  hard_block: false,
  use_board_qty_kg: true,
  raw_areas: ["RB1", "AMB", "RC1"],
  offsite_areas: ["SL3"],
};

/** Balances within EPS of zero count as covered: draws that exactly exhaust
 * the opening must not flip a block on float noise. */
export const EPS = 1e-6;

export interface TimelineBlock {
  /** Board key; split MOs share block_id, so the start is part of it.
   * Filled in by addBlock when absent (`${block_id}|${String(start_h)}` —
   * blockIdentity.blockKey, at full precision; fix FE / audit stock-14). */
  key?: string | null;
  block_id: string;
  sku: string;
  line_name?: string;
  start_h: number;
  end_h: number;
  /** null/unknown -> NO_DATA (grey "?", never green). */
  cases: number | null;
  locked?: boolean;
  running?: boolean;
  /** manprg's remaining cases for a running MO; null -> uniform tail. */
  cases_left?: number | null;
}

export interface SkuNeedItem {
  item: string;
  /** Need per ONE case (BomGraph.explode(sku, 1.0)); <= 0 -> no group. */
  per_case: number;
  unit: string;
  alts: string[];
}
export interface SkuNeed { kg_per_case: number; items: SkuNeedItem[] }
export type SkuNeeds = Record<string, SkuNeed>;

/** An ALREADY gated receipt (report `inbound.receipts` / StockArgs.receipts). */
export interface Receipt {
  ready_h: number;
  qty: number;
  po8: string;
  tier: "erp" | "appt" | string;
  receipt_date: string | null;
  label: string;
}

export interface Draw { key: string; a: number; e: number; qty: number }
export interface ItemTimeline {
  opening: number;
  draws: Draw[];
  receipts: Receipt[];
  snapshot_h: number;
}
export interface Group {
  primary: string;
  members: string[];
  per_case: number;
  unit: string;
  tracked: boolean;
}
export interface Timelines {
  items: Record<string, ItemTimeline>;
  groups: Record<string, Group[]>;
  blocks: Record<string, TimelineBlock>;
  tracked: string[];
  in_house: string[];
}

export interface Binding {
  po8: string;
  qty: number;
  ready_h: number;
  receipt_date: string | null;
  label: string;
}

export interface SupplyItem {
  item: string;
  unit: string;
  need: number;
  opening_at_start: number;
  minH: number;
  minP: number;
  minR: number;
  verdict: Verdict;
  backed: boolean;
  covered_frac: number;
  depletion_h: number | null;
  covered_frac_planned: number;
  depletion_planned_h: number | null;
  binding: Binding | null;
  lead_h: number | null;
  safe_from_h: number | null;
  dependent_frac: number;
  minor: boolean;
  mid_run: boolean;
}

export interface Supply {
  key: string;
  verdict: Verdict;
  backed: boolean;
  minor: boolean;
  mid_run: boolean;
  /** The worst group's primary item. */
  item: string | null;
  covered_frac: number | null;
  /** d_H of the worst group, or d_P when SHORT. */
  depletion_h: number | null;
  lead_h: number | null;
  safe_from_h: number | null;
  binding: Binding | null;
  action: "none" | "move" | "chase_po";
  lead_from: string;
  items: SupplyItem[];
  untracked: string[];
  co_consumers: Record<string, string[]>;
  text: string;
}

export interface EarliestClearStart {
  safe_from_h: number | null;
  verdict_now: Supply | null;
  verdict_at_safe: Supply | null;
}

/** Planning anchor for stamps: a Date (its local wall clock) or a string
 * like "2026-08-31", "2026-08-31 00:00[:00]" or "2026-08-31T00:00:00". */
export type Anchor = Date | string;

const VERDICT_OK: Verdict = "OK";
const VERDICT_DEPENDENT: Verdict = "DEPENDENT";
const VERDICT_SHORT: Verdict = "SHORT";
const VERDICT_NO_DATA: Verdict = "NO_DATA";

// --------------------------------------------------------------------------
// small helpers
// --------------------------------------------------------------------------

function mergeRules(rules?: Partial<Rules> | null): Rules {
  return { ...DEFAULT_RULES, ...(rules ?? {}) };
}

function isNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

function clamp01(x: number): number {
  return x < 0 ? 0 : x > 1 ? 1 : x;
}

/** timeline._item: canonical item key, a stripped str, so "754751 " and
 * 754751 pool as one item. Only trims, never reformats a code. */
function itemKey(x: unknown): string {
  return x == null ? "" : String(x).trim();
}

// Python float() literal grammar (sign, digits with single underscores
// between them, optional fraction and exponent); inf/nan fail on purpose.
const PY_FLOAT = /^[+-]?(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|\.\d(?:_?\d)*)(?:[eE][+-]?\d(?:_?\d)*)?$/;

/** timeline._num: lenient number. Numbers pass through, strings may carry
 * commas; bools, blanks, garbage, objects and non-finite values -> null. */
function pyNum(v: unknown): number | null {
  if (v == null || typeof v === "boolean") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v !== "string") return null;
  const s = v.trim().replace(/,/g, "");
  if (!PY_FLOAT.test(s)) return null;
  const f = Number(s.replace(/_/g, ""));
  return Number.isFinite(f) ? f : null;
}

/** Python `_num(v) or 0.0`: null and (-)0 both read as 0. */
function numOr0(v: unknown): number {
  const n = pyNum(v);
  return n === null || n === 0 ? 0 : n;
}

/** Own-property lookup: item codes / SKUs are arbitrary strings and must
 * never fall through to Object.prototype ("constructor"). */
function own<T>(o: Record<string, T> | null | undefined, k: string): T | undefined {
  return o != null && Object.prototype.hasOwnProperty.call(o, k) ? o[k] : undefined;
}

const cmpNum = (a: number, b: number): number => (a < b ? -1 : a > b ? 1 : 0);
const cmpStr = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/** Python's f"{x:.{d}f}": correctly rounded, ties to even. JS toFixed rounds
 * an exact tie away from zero (0.125 -> "0.13", Python "0.12"); an exact tie
 * at d decimals is precisely x = j / 2^(d+1) with j odd, so test that. */
function pyFixed(x: number, d: number): string {
  const p = 2 ** d;
  if (Number.isFinite(x) && Number.isInteger(x * p * 2) && !Number.isInteger(x * p)) {
    const lo = Math.floor(Math.abs(x) * 10 ** d); // exact: a half-integer
    const n = lo % 2 === 0 ? lo : lo + 1;
    const s = String(n).padStart(d + 1, "0");
    const body = d ? `${s.slice(0, -d)}.${s.slice(-d)}` : s;
    return (x < 0 ? "-" : "") + body;
  }
  return x.toFixed(d);
}

/** Board key `block_id|start_h` — the Gantt's blockIdentity.blockKey at full
 * precision (timeline.block_key / _js_num print JS `String(number)` form on
 * the Python side). The old `@start.toFixed(2)` collapsed two rows of one
 * MO less than 0.005 h apart into one key and addBlock silently dropped the
 * second draw (fix FE, audit stock-14 / K-8). An explicit `key` wins. */
export function blockKey(block: { key?: string | null; block_id?: string; start_h?: number }): string {
  const k = block.key;
  if (k) return String(k);
  const s = Number(block.start_h ?? 0);
  return `${block.block_id ?? ""}|${String(s === 0 ? 0 : s)}`;
}

/** SHORT 4 > DEPENDENT 3 > NO_DATA 2 > OK backed 1 > OK 0; minor DEPENDENT
 * ranks with backed (grey chip). Works on a Supply or a SupplyItem. */
export function supplyRank(supply: { verdict?: string; minor?: boolean; backed?: boolean }): number {
  const v = supply.verdict;
  if (v === VERDICT_SHORT) return 4;
  if (v === VERDICT_DEPENDENT) return supply.minor ? 1 : 3;
  if (v === VERDICT_NO_DATA) return 2;
  return supply.backed ? 1 : 0;
}

// --------------------------------------------------------------------------
// §3.1–3.2  timelines and the draw model
// --------------------------------------------------------------------------

/** [a, e, qty] the block draws of one item, or null when it draws nothing.
 * Stock counted at S already reflects consumption up to S, so a running
 * block only draws what is left: cases_left when manprg knows it, else the
 * uniform tail of the board quantity. */
function blockDraw(block: TimelineBlock, perCase: number, snapshotH: number)
  : [number, number, number] | null {
  const start = Number(block.start_h);
  const end = Number(block.end_h);
  const cases = block.cases;
  if (!isNum(cases) || cases <= 0 || perCase <= 0) return null;
  const need = cases * perCase;
  if (end <= snapshotH || end <= start) return null;
  let remaining: number;
  let a: number;
  if (start < snapshotH) {
    const cl = block.cases_left;
    if (isNum(cl)) remaining = cl * perCase;
    else remaining = need * (end - snapshotH) / (end - start);
    a = snapshotH;
  } else {
    remaining = need;
    a = start;
  }
  if (remaining <= 0) return null;
  return [a, end, remaining];
}

/** timeline._capacity: quantity of one item still unallocated for a draw
 * starting at a — opening + receipts landed by a - every draw already
 * registered (whatever its hours: sequential netting counts a registered
 * block in full). */
function capacity(it: ItemTimeline, a: number): number {
  let cap = Number(it.opening);
  for (const r of it.receipts) {
    if (Number(r.ready_h) <= a) cap += Number(r.qty);
  }
  for (const d of it.draws) cap -= Number(d.qty);
  return cap;
}

/** timeline._allocate (fix FE port of K-3 / audit stock-5): sequential
 * netting of one block's draw over a requirement group -> [member, qty]
 * in members order, primary first. Each member gives what it still has
 * unallocated at a; an alternate is touched only for what the earlier
 * members could not give; whatever no member can give stays on the
 * PRIMARY (the shortage shows on the curve the group is named after). A
 * single-member group books exactly qty on its primary (the old rule).
 * One physical lot is now promised to at most one run. */
function allocate(items: Record<string, ItemTimeline>, g: Group, a: number, qty: number): [string, number][] {
  const members = g.members;
  const primary = g.primary;
  if (members.length === 1) return [[primary, qty]];
  let left = qty;
  const takes: [string, number][] = [];
  for (const m of members) {
    const cap = capacity(items[m], a);
    if (cap <= EPS) continue;
    const take = cap >= left ? left : cap;
    takes.push([m, take]);
    left -= take;
    if (left <= EPS) break;
  }
  // the primary carries its own take plus the shortfall: computed as a
  // difference so the pieces always sum to qty exactly (same op order as
  // Python: alt_total summed in takes order, prim = qty - alt_total)
  let altTotal = 0;
  const out: [string, number][] = [];
  for (const [m, take] of takes) {
    if (m !== primary) altTotal += take;
  }
  const prim = qty - altTotal;
  if (prim > EPS) out.push([primary, prim]);
  for (const [m, take] of takes) {
    if (m !== primary && take > EPS) out.push([m, take]);
  }
  return out;
}

/** Register a block and append its draws to its recipe items' curves.
 * A key already registered is left untouched and its entry returned: the
 * key is block_id|start, so a repeat is the same board row sent twice,
 * and drawing it again would fake a shortage. removeBlock first (on a
 * copy) or build fresh timelines to change a block.
 *
 * Draws are netted sequentially in registration order (allocate):
 * buildTimelines registers the board in (start, end, key) order, so an
 * earlier run takes the shared stock first; a block added afterwards (a
 * preview, a virtual block) is allocated what is left, which is the
 * conservative answer for a NEW run. */
export function addBlock(timelines: Timelines, block: TimelineBlock): TimelineBlock {
  const b: TimelineBlock = { ...block };
  const key = blockKey(b);
  const existing = own(timelines.blocks, key);
  if (existing !== undefined) return existing;
  b.key = key;
  timelines.blocks[key] = b;
  const items = timelines.items;
  for (const g of own(timelines.groups, String(b.sku ?? "")) ?? []) {
    const d = blockDraw(b, g.per_case, items[g.primary].snapshot_h);
    if (d === null) continue;
    const [a, e, qty] = d;
    for (const [m, q] of allocate(items, g, a, qty)) {
      items[m].draws.push({ key, a, e, qty: q });
    }
  }
  return b;
}

/** Per-item curves + per-SKU requirement groups, JSON-able (§3.6). */
export function buildTimelines(
  blocks: TimelineBlock[] | null | undefined,
  skuNeeds: SkuNeeds | null | undefined,
  opening: Record<string, number> | null | undefined,
  receipts: Record<string, Receipt[]> | null | undefined,
  snapshotH: Record<string, number> | null | undefined,
  opts: { tracked?: Iterable<string> | null; in_house?: Iterable<string> | null },
): Timelines {
  // Every item key (recipe items and alts, opening, receipts, snapshot_h,
  // tracked, in_house) goes through itemKey first, so a stray space or an
  // int code still lands on the same curve. Keys that collide after
  // trimming pool: openings add, receipts concatenate, the first snapshot
  // hour wins (first in Object.keys order: JS lists integer-like keys
  // ahead of others, Python keeps payload order -- build_stock_payload
  // never emits colliding keys). Duplicate block keys keep the first.
  const trackedSet = new Set(Array.from(opts.tracked ?? [], (x) => itemKey(x)));
  trackedSet.delete("");
  const inHouseSet = new Set(Array.from(opts.in_house ?? [], (x) => itemKey(x)));
  inHouseSet.delete("");
  const openingBy: Record<string, number> = {};
  const openingIn = opening ?? {};
  for (const k of Object.keys(openingIn)) {
    const i = itemKey(k);
    openingBy[i] = (own(openingBy, i) ?? 0) + numOr0(openingIn[k]);
  }
  const receiptsBy: Record<string, Receipt[]> = {};
  const receiptsIn = receipts ?? {};
  for (const k of Object.keys(receiptsIn)) {
    const i = itemKey(k);
    const lst = own(receiptsBy, i) ?? (receiptsBy[i] = []);
    for (const r of receiptsIn[k] ?? []) lst.push(r);
  }
  const snapshotBy: Record<string, number> = {};
  const snapshotIn = snapshotH ?? {};
  for (const k of Object.keys(snapshotIn)) {
    const i = itemKey(k);
    if (own(snapshotBy, i) === undefined) snapshotBy[i] = numOr0(snapshotIn[k]);
  }
  const items: Record<string, ItemTimeline> = {};
  const groups: Record<string, Group[]> = {};

  const ensureItem = (i: string): void => {
    if (own(items, i) !== undefined) return;
    const rs = (own(receiptsBy, i) ?? []).map((r) => ({ ...r }));
    rs.sort((x, y) =>
      cmpNum(Number(x.ready_h), Number(y.ready_h))
      || cmpStr(String(x.po8 ?? ""), String(y.po8 ?? "")));
    items[i] = {
      opening: own(openingBy, i) ?? 0,
      draws: [],
      receipts: rs,
      snapshot_h: own(snapshotBy, i) ?? 0,
    };
  };

  const needs = skuNeeds ?? {};
  for (const sku of Object.keys(needs)) {
    const need = needs[sku];
    const gl: Group[] = [];
    for (const ent of need?.items ?? []) {
      const primary = itemKey(ent.item);
      const perCase = Number(ent.per_case ?? 0) || 0;
      const members = [primary];
      for (const a0 of ent.alts ?? []) {
        const a = itemKey(a0);
        if (a && !members.includes(a)) members.push(a);
      }
      // In-house intermediates (BT001/BT002) are made on demand: a group
      // touching one never gates (coverage.py precedent).
      if (perCase <= 0 || members.some((m) => inHouseSet.has(m))) continue;
      gl.push({
        primary,
        members,
        per_case: perCase,
        unit: String(ent.unit ?? ""),
        tracked: members.some((m) => trackedSet.has(m)),
      });
      for (const m of members) ensureItem(m);
    }
    groups[String(sku)] = gl;
  }

  const tl: Timelines = {
    items, groups, blocks: {},
    tracked: [...trackedSet].sort(), in_house: [...inHouseSet].sort(),
  };
  // Board order for the sequential netting = time order: the run that
  // starts first takes the shared stock first (ties: shorter run, then
  // key). Array.prototype.sort is stable (ES2019), like Python's sorted,
  // so the input order only matters between exact twins.
  const ordered = [...(blocks ?? [])].sort((x, y) =>
    cmpNum(numOr0(x.start_h), numOr0(y.start_h))
    || cmpNum(numOr0(x.end_h), numOr0(y.end_h))
    || cmpStr(blockKey(x), blockKey(y)));
  for (const b of ordered) addBlock(tl, b);
  return tl;
}

/** Cheap copy: only draw lists and the block map are ever mutated. */
export function copyTimelines(timelines: Timelines): Timelines {
  const items: Record<string, ItemTimeline> = {};
  for (const i of Object.keys(timelines.items)) {
    items[i] = { ...timelines.items[i], draws: timelines.items[i].draws.slice() };
  }
  return {
    items,
    groups: timelines.groups,
    blocks: { ...timelines.blocks },
    tracked: timelines.tracked ?? [],
    in_house: timelines.in_house ?? [],
  };
}

/** Forget a registered block and its draws: the inverse of addBlock, for
 * previews on a copyTimelines() copy (drop the dragged block, addBlock its
 * virtual position, evaluateBlock). True when the key was registered.
 * No Python twin: the report never moves a block, so a rebuild without
 * the block is the reference (see the parity test). */
export function removeBlock(timelines: Timelines, key: string): boolean {
  if (own(timelines.blocks, key) === undefined) return false;
  delete timelines.blocks[key];
  for (const i of Object.keys(timelines.items)) {
    const it = timelines.items[i];
    if (it.draws.some((d) => d.key === key)) it.draws = it.draws.filter((d) => d.key !== key);
  }
  return true;
}

// --------------------------------------------------------------------------
// curve evaluation
// --------------------------------------------------------------------------

function consumed(d: Draw, t: number): number {
  const a = d.a, e = d.e, q = d.qty;
  if (t <= a || e <= a) return 0;
  if (t >= e) return q;
  return q * (t - a) / (e - a);
}

function hardAt(opening: number, draws: Draw[], t: number): number {
  let v = opening;
  for (const d of draws) v -= consumed(d, t);
  return v;
}

/** Evaluation points [t, side]: side 0 = just before t (receipts with
 * ready_h < t), side 1 = at t (ready_h <= t). H is continuous so both share
 * one H(t); only the receipt step differs. Draw breakpoints keep every
 * segment linear; receipt hours catch the dip right before a truck. */
function points(ws: number, we: number, draws: Draw[], receipts: Receipt[]): [number, number][] {
  const ts = new Set<number>([ws, we]);
  for (const d of draws) {
    for (const x of [d.a, d.e]) if (ws < x && x < we) ts.add(x);
  }
  for (const r of receipts) {
    const rh = Number(r.ready_h);
    if (ws < rh && rh <= we) ts.add(rh);
  }
  const out: [number, number][] = [];
  for (const t of [...ts].sort(cmpNum)) {
    if (t > ws) out.push([t, 0]);
    out.push([t, 1]);
  }
  return out;
}

/** [min value, first hour the curve is < 0 or null] for H + steps.
 * hardPoints: [t, side, H(t)] ascending. The first crossing is linearly
 * interpolated inside the segment where it happens (P is linear between
 * consecutive points; the before/at pair at one t is a zero-length step). */
function curveMin(hardPoints: [number, number, number][], receipts: Receipt[])
  : [number, number | null] {
  let best: number | null = null;
  let firstNeg: number | null = null;
  let prevT: number | null = null;
  let prevV = 0;
  for (const [t, side, h] of hardPoints) {
    let added = 0;
    for (const r of receipts) {
      const rh = Number(r.ready_h);
      if (rh < t || (side === 1 && rh === t)) added += Number(r.qty);
    }
    const v = h + added;
    if (best === null || v < best) best = v;
    if (firstNeg === null && v < -EPS) {
      if (prevT === null || prevT === t) firstNeg = t;
      else if (prevV <= 0) firstNeg = prevT;
      else firstNeg = prevT + (t - prevT) * prevV / (prevV - v);
    }
    prevT = t;
    prevV = v;
  }
  return [best === null ? 0 : best, firstNeg];
}

/** Item balance at hour t (with receipts landed by t when planned). */
export function balanceAt(timelines: Timelines, item: string, t: number, planned = true): number {
  const it = timelines.items[item];
  let v = hardAt(it.opening, it.draws, t);
  if (planned) {
    for (const r of it.receipts) if (Number(r.ready_h) <= t) v += Number(r.qty);
  }
  return v;
}

// --------------------------------------------------------------------------
// §3.3–3.4  verdict per (block, group)
// --------------------------------------------------------------------------

function bindingDict(r: Receipt | null): Binding | null {
  if (r === null) return null;
  return {
    po8: String(r.po8 ?? ""), qty: Number(r.qty), ready_h: Number(r.ready_h),
    receipt_date: r.receipt_date ?? null, label: String(r.label ?? ""),
  };
}

function evaluateGroup(
  block: TimelineBlock, key: string, g: Group, timelines: Timelines, R: Rules,
  L: number, feedState: FeedState, windowEndH: number,
): SupplyItem {
  const items = timelines.items;
  const start = Number(block.start_h);
  const end = Number(block.end_h);
  const primary = g.primary;
  const members = g.members;
  const S = items[primary].snapshot_h;
  const ws = Math.max(start, S);
  const we = end;
  let opening = 0;
  const draws: Draw[] = [];
  const receipts: Receipt[] = [];
  for (const m of members) {
    const it = items[m];
    opening += it.opening;
    draws.push(...it.draws);
    receipts.push(...it.receipts);
  }
  receipts.sort((x, y) =>
    cmpNum(Number(x.ready_h), Number(y.ready_h))
    || cmpStr(String(x.po8 ?? ""), String(y.po8 ?? ""))
    || cmpNum(Number(x.qty), Number(y.qty)));
  // The block's own draw may sit in pieces on several members after the
  // sequential netting (allocate); its need is their sum (members order,
  // same summation order as Python).
  let need = 0;
  for (const m of members) {
    for (const d of items[m].draws) {
      if (d.key === key) need += Number(d.qty);
    }
  }

  const entry: SupplyItem = {
    item: primary, unit: g.unit, need,
    opening_at_start: hardAt(opening, draws, ws),
    minH: 0, minP: 0, minR: 0,
    verdict: VERDICT_OK, backed: false,
    covered_frac: 1, depletion_h: null,
    covered_frac_planned: 1, depletion_planned_h: null,
    binding: null, lead_h: null, safe_from_h: null,
    dependent_frac: 0, minor: false, mid_run: false,
  };
  if (need <= 0 || we <= ws) {
    // Nothing drawn (finished before the count, zero length) — the group
    // cannot fail this block whatever other blocks do to it.
    const h = entry.opening_at_start;
    entry.minH = entry.minP = entry.minR = h;
    return entry;
  }

  const pts = points(ws, we, draws, receipts);
  const hard: [number, number, number][] = pts.map(([t, side]) => [t, side, hardAt(opening, draws, t)]);
  const [minH, dH] = curveMin(hard, []);
  const [minP, dP] = curveMin(hard, receipts);
  let ref: number;
  if (R.lead_measured_from === "depletion") ref = dH !== null ? dH : start;
  else ref = start;
  const reliable = receipts.filter((r) => Number(r.ready_h) <= ref - L);
  const [minR] = curveMin(hard, reliable);

  let verdict: Verdict;
  let backed: boolean;
  if (minH >= -EPS) { verdict = VERDICT_OK; backed = false; }
  else if (feedState !== "ok") { verdict = VERDICT_NO_DATA; backed = false; }
  else if (minR >= -EPS) { verdict = VERDICT_OK; backed = true; }
  else if (minP >= -EPS) { verdict = VERDICT_DEPENDENT; backed = false; }
  else {
    // Audit stock-1 (K-4, ported): SHORT must be reachable. The feed's
    // horizon (windowEndH) can only excuse a crossing when the feed shows
    // inbound for this group at all (a truck counted before the block
    // ends) AND the horizon reaches into the run yet ends before the
    // crossing — "the next truck may not be in the extract yet". No
    // inbound at all, or a horizon that ends before the run even starts,
    // is a proven shortage on the data we have.
    let inbound = false;
    for (const r of receipts) {
      if (Number(r.ready_h) < we) { inbound = true; break; }
    }
    verdict = inbound && dP !== null && dP > windowEndH && windowEndH >= ws
      ? VERDICT_NO_DATA : VERDICT_SHORT;
    backed = false;
  }

  // Binding receipt: latest-first, drop while the planned curve stays >= 0;
  // the first one that cannot be dropped is the truck this block actually
  // waits for (SHORT keeps the latest counted one for the text).
  const counted = receipts.filter((r) => Number(r.ready_h) < we);
  let kept = counted.slice();
  let binding: Receipt | null = null;
  for (let i = counted.length - 1; i >= 0; i--) {
    const r = counted[i];
    const trial = kept.filter((x) => x !== r);
    const [m] = curveMin(hard, trial);
    if (m >= -EPS) kept = trial;
    else { binding = r; break; }
  }

  // Share of the block's REMAINING draw that on-hand covers (audit
  // stock-11 / K-8, ported): the draw is linear on [ws, we], so
  // (dH - ws) / (we - ws) is both the time and the quantity fraction of
  // what this block still has to draw when the curve first dips. Measuring
  // from `start` counted the hours a running block had already produced
  // before the stock count.
  const span = we - ws;
  const covered = dH === null ? 1 : clamp01((dH - ws) / span);
  const coveredP = dP === null ? 1 : clamp01((dP - ws) / span);
  const depFrac = minH < 0 ? clamp01(-minH / need) : 0;
  entry.minH = minH;
  entry.minP = minP;
  entry.minR = minR;
  entry.verdict = verdict;
  entry.backed = backed;
  entry.covered_frac = covered;
  entry.depletion_h = dH;
  entry.covered_frac_planned = coveredP;
  entry.depletion_planned_h = dP;
  entry.binding = bindingDict(binding);
  entry.lead_h = binding ? ref - Number(binding.ready_h) : null;
  entry.safe_from_h = binding ? Number(binding.ready_h) + L : null;
  entry.mid_run = binding !== null && Number(binding.ready_h) > start;
  entry.dependent_frac = depFrac;
  entry.minor = verdict === VERDICT_DEPENDENT && depFrac < Number(R.dependent_frac_floor);
  return entry;
}

// --------------------------------------------------------------------------
// §3.5  block verdict
// --------------------------------------------------------------------------

function emptySupply(key: string, verdict: Verdict, leadFrom: string): Supply {
  return {
    key, verdict, backed: false, minor: false, mid_run: false, item: null,
    covered_frac: null, depletion_h: null, lead_h: null, safe_from_h: null,
    binding: null, action: "none", lead_from: leadFrom, items: [],
    untracked: [], co_consumers: {}, text: "",
  };
}

export function evaluateBlock(
  block: TimelineBlock, timelines: Timelines, rules: Partial<Rules> | null | undefined,
  feedState: FeedState, receiptsWindowEndH: number,
): Supply {
  const R = mergeRules(rules);
  const L = 24 * Number(R.min_days_after_delivery);
  // lead_from rides along so verdictText can word the lead correctly
  // ("before start" vs "before it runs out") without the rules.
  const leadFrom = String(R.lead_measured_from || "block_start");
  const key = blockKey(block);
  const sku = String(block.sku ?? "");
  const groups = own(timelines.groups, sku);
  // No recipe / unknown quantity: grey "?", never green (plan §8).
  if (groups === undefined || !isNum(block.cases)) {
    const sup = emptySupply(key, VERDICT_NO_DATA, leadFrom);
    sup.text = verdictText(sup);
    return sup;
  }
  const start = Number(block.start_h);
  const end = Number(block.end_h);
  const items = timelines.items;
  const entries: SupplyItem[] = [];
  const untracked: string[] = [];
  const co: Record<string, string[]> = {};
  for (const g of groups) {
    if (!g.tracked) {
      untracked.push(g.primary);
      continue;
    }
    entries.push(evaluateGroup(block, key, g, timelines, R, L, feedState, receiptsWindowEndH));
    const ws = Math.max(start, items[g.primary].snapshot_h);
    for (const m of g.members) {
      const others = items[m].draws.filter((d) => d.key !== key && d.a < end && d.e > ws);
      if (others.length) {
        others.sort((x, y) => cmpNum(x.a, y.a) || cmpStr(x.key, y.key));
        co[m] = others.map((d) => d.key);
      }
    }
  }

  if (!entries.length) {
    const sup = emptySupply(key, VERDICT_OK, leadFrom);
    sup.untracked = untracked;
    sup.text = verdictText(sup);
    return sup;
  }

  // Worst group: first in recipe order among the max rank.
  let worst = entries[0];
  for (const e of entries.slice(1)) {
    if (supplyRank(e) > supplyRank(worst)) worst = e;
  }
  const verdict = worst.verdict;
  const locked = Boolean(block.locked) || Boolean(block.running);
  let action: Supply["action"];
  if (verdict === VERDICT_OK) action = "none";
  else action = locked ? "chase_po" : "move";
  const sup: Supply = {
    key,
    verdict,
    backed: worst.backed,
    minor: worst.minor,
    mid_run: worst.mid_run,
    item: worst.item,
    covered_frac: worst.covered_frac,
    depletion_h: verdict === VERDICT_SHORT ? worst.depletion_planned_h : worst.depletion_h,
    lead_h: worst.lead_h,
    safe_from_h: worst.safe_from_h,
    binding: worst.binding,
    action,
    lead_from: leadFrom,
    items: entries,
    untracked,
    co_consumers: co,
    text: "",
  };
  sup.text = verdictText(sup);
  return sup;
}

export function evaluateBoard(
  blocks: TimelineBlock[] | null | undefined, timelines: Timelines,
  rules: Partial<Rules> | null | undefined, feedState: FeedState, receiptsWindowEndH: number,
): Record<string, Supply> {
  const out: Record<string, Supply> = {};
  for (const b of blocks ?? []) {
    const sup = evaluateBlock(b, timelines, rules, feedState, receiptsWindowEndH);
    out[sup.key] = sup;
  }
  return out;
}

/** First start >= fromH at which a virtual block reads OK (plain or
 * backed). Candidates: fromH and every receipt + L of the SKU's recipe
 * items — the only hours where a verdict can improve. */
export function earliestClearStart(
  sku: string, cases: number | null, durationH: number, timelines: Timelines,
  rules: Partial<Rules> | null | undefined, feedState: FeedState,
  receiptsWindowEndH: number, fromH: number, lineName = "",
): EarliestClearStart {
  const R = mergeRules(rules);
  const L = 24 * Number(R.min_days_after_delivery);
  sku = String(sku);
  fromH = Number(fromH);
  const cands = new Set<number>([fromH]);
  for (const g of own(timelines.groups, sku) ?? []) {
    for (const m of g.members) {
      for (const r of timelines.items[m].receipts) {
        const rh = Number(r.ready_h);
        if (rh >= fromH - L) cands.add(rh + L);
      }
    }
  }
  let verdictNow: Supply | null = null;
  for (const c of [...cands].sort(cmpNum)) {
    const tl = copyTimelines(timelines);
    const vb: TimelineBlock = {
      key: `virtual:${sku}@${pyFixed(c, 2)}`, block_id: "virtual",
      sku, line_name: lineName, start_h: c, end_h: c + Number(durationH),
      cases, locked: false, running: false, cases_left: null,
    };
    addBlock(tl, vb);
    const sup = evaluateBlock(vb, tl, R, feedState, receiptsWindowEndH);
    if (verdictNow === null) verdictNow = sup;
    if (sup.verdict === VERDICT_OK) {
      return { safe_from_h: c, verdict_now: verdictNow, verdict_at_safe: sup };
    }
  }
  return { safe_from_h: null, verdict_now: verdictNow, verdict_at_safe: null };
}

// --------------------------------------------------------------------------
// text
// --------------------------------------------------------------------------

// floor(x*10+0.5): identical in Python and JS (Python's round() is half-even,
// Math.round half-up — the text must match across the port).
function r1(x: number): number {
  return Math.floor(Number(x) * 10 + 0.5) / 10;
}

function days(h: number): string {
  return `${pyFixed(r1(Math.abs(h) / 24), 1)} d`;
}

/** timeline._lead: whole hours (half-up) under a day — a truck 1 h before
 * start must not read "0.0 d" — tenths of a day from 24 h on. */
function lead(h: number): string {
  const a = Math.abs(Number(h));
  return a < 24 ? `${Math.floor(a + 0.5)} h` : days(h);
}

/** timeline._shown_hours: the hours lead()'s rounding actually displays
 * (24 h -> 24.0, 95 h -> 4.0 d -> 96.0). */
function shownHours(h: number): number {
  const a = Math.abs(Number(h));
  if (a < 24) return Math.floor(a + 0.5);
  return r1(a / 24) * 24;
}

/** timeline._lead_vs_buffer (audit stock-13 / K-8, ported): lead(), unless
 * its rounding would read AT OR ABOVE the buffer while the lead is really
 * under it (95 h printed "4.0 d before start (min 4 d)" on a DEPENDENT
 * block). Then whole hours, floored, so the sentence explains the verdict:
 * "95 h before start (min 4 d)". */
function leadVsBuffer(leadH: number, bufferH: number | null): string {
  if (bufferH !== null && 0 <= leadH && leadH < bufferH && shownHours(leadH) >= bufferH) {
    return `${Math.floor(leadH)} h`;
  }
  return lead(leadH);
}

function pct(frac: number): number {
  return Math.floor(Number(frac) * 100 + 0.5);
}

function gnum(x: number): string {
  const r = r1(x);
  return Number.isInteger(r) ? String(Math.trunc(r)) : pyFixed(r, 1);
}

const DAY_ABBR = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const pad2 = (n: number): string => String(n).padStart(2, "0");

/** Anchor as a naive wall clock in UTC milliseconds (Python datetime has no
 * zone: hours add without DST, so all arithmetic runs on UTC fields). */
function anchorUtcMs(anchor: Anchor): number {
  if (anchor instanceof Date) {
    return Date.UTC(anchor.getFullYear(), anchor.getMonth(), anchor.getDate(),
      anchor.getHours(), anchor.getMinutes(), anchor.getSeconds());
  }
  const m = /^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?/.exec(String(anchor));
  if (m) {
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] ?? 0), +(m[5] ?? 0), +(m[6] ?? 0));
  }
  const d = new Date(String(anchor));
  return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds());
}

function roundHalfEven(x: number): number {
  const f = Math.floor(x);
  if (x - f === 0.5) return f % 2 === 0 ? f : f + 1;
  return Math.round(x);
}

/** Whole microseconds of timedelta(hours=h), the way CPython builds it:
 * integer part x 3.6e9 exactly, the fraction scaled and split again, the
 * leftover rounded half-even. Keeps a stamp one microsecond from a minute
 * boundary on the same side as Python. */
function timedeltaUs(hours: number): number {
  const ip = Math.trunc(hours);
  const fp = hours - ip;
  const d = fp * 3.6e9;
  const di = Math.trunc(d);
  return ip * 3.6e9 + di + roundHalfEven(d - di);
}

/** helpers.timefmt.hour_to_stamp: "Wed 9/2 16:00" (real minutes). */
export function hourStamp(hour: number, anchor: Anchor): string {
  const secs = Math.floor(timedeltaUs(Number(hour)) / 1e6);
  const d = new Date(anchorUtcMs(anchor) + secs * 1000);
  return `${DAY_ABBR[d.getUTCDay()]} ${d.getUTCMonth() + 1}/${d.getUTCDate()} `
    + `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}

/** One-line planner sentence; real stamps when an anchor is given. */
export function verdictText(supply: Supply, anchor?: Anchor | null): string {
  const stamp = (h: number): string =>
    anchor != null ? hourStamp(h, anchor) : `h${pyFixed(r1(h), 1)}`;

  const v = supply.verdict;
  const item = supply.item ?? null;
  if (item === null) {
    if (v === VERDICT_OK) return "✅ supply: no tracked components to check";
    return "? supply: no recipe or quantity data";
  }
  let entry: SupplyItem | null = null;
  for (const e of supply.items ?? []) {
    if (e.item === item) { entry = e; break; }
  }
  // Python: entry.get("depletion_h", supply.get("depletion_h")) — the
  // worst group's HARD depletion (SHORT keeps d_P at the supply level).
  const dH: number | null = entry !== null && entry.depletion_h !== undefined
    ? entry.depletion_h
    : (supply.depletion_h ?? null);
  const coveredRaw = supply.covered_frac;
  const covered = coveredRaw == null ? 1 : Number(coveredRaw);
  let cover = `on hand covers ${pct(covered)}%`;
  if (dH !== null) cover += ` (runs out ${stamp(dH)})`;

  const b = supply.binding ?? null;
  const leadH = supply.lead_h ?? null;
  const safe = supply.safe_from_h ?? null;
  let po: string | null = null;
  if (b && leadH !== null) {
    const depMode = supply.lead_from === "depletion";
    const refWord = depMode ? "it runs out" : "start";
    const mid = supply.mid_run ? " (mid-run)" : "";
    const bufferH = safe !== null ? Number(safe) - Number(b.ready_h) : null;
    const leadTxt = leadH >= 0
      ? `${leadVsBuffer(leadH, bufferH)} before ${refWord}${mid}`
      : `${lead(leadH)} after ${refWord}${mid}`;
    po = `PO ${b.po8} lands ${stamp(b.ready_h)} — ${leadTxt}`;
    if (safe !== null) po += ` (min ${gnum((safe - b.ready_h) / 24)} d)`;
  }

  if (v === VERDICT_OK) {
    if (!supply.backed) return `✅ ${item}: on hand covers the run`;
    return `🚚 ${item}: ${cover} · ${po ?? "None"} · backed`;
  }
  if (v === VERDICT_DEPENDENT) {
    const parts = [`🚚 ${item}: ${cover}`];
    if (po) parts.push(po);
    if (safe !== null) parts.push(`safe from ${stamp(safe)}`);
    if (supply.minor) parts.push("minor share");
    return parts.join(" · ");
  }
  if (v === VERDICT_SHORT) {
    const parts = [`⛔ ${item}: ${cover}`];
    const dp = supply.depletion_h ?? null;
    if (b) {
      const stop = dp !== null ? ` the line stops ${stamp(dp)}` : "";
      parts.push(`even with PO ${b.po8} (${stamp(b.ready_h)})` + stop);
    } else {
      parts.push("no inbound counted");
    }
    return parts.join(" · ");
  }
  return `? ${item}: ${cover} · no inbound data covering the gap`;
}
