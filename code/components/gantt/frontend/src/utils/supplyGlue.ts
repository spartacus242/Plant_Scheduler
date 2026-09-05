// supplyGlue.ts — board blocks -> supply-timeline verdicts (client glue).
//
// The pure seam between the Gantt's ScheduleBlock state and utils/stockRisk
// (the timeline engine port): block -> TimelineBlock per contract 2026-09-01
// §5, one evaluateSchedule per schedule change, a per-frame previewSupply
// for the dragged block only, and the chip / banner wording of §9. No React,
// no DOM — tests/test_supply_glue.py compiles this file (with stockRisk and
// blockIdentity, both import-free) under the frontend's tsc and pins it in
// node. Every stock surface is gated upstream on `!!args.stock`; nothing
// here runs without a payload.
//
// Stamps: hourStamp from stockRisk (helpers.timefmt.hour_to_stamp twin,
// real minutes) — NOT layout.hourToStamp, which prints ":00" and would turn
// "runs out Fri 9/4 23:19" into "23:00" while the Reconcile finding says
// 23:19.

import type { ScheduleBlock, StockArgs } from "../types";
import { blockKey, sameBlock } from "./blockIdentity";
import {
  DEFAULT_RULES, EPS, addBlock, balanceAt, buildTimelines, copyTimelines, earliestClearStart, evaluateBlock,
  evaluateBoard, hourStamp, removeBlock, supplyRank,
  type Anchor, type EarliestClearStart, type Receipt, type Rules, type Supply, type SupplyItem,
  type TimelineBlock, type Timelines,
} from "./stockRisk";

export type Caps = Record<string, Record<string, number>>;
export type StampFn = (h: number) => string;

/** Own-property lookup: item codes / SKUs are arbitrary strings and must
 * never fall through to Object.prototype. */
function own<T>(o: Record<string, T> | null | undefined, k: string): T | undefined {
  return o != null && Object.prototype.hasOwnProperty.call(o, k) ? o[k] : undefined;
}

function num(x: unknown): number {
  const n = Number(x);
  return Number.isFinite(n) ? n : 0;
}

/** Rate for (line, sku): the line's own row, else the double-line group
 * ("P17A" -> "P17") the way validation.getRate falls back. */
function rateFor(caps: Caps | null | undefined, lineName: string, sku: string): number {
  const direct = num(own(own(caps, lineName), sku));
  if (direct > 0) return direct;
  const m = /^(.*\d)[AB]$/.exec(lineName);
  return m ? num(own(own(caps, m[1]), sku)) : 0;
}

/** The 4-key StockArgs.rules over the engine defaults (§1). */
export function rulesOf(stock: StockArgs | null | undefined): Rules {
  return { ...DEFAULT_RULES, ...(stock?.rules ?? {}) };
}

export function stampFor(anchor: Anchor): StampFn {
  return (h: number) => hourStamp(h, anchor);
}

// --------------------------------------------------------------------------
// block -> timeline block (§5)
// --------------------------------------------------------------------------

/** True when the block's need comes from its own kg (qty_kg / kg_per_case)
 * rather than the rate x hours fallback — the popover says which. */
export function casesFromKg(block: ScheduleBlock, stock: StockArgs): boolean {
  const kpc = num(own(stock.sku_needs, String(block.sku ?? ""))?.kg_per_case);
  return kpc > 0 && num(block.qty_kg) > 0;
}

/** Production block -> engine block; null for windows/trials/etc. Cases:
 * board kg / kg_per_case, else rate x hours / kg_per_case, else null
 * (-> NO_DATA "?", never green). locked mirrors the board's immovable
 * rules (locked/completed/pinned/inside the lock window) so the verdict's
 * action reads "chase PO" for blocks the planner cannot move. */
export function toTimelineBlock(
  block: ScheduleBlock, stock: StockArgs, capabilities: Caps | null | undefined,
  lockedThroughH: number | null | undefined,
): TimelineBlock | null {
  if (block.block_type !== "sku") return null;
  const sku = String(block.sku ?? "");
  const start = num(block.start_hour);
  const end = num(block.end_hour);
  const kpc = num(own(stock.sku_needs, sku)?.kg_per_case);
  const kg = num(block.qty_kg);
  let cases: number | null = null;
  if (kpc > 0) {
    if (kg > 0) cases = kg / kpc;
    else {
      const rate = rateFor(capabilities, String(block.line_name ?? ""), sku);
      if (rate > 0 && end > start) cases = (rate * (end - start)) / kpc;
    }
  }
  const lockH = lockedThroughH == null ? -Infinity : num(lockedThroughH);
  const cl = own(stock.cases_left ?? {}, String(block.order_id ?? ""));
  return {
    key: blockKey(block),
    block_id: String(block.id),
    sku,
    line_name: String(block.line_name ?? ""),
    start_h: start,
    end_h: end,
    cases,
    locked: Boolean(block.locked) || Boolean(block.completed) || Boolean(block.pinned) || start < lockH,
    running: String(block.attrs ?? "").includes("current_state:running"),
    cases_left: typeof cl === "number" && Number.isFinite(cl) ? cl : null,
  };
}

export function boardBlocks(
  schedule: readonly ScheduleBlock[], stock: StockArgs, capabilities: Caps | null | undefined,
  lockedThroughH: number | null | undefined,
): TimelineBlock[] {
  const out: TimelineBlock[] = [];
  for (const b of schedule) {
    const tb = toTimelineBlock(b, stock, capabilities, lockedThroughH);
    if (tb) out.push(tb);
  }
  return out;
}

// --------------------------------------------------------------------------
// whole-board evaluation
// --------------------------------------------------------------------------

/** Counts follow supply_rank like calendar_io.board_supply_summary: a minor
 * DEPENDENT is a grey chip, so it lands in `backed`, not `dependent`. */
export interface SupplyCounts { short: number; dependent: number; no_data: number; backed: number }

export interface ScheduleSupply {
  /** `${id}|${start_hour}` -> Supply (blockIdentity.blockKey). */
  verdicts: Map<string, Supply>;
  timelines: Timelines;
  blocks: TimelineBlock[];
  counts: SupplyCounts;
}

export function evaluateSchedule(
  schedule: readonly ScheduleBlock[], stock: StockArgs, capabilities: Caps | null | undefined,
  lockedThroughH: number | null | undefined,
): ScheduleSupply {
  const blocks = boardBlocks(schedule, stock, capabilities, lockedThroughH);
  const timelines = buildTimelines(
    blocks, stock.sku_needs, stock.opening, stock.receipts, stock.snapshot_h,
    { tracked: stock.tracked, in_house: stock.in_house },
  );
  const board = evaluateBoard(
    blocks, timelines, stock.rules, stock.feed_state, num(stock.receipts_window_end_h));
  const verdicts = new Map<string, Supply>();
  const counts: SupplyCounts = { short: 0, dependent: 0, no_data: 0, backed: 0 };
  for (const k of Object.keys(board)) {
    const sup = board[k];
    verdicts.set(k, sup);
    const r = supplyRank(sup);
    if (r === 4) counts.short += 1;
    else if (r === 3) counts.dependent += 1;
    else if (r === 2) counts.no_data += 1;
    else if (r === 1) counts.backed += 1;
  }
  return { verdicts, timelines, blocks, counts };
}

export function verdictFor(
  verdicts: Map<string, Supply> | null | undefined, block: { id: string; start_hour: number },
): Supply | null {
  return verdicts?.get(blockKey(block)) ?? null;
}

/** The dragged block at its preview position against the OTHER blocks:
 * the board's timelines are copied, the block's own draws removed and the
 * virtual block added — no rebuild per frame. Without `base` (no board
 * evaluation yet) the timelines are rebuilt without the block. */
export function previewSupply(
  block: ScheduleBlock, previewStart: number, previewEnd: number, previewLine: string,
  schedule: readonly ScheduleBlock[], stock: StockArgs, capabilities: Caps | null | undefined,
  lockedThroughH: number | null | undefined, base?: Timelines | null,
): Supply | null {
  if (block.block_type !== "sku") return null;
  const moved: ScheduleBlock = {
    ...block, line_name: previewLine, start_hour: previewStart, end_hour: previewEnd,
    run_hours: previewEnd - previewStart,
  };
  const vb = toTimelineBlock(moved, stock, capabilities, lockedThroughH);
  if (!vb) return null;
  let tl: Timelines;
  if (base) {
    tl = copyTimelines(base);
    removeBlock(tl, blockKey(block));
  } else {
    const rest = schedule.filter((b) => !sameBlock(b, block));
    tl = buildTimelines(
      boardBlocks(rest, stock, capabilities, lockedThroughH),
      stock.sku_needs, stock.opening, stock.receipts, stock.snapshot_h,
      { tracked: stock.tracked, in_house: stock.in_house },
    );
  }
  addBlock(tl, vb);
  return evaluateBlock(vb, tl, stock.rules, stock.feed_state, num(stock.receipts_window_end_h));
}

/** The requirement group the engine pools for (sku, item): the primary
 * plus its recipe alternates, trimmed and de-duplicated the way
 * buildTimelines keys them. Without a SKU (the gate only sees a Supply)
 * every recipe naming `item` as primary contributes its alternates —
 * over-pooling can only make the refusal rarer, never a false refusal. */
function groupMembers(stock: StockArgs, item: string, sku?: string | null): string[] {
  const members = [item];
  const skus = sku != null ? [String(sku)] : Object.keys(stock.sku_needs ?? {});
  for (const s of skus) {
    for (const ent of own(stock.sku_needs, s)?.items ?? []) {
      if (String(ent.item).trim() !== item) continue;
      for (const a of ent.alts ?? []) {
        const k = String(a).trim();
        if (k && !members.includes(k)) members.push(k);
      }
    }
  }
  return members;
}

/** The only refusal (§9): hard_block on, SHORT, the worst item's GROUP
 * (primary + alternates, as the engine pools it) has zero on hand and
 * zero inbound, and the feed is fresh — nothing could rescue the run, so
 * placing it is a plan the plant cannot execute. */
export function isHardBlock(
  supply: Supply | null | undefined, stock: StockArgs, sku?: string | null,
): boolean {
  if (!supply || !rulesOf(stock).hard_block) return false;
  if (supply.verdict !== "SHORT" || stock.feed_state !== "ok") return false;
  const item = supply.item;
  if (!item) return false;
  let opening = 0;
  for (const m of groupMembers(stock, item, sku)) {
    opening += num(own(stock.opening, m));
    if ((own(stock.receipts, m) ?? []).length > 0) return false;
  }
  return opening <= EPS;
}

// --------------------------------------------------------------------------
// wording (§9)
// --------------------------------------------------------------------------

function pct(frac: number | null | undefined): number {
  return Math.floor(num(frac == null ? 1 : frac) * 100 + 0.5);
}

/** |h| in days, one decimal ("1.3"). */
export function days(h: number): string {
  return (Math.floor((Math.abs(h) / 24) * 10 + 0.5) / 10).toFixed(1);
}

/** A lead the way timeline._lead prints it: whole hours (half-up) under a
 * day — "1 h", never "0.0 d" — tenths of a day from 24 h on ("1.3 d").
 * `compact` drops the space for the block chip ("14h", "1.3d"). */
export function leadText(h: number, compact = false): string {
  const a = Math.abs(num(h));
  const sp = compact ? "" : " ";
  return a < 24 ? `${Math.floor(a + 0.5)}${sp}h` : `${days(h)}${sp}d`;
}

function gnum(x: number): string {
  const r = Math.floor(x * 10 + 0.5) / 10;
  return Number.isInteger(r) ? String(Math.trunc(r)) : r.toFixed(1);
}

export type ChipTone = "warn" | "crit" | "muted";
export interface SupplyChip { text: string; tone: ChipTone }

/** Block-face chip: 🚚 1.3d / 🚚 14h orange (DEPENDENT), ⛔ 39% red
 * (SHORT), 🚚 grey (backed or minor), ? grey (NO_DATA), null for plain OK.
 * A mid-run truck has no lead to quote. */
export function chipFor(supply: Supply | null | undefined): SupplyChip | null {
  if (!supply) return null;
  const v = supply.verdict;
  if (v === "SHORT") return { text: `⛔ ${pct(supply.covered_frac)}%`, tone: "crit" };
  if (v === "DEPENDENT") {
    if (supply.minor) return { text: "🚚", tone: "muted" };
    const lead = supply.lead_h;
    if (lead == null) return { text: "🚚", tone: "warn" };
    return { text: lead < 0 ? "🚚 mid-run" : `🚚 ${leadText(lead, true)}`, tone: "warn" };
  }
  if (v === "NO_DATA") return { text: "?", tone: "muted" };
  return supply.backed ? { text: "🚚", tone: "muted" } : null;
}

/** Warn-banner wording: DEPENDENT (not minor) and SHORT deserve the
 * planner's attention after a commit; the rest stay chips. */
export function needsBanner(supply: Supply | null | undefined): boolean {
  if (!supply) return false;
  return supply.verdict === "SHORT" || (supply.verdict === "DEPENDENT" && !supply.minor);
}

/** The planner sentence with real stamps. DEPENDENT reads exactly like
 * stockRisk.verdictText (and the Reconcile finding); SHORT / NO_DATA /
 * backed take the shorter forms of §9. */
export function humanText(supply: Supply | null | undefined, stamp: StampFn): string {
  if (!supply) return "";
  const v = supply.verdict;
  const item = supply.item;
  if (item == null) {
    return v === "OK" ? "✅ supply: no tracked components to check"
      : "? supply: no recipe or quantity data";
  }
  let entry: SupplyItem | null = null;
  for (const e of supply.items ?? []) {
    if (e.item === item) { entry = e; break; }
  }
  // The worst group's HARD depletion (SHORT keeps d_P at the supply level).
  const dH = entry ? entry.depletion_h : supply.depletion_h;
  const covered = pct(supply.covered_frac);
  const b = supply.binding;
  const lead = supply.lead_h;
  const safe = supply.safe_from_h;
  const refWord = supply.lead_from === "depletion" ? "it runs out" : "start";
  const leadTxt = lead == null ? null
    : `${leadText(lead)} ${lead >= 0 ? "before" : "after"} ${refWord}`;

  if (v === "OK") {
    if (!supply.backed || !b) return `✅ ${item}: on hand covers the run`;
    return `🚚 ${item} backed by PO ${b.po8} (lands ${stamp(b.ready_h)}`
      + (leadTxt ? `, ${leadTxt}` : "") + ")";
  }
  if (v === "DEPENDENT") {
    let cover = `on hand covers ${covered}%`;
    if (dH != null) cover += ` (runs out ${stamp(dH)})`;
    const parts = [`🚚 ${item}: ${cover}`];
    if (b && leadTxt) {
      let po = `PO ${b.po8} lands ${stamp(b.ready_h)} — ${leadTxt}${supply.mid_run ? " (mid-run)" : ""}`;
      if (safe != null) po += ` (min ${gnum((safe - b.ready_h) / 24)} d)`;
      parts.push(po);
    }
    if (safe != null) parts.push(`safe from ${stamp(safe)}`);
    if (supply.minor) parts.push("minor share");
    return parts.join(" · ");
  }
  if (v === "SHORT") {
    const head = `⛔ ${item}: runs out at ${covered}%${dH != null ? ` (${stamp(dH)})` : ""}`;
    if (b) {
      const dp = supply.depletion_h;
      return `${head} — even with PO ${b.po8} (lands ${stamp(b.ready_h)})`
        + (dp != null ? ` the line stops ${stamp(dp)}` : "");
    }
    return `${head} — no receipt covers the rest`;
  }
  return `? ${item}: on hand covers ${covered}% — no inbound data covering the gap`;
}

// --------------------------------------------------------------------------
// popover helpers
// --------------------------------------------------------------------------

/** Items worst-first (supply_rank desc), recipe order within a rank. */
export function sortedItems(supply: Supply): SupplyItem[] {
  return supply.items
    .map((e, i) => ({ e, i }))
    .sort((a, b) => supplyRank(b.e) - supplyRank(a.e) || a.i - b.i)
    .map((x) => x.e);
}

/** Receipts the engine counted for this (sku, item) group on a block ending
 * at `endH`: the item's and its alternates' gated receipts with ready_h
 * before the block ends, soonest first. */
export function countedReceipts(
  stock: StockArgs, sku: string, item: string, endH: number,
): Receipt[] {
  const members = [item];
  for (const ent of own(stock.sku_needs, sku)?.items ?? []) {
    if (String(ent.item).trim() !== item) continue;
    for (const a of ent.alts ?? []) {
      const k = String(a).trim();
      if (k && !members.includes(k)) members.push(k);
    }
  }
  const out: Receipt[] = [];
  for (const m of members) {
    for (const r of own(stock.receipts, m) ?? []) {
      if (num(r.ready_h) < endH) out.push(r);
    }
  }
  out.sort((x, y) => num(x.ready_h) - num(y.ready_h) || String(x.po8).localeCompare(String(y.po8)));
  return out;
}

// --------------------------------------------------------------------------
// before placement (§9: holding cards, picker / place rows, axis trucks)
// --------------------------------------------------------------------------

/** stockRisk.earliestClearStart plus the inputs it was asked about, so a
 * pill can tell "clear at from_h" from "clear later". */
export interface SafeStart extends EarliestClearStart {
  from_h: number;
  duration_h: number;
  cases: number | null;
}

/** Earliest clear start for a run that is NOT on the board yet (holding
 * card, picker row): `kg` at `ratePerH`, from `fromH` on, judged ALONE
 * against the placed board's timelines — nothing is removed, other
 * unplaced cards are not counted. cases = kg / kg_per_case, duration =
 * kg / ratePerH (opts.fallbackHours when the rate is unknown). No recipe,
 * no kg or no duration -> null cases -> NO_DATA: a zero-length run draws
 * nothing and would otherwise read green. */
export function earliestSafeStartFor(
  sku: string, kg: number, ratePerH: number, fromH: number,
  timelines: Timelines, stock: StockArgs,
  opts?: { lineName?: string; fallbackHours?: number },
): SafeStart {
  const key = String(sku ?? "");
  const kpc = num(own(stock.sku_needs, key)?.kg_per_case);
  const kgN = num(kg);
  const rate = num(ratePerH);
  const durationH = rate > 0 && kgN > 0 ? kgN / rate : Math.max(0, num(opts?.fallbackHours));
  const cases = kpc > 0 && kgN > 0 && durationH > 0 ? kgN / kpc : null;
  const res = earliestClearStart(
    key, cases, durationH, timelines, stock.rules, stock.feed_state,
    num(stock.receipts_window_end_h), num(fromH), opts?.lineName ?? "",
  );
  return { ...res, from_h: num(fromH), duration_h: durationH, cases };
}

export type PillTone = ChipTone | "ok";
export interface SafeStartPill { text: string; tone: PillTone; title: string }

/** The before-placement pill (§9): "✓ clear" when on hand alone covers
 * the run at from_h, "🚚 backed" (grey, like the block chip) when it is OK
 * only because a truck inside the buffer counts, "🚚 from <stamp>" when a
 * later start clears (+ " · after due window" past the order's
 * due_end_hour), "? no data" when the engine cannot judge it, "⛔ no clear
 * start" when no candidate start is OK. The title carries the planner
 * sentence(s) and says the run was judged alone. */
export function safeStartPill(
  res: SafeStart, stamp: StampFn, dueEndH?: number | null,
): SafeStartPill {
  const now = res.verdict_now;
  const safe = res.safe_from_h;
  const lines: string[] = [];
  if (now) lines.push(`at ${stamp(res.from_h)}: ${humanText(now, stamp)}`);
  let text: string;
  let tone: PillTone;
  if (safe != null && safe <= res.from_h + EPS) {
    const backed = now != null && now.verdict === "OK" && Boolean(now.backed);
    text = backed ? "🚚 backed" : "✓ clear";
    tone = backed ? "muted" : "ok";
  } else if (safe != null) {
    const late = dueEndH != null && safe > num(dueEndH) + EPS;
    text = `🚚 from ${stamp(safe)}${late ? " · after due window" : ""}`;
    tone = "warn";
    if (res.verdict_at_safe) lines.push(`from ${stamp(safe)}: ${humanText(res.verdict_at_safe, stamp)}`);
    if (late) lines.push(`due window ends ${stamp(num(dueEndH))}`);
  } else if (!now || now.verdict === "NO_DATA") {
    text = "? no data";
    tone = "muted";
    if (!now) lines.push("? supply: no recipe or quantity data");
  } else {
    text = "⛔ no clear start";
    tone = "crit";
    lines.push("no start inside the receipts window reads OK");
  }
  lines.push("judged alone against the placed board");
  return { text, tone, title: lines.join("\n") };
}

/** Calendar-day column of an hour offset (the axis draws day k over
 * [24k, 24k+24) from the page anchor). */
export function dayIndexOf(h: number, hoursPerDay = 24): number {
  return Math.floor(num(h) / hoursPerDay);
}

/** Every gated receipt of every item, bucketed by calendar day (soonest
 * first inside a day) — the axis draws one truck per day that has any.
 * Days before the visible window simply never get asked for; a stale feed
 * with a negative receipts_window_end_h has already been gated to nothing
 * by Python, so there is nothing to tolerate here beyond bad ready_h. */
export function receiptsByDay(stock: StockArgs, hoursPerDay = 24): Map<number, Receipt[]> {
  const out = new Map<number, Receipt[]>();
  const table = stock.receipts ?? {};
  for (const item of Object.keys(table)) {
    for (const r of own(table, item) ?? []) {
      const rh = Number(r.ready_h);
      if (!Number.isFinite(rh)) continue;
      const d = dayIndexOf(rh, hoursPerDay);
      let arr = out.get(d);
      if (!arr) { arr = []; out.set(d, arr); }
      arr.push(r);
    }
  }
  for (const arr of out.values()) {
    arr.sort((x, y) => num(x.ready_h) - num(y.ready_h) || String(x.po8).localeCompare(String(y.po8)));
  }
  return out;
}

// --------------------------------------------------------------------------
// supply detail panel (planner feedback 2026-09-02)
// --------------------------------------------------------------------------
//
// The popover shows only what can bite (SHORT / DEPENDENT / NO_DATA / backed
// rows); the "who else draws it, which trucks, where it runs out" story
// lives in a modal built from these rows. Pure so tests/test_supply_detail
// can pin the rows under node; the panel only formats.

export type DrawTag = "this block" | "running" | "MO locked" | "planner";

export interface DetailDraw {
  key: string;
  /** Member the draw is on (the primary or one of its alternates). */
  item: string;
  is_alt: boolean;
  line_name: string;
  sku: string;
  order_id: string;
  a: number;
  e: number;
  qty: number;
  tag: DrawTag;
  /** Pooled group balance (planned curve) just before / after the draw. */
  balance_before: number;
  balance_after: number;
}

export interface DetailReceipt {
  item: string;
  is_alt: boolean;
  po8: string;
  qty: number;
  unit: string;
  receipt_date: string | null;
  ready_h: number;
  tier: string;
  /** Hours before the lead reference (block start, or its depletion). */
  lead_h: number;
  /** Lands before this block ends — the engine counted it. */
  counted: boolean;
  /** The truck this block actually waits for (SupplyItem.binding). */
  binding: boolean;
}

export interface DetailItem {
  item: string;
  designation: string;
  unit: string;
  verdict: SupplyItem["verdict"];
  backed: boolean;
  minor: boolean;
  mid_run: boolean;
  rank: number;
  need: number;
  opening_at_start: number;
  covered_frac: number;
  depletion_h: number | null;
  safe_from_h: number | null;
  dependent_frac: number;
  sentence: string;
  /** Primary first, then recipe alternates (the engine's pooled group). */
  members: string[];
  draws: DetailDraw[];
  receipts: DetailReceipt[];
}

export interface SupplyDetail {
  items: DetailItem[];
  ok_items: { item: string; designation: string }[];
  untracked: string[];
}

/** Locale-proof thousands grouping ("67,200"): toLocaleString follows the
 * host locale and the node harness must read the same digits. */
export function fmtQty(v: number): string {
  const n = Math.round(num(v));
  const s = String(Math.abs(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return n < 0 ? `-${s}` : s;
}

/** "on hand at start 22,045 EA → covers 39%, runs out Fri 9/4 23:19 ·
 * dependent share 78%" — the item line of the popover and the panel. */
export function itemSentence(e: SupplyItem, stamp: StampFn): string {
  let s = `on hand at start ${fmtQty(e.opening_at_start)} ${e.unit} → covers ${pct(e.covered_frac)}%`;
  if (e.depletion_h != null) s += `, runs out ${stamp(e.depletion_h)}`;
  if (e.dependent_frac > 0) s += ` · dependent share ${pct(e.dependent_frac)}%`;
  return s;
}

/** Pooled members of the (sku, item) group as the engine keyed them;
 * [item] when the timelines carry no such group. */
function detailMembers(timelines: Timelines, sku: string, item: string): string[] {
  for (const g of own(timelines.groups, sku) ?? []) {
    if (g.primary === item) return g.members.slice();
  }
  return [item];
}

/** Group balance = the members' curves summed (evaluateGroup pools the
 * openings, draws and receipts the same way). */
function groupBalance(timelines: Timelines, members: string[], t: number): number {
  let v = 0;
  for (const m of members) {
    if (own(timelines.items, m) !== undefined) v += balanceAt(timelines, m, t, true);
  }
  return v;
}

/** Rows for the detail panel of ONE block: the non-OK items worst-first
 * with every draw on their curves from the stock snapshot through this
 * block's end (resolved to board rows by key) and every open PO; plain-OK
 * items and the untracked list ride along. `stamp` only words the
 * sentence (hours print as "h95.3" without one). */
export function supplyDetail(
  block: ScheduleBlock, supply: Supply, timelines: Timelines, stock: StockArgs,
  schedule: readonly ScheduleBlock[], rules?: Partial<Rules> | null, stamp?: StampFn,
): SupplyDetail {
  const R: Rules = { ...rulesOf(stock), ...(rules ?? {}) };
  const st: StampFn = stamp ?? ((h) => `h${(Math.floor(h * 10 + 0.5) / 10).toFixed(1)}`);
  const desig = (i: string): string => own(stock.designations, i) ?? "";
  const key = blockKey(block);
  const sku = String(block.sku ?? "");
  const end = num(block.end_hour);
  const byKey = new Map<string, ScheduleBlock>();
  for (const b of schedule) byKey.set(blockKey(b), b);

  const items: DetailItem[] = [];
  const ok: SupplyDetail["ok_items"] = [];
  for (const e of sortedItems(supply)) {
    const rank = supplyRank(e);
    if (rank === 0) { ok.push({ item: e.item, designation: desig(e.item) }); continue; }
    const members = detailMembers(timelines, sku, e.item);
    const ref = R.lead_measured_from === "depletion" ? (e.depletion_h ?? num(block.start_hour)) : num(block.start_hour);

    const draws: DetailDraw[] = [];
    for (const m of members) {
      const it = own(timelines.items, m);
      if (!it) continue;
      const S = it.snapshot_h;
      for (const d of it.draws) {
        if (!(d.a < end && d.e > S)) continue;
        const b = byKey.get(d.key);
        const tb = own(timelines.blocks, d.key);
        const running = tb?.running ?? String(b?.attrs ?? "").includes("current_state:running");
        const locked = tb?.locked ?? Boolean(b?.locked || b?.completed || b?.pinned);
        const tag: DrawTag = d.key === key ? "this block" : running ? "running" : locked ? "MO locked" : "planner";
        draws.push({
          key: d.key, item: m, is_alt: m !== e.item,
          line_name: String(b?.line_name ?? tb?.line_name ?? ""),
          sku: String(b?.sku ?? tb?.sku ?? ""),
          order_id: String(b?.order_id ?? ""),
          a: d.a, e: d.e, qty: d.qty, tag,
          balance_before: groupBalance(timelines, members, d.a),
          balance_after: groupBalance(timelines, members, d.e),
        });
      }
    }
    draws.sort((x, y) => x.a - y.a || (x.key < y.key ? -1 : x.key > y.key ? 1 : 0));

    const receipts: DetailReceipt[] = [];
    for (const m of members) {
      for (const r of own(stock.receipts, m) ?? []) {
        const rh = num(r.ready_h);
        receipts.push({
          item: m, is_alt: m !== e.item, po8: String(r.po8 ?? ""), qty: num(r.qty), unit: e.unit,
          receipt_date: r.receipt_date ?? null, ready_h: rh, tier: String(r.tier ?? ""),
          lead_h: ref - rh, counted: rh < end,
          binding: e.binding !== null && e.binding.po8 === String(r.po8 ?? "")
            && Math.abs(e.binding.ready_h - rh) < 1e-9,
        });
      }
    }
    receipts.sort((x, y) => x.ready_h - y.ready_h || (x.po8 < y.po8 ? -1 : x.po8 > y.po8 ? 1 : 0));

    items.push({
      item: e.item, designation: desig(e.item), unit: e.unit, verdict: e.verdict,
      backed: e.backed, minor: e.minor, mid_run: e.mid_run, rank,
      need: e.need, opening_at_start: e.opening_at_start, covered_frac: e.covered_frac,
      depletion_h: e.depletion_h, safe_from_h: e.safe_from_h, dependent_frac: e.dependent_frac,
      sentence: itemSentence(e, st), members, draws, receipts,
    });
  }
  return { items, ok_items: ok, untracked: supply.untracked.slice() };
}

/** Items the "Supply details" button counts: SHORT, DEPENDENT (not minor)
 * and NO_DATA — a backed row is shown but is not "at risk". */
export function atRiskCount(supply: Supply | null | undefined): number {
  let n = 0;
  for (const e of supply?.items ?? []) if (supplyRank(e) >= 2) n += 1;
  return n;
}
