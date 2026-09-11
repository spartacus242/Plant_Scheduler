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
  hourToX,
  xToHour,
} from "../utils/layout";
import type { ResizeState } from "../hooks/useBlockResize";
import type { InsertPlan } from "../utils/dragPreview";
import { CHART, T } from "../utils/theme";
import { TYPE_COLORS } from "../utils/colors";

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
  /** Translucent snapped landing cell for the current drag - the exact
   * placement onDragEnd would commit; red-tinted when the drop would refuse. */
  dropGhost?: {
    lineName: string;
    startHour: number;
    endHour: number;
    valid: boolean;
    fill: string;
  } | null;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  onResizeStart: (blockId: string, edge: "left" | "right", startH: number, endH: number, clientX: number, hourWidth: number) => void;
  onContextMenu: (e: React.MouseEvent, blockId: string) => void;
  /** Right-click on EMPTY row space: opens the blank-space SKU picker at
   * that (line, hour). Blocks stop propagation, so this only fires on gaps. */
  onEmptyContextMenu?: (e: React.MouseEvent, lineName: string, lineId: number, hour: number) => void;
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
  /** True when the LIVE drop geometry (planDrop → dropGhost) targets this
   * row. The old dnd-kit isOver highlight went stale after any scroll
   * (SVG ancestors are invisible to its rect bookkeeping) and could light
   * a DIFFERENT line than the one the drop would actually hit — the
   * highlight now follows the same source as the ghost and the drop
   * (user rule 2026-09-01: highlighted line + dotted preview = same line). */
  isDragTarget?: boolean;
}> = ({ row, index, svgWidth, isCapable, isDragTarget = false }) => {
  const { setNodeRef: setDropRef } = useDroppable({ id: `line_${row.name}` });
  const setNodeRef = setDropRef as unknown as React.Ref<SVGGElement>;
  const y = HEADER_HEIGHT + index * LINE_HEIGHT;
  const half = LINE_HEIGHT / 2;

  let fill: string;
  if (isDragTarget) {
    fill = isCapable === false ? CHART.rowTargetBad : CHART.rowTargetOk;
  } else if (isCapable === true) {
    fill = CHART.rowCapable;
  } else if (isCapable === false) {
    fill = CHART.rowIncapable;
  } else {
    fill = index % 2 === 0 ? CHART.rowEven : CHART.rowOdd;
  }

  return (
    <g ref={setNodeRef}>
      <rect x={0} y={y} width={svgWidth} height={LINE_HEIGHT} fill={fill} />
      {/* Split rule between side A (top) and side B (bottom). */}
      {row.isDouble && (
        <line
          x1={LINE_LABEL_WIDTH}
          y1={y + half}
          x2={svgWidth}
          y2={y + half}
          stroke={CHART.dayLine}
          strokeDasharray="3 3"
        />
      )}
      <line x1={0} y1={y + LINE_HEIGHT} x2={svgWidth} y2={y + LINE_HEIGHT} stroke={CHART.dayLine} />
    </g>
  );
};

/** Sticky left strip: line names (+ A/B side letters) drawn ON TOP of the
 * blocks and kept at the container's left edge by a scroll-driven
 * translate in GanttChart. */
const LineLabelsOverlay: React.FC<{ rows: GanttRow[]; svgHeight: number }> = ({ rows, svgHeight }) => (
  <>
    <rect x={0} y={HEADER_HEIGHT} width={LINE_LABEL_WIDTH} height={svgHeight - HEADER_HEIGHT} fill={CHART.labelStrip} opacity={0.96} />
    <line x1={LINE_LABEL_WIDTH} y1={HEADER_HEIGHT} x2={LINE_LABEL_WIDTH} y2={svgHeight} stroke={T.rule} />
    {rows.map((row, i) => {
      const y = HEADER_HEIGHT + i * LINE_HEIGHT;
      const half = LINE_HEIGHT / 2;
      return (
        <g key={row.name}>
          <text x={6} y={y + LINE_HEIGHT / 2} dominantBaseline="middle" fontSize={11.5} fontWeight={700} fill={T.ink}>
            {row.name}
          </text>
          {row.isDouble && (
            <>
              <text x={LINE_LABEL_WIDTH - 12} y={y + half / 2} textAnchor="end" dominantBaseline="middle" fontSize={8} fill={T.ink3}>
                A
              </text>
              <text x={LINE_LABEL_WIDTH - 12} y={y + half + half / 2} textAnchor="end" dominantBaseline="middle" fontSize={8} fill={T.ink3}>
                B
              </text>
            </>
          )}
          <line x1={0} y1={y + LINE_HEIGHT} x2={LINE_LABEL_WIDTH} y2={y + LINE_HEIGHT} stroke={CHART.dayLine} />
        </g>
      );
    })}
  </>
);

export const GanttChart: React.FC<Props> = ({
  schedule, cipWindows, lines, viewStart, viewEnd, hourWidth, anchor,
  resizing, highlightSku, capableLines, lockedThroughH, insertPreview, dropGhost, svgRef: externalSvgRef,
  onResizeStart, onContextMenu, onEmptyContextMenu, onBlockClick, onZoomIn, onZoomOut, onResetZoom,
}) => {
  const localSvgRef = useRef<SVGSVGElement>(null);
  const svgRef = externalSvgRef ?? localSvgRef;
  // One display row per group: the two sides of a double line share a row.
  const rows = React.useMemo(() => buildRows(lines), [lines]);
  const svgWidth = LINE_LABEL_WIDTH + (viewEnd - viewStart) * hourWidth;
  const svgHeight = HEADER_HEIGHT + rows.length * LINE_HEIGHT + 4;

  // Frozen panes: the day/week header sticks to the top of whatever is
  // scrolling and the line-name strip sticks to the left edge. Two scroll
  // sources exist: the container's own overflow scroll (zoomed horizontal)
  // AND the parent Streamlit page — the component iframe auto-grows to full
  // content height, so vertical scrolling happens in the PARENT document.
  // The iframe is same-origin, so window.frameElement gives our position in
  // the parent viewport and we pin the header against it. Transforms are
  // set directly on the DOM (rAF-throttled) so scrolling stays 60fps.
  const scrollRef = useRef<HTMLDivElement>(null);
  const headerGRef = useRef<SVGGElement>(null);
  const labelsGRef = useRef<SVGGElement>(null);
  const scrollRafRef = useRef(0);
  const handleScroll = React.useCallback(() => {
    cancelAnimationFrame(scrollRafRef.current);
    scrollRafRef.current = requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (!el) return;
      let pageOffset = 0;
      try {
        const fe = window.frameElement as HTMLElement | null;
        const feTop = fe ? fe.getBoundingClientRect().top : 0;
        // Streamlit's top toolbar is a fixed overlay — pin below it, not
        // behind it (it swallowed the header at viewport y=0).
        let allowance = 0;
        const hdr = fe?.ownerDocument?.querySelector(
          'header[data-testid="stHeader"]');
        if (hdr) allowance = hdr.getBoundingClientRect().height;
        pageOffset = Math.max(
          0, allowance - (feTop + el.getBoundingClientRect().top));
      } catch {
        pageOffset = 0; // cross-origin embed: container scroll only
      }
      const maxY = Math.max(0, svgHeight - HEADER_HEIGHT - 8);
      const y = Math.min(el.scrollTop + pageOffset, maxY);
      headerGRef.current?.setAttribute("transform", `translate(0, ${y})`);
      labelsGRef.current?.setAttribute("transform", `translate(${el.scrollLeft}, 0)`);
    });
  }, [svgHeight]);
  React.useEffect(() => {
    handleScroll();
    let pw: Window | null = null;
    try {
      pw = window.parent && window.parent !== window ? window.parent : null;
      // touch the parent document to prove same-origin before subscribing
      void pw?.document;
    } catch {
      pw = null;
    }
    const opts: AddEventListenerOptions = { passive: true, capture: true };
    pw?.addEventListener("scroll", handleScroll, opts);
    pw?.addEventListener("resize", handleScroll, opts);
    return () => {
      pw?.removeEventListener("scroll", handleScroll, opts);
      pw?.removeEventListener("resize", handleScroll, opts);
    };
  }, [handleScroll]);

  const allBlocks = [...schedule, ...cipWindows];

  // Right-click on empty chart space -> (line, hour) for the SKU picker.
  // Blocks call stopPropagation in their own handler, so reaching the svg
  // means the click landed on a gap (or the header/label strip, filtered out).
  const handleSvgContextMenu = React.useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (!onEmptyContextMenu) return;
      const svg = svgRef.current;
      if (!svg) return;
      e.preventDefault(); // no browser menu anywhere on the chart
      // The frozen header band and line-label strip are scroll-translated
      // groups drawn ON TOP of the rows: while scrolled, their screen
      // position no longer matches the untransformed hit-test below, so a
      // right-click on them must not open the picker for whatever row/hour
      // they happen to cover. Both groups paint background rects, so any
      // click on them lands inside the group.
      const target = e.target as Node | null;
      if (target && (headerGRef.current?.contains(target) || labelsGRef.current?.contains(target))) {
        return;
      }
      const rect = svg.getBoundingClientRect();
      const xIn = e.clientX - rect.left;
      const yIn = e.clientY - rect.top;
      const rowIdx = Math.floor((yIn - HEADER_HEIGHT) / LINE_HEIGHT);
      if (xIn <= LINE_LABEL_WIDTH || rowIdx < 0 || rowIdx >= rows.length) return;
      const hour = xToHour(xIn, viewStart, hourWidth);
      if (hour < viewStart || hour > viewEnd) return;
      onEmptyContextMenu(e, rows[rowIdx].name, rows[rowIdx].lineId, hour);
    },
    [onEmptyContextMenu, rows, viewStart, viewEnd, hourWidth, svgRef],
  );

  return (
    <>
      {/* HTML zoom controls — above the SVG */}
      <ZoomControls onZoomIn={onZoomIn} onZoomOut={onZoomOut} onResetZoom={onResetZoom} />

      {/* Single SVG containing time axis + rows + blocks */}
      <style>{`
        .gantt-scroll { scrollbar-width: auto; scrollbar-color: #A9B4BF #E8ECF0; }
        .gantt-scroll::-webkit-scrollbar { height: 12px; width: 12px; }
        .gantt-scroll::-webkit-scrollbar-track { background: #E8ECF0; border-radius: 6px; }
        .gantt-scroll::-webkit-scrollbar-thumb { background: #A9B4BF; border-radius: 6px; border: 2px solid #E8ECF0; }
        .gantt-scroll::-webkit-scrollbar-thumb:hover { background: #7A8592; }
      `}</style>
      <div ref={scrollRef} onScroll={handleScroll} className="gantt-scroll" style={{ overflowX: "auto", overflowY: "auto", maxHeight: 640, width: "100%", border: `1px solid ${T.rule}`, borderRadius: 10, background: T.surface }}>
        <svg ref={svgRef as React.RefObject<SVGSVGElement>} width={svgWidth} height={svgHeight} style={{ display: "block" }} onContextMenu={handleSvgContextMenu}>
          {/* Hatch fills: downtime / maintenance / contractor windows are
              CONSTRAINTS, drawn as diagonal hatching so they never read as a
              production run. */}
          <defs>
            {(["line_down", "maintenance", "contractor"] as const).map((k) => (
              <pattern key={k} id={`fs-hatch-${k}`} patternUnits="userSpaceOnUse" width={8} height={8} patternTransform="rotate(45)">
                <rect width={8} height={8} fill="#FFFFFF" />
                <line x1={0} y1={0} x2={0} y2={8} stroke={TYPE_COLORS[k]} strokeWidth={3} opacity={0.7} />
              </pattern>
            ))}
          </defs>
          {/* Time axis body layer: gridlines that scroll with the rows */}
          <TimeAxisSvg
            viewStart={viewStart}
            viewEnd={viewEnd}
            hourWidth={hourWidth}
            anchor={anchor}
            svgWidth={svgWidth}
            svgHeight={svgHeight}
            layer="body"
          />

          {/* Line rows (droppable zones); highlight follows the drop ghost's
              live target row, never dnd-kit's stale collision rects. */}
          {rows.map((row, i) => (
            <LineRow
              key={row.name}
              row={row}
              index={i}
              svgWidth={svgWidth}
              isCapable={capableLines ? capableLines.has(row.name) : null}
              isDragTarget={dropGhost != null && rowIndexOf(rows, dropGhost.lineName) === i}
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
                fill={CHART.lockShade}
                opacity={0.09}
                pointerEvents="none"
              />
              {lockedThroughH <= viewEnd && (
                <g pointerEvents="none">
                  <line
                    x1={LINE_LABEL_WIDTH + (lockedThroughH - viewStart) * hourWidth}
                    y1={HEADER_HEIGHT - 6}
                    x2={LINE_LABEL_WIDTH + (lockedThroughH - viewStart) * hourWidth}
                    y2={HEADER_HEIGHT + rows.length * LINE_HEIGHT}
                    stroke={CHART.lockLine}
                    strokeWidth={2}
                    strokeDasharray="6 3"
                  />
                  <text
                    x={LINE_LABEL_WIDTH + (lockedThroughH - viewStart) * hourWidth - 6}
                    y={HEADER_HEIGHT + 12}
                    textAnchor="end"
                    fontSize={10}
                    fontWeight={700}
                    fill={CHART.lockLine}
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
                  rx={4} fill="none" stroke={CHART.insert} strokeWidth={2} strokeDasharray="5 3" opacity={0.8}
                />
                <line x1={ix} y1={y} x2={ix} y2={y + LINE_HEIGHT} stroke={CHART.insert} strokeWidth={2} />
                <text x={ix + 3} y={y + 10} fontSize={9} fontWeight={700} fill={CHART.insert}>
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
                  immovable={Boolean(block.locked) || Boolean(block.pinned)
                    || (block.attrs ?? "").includes("current_state:")
                    || (lockedThroughH != null
                        && block.start_hour < lockedThroughH - 1e-9)}
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

          {/* Drop ghost: the snapped landing cell the drop would commit,
              drawn over the blocks (translucent) while the drag overlay
              floats above. Green-ish/block-colored when the drop is valid,
              red-tinted when it would be refused. */}
          {dropGhost && (() => {
            const li = rowIndexOf(rows, dropGhost.lineName);
            if (li < 0) return null;
            const rowY = HEADER_HEIGHT + li * LINE_HEIGHT;
            const slot = blockSlot(rows[li], dropGhost.lineName, LINE_HEIGHT);
            const rowH = slot.height ?? LINE_HEIGHT;
            const pad = rowH >= LINE_HEIGHT ? 4 : 2; // mirror GanttBlock's inset
            const gx = hourToX(dropGhost.startHour, viewStart, hourWidth);
            const gw = Math.max(2, (dropGhost.endHour - dropGhost.startHour) * hourWidth);
            return (
              <g pointerEvents="none">
                <rect
                  x={gx}
                  y={rowY + slot.y + pad}
                  width={gw}
                  height={Math.max(6, rowH - pad * 2)}
                  rx={4}
                  fill={dropGhost.valid ? dropGhost.fill : T.bad}
                  fillOpacity={dropGhost.valid ? 0.35 : 0.2}
                  stroke={dropGhost.valid ? T.ink : T.bad}
                  strokeWidth={1.5}
                  strokeDasharray="5 3"
                />
              </g>
            );
          })()}

          {/* Frozen panes (drawn last = on top). The line-name strip pins to
              the viewport's left edge; the header band pins to its top.
              Their transforms are updated in handleScroll. */}
          <g ref={labelsGRef}>
            <LineLabelsOverlay rows={rows} svgHeight={svgHeight} />
          </g>
          <g ref={headerGRef}>
            <TimeAxisSvg
              viewStart={viewStart}
              viewEnd={viewEnd}
              hourWidth={hourWidth}
              anchor={anchor}
              svgWidth={svgWidth}
              svgHeight={svgHeight}
              layer="header"
            />
          </g>
        </svg>
      </div>
    </>
  );
};
