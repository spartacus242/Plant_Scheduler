// GanttChart.tsx — Line rows, drop zones, SVG canvas (no DndContext — parent owns it).

import React, { useRef } from "react";
import { useDroppable } from "@dnd-kit/core";
import type { ScheduleBlock, LineInfo } from "../types";
import { isWindowBlock } from "../types";
import { GanttBlock } from "./GanttBlock";
import { TimeAxisSvg, ZoomControls } from "./TimeAxis";
import type { GanttRow } from "../utils/ganttRows";
import { buildRows, blockSlot, rowIndexOf } from "../utils/ganttRows";
import {
  LINE_HEIGHT,
  HEADER_HEIGHT,
  LINE_LABEL_WIDTH,
} from "../utils/layout";
import type { ResizeState } from "../hooks/useBlockResize";
import type { InsertPlan } from "../utils/dragPreview";

interface Props {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  lines: LineInfo[];
  viewStart: number;
  viewEnd: number;
  hourWidth: number;
  anchor: Date;
  resizing: ResizeState;
  highlightSku: string | null;
  capableLines: Set<string> | null;
  /** 2-week lock boundary (hour offset); draws the lock line + shading. */
  lockedThroughH?: number | null;
  /** Live insert preview: dashed ghost of the displaced block at its slid
   * position, so the user SEES the right block moving over before dropping. */
  insertPreview?: InsertPlan | null;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  onResizeStart: (blockId: string, edge: "left" | "right", startH: number, endH: number, clientX: number, hourWidth: number) => void;
  onContextMenu: (e: React.MouseEvent, blockId: string) => void;
  onBlockClick: (blockId: string) => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onResetZoom: () => void;
}

/**
 * A droppable row.
 *
 * A Bossar double line renders as ONE row split horizontally: an "A" half on
 * top and a "B" half underneath, divided by a dashed rule, with the group name
 * (e.g. "P17") as the heading. Single lines P09-P16 render exactly as before.
 */
const LineRow: React.FC<{
  row: GanttRow;
  index: number;
  svgWidth: number;
  isCapable: boolean | null;
}> = ({ row, index, svgWidth, isCapable }) => {
  const { setNodeRef: setDropRef, isOver } = useDroppable({ id: `line_${row.name}` });
  const setNodeRef = setDropRef as unknown as React.Ref<SVGGElement>;
  const y = HEADER_HEIGHT + index * LINE_HEIGHT;
  const half = LINE_HEIGHT / 2;

  let fill: string;
  if (isOver) {
    fill = isCapable === false ? "#e57373" : "#66bb6a";
  } else if (isCapable === true) {
    fill = "#a5d6a7";
  } else if (isCapable === false) {
    fill = "#ef9a9a";
  } else {
    fill = index % 2 === 0 ? "#fff" : "#fafafa";
  }

  return (
    <g ref={setNodeRef}>
      <rect x={0} y={y} width={svgWidth} height={LINE_HEIGHT} fill={fill} />
      {row.isDouble && (
        <>
          {/* Split rule between side A (top) and side B (bottom). */}
          <line
            x1={LINE_LABEL_WIDTH}
            y1={y + half}
            x2={svgWidth}
            y2={y + half}
            stroke="#c7ccd4"
            strokeDasharray="3 3"
          />
          <text x={LINE_LABEL_WIDTH - 12} y={y + half / 2} textAnchor="end" dominantBaseline="middle" fontSize={8} fill="#777">
            A
          </text>
          <text x={LINE_LABEL_WIDTH - 12} y={y + half + half / 2} textAnchor="end" dominantBaseline="middle" fontSize={8} fill="#777">
            B
          </text>
        </>
      )}
      <line x1={0} y1={y + LINE_HEIGHT} x2={svgWidth} y2={y + LINE_HEIGHT} stroke="#eee" />
      <text x={4} y={y + LINE_HEIGHT / 2} dominantBaseline="middle" fontSize={11} fontWeight={600} fill="#333">
        {row.name}
      </text>
    </g>
  );
};

export const GanttChart: React.FC<Props> = ({
  schedule, cipWindows, lines, viewStart, viewEnd, hourWidth, anchor,
  resizing, highlightSku, capableLines, lockedThroughH, insertPreview, svgRef: externalSvgRef,
  onResizeStart, onContextMenu, onBlockClick, onZoomIn, onZoomOut, onResetZoom,
}) => {
  const localSvgRef = useRef<SVGSVGElement>(null);
  const svgRef = externalSvgRef ?? localSvgRef;
  // One display row per group: the two sides of a double line share a row.
  const rows = React.useMemo(() => buildRows(lines), [lines]);
  const svgWidth = LINE_LABEL_WIDTH + (viewEnd - viewStart) * hourWidth;
  const svgHeight = HEADER_HEIGHT + rows.length * LINE_HEIGHT + 4;

  const allBlocks = [...schedule, ...cipWindows];

  return (
    <>
      {/* HTML zoom controls — above the SVG */}
      <ZoomControls onZoomIn={onZoomIn} onZoomOut={onZoomOut} onResetZoom={onResetZoom} />

      {/* Single SVG containing time axis + rows + blocks */}
      <style>{`
        .gantt-scroll { scrollbar-width: auto; scrollbar-color: #90a4ae #eceff1; }
        .gantt-scroll::-webkit-scrollbar { height: 12px; width: 12px; }
        .gantt-scroll::-webkit-scrollbar-track { background: #eceff1; border-radius: 6px; }
        .gantt-scroll::-webkit-scrollbar-thumb { background: #90a4ae; border-radius: 6px; border: 2px solid #eceff1; }
        .gantt-scroll::-webkit-scrollbar-thumb:hover { background: #607d8b; }
      `}</style>
      <div className="gantt-scroll" style={{ overflowX: "auto", overflowY: "auto", maxHeight: 640, width: "100%", border: "1px solid #e0e0e5", borderRadius: 8 }}>
        <svg ref={svgRef as React.RefObject<SVGSVGElement>} width={svgWidth} height={svgHeight} style={{ display: "block" }}>
          {/* Time axis: day labels, shift lines, week boundary — all SVG */}
          <TimeAxisSvg
            viewStart={viewStart}
            viewEnd={viewEnd}
            hourWidth={hourWidth}
            anchor={anchor}
            svgWidth={svgWidth}
            svgHeight={svgHeight}
          />

          {/* Line rows (droppable zones) */}
          {rows.map((row, i) => (
            <LineRow
              key={row.name}
              row={row}
              index={i}
              svgWidth={svgWidth}
              isCapable={capableLines ? capableLines.has(row.name) : null}
            />
          ))}

          {/* 2-week lock window: shaded committed zone + boundary line.
              Drawn UNDER the blocks (shading) with the line and label on top
              of the rows so the boundary reads at every zoom. */}
          {lockedThroughH != null && lockedThroughH > viewStart && (
            <>
              <rect
                x={LINE_LABEL_WIDTH}
                y={HEADER_HEIGHT}
                width={Math.max(0, (Math.min(lockedThroughH, viewEnd) - viewStart) * hourWidth)}
                height={rows.length * LINE_HEIGHT}
                fill="#607d8b"
                opacity={0.07}
                pointerEvents="none"
              />
              {lockedThroughH <= viewEnd && (
                <g pointerEvents="none">
                  <line
                    x1={LINE_LABEL_WIDTH + (lockedThroughH - viewStart) * hourWidth}
                    y1={HEADER_HEIGHT - 6}
                    x2={LINE_LABEL_WIDTH + (lockedThroughH - viewStart) * hourWidth}
                    y2={HEADER_HEIGHT + rows.length * LINE_HEIGHT}
                    stroke="#546e7a"
                    strokeWidth={2}
                    strokeDasharray="6 3"
                  />
                  <text
                    x={LINE_LABEL_WIDTH + (lockedThroughH - viewStart) * hourWidth - 6}
                    y={HEADER_HEIGHT + 12}
                    textAnchor="end"
                    fontSize={10}
                    fontWeight={700}
                    fill="#546e7a"
                  >
                    🔒 locked
                  </text>
                </g>
              )}
            </>
          )}

          {/* Insert preview: dashed outline of the displaced block at its
              slid-right position + insertion marker. */}
          {insertPreview && (() => {
            const nb = allBlocks.find((b) => b.id === insertPreview.nextId);
            if (!nb) return null;
            const li = rowIndexOf(rows, nb.line_name);
            if (li < 0) return null;
            const y = HEADER_HEIGHT + li * LINE_HEIGHT;
            const slot = blockSlot(rows[li], nb.line_name, LINE_HEIGHT);
            const gx = LINE_LABEL_WIDTH + (nb.start_hour + insertPreview.deltaH - viewStart) * hourWidth;
            const gw = (nb.end_hour - nb.start_hour) * hourWidth;
            const ix = LINE_LABEL_WIDTH + (insertPreview.insStart - viewStart) * hourWidth;
            return (
              <g pointerEvents="none">
                <rect
                  x={gx} y={y + slot.y + 2} width={Math.max(gw, 2)} height={(slot.height ?? LINE_HEIGHT) - 8}
                  rx={4} fill="none" stroke="#1976d2" strokeWidth={2} strokeDasharray="5 3" opacity={0.8}
                />
                <line x1={ix} y1={y} x2={ix} y2={y + LINE_HEIGHT} stroke="#1976d2" strokeWidth={2} />
                <text x={ix + 3} y={y + 10} fontSize={9} fontWeight={700} fill="#1976d2">
                  insert - {insertPreview.shiftedCount} block(s) slide {insertPreview.deltaH.toFixed(1)}h
                </text>
              </g>
            );
          })()}

          {/* Blocks */}
          {allBlocks.map((block) => {
            const lineIndex = rowIndexOf(rows, block.line_name);
            if (lineIndex < 0) return null;
            const y = HEADER_HEIGHT + lineIndex * LINE_HEIGHT;
            // A block named for a single side shades only its half of the row;
            // anything on the group spans the full height (both sides running).
            const slot = blockSlot(rows[lineIndex], block.line_name, LINE_HEIGHT);
            const isThisResizing = resizing.blockId === block.id;
            const isHighlighted = highlightSku !== null && block.sku === highlightSku && !isWindowBlock(block.block_type);
            const isDimmed = highlightSku !== null && !isHighlighted;
            return (
              <g key={block.id} transform={`translate(0, ${y})`}>
                <GanttBlock
                  block={block}
                  lineIndex={lineIndex}
                  viewStart={viewStart}
                  hourWidth={hourWidth}
                  anchor={anchor}
                  isResizing={isThisResizing}
                  previewStart={isThisResizing ? resizing.previewStart : undefined}
                  previewEnd={isThisResizing ? resizing.previewEnd : undefined}
                  isHighlighted={isHighlighted}
                  isDimmed={isDimmed}
                  slotY={slot.y}
                  slotHeight={slot.height}
                  side={slot.side}
                  onResizeStart={onResizeStart}
                  onContextMenu={onContextMenu}
                  onClick={onBlockClick}
                />
              </g>
            );
          })}
        </svg>
      </div>
    </>
  );
};
