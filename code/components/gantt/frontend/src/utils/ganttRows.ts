// ganttRows.ts - Collapse the flat line list into Gantt rows.
//
// A Bossar double line (P17-P22) is ONE row split horizontally: side A on the
// top half, side B on the bottom half. A production block on the group runs on
// both sides and spans the full row height; a block on a single side shades
// only its half, which makes a half-rate stretch obvious at a glance.
//
// Single lines (P09-P16) get exactly the row they have today.

import type { LineInfo, ScheduleBlock } from "../types";
import { groupOf, isDouble, sideOf, sidesOf } from "./abLines";

export interface GanttRow {
  /** Group name shown in the row label, e.g. "P17" or "P09". */
  name: string;
  /** line_id of the group (or of the single line). */
  lineId: number;
  isDouble: boolean;
  /** ["P17A","P17B"] on a double row; [] on a single row. */
  sides: string[];
  /** Every line_name that maps onto this row (drop targets). */
  members: string[];
}

/** Build the display rows, one per group, preserving input order. */
export function buildRows(lines: LineInfo[]): GanttRow[] {
  const rows: GanttRow[] = [];
  const byName = new Map<string, GanttRow>();
  for (const l of lines) {
    const g = (l.line_group && l.line_group.trim()) || groupOf(l.line_name);
    let row = byName.get(g);
    if (!row) {
      const dbl = l.is_double ?? isDouble(l.line_name);
      row = {
        name: g,
        lineId: l.line_id,
        isDouble: dbl,
        sides: dbl ? sidesOf(g) : [],
        members: [],
      };
      byName.set(g, row);
      rows.push(row);
    }
    if (!row.members.includes(l.line_name)) row.members.push(l.line_name);
    if (row.isDouble && !row.members.includes(g)) row.members.push(g);
  }
  return rows;
}

/** Index of the row a block belongs to, or -1. */
export function rowIndexOf(rows: GanttRow[], lineName: string): number {
  const g = groupOf(lineName);
  return rows.findIndex((r) => r.name === g || r.members.includes(lineName));
}

/**
 * Vertical placement of a block within its row.
 *
 * A block named for a side ("P17A") occupies that half; anything else on a
 * double row (the group name, or a single line) occupies the full height.
 */
export function blockSlot(
  row: GanttRow | undefined,
  lineName: string,
  lineHeight: number,
): { y: number; height: number; side: string | null } {
  const side = sideOf(lineName);
  if (!row?.isDouble || !side) return { y: 0, height: lineHeight, side: null };
  const half = lineHeight / 2;
  return { y: side === "A" ? 0 : half, height: half, side };
}

/** True when the block sits on one side only of a double line. */
export function isSideBlock(block: ScheduleBlock): boolean {
  return sideOf(block.line_name) !== null;
}
