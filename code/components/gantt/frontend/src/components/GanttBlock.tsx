// GanttBlock.tsx — Single draggable/resizable block (SVG rect).

import React, { useCallback } from "react";
import { useDraggable } from "@dnd-kit/core";
import type { ScheduleBlock } from "../types";
import { hourToX, LINE_HEIGHT, hourToStamp } from "../utils/layout";
import { skuColor, skuTextColor, blockLabel } from "../utils/colors";

interface Props {
  block: ScheduleBlock;
  lineIndex: number;
  viewStart: number;
  hourWidth: number;
  /** Planning anchor, so the hover tooltip reads as date + time. */
  anchor: Date;
  isResizing: boolean;
  previewStart?: number;
  previewEnd?: number;
  isHighlighted: boolean;
  /** True when ANOTHER SKU is highlighted: this block fades back so the
   * highlighted campaign stands out (user request 2026-08-14). */
  isDimmed?: boolean;
  /** Vertical offset within the row (0 unless the block sits on one side). */
  slotY?: number;
  /** Height of the slot; the full row height unless one-sided. */
  slotHeight?: number;
  /** "A" / "B" when the block occupies a single side of a double line. */
  side?: string | null;
  onResizeStart: (blockId: string, edge: "left" | "right", startH: number, endH: number, clientX: number, hourWidth: number) => void;
  onContextMenu: (e: React.MouseEvent, blockId: string) => void;
  onClick: (blockId: string) => void;
}

export const GanttBlock: React.FC<Props> = ({
  block, lineIndex, viewStart, hourWidth, anchor, isResizing, previewStart, previewEnd,
  isHighlighted, isDimmed = false, slotY = 0, slotHeight, side = null, onResizeStart, onContextMenu, onClick,
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
  // A one-sided block is inset into its half of the row; a full-row block keeps
  // the original 4px padding top and bottom.
  const rowH = slotHeight ?? LINE_HEIGHT;
  const pad = rowH >= LINE_HEIGHT ? 4 : 2;
  const y = slotY + pad;
  const h = Math.max(6, rowH - pad * 2);
  // Completed manprg history renders grey and read-only (user 2026-08-14).
  const bg = block.completed ? "#b0bec5" : skuColor(block.sku, block.block_type);
  const fg = block.completed ? "#37474f" : skuTextColor(bg);

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

  const rawDesc = block.sku_description || "";
  const desc = /^nan$/i.test(rawDesc.trim()) ? "" : rawDesc;  // defensive: never render 'nan'
  const baseLabel = blockLabel(block.block_type, block.sku, block.label);
  const hoursTxt = (Number.isFinite(block.run_hours) ? block.run_hours : 0).toFixed(1);

  // Estimate available characters from pixel width (~6.5px per char at 11px font)
  const charBudget = Math.floor((w - 12) / 6.5);
  let label: string;
  if (block.block_type === "cip" || block.block_type === "line_down" || block.block_type === "maintenance" || block.block_type === "contractor") {
    label = charBudget <= 0 ? "" : (baseLabel.length <= charBudget ? baseLabel : baseLabel.slice(0, Math.max(charBudget - 1, 1)) + "…");
  } else if (charBudget <= 0) {
    label = "";
  } else {
    const withHours = `${baseLabel} (${hoursTxt}h)`;
    const withDesc = desc ? `${baseLabel} ${desc} (${hoursTxt}h)` : withHours;
    if (withDesc.length <= charBudget) {
      label = withDesc;
    } else if (withHours.length <= charBudget) {
      label = withHours;
    } else if (baseLabel.length <= charBudget) {
      label = baseLabel;
    } else {
      label = baseLabel.slice(0, Math.max(charBudget - 1, 1)) + "…";
    }
  }

  // Hover tooltip: wall-clock start/end, duration, live completion, cases left.
  const tooltip = [
    baseLabel,
    desc,
    side ? `${block.line_name} (side ${side} only - half rate)` : `${block.line_name}`,
    `${hourToStamp(startH, anchor)} -> ${hourToStamp(endH, anchor)} (${(endH - startH).toFixed(1)}h)`,
    typeof block.completion_pct === "number"
      ? `Completion: ${block.completion_pct.toFixed(1)}%${typeof block.cases_left === "number" ? ` · ${block.cases_left.toLocaleString()} cases left` : ""}`
      : "",
  ].filter(Boolean).join("\n");

  const strokeColor = isDragging ? "#333" : "none";
  const strokeW = isDragging ? 2 : 0;

  return (
    <g
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      transform={`translate(${dragX}, ${dragY})`}
      style={{
        cursor: isDragging ? "grabbing" : "grab",
        opacity: isDragging ? 0.6 : isDimmed ? 0.25 : 1,
        transition: "opacity 120ms ease",
      }}
      onContextMenu={handleContext}
      onClick={handleClick}
    >
      <title>{tooltip}</title>
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
      {/* Live completion fill (manprg): left-to-right progress on production bars */}
      {typeof block.completion_pct === "number" && block.completion_pct > 0 && (() => {
        const pct = Math.min(Math.max(block.completion_pct, 0), 100);
        const fw = Math.max((w * pct) / 100, 0);
        return (
          <>
            <rect
              x={x}
              y={y}
              width={fw}
              height={h}
              rx={4}
              fill="rgba(255,255,255,0.35)"
              pointerEvents="none"
            />
            <line x1={x + fw} y1={y} x2={x + fw} y2={y + h} stroke="rgba(255,255,255,0.7)" strokeWidth={1.5} pointerEvents="none" />
          </>
        );
      })()}
      {w > 20 && label && (
        <text
          x={x + 6}
          y={y + h / 2 + 1}
          textAnchor="start"
          dominantBaseline="middle"
          fontSize={h < LINE_HEIGHT / 2 ? 8 : w > 60 ? 11 : 9}
          fill={fg}
          fontWeight={600}
          pointerEvents="none"
          style={{ userSelect: "none" }}
        >
          {label}
        </text>
      )}
      {/* Resize handles: adaptive width — generous on wide blocks (easier to
          grab than a fixed 8px sliver), but never more than a third of a
          narrow block so its body stays draggable. */}
      {(() => {
        const hw = Math.min(14, Math.max(5, Math.max(w, 2) / 3));
        return (
          <>
            <rect x={x} y={y} width={hw} height={h} fill="transparent" style={{ cursor: "ew-resize" }} onPointerDown={handleLeftResize} />
            <rect x={x + Math.max(w, 2) - hw} y={y} width={hw} height={h} fill="transparent" style={{ cursor: "ew-resize" }} onPointerDown={handleRightResize} />
          </>
        );
      })()}
    </g>
  );
};
