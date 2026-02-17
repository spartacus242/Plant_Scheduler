// kpi.ts — Client-side KPI computation (mirrors sandbox_engine.py).

import type { ScheduleBlock, DemandTarget, AdherenceRow, KpiData } from "../types";

export function computeAdherence(
  schedule: ScheduleBlock[],
  demand: DemandTarget[],
  caps: Record<string, Record<string, number>>,
): AdherenceRow[] {
  // Sum scheduled qty per order
  const schedByOrder: Record<string, number> = {};
  for (const b of schedule) {
    if (b.block_type === "cip") continue;
    const rate = caps[b.line_name]?.[b.sku] ?? 0;
    schedByOrder[b.order_id] = (schedByOrder[b.order_id] ?? 0) + rate * b.run_hours;
  }

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
    if (b.block_type === "cip") continue;
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
