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
  let qty: number;
  if (oldRate > 0) {
    const srcGroup = groupOf(block.line_name);
    qty =
      qtyOverWindow(srcGroup, block.start_hour, block.start_hour + block.run_hours, downtime, oldRate) ||
      oldRate * block.run_hours;
  } else if (block.qty_kg && block.qty_kg > 0) {
    // A holding card (line_name "") or a block parked off any capable line
    // has no source rate: its TONNAGE is the physical fact, so price it on
    // the target line (fix FE / audit ui-1: returning run_hours here kept
    // the card's mean-rate duration on EVERY line — 170.6 h of 280103 on
    // P15 at 630 kg/h claimed 1,152 kg/h; the right-click Place path
    // already priced it at the line's rate, so the two gestures disagreed
    // by 80-140 h).
    qty = block.qty_kg;
  } else {
    return block.run_hours; // no kg and no source rate: nothing to re-price from
  }
  const start = startHour ?? block.start_hour;
  const hours = hoursForQty(groupOf(newLine), qty, start, downtime, newRate);
  return hours === null ? null : Math.max(1, hours);
}

/**
 * Kg a block may claim once placed on `lineName` over [startHour,
 * startHour + duration]: never more than the line physically makes in that
 * window (half rate on one-sided stretches), never more than the block's
 * own tonnage (its demand). Unknown kg (null/undefined/0) stays unknown —
 * never invent a number. Fix FE / audit ui-14: restoreFromHolding used to
 * carry the card's kg onto whatever duration the caller chose, which is
 * how ui-1's wrong duration became a wrong kg/h row on disk.
 */
export function placedQtyKg(
  block: ScheduleBlock,
  lineName: string,
  startHour: number,
  duration: number,
  caps: Record<string, Record<string, number>>,
  downtime: SideDowntime = {},
): number | undefined {
  const kg = block.qty_kg;
  if (!kg || kg <= 0) return kg;
  const rate = getRate(lineName, block.sku, caps);
  const cap = rate > 0 ? qtyOverWindow(groupOf(lineName), startHour, startHour + duration, downtime, rate) : 0;
  if (cap <= 0) return kg;
  return Math.round(Math.min(kg, cap) * 10) / 10;
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
