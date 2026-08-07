// GanttSandbox.tsx — Main component: layout, state, Streamlit wiring.
// Owns the DndContext so drags work across chart, holding area, and palette.

import React, { useState, useCallback, useEffect, useMemo, useRef } from "react";
import {
  DndContext, DragOverlay, PointerSensor, useSensor, useSensors,
  type DragEndEvent, type DragStartEvent, type DragMoveEvent,
} from "@dnd-kit/core";
import type { SandboxArgs, ScheduleBlock, LineInfo } from "./types";
import { isWindowBlock } from "./types";
import { useScheduleState } from "./hooks/useScheduleState";
import { useBlockResize } from "./hooks/useBlockResize";
import { useContextMenu } from "./hooks/useContextMenu";
import { computeKpis, computeAdherence } from "./utils/kpi";
import { isCapable, recalcDuration, findOverlapsOnLine } from "./utils/validation";
import { LINE_HEIGHT, MIN_HOUR_WIDTH, MAX_HOUR_WIDTH, snapToHour, fitToWidth, xToHour, hourToStamp } from "./utils/layout";
import { getRate } from "./utils/validation";
import { computeDragPreview, type DragPreview } from "./utils/dragPreview";
import { isDouble } from "./utils/abLines";
import { buildRows } from "./utils/ganttRows";
import { skuColor, skuTextColor, blockLabel } from "./utils/colors";
import { setComponentValue, setFrameHeight } from "./streamlit";

import { KpiBar } from "./components/KpiBar";
import { GanttChart } from "./components/GanttChart";
import { HoldingArea } from "./components/HoldingArea";
import { Palette } from "./components/Palette";
import { AdherenceTable } from "./components/AdherenceTable";
import { ContextMenu } from "./components/ContextMenu";
import { BlockPopover } from "./components/BlockPopover";
import { DragPreviewBadge } from "./components/DragPreviewBadge";

interface Props {
  args: SandboxArgs;
}

export const GanttSandbox: React.FC<Props> = ({ args }) => {
  const [data, actions] = useScheduleState(args);
  const { schedule, cipWindows, holdingArea, lastAction } = data;

  const horizon = args.config.horizon_hours || 336;
  const containerRef = useRef<HTMLDivElement>(null);
  const chartSvgRef = useRef<SVGSVGElement | null>(null);

  // ── View state: auto-fit 2 weeks ──
  const [hourWidth, setHourWidth] = useState(() => {
    const estimatedWidth = 1200;
    return fitToWidth(estimatedWidth, horizon);
  });
  const [viewStart, setViewStart] = useState(0);
  const viewEnd = viewStart + horizon;

  const anchor = useMemo(() => new Date(args.config.planning_anchor), [args.config.planning_anchor]);
  const caps = args.capabilities;
  // The chart draws ONE row per group (a double line's A and B sides share a
  // row), so every index-based drag calculation must use the same collapsed
  // list, not the raw per-side lines.csv rows.
  const lines = useMemo<LineInfo[]>(
    () => buildRows(args.lines).map((r) => ({
      line_id: r.lineId,
      line_name: r.name,
      line_group: r.name,
      is_double: r.isDouble,
    })),
    [args.lines],
  );
  // Per-side scheduled downtime (STEP 1 of the workflow). Drives the half-rate
  // duration maths for the Bossar double lines P17-P22.
  const downtime = useMemo(() => args.sideDowntime ?? {}, [args.sideDowntime]);

  useEffect(() => {
    const measure = () => {
      const w = containerRef.current?.offsetWidth ?? 1200;
      setHourWidth(fitToWidth(w, horizon));
      setViewStart(0);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [horizon]);

  const [highlightSku, setHighlightSku] = useState<string | null>(null);
  const [activeDragSku, setActiveDragSku] = useState<string | null>(null);
  const [activeDragBlock, setActiveDragBlock] = useState<ScheduleBlock | null>(null);
  const [dragPreview, setDragPreview] = useState<DragPreview | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const capableLines = useMemo(() => {
    if (!activeDragSku) return null;
    const set = new Set<string>();
    for (const ln of lines) {
      if (isCapable(ln.line_name, activeDragSku, caps)) set.add(ln.line_name);
    }
    return set;
  }, [activeDragSku, lines, caps]);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const reject = useCallback((msg: string) => {
    setErrorMsg(msg);
    actions.reportAction(`Rejected: ${msg}`);
  }, [actions]);

  const hourFromPointer = useCallback((clientX: number | undefined): number => {
    const svg = chartSvgRef.current;
    if (!svg || clientX == null) return 0;
    const rect = svg.getBoundingClientRect();
    const xInSvg = clientX - rect.left;
    return Math.max(0, Math.min(horizon - 1, snapToHour(xToHour(xInSvg, viewStart, hourWidth))));
  }, [horizon, viewStart, hourWidth]);

  const onDragStart = useCallback(
    (event: DragStartEvent) => {
      setErrorMsg(null);
      setDragPreview(null);
      const blockData = event.active.data.current?.block as ScheduleBlock | undefined;
      if (blockData) {
        setActiveDragBlock(blockData);
        if (!isWindowBlock(blockData.block_type)) {
          setActiveDragSku(blockData.sku);
        }
      }
    },
    [],
  );

  // Live, rate-aware preview of the placement the drop would produce.
  // Never mutates committed state - purely visual until onDragEnd.
  const onDragMove = useCallback(
    (event: DragMoveEvent) => {
      const { active, over, delta } = event;
      if (!active) return;
      const activeId = active.id as string;
      const block =
        (active.data.current?.block as ScheduleBlock | undefined) ??
        schedule.find((b) => b.id === activeId) ??
        cipWindows.find((b) => b.id === activeId);
      if (!block) return;
      const translated = active.rect.current.translated;
      const pointerX = translated ? translated.left + translated.width / 2 : undefined;
      setDragPreview(
        computeDragPreview({
          block,
          activeId,
          overId: over?.id as string | undefined,
          deltaX: delta.x,
          deltaY: delta.y,
          pointerHour: hourFromPointer(pointerX),
          lines,
          caps,
          allBlocks: [...schedule, ...cipWindows],
          hourWidth,
          lineHeight: LINE_HEIGHT,
          anchor,
          downtime,
        }),
      );
    },
    [schedule, cipWindows, lines, caps, hourWidth, hourFromPointer, anchor, downtime],
  );

  const onDragCancel = useCallback(() => {
    setActiveDragSku(null);
    setActiveDragBlock(null);
    setDragPreview(null);
  }, []);

  const onDragEnd = useCallback(
    (event: DragEndEvent) => {
      setActiveDragSku(null);
      setActiveDragBlock(null);
      setDragPreview(null);
      const { active, over, delta } = event;
      if (!active) return;

      const activeId = active.id as string;
      const overId = over?.id as string | undefined;
      const translated = active.rect.current.translated;
      const pointerX = translated
        ? translated.left + translated.width / 2
        : undefined;

      // ── Drag FROM holding TO a line row ──
      if (activeId.startsWith("holding_")) {
        if (!overId?.startsWith("line_")) {
          reject("Drop onto a production line to restore from holding");
          return;
        }
        const blockId = activeId.replace("holding_", "");
        const targetLineName = overId.replace("line_", "");
        const targetLine = lines.find((l) => l.line_name === targetLineName);
        if (!targetLine) return;
        const block = holdingArea.find((b) => b.id === blockId);
        if (!block) return;
        if (block.locked) {
          reject("Block is locked");
          return;
        }
        if (block.block_type !== "cip" && !isCapable(targetLineName, block.sku, caps)) {
          reject(`Line ${targetLineName} cannot run ${block.sku}`);
          return;
        }
        let dur = block.run_hours;
        if (block.block_type !== "cip") {
          const newDur = recalcDuration(block, targetLineName, caps, downtime, hourFromPointer(pointerX));
          if (newDur !== null) dur = newDur;
        }
        const startHour = hourFromPointer(pointerX);
        const endHour = startHour + dur;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, targetLineName, block.id, startHour, endHour)) {
          reject(`Overlap on ${targetLineName} at ${hourToStamp(startHour, anchor)}`);
          return;
        }
        setErrorMsg(null);
        actions.restoreFromHolding(blockId, targetLine.line_name, targetLine.line_id, startHour, dur);
        return;
      }

      // ── Drag TO holding area ──
      if (overId === "holding_area") {
        const moving =
          schedule.find((b) => b.id === activeId) ??
          cipWindows.find((b) => b.id === activeId);
        if (moving?.locked) {
          reject("Block is locked");
          return;
        }
        setErrorMsg(null);
        actions.removeToHolding(activeId);
        return;
      }

      // ── Normal in-chart drag ──
      const block =
        schedule.find((b) => b.id === activeId) ??
        cipWindows.find((b) => b.id === activeId);
      if (!block) return;

      if (block.locked) {
        reject("Block is locked");
        return;
      }

      const deltaHours = snapToHour(delta.x / hourWidth);
      const startLineIndex = lines.findIndex((l) => l.line_name === block.line_name);
      const lineDelta = Math.round(delta.y / LINE_HEIGHT);
      const targetLineIndex = Math.max(0, Math.min(startLineIndex + lineDelta, lines.length - 1));
      const targetLine = lines[targetLineIndex];
      if (!targetLine) return;

      const sameLine = targetLine.line_name === block.line_name;

      if (sameLine) {
        const newStart = Math.max(0, snapToHour(block.start_hour + deltaHours));
        if (newStart === block.start_hour) return;
        // On a double line, sliding into or out of a side-down window changes
        // how long the block takes, so re-integrate rather than keep the hours.
        let dur = block.run_hours;
        if (block.block_type !== "cip" && isDouble(block.line_name)) {
          const newDur = recalcDuration(block, block.line_name, caps, downtime, newStart);
          if (newDur !== null) dur = newDur;
        }
        const newEnd = newStart + dur;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, block.line_name, block.id, newStart, newEnd)) {
          reject(`Overlap on ${block.line_name} at ${hourToStamp(newStart, anchor)}`);
          return;
        }
        setErrorMsg(null);
        actions.moveBlock(block.id, block.line_name, block.line_id, newStart, dur);
      } else {
        if (block.block_type !== "cip" && !isCapable(targetLine.line_name, block.sku, caps)) {
          reject(`Line ${targetLine.line_name} cannot run ${block.sku}`);
          return;
        }
        let dur = block.run_hours;
        if (block.block_type !== "cip") {
          const newStartForCalc = Math.max(0, snapToHour(block.start_hour + deltaHours));
          const newDur = recalcDuration(block, targetLine.line_name, caps, downtime, newStartForCalc);
          if (newDur === null) {
            reject(`No rate for ${block.sku} on ${targetLine.line_name}`);
            return;
          }
          dur = newDur;
        }
        const newStart = Math.max(0, snapToHour(block.start_hour + deltaHours));
        const newEnd = newStart + dur;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, targetLine.line_name, block.id, newStart, newEnd)) {
          reject(`Overlap on ${targetLine.line_name} at ${hourToStamp(newStart, anchor)}`);
          return;
        }
        setErrorMsg(null);
        actions.moveBlock(block.id, targetLine.line_name, targetLine.line_id, newStart, dur);
      }
    },
    [schedule, cipWindows, holdingArea, actions, hourWidth, caps, lines, hourFromPointer, reject, anchor, downtime],
  );

  const onResizeCommit = useCallback(
    (id: string, newStart: number, newEnd: number) => actions.resizeBlock(id, newStart, newEnd),
    [actions],
  );
  const { resizing, startResize } = useBlockResize(args.config.min_run_hours, onResizeCommit);

  const { menu, openMenu, closeMenu } = useContextMenu();
  const handleContextMenu = useCallback(
    (e: React.MouseEvent, blockId: string) => {
      const block = [...schedule, ...cipWindows].find((b) => b.id === blockId);
      if (!block) return;
      if (block.locked) {
        reject("Block is locked");
        return;
      }
      openMenu(e.clientX, e.clientY, blockId, block.block_type, block.start_hour, block.end_hour);
    },
    [schedule, cipWindows, openMenu, reject],
  );

  const [popover, setPopover] = useState<{ block: ScheduleBlock; x: number; y: number } | null>(null);
  const handleBlockClick = useCallback(
    (blockId: string) => {
      const block = [...schedule, ...cipWindows].find((b) => b.id === blockId);
      if (!block) return;
      setPopover({ block, x: 300, y: 200 });
    },
    [schedule, cipWindows],
  );

  const kpis = useMemo(
    () => computeKpis(schedule, cipWindows, args.demandTargets, args.capabilities),
    [schedule, cipWindows, args.demandTargets, args.capabilities],
  );
  const adherenceRows = useMemo(
    () => computeAdherence(schedule, args.demandTargets, args.capabilities),
    [schedule, args.demandTargets, args.capabilities],
  );

  const zoomIn = useCallback(() => setHourWidth((w) => Math.min(w * 1.3, MAX_HOUR_WIDTH)), []);
  const zoomOut = useCallback(() => setHourWidth((w) => Math.max(w / 1.3, MIN_HOUR_WIDTH)), []);
  const resetZoom = useCallback(() => {
    const w = containerRef.current?.offsetWidth ?? 1200;
    setHourWidth(fitToWidth(w, horizon));
    setViewStart(0);
  }, [horizon]);

  const lastPushed = useRef("");
  useEffect(() => {
    const key = JSON.stringify({ schedule, cipWindows, holdingArea, lastAction });
    if (key !== lastPushed.current) {
      lastPushed.current = key;
      setComponentValue({ schedule, cipWindows, holdingArea, lastAction });
    }
  }, [schedule, cipWindows, holdingArea, lastAction]);

  useEffect(() => {
    const h = containerRef.current?.scrollHeight ?? 800;
    setFrameHeight(h + 20);
  });

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === "z") { e.preventDefault(); actions.undo(); }
      if (e.ctrlKey && e.key === "y") { e.preventDefault(); actions.redo(); }
      if (e.key === "Escape") { closeMenu(); setPopover(null); setHighlightSku(null); setErrorMsg(null); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [actions, closeMenu]);

  const ghostBg = activeDragBlock
    ? skuColor(activeDragBlock.sku, activeDragBlock.block_type)
    : "#00f2c3";
  const ghostFg = skuTextColor(ghostBg);

  // Width of the drag ghost in px: the previewed duration on the hovered line,
  // falling back to the block's committed duration before the first move event.
  const ghostHours = dragPreview?.hours ?? activeDragBlock?.run_hours ?? 0;
  const ghostWidth = Math.max(60, ghostHours * hourWidth);

  return (
    <div
      ref={containerRef}
      style={{ fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" }}
      onClick={() => { closeMenu(); setPopover(null); }}
    >
      <KpiBar kpis={kpis} />

      {errorMsg && (
        <div
          style={{
            background: "#fdecea",
            color: "#b71c1c",
            border: "1px solid #f5c6cb",
            borderRadius: 6,
            padding: "6px 10px",
            fontSize: 12,
            marginBottom: 6,
          }}
        >
          {errorMsg}
        </div>
      )}

      <DndContext
        sensors={sensors}
        onDragStart={onDragStart}
        onDragMove={onDragMove}
        onDragEnd={onDragEnd}
        onDragCancel={onDragCancel}
      >
        <GanttChart
          schedule={schedule}
          cipWindows={cipWindows}
          lines={args.lines}
          viewStart={viewStart}
          viewEnd={Math.min(viewEnd, horizon)}
          hourWidth={hourWidth}
          anchor={anchor}
          resizing={resizing}
          highlightSku={highlightSku}
          capableLines={capableLines}
          svgRef={chartSvgRef}
          onResizeStart={startResize}
          onContextMenu={handleContextMenu}
          onBlockClick={handleBlockClick}
          onZoomIn={zoomIn}
          onZoomOut={zoomOut}
          onResetZoom={resetZoom}
        />

        <div style={{ marginTop: 8 }}>
          <HoldingArea blocks={holdingArea} />
        </div>

        <DragOverlay dropAnimation={null}>
          {activeDragBlock ? (
            <div style={{ pointerEvents: "none" }}>
              <div
                style={{
                  background: ghostBg,
                  color: ghostFg,
                  padding: "6px 10px",
                  borderRadius: 4,
                  fontSize: 12,
                  fontWeight: 600,
                  boxShadow: "0 4px 12px rgba(0,0,0,0.25)",
                  opacity: 0.9,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  boxSizing: "border-box",
                  // Rate-aware live resize: the ghost takes the width the block
                  // would occupy on the hovered line (hours x hourWidth).
                  width: ghostWidth,
                  height: LINE_HEIGHT - 8,
                  lineHeight: `${LINE_HEIGHT - 20}px`,
                  border: dragPreview && !dragPreview.valid ? "2px solid #b71c1c" : "2px solid #333",
                  filter: dragPreview && !dragPreview.valid ? "saturate(0.4)" : undefined,
                }}
              >
                {blockLabel(activeDragBlock.block_type, activeDragBlock.sku, activeDragBlock.label)}
                {dragPreview ? ` (${dragPreview.hours.toFixed(1)}h)` : ""}
              </div>
              {dragPreview && <DragPreviewBadge preview={dragPreview} anchor={anchor} />}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>

      <Palette
        lines={lines}
        cipDuration={args.config.cip_duration_h}
        onAddCip={actions.addCip}
        onAddTrial={actions.addTrial}
        onAddWindowBlock={actions.addWindowBlock}
      />

      <div style={{ marginTop: 8 }}>
        <strong style={{ fontSize: 13, display: "block", marginBottom: 4 }}>
          SKU Adherence (live) — click a row to highlight on chart
        </strong>
        <AdherenceTable
          rows={adherenceRows}
          highlightSku={highlightSku}
          onSkuClick={setHighlightSku}
        />
      </div>

      <ContextMenu
        menu={menu}
        onSplit={actions.splitBlock}
        onRemove={actions.removeToHolding}
        onDetails={handleBlockClick}
        onClose={closeMenu}
        minRunHours={args.config.min_run_hours}
        anchor={anchor}
      />

      {popover && (
        <BlockPopover
          block={popover.block}
          x={popover.x}
          y={popover.y}
          rate={getRate(popover.block.line_name, popover.block.sku, args.capabilities)}
          anchor={anchor}
          onClose={() => setPopover(null)}
        />
      )}

      {lastAction && (
        <div style={{ fontSize: 11, color: lastAction.startsWith("Rejected") ? "#b71c1c" : "#888", marginTop: 4 }}>
          Last action: {lastAction}
        </div>
      )}
    </div>
  );
};
