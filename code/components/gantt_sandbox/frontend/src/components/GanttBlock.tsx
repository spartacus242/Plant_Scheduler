// GanttBlock.tsx — Single draggable/resizable block (SVG rect).

import React, { useCallback } from "react";
import { useDraggable } from "@dnd-kit/core";
import type { ScheduleBlock } from "../types";
import { hourToX, LINE_HEIGHT } from "../utils/layout";
import { skuColor, skuTextColor } from "../utils/colors";

interface Props {
  block: ScheduleBlock;
  lineIndex: number;
  viewStart: number;
  hourWidth: number;
  isResizing: boolean;
  previewStart?: number;
  previewEnd?: number;
  isHighlighted: boolean;
  onResizeStart: (blockId: string, edge: "left" | "right", startH: number, endH: number, clientX: number, hourWidth: number) => void;
  onContextMenu: (e: React.MouseEvent, blockId: string) => void;
  onClick: (blockId: string) => void;
}

export const GanttBlock: React.FC<Props> = ({
  block, lineIndex, viewStart, hourWidth, isResizing, previewStart, previewEnd,
  isHighlighted, onResizeStart, onContextMenu, onClick,
}) => {
  const { attributes, listeners, setNodeRef: setDragRef, transform, isDragging } = useDraggable({
    id: block.id,
    data: { block },
  });
  const setNodeRef = setDragRef as unknown as React.Ref<SVGGElement>;

  const startH = isResizing ? (previewStart ?? block.start_hour) : block.start_hour;
  const endH = isResizing ? (previewEnd ?? block.end_hour) : block.end_hour;
  const x = hourToX(startH, viewStart, hourWidth);
  const w = (endH - startH) * hourWidth;
  const y = 4;
  const h = LINE_HEIGHT - 8;
  const bg = skuColor(block.sku, block.block_type);
  const fg = skuTextColor(bg);

  const handleLeftResize = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
      e.preventDefault();
      onResizeStart(block.id, "left", block.start_hour, block.end_hour, e.clientX, hourWidth);
    },
    [block, hourWidth, onResizeStart],
  );

  const handleRightResize = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
      e.preventDefault();
      onResizeStart(block.id, "right", block.start_hour, block.end_hour, e.clientX, hourWidth);
    },
    [block, hourWidth, onResizeStart],
  );

  const handleContext = useCallback(
    (e: React.MouseEvent) => { e.preventDefault(); onContextMenu(e, block.id); },
    [block.id, onContextMenu],
  );

  const handleClick = useCallback(
    (e: React.MouseEvent) => { e.stopPropagation(); onClick(block.id); },
    [block.id, onClick],
  );

  const dragX = isDragging && transform ? transform.x : 0;
  const dragY = isDragging && transform ? transform.y : 0;

  const label = block.block_type === "cip"
    ? "CIP"
    : block.block_type === "trial"
      ? `T:${block.sku}`
      : block.sku;

  // Highlight effect: pulsing stroke
  const strokeColor = isHighlighted ? "#FFD700" : isDragging ? "#333" : "none";
  const strokeW = isHighlighted ? 3 : isDragging ? 2 : 0;

  return (
    <g
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      transform={`translate(${dragX}, ${dragY})`}
      style={{ cursor: isDragging ? "grabbing" : "grab", opacity: isDragging ? 0.6 : 1 }}
      onContextMenu={handleContext}
      onClick={handleClick}
    >
      {/* Highlight glow */}
      {isHighlighted && (
        <rect
          x={x - 2}
          y={y - 2}
          width={Math.max(w, 2) + 4}
          height={h + 4}
          rx={6}
          fill="none"
          stroke="#FFD700"
          strokeWidth={2}
          opacity={0.5}
        />
      )}
      <rect
        x={x}
        y={y}
        width={Math.max(w, 2)}
        height={h}
        rx={4}
        fill={bg}
        stroke={strokeColor}
        strokeWidth={strokeW}
      />
      {/* Label */}
      {w > 20 && (
        <text
          x={x + w / 2}
          y={y + h / 2 + 1}
          textAnchor="middle"
          dominantBaseline="middle"
          fontSize={w > 60 ? 11 : 9}
          fill={fg}
          fontWeight={600}
          pointerEvents="none"
          style={{ userSelect: "none" }}
        >
          {w > 80 ? `${label} (${block.run_hours}h)` : label}
        </text>
      )}
      {/* Left resize handle */}
      <rect x={x} y={y} width={8} height={h} fill="transparent" style={{ cursor: "ew-resize" }} onPointerDown={handleLeftResize} />
      {/* Right resize handle */}
      <rect x={x + Math.max(w, 2) - 8} y={y} width={8} height={h} fill="transparent" style={{ cursor: "ew-resize" }} onPointerDown={handleRightResize} />
    </g>
  );
};
