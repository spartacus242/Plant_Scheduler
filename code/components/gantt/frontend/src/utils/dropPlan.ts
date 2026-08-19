// dropPlan.ts - ONE source of truth for where a dragged block lands.
//
// The landing is derived from two rects read at the SAME instant in the SAME
// coordinate space (the iframe viewport): the dragged ghost's current rect
// (dnd-kit's active.rect.current.translated - the DragOverlay is a
// position:fixed element glued to the pointer) and the chart SVG's live
// getBoundingClientRect(). Pointer deltas are NEVER used: dnd-kit's
// scrollable-ancestor walk bails out on SVG nodes (getScrollableAncestors
// returns [] for our <g> draggables), so event.delta carries no scroll
// adjustment - and the parent-page auto-scroll happens outside the iframe
// where dnd-kit cannot see it at all. Comparing the two live rects makes
// every scroll source cancel out by construction: container scroll moves
// svgRect, parent-page scroll moves neither, and the difference always reads
// the true content position under the ghost.

import { snapToHour, xToHour, yToLineIndex, LINE_HEIGHT, HEADER_HEIGHT } from "./layout";
import { groupOf } from "./abLines";

/** Minimal rect shape - satisfied by DOMRect and dnd-kit's ClientRect. */
export interface DropRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface DropPlanArgs {
  /** Dragged ghost's CURRENT rect (active.rect.current.translated). */
  translated: DropRect;
  /** Chart svg's CURRENT bounding rect, read at the same instant. */
  svgRect: DropRect;
  viewStart: number;
  viewEnd: number;
  hourWidth: number;
  /** One line name per chart row, in render order (the collapsed row list). */
  rowNames: string[];
  /** Block duration used for the right-edge clamp. */
  durationH: number;
  /** Block's current line; preserves an A/B side name on a same-row drop. */
  sourceLineName?: string;
  /** Holding-area drags: the ghost's center must sit on a real row. */
  requireInsideRows?: boolean;
}

export interface DropPlan {
  rowIdx: number;
  lineName: string;
  snappedStartHour: number;
  endHour: number;
  /** Geometric validity only - capability/lock/overlap are layered on top. */
  valid: boolean;
  reason: string | null;
}

/** Row index of a line name in the collapsed row list ("P17A" -> its group row). */
function rowIndexOfName(rowNames: string[], lineName: string): number {
  const direct = rowNames.indexOf(lineName);
  return direct >= 0 ? direct : rowNames.indexOf(groupOf(lineName));
}

/**
 * Plan the landing of the current drag: the block's LEFT EDGE picks the
 * hour (snapped to the same whole-hour grid as every other placement) and
 * its vertical CENTER picks the row. Pure - shared by the live ghost
 * preview and the drop commit so they can never disagree.
 */
export function planDrop(args: DropPlanArgs): DropPlan {
  const {
    translated, svgRect, viewStart, viewEnd, hourWidth,
    rowNames, durationH, sourceLineName, requireInsideRows = false,
  } = args;

  // Hour under the ghost's left edge, snapped, clamped into the view so the
  // block can never start before the chart or end past its right edge.
  const xInSvg = translated.left - svgRect.left;
  const rawHour = xToHour(xInSvg, viewStart, hourWidth);
  const maxStart = Math.max(viewStart, viewEnd - durationH);
  const snappedStartHour = Math.max(
    Math.max(0, viewStart),
    Math.min(snapToHour(rawHour), maxStart),
  );

  // Row under the ghost's vertical center, clamped to the row band.
  const yCenter = translated.top + translated.height / 2 - svgRect.top;
  const rawRow = yToLineIndex(yCenter);
  const rowIdx = Math.max(0, Math.min(rawRow, rowNames.length - 1));
  const insideRows =
    rawRow >= 0 && rawRow < rowNames.length &&
    yCenter >= HEADER_HEIGHT && yCenter <= HEADER_HEIGHT + rowNames.length * LINE_HEIGHT;

  // A drop back onto the source row keeps the block's own line name, so a
  // side-named block ("P17A") slides along its side instead of being
  // rewritten to the group.
  let lineName = rowNames[rowIdx];
  if (sourceLineName && rowIndexOfName(rowNames, sourceLineName) === rowIdx) {
    lineName = sourceLineName;
  }

  const valid = requireInsideRows ? insideRows : true;
  return {
    rowIdx,
    lineName,
    snappedStartHour,
    endHour: snappedStartHour + durationH,
    valid,
    reason: valid ? null : "Drop onto a production line to restore from holding",
  };
}
