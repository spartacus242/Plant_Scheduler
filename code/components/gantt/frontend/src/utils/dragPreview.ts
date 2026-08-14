// dragPreview.ts - Rate-aware live preview of where a dragged block would land.
// Pure computation: mirrors the drop rules in GanttSandbox.onDragEnd exactly so
// the preview never promises something the drop would refuse.

import type { ScheduleBlock, LineInfo } from "../types";
import { isCapable, recalcDuration, findOverlapsOnLine, getRate } from "./validation";
import { snapToHour, hourToStamp } from "./layout";
import type { SideDowntime } from "./abLines";
import { groupOf, hasOneSidedStretch, isDouble } from "./abLines";

export interface InsertPlan {
  /** Where the dragged block lands (after the left neighbour + its setup). */
  insStart: number;
  insEnd: number;
  /** The displaced block (first whose span the drop point hits). */
  nextId: string;
  nextLabel: string;
  /** Uniform right-shift applied to the displaced block and everything after it. */
  deltaH: number;
  shiftedCount: number;
  /** Set when the insert cannot be committed (locked block in the path, horizon overflow). */
  blockedReason: string | null;
}

export interface DragPreview {
  /** Line the block would land on. */
  targetLine: string;
  startHour: number;
  endHour: number;
  /** Duration on the target line (recalculated from the rate map). */
  hours: number;
  /** Rate (kg/h) used for the target line; 0 when the line cannot run the SKU. */
  rate: number;
  /** Rate on the block's current line, for comparison. */
  sourceRate: number;
  /** Duration before the move, so the badge can show a change. */
  sourceHours: number;
  /** False when the drop would be refused (incapable line or overlap). */
  valid: boolean;
  /** Human reason when invalid. */
  reason: string | null;
  /** True when the placement crosses hours where only one side is running. */
  oneSided: boolean;
  /** Present when the drop point hits occupied space and the block could be
   * INSERTED there, sliding the displaced block (and everything after it on
   * the line) to the right. */
  insert?: InsertPlan | null;
}

export interface InsertContext {
  /** setup hours between two blocks (0 for windows / same SKU). */
  setupBetween: (from: ScheduleBlock, to: ScheduleBlock) => number;
  horizonH: number;
  lockedThroughH: number | null;
  isBlockLocked: (b: ScheduleBlock) => boolean;
}

/**
 * Plan an insert-at-drop-point: the dragged block lands after its left
 * neighbour (setup respected) and the displaced block plus everything after
 * it slides right by one uniform delta (relative gaps preserved, so no new
 * overlaps can appear downstream). Pure — shared by the drag preview and the
 * drop commit so they can never disagree.
 */
export function computeInsertPlan(
  dragged: ScheduleBlock,
  targetLine: string,
  dropHour: number,
  durH: number,
  allBlocks: ScheduleBlock[],
  ctx: InsertContext,
): InsertPlan | null {
  const others = allBlocks
    .filter((b) => b.id !== dragged.id && b.line_name === targetLine)
    // Display-only cip_info overlays are not calendar data (stripped on
    // save, redrawn from the live feed) - they neither shift nor block an
    // insert; a resulting overlap shows in the KPI bar and Reconcile.
    .filter((b) => !String(b.id).startsWith("cipinfo_"))
    .sort((a, b) => a.start_hour - b.start_hour);
  const next = others.find((b) => b.end_hour > dropHour + 1e-9);
  if (!next || next.start_hour > dropHour + 1e-9) return null; // free space — normal move
  const idx = others.indexOf(next);
  const prev = idx > 0 ? others[idx - 1] : undefined;
  const insStart = Math.max(
    dropHour,
    prev ? prev.end_hour + ctx.setupBetween(prev, dragged) : 0,
    next.start_hour, // never start before the displaced block's own start
  );
  const insEnd = insStart + durH;
  const needNextStart = insEnd + ctx.setupBetween(dragged, next);
  const deltaH = Math.max(0, needNextStart - next.start_hour);
  const shifted = others.filter((b) => b.start_hour >= next.start_hour - 1e-9);
  let blockedReason: string | null = null;
  const lockedHit = shifted.find((b) => ctx.isBlockLocked(b));
  if (ctx.lockedThroughH != null && insStart < ctx.lockedThroughH - 1e-9) {
    blockedReason = "insert point is inside the locked window";
  } else if (lockedHit) {
    blockedReason = `would shift locked block ${lockedHit.sku || lockedHit.label}`;
  } else {
    const lastEnd = Math.max(...shifted.map((b) => b.end_hour)) + deltaH;
    if (lastEnd > ctx.horizonH + 1e-9) {
      blockedReason = `shift would push ${(lastEnd - ctx.horizonH).toFixed(1)}h past the horizon`;
    }
  }
  return {
    insStart,
    insEnd,
    nextId: next.id,
    nextLabel: String(next.sku || next.label || next.block_type),
    deltaH,
    shiftedCount: shifted.length,
    blockedReason,
  };
}

export interface DragPreviewInput {
  block: ScheduleBlock;
  /** dnd-kit active id (holding drags are prefixed "holding_"). */
  activeId: string;
  /** dnd-kit over id, if any. */
  overId?: string;
  deltaX: number;
  deltaY: number;
  /** Hour under the pointer; used for holding-area restores. */
  pointerHour: number;
  lines: LineInfo[];
  caps: Record<string, Record<string, number>>;
  /** Every committed block, for overlap checking. */
  allBlocks: ScheduleBlock[];
  hourWidth: number;
  lineHeight: number;
  /** Planning anchor, so rejection reasons name a wall-clock moment. */
  anchor: Date;
  /** Per-side scheduled downtime, keyed by line/side name. */
  downtime?: SideDowntime;
  /** When provided, an overlapping drop point is planned as an INSERT. */
  insertCtx?: InsertContext;
}

/**
 * Compute the placement the current drag would produce.
 * Returns null when there is nothing meaningful to preview (e.g. a holding
 * block not yet over a line row).
 */
export function computeDragPreview(input: DragPreviewInput): DragPreview | null {
  const {
    block, activeId, overId, deltaX, deltaY, pointerHour,
    lines, caps, allBlocks, hourWidth, lineHeight, anchor, downtime = {},
    insertCtx,
  } = input;

  const sourceRate = getRate(block.line_name, block.sku, caps);
  const sourceHours = block.run_hours;

  // Drag to the holding area: nothing to place on the grid.
  if (overId === "holding_area") return null;

  let targetLineName: string;
  let startHour: number;

  if (activeId.startsWith("holding_")) {
    if (!overId?.startsWith("line_")) return null;
    targetLineName = overId.replace("line_", "");
    startHour = pointerHour;
  } else {
    const startLineIndex = lines.findIndex((l) => l.line_name === block.line_name);
    if (startLineIndex < 0) return null;
    const lineDelta = Math.round(deltaY / lineHeight);
    const targetLineIndex = Math.max(0, Math.min(startLineIndex + lineDelta, lines.length - 1));
    const targetLine = lines[targetLineIndex];
    if (!targetLine) return null;
    targetLineName = targetLine.line_name;
    const deltaHours = snapToHour(deltaX / hourWidth);
    startHour = Math.max(0, snapToHour(block.start_hour + deltaHours));
  }

  const sameLine = targetLineName === block.line_name && !activeId.startsWith("holding_");
  const rate = getRate(targetLineName, block.sku, caps);
  const targetGroup = groupOf(targetLineName);

  // Duration: window blocks (cip/maintenance/contractor/line_down) and same-line
  // moves keep their hours; production/trial moves recalculate from the rate map.
  let hours = block.run_hours;
  let valid = true;
  let reason: string | null = null;

  // Duration is recalculated whenever the placement could cross a one-sided
  // (half-rate) stretch, not only on a line change: sliding a block along a
  // double line into or out of a side-down window changes how long it takes.
  const needsRecalc =
    block.block_type !== "cip" &&
    (!sameLine ||
      (isDouble(targetGroup) &&
        (hasOneSidedStretch(targetGroup, startHour, startHour + block.run_hours, downtime) ||
          hasOneSidedStretch(targetGroup, block.start_hour, block.start_hour + block.run_hours, downtime))));

  if (!sameLine && block.block_type !== "cip" && !isCapable(targetLineName, block.sku, caps)) {
    valid = false;
    reason = `Line ${targetLineName} cannot run ${block.sku}`;
  } else if (needsRecalc) {
    const newDur = recalcDuration(block, targetLineName, caps, downtime, startHour);
    if (newDur === null) {
      valid = false;
      reason = `No rate for ${block.sku} on ${targetLineName}`;
    } else {
      hours = newDur;
    }
  }

  if (block.locked) {
    valid = false;
    reason = "Block is locked";
  }

  const endHour = startHour + hours;

  const oneSided = hasOneSidedStretch(targetGroup, startHour, endHour, downtime);

  let insert: InsertPlan | null = null;
  if (valid && findOverlapsOnLine(allBlocks, targetLineName, block.id, startHour, endHour)) {
    insert = insertCtx
      ? computeInsertPlan(block, targetLineName, startHour, hours, allBlocks, insertCtx)
      : null;
    if (insert && !insert.blockedReason) {
      // still a valid gesture: it becomes an INSERT on drop
      reason = null;
    } else {
      valid = false;
      reason = insert?.blockedReason
        ? `Cannot insert: ${insert.blockedReason}`
        : `Overlap on ${targetLineName} at ${hourToStamp(startHour, anchor)}`;
    }
  }

  return {
    targetLine: targetLineName,
    startHour,
    endHour,
    hours,
    rate,
    sourceRate,
    sourceHours,
    valid,
    reason,
    oneSided,
    insert,
  };
}
