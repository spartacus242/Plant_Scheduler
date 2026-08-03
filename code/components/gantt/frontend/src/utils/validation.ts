// validation.ts — Client-side validation for sandbox operations.

import type { ScheduleBlock } from "../types";

export function isCapable(
  lineName: string,
  sku: string,
  caps: Record<string, Record<string, number>>,
): boolean {
  return (caps[lineName]?.[sku] ?? 0) > 0;
}

export function getRate(
  lineName: string,
  sku: string,
  caps: Record<string, Record<string, number>>,
): number {
  return caps[lineName]?.[sku] ?? 0;
}

export function recalcDuration(
  block: ScheduleBlock,
  newLine: string,
  caps: Record<string, Record<string, number>>,
): number | null {
  const oldRate = getRate(block.line_name, block.sku, caps);
  const newRate = getRate(newLine, block.sku, caps);
  if (newRate <= 0) return null;
  if (oldRate <= 0) return block.run_hours;
  const qty = oldRate * block.run_hours;
  return Math.ceil(qty / newRate);
}

export function checkOverlaps(blocks: ScheduleBlock[]): string[] {
  const byLine: Record<string, ScheduleBlock[]> = {};
  for (const b of blocks) {
    (byLine[b.line_name] ??= []).push(b);
  }
  const issues: string[] = [];
  for (const [ln, lineBlocks] of Object.entries(byLine)) {
    const sorted = [...lineBlocks].sort((a, b) => a.start_hour - b.start_hour);
    for (let i = 1; i < sorted.length; i++) {
      if (sorted[i].start_hour < sorted[i - 1].end_hour) {
        issues.push(
          `${ln}: ${sorted[i - 1].order_id} (ends h${sorted[i - 1].end_hour}) overlaps ${sorted[i].order_id} (starts h${sorted[i].start_hour})`,
        );
      }
    }
  }
  return issues;
}

export function findOverlapsOnLine(
  blocks: ScheduleBlock[],
  lineName: string,
  excludeId: string,
  startHour: number,
  endHour: number,
): boolean {
  return blocks.some(
    (b) =>
      b.line_name === lineName &&
      b.id !== excludeId &&
      b.start_hour < endHour &&
      b.end_hour > startHour,
  );
}
