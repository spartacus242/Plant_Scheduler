// kpi.ts — Live KPI recompute, an exact port of the Gantt-KPI rules in
// helpers/scorecard_engine.py (compute_adherence / score_changeovers /
// gantt_kpis). PYTHON IS THE SOURCE OF TRUTH: the server payload
// (SandboxArgs.kpis) is rendered verbatim until the user edits; this module
// only exists so the numbers keep updating live during a what-if session.
// Changeover severity/hour rules are NOT re-implemented here — each SKU pair
// is classified by lookup in the co_pairs map computed server-side.
// tests/test_kpi_parity.py asserts this file and the Python engine agree on a
// golden fixture; change the rules in both places or not at all.

import type { ScheduleBlock, DemandTarget, AdherenceRow, KpiData, CoPairInfo } from "../types";
import { isWindowBlock } from "../types";
import { meanCapableRate } from "./rates";

/** Fallback when no co_pairs/co_default were passed (e.g. dev harness):
 * mirrors scorecard_config defaults base 0.5 + recipe 1.0. */
const CO_DEFAULT_FALLBACK: CoPairInfo = { recipe: 1, format: 0, hours: 1.5, cip_req: 0 };

/** Production only — trials produce no saleable qty and, per scorecard rules,
 * do not count as changeover transitions either (Python's _production). */
function isProduction(b: ScheduleBlock): boolean {
  return b.block_type === "sku";
}

export function computeAdherence(
  schedule: ScheduleBlock[],
  demand: DemandTarget[],
  caps: Record<string, Record<string, number>>,
  coveredByOrder?: Record<string, number>,
): AdherenceRow[] {
  // Sum scheduled qty per order. The block's own qty_kg (the solver's real
  // decomposition) wins over rate x duration. Trials are blocked hours,
  // never tonnage (user rule 2026-08-14).
  const schedByOrder: Record<string, number> = {};
  const demandIds = new Set(demand.map((d) => d.order_id));
  // committed-MO / manual blocks whose order id matches no demand order:
  // their kg still IS production of that SKU - ALWAYS waterfall it below.
  // Board kg counts from the board, no matter what credit map arrives —
  // the old covered mode skipped this and made the ledger the whole
  // number, unfalsifiable against the calendar it sat on (user mandate
  // 2026-08-21: the metrics must score calendar_blocks.csv).
  const unmatchedBySku: Record<string, number> = {};
  // EVERY block keeps its position for pass 1; a matching order id is a
  // PREFERENCE for leftover kg, not a bypass (2026-09-01: a 280256-W37 run
  // scheduled mostly in W38 hours credited W37 outright, W38 read 0%).
  const blocks: Array<{ sku: string; kg: number; s: number; e: number; own: string }> = [];
  for (const b of schedule) {
    if (!isProduction(b)) continue;
    const kg = b.qty_kg && b.qty_kg > 0
      ? b.qty_kg
      : (caps[b.line_name]?.[b.sku] ?? 0) * Math.max(0, b.end_hour - b.start_hour);
    blocks.push({ sku: b.sku, kg, s: b.start_hour, e: b.end_hour,
                  own: demandIds.has(b.order_id) ? b.order_id : "" });
  }
  // coveredByOrder is NON-BOARD kg ONLY: made kg from completed MOs the
  // board hides (built server-side with made_only=True). It only ever ADDS
  // kg no visible block represents; a map with committed-MO kg would
  // double-count — those ARE board blocks and were counted above.
  if (coveredByOrder) {
    for (const [oid, kg] of Object.entries(coveredByOrder)) {
      if (demandIds.has(oid) && kg > 0) {
        schedByOrder[oid] = (schedByOrder[oid] ?? 0) + kg;
      }
    }
  }
  const bySku: Record<string, DemandTarget[]> = {};
  for (const d of demand) (bySku[d.sku] ??= []).push(d);
  for (const skuOrders of Object.values(bySku)) {
    skuOrders.sort((a, b2) => (a.due_start_hour ?? 0) - (b2.due_start_hour ?? 0));
  }
  // Pass 1 — POSITION-AWARE (user rule 2026-09-01, exact port of
  // compute_adherence), EVERY block: it first credits same-SKU orders whose
  // due windows OVERLAP its own hours, pro-rated by overlap share of the
  // block's duration, capped at each order's max; kg the windows could not
  // take goes to the block's OWN order if it has room (the order id is the
  // planner's intent and wins where position is silent), then to the SKU
  // pool. Zero-width windows (no due hours) reduce this to the legacy
  // order-id credit.
  const capByOid: Record<string, number> = {};
  for (const d of demand) capByOid[d.order_id] = Math.max(d.qty_max, d.qty_min);
  const leftoverBySku: Record<string, number> = {};
  blocks.sort((a, b2) => a.s - b2.s || a.e - b2.e);
  for (const blk of blocks) {
    let remaining = blk.kg;
    const dur = Math.max(blk.e - blk.s, 1e-9);
    for (const d of bySku[blk.sku] ?? []) {
      const ds = d.due_start_hour ?? 0;
      const de = d.due_end_hour ?? 0;
      const ov = Math.max(0, Math.min(blk.e, de) - Math.max(blk.s, ds));
      if (ov <= 0) continue;
      const have = schedByOrder[d.order_id] ?? 0;
      const take = Math.min(blk.kg * (ov / dur), Math.max(0, capByOid[d.order_id] - have), remaining);
      if (take > 0) {
        schedByOrder[d.order_id] = have + take;
        remaining -= take;
      }
    }
    if (remaining > 1e-9 && blk.own) {
      // UNCAPPED, like the legacy direct credit: an over-booked order must
      // read OVER, not shed kg into the pool.
      schedByOrder[blk.own] = (schedByOrder[blk.own] ?? 0) + remaining;
      remaining = 0;
    }
    if (remaining > 1e-9) {
      leftoverBySku[blk.sku] = (leftoverBySku[blk.sku] ?? 0) + remaining;
    }
  }
  // Pass 2 — legacy earliest-due waterfall for what pass 1 could not place
  // positionally (plant logic: what is running now covers the nearest due).
  for (const [sku, kg0] of Object.entries(leftoverBySku)) {
    let kg = kg0;
    const orders = bySku[sku] ?? [];
    for (let i = 0; i < orders.length && kg > 1e-9; i++) {
      const d = orders[i];
      const have = schedByOrder[d.order_id] ?? 0;
      // Every order takes at most its qty_max. The old "last order takes
      // the remainder" rule dumped ALL surplus committed tonnage onto one
      // order — a W35 order read 1764% adherence because a week of W33
      // committed production of its SKU landed on it (2026-08-14). Surplus
      // beyond every open order's max is production serving demand that is
      // not on this board (already-shipped weeks) — leave it uncredited.
      const room = Math.max(0, Math.max(d.qty_max, d.qty_min) - have);
      const take = Math.min(kg, room);
      if (take > 0) {
        schedByOrder[d.order_id] = have + take;
        kg -= take;
      }
    }
  }

  // Mean capable-line rate per SKU: one rule (utils/rates, distinct lines,
  // group-expanded side keys collapse) — fix FE / audit ui-4. Python's
  // compute_adherence rows carry no rate; this field is client-only.
  const avgRate = (sku: string): number => meanCapableRate(caps, sku);

  const rows: AdherenceRow[] = demand.map((d) => {
    const sq = schedByOrder[d.order_id] ?? 0;
    // % of TARGET (the 100% point), not of qty_min. Dividing by qty_min
    // (90% of target) made a fill at the allowed 110% cap read as "122%"
    // — the planner's band is 90-110 of target (user rule 2026-08-14).
    const target = d.qty_min > 0 && d.qty_max >= d.qty_min
      ? (d.qty_min + d.qty_max) / 2
      : Math.max(d.qty_min, d.qty_max);
    const pct = target > 0 ? (sq / target) * 100 : (sq > 0 ? 999 : 100);
    // MET = between min and max inclusive
    let status: "MET" | "UNDER" | "OVER" = "MET";
    if (sq < d.qty_min) status = "UNDER";
    else if (d.qty_max > 0 && sq > d.qty_max) status = "OVER";
    return {
      order_id: d.order_id,
      sku: d.sku,
      qty_min: d.qty_min,
      qty_max: d.qty_max,
      scheduled_qty: Math.round(sq),
      pct_adherence: Math.round(pct * 10) / 10,
      status,
      avg_rate_kgph: avgRate(d.sku),
    };
  });

  // Codepoint sort, matching Python's str sort (not localeCompare).
  rows.sort((a, b) => (a.sku < b.sku ? -1 : a.sku > b.sku ? 1 : 0));
  return rows;
}

/** Target (the 100% point) for an order: the midpoint of the min/max band,
 * matching compute_adherence's pct denominator. */
export function orderTarget(qtyMin: number, qtyMax: number): number {
  return qtyMin > 0 && qtyMax >= qtyMin ? (qtyMin + qtyMax) / 2 : Math.max(qtyMin, qtyMax);
}

/** Kg an order contributes to its WEEK's fulfillment: capped at the order's
 * own target — overproduction on one order cannot serve another order's
 * demand, so excess must not raise the week's percentage (audit 2026-08-18:
 * uncapped credit read W35 96% vs 93.7% true). Mirrors the identical cap in
 * scorecard_engine.weekly_breakdown; change both or neither. */
export function weekFulfillmentCredit(row: AdherenceRow): number {
  return Math.min(row.scheduled_qty, orderTarget(row.qty_min, row.qty_max));
}

export interface ChangeoverCounts {
  total: number;
  recipe: number;
  format: number;
  hours: number;
  perLine: Record<string, number>;
  /** Flagged (cip_req) transitions with NO CIP in the gap — hygiene rule. */
  cipReqViolations: number;
  /** Transitions fully waived because a CIP sits in the gap (the plant
   * retools during the clean) — excluded from every count above. */
  transitionsAtCip: number;
}

export function countChangeovers(
  schedule: ScheduleBlock[],
  coPairs: Record<string, CoPairInfo> = {},
  coDefault: CoPairInfo = CO_DEFAULT_FALLBACK,
  cipWindows: ScheduleBlock[] = [],
): ChangeoverCounts {
  // Group by line_id (not line_name) exactly like score_changeovers; the
  // per-line display map is keyed by line_name, summed across ids sharing it.
  const byLineId: Record<number, ScheduleBlock[]> = {};
  for (const b of schedule) {
    if (!isProduction(b)) continue;
    (byLineId[b.line_id] ??= []).push(b);
  }

  // Per-line CIP intervals: a transition whose gap fully contains a CIP
  // block is WAIVED from every changeover metric (the plant retools during
  // the clean — user rule 2026-08-26), and it satisfies cip_req.
  const cipsByLineId: Record<number, [number, number][]> = {};
  for (const c of cipWindows) {
    if (c.block_type !== "cip") continue;
    (cipsByLineId[c.line_id] ??= []).push([c.start_hour, c.end_hour]);
  }
  const cipBetween = (lineId: number, aEnd: number, bStart: number): boolean =>
    (cipsByLineId[lineId] ?? []).some(
      ([cs, ce]) => cs >= aEnd - 1e-6 && ce <= bStart + 1e-6,
    );

  const perLine: Record<string, number> = {};
  let total = 0;
  let recipe = 0;
  let format = 0;
  let hours = 0;
  let cipReqViolations = 0;
  let transitionsAtCip = 0;
  for (const blocks of Object.values(byLineId)) {
    const sorted = [...blocks].sort((a, b) => a.start_hour - b.start_hour);
    const lineName = sorted[0].line_name ?? "";
    let lineCount = 0;
    for (let i = 1; i < sorted.length; i++) {
      const a = sorted[i - 1];
      const b = sorted[i];
      const from = a.sku;
      const to = b.sku;
      if (from === to) continue;
      const pair = coPairs[`${from}|${to}`] ?? coDefault;
      if (cipBetween(a.line_id, a.end_hour, b.start_hour)) {
        // Fully waived: the clean subsumes the changeover work.
        transitionsAtCip++;
        continue;
      }
      if ((pair.cip_req ?? 0) === 1) cipReqViolations++;
      total++;
      lineCount++;
      recipe += pair.recipe;
      format += pair.format;
      hours += pair.hours;
    }
    perLine[lineName] = (perLine[lineName] ?? 0) + lineCount;
  }
  return {
    total, recipe, format,
    hours: Math.round(hours * 100) / 100,
    perLine, cipReqViolations, transitionsAtCip,
  };
}

/** One ISO week's changeover counts — the `weekly` rows score_changeovers
 * returns when given week_bounds (fix FE / audit ui-5, C30). Field names
 * are the Python row's. */
export interface WeekChangeovers {
  /** index into the marks array (bounds[i]) */
  idx: number;
  start_h: number;
  span_h: number;
  /** production hours inside the week (overlap share) */
  prod_h: number;
  sku_transitions: number;
  recipe_changes: number;
  format_changes: number;
  topload_changes: number;
  ffs_changes: number;
  casepacker_changes: number;
  ttp_changes: number;
  recipe_only_changes: number;
  co_hours: number;
  /** cip_req-flagged transitions with no CIP in the gap (hygiene) */
  cip_req_violations: number;
  /** the first violating transition of the week — where a clean should go */
  cip_gap: { lineName: string; lineId: number; start: number } | null;
}

/**
 * Exact port of score_changeovers(..., week_bounds, horizon_h)["weekly"]:
 * every transition buckets into the week the INCOMING block starts
 * (bisect_right(marks, start) - 1, clamped at 0), a transition whose gap
 * fully contains a CIP window is WAIVED from every count (the plant retools
 * during the clean — user rule 2026-08-26), a cip_req pair with no CIP in
 * the gap is a violation but still counts as a transition, and a pair that
 * touches no machine is recipe-only. `weekMarks` are the week start hours
 * (marks[0] = 0, then the Monday boundaries — scorecard _iso_week_bounds);
 * `horizonH` closes the last week. Only weeks with at least one transition
 * or production hour are returned, like the Python rows (wk_acc).
 * tests/test_kpi_parity.py asserts this against the Python rows.
 */
export function countChangeoversByWeek(
  schedule: ScheduleBlock[],
  coPairs: Record<string, CoPairInfo> = {},
  coDefault: CoPairInfo = CO_DEFAULT_FALLBACK,
  cipWindows: ScheduleBlock[] = [],
  weekMarks: number[] = [0],
  horizonH?: number,
): WeekChangeovers[] {
  const marks = weekMarks.length ? weekMarks : [0];
  const wkEnd = (i: number): number => {
    if (i + 1 < marks.length) return marks[i + 1];
    if (horizonH !== undefined) return horizonH;
    return marks[i] + 168;
  };
  // bisect_right(marks, h) - 1, clamped at 0
  const wkOf = (h: number): number => {
    let i = 0;
    while (i < marks.length && marks[i] <= h) i++;
    return Math.max(0, i - 1);
  };
  const acc: Record<number, WeekChangeovers> = {};
  const get = (i: number): WeekChangeovers =>
    (acc[i] ??= {
      idx: i, start_h: marks[i], span_h: wkEnd(i) - marks[i], prod_h: 0,
      sku_transitions: 0, recipe_changes: 0, format_changes: 0,
      topload_changes: 0, ffs_changes: 0, casepacker_changes: 0, ttp_changes: 0,
      recipe_only_changes: 0, co_hours: 0, cip_req_violations: 0, cip_gap: null,
    });

  const byLineId: Record<number, ScheduleBlock[]> = {};
  for (const b of schedule) {
    if (!isProduction(b)) continue;
    (byLineId[b.line_id] ??= []).push(b);
  }
  const cipsByLineId: Record<number, [number, number][]> = {};
  for (const c of cipWindows) {
    if (c.block_type !== "cip") continue;
    (cipsByLineId[c.line_id] ??= []).push([c.start_hour, c.end_hour]);
  }
  const cipBetween = (lineId: number, aEnd: number, bStart: number): boolean =>
    (cipsByLineId[lineId] ?? []).some(([cs, ce]) => cs >= aEnd - 1e-6 && ce <= bStart + 1e-6);

  for (const blocks of Object.values(byLineId)) {
    const sorted = [...blocks].sort((a, b) => a.start_hour - b.start_hour);
    for (let i = 1; i < sorted.length; i++) {
      const a = sorted[i - 1];
      const b = sorted[i];
      if (a.sku === b.sku) continue;
      if (cipBetween(a.line_id, a.end_hour, b.start_hour)) continue; // waived
      const pair = coPairs[`${a.sku}|${b.sku}`] ?? coDefault;
      const w = get(wkOf(b.start_hour));
      w.sku_transitions += 1;
      w.recipe_changes += pair.recipe ? 1 : 0;
      w.format_changes += pair.format ? 1 : 0;
      const tl = (pair.tl ?? 0) === 1 ? 1 : 0;
      const ffs = (pair.ffs ?? 0) === 1 ? 1 : 0;
      const cp = (pair.cp ?? 0) === 1 ? 1 : 0;
      const ttp = (pair.ttp ?? 0) === 1 ? 1 : 0;
      w.topload_changes += tl;
      w.ffs_changes += ffs;
      w.casepacker_changes += cp;
      w.ttp_changes += ttp;
      if (!(tl || ffs || cp || ttp)) w.recipe_only_changes += 1;
      w.co_hours += pair.hours;
      if ((pair.cip_req ?? 0) === 1) {
        w.cip_req_violations += 1;
        if (!w.cip_gap) w.cip_gap = { lineName: a.line_name, lineId: a.line_id, start: a.end_hour };
      }
    }
  }
  // production hours per week (overlap share)
  for (const b of schedule) {
    if (!isProduction(b)) continue;
    for (let i = 0; i < marks.length; i++) {
      const ov = Math.max(0, Math.min(b.end_hour, wkEnd(i)) - Math.max(b.start_hour, marks[i]));
      if (ov > 0) get(i).prod_h += ov;
    }
  }
  const out = Object.keys(acc).map((k) => acc[Number(k)]).sort((x, y) => x.idx - y.idx);
  for (const w of out) {
    w.co_hours = Math.round(w.co_hours * 100) / 100;
    w.prod_h = Math.round(w.prod_h * 100) / 100;
    w.start_h = Math.round(w.start_h * 100) / 100;
    w.span_h = Math.round(w.span_h * 100) / 100;
  }
  return out;
}

export function computeKpis(
  schedule: ScheduleBlock[],
  cipWindows: ScheduleBlock[],
  demand: DemandTarget[],
  caps: Record<string, Record<string, number>>,
  coPairs: Record<string, CoPairInfo> = {},
  coDefault: CoPairInfo = CO_DEFAULT_FALLBACK,
  coveredByOrder?: Record<string, number>,
): KpiData {
  const adherence = computeAdherence(schedule, demand, caps, coveredByOrder);
  const met = adherence.filter((r) => r.status === "MET").length;
  const pct = adherence.length > 0 ? Math.round((met / adherence.length) * 1000) / 10 : 100;
  const co = countChangeovers(schedule, coPairs, coDefault, cipWindows);
  return {
    pctAdherence: pct,
    ordersMet: met,
    ordersTotal: adherence.length,
    totalChangeovers: co.total,
    recipeChanges: co.recipe,
    formatChanges: co.format,
    totalCoHours: co.hours,
    perLineChangeovers: co.perLine,
    overlaps: checkOverlapsSimple([...schedule, ...cipWindows]),
  };
}

/** Client-only diagnostic — not part of the Python payload (the scorecard has
 * its own conflict metrics); flags physically impossible drops immediately.
 * Only pairs involving PRODUCTION (or a trial) count: two windows lying on
 * top of each other — a projected CIP inside a line-down span, overlapping
 * maintenance — is a display fact, not a scheduling error (user report
 * 2026-08-18: "2 overlaps on P11 but P11 is down" — the CIP markers). */
export function checkOverlapsSimple(blocks: ScheduleBlock[]): string[] {
  const byLine: Record<string, ScheduleBlock[]> = {};
  for (const b of blocks) (byLine[b.line_name] ??= []).push(b);
  const issues: string[] = [];
  for (const [ln, lb] of Object.entries(byLine)) {
    const sorted = [...lb].sort((a, b) => a.start_hour - b.start_hour);
    for (let i = 0; i < sorted.length; i++) {
      for (let j = i + 1; j < sorted.length; j++) {
        const a = sorted[i];
        const b = sorted[j];
        if (b.start_hour >= a.end_hour - 1e-9) break; // sorted: no later hit
        if (isWindowBlock(a.block_type) && isWindowBlock(b.block_type)) {
          continue; // window-on-window: harmless
        }
        const nm = (x: ScheduleBlock) => x.sku || x.label || x.block_type;
        issues.push(`${ln}: ${nm(b)} overlaps ${nm(a)} at h${Math.round(b.start_hour)}`);
      }
    }
  }
  return issues;
}

/** Adapt the server payload to the KpiData shape; overlaps stay client-side. */
export function serverKpisToKpiData(
  server: {
    pct_adherence: number;
    orders_met: number;
    orders_total: number;
    changeovers: {
      recipe_changes: number;
      format_changes: number;
      total_co_hours: number;
      sku_transitions: number;
    };
    per_line_changeovers: Record<string, number>;
  },
  overlaps: string[],
): KpiData {
  return {
    pctAdherence: server.pct_adherence,
    ordersMet: server.orders_met,
    ordersTotal: server.orders_total,
    totalChangeovers: server.changeovers.sku_transitions,
    recipeChanges: server.changeovers.recipe_changes,
    formatChanges: server.changeovers.format_changes,
    totalCoHours: server.changeovers.total_co_hours,
    perLineChangeovers: server.per_line_changeovers,
    overlaps,
  };
}
