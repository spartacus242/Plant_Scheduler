// GanttSandbox.tsx — Main component: layout, state, Streamlit wiring.
// Owns the DndContext so drags work across chart, holding area, and palette.

import React, { useState, useCallback, useEffect, useMemo, useRef } from "react";
import {
  DndContext, DragOverlay, PointerSensor, useSensor, useSensors,
  type DragEndEvent, type DragStartEvent,
} from "@dnd-kit/core";
import type { SandboxArgs, ScheduleBlock } from "./types";
import { useScheduleState } from "./hooks/useScheduleState";
import { useBlockResize } from "./hooks/useBlockResize";
import { useContextMenu } from "./hooks/useContextMenu";
import { computeKpis, computeAdherence } from "./utils/kpi";
import { isCapable, recalcDuration, findOverlapsOnLine } from "./utils/validation";
import { LINE_HEIGHT, MIN_HOUR_WIDTH, MAX_HOUR_WIDTH, snapToHour, fitToWidth } from "./utils/layout";
import { getRate } from "./utils/validation";
import { setComponentValue, setFrameHeight } from "./streamlit";

import { KpiBar } from "./components/KpiBar";
import { GanttChart } from "./components/GanttChart";
import { HoldingArea } from "./components/HoldingArea";
import { Palette } from "./components/Palette";
import { AdherenceTable } from "./components/AdherenceTable";
import { ContextMenu } from "./components/ContextMenu";
import { BlockPopover } from "./components/BlockPopover";

interface Props {
  args: SandboxArgs;
}

export const GanttSandbox: React.FC<Props> = ({ args }) => {
  const [data, actions] = useScheduleState(args);
  const { schedule, cipWindows, holdingArea, lastAction } = data;

  const horizon = args.config.horizon_hours || 336;
  const containerRef = useRef<HTMLDivElement>(null);

  // ── View state: auto-fit 2 weeks ──
  const [hourWidth, setHourWidth] = useState(() => {
    // Start with a reasonable default; will auto-fit on mount
    const estimatedWidth = 1200;
    return fitToWidth(estimatedWidth, horizon);
  });
  const [viewStart, setViewStart] = useState(0);
  const viewEnd = viewStart + horizon; // Always show full horizon

  const anchor = useMemo(() => new Date(args.config.planning_anchor), [args.config.planning_anchor]);
  const caps = args.capabilities;
  const lines = args.lines;

  // Auto-fit to container width on mount and resize
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

  // ── Highlight state (click SKU in adherence table) ──
  const [highlightSku, setHighlightSku] = useState<string | null>(null);

  // ── Capable-line highlighting during drag ──
  const [activeDragSku, setActiveDragSku] = useState<string | null>(null);
  const capableLines = useMemo(() => {
    if (!activeDragSku) return null;
    const set = new Set<string>();
    for (const ln of lines) {
      if (isCapable(ln.line_name, activeDragSku, caps)) set.add(ln.line_name);
    }
    return set;
  }, [activeDragSku, lines, caps]);

  // ── Sensors ──
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  // ── Drag start: track which SKU is being dragged ──
  const onDragStart = useCallback(
    (event: DragStartEvent) => {
      const blockData = event.active.data.current?.block as ScheduleBlock | undefined;
      if (blockData && blockData.block_type !== "cip") {
        setActiveDragSku(blockData.sku);
      }
    },
    [],
  );

  // ── Drag end: unified handler ──
  const onDragEnd = useCallback(
    (event: DragEndEvent) => {
      setActiveDragSku(null);
      const { active, over, delta } = event;
      if (!active) return;

      const activeId = active.id as string;
      const overId = over?.id as string | undefined;

      // ── Drag FROM holding TO a line row ──
      if (activeId.startsWith("holding_")) {
        if (!overId?.startsWith("line_")) return;
        const blockId = activeId.replace("holding_", "");
        const targetLineName = overId.replace("line_", "");
        const targetLine = lines.find((l) => l.line_name === targetLineName);
        if (!targetLine) return;
        const block = holdingArea.find((b) => b.id === blockId);
        if (!block) return;
        if (block.block_type !== "cip" && !isCapable(targetLineName, block.sku, caps)) return;
        let dur = block.run_hours;
        if (block.block_type !== "cip") {
          const newDur = recalcDuration(block, targetLineName, caps);
          if (newDur !== null) dur = newDur;
        }
        actions.restoreFromHolding(blockId, targetLine.line_name, targetLine.line_id, 0, dur);
        return;
      }

      // ── Drag TO holding area ──
      if (overId === "holding_area") {
        actions.removeToHolding(activeId);
        return;
      }

      // ── Normal in-chart drag: compute target line from delta.y ──
      const block =
        schedule.find((b) => b.id === activeId) ??
        cipWindows.find((b) => b.id === activeId);
      if (!block) return;

      const deltaHours = snapToHour(delta.x / hourWidth);
      // Compute target line from vertical delta
      const startLineIndex = lines.findIndex((l) => l.line_name === block.line_name);
      const lineDelta = Math.round(delta.y / LINE_HEIGHT);
      const targetLineIndex = Math.max(0, Math.min(startLineIndex + lineDelta, lines.length - 1));
      const targetLine = lines[targetLineIndex];
      if (!targetLine) return;

      const sameLine = targetLine.line_name === block.line_name;

      if (sameLine) {
        const newStart = Math.max(0, snapToHour(block.start_hour + deltaHours));
        if (newStart === block.start_hour) return; // no change
        const newEnd = newStart + block.run_hours;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, block.line_name, block.id, newStart, newEnd)) return;
        actions.moveBlock(block.id, block.line_name, block.line_id, newStart, block.run_hours);
      } else {
        // Cross-line move
        if (block.block_type !== "cip" && !isCapable(targetLine.line_name, block.sku, caps)) return;
        let dur = block.run_hours;
        if (block.block_type !== "cip") {
          const newDur = recalcDuration(block, targetLine.line_name, caps);
          if (newDur === null) return;
          dur = newDur;
        }
        const newStart = Math.max(0, snapToHour(block.start_hour + deltaHours));
        const newEnd = newStart + dur;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, targetLine.line_name, block.id, newStart, newEnd)) return;
        actions.moveBlock(block.id, targetLine.line_name, targetLine.line_id, newStart, dur);
      }
    },
    [schedule, cipWindows, holdingArea, actions, hourWidth, caps, lines],
  );

  // ── Resize ──
  const onResizeCommit = useCallback(
    (id: string, newStart: number, newEnd: number) => actions.resizeBlock(id, newStart, newEnd),
    [actions],
  );
  const { resizing, startResize } = useBlockResize(args.config.min_run_hours, onResizeCommit);

  // ── Context menu ──
  const { menu, openMenu, closeMenu } = useContextMenu();
  const handleContextMenu = useCallback(
    (e: React.MouseEvent, blockId: string) => {
      const block = [...schedule, ...cipWindows].find((b) => b.id === blockId);
      if (!block) return;
      openMenu(e.clientX, e.clientY, blockId, block.block_type, block.start_hour, block.end_hour);
    },
    [schedule, cipWindows, openMenu],
  );

  // ── Block popover ──
  const [popover, setPopover] = useState<{ block: ScheduleBlock; x: number; y: number } | null>(null);
  const handleBlockClick = useCallback(
    (blockId: string) => {
      const block = [...schedule, ...cipWindows].find((b) => b.id === blockId);
      if (!block) return;
      setPopover({ block, x: 300, y: 200 });
    },
    [schedule, cipWindows],
  );

  // ── KPIs ──
  const kpis = useMemo(
    () => computeKpis(schedule, cipWindows, args.demandTargets, args.capabilities),
    [schedule, cipWindows, args.demandTargets, args.capabilities],
  );
  const adherenceRows = useMemo(
    () => computeAdherence(schedule, args.demandTargets, args.capabilities),
    [schedule, args.demandTargets, args.capabilities],
  );

  // ── Zoom/pan ──
  const zoomIn = useCallback(() => setHourWidth((w) => Math.min(w * 1.3, MAX_HOUR_WIDTH)), []);
  const zoomOut = useCallback(() => setHourWidth((w) => Math.max(w / 1.3, MIN_HOUR_WIDTH)), []);
  const resetZoom = useCallback(() => {
    const w = containerRef.current?.offsetWidth ?? 1200;
    setHourWidth(fitToWidth(w, horizon));
    setViewStart(0);
  }, [horizon]);
  // pan removed — full 2-week view fits in viewport, no scrolling needed

  // ── Push state to Python ──
  const lastPushed = useRef("");
  useEffect(() => {
    const key = JSON.stringify({ schedule, cipWindows, holdingArea });
    if (key !== lastPushed.current) {
      lastPushed.current = key;
      setComponentValue({ schedule, cipWindows, holdingArea, lastAction });
    }
  }, [schedule, cipWindows, holdingArea, lastAction]);

  // ── Auto-size iframe ──
  useEffect(() => {
    const h = containerRef.current?.scrollHeight ?? 800;
    setFrameHeight(h + 20);
  });

  // ── Keyboard shortcuts ──
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === "z") { e.preventDefault(); actions.undo(); }
      if (e.ctrlKey && e.key === "y") { e.preventDefault(); actions.redo(); }
      if (e.key === "Escape") { closeMenu(); setPopover(null); setHighlightSku(null); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [actions, closeMenu]);

  return (
    <div
      ref={containerRef}
      style={{ fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" }}
      onClick={() => { closeMenu(); setPopover(null); }}
    >
      <KpiBar kpis={kpis} />

      <DndContext sensors={sensors} onDragStart={onDragStart} onDragEnd={onDragEnd}>
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

        <DragOverlay />
      </DndContext>

      <Palette
        lines={args.lines}
        cipDuration={args.config.cip_duration_h}
        onAddCip={actions.addCip}
        onAddTrial={actions.addTrial}
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
      />

      {popover && (
        <BlockPopover
          block={popover.block}
          x={popover.x}
          y={popover.y}
          rate={getRate(popover.block.line_name, popover.block.sku, args.capabilities)}
          onClose={() => setPopover(null)}
        />
      )}

      {lastAction && (
        <div style={{ fontSize: 11, color: "#888", marginTop: 4 }}>
          Last action: {lastAction}
        </div>
      )}
    </div>
  );
};
