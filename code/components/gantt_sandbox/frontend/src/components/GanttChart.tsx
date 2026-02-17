// GanttChart.tsx — Line rows, drop zones, SVG canvas (no DndContext — parent owns it).

import React, { useRef } from "react";
import { useDroppable } from "@dnd-kit/core";
import type { ScheduleBlock, LineInfo } from "../types";
import { GanttBlock } from "./GanttBlock";
import { TimeAxisSvg, ZoomControls } from "./TimeAxis";
import {
  LINE_HEIGHT,
  HEADER_HEIGHT,
  LINE_LABEL_WIDTH,
} from "../utils/layout";
import type { ResizeState } from "../hooks/useBlockResize";

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
  onResizeStart: (blockId: string, edge: "left" | "right", startH: number, endH: number, clientX: number, hourWidth: number) => void;
  onContextMenu: (e: React.MouseEvent, blockId: string) => void;
  onBlockClick: (blockId: string) => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onResetZoom: () => void;
}

/** A droppable line row */
const LineRow: React.FC<{
  line: LineInfo;
  index: number;
  svgWidth: number;
  isCapable: boolean | null;
}> = ({ line, index, svgWidth, isCapable }) => {
  const { setNodeRef: setDropRef, isOver } = useDroppable({ id: `line_${line.line_name}` });
  const setNodeRef = setDropRef as unknown as React.Ref<SVGGElement>;
  const y = HEADER_HEIGHT + index * LINE_HEIGHT;

  let fill: string;
  if (isOver) {
    fill = isCapable === false ? "#ffe0e0" : "#d8e8ff";
  } else if (isCapable === true) {
    fill = "#e8f5e8";
  } else if (isCapable === false) {
    fill = "#f5e8e8";
  } else {
    fill = index % 2 === 0 ? "#fff" : "#fafafa";
  }

  return (
    <g ref={setNodeRef}>
      <rect x={0} y={y} width={svgWidth} height={LINE_HEIGHT} fill={fill} />
      <line x1={0} y1={y + LINE_HEIGHT} x2={svgWidth} y2={y + LINE_HEIGHT} stroke="#eee" />
      <text x={4} y={y + LINE_HEIGHT / 2} dominantBaseline="middle" fontSize={11} fontWeight={600} fill="#333">
        {line.line_name}
      </text>
    </g>
  );
};

export const GanttChart: React.FC<Props> = ({
  schedule, cipWindows, lines, viewStart, viewEnd, hourWidth, anchor,
  resizing, highlightSku, capableLines,
  onResizeStart, onContextMenu, onBlockClick, onZoomIn, onZoomOut, onResetZoom,
}) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const svgWidth = LINE_LABEL_WIDTH + (viewEnd - viewStart) * hourWidth;
  const svgHeight = HEADER_HEIGHT + lines.length * LINE_HEIGHT + 4;

  const allBlocks = [...schedule, ...cipWindows];

  return (
    <>
      {/* HTML zoom controls — above the SVG */}
      <ZoomControls onZoomIn={onZoomIn} onZoomOut={onZoomOut} onResetZoom={onResetZoom} />

      {/* Single SVG containing time axis + rows + blocks */}
      <div style={{ overflowX: "hidden", overflowY: "hidden", border: "1px solid #e0e0e5", borderRadius: 8 }}>
        <svg ref={svgRef} width={svgWidth} height={svgHeight} style={{ display: "block" }}>
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
          {lines.map((line, i) => (
            <LineRow
              key={line.line_name}
              line={line}
              index={i}
              svgWidth={svgWidth}
              isCapable={capableLines ? capableLines.has(line.line_name) : null}
            />
          ))}

          {/* Blocks */}
          {allBlocks.map((block) => {
            const lineIndex = lines.findIndex((l) => l.line_name === block.line_name);
            if (lineIndex < 0) return null;
            const y = HEADER_HEIGHT + lineIndex * LINE_HEIGHT;
            const isThisResizing = resizing.blockId === block.id;
            const isHighlighted = highlightSku !== null && block.sku === highlightSku && block.block_type !== "cip";
            return (
              <g key={block.id} transform={`translate(0, ${y})`}>
                <GanttBlock
                  block={block}
                  lineIndex={lineIndex}
                  viewStart={viewStart}
                  hourWidth={hourWidth}
                  isResizing={isThisResizing}
                  previewStart={isThisResizing ? resizing.previewStart : undefined}
                  previewEnd={isThisResizing ? resizing.previewEnd : undefined}
                  isHighlighted={isHighlighted}
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
