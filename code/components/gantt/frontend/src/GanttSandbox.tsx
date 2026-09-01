// GanttSandbox.tsx — Main component: layout, state, Streamlit wiring.
// Owns the DndContext so drags work across chart, holding area, and palette.

import React, { useState, useCallback, useEffect, useMemo, useRef } from "react";
import {
  DndContext, DragOverlay, PointerSensor, useSensor, useSensors,
  type Active, type DragEndEvent, type DragStartEvent, type DragMoveEvent,
} from "@dnd-kit/core";
import type { SandboxArgs, ScheduleBlock, LineInfo } from "./types";
import { isWindowBlock } from "./types";
import { useScheduleState } from "./hooks/useScheduleState";
import { useBlockResize } from "./hooks/useBlockResize";
import { useContextMenu } from "./hooks/useContextMenu";
import { computeKpis, computeAdherence, checkOverlapsSimple, serverKpisToKpiData, orderTarget, weekFulfillmentCredit } from "./utils/kpi";
import { isCapable, recalcDuration, findOverlapsOnLine } from "./utils/validation";
import { LINE_HEIGHT, MIN_HOUR_WIDTH, MAX_HOUR_WIDTH, fitToWidth, hourToStamp, displayOrderId, setDemandBaseWeek, isoWeekLabel, isoWeekAtHour, isPastDemandWeek } from "./utils/layout";
import { getRate } from "./utils/validation";
import { computeDragPreview, computeInsertPlan, samePreview, type DragPreview, type InsertContext } from "./utils/dragPreview";
import { planDrop, type DropPlan } from "./utils/dropPlan";
import { isDouble, groupOf, sideOf } from "./utils/abLines";
import { buildRows } from "./utils/ganttRows";
import { skuColor, skuTextColor, blockLabel } from "./utils/colors";
import { coFlagsForPair, planPlacement, remainingDemandBySku, splitPlacementByOrders, type PlacementPlan } from "./utils/skuPicker";
import { setComponentValue, setFrameHeight } from "./streamlit";

import { GanttChart } from "./components/GanttChart";
import { HoldingArea } from "./components/HoldingArea";
import { AdherenceTable } from "./components/AdherenceTable";
import { ContextMenu } from "./components/ContextMenu";
import { BlockPopover } from "./components/BlockPopover";
import { HoldingPlacePopover, type HoldingPlaceRow } from "./components/HoldingPlacePopover";
import { deriveAutoHolding } from "./utils/holdingDerive";
import { SkuPickerPopover, type PickerRowData } from "./components/SkuPickerPopover";
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
  // NON-BOARD credit: made kg from completed MOs the board hides (server
  // builds it made_only — never committed-MO kg, those ARE board blocks).
  // Added on top of board kg in every live fulfillment computation.
  const coveredByOrder = useMemo(
    () => args.kpis?.covered_by_order ?? undefined,
    [args.kpis],
  );
  // Order-id week labels count from the DEMAND anchor's ISO week (see
  // setDemandBaseWeek) — set before any child renders labels.
  setDemandBaseWeek(args.config.demand_base_iso_week ?? null);
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

  // A user zoom must survive resize events. The frame-height effect makes
  // Streamlit resize the iframe on EVERY render, which fires window.resize
  // INSIDE the iframe, which used to re-fit the chart — every zoom click was
  // reverted within a frame, so nothing ever overflowed and the scrollbars
  // never appeared (user report 2026-08-14: zoom + scroll both dead).
  const userZoomed = useRef(false);
  useEffect(() => {
    const measure = () => {
      if (userZoomed.current) return;  // never clobber a chosen zoom
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
  // Planner-pinned blocks are immovable like MOs — the solver plans around
  // them — but unlike locked blocks the PLANNER can free them again via the
  // popup's unpin toggle, so the reason says how.
  // Committed manprg MOs are plant fact — never movable on the board
  // (user report 2026-08-18: MOs could be dragged and slid apart by
  // insert-between). The solver plans AROUND them; so does the planner.
  const isCommittedMo = useCallback(
    (b: ScheduleBlock): boolean =>
      (b.attrs ?? "").includes("current_state:") &&
      // Projected cleans are a FORECAST of the plant's CIP grid, not plant
      // fact: a planner CIP re-forecasts them (2026-09-01). Scheduled
      // cleans (cip_scheduled) stay locked.
      !(b.block_type === "cip" && (b.attrs ?? "").includes("cip_projected")),
    [],
  );
  const isBlockLocked = useCallback(
    (b: ScheduleBlock): boolean =>
      Boolean(b.locked) ||
      Boolean(b.pinned) ||
      isCommittedMo(b) ||
      (lockedThroughH != null && b.start_hour < lockedThroughH - 1e-9),
    [lockedThroughH, isCommittedMo],
  );
  const lockReason = useCallback(
    (b: ScheduleBlock): string =>
      b.locked
        ? "Block is locked"
        : b.pinned
          ? "📌 Pinned for the solver — unpin it in the block popup to move it"
          : isCommittedMo(b)
            ? "Committed manprg MO — the plant is already running this plan"
            : `Inside the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`,
    [lockedThroughH, anchor, isCommittedMo],
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

  const insertCtx = useMemo<InsertContext>(() => ({
    setupBetween: (from, to) => setupBetween(from, to),
    horizonH: horizon,
    lockedThroughH,
    isBlockLocked,
  }), [setupBetween, horizon, lockedThroughH, isBlockLocked]);

  // ── Landing geometry ──
  // The ghost's CURRENT rect against the svg's CURRENT rect, read at the
  // same instant - never pointer deltas (dnd-kit's scroll adjustment is dead
  // for SVG draggables, and the parent-page auto-scroll is invisible to the
  // iframe entirely). Used by the live ghost preview AND the drop commit, so
  // the block always lands exactly where the ghost showed.
  const rowNames = useMemo(() => lines.map((l) => l.line_name), [lines]);
  const computeLanding = useCallback(
    (active: Active, block: ScheduleBlock, fromHolding: boolean): DropPlan | null => {
      const translated = active.rect.current.translated;
      const svg = chartSvgRef.current;
      if (!translated || !svg) return null;
      return planDrop({
        translated,
        svgRect: svg.getBoundingClientRect(),
        viewStart,
        viewEnd: Math.min(viewEnd, horizon),
        hourWidth,
        rowNames,
        durationH: block.run_hours,
        // Holding cards have no source row: the ghost's own position decides.
        sourceLineName: fromHolding ? undefined : block.line_name,
        requireInsideRows: fromHolding,
      });
    },
    [viewStart, viewEnd, horizon, hourWidth, rowNames],
  );

  // ── Auto-scroll the PARENT page while dragging near the viewport edge ──
  // The holding area lives below the chart; dragging a card up to a line
  // needs the page to scroll mid-drag (user request 2026-08-18). The iframe
  // never scrolls itself — the parent document does. Same-origin, so we can
  // read our frame position and drive whichever parent element scrolls.
  const autoScrollRaf = useRef(0);
  const dragPointerY = useRef<number | null>(null);
  const dragPointerTracker = useRef((e: PointerEvent) => {
    dragPointerY.current = e.clientY;
  });
  // Any in-iframe scroll during a drag (wheel on .gantt-scroll, programmatic)
  // slides the chart under the pointer-frozen ghost WITHOUT any dnd-kit
  // event: recompute the landing preview from the live rects. Capture phase,
  // because scroll events do not bubble.
  const dragScrollRefresh = useRef(() => {
    refreshPreviewRef.current();
  });
  const startAutoScroll = useCallback(() => {
    window.addEventListener("pointermove", dragPointerTracker.current);
    window.addEventListener("scroll", dragScrollRefresh.current, { capture: true, passive: true });
    const step = () => {
      try {
        const fe = window.frameElement as HTMLElement | null;
        const pw = window.parent && window.parent !== window ? window.parent : null;
        if (fe && pw && dragPointerY.current !== null) {
          const py = fe.getBoundingClientRect().top + dragPointerY.current;
          const vh = pw.innerHeight;
          const topZone = 140;   // below Streamlit's fixed toolbar
          const bottomZone = 90;
          let dy = 0;
          if (py < topZone) dy = -Math.min(28, Math.max(6, (topZone - py) / 3));
          else if (py > vh - bottomZone) dy = Math.min(28, Math.max(6, (py - (vh - bottomZone)) / 3));
          if (dy !== 0) {
            const cands: (Element | null)[] = [
              pw.document.scrollingElement,
              pw.document.querySelector('[data-testid="stAppViewContainer"]'),
              pw.document.querySelector('[data-testid="stMain"]'),
              pw.document.querySelector("section.main"),
            ];
            for (const c of cands) {
              const el = c as HTMLElement | null;
              if (el && el.scrollHeight > el.clientHeight + 4) {
                el.scrollTop += dy;
                break;
              }
            }
          }
        }
      } catch {
        /* cross-origin embed: no page auto-scroll */
      }
      // Track the content under the ghost even when no pointer event fires:
      // both this loop's parent-page scroll and a wheel/programmatic
      // .gantt-scroll scroll move the chart without telling dnd-kit.
      refreshPreviewRef.current();
      autoScrollRaf.current = requestAnimationFrame(step);
    };
    cancelAnimationFrame(autoScrollRaf.current);
    autoScrollRaf.current = requestAnimationFrame(step);
  }, []);
  const stopAutoScroll = useCallback(() => {
    cancelAnimationFrame(autoScrollRaf.current);
    autoScrollRaf.current = 0;
    dragPointerY.current = null;
    window.removeEventListener("pointermove", dragPointerTracker.current);
    window.removeEventListener("scroll", dragScrollRefresh.current, { capture: true });
  }, []);

  // Latest drag state, so the auto-scroll rAF loop can recompute the preview
  // between pointer events (a pure scroll fires no dnd-kit events at all -
  // without this the ghost would freeze while the content slides under it).
  const dragCtxRef = useRef<{ active: Active; overId?: string } | null>(null);

  // Live, rate-aware preview of the placement the drop would produce.
  // Never mutates committed state - purely visual until onDragEnd.
  const refreshDragPreview = useCallback(() => {
    const ctx = dragCtxRef.current;
    if (!ctx) return;
    const { active, overId } = ctx;
    const activeId = String(active.id);
    const fromHolding = activeId.startsWith("holding_");
    const block =
      (active.data.current?.block as ScheduleBlock | undefined) ??
      schedule.find((b) => b.id === activeId) ??
      cipWindows.find((b) => b.id === activeId);
    if (!block) return;
    const next = computeDragPreview({
      block,
      activeId,
      overId,
      plan: computeLanding(active, block, fromHolding),
      lines,
      caps,
      allBlocks: [...schedule, ...cipWindows],
      anchor,
      downtime,
      insertCtx,
    });
    // Recomputed every frame while auto-scroll runs: only re-render on change.
    setDragPreview((prev) => (samePreview(prev, next) ? prev : next));
  }, [schedule, cipWindows, lines, caps, anchor, downtime, insertCtx, computeLanding]);
  const refreshPreviewRef = useRef(refreshDragPreview);
  useEffect(() => { refreshPreviewRef.current = refreshDragPreview; });

  const onDragStart = useCallback(
    (event: DragStartEvent) => {
      setErrorMsg(null);
      setDragPreview(null);
      dragCtxRef.current = { active: event.active };
      const blockData = event.active.data.current?.block as ScheduleBlock | undefined;
      if (blockData) {
        setActiveDragBlock(blockData);
        if (!isWindowBlock(blockData.block_type)) {
          setActiveDragSku(blockData.sku);
        }
      }
      startAutoScroll();
    },
    [startAutoScroll],
  );

  const onDragMove = useCallback(
    (event: DragMoveEvent) => {
      const { active, over } = event;
      if (!active) return;
      dragCtxRef.current = { active, overId: over?.id as string | undefined };
      refreshDragPreview();
    },
    [refreshDragPreview],
  );

  const onDragCancel = useCallback(() => {
    stopAutoScroll();
    dragCtxRef.current = null;
    setActiveDragSku(null);
    setActiveDragBlock(null);
    setDragPreview(null);
  }, [stopAutoScroll]);

  const onDragEnd = useCallback(
    (event: DragEndEvent) => {
      stopAutoScroll();
      setActiveDragSku(null);
      setActiveDragBlock(null);
      setDragPreview(null);
      dragCtxRef.current = null;
      const { active, over } = event;
      if (!active) return;

      const activeId = active.id as string;
      const overId = over?.id as string | undefined;

      // ── Drag FROM holding TO a line row ──
      if (activeId.startsWith("holding_")) {
        const blockId = activeId.replace("holding_", "");
        const block = holdingArea.find((b) => b.id === blockId);
        if (!block) return;
        const plan = computeLanding(active, block, true);
        if (!plan || !plan.valid) {
          reject(plan?.reason ?? "Drop onto a production line to restore from holding");
          return;
        }
        const targetLine = lines[plan.rowIdx];
        if (!targetLine) return;
        const targetLineName = targetLine.line_name;
        if (block.locked) {
          reject("Block is locked");
          return;
        }
        if (block.block_type !== "cip" && !isCapable(targetLineName, block.sku, caps)) {
          reject(`Line ${targetLineName} cannot run ${block.sku}`);
          return;
        }
        const startHour = plan.snappedStartHour;
        let dur = block.run_hours;
        if (block.block_type !== "cip") {
          const newDur = recalcDuration(block, targetLineName, caps, downtime, startHour);
          if (newDur !== null) dur = newDur;
        }
        if (intoLockedZone(startHour)) {
          reject(`Cannot drop into the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`);
          return;
        }
        const allBlocks = [...schedule, ...cipWindows];
        // Resize-to-fit (user rule 2026-09-01): an oversized card no longer
        // bounces off the next block/CIP. Room = from the drop point to the
        // next obstruction on the line minus the changeover into it; the
        // card fills what fits and the remainder stays in holding. A drop
        // whose START sits inside an existing block is still refused.
        const onLine = allBlocks
          .filter((b) => b.id !== block.id && b.line_name === targetLineName)
          .sort((a, b2) => a.start_hour - b2.start_hour);
        if (onLine.some((b) => b.start_hour <= startHour + 1e-6
                               && b.end_hour > startHour + 1e-6)) {
          reject(`Overlap on ${targetLineName} at ${hourToStamp(startHour, anchor)}`);
          return;
        }
        const nextBlk = onLine.find((b) => b.start_hour > startHour + 1e-6);
        const roomEnd = nextBlk
          ? nextBlk.start_hour - setupBetween(block, nextBlk)
          : Number.POSITIVE_INFINITY;
        const room = Math.floor((roomEnd - startHour) * 10) / 10;
        const minRunH = Number(args.config.min_run_hours || 0);
        if (room < Math.max(minRunH, 0.1)) {
          reject(`Gap on ${targetLineName} is only ${Math.max(room, 0).toFixed(1)}h — `
                 + `smaller than the ${Math.max(minRunH, 0.1)}h minimum run`);
          return;
        }
        setErrorMsg(null);
        if (dur <= room + 1e-6) {
          actions.restoreFromHolding(blockId, targetLine.line_name, targetLine.line_id, startHour, dur);
        } else {
          actions.restorePartialFromHolding(
            blockId, targetLine.line_name, targetLine.line_id, startHour, room, dur);
        }
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

      // Same landing function as the ghost preview: the block lands exactly
      // where the ghost showed, whatever scrolled during the drag. No
      // geometry (ghost never measured) -> leave the block where it is.
      const plan = computeLanding(active, block, false);
      if (!plan) return;
      const targetLine = lines[plan.rowIdx];
      if (!targetLine) return;

      const sameLine = plan.lineName === block.line_name;

      if (sameLine) {
        const newStart = plan.snappedStartHour;
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
          const plan = computeInsertPlan(block, block.line_name, newStart, dur, allBlocks, insertCtx);
          const nextBlk = plan ? allBlocks.find((n) => n.id === plan.nextId) : undefined;
          if (plan && nextBlk && !plan.blockedReason) {
            const shifted = allBlocks
              .filter((b) => b.id !== block.id && b.line_name === block.line_name)
              .filter((b) => b.start_hour >= nextBlk.start_hour - 1e-9);
            const lockedHit = shifted.find(isBlockLocked);
            if (lockedHit) {
              reject(`Cannot insert: would slide ${lockedHit.sku || lockedHit.label} — ${lockReason(lockedHit)}`);
              return;
            }
            setErrorMsg(null);
            actions.insertShift(block.id, block.line_name, block.line_id, plan.insStart, dur, shifted.map((b) => b.id), plan.deltaH);
            return;
          }
          reject(plan && plan.blockedReason
            ? `Cannot insert: ${plan.blockedReason}`
            : `Overlap on ${block.line_name} at ${hourToStamp(newStart, anchor)}`);
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
        const newStart = plan.snappedStartHour;
        if (block.block_type !== "cip") {
          const newDur = recalcDuration(block, targetLine.line_name, caps, downtime, newStart);
          if (newDur === null) {
            reject(`No rate for ${block.sku} on ${targetLine.line_name}`);
            return;
          }
          dur = newDur;
        }
        if (intoLockedZone(newStart)) {
          reject(`Cannot move into the locked window (committed through ${hourToStamp(lockedThroughH ?? 0, anchor)})`);
          return;
        }
        const newEnd = newStart + dur;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, targetLine.line_name, block.id, newStart, newEnd)) {
          const plan = computeInsertPlan(block, targetLine.line_name, newStart, dur, allBlocks, insertCtx);
          const nextBlk = plan ? allBlocks.find((n) => n.id === plan.nextId) : undefined;
          if (plan && nextBlk && !plan.blockedReason) {
            const shifted = allBlocks
              .filter((b) => b.id !== block.id && b.line_name === targetLine.line_name)
              .filter((b) => b.start_hour >= nextBlk.start_hour - 1e-9);
            const lockedHit = shifted.find(isBlockLocked);
            if (lockedHit) {
              reject(`Cannot insert: would slide ${lockedHit.sku || lockedHit.label} — ${lockReason(lockedHit)}`);
              return;
            }
            setErrorMsg(null);
            actions.insertShift(block.id, targetLine.line_name, targetLine.line_id, plan.insStart, dur, shifted.map((b) => b.id), plan.deltaH);
            return;
          }
          reject(plan && plan.blockedReason
            ? `Cannot insert: ${plan.blockedReason}`
            : `Overlap on ${targetLine.line_name} at ${hourToStamp(newStart, anchor)}`);
          return;
        }
        setErrorMsg(null);
        actions.moveBlock(block.id, targetLine.line_name, targetLine.line_id, newStart, dur);
        setWarnMsg(setupWarning(block.id, targetLine.line_name, newStart, newEnd));
      }
    },
    [schedule, cipWindows, holdingArea, actions, caps, lines, computeLanding, reject, anchor, downtime, isBlockLocked, lockReason, intoLockedZone, lockedThroughH, insertCtx, stopAutoScroll],
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
      // Plain right-click on a SKU/trial block toggles the SKU highlight —
      // same effect as clicking its row in the adherence list; right-click
      // again (any block of that SKU) clears it. Works on locked/committed
      // blocks too: highlighting is read-only. Shift+right-click keeps the
      // Split/Remove menu; windows (CIP/downtime) keep the plain menu.
      const sku = (block as ScheduleBlock).sku;
      if (!e.shiftKey && (block.block_type === "sku" || block.block_type === "trial") && sku) {
        setHighlightSku((prev) => (prev === sku ? null : sku));
        return;
      }
      if (isBlockLocked(block)) {
        reject(lockReason(block));
        return;
      }
      openMenu(e.clientX, e.clientY, blockId, block.block_type, block.start_hour, block.end_hour);
    },
    [schedule, cipWindows, openMenu, reject, isBlockLocked, lockReason, setHighlightSku],
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

  // ── Blank-space SKU picker (user request 2026-08-19) ──
  // RIGHT-click on an empty gap lists demand-plan SKUs the line can run,
  // with the changeover types the placement would create and a snap-left
  // plan. Left-click keeps its existing deselect behavior.
  const [picker, setPicker] = useState<{
    lineName: string; lineId: number; hour: number; x: number; y: number;
  } | null>(null);
  // Read-only mounts (compare / generate previews) send none of the picker
  // payloads: without lineCapableSkus the picker would offer placements with
  // zero setup and all-clean chips, and place unlocked blocks on a board
  // whose blocks are locked. Only the calendar page sends the payload.
  const pickerEnabled = Boolean(
    args.lineCapableSkus && Object.keys(args.lineCapableSkus).length > 0,
  );
  const handleEmptyContextMenu = useCallback(
    (e: React.MouseEvent, lineName: string, lineId: number, hour: number) => {
      closeMenu();
      setPopover(null);
      setPicker({ lineName, lineId, hour, x: e.clientX, y: e.clientY });
    },
    [closeMenu],
  );

  // ── Planner CIPs (user request 2026-09-01) ──
  // A clean can be inserted flush before/after a block (popup), at a gap
  // (SKU picker "+ CIP here"), or from a week card's "CIP req" chip. The
  // line's LATER projected cleans re-forecast from it (addCipReforecast).
  const cipIntervalFor = useCallback((lineName: string): number => {
    const cfgI = args.config.cip_interval_h?.[lineName];
    if (cfgI && cfgI > 0) return cfgI;
    // No cip_info for this line: infer from the spacing of its projected
    // cleans, else the configured default.
    const proj = cipWindows
      .filter((b) => b.block_type === "cip" && b.line_name === lineName &&
                     (b.attrs ?? "").includes("cip_projected"))
      .map((b) => b.start_hour).sort((a, b) => a - b);
    let best = 0;
    for (let i = 1; i < proj.length; i++) {
      const dlt = proj[i] - proj[i - 1];
      if (dlt > 1 && (best === 0 || dlt < best)) best = dlt;
    }
    return best > 0 ? best : (args.config.cip_interval_default_h || 120);
  }, [args.config, cipWindows]);

  const placeCip = useCallback((lineName: string, lineId: number, startHour: number): string | null => {
    const d = args.config.cip_duration_h || 6;
    if (lockedThroughH != null && startHour < lockedThroughH - 1e-9) {
      return `Cannot place a CIP inside the locked window (committed through ${hourToStamp(lockedThroughH, anchor)})`;
    }
    const onLine = [...schedule, ...cipWindows]
      .filter((b) => b.line_name === lineName &&
                     !String(b.id).startsWith("cipinfo_") && !String(b.id).startsWith("dt_"))
      .sort((a, b) => a.start_hour - b.start_hour);
    const inside = onLine.find((b) => b.start_hour < startHour - 1e-6 && b.end_hour > startHour + 1e-6);
    if (inside) return `${hourToStamp(startHour, anchor)} is inside ${inside.sku || inside.label} — use the block's edge`;
    const next = onLine.find((b) => b.start_hour >= startHour - 1e-6);
    const shortfall = next ? Math.max(0, startHour + d - next.start_hour) : 0;
    const shifted = shortfall > 0 && next
      ? onLine.filter((b) => b.start_hour >= next.start_hour - 1e-6)
      : [];
    const fixed = shifted.find((b) => isBlockLocked(b));
    if (fixed) return `Would move committed block ${fixed.sku || fixed.label} — place the CIP after it instead`;
    setErrorMsg(null);
    actions.addCipReforecast({
      lineName, lineId, startHour, duration: d, intervalH: cipIntervalFor(lineName),
      horizonH: horizon, shiftIds: shifted.map((b) => b.id), shiftH: shortfall,
    });
    return null;
  }, [args.config, lockedThroughH, schedule, cipWindows, isBlockLocked, actions,
      cipIntervalFor, horizon, anchor]);

  const prevEndOnLine = useCallback((lineName: string, beforeHour: number, excludeId?: string): number =>
    Math.max(0, ...[...schedule, ...cipWindows]
      .filter((b) => b.id !== excludeId && b.line_name === lineName &&
                     b.end_hour <= beforeHour + 1e-6 &&
                     !String(b.id).startsWith("cipinfo_") && !String(b.id).startsWith("dt_"))
      .map((b) => b.end_hour)),
  [schedule, cipWindows]);

  const handlePopoverAddCip = useCallback((blockId: string, dir: "before" | "after"): string | null => {
    const block = [...schedule, ...cipWindows].find((b) => b.id === blockId);
    if (!block) return "block not found";
    if (dir === "after") return placeCip(block.line_name, block.line_id, block.end_hour);
    const d = args.config.cip_duration_h || 6;
    const start = Math.max(prevEndOnLine(block.line_name, block.start_hour, blockId),
                           block.start_hour - d, lockedThroughH ?? 0);
    return placeCip(block.line_name, block.line_id, start);
  }, [schedule, cipWindows, placeCip, prevEndOnLine, args.config, lockedThroughH]);

  const handlePickerAddCip = useCallback((): string | null => {
    if (!picker) return "no gap selected";
    const start = Math.max(prevEndOnLine(picker.lineName, picker.hour), lockedThroughH ?? 0);
    return placeCip(picker.lineName, picker.lineId, start);
  }, [picker, prevEndOnLine, lockedThroughH, placeCip]);

  const handlePickerAddTrial = useCallback((sku: string, hours: number): string | null => {
    if (!picker) return "no gap selected";
    const start = Math.max(prevEndOnLine(picker.lineName, picker.hour), lockedThroughH ?? 0);
    if (findOverlapsOnLine([...schedule, ...cipWindows], picker.lineName, "", start, start + hours)) {
      return `Not enough room on ${picker.lineName} for a ${hours}h trial at ${hourToStamp(start, anchor)}`;
    }
    setErrorMsg(null);
    actions.addTrial(picker.lineName, picker.lineId, sku, start, hours);
    return null;
  }, [picker, prevEndOnLine, lockedThroughH, schedule, cipWindows, actions, anchor]);

  const handleWeekCipFix = useCallback((gap: { lineName: string; lineId: number; start: number }) => {
    const err = placeCip(gap.lineName, gap.lineId, gap.start);
    if (err) reject(err);
  }, [placeCip, reject]);

  // ── Holding-card placement menu (user request 2026-09-01) ──
  // RIGHT-click a holding card: one row per line that can run its SKU, with
  // the earliest snap-left gap, changeover chips, and a Place button. An
  // oversized card places what fits; the remainder stays in holding.
  const [holdMenu, setHoldMenu] = useState<{ block: ScheduleBlock; x: number; y: number } | null>(null);

  const holdMenuRows = useMemo<HoldingPlaceRow[]>(() => {
    if (!holdMenu) return [];
    const block = holdMenu.block;
    const cardKg = block.qty_kg && block.qty_kg > 0 ? block.qty_kg : 0;
    const nowH = (Date.now() - anchor.getTime()) / 3600_000;
    const base = Math.max(0, nowH, lockedThroughH ?? 0);
    const rows: HoldingPlaceRow[] = [];
    for (const ln of lines) {
      const rate = getRate(ln.line_name, block.sku, caps);
      if (!(rate > 0)) continue;
      const group = groupOf(ln.line_name);
      const blocksOnLine = [...schedule, ...cipWindows].filter(
        (b) => groupOf(b.line_name) === group &&
          !(isWindowBlock(b.block_type) && sideOf(b.line_name)),
      );
      // No gap enumerator exists — probe snap-left at the horizon start and
      // just after each block's end; the first plan without a reason wins.
      const probes = [base, ...blocksOnLine.map((b) => b.end_hour + 0.05)]
        .filter((h) => h >= base - 1e-6)
        .sort((a, b) => a - b);
      let plan: PlacementPlan | null = null;
      for (const h of probes) {
        const p = planPlacement({
          clickHour: h,
          sku: block.sku,
          rate,
          remainingKg: cardKg > 0 ? cardKg : rate * block.run_hours,
          blocksOnLine,
          changeovers: args.changeovers ?? {},
          lockedThroughH,
          nowH,
          horizonH: horizon,
          minRunH: args.config.min_run_hours,
          lineGroup: group,
          downtime,
        });
        if (p.reason === null) { plan = p; break; }
      }
      if (!plan) continue;
      rows.push({
        lineName: ln.line_name,
        lineId: ln.line_id,
        plan,
        inFlags: plan.prevSku ? coFlagsForPair(args.coFlags, plan.prevSku, block.sku) : [],
        outFlags: plan.nextSku ? coFlagsForPair(args.coFlags, block.sku, plan.nextSku) : [],
      });
    }
    rows.sort((a, b) => a.plan.startHour - b.plan.startHour);
    return rows;
  }, [holdMenu, schedule, cipWindows, lines, args.changeovers, args.coFlags,
      anchor, lockedThroughH, horizon, caps, downtime, args.config.min_run_hours]);

  const handleHoldingPlace = useCallback(
    (row: HoldingPlaceRow) => {
      if (!holdMenu || row.plan.reason) return;
      const block = holdMenu.block;
      const { startHour, durationH } = row.plan;
      const allBlocks = [...schedule, ...cipWindows];
      if (findOverlapsOnLine(allBlocks, row.lineName, block.id, startHour, startHour + durationH)) {
        reject(`Overlap on ${row.lineName} at ${hourToStamp(startHour, anchor)}`);
        setHoldMenu(null);
        return;
      }
      const rate = getRate(row.lineName, block.sku, caps);
      const cardKg = block.qty_kg && block.qty_kg > 0 ? block.qty_kg : 0;
      const fullDur = rate > 0 && cardKg > 0
        ? Math.ceil((cardKg / rate) * 10) / 10
        : block.run_hours;
      setErrorMsg(null);
      if (durationH >= fullDur - 1e-6) {
        actions.restoreFromHolding(block.id, row.lineName, row.lineId, startHour, fullDur);
      } else {
        actions.restorePartialFromHolding(
          block.id, row.lineName, row.lineId, startHour, durationH, fullDur);
      }
      setHoldMenu(null);
    },
    [holdMenu, schedule, cipWindows, caps, anchor, actions, reject],
  );

  const pickerRows = useMemo<PickerRowData[]>(() => {
    if (!picker) return [];
    const adh = computeAdherence(schedule, args.demandTargets, args.capabilities, coveredByOrder);
    const remaining = remainingDemandBySku(adh, anchor);
    // Candidates: server list (capable==1 ∩ demand plan); derived from the
    // caps map for a line the server list happens to miss (the picker only
    // opens at all when the payload is present — see pickerEnabled).
    let cands = args.lineCapableSkus?.[picker.lineName];
    if (!cands || cands.length === 0) {
      const demSkus = [...new Set(args.demandTargets.map((d) => d.sku))];
      cands = demSkus
        .map((sku) => ({ sku, rate: getRate(picker.lineName, sku, caps) }))
        .filter((c) => c.rate > 0);
    }
    const group = groupOf(picker.lineName);
    // Blocks sharing the clicked ROW. Side-named WINDOWS (P17A down) are
    // excluded: they only halve the rate, which qtyOverWindow already
    // integrates via sideDowntime — they do not forbid placement.
    const blocksOnLine = [...schedule, ...cipWindows].filter(
      (b) => groupOf(b.line_name) === group &&
        !(isWindowBlock(b.block_type) && sideOf(b.line_name)),
    );
    const nowH = (Date.now() - anchor.getTime()) / 3600_000;
    const rows: PickerRowData[] = [];
    for (const c of cands) {
      const rem = remaining[c.sku] ?? 0;
      if (rem <= 0) continue; // fully covered: not offered
      const rate = c.rate > 0 ? c.rate : getRate(picker.lineName, c.sku, caps);
      const plan = planPlacement({
        clickHour: picker.hour,
        sku: c.sku,
        rate,
        remainingKg: rem,
        blocksOnLine,
        changeovers: args.changeovers ?? {},
        lockedThroughH,
        nowH,
        horizonH: horizon,
        minRunH: args.config.min_run_hours,
        lineGroup: group,
        downtime,
      });
      rows.push({
        sku: c.sku,
        desc: args.skuDescriptions?.[c.sku] ?? "",
        remainingKg: rem,
        plan,
        inFlags: plan.prevSku ? coFlagsForPair(args.coFlags, plan.prevSku, c.sku) : [],
        outFlags: plan.nextSku ? coFlagsForPair(args.coFlags, c.sku, plan.nextSku) : [],
      });
    }
    rows.sort((a, b) => b.remainingKg - a.remainingKg);
    return rows;
  }, [picker, schedule, cipWindows, args.demandTargets, args.capabilities,
      args.changeovers, args.lineCapableSkus, args.skuDescriptions, args.coFlags,
      coveredByOrder, anchor, lockedThroughH, horizon, caps, downtime,
      args.config.min_run_hours]);

  const handlePickerPlace = useCallback(
    (row: PickerRowData) => {
      if (!picker || row.plan.reason) return;
      const { startHour, durationH, qtyKg } = row.plan;
      const allBlocks = [...schedule, ...cipWindows];
      // Belt and braces: the plan is gap-derived, but the board may have
      // changed under the open popup.
      if (findOverlapsOnLine(allBlocks, picker.lineName, "", startHour, startHour + durationH)) {
        reject(`Overlap on ${picker.lineName} at ${hourToStamp(startHour, anchor)}`);
        setPicker(null);
        return;
      }
      // One placement, one undo step — but split into per-order segments so
      // every kg credits the demand order it actually fills (adherence and
      // the holding rebuild are per order; one big block on one order would
      // leave the other weeks' cards standing).
      const adh = computeAdherence(schedule, args.demandTargets, args.capabilities, coveredByOrder);
      const segments = splitPlacementByOrders(
        row.sku, { startHour, durationH, qtyKg }, adh, args.demandTargets, anchor,
      );
      if (!segments.length) {
        reject(`No open demand order for ${row.sku}`);
        setPicker(null);
        return;
      }
      setErrorMsg(null);
      actions.addProduction(picker.lineName, picker.lineId, row.sku, row.desc, segments);
      setPicker(null);
    },
    [picker, schedule, cipWindows, args.demandTargets, args.capabilities,
     coveredByOrder, anchor, actions, reject],
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

  // Pin toggle (popup): a pinned demand block becomes committed line-time —
  // the solver must plan around it (Scenario F stages it as a blocked window
  // and credits its kg against demand). The block also refuses drag/resize/
  // edits until unpinned, so the board can't silently disagree with what the
  // solve was told.
  const handleTogglePin = useCallback(
    (blockId: string, pinned: boolean) => {
      const block = schedule.find((b) => b.id === blockId);
      if (!block) return;
      actions.updateBlock(blockId, { pinned });
      actions.reportAction(
        pinned
          ? `📌 Pinned ${block.order_id || block.sku} — the solver will plan around it`
          : `Unpinned ${block.order_id || block.sku} — it can move again`,
      );
      setPopover((p) =>
        p && p.block.id === blockId ? { ...p, block: { ...p.block, pinned } } : p,
      );
    },
    [schedule, actions],
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

  // Fill the empty space next to a block (user request 2026-08-18): extend
  // the block's edge to the neighbouring block minus the required setup
  // time (from_sku -> to_sku), or to the horizon/lock boundary when the
  // line is open. Resize semantics — qty scales with duration.
  const handleFill = useCallback(
    (blockId: string, dir: "left" | "right" | "both"): string | null => {
      const block =
        schedule.find((b) => b.id === blockId) ?? cipWindows.find((b) => b.id === blockId);
      if (!block) return "Block no longer exists";
      if (isBlockLocked(block)) return lockReason(block);
      const others = [...schedule, ...cipWindows].filter(
        (b) => b.id !== blockId && b.line_name === block.line_name,
      );
      let newStart = block.start_hour;
      let newEnd = block.end_hour;
      const notes: string[] = [];
      if (dir === "left" || dir === "both") {
        const left = others
          .filter((b) => b.end_hour <= block.start_hour + 1e-9)
          .sort((a, b) => b.end_hour - a.end_hour)[0];
        const setup = left ? setupBetween(left, block) : 0;
        const floor = Math.max(
          0,
          lockedThroughH ?? 0,
          left ? left.end_hour + setup : 0,
        );
        if (floor < block.start_hour - 1e-9) {
          newStart = floor;
          notes.push(
            left
              ? `left to ${left.sku || left.label}${setup > 0 ? ` +${setup}h setup` : ""}`
              : "left to the line start",
          );
        }
      }
      if (dir === "right" || dir === "both") {
        const right = others
          .filter((b) => b.start_hour >= block.end_hour - 1e-9)
          .sort((a, b) => a.start_hour - b.start_hour)[0];
        const setup = right ? setupBetween(block, right) : 0;
        const ceil = right ? right.start_hour - setup : horizon;
        if (ceil > block.end_hour + 1e-9) {
          newEnd = ceil;
          notes.push(
            right
              ? `right to ${right.sku || right.label}${setup > 0 ? ` -${setup}h setup` : ""}`
              : "right to the horizon",
          );
        }
      }
      if (newStart === block.start_hour && newEnd === block.end_hour) {
        return "Nothing to fill — the block already touches its neighbours";
      }
      const clash = others.find(
        (b) => b.start_hour < newEnd - 1e-9 && b.end_hour > newStart + 1e-9,
      );
      if (clash) {
        return `No room: would overlap ${clash.sku || clash.label}`;
      }
      setErrorMsg(null);
      actions.resizeBlock(blockId, newStart, newEnd);
      actions.reportAction(
        `Filled ${block.order_id || block.sku} ${notes.join(" and ")}`,
      );
      return null;
    },
    [schedule, cipWindows, actions, isBlockLocked, lockReason, lockedThroughH, horizon, setupBetween],
  );

  // Remaining demand for a SKU by week (popup helper): target minus what
  // the CURRENT board schedules per order, from the same adherence rules.
  const demandLeftForSku = useCallback(
    (sku: string): { week: string; left_kg: number; total_kg: number; scheduled_kg: number }[] => {
      const rows = computeAdherence(schedule, args.demandTargets, args.capabilities, coveredByOrder);
      const bySku = rows.filter((r) => r.sku === sku);
      const out: { week: string; left_kg: number; total_kg: number; scheduled_kg: number }[] = [];
      const nowIso = isoWeekAtHour(new Date(), 0);
      for (const r of bySku) {
        const m = /-W(\d+)$/.exec(r.order_id);
        const wkNum = m ? isoWeekLabel(parseInt(m[1], 10), anchor) : null;
        if (wkNum !== null && wkNum < nowIso) continue; // past weeks are misses, not plan items
        const wk = wkNum !== null ? `W${wkNum}` : "—";
        const target = r.qty_min > 0 && r.qty_max >= r.qty_min
          ? (r.qty_min + r.qty_max) / 2
          : Math.max(r.qty_min, r.qty_max);
        out.push({
          week: wk,
          left_kg: Math.max(0, Math.round(target - r.scheduled_qty)),
          total_kg: Math.round(target),
          // Unclamped board credit: the popup shows % covered and the kg
          // OVER target — 100 kg over reads very differently from 10,000
          // (user request 2026-09-01).
          scheduled_kg: Math.round(r.scheduled_qty),
        });
      }
      return out;
    },
    [schedule, args.demandTargets, args.capabilities, anchor, coveredByOrder],
  );

  // Python (helpers/scorecard_engine.gantt_kpis) is the source of truth for
  // KPI numbers: render the server payload verbatim until the user edits,
  // then recompute live with the SAME rules (kpi.ts is an exact port driven
  // by the server's co_pairs classification map). Overlaps are a client-side
  // diagnostic in both paths.
  const initialState = useRef({ schedule, cipWindows });
  const edited =
    schedule !== initialState.current.schedule ||
    cipWindows !== initialState.current.cipWindows;

  // Live holding (user rule 2026-09-01): once the board has been edited,
  // re-derive the auto cards from the current schedule on EVERY change —
  // SKU-picker adds, resizes, splits, trash — not just drags out of
  // holding. Same math as the server rebuild, so "Refresh checks" then
  // agrees instead of correcting. Until the first edit the server's cards
  // stand (they already reflect the saved board plus made-kg credit).
  const replaceAutoHolding = actions.replaceAutoHolding;
  useEffect(() => {
    if (!edited) return;
    replaceAutoHolding(deriveAutoHolding(
      schedule, args.demandTargets, args.capabilities, coveredByOrder,
      anchor, args.skuDescriptions ?? {}));
  }, [edited, schedule, args.demandTargets, args.capabilities, coveredByOrder,
      anchor, args.skuDescriptions, replaceAutoHolding]);

  const kpis = useMemo(() => {
    if (!edited && args.kpis) {
      return serverKpisToKpiData(args.kpis, checkOverlapsSimple([...schedule, ...cipWindows]));
    }
    return computeKpis(
      schedule, cipWindows, args.demandTargets, args.capabilities,
      args.kpis?.co_pairs, args.kpis?.co_default, coveredByOrder,
    );
  }, [edited, args.kpis, schedule, cipWindows, args.demandTargets, args.capabilities, coveredByOrder]);

  const adherenceRows = useMemo(() => {
    if (!edited && args.kpis) return args.kpis.adherence;
    return computeAdherence(schedule, args.demandTargets, args.capabilities, coveredByOrder);
  }, [edited, args.kpis, schedule, args.demandTargets, args.capabilities, coveredByOrder]);

  // The adherence table below the chart plans CURRENT/FUTURE weeks only —
  // past-week misses live on the Reconcile page, same rule as the holding
  // area and the popup demand list (finding 11, 2026-08-18).
  const currentAdherenceRows = useMemo(
    () => adherenceRows.filter((r) => !isPastDemandWeek(r.order_id, anchor)),
    [adherenceRows, anchor],
  );

  // Per-week chips (user request 2026-08-18): fulfillment vs the demand due
  // that ISO week + changeovers by machine, recomputed live client-side.
  const weekStats = useMemo(() => {
    const stats: Record<string, { boardPct: number | null; madePct: number | null; tl: number; ffs: number; cp: number; ttp: number; cipReq: number;
  cipGap: { lineName: string; lineId: number; start: number } | null; board: number; credit: number; target: number }> = {};
    // MASTER-FILE RULE (user mandate 2026-08-21): the headline number is
    // BOARD fill — kg of calendar_blocks.csv blocks in the week (per-order
    // credit capped at target) vs the week's demand target. The board pass
    // runs with NO credit map (the waterfall still attributes committed-MO
    // blocks: they ARE board blocks); the credited pass adds ONLY made kg
    // from completed MOs the board hides, and that addend renders as a
    // visibly separate "+ made" element — never silently summed into the
    // headline. A nearly empty week must read as a small board %.
    const rows = computeAdherence(schedule, args.demandTargets, args.capabilities, coveredByOrder);
    const boardByOrder: Record<string, number> = {};
    for (const r of computeAdherence(schedule, args.demandTargets, args.capabilities)) {
      boardByOrder[r.order_id] = r.scheduled_qty;
    }
    const dem: Record<string, { sched: number; board: number; target: number }> = {};
    const nowIsoWk = isoWeekAtHour(new Date(), 0);
    for (const r of rows) {
      const m = /-W(\d+)$/.exec(r.order_id);
      if (!m) continue;
      const wkNum = isoWeekLabel(parseInt(m[1], 10), anchor);
      if (wkNum < nowIsoWk) continue; // past weeks are misses, not plan
      const wk = `W${wkNum}`;
      const d = (dem[wk] ??= { sched: 0, board: 0, target: 0 });
      // Credit caps at the order's own target — overproduction on one order
      // cannot raise the week's fulfillment (same cap as weekly_breakdown).
      d.sched += weekFulfillmentCredit(r);
      const tgt = orderTarget(r.qty_min, r.qty_max);
      d.board += Math.min(boardByOrder[r.order_id] ?? 0, tgt);
      d.target += tgt;
    }
    const byLine: Record<string, ScheduleBlock[]> = {};
    for (const b of schedule) {
      if (b.block_type !== "sku") continue;
      (byLine[b.line_name] ??= []).push(b);
    }
    const pairs = args.kpis?.co_pairs ?? {};
    const dflt = args.kpis?.co_default ?? { recipe: 1, format: 0, hours: 1.5 };
    // Same CIP rules as countChangeovers (score_changeovers is the source of
    // truth): a CIP fully inside the gap waives the transition entirely; a
    // cip_req-flagged pair with no CIP in the gap is a hygiene violation.
    const cipsByLineId: Record<number, [number, number][]> = {};
    for (const c of cipWindows) {
      if (c.block_type !== "cip") continue;
      (cipsByLineId[c.line_id] ??= []).push([c.start_hour, c.end_hour]);
    }
    for (const blocks of Object.values(byLine)) {
      const sorted = [...blocks].sort((a, b) => a.start_hour - b.start_hour);
      for (let i = 1; i < sorted.length; i++) {
        const prev = sorted[i - 1];
        const from = prev.sku;
        const to = sorted[i].sku;
        if (from === to) continue;
        const waived = (cipsByLineId[prev.line_id] ?? []).some(
          ([cs, ce]) => cs >= prev.end_hour - 1e-6 && ce <= sorted[i].start_hour + 1e-6,
        );
        if (waived) continue;
        const wk = `W${isoWeekAtHour(anchor, sorted[i].start_hour)}`;
        const st = (stats[wk] ??= { boardPct: null, madePct: null, tl: 0, ffs: 0, cp: 0, ttp: 0, cipReq: 0, cipGap: null as { lineName: string; lineId: number; start: number } | null, board: 0, credit: 0, target: 0 });
        const pi = pairs[`${from}|${to}`] ?? dflt;
        st.tl += pi.tl ?? 0;
        st.ffs += pi.ffs ?? 0;
        st.cp += pi.cp ?? 0;
        st.ttp += pi.ttp ?? 0;
        if ((pi.cip_req ?? 0) === 1) {
          st.cipReq += 1;
          if (!st.cipGap) st.cipGap = { lineName: prev.line_name, lineId: prev.line_id, start: prev.end_hour };
        }
      }
    }
    for (const [wk, d] of Object.entries(dem)) {
      const st = (stats[wk] ??= { boardPct: null, madePct: null, tl: 0, ffs: 0, cp: 0, ttp: 0, cipReq: 0, cipGap: null as { lineName: string; lineId: number; start: number } | null, board: 0, credit: 0, target: 0 });
      st.board = d.board;
      st.credit = Math.max(0, d.sched - d.board);
      st.target = d.target;
      st.boardPct = d.target > 0 ? Math.round((d.board / d.target) * 1000) / 10 : null;
      st.madePct = d.target > 0 ? Math.round((st.credit / d.target) * 1000) / 10 : null;
    }
    // computedAt makes freshness visible on the card row: the cards derive
    // live from the current board + ledger on every edit/rerun — Save is
    // never required for them to be accurate.
    return { byWeek: stats, computedAt: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) };
  }, [schedule, cipWindows, args.demandTargets, args.capabilities, args.kpis, anchor, coveredByOrder]);

  const zoomIn = useCallback(() => {
    userZoomed.current = true;
    setHourWidth((w) => Math.min(w * 1.3, MAX_HOUR_WIDTH));
  }, []);
  const zoomOut = useCallback(() => {
    userZoomed.current = true;
    setHourWidth((w) => Math.max(w / 1.3, MIN_HOUR_WIDTH));
  }, []);
  const resetZoom = useCallback(() => {
    userZoomed.current = false;
    const w = containerRef.current?.offsetWidth ?? 1200;
    setHourWidth(fitToWidth(w, horizon));
    setViewStart(0);
  }, [horizon]);

  // Buffered sync (user request 2026-08-18): edits stay CLIENT-SIDE — the
  // KPI bar, overlap banner and per-week chips recompute live in kpi.ts —
  // and nothing reruns the Streamlit page until the planner clicks
  // "Refresh checks". That click pushes the state up, where Python
  // rescoring, holding rebuild and the full scorecard run once.
  const lastPushed = useRef("");
  const [dirty, setDirty] = useState(false);
  const adoptingHolding = useRef(false);
  useEffect(() => {
    const key = JSON.stringify({ schedule, cipWindows, holdingArea, lastAction });
    if (lastPushed.current === "") {
      lastPushed.current = key; // initial mount is not an edit
      return;
    }
    if (adoptingHolding.current) {
      // Server-derived holding adoption is NOT a user edit — fold it into
      // the pushed-state key so the dirty flag stays honest.
      adoptingHolding.current = false;
      lastPushed.current = key;
      return;
    }
    if (key !== lastPushed.current) setDirty(true);
  }, [schedule, cipWindows, holdingArea, lastAction]);

  // After a Refresh push, Python rebuilds the holding area (cards clear when
  // the placed kg covers an order) and re-renders the component with the new
  // holdingArea arg — WITHOUT remounting, so client state must adopt it or
  // the cards on screen stay stale. Never adopt over unpushed edits: the
  // planner's in-flight drags win and the next push re-derives anyway.
  //
  // lastSeenServerHolding records every server holding we SAW, adopted or
  // not — recording only on adoption replays stale args after a Refresh:
  //   1. edits pending (dirty), page reruns with holding H1: skip adopt,
  //      but remember H1 as seen;
  //   2. "⟳ Refresh checks" flips dirty false — this effect re-fires with
  //      args.holdingArea STILL H1 (pre-push); H1 == last seen, so the
  //      just-pushed holding is not clobbered by the old one;
  //   3. Python's rerun arrives with the rebuilt H2: differs from last
  //      seen and not dirty -> adopt.
  const lastSeenServerHolding = useRef(JSON.stringify(args.holdingArea ?? []));
  useEffect(() => {
    const incoming = JSON.stringify(args.holdingArea ?? []);
    const changed = incoming !== lastSeenServerHolding.current;
    lastSeenServerHolding.current = incoming;
    if (!changed) return;
    if (dirty) return; // retry once the edits are pushed
    adoptingHolding.current = true;
    actions.setHoldingFromServer(args.holdingArea ?? []);
  }, [args.holdingArea, dirty, actions]);
  const pushRefresh = useCallback(() => {
    lastPushed.current = JSON.stringify({ schedule, cipWindows, holdingArea, lastAction });
    setDirty(false);
    setComponentValue({ schedule, cipWindows, holdingArea, lastAction });
  }, [schedule, cipWindows, holdingArea, lastAction]);

  useEffect(() => {
    const h = containerRef.current?.scrollHeight ?? 800;
    setFrameHeight(h + 20);
  });

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === "z") { e.preventDefault(); actions.undo(); }
      if (e.ctrlKey && e.key === "y") { e.preventDefault(); actions.redo(); }
      if (e.key === "Escape") { closeMenu(); setPopover(null); setPicker(null); setHoldMenu(null); setHighlightSku(null); setErrorMsg(null); }
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

  // Snapped landing cell, drawn in the SVG under the floating drag overlay.
  // On a valid insert gesture the block itself lands at the insert point, so
  // the ghost moves there and the displaced-block preview shows the slide.
  const dropGhost = useMemo(() => {
    if (!dragPreview) return null;
    const ins = dragPreview.insert && !dragPreview.insert.blockedReason ? dragPreview.insert : null;
    return {
      lineName: dragPreview.targetLine,
      startHour: ins ? ins.insStart : dragPreview.startHour,
      endHour: ins ? ins.insEnd : dragPreview.endHour,
      valid: dragPreview.valid,
      fill: ghostBg,
    };
  }, [dragPreview, ghostBg]);

  return (
    <div
      ref={containerRef}
      style={{ fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" }}
      onClick={() => { closeMenu(); setPopover(null); setPicker(null); setHoldMenu(null); }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ flex: 1 }}>
          {kpis.overlaps.length > 0 && (
            <span style={{ color: "#b71c1c", fontWeight: 700, fontSize: 13 }}>
              ⚠ {kpis.overlaps.length} overlap(s): {kpis.overlaps[0]}
            </span>
          )}
        </div>
        <button
          onClick={pushRefresh}
          title="Push your edits up: rescore, rebuild holding, rerun all checks"
          style={{
            padding: "8px 16px", borderRadius: 8, fontWeight: 700,
            fontSize: 13, cursor: "pointer",
            border: dirty ? "2px solid #f57c00" : "1px solid #ccc",
            background: dirty ? "#fff3e0" : "#fff",
            color: dirty ? "#e65100" : "#666",
          }}
        >
          {dirty ? "⟳ Refresh checks • unsaved edits" : "⟳ Refresh checks"}
        </button>
      </div>

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
        {/* Per-week stats OUTSIDE the chart (user 2026-08-18): a simple
            row that roughly aligns with the 3-week window; live-updating. */}
        {Object.keys(weekStats.byWeek).length > 0 && (
          <>
            <div style={{ display: "flex", gap: 8, margin: "6px 0 4px 50px" }}>
              {Object.entries(weekStats.byWeek)
                .sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }))
                .map(([wk, ws]) => (
                  <div key={wk}
                       title={ws.target > 0
                         ? `${wk}: ${Math.round(ws.board).toLocaleString()} kg on the board + ${Math.round(ws.credit).toLocaleString()} kg covered thanks to production already made (hidden completed MOs), target ${Math.round(ws.target).toLocaleString()} kg`
                         : undefined}
                       style={{
                    flex: 1, padding: "6px 12px", borderRadius: 8,
                    background: "#f7f7fa", border: "1px solid #e0e0e5",
                    display: "flex", alignItems: "baseline", gap: 12,
                  }}>
                    <span style={{ fontWeight: 800, fontSize: 14, color: "#455a64" }}>{wk}</span>
                    {ws.boardPct !== null && (
                      <span style={{ fontWeight: 800, fontSize: 16,
                                     color: ws.boardPct < 90 ? "#c62828" : "#2e7d32" }}>
                        {ws.boardPct}%<span style={{ fontSize: 10, fontWeight: 600, color: "#888" }}> on board</span>
                      </span>
                    )}
                    {ws.madePct !== null && ws.madePct > 0 && (
                      <span style={{ fontWeight: 700, fontSize: 12, color: "#6a7b8c" }}>
                        + {ws.madePct}%<span style={{ fontSize: 10, fontWeight: 600, color: "#8a94a0" }}> already made</span>
                      </span>
                    )}
                    <span style={{ fontSize: 11.5, color: "#555", fontWeight: 600 }}>
                      TL {ws.tl} · FFS {ws.ffs} · CP {ws.cp} · TTP {ws.ttp}
                    </span>
                    {ws.cipReq > 0 && (
                      <span
                        title={`${ws.cipReq} transition${ws.cipReq === 1 ? "" : "s"} this week require${ws.cipReq === 1 ? "s" : ""} a CIP between the SKUs but no CIP block sits in the gap — schedule a clean or resequence`}
                        style={{
                          fontSize: 11, fontWeight: 800, color: "#fff",
                          background: "#c62828", borderRadius: 10, padding: "1px 8px",
                        }}>
                        CIP req {ws.cipReq}
                      </span>
                    )}
                    {ws.cipGap && (
                      <button
                        title={`Insert a clean at the first violating transition (${ws.cipGap.lineName} at ${hourToStamp(ws.cipGap.start, anchor)}); later projected CIPs re-forecast from it`}
                        style={{ fontSize: 11, fontWeight: 800, borderRadius: 10, padding: "1px 8px",
                                 border: "1px solid #1565c0", background: "#e3f2fd", color: "#0d47a1", cursor: "pointer" }}
                        onClick={() => handleWeekCipFix(ws.cipGap!)}
                      >
                        + CIP
                      </button>
                    )}
                  </div>
                ))}
            </div>
            <div style={{ fontSize: 11, color: "#8a94a0", margin: "0 0 4px 50px" }}>
              On board % = kg of calendar blocks vs the week&rsquo;s demand target
              (per-order credit capped at target); &ldquo;+ already made&rdquo; = extra coverage
              from completed MOs the board hides — shown separately, never summed
              into the headline. Computed {weekStats.computedAt} from the current
              board — live on every edit and refresh, no Save needed. Hover a card
              for the kg split.
            </div>
          </>
        )}
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
          insertPreview={dragPreview && dragPreview.insert && !dragPreview.insert.blockedReason ? dragPreview.insert : null}
          dropGhost={dropGhost}
          svgRef={chartSvgRef}
          onResizeStart={guardedStartResize}
          onContextMenu={handleContextMenu}
          onEmptyContextMenu={pickerEnabled ? handleEmptyContextMenu : undefined}
          onBlockClick={handleBlockClick}
          onZoomIn={zoomIn}
          onZoomOut={zoomOut}
          onResetZoom={resetZoom}
        />
        {pickerEnabled && (
          <div style={{ fontSize: 11, color: "#8a94a0", marginTop: 2 }}>
            Right-click an empty gap on a line to add a demand-plan SKU there
            (snaps left, changeover setup respected).
          </div>
        )}

        <div style={{ marginTop: 8 }}>
          <HoldingArea blocks={holdingArea} anchor={anchor} skuFormats={args.skuFormats ?? {}}
            highlightSku={highlightSku}
            onCardContextMenu={(block, cx, cy, shiftKey) => {
              // Same convention as calendar blocks (2026-09-01): plain
              // right-click toggles the SKU highlight, Shift+right-click
              // opens the action menu (here: the placement menu).
              closeMenu(); setPopover(null); setPicker(null);
              if (shiftKey && pickerEnabled) {
                setHoldMenu({ block, x: cx, y: cy });
              } else {
                setHoldMenu(null);
                setHighlightSku((prev) => (prev === block.sku ? null : block.sku));
              }
            }} />
        </div>

        <DragOverlay dropAnimation={null}>
          {activeDragBlock ? (
            <div style={{ pointerEvents: "none", position: "relative" }}>
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


      <div style={{ marginTop: 8 }}>
        <strong style={{ fontSize: 13, display: "block", marginBottom: 4 }}>
          SKU Adherence (live) — click a row to highlight on chart · "+" adds the missing tonnage to holding
        </strong>
        <AdherenceTable
          rows={currentAdherenceRows}
          formatOrder={(id) => displayOrderId(id, anchor)}
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

      {holdMenu && (
        <HoldingPlacePopover
          block={holdMenu.block}
          x={holdMenu.x}
          y={holdMenu.y}
          rows={holdMenuRows}
          anchor={anchor}
          onPlace={handleHoldingPlace}
          onClose={() => setHoldMenu(null)}
        />
      )}

      {picker && (
        <SkuPickerPopover
          lineName={picker.lineName}
          hour={picker.hour}
          x={picker.x}
          y={picker.y}
          anchor={anchor}
          rows={pickerRows}
          onPlace={handlePickerPlace}
          onAddCip={handlePickerAddCip}
          onAddTrial={handlePickerAddTrial}
          onClose={() => setPicker(null)}
        />
      )}

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
          onFill={isBlockLocked(popover.block) ? undefined : handleFill}
          onRemove={isBlockLocked(popover.block) ? undefined : (id) => {
            actions.removeToHolding(id);
            setPopover(null);
          }}
          onAddCip={handlePopoverAddCip}
          demandLeft={popover.block.block_type === "sku"
            ? demandLeftForSku(popover.block.sku) : undefined}
          onTogglePin={
            popover.block.block_type === "sku" &&
            !popover.block.locked &&
            !popover.block.completed &&
            // committed manprg MOs are already fixed — pinning would
            // double-commit them in the solver staging
            !(popover.block.attrs ?? "").includes("current_state:") &&
            !(lockedThroughH != null && popover.block.start_hour < lockedThroughH - 1e-9)
              ? handleTogglePin
              : undefined
          }
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
