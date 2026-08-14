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
  // Non-blocking notices (e.g. "setup time not respected") — orange, not red.
  const [warnMsg, setWarnMsg] = useState<string | null>(null);

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

  // ── 2-week lock window ──
  // Blocks starting before locked_through_h are committed to the plant:
  // no drag, no resize, no typed edit — and nothing may be moved INTO the
  // frozen zone either (that would silently change the committed plan).
  const lockedThroughH = args.config.locked_through_h ?? null;
  const isBlockLocked = useCallback(
    (b: ScheduleBlock): boolean =>
      Boolean(b.locked) ||
      (lockedThroughH != null && b.start_hour < lockedThroughH - 1e-9),
    [lockedThroughH],
  );
  const lockReason = useCallback(
    (b: ScheduleBlock): string =>
      b.locked
        ? "Block is locked"
        : `Inside the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`,
    [lockedThroughH, anchor],
  );
  const intoLockedZone = useCallback(
    (startHour: number): boolean =>
      lockedThroughH != null && startHour < lockedThroughH - 1e-9,
    [lockedThroughH],
  );

  // Setup hours between two SKUs from the changeover matrix (0 when the pair
  // is unknown or either side is a non-production window: a CIP's 6h wash
  // absorbs any changeover).
  const setupBetween = useCallback(
    (from: ScheduleBlock | undefined, to: ScheduleBlock | undefined): number => {
      if (!from || !to) return 0;
      if (isWindowBlock(from.block_type) || isWindowBlock(to.block_type)) return 0;
      if (from.sku === to.sku) return 0;
      return Number(args.changeovers?.[from.sku]?.[to.sku] ?? 0) || 0;
    },
    [args.changeovers],
  );

  // Non-blocking honesty check after a placement: does the gap to either
  // neighbour undercut the required setup time? The planner MAY place blocks
  // closer (their call) — but never silently.
  const setupWarning = useCallback(
    (blockId: string, lineName: string, newStart: number, newEnd: number): string | null => {
      const others = [...schedule, ...cipWindows].filter(
        (b) => b.id !== blockId && b.line_name === lineName,
      );
      const me = [...schedule, ...cipWindows].find((b) => b.id === blockId);
      const left = others.filter((b) => b.end_hour <= newStart + 1e-9)
        .sort((a, b) => b.end_hour - a.end_hour)[0];
      const right = others.filter((b) => b.start_hour >= newEnd - 1e-9)
        .sort((a, b) => a.start_hour - b.start_hour)[0];
      const msgs: string[] = [];
      if (me && left) {
        const need = setupBetween(left, me);
        const gap = newStart - left.end_hour;
        if (need > 0 && gap < need - 1e-9) {
          msgs.push(`gap to ${left.sku || left.label} is ${gap.toFixed(1)}h but the changeover needs ${need}h`);
        }
      }
      if (me && right) {
        const need = setupBetween(me, right);
        const gap = right.start_hour - newEnd;
        if (need > 0 && gap < need - 1e-9) {
          msgs.push(`gap to ${right.sku || right.label} is ${gap.toFixed(1)}h but the changeover needs ${need}h`);
        }
      }
      return msgs.length
        ? `⚠ Setup time not respected: ${msgs.join("; ")}. The plant will need that time anyway.`
        : null;
    },
    [schedule, cipWindows, setupBetween],
  );

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
        if (intoLockedZone(startHour)) {
          reject(`Cannot drop into the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`);
          return;
        }
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
        if (moving && isBlockLocked(moving)) {
          reject(lockReason(moving));
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

      if (isBlockLocked(block)) {
        reject(lockReason(block));
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
        if (intoLockedZone(newStart)) {
          reject(`Cannot move into the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`);
          return;
        }
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
        setWarnMsg(setupWarning(block.id, block.line_name, newStart, newStart + dur));
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
        if (intoLockedZone(newStart)) {
          reject(`Cannot move into the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`);
          return;
        }
        const newEnd = newStart + dur;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, targetLine.line_name, block.id, newStart, newEnd)) {
          reject(`Overlap on ${targetLine.line_name} at ${hourToStamp(newStart, anchor)}`);
          return;
        }
        setErrorMsg(null);
        actions.moveBlock(block.id, targetLine.line_name, targetLine.line_id, newStart, dur);
        setWarnMsg(setupWarning(block.id, targetLine.line_name, newStart, newEnd));
      }
    },
    [schedule, cipWindows, holdingArea, actions, hourWidth, caps, lines, hourFromPointer, reject, anchor, downtime, isBlockLocked, lockReason, intoLockedZone, lockedThroughH],
  );

  const onResizeCommit = useCallback(
    (id: string, newStart: number, newEnd: number) => actions.resizeBlock(id, newStart, newEnd),
    [actions],
  );
  const { resizing, startResize } = useBlockResize(args.config.min_run_hours, onResizeCommit);

  // Resize entry gate: drags and edits check locks, so resize must too — an
  // ungated handle let a locked block be resized (found in slice-1 QA).
  const guardedStartResize = useCallback<typeof startResize>(
    (blockId, edge, startHour, endHour, clientX, hw) => {
      const block =
        schedule.find((b) => b.id === blockId) ?? cipWindows.find((b) => b.id === blockId);
      if (block && isBlockLocked(block)) {
        reject(lockReason(block));
        return;
      }
      startResize(blockId, edge, startHour, endHour, clientX, hw);
    },
    [schedule, cipWindows, isBlockLocked, lockReason, reject, startResize],
  );

  const { menu, openMenu, closeMenu } = useContextMenu();
  const handleContextMenu = useCallback(
    (e: React.MouseEvent, blockId: string) => {
      const block = [...schedule, ...cipWindows].find((b) => b.id === blockId);
      if (!block) return;
      if (isBlockLocked(block)) {
        reject(lockReason(block));
        return;
      }
      openMenu(e.clientX, e.clientY, blockId, block.block_type, block.start_hour, block.end_hour);
    },
    [schedule, cipWindows, openMenu, reject, isBlockLocked, lockReason],
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

  // Typed edits from the popover (start / duration / tonnage). Same guards as
  // a drag: locked blocks reject, min-run enforced, overlaps reject. Returns
  // null when committed (popover closes) or the rejection reason — the
  // popover shows it inline, because the chart's top banner is out of the
  // user's sight while the popup is open.
  const handleApplyEdit = useCallback(
    (blockId: string, edit: { startHour: number; durationH: number; qtyKg: number | null }): string | null => {
      const block =
        schedule.find((b) => b.id === blockId) ?? cipWindows.find((b) => b.id === blockId);
      if (!block) return "Block no longer exists";
      if (isBlockLocked(block)) {
        reject(lockReason(block));
        return lockReason(block);
      }
      if (intoLockedZone(edit.startHour)) {
        const msg = `Cannot move into the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`;
        reject(msg);
        return msg;
      }
      const minDur = isWindowBlock(block.block_type) ? 1 : args.config.min_run_hours;
      if (edit.durationH < minDur) {
        const msg = `Duration ${edit.durationH}h is under the ${minDur}h minimum`;
        reject(msg);
        return msg;
      }
      const newStart = Math.max(0, edit.startHour);
      const newEnd = newStart + edit.durationH;
      const allBlocks = [...schedule, ...cipWindows];
      const clash = allBlocks.find(
        (b) =>
          b.line_name === block.line_name &&
          b.id !== block.id &&
          b.start_hour < newEnd &&
          b.end_hour > newStart,
      );
      if (clash) {
        const msg =
          `Overlaps ${clash.sku || clash.label || clash.block_type} ` +
          `(${hourToStamp(clash.start_hour, anchor)} - ${hourToStamp(clash.end_hour, anchor)}) ` +
          `on ${block.line_name}`;
        reject(msg);
        return msg;
      }
      setErrorMsg(null);
      const patch: Partial<ScheduleBlock> = {
        start_hour: newStart,
        end_hour: newEnd,
        run_hours: edit.durationH,
      };
      if (!isWindowBlock(block.block_type)) {
        patch.qty_kg = edit.qtyKg ?? undefined;
      }
      actions.updateBlock(blockId, patch);
      actions.reportAction(
        `Edited ${block.order_id || block.sku}: ${hourToStamp(newStart, anchor)} for ${edit.durationH}h` +
          (edit.qtyKg ? `, ${edit.qtyKg.toLocaleString()} kg` : ""),
      );
      setWarnMsg(setupWarning(blockId, block.line_name, newStart, newEnd));
      return null;
    },
    [schedule, cipWindows, actions, reject, anchor, args.config.min_run_hours, isBlockLocked, lockReason, intoLockedZone, lockedThroughH],
  );

  // Snap flush against the neighbouring block, leaving EXACTLY the setup
  // time between the two SKUs (user request 2026-08-14). Returns null on
  // success or the reason it could not snap.
  const handleSnap = useCallback(
    (blockId: string, dir: "left" | "right"): string | null => {
      const block =
        schedule.find((b) => b.id === blockId) ?? cipWindows.find((b) => b.id === blockId);
      if (!block) return "Block no longer exists";
      if (isBlockLocked(block)) return lockReason(block);
      const dur = block.end_hour - block.start_hour;
      const others = [...schedule, ...cipWindows].filter(
        (b) => b.id !== blockId && b.line_name === block.line_name,
      );
      let newStart: number;
      let against: ScheduleBlock | undefined;
      let setup: number;
      if (dir === "left") {
        against = others
          .filter((b) => b.start_hour < block.start_hour + 1e-9)
          .sort((a, b) => b.end_hour - a.end_hour)[0];
        if (!against) return "No block to the left on this line";
        setup = setupBetween(against, block);
        newStart = against.end_hour + setup;
      } else {
        against = others
          .filter((b) => b.end_hour > block.end_hour - 1e-9)
          .sort((a, b) => a.start_hour - b.start_hour)[0];
        if (!against) return "No block to the right on this line";
        setup = setupBetween(block, against);
        newStart = against.start_hour - setup - dur;
      }
      if (newStart < 0) return "Not enough room before hour 0";
      if (intoLockedZone(newStart)) {
        return `Cannot move into the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`;
      }
      const newEnd = newStart + dur;
      const clash = others.find(
        (b) => b.start_hour < newEnd - 1e-9 && b.end_hour > newStart + 1e-9,
      );
      if (clash) {
        return `No room: would overlap ${clash.sku || clash.label} (${hourToStamp(clash.start_hour, anchor)} - ${hourToStamp(clash.end_hour, anchor)})`;
      }
      setErrorMsg(null);
      actions.updateBlock(blockId, {
        start_hour: newStart,
        end_hour: newEnd,
        run_hours: dur,
      });
      actions.reportAction(
        `Snapped ${block.order_id || block.sku} ${dir} against ${against.sku || against.label}` +
          (setup > 0 ? ` (setup ${setup}h respected)` : " (no setup needed)"),
      );
      return null;
    },
    [schedule, cipWindows, actions, isBlockLocked, lockReason, intoLockedZone, lockedThroughH, anchor, setupBetween],
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
      {warnMsg && (
        <div
          style={{
            background: "#fff8e1",
            color: "#8d6e00",
            border: "1px solid #ffe082",
            borderRadius: 6,
            padding: "6px 10px",
            fontSize: 12,
            marginBottom: 6,
          }}
          onClick={() => setWarnMsg(null)}
        >
          {warnMsg}
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
          lockedThroughH={lockedThroughH}
          svgRef={chartSvgRef}
          onResizeStart={guardedStartResize}
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
          SKU Adherence (live) — click a row to highlight on chart · "+" adds the missing tonnage to holding
        </strong>
        <AdherenceTable
          rows={adherenceRows}
          highlightSku={highlightSku}
          onSkuClick={setHighlightSku}
          onAddToHolding={(row, missingKg, runHours) => {
            actions.addToHolding(row.order_id, row.sku, Math.max(0.5, runHours), missingKg);
          }}
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
          onApply={isBlockLocked(popover.block) ? undefined : handleApplyEdit}
          onSnap={isBlockLocked(popover.block) ? undefined : handleSnap}
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
