// dragPreview.ts - Rate-aware live preview of where a dragged block would land.
// Pure computation: mirrors the drop rules in GanttSandbox.onDragEnd exactly so
// the preview never promises something the drop would refuse.

import type { ScheduleBlock, LineInfo } from "../types";
import { isCapable, recalcDuration, findOverlapsOnLine, getRate } from "./validation";
import { blockKey, sameBlock } from "./blockIdentity";
import { hourToStamp } from "./layout";
import type { SideDowntime } from "./abLines";
import { groupOf, hasOneSidedStretch, isDouble } from "./abLines";
import type { DropPlan } from "./dropPlan";
import type { Supply } from "./stockRisk";

export interface InsertPlan {
  /** Where the dragged block lands (after the left neighbour + its setup). */
  insStart: number;
  insEnd: number;
  /** The displaced block (first whose span the drop point hits). */
  nextId: string;
  /** Its PIECE key (blockIdentity.blockKey): split MO pieces share an id,
   * so callers resolve the displaced block by key, never by nextId alone. */
  nextKey: string;
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
  /** Supply verdict of the dragged block AT the preview position (stock
   * payload present, production blocks only) — set by the caller after the
   * geometric preview, never by computeDragPreview. */
  supply?: Supply | null;
  /** The planner sentence for `supply`, stamped with the page anchor. */
  supplyText?: string | null;
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
    // The dragged PIECE only (split MO pieces share an id): its sibling is
    // a real neighbour that must be displaced, not skipped.
    .filter((b) => !sameBlock(b, dragged) && b.line_name === targetLine)
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
    nextKey: blockKey(next),
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
  /** Geometric landing from planDrop; null when the ghost is unmeasurable. */
  plan: DropPlan | null;
  lines: LineInfo[];
  caps: Record<string, Record<string, number>>;
  /** Every committed block, for overlap checking. */
  allBlocks: ScheduleBlock[];
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
    block, activeId, overId, plan,
    lines, caps, allBlocks, anchor, downtime = {},
    insertCtx,
  } = input;

  const sourceRate = getRate(block.line_name, block.sku, caps);
  const sourceHours = block.run_hours;
  const fromHolding = activeId.startsWith("holding_");

  // Drag to the holding area: nothing to place on the grid.
  if (overId === "holding_area") return null;
  if (!plan) return null;
  // Holding card not yet over the chart rows: nothing to place.
  if (fromHolding && !plan.valid) return null;
  if (!lines[plan.rowIdx]) return null;

  const targetLineName = plan.lineName;
  const startHour = plan.snappedStartHour;

  const sameLine = targetLineName === block.line_name && !fromHolding;
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

  // The drop refuses any landing inside the locked window - the ghost must
  // show that refusal, not promise a placement the drop would reject.
  const lockedThroughH = insertCtx?.lockedThroughH ?? null;
  if (valid && lockedThroughH != null && startHour < lockedThroughH - 1e-9) {
    valid = false;
    reason = `Cannot ${fromHolding ? "drop" : "move"} into the locked window (committed through ${hourToStamp(lockedThroughH, anchor)})`;
  }

  const endHour = startHour + hours;

  const oneSided = hasOneSidedStretch(targetGroup, startHour, endHour, downtime);

  let insert: InsertPlan | null = null;
  // Exclude the dragged PIECE only: excluding by id let a split MO piece be
  // dropped over its own sibling (same id, different start).
  const obstacles = allBlocks.filter((b) => !sameBlock(b, block));
  if (valid && findOverlapsOnLine(obstacles, targetLineName, "", startHour, endHour)) {
    // Holding restores never insert-shift on drop, so the preview must not
    // promise one - an overlapping landing is a plain refusal there.
    insert = insertCtx && !fromHolding
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

/**
 * Field-wise equality so the every-frame preview recompute (auto-scroll rAF
 * loop) only re-renders when the landing actually changed.
 */
export function samePreview(a: DragPreview | null, b: DragPreview | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  const sameInsert =
    (a.insert ?? null) === (b.insert ?? null) ||
    (a.insert != null && b.insert != null &&
      a.insert.insStart === b.insert.insStart &&
      a.insert.insEnd === b.insert.insEnd &&
      a.insert.nextKey === b.insert.nextKey &&
      a.insert.deltaH === b.insert.deltaH &&
      a.insert.shiftedCount === b.insert.shiftedCount &&
      a.insert.blockedReason === b.insert.blockedReason);
  // A fresh Supply object every frame: compare what the badge renders.
  const sa = a.supply ?? null;
  const sb = b.supply ?? null;
  const sameSupply =
    sa === sb ||
    (sa != null && sb != null &&
      sa.verdict === sb.verdict &&
      sa.minor === sb.minor &&
      sa.backed === sb.backed &&
      sa.item === sb.item &&
      sa.covered_frac === sb.covered_frac &&
      sa.lead_h === sb.lead_h &&
      sa.safe_from_h === sb.safe_from_h &&
      sa.depletion_h === sb.depletion_h &&
      (a.supplyText ?? null) === (b.supplyText ?? null));
  return (
    a.targetLine === b.targetLine &&
    a.startHour === b.startHour &&
    a.endHour === b.endHour &&
    a.hours === b.hours &&
    a.rate === b.rate &&
    a.sourceRate === b.sourceRate &&
    a.sourceHours === b.sourceHours &&
    a.valid === b.valid &&
    a.reason === b.reason &&
    a.oneSided === b.oneSided &&
    sameInsert &&
    sameSupply
  );
}
