// kpi.ts — Client-side KPI computation (mirrors sandbox_engine.py).

import type { ScheduleBlock, DemandTarget, AdherenceRow, KpiData } from "../types";
import { isWindowBlock } from "../types";

export function computeAdherence(
  schedule: ScheduleBlock[],
  demand: DemandTarget[],
  caps: Record<string, Record<string, number>>,
): AdherenceRow[] {
  // Sum scheduled qty per order. The block's own qty_kg (the solver's real
  // decomposition) wins over rate x hours. Trials are blocked hours, never
  // tonnage (user rule 2026-08-14).
  const schedByOrder: Record<string, number> = {};
  const demandIds = new Set(demand.map((d) => d.order_id));
  // committed-MO / manual blocks whose order id matches no demand order:
  // their kg still IS production of that SKU - waterfall it below.
  const unmatchedBySku: Record<string, number> = {};
  for (const b of schedule) {
    if (isWindowBlock(b.block_type) || b.is_trial || b.block_type === "trial") continue;
    const kg = b.qty_kg && b.qty_kg > 0
      ? b.qty_kg
      : (caps[b.line_name]?.[b.sku] ?? 0) * b.run_hours;
    if (demandIds.has(b.order_id)) {
      schedByOrder[b.order_id] = (schedByOrder[b.order_id] ?? 0) + kg;
    } else {
      unmatchedBySku[b.sku] = (unmatchedBySku[b.sku] ?? 0) + kg;
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
      const room = i === orders.length - 1
        ? kg  // last order takes the remainder
        : Math.max(0, Math.max(d.qty_max, d.qty_min) - have);
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
    // Show actual % of target (no cap at 100)
    const pct = d.qty_min > 0 ? (sq / d.qty_min) * 100 : (sq > 0 ? 999 : 100);
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

  rows.sort((a, b) => a.sku.localeCompare(b.sku));
  return rows;
}

export function countChangeovers(
  schedule: ScheduleBlock[],
): { total: number; perLine: Record<string, number> } {
  const byLine: Record<string, ScheduleBlock[]> = {};
  for (const b of schedule) {
    if (isWindowBlock(b.block_type)) continue;
    (byLine[b.line_name] ??= []).push(b);
  }

  const perLine: Record<string, number> = {};
  let total = 0;
  for (const [ln, blocks] of Object.entries(byLine)) {
    const sorted = [...blocks].sort((a, b) => a.start_hour - b.start_hour);
    let count = 0;
    for (let i = 1; i < sorted.length; i++) {
      if (sorted[i].sku !== sorted[i - 1].sku) count++;
    }
    perLine[ln] = count;
    total += count;
  }
  return { total, perLine };
}

export function computeKpis(
  schedule: ScheduleBlock[],
  cipWindows: ScheduleBlock[],
  demand: DemandTarget[],
  caps: Record<string, Record<string, number>>,
): KpiData {
  const adherence = computeAdherence(schedule, demand, caps);
  const met = adherence.filter((r) => r.status === "MET").length;
  const pct = adherence.length > 0 ? Math.round((met / adherence.length) * 1000) / 10 : 100;
  const { total, perLine } = countChangeovers(schedule);
  const allBlocks = [...schedule, ...cipWindows];
  const overlaps = checkOverlapsSimple(allBlocks);
  return {
    pctAdherence: pct,
    ordersMet: met,
    ordersTotal: adherence.length,
    totalChangeovers: total,
    perLineChangeovers: perLine,
    overlaps,
  };
}

function checkOverlapsSimple(blocks: ScheduleBlock[]): string[] {
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
