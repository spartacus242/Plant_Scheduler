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
): AdherenceRow[] {
  // Sum scheduled qty per order: rate(line, sku) * duration, production only.
  const schedByOrder: Record<string, number> = {};
  for (const b of schedule) {
    if (!isProduction(b)) continue;
    const rate = caps[b.line_name]?.[b.sku] ?? 0;
    const dur = Math.max(0, b.end_hour - b.start_hour);
    schedByOrder[b.order_id] = (schedByOrder[b.order_id] ?? 0) + rate * dur;
  }

  const rows: AdherenceRow[] = demand.map((d) => {
    const sq = schedByOrder[d.order_id] ?? 0;
    const pct = d.qty_min > 0 ? (sq / d.qty_min) * 100 : (sq > 0 ? 999 : 100);
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
): KpiData {
  const adherence = computeAdherence(schedule, demand, caps);
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
 * its own conflict metrics); flags physically impossible drops immediately. */
export function checkOverlapsSimple(blocks: ScheduleBlock[]): string[] {
  const byLine: Record<string, ScheduleBlock[]> = {};
  for (const b of blocks) (byLine[b.line_name] ??= []).push(b);
  const issues: string[] = [];
  for (const [ln, lb] of Object.entries(byLine)) {
    const sorted = [...lb].sort((a, b) => a.start_hour - b.start_hour);
    for (let i = 1; i < sorted.length; i++) {
      if (sorted[i].start_hour < sorted[i - 1].end_hour) {
        issues.push(`${ln}: overlap at h${sorted[i].start_hour}`);
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
