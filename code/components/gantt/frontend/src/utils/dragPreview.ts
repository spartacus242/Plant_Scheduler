// dragPreview.ts - Rate-aware live preview of where a dragged block would land.
// Pure computation: mirrors the drop rules in GanttSandbox.onDragEnd exactly so
// the preview never promises something the drop would refuse.

import type { ScheduleBlock, LineInfo } from "../types";
import { isCapable, recalcDuration, findOverlapsOnLine, getRate } from "./validation";
import { snapToHour, hourToStamp } from "./layout";
import type { SideDowntime } from "./abLines";
import { groupOf, hasOneSidedStretch, isDouble } from "./abLines";

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

  if (valid && findOverlapsOnLine(allBlocks, targetLineName, block.id, startHour, endHour)) {
    valid = false;
    reason = `Overlap on ${targetLineName} at ${hourToStamp(startHour, anchor)}`;
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
  };
}
