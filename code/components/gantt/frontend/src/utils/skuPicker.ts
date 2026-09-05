// skuPicker.ts — pure math for the blank-space "add a SKU" picker.
//
// Right-clicking an empty gap on a line lists demand-plan SKUs the line can
// run. Placing one SNAPS LEFT: the new block starts at the previous block's
// end plus the required changeover setup (or at the later of hour 0 / the
// wall clock / the lock boundary when the line is open to the left), and its
// duration is capped so the block never overlaps the next block or undercuts
// the setup time toward it.
//
// CO_FLAG_BITS mirrors helpers/calendar_io.CO_FLAG_COLUMNS (bit i = column i);
// change both or neither. tests/test_sku_picker_math.py pins this module
// under node via the frontend's own tsc.

import type { ScheduleBlock, DemandTarget, AdherenceRow, CoPairInfo } from "../types";
import { isWindowBlock } from "../types";
import { hoursForQty, qtyOverWindow, type SideDowntime } from "./abLines";
import { isPastDemandWeek } from "./layout";
import { orderTarget } from "./kpi";

export const CIP_REQ_LABEL = "CIP req";

export const CO_FLAG_BITS = [
  { bit: 1, label: "ttp" },
  { bit: 2, label: "ffs" },
  { bit: 4, label: "tpld" },
  { bit: 8, label: "cspkr" },
  { bit: 16, label: "conv-org" },
  { bit: 32, label: "cin-non" },
  // 7th column when the server adds cip_req_after to CO_FLAG_COLUMNS (fix
  // FE / audit changeover-9, C37); until then the chip comes from
  // kpis.co_pairs[pair].cip_req (coFlagsForPair's coPairs argument).
  { bit: 64, label: CIP_REQ_LABEL },
] as const;

/** Human chips for a changeover bitmask, e.g. 6 -> ["ffs", "tpld"]. */
export function coFlagLabels(mask: number | undefined): string[] {
  const m = mask ?? 0;
  return CO_FLAG_BITS.filter((f) => m & f.bit).map((f) => f.label);
}

/** Chips for the FROM|TO transition, or null when the pair is absent from
 * coFlags — build_co_flags only covers demand-plan pairs, so a missing key
 * means UNKNOWN, not clean (the chip renders "?"). A same-SKU "transition"
 * is no changeover at all: honestly clean regardless of the table.
 * `coPairs` (kpis.co_pairs, the scorecard's per-pair classification over
 * the FULL standards file) adds the "CIP req" chip and, for a pair the
 * coFlags table lacks, its machine flags — so a cip_req pair is never
 * shown as a 1-3 h gap the solver would refuse (audit changeover-9). */
export function coFlagsForPair(
  coFlags: Record<string, number> | undefined,
  from: string,
  to: string,
  coPairs?: Record<string, CoPairInfo> | null,
): string[] | null {
  if (from === to) return [];
  const key = `${from}|${to}`;
  const mask = coFlags?.[key];
  const pair = coPairs?.[key];
  let labels: string[] | null = mask === undefined ? null : coFlagLabels(mask);
  if (labels === null && pair) {
    labels = [];
    if ((pair.ttp ?? 0) === 1) labels.push("ttp");
    if ((pair.ffs ?? 0) === 1) labels.push("ffs");
    if ((pair.tl ?? 0) === 1) labels.push("tpld");
    if ((pair.cp ?? 0) === 1) labels.push("cspkr");
  }
  if (labels !== null && pair && (pair.cip_req ?? 0) === 1 && !labels.includes(CIP_REQ_LABEL)) {
    labels.push(CIP_REQ_LABEL);
  }
  return labels;
}

/**
 * The setup hours the SOLVER reserves for a transition (fix FE / audit C36
 * + changeover-9, C37): the standard rounded UP to whole hours
 * (changeover_cache.round_setup_hours — a quarter-hour standard must never
 * be short-changed), and at least the CIP duration when the pair is
 * cip_req_after (apply_cip_req_setup_floor: the clean is modeled as TIME).
 * The picker's snap-left start, its "+Nh" caption, the setup warning and
 * the drop room all price with this, so a planner never plans a 1-3 h gap
 * for a pair the solver then refuses. `rawSetupH` is the matrix value
 * (already whole hours from load_changeover_setup_nested; ceil is
 * idempotent there and guards an older payload).
 */
export function effectiveSetupHours(
  rawSetupH: number | null | undefined,
  cipReq: boolean,
  cipDurationH: number,
): number {
  const raw = Number(rawSetupH ?? 0);
  const base = Number.isFinite(raw) && raw > 0 ? Math.ceil(raw - 1e-9) : 0;
  const floor = cipReq ? Math.max(0, Number(cipDurationH) || 0) : 0;
  return Math.max(base, floor);
}

/** True when kpis.co_pairs flags FROM|TO as needing a clean in between. */
export function isCipReqPair(
  coPairs: Record<string, CoPairInfo> | null | undefined, from: string, to: string,
): boolean {
  if (from === to) return false;
  return (coPairs?.[`${from}|${to}`]?.cip_req ?? 0) === 1;
}

const EPS = 1e-9;
const round1 = (v: number) => Math.round(v * 10) / 10;
// Snap UP onto the 0.1h grid (EPS keeps values already on the grid put).
const ceil1 = (v: number) => Math.ceil(v * 10 - EPS) / 10;

export interface PlacementPlan {
  startHour: number;
  durationH: number;
  qtyKg: number;
  /** SKU left/right of the gap (null when the neighbour is a window block or
   * the line is open on that side) — drives the changeover chips. */
  prevSku: string | null;
  nextSku: string | null;
  /** block_type of the neighbour (null = line open on that side), so the
   * picker can say "after CIP" instead of pretending the line starts there. */
  prevType: string | null;
  nextType: string | null;
  setupBeforeH: number;
  setupAfterH: number;
  /** null = placeable; otherwise why not (row renders disabled). */
  reason: string | null;
}

type GapBlock = Pick<ScheduleBlock, "sku" | "start_hour" | "end_hour" | "block_type">;

/**
 * Snap-left placement of `sku` into the gap around `clickHour`.
 *
 *   start    = max(prev.end + setup(prev→sku), 0, nowH, lockedThroughH),
 *              snapped UP onto the 0.1h grid — nowH is a wall clock and
 *              block edges can sit anywhere, but every boundary derived
 *              from the start (order-split segments) rounds to 0.1h, and
 *              an off-grid start lets a rounded boundary land before its
 *              sibling's end (self-overlap).
 *   endLimit = next ? next.start − setup(sku→next) : horizon
 *   duration = min(endLimit − start, max(minRunH, hours for remainingKg))
 *   qty      = kg the line physically makes over [start, start+duration]
 *              (half-rate downtime stretches integrate via abLines)
 *
 * Window blocks (CIP & co) absorb any changeover: setup 0, same rule as
 * GanttSandbox.setupBetween.
 */
export function planPlacement(a: {
  clickHour: number;
  sku: string;
  /** Whole-line rate (group rate on a double line). */
  rate: number;
  remainingKg: number;
  /** Every block sharing the clicked row. */
  blocksOnLine: GapBlock[];
  changeovers: Record<string, Record<string, number>>;
  lockedThroughH: number | null;
  /** Wall-clock hours since anchor — nothing gets placed in the past. */
  nowH: number;
  horizonH: number;
  minRunH: number;
  /** groupOf(line) — drives the half-rate downtime integration. */
  lineGroup: string;
  downtime?: SideDowntime;
  /** Setup hours FROM -> TO. Default: the `changeovers` matrix value
   * rounded UP to whole hours (effectiveSetupHours without a cip_req
   * floor); the sandbox passes its own rule with the cip_req floor. */
  setupFor?: (from: string, to: string) => number;
}): PlacementPlan {
  const empty: PlacementPlan = {
    startHour: 0, durationH: 0, qtyKg: 0, prevSku: null, nextSku: null,
    prevType: null, nextType: null, setupBeforeH: 0, setupAfterH: 0,
    reason: null,
  };
  if (a.rate <= 0) return { ...empty, reason: "No rate for this SKU on this line" };

  const sorted = [...a.blocksOnLine].sort((x, y) => x.start_hour - y.start_hour);
  const inside = sorted.find(
    (b) => b.start_hour - EPS <= a.clickHour && a.clickHour < b.end_hour - EPS,
  );
  if (inside) {
    return { ...empty, reason: `Not empty space — ${inside.sku || inside.block_type} sits here` };
  }
  const prev = sorted.filter((b) => b.end_hour <= a.clickHour + EPS).pop();
  const next = sorted.find((b) => b.start_hour >= a.clickHour - EPS);

  const setupH = (from: string, to: string): number => {
    if (from === to) return 0;
    if (a.setupFor) return a.setupFor(from, to);
    return effectiveSetupHours(a.changeovers?.[from]?.[to], false, 0);
  };
  const setupBeforeH =
    prev && !isWindowBlock(prev.block_type) ? setupH(prev.sku, a.sku) : 0;
  const setupAfterH =
    next && !isWindowBlock(next.block_type) ? setupH(a.sku, next.sku) : 0;
  const prevSku = prev && !isWindowBlock(prev.block_type) ? prev.sku : null;
  const nextSku = next && !isWindowBlock(next.block_type) ? next.sku : null;

  const floor = Math.max(0, a.nowH, a.lockedThroughH ?? 0);
  // ceil, not round: the snapped start must never precede the legal start.
  const startHour = ceil1(Math.max(prev ? prev.end_hour + setupBeforeH : 0, floor));
  const endLimit = Math.min(
    next ? next.start_hour - setupAfterH : a.horizonH,
    a.horizonH,
  );
  const roomH = endLimit - startHour;
  const base: PlacementPlan = {
    ...empty, startHour, prevSku, nextSku,
    prevType: prev ? prev.block_type : null,
    nextType: next ? next.block_type : null,
    setupBeforeH, setupAfterH,
  };
  if (roomH < a.minRunH - EPS) {
    return {
      ...base,
      reason: `No room: ${Math.max(0, round1(roomH))}h free after setup, ` +
        `need ≥ ${a.minRunH}h`,
    };
  }
  if (a.remainingKg <= 0) return { ...base, reason: "No remaining demand" };

  const dt = a.downtime ?? {};
  const idealH =
    hoursForQty(a.lineGroup, a.remainingKg, startHour, dt, a.rate) ??
    a.remainingKg / a.rate;
  const durRaw = Math.min(roomH, Math.max(a.minRunH, idealH));
  // Round to 0.1h but never past the room (a round-up would eat setup time).
  const durationH = Math.min(round1(durRaw), Math.floor(roomH * 10 + EPS) / 10);
  const qtyKg = round1(
    qtyOverWindow(a.lineGroup, startHour, startHour + durationH, dt, a.rate),
  );
  return { ...base, durationH, qtyKg };
}

/** Remaining demand kg per SKU over CURRENT/FUTURE weeks: target minus
 * scheduled (covered credit is already inside the adherence rows), floored
 * at 0 per order — the same rules as the block popup's demandLeft list. */
export function remainingDemandBySku(
  adherence: AdherenceRow[],
  anchor: Date,
  now: Date = new Date(),
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of adherence) {
    if (isPastDemandWeek(r.order_id, anchor, now)) continue;
    const left = Math.max(0, orderTarget(r.qty_min, r.qty_max) - r.scheduled_qty);
    out[r.sku] = (out[r.sku] ?? 0) + left;
  }
  return out;
}

export interface PlacementSegment {
  orderId: string;
  startHour: number;
  durationH: number;
  qtyKg: number;
}

/** Split a placed run into contiguous per-order segments, earliest-due
 * CURRENT/FUTURE order first, each taking at most what it still needs
 * (target − scheduled; covered credit is already inside the adherence rows).
 * Any overrun stays on the last touched order. The blocks must carry real
 * demand order ids — adherence and the holding rebuild credit per order, so
 * one big block on one order would leave the other weeks' cards standing. */
export function splitPlacementByOrders(
  sku: string,
  plan: { startHour: number; durationH: number; qtyKg: number },
  adherence: AdherenceRow[],
  demand: DemandTarget[],
  anchor: Date,
  now: Date = new Date(),
): PlacementSegment[] {
  if (plan.durationH <= EPS || plan.qtyKg <= EPS) return [];
  const bySkuId: Record<string, AdherenceRow> = {};
  for (const r of adherence) bySkuId[r.order_id] = r;
  const cands = demand
    .filter((d) => d.sku === sku && !isPastDemandWeek(d.order_id, anchor, now))
    .sort((x, y) => (x.due_start_hour ?? 0) - (y.due_start_hour ?? 0));
  if (!cands.length) return [];
  let qtyLeft = plan.qtyKg;
  const takes: { orderId: string; qty: number }[] = [];
  for (const d of cands) {
    if (qtyLeft <= EPS) break;
    const scheduled = bySkuId[d.order_id]?.scheduled_qty ?? 0;
    const room = Math.max(0, orderTarget(d.qty_min, d.qty_max) - scheduled);
    const take = Math.min(qtyLeft, room);
    if (take > EPS) {
      takes.push({ orderId: d.order_id, qty: take });
      qtyLeft -= take;
    }
  }
  // Every open order full (min-run overshoot, or a race with another edit):
  // the whole run lands on the last open order rather than vanishing.
  if (!takes.length) takes.push({ orderId: cands[cands.length - 1].order_id, qty: 0 });
  if (qtyLeft > EPS) takes[takes.length - 1].qty += qtyLeft;
  // Contiguous segments; boundaries at cumulative qty share, rounded to 0.1h
  // with the LAST boundary pinned to the plan's exact end (no drift).
  const total = takes.reduce((s, t) => s + t.qty, 0) || 1;
  const segs: PlacementSegment[] = [];
  let cursor = plan.startHour;
  let cum = 0;
  for (let i = 0; i < takes.length; i++) {
    cum += takes[i].qty;
    const boundary = i === takes.length - 1
      ? plan.startHour + plan.durationH
      : round1(plan.startHour + plan.durationH * (cum / total));
    const durationH = round1(boundary - cursor);
    if (durationH > EPS) {
      segs.push({
        orderId: takes[i].orderId,
        startHour: cursor,
        durationH,
        qtyKg: round1(takes[i].qty),
      });
      // Monotonic advance: with an off-grid cursor, round1 can pull the
      // next start BEFORE this segment's end — siblings must never overlap.
      const segEnd = cursor + durationH;
      cursor = Math.max(round1(segEnd), segEnd);
    }
  }
  return segs;
}
