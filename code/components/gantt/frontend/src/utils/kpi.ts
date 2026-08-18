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

/** Fallback when no co_pairs/co_default were passed (e.g. dev harness):
 * mirrors scorecard_config defaults base 0.5 + recipe 1.0. */
const CO_DEFAULT_FALLBACK: CoPairInfo = { recipe: 1, format: 0, hours: 1.5 };

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
  // their kg still IS production of that SKU - waterfall it below.
  const unmatchedBySku: Record<string, number> = {};
  for (const b of schedule) {
    if (!isProduction(b)) continue;
    const kg = b.qty_kg && b.qty_kg > 0
      ? b.qty_kg
      : (caps[b.line_name]?.[b.sku] ?? 0) * Math.max(0, b.end_hour - b.start_hour);
    if (demandIds.has(b.order_id)) {
      schedByOrder[b.order_id] = (schedByOrder[b.order_id] ?? 0) + kg;
    } else if (!coveredByOrder) {
      unmatchedBySku[b.sku] = (unmatchedBySku[b.sku] ?? 0) + kg;
    }
  }
  // Coverage-ledger baseline (committed MOs + kg already MADE by hidden
  // completed blocks). The SKU waterfall is disabled in this mode — the
  // ledger already credits committed MOs, and finished production never
  // reaches the board at all (user report 2026-08-18: W34 read 20.8%
  // while physically full).
  if (coveredByOrder) {
    for (const [oid, kg] of Object.entries(coveredByOrder)) {
      if (demandIds.has(oid) && kg > 0) {
        schedByOrder[oid] = (schedByOrder[oid] ?? 0) + kg;
      }
    }
  }
  // Waterfall: committed production credits the EARLIEST-due open order of
  // its SKU first (plant logic: what is running now covers the nearest due),
  // spilling forward; any remainder lands on the last order of that SKU.
  const bySku: Record<string, DemandTarget[]> = {};
  for (const d of demand) (bySku[d.sku] ??= []).push(d);
  for (const skuOrders of Object.values(bySku)) {
    skuOrders.sort((a, b2) => (a.due_start_hour ?? 0) - (b2.due_start_hour ?? 0));
  }
  for (const [sku, kg0] of Object.entries(unmatchedBySku)) {
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

  // Mean capable-line rate per SKU (only capable lines with rate > 0)
  const avgRateBySku: Record<string, { sum: number; n: number }> = {};
  for (const lineName of Object.keys(caps)) {
    for (const [sku, rate] of Object.entries(caps[lineName])) {
      if (rate > 0) {
        const arr = avgRateBySku[sku] ?? { sum: 0, n: 0 };
        arr.sum += rate;
        arr.n += 1;
        avgRateBySku[sku] = arr;
      }
    }
  }
  const avgRate = (sku: string): number => {
    const arr = avgRateBySku[sku];
    return arr && arr.n > 0 ? arr.sum / arr.n : 0;
  };

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

export interface ChangeoverCounts {
  total: number;
  recipe: number;
  format: number;
  hours: number;
  perLine: Record<string, number>;
}

export function countChangeovers(
  schedule: ScheduleBlock[],
  coPairs: Record<string, CoPairInfo> = {},
  coDefault: CoPairInfo = CO_DEFAULT_FALLBACK,
): ChangeoverCounts {
  // Group by line_id (not line_name) exactly like score_changeovers; the
  // per-line display map is keyed by line_name, summed across ids sharing it.
  const byLineId: Record<number, ScheduleBlock[]> = {};
  for (const b of schedule) {
    if (!isProduction(b)) continue;
    (byLineId[b.line_id] ??= []).push(b);
  }

  const perLine: Record<string, number> = {};
  let total = 0;
  let recipe = 0;
  let format = 0;
  let hours = 0;
  for (const blocks of Object.values(byLineId)) {
    const sorted = [...blocks].sort((a, b) => a.start_hour - b.start_hour);
    const lineName = sorted[0].line_name ?? "";
    let lineCount = 0;
    for (let i = 1; i < sorted.length; i++) {
      const from = sorted[i - 1].sku;
      const to = sorted[i].sku;
      if (from === to) continue;
      total++;
      lineCount++;
      const pair = coPairs[`${from}|${to}`] ?? coDefault;
      recipe += pair.recipe;
      format += pair.format;
      hours += pair.hours;
    }
    perLine[lineName] = (perLine[lineName] ?? 0) + lineCount;
  }
  return { total, recipe, format, hours: Math.round(hours * 100) / 100, perLine };
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
  const co = countChangeovers(schedule, coPairs, coDefault);
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
