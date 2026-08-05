// validation.ts — Client-side validation for sandbox operations.

import type { ScheduleBlock } from "../types";
import type { SideDowntime } from "./abLines";
import { groupOf, groupRate, hoursForQty, qtyOverWindow } from "./abLines";

export function isCapable(
  lineName: string,
  sku: string,
  caps: Record<string, Record<string, number>>,
): boolean {
  return getRate(lineName, sku, caps) > 0;
}

/**
 * Whole-line rate (kg/h) for a SKU on a line. For a Bossar double line this is
 * the BOTH-SIDES-UP rate, whether capabilities_rates.csv is keyed by the group
 * ("P17") or by the halved per-side rows ("P17A"/"P17B").
 */
export function getRate(
  lineName: string,
  sku: string,
  caps: Record<string, Record<string, number>>,
): number {
  const direct = caps[lineName]?.[sku] ?? 0;
  if (direct > 0) return direct;
  return groupRate(caps, lineName, sku);
}

/**
 * Duration of `block` if it moved to `newLine` starting at `startHour`.
 *
 * The block's quantity is recovered from what it actually produces where it
 * sits today (which already accounts for any one-sided hours it crosses), then
 * re-integrated hour by hour on the target line: full rate with both sides up,
 * half rate with one side down, zero with both down. With no downtime this
 * reduces exactly to the old qty / rate calculation.
 */
export function recalcDuration(
  block: ScheduleBlock,
  newLine: string,
  caps: Record<string, Record<string, number>>,
  downtime: SideDowntime = {},
  startHour?: number,
): number | null {
  const oldRate = getRate(block.line_name, block.sku, caps);
  const newRate = getRate(newLine, block.sku, caps);
  if (newRate <= 0) return null;
  if (oldRate <= 0) return block.run_hours;
  const srcGroup = groupOf(block.line_name);
  const qty =
    qtyOverWindow(srcGroup, block.start_hour, block.start_hour + block.run_hours, downtime, oldRate) ||
    oldRate * block.run_hours;
  const start = startHour ?? block.start_hour;
  const hours = hoursForQty(groupOf(newLine), qty, start, downtime, newRate);
  return hours === null ? null : Math.max(1, hours);
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
