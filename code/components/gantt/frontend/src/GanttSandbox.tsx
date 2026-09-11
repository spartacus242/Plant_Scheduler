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
import { computeKpis, computeAdherence, checkOverlapsSimple, serverKpisToKpiData, orderTarget, weekFulfillmentCredit, countChangeoversByWeek } from "./utils/kpi";
import { isCapable, recalcDuration, findOverlapsOnLine, placedQtyKg } from "./utils/validation";
import { LINE_HEIGHT, MIN_HOUR_WIDTH, MAX_HOUR_WIDTH, fitToWidth, hourToStamp, displayOrderId, setDemandBaseWeek, isoWeekLabel, isoWeekAtHour, isPastDemandWeek, nowHour, isoWeekKey, demandWeekKey, mondayBoundaries } from "./utils/layout";
import { getRate } from "./utils/validation";
import { meanCapableRate } from "./utils/rates";
import { computeDragPreview, computeInsertPlan, samePreview, type DragPreview, type InsertContext } from "./utils/dragPreview";
import { planDrop, type DropPlan } from "./utils/dropPlan";
import { isDouble, groupOf, sideOf } from "./utils/abLines";
import { buildRows } from "./utils/ganttRows";
import { skuColor, skuTextColor, blockLabel } from "./utils/colors";
import { coFlagsForPair, planPlacement, remainingDemandBySku, splitPlacementByOrders, effectiveSetupHours, isCipReqPair, type PlacementPlan } from "./utils/skuPicker";
import { blockKey, findBlock, findIdCollisions, sameBlock, type BlockRef } from "./utils/blockIdentity";
import { supplyRank, type Supply } from "./utils/stockRisk";
import {
  evaluateSchedule, humanText, isHardBlock, needsBanner, previewSupply, receiptsByDay, rulesOf, stampFor,
  type ScheduleSupply,
} from "./utils/supplyGlue";
import { setComponentValue, setFrameHeight } from "./streamlit";

import { GanttChart } from "./components/GanttChart";
import { ReceiptDaysContext } from "./components/TimeAxis";
import { HoldingArea } from "./components/HoldingArea";
import { AdherenceTable } from "./components/AdherenceTable";
import { ContextMenu } from "./components/ContextMenu";
import { BlockPopover } from "./components/BlockPopover";
import { SupplyDetailPanel } from "./components/SupplyDetailPanel";
import { HoldingPlacePopover, type HoldingPlaceRow } from "./components/HoldingPlacePopover";
import { deriveAutoHolding } from "./utils/holdingDerive";
import { SkuPickerPopover, type PickerRowData } from "./components/SkuPickerPopover";
import { DragPreviewBadge } from "./components/DragPreviewBadge";
import { SupplyPill } from "./components/KpiBar";
import { Legend } from "./components/Legend";
import { T, TILE, TILE_LABEL, BTN_BASE, BTN_PRIMARY, FONT_SANS, chipStyle } from "./utils/theme";

interface Props {
  args: SandboxArgs;
}

// Popup Apply / Snap on a projected clean: useScheduleState retags it as a
// planner clean and re-forecasts the line (planner request 2026-09-11);
// the report line says so, since reportAction overwrites the hook's note.
const PROJECTED_CIP_NOTE = " — projected clean is now a planner CIP held for the solver; the line's later cleans re-forecast";

export const GanttSandbox: React.FC<Props> = ({ args }) => {
  const [data, actions] = useScheduleState(args);
  const { schedule, cipWindows, holdingArea, holdingDismissed, lastAction } = data;

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
  setDemandBaseWeek(args.config.demand_base_iso_week ?? null, args.config.demand_anchor ?? null);
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

  // ── Supply timeline (contract 2026-09-01 §5/§9) ──
  // Verdicts are computed CLIENT-SIDE over the CURRENT schedule on every
  // edit; the payload only carries the tables. No payload (read-only
  // mounts, old report cache) = no stock surface anywhere.
  const stock = args.stock ?? null;
  const stockEnabled = stock !== null;
  const supply = useMemo<ScheduleSupply | null>(
    () => (stock ? evaluateSchedule(schedule, stock, caps, lockedThroughH) : null),
    [schedule, stock, caps, lockedThroughH],
  );
  // Real-minute stamps (helpers.timefmt.hour_to_stamp twin) so the banner
  // agrees with the Reconcile finding to the minute.
  const supplyStamp = useMemo(() => stampFor(anchor), [anchor]);
  // Header trucks (TimeAxis): receipts per calendar day, once per payload.
  const receiptDays = useMemo(() => (stock ? receiptsByDay(stock) : null), [stock]);
  // "Now" in board hours for the holding-card pills, refreshed with the
  // board (the same events that re-judge the pills) rather than per render:
  // the cards judge from max(now, lock) exactly like the Place popover's
  // rows (holdMenuRows takes a fresh one at open time), never from a lock
  // boundary already in the past.
  const holdingNowH = useMemo(
    () => nowHour(anchor),   // naive wall clock, like Python (layout.ts)
    [anchor, schedule, holdingArea],  // refresh triggers, not inputs
  );
  // Session acknowledgements: hide the chip for a key, keep the popover text.
  const [ackedKeys, setAckedKeys] = useState<Set<string>>(() => new Set());
  const acknowledgeKey = useCallback((key: string) => {
    setAckedKeys((prev) => { const next = new Set(prev); next.add(key); return next; });
  }, []);
  // Acknowledged = the WHOLE block-face decoration goes (chip, SHORT hatch,
  // DEPENDENT tick): GanttBlock draws all three from this one verdict, and
  // that is the intended reading — the planner has seen it; the popover,
  // pill counts and Reconcile keep the verdict.
  const supplyFor = useCallback(
    (b: ScheduleBlock): Supply | null => {
      if (!supply) return null;
      const key = blockKey(b);
      if (ackedKeys.has(key)) return null;
      return supply.verdicts.get(key) ?? null;
    },
    [supply, ackedKeys],
  );
  // The dragged / edited block at a candidate position against the OTHER
  // blocks (copy of the board timelines, no rebuild).
  const supplyPreview = useCallback(
    (block: ScheduleBlock, start: number, end: number, lineName: string): Supply | null =>
      stock && block.block_type === "sku"
        ? previewSupply(block, start, end, lineName, schedule, stock, caps, lockedThroughH,
                        supply?.timelines)
        : null,
    [stock, schedule, caps, lockedThroughH, supply],
  );
  // Pre-commit gate: the ONLY refusal is hard_block (zero on hand, zero
  // inbound, fresh feed). Everything else is warn-only after the commit.
  const supplyGate = useCallback(
    (block: ScheduleBlock, start: number, end: number, lineName: string): string | null => {
      if (!stock) return null;
      const s = supplyPreview(block, start, end, lineName);
      return s && isHardBlock(s, stock) ? humanText(s, supplyStamp) : null;
    },
    [stock, supplyPreview, supplyStamp],
  );
  // Post-commit banner: commits queue the (line, span) windows they touched,
  // stamped with the `schedule` they were queued against; once the schedule
  // state has actually changed (and `supply` re-derived), the worst
  // DEPENDENT/SHORT production block inside a window is announced — unless
  // an earlier banner (setup warning) already stands.
  // Windows, not keys: a split mints new ids and an insert slides a run of
  // neighbours, so "what changed" is a span on a line.
  // Production blocks only: a window edit leaves `schedule` untouched, so
  // the pending span would otherwise fire on a later, unrelated edit.
  // The stamp closes the same hole for a commit that turned out to be a
  // no-op (the mutator bailed, the schedule reference never changed): the
  // drain runs every render and drops a window whose schedule is still the
  // current one instead of keeping it for the next real edit.
  type SupplyWindow = { lineName: string; start: number; end: number };
  const pendingSupply = useRef<{ schedule: ScheduleBlock[]; windows: SupplyWindow[] } | null>(null);
  const queueSupplyCheck = useCallback(
    (block: ScheduleBlock | null | undefined, windows: SupplyWindow[]) => {
      if (!stock || !block || block.block_type !== "sku" || windows.length === 0) return;
      pendingSupply.current = { schedule, windows };
    },
    [stock, schedule],
  );
  useEffect(() => {
    const pend = pendingSupply.current;
    if (!pend || !supply) return;
    pendingSupply.current = null;
    if (pend.schedule === schedule) return; // no-op commit: nothing to announce
    let worst: Supply | null = null;
    for (const b of schedule) {
      if (b.block_type !== "sku") continue;
      const inWindow = pend.windows.some((w) =>
        w.lineName === b.line_name && b.start_hour < w.end + 1e-6 && b.end_hour > w.start - 1e-6);
      if (!inWindow) continue;
      const s = supply.verdicts.get(blockKey(b));
      if (!s || !needsBanner(s)) continue;
      if (!worst || supplyRank(s) > supplyRank(worst)) worst = s;
    }
    if (!worst) return;
    const text = humanText(worst, supplyStamp);
    setWarnMsg((prev) => prev ?? text);
  });
  // Supply banners are per-commit: a commit clears the standing one so a
  // stale sentence never outlives the edit it described. Boards WITHOUT a
  // stock payload keep the pre-supply behaviour exactly (a setup warning
  // stands until the next drag / typed edit replaces it).
  const clearSupplyBanner = useCallback(() => {
    if (stockEnabled) setWarnMsg(null);
  }, [stockEnabled]);

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

  // Setup hours between two SKUs: the matrix value rounded UP to whole hours
  // and floored at the CIP duration for a cip_req pair — exactly what the
  // solver reserves (skuPicker.effectiveSetupHours; fix FE / audit C36,
  // changeover-9). 0 for the same SKU or an unknown pair.
  const setupHoursFor = useCallback(
    (fromSku: string, toSku: string): number => {
      if (fromSku === toSku) return 0;
      return effectiveSetupHours(
        args.changeovers?.[fromSku]?.[toSku],
        isCipReqPair(args.kpis?.co_pairs, fromSku, toSku),
        args.config.cip_duration_h || 6,
      );
    },
    [args.changeovers, args.kpis, args.config.cip_duration_h],
  );
  // Between two BLOCKS (0 when either side is a non-production window: a
  // CIP's 6h wash absorbs any changeover).
  const setupBetween = useCallback(
    (from: ScheduleBlock | undefined, to: ScheduleBlock | undefined): number => {
      if (!from || !to) return 0;
      if (isWindowBlock(from.block_type) || isWindowBlock(to.block_type)) return 0;
      return setupHoursFor(from.sku, to.sku);
    },
    [setupHoursFor],
  );

  // Non-blocking honesty check after a placement: does the gap to either
  // neighbour undercut the required setup time? The planner MAY place blocks
  // closer (their call) — but never silently.
  const setupWarning = useCallback(
    (me: ScheduleBlock, lineName: string, newStart: number, newEnd: number): string | null => {
      // The piece itself, not "every block with this id": a split MO's
      // sibling is a real neighbour.
      const others = [...schedule, ...cipWindows].filter(
        (b) => !sameBlock(b, me) && b.line_name === lineName,
      );
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
    // The draggable id is the PIECE key (id|start_hour); the block object
    // rides in the drag data.
    const block =
      (active.data.current?.block as ScheduleBlock | undefined) ??
      schedule.find((b) => blockKey(b) === activeId) ??
      cipWindows.find((b) => blockKey(b) === activeId);
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
    // Supply row for the dragged production block only, at the landing the
    // drop would commit (the insert point on an insert gesture).
    if (next && stockEnabled && block.block_type === "sku") {
      const ins = next.insert && !next.insert.blockedReason ? next.insert : null;
      const s = supplyPreview(block, ins ? ins.insStart : next.startHour,
                              ins ? ins.insEnd : next.endHour, next.targetLine);
      next.supply = s;
      next.supplyText = s ? humanText(s, supplyStamp) : null;
    }
    // Recomputed every frame while auto-scroll runs: only re-render on change.
    setDragPreview((prev) => (samePreview(prev, next) ? prev : next));
  }, [schedule, cipWindows, lines, caps, anchor, downtime, insertCtx, computeLanding,
      stockEnabled, supplyPreview, supplyStamp]);
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
      // The block dnd-kit carries (GanttBlock / HoldingCard put it in the
      // drag data) resolved to the live state object — every mutation below
      // names the PIECE, never a bare id shared by split MO pieces.
      const carried = active.data.current?.block as ScheduleBlock | undefined;

      // ── Drag FROM holding TO a line row ──
      if (activeId.startsWith("holding_")) {
        // HoldingCard registers `holding_<piece key>` (id|start_hour): the
        // carried object names the card; the key is only the fallback.
        const activeKey = activeId.replace("holding_", "");
        const block = (carried && findBlock(holdingArea, carried)) ?? holdingArea.find((b) => blockKey(b) === activeKey);
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
          .filter((b) => !sameBlock(b, block) && b.line_name === targetLineName)
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
        const placedH = dur <= room + 1e-6 ? dur : room;
        const gate = supplyGate(block, startHour, startHour + placedH, targetLineName);
        if (gate) {
          reject(gate);
          return;
        }
        setErrorMsg(null);
        clearSupplyBanner();
        if (dur <= room + 1e-6) {
          // dur is the card's kg priced at THIS line's rate (recalcDuration)
          // and the kg is capped at what the line makes in that window —
          // never a physically impossible row (fix FE / audit ui-1, ui-14).
          actions.restoreFromHolding(block, targetLine.line_name, targetLine.line_id, startHour, dur,
            placedQtyKg(block, targetLineName, startHour, dur, caps, downtime));
        } else {
          actions.restorePartialFromHolding(
            block, targetLine.line_name, targetLine.line_id, startHour, room, dur);
        }
        queueSupplyCheck(block, [{ lineName: targetLineName, start: startHour, end: startHour + placedH }]);
        return;
      }

      // ── Drag TO holding area ──
      if (overId === "holding_area") {
        const moving = carried
          ? (findBlock(schedule, carried) ?? findBlock(cipWindows, carried))
          : undefined;
        if (!moving) return;
        if (isBlockLocked(moving)) {
          reject(lockReason(moving));
          return;
        }
        setErrorMsg(null);
        actions.removeToHolding(moving);
        return;
      }

      // ── Normal in-chart drag ──
      const block = carried
        ? (findBlock(schedule, carried) ?? findBlock(cipWindows, carried))
        : undefined;
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
        // Exclude the dragged PIECE only: excluding by id let a split MO
        // piece land on its own sibling (same id, different start). The
        // displaced neighbour is resolved by piece key for the same reason.
        if (findOverlapsOnLine(allBlocks.filter((b) => !sameBlock(b, block)), block.line_name, "", newStart, newEnd)) {
          const plan = computeInsertPlan(block, block.line_name, newStart, dur, allBlocks, insertCtx);
          const nextBlk = plan ? allBlocks.find((n) => blockKey(n) === plan.nextKey) : undefined;
          if (plan && nextBlk && !plan.blockedReason) {
            const shifted = allBlocks
              .filter((b) => !sameBlock(b, block) && b.line_name === block.line_name)
              .filter((b) => b.start_hour >= nextBlk.start_hour - 1e-9);
            const lockedHit = shifted.find(isBlockLocked);
            if (lockedHit) {
              reject(`Cannot insert: would slide ${lockedHit.sku || lockedHit.label} — ${lockReason(lockedHit)}`);
              return;
            }
            const gate = supplyGate(block, plan.insStart, plan.insStart + dur, block.line_name);
            if (gate) { reject(gate); return; }
            setErrorMsg(null);
            clearSupplyBanner();
            actions.insertShift(block, block.line_name, block.line_id, plan.insStart, dur, shifted, plan.deltaH);
            // The inserted block AND every slid neighbour may have changed verdict.
            queueSupplyCheck(block, [{ lineName: block.line_name, start: plan.insStart,
              end: Math.max(plan.insStart + dur, ...shifted.map((b) => b.end_hour + plan.deltaH)) }]);
            return;
          }
          reject(plan && plan.blockedReason
            ? `Cannot insert: ${plan.blockedReason}`
            : `Overlap on ${block.line_name} at ${hourToStamp(newStart, anchor)}`);
          return;
        }
        const gate = supplyGate(block, newStart, newEnd, block.line_name);
        if (gate) { reject(gate); return; }
        setErrorMsg(null);
        actions.moveBlock(block, block.line_name, block.line_id, newStart, dur);
        setWarnMsg(setupWarning(block, block.line_name, newStart, newStart + dur));
        queueSupplyCheck(block, [{ lineName: block.line_name, start: newStart, end: newEnd }]);
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
        // Same piece-only exclusion / key resolution as the same-line path.
        if (findOverlapsOnLine(allBlocks.filter((b) => !sameBlock(b, block)), targetLine.line_name, "", newStart, newEnd)) {
          const plan = computeInsertPlan(block, targetLine.line_name, newStart, dur, allBlocks, insertCtx);
          const nextBlk = plan ? allBlocks.find((n) => blockKey(n) === plan.nextKey) : undefined;
          if (plan && nextBlk && !plan.blockedReason) {
            const shifted = allBlocks
              .filter((b) => !sameBlock(b, block) && b.line_name === targetLine.line_name)
              .filter((b) => b.start_hour >= nextBlk.start_hour - 1e-9);
            const lockedHit = shifted.find(isBlockLocked);
            if (lockedHit) {
              reject(`Cannot insert: would slide ${lockedHit.sku || lockedHit.label} — ${lockReason(lockedHit)}`);
              return;
            }
            const gate = supplyGate(block, plan.insStart, plan.insStart + dur, targetLine.line_name);
            if (gate) { reject(gate); return; }
            setErrorMsg(null);
            clearSupplyBanner();
            actions.insertShift(block, targetLine.line_name, targetLine.line_id, plan.insStart, dur, shifted, plan.deltaH);
            queueSupplyCheck(block, [{ lineName: targetLine.line_name, start: plan.insStart,
              end: Math.max(plan.insStart + dur, ...shifted.map((b) => b.end_hour + plan.deltaH)) }]);
            return;
          }
          reject(plan && plan.blockedReason
            ? `Cannot insert: ${plan.blockedReason}`
            : `Overlap on ${targetLine.line_name} at ${hourToStamp(newStart, anchor)}`);
          return;
        }
        const gate = supplyGate(block, newStart, newEnd, targetLine.line_name);
        if (gate) { reject(gate); return; }
        setErrorMsg(null);
        actions.moveBlock(block, targetLine.line_name, targetLine.line_id, newStart, dur);
        setWarnMsg(setupWarning(block, targetLine.line_name, newStart, newEnd));
        queueSupplyCheck(block, [{ lineName: targetLine.line_name, start: newStart, end: newEnd }]);
      }
    },
    [schedule, cipWindows, holdingArea, actions, caps, lines, computeLanding, reject, anchor, downtime,
     isBlockLocked, lockReason, intoLockedZone, lockedThroughH, insertCtx, stopAutoScroll, setupBetween,
     setupWarning, supplyGate, queueSupplyCheck, clearSupplyBanner, args.config.min_run_hours],
  );

  // The resize hook carries the piece (BlockRef) from pointer-down to
  // commit; the commit re-reads the live object for the supply gate.
  const onResizeCommit = useCallback(
    (ref: BlockRef, newStart: number, newEnd: number) => {
      const block = findBlock(schedule, ref) ?? findBlock(cipWindows, ref);
      if (block) {
        const gate = supplyGate(block, newStart, newEnd, block.line_name);
        if (gate) { reject(gate); return; }
      }
      clearSupplyBanner();
      actions.resizeBlock(ref, newStart, newEnd);
      queueSupplyCheck(block, block ? [{ lineName: block.line_name, start: newStart, end: newEnd }] : []);
    },
    [actions, schedule, cipWindows, supplyGate, reject, queueSupplyCheck, clearSupplyBanner],
  );
  const { resizing, startResize } = useBlockResize(args.config.min_run_hours, onResizeCommit);

  // Resize entry gate: drags and edits check locks, so resize must too — an
  // ungated handle let a locked block be resized (found in slice-1 QA).
  const guardedStartResize = useCallback<typeof startResize>(
    (block, edge, startHour, endHour, clientX, hw) => {
      if (isBlockLocked(block as ScheduleBlock)) {
        reject(lockReason(block as ScheduleBlock));
        return;
      }
      startResize(block, edge, startHour, endHour, clientX, hw);
    },
    [isBlockLocked, lockReason, reject, startResize],
  );

  const { menu, openMenu, closeMenu } = useContextMenu();
  // The menu state keeps (id, start_hour): enough to name the piece again
  // (blockIdentity) without widening useContextMenu.
  const menuRef = useMemo<BlockRef | null>(
    () => (menu.visible && menu.blockId ? { id: menu.blockId, start_hour: menu.startHour } : null),
    [menu.visible, menu.blockId, menu.startHour],
  );
  const handleContextMenu = useCallback(
    (e: React.MouseEvent, block: ScheduleBlock) => {
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
      openMenu(e.clientX, e.clientY, block.id, block.block_type, block.start_hour, block.end_hour);
    },
    [openMenu, reject, isBlockLocked, lockReason, setHighlightSku],
  );

  const [popover, setPopover] = useState<{ block: ScheduleBlock; x: number; y: number } | null>(null);
  // "Supply details" modal (planner feedback 2026-09-02), opened from the
  // popover's button; re-resolved by piece at render time.
  const [supplyDetailFor, setSupplyDetailFor] = useState<ScheduleBlock | null>(null);
  const handleBlockClick = useCallback(
    (ref: BlockRef) => {
      const block = findBlock(schedule, ref) ?? findBlock(cipWindows, ref);
      if (!block) return;
      setPopover({ block, x: 300, y: 200 });
    },
    [schedule, cipWindows],
  );

  // ?focus=<block_id> (Reconcile finding -> this block): seed the SKU
  // highlight and scroll the piece into view once per focus value.
  const focusedRef = useRef<string | null>(null);
  useEffect(() => {
    const id = args.focusBlock ?? null;
    if (!id || focusedRef.current === id) return;
    const b = schedule.find((x) => x.id === id) ?? cipWindows.find((x) => x.id === id);
    if (!b) return;
    focusedRef.current = id;
    if (b.sku && !isWindowBlock(b.block_type)) setHighlightSku(b.sku);
    const esc = typeof CSS !== "undefined" && typeof CSS.escape === "function"
      ? CSS.escape(id) : id.replace(/["\\]/g, "\\$&");
    const raf = requestAnimationFrame(() => {
      const el = containerRef.current?.querySelector(`[data-block-id="${esc}"]`);
      el?.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    });
    return () => cancelAnimationFrame(raf);
  }, [args.focusBlock, schedule, cipWindows]);

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
  // Interval source = current_state.project_cips' (fix FE / audit cip-14):
  // cip_info MaxHoursBetweenCIP for the line (config.cip_interval_h), else
  // the configured default — never inferred from the spacing of the
  // projected cleans already drawn (that spacing is an OUTPUT of the rule).
  const cipIntervalFor = useCallback((lineName: string): number => {
    const cfgI = args.config.cip_interval_h?.[lineName];
    if (cfgI && cfgI > 0) return cfgI;
    return args.config.cip_interval_default_h || 120;
  }, [args.config]);

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
    // shiftIds are the PIECES (objects): a bare id would slide the first
    // piece of a split MO whichever piece sits after the clean.
    actions.addCipReforecast({
      lineName, lineId, startHour, duration: d, intervalH: cipIntervalFor(lineName),
      horizonH: horizon, shiftIds: shifted, shiftH: shortfall,
      // a projected slot that would split/push a committed block is skipped
      immovable: isBlockLocked,
    });
    const slidRun = shortfall > 0 ? shifted.find((b) => b.block_type === "sku") : undefined;
    if (slidRun) {
      clearSupplyBanner();
      queueSupplyCheck(slidRun, [{ lineName, start: startHour, end: horizon }]);
    }
    return null;
  }, [args.config, lockedThroughH, schedule, cipWindows, isBlockLocked, actions,
      cipIntervalFor, horizon, anchor, queueSupplyCheck, clearSupplyBanner]);

  const prevEndOnLine = useCallback((lineName: string, beforeHour: number, exclude?: ScheduleBlock): number =>
    Math.max(0, ...[...schedule, ...cipWindows]
      .filter((b) => !(exclude && sameBlock(b, exclude)) && b.line_name === lineName &&
                     b.end_hour <= beforeHour + 1e-6 &&
                     !String(b.id).startsWith("cipinfo_") && !String(b.id).startsWith("dt_"))
      .map((b) => b.end_hour)),
  [schedule, cipWindows]);

  const handlePopoverAddCip = useCallback((ref: ScheduleBlock, dir: "before" | "after"): string | null => {
    const block = findBlock(schedule, ref) ?? findBlock(cipWindows, ref);
    if (!block) return "block not found";
    if (dir === "after") return placeCip(block.line_name, block.line_id, block.end_hour);
    const d = args.config.cip_duration_h || 6;
    const start = Math.max(prevEndOnLine(block.line_name, block.start_hour, block),
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
    const nowH = nowHour(anchor);
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
          setupFor: setupHoursFor,
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
        inFlags: plan.prevSku ? coFlagsForPair(args.coFlags, plan.prevSku, block.sku, args.kpis?.co_pairs) : [],
        outFlags: plan.nextSku ? coFlagsForPair(args.coFlags, block.sku, plan.nextSku, args.kpis?.co_pairs) : [],
      });
    }
    rows.sort((a, b) => a.plan.startHour - b.plan.startHour);
    return rows;
  }, [holdMenu, schedule, cipWindows, lines, args.changeovers, args.coFlags, args.kpis, setupHoursFor,
      anchor, lockedThroughH, horizon, caps, downtime, args.config.min_run_hours]);

  const handleHoldingPlace = useCallback(
    (row: HoldingPlaceRow) => {
      if (!holdMenu || row.plan.reason) return;
      const block = holdMenu.block;
      const { startHour, durationH } = row.plan;
      const allBlocks = [...schedule, ...cipWindows];
      // A parked piece's sibling still on the board shares its id: exclude
      // the card itself only, so the sibling stays an obstacle.
      if (findOverlapsOnLine(allBlocks.filter((b) => !sameBlock(b, block)), row.lineName, "", startHour, startHour + durationH)) {
        reject(`Overlap on ${row.lineName} at ${hourToStamp(startHour, anchor)}`);
        setHoldMenu(null);
        return;
      }
      // ONE pricing rule for both gestures (drag and right-click Place):
      // the card's kg integrated at THIS line's rate over the placed window
      // (recalcDuration -> hoursForQty, whole hours). Fix FE / audit ui-1:
      // the two paths used to disagree by 80-140 h on the same card.
      const fullDur = recalcDuration(block, row.lineName, caps, downtime, startHour) ?? block.run_hours;
      const placedH = durationH >= fullDur - 1e-6 ? fullDur : durationH;
      const gate = supplyGate(block, startHour, startHour + placedH, row.lineName);
      if (gate) {
        reject(gate);
        setHoldMenu(null);
        return;
      }
      setErrorMsg(null);
      clearSupplyBanner();
      if (durationH >= fullDur - 1e-6) {
        actions.restoreFromHolding(block, row.lineName, row.lineId, startHour, fullDur,
          placedQtyKg(block, row.lineName, startHour, fullDur, caps, downtime));
      } else {
        actions.restorePartialFromHolding(
          block, row.lineName, row.lineId, startHour, durationH, fullDur);
      }
      queueSupplyCheck(block, [{ lineName: row.lineName, start: startHour, end: startHour + placedH }]);
      setHoldMenu(null);
    },
    [holdMenu, schedule, cipWindows, caps, downtime, anchor, actions, reject, supplyGate, queueSupplyCheck,
     clearSupplyBanner],
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
    const nowH = nowHour(anchor);
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
        setupFor: setupHoursFor,
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
        inFlags: plan.prevSku ? coFlagsForPair(args.coFlags, plan.prevSku, c.sku, args.kpis?.co_pairs) : [],
        outFlags: plan.nextSku ? coFlagsForPair(args.coFlags, c.sku, plan.nextSku, args.kpis?.co_pairs) : [],
      });
    }
    rows.sort((a, b) => b.remainingKg - a.remainingKg);
    return rows;
  }, [picker, schedule, cipWindows, args.demandTargets, args.capabilities, args.kpis, setupHoursFor,
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
      // Hard-block gate on the whole placement as ONE virtual run (the
      // per-order segments are contiguous and draw the same components).
      const virtual: ScheduleBlock = {
        id: "picker_virtual", line_id: picker.lineId, line_name: picker.lineName,
        order_id: segments[0].orderId, sku: row.sku, start_hour: startHour,
        end_hour: startHour + durationH, run_hours: durationH, is_trial: false,
        block_type: "sku", qty_kg: qtyKg > 0 ? qtyKg : undefined,
      };
      const gate = supplyGate(virtual, startHour, startHour + durationH, picker.lineName);
      if (gate) {
        reject(gate);
        setPicker(null);
        return;
      }
      setErrorMsg(null);
      clearSupplyBanner();
      actions.addProduction(picker.lineName, picker.lineId, row.sku, row.desc, segments);
      queueSupplyCheck(virtual, [{ lineName: picker.lineName, start: startHour, end: startHour + durationH }]);
      setPicker(null);
    },
    [picker, schedule, cipWindows, args.demandTargets, args.capabilities,
     coveredByOrder, anchor, actions, reject, supplyGate, queueSupplyCheck, clearSupplyBanner],
  );

  // Typed edits from the popover (start / duration / tonnage). Same guards as
  // a drag: locked blocks reject, min-run enforced, overlaps reject. Returns
  // null when committed (popover closes) or the rejection reason — the
  // popover shows it inline, because the chart's top banner is out of the
  // user's sight while the popup is open.
  const handleApplyEdit = useCallback(
    (ref: BlockRef, edit: { startHour: number; durationH: number; qtyKg: number | null }): string | null => {
      const block = findBlock(schedule, ref) ?? findBlock(cipWindows, ref);
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
          !sameBlock(b, block) &&
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
      const patch: Partial<ScheduleBlock> = {
        start_hour: newStart,
        end_hour: newEnd,
        run_hours: edit.durationH,
      };
      if (!isWindowBlock(block.block_type)) {
        patch.qty_kg = edit.qtyKg ?? undefined;
      }
      // Typed kg changes the need: gate the block AS EDITED.
      const gate = supplyGate({ ...block, ...patch }, newStart, newEnd, block.line_name);
      if (gate) {
        reject(gate);
        return gate;
      }
      setErrorMsg(null);
      const wasProjectedCip = block.block_type === "cip" && (block.attrs ?? "").includes("cip_projected");
      actions.updateBlock(block, patch);
      actions.reportAction(
        `Edited ${block.order_id || block.sku}: ${hourToStamp(newStart, anchor)} for ${edit.durationH}h` +
          (edit.qtyKg ? `, ${edit.qtyKg.toLocaleString()} kg` : "") +
          (wasProjectedCip ? PROJECTED_CIP_NOTE : ""),
      );
      setWarnMsg(setupWarning(block, block.line_name, newStart, newEnd));
      queueSupplyCheck(block, [{ lineName: block.line_name, start: newStart, end: newEnd }]);
      return null;
    },
    [schedule, cipWindows, actions, reject, anchor, args.config.min_run_hours, isBlockLocked, lockReason,
     intoLockedZone, lockedThroughH, setupWarning, supplyGate, queueSupplyCheck],
  );

  // Pin toggle (popup): a pinned demand block becomes committed line-time —
  // the solver must plan around it (Scenario F stages it as a blocked window
  // and credits its kg against demand). The block also refuses drag/resize/
  // edits until unpinned, so the board can't silently disagree with what the
  // solve was told.
  const handleTogglePin = useCallback(
    (ref: BlockRef, pinned: boolean) => {
      const block = findBlock(schedule, ref);
      if (!block) return;
      actions.updateBlock(block, { pinned });
      actions.reportAction(
        pinned
          ? `📌 Pinned ${block.order_id || block.sku} — the solver will plan around it`
          : `Unpinned ${block.order_id || block.sku} — it can move again`,
      );
      setPopover((p) =>
        p && sameBlock(p.block, block) ? { ...p, block: { ...p.block, pinned } } : p,
      );
    },
    [schedule, actions],
  );

  // Snap flush against the neighbouring block, leaving EXACTLY the setup
  // time between the two SKUs (user request 2026-08-14). Returns null on
  // success or the reason it could not snap.
  const handleSnap = useCallback(
    (ref: BlockRef, dir: "left" | "right"): string | null => {
      const block = findBlock(schedule, ref) ?? findBlock(cipWindows, ref);
      if (!block) return "Block no longer exists";
      if (isBlockLocked(block)) return lockReason(block);
      const dur = block.end_hour - block.start_hour;
      const others = [...schedule, ...cipWindows].filter(
        (b) => !sameBlock(b, block) && b.line_name === block.line_name,
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
      const gate = supplyGate(block, newStart, newEnd, block.line_name);
      if (gate) return gate;
      setErrorMsg(null);
      clearSupplyBanner();
      const wasProjectedCip = block.block_type === "cip" && (block.attrs ?? "").includes("cip_projected");
      actions.updateBlock(block, {
        start_hour: newStart,
        end_hour: newEnd,
        run_hours: dur,
      });
      actions.reportAction(
        `Snapped ${block.order_id || block.sku} ${dir} against ${against.sku || against.label}` +
          (setup > 0 ? ` (setup ${setup}h respected)` : " (no setup needed)") +
          (wasProjectedCip ? PROJECTED_CIP_NOTE : ""),
      );
      queueSupplyCheck(block, [{ lineName: block.line_name, start: newStart, end: newEnd }]);
      return null;
    },
    [schedule, cipWindows, actions, isBlockLocked, lockReason, intoLockedZone, lockedThroughH, anchor,
     setupBetween, supplyGate, queueSupplyCheck, clearSupplyBanner],
  );

  // Fill the empty space next to a block (user request 2026-08-18): extend
  // the block's edge to the neighbouring block minus the required setup
  // time (from_sku -> to_sku), or to the horizon/lock boundary when the
  // line is open. Resize semantics — qty scales with duration.
  const handleFill = useCallback(
    (ref: BlockRef, dir: "left" | "right" | "both"): string | null => {
      const block = findBlock(schedule, ref) ?? findBlock(cipWindows, ref);
      if (!block) return "Block no longer exists";
      if (isBlockLocked(block)) return lockReason(block);
      const others = [...schedule, ...cipWindows].filter(
        (b) => !sameBlock(b, block) && b.line_name === block.line_name,
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
      // Fill scales kg with the duration (resizeBlock): gate the grown run.
      const grownKg = block.qty_kg && block.end_hour > block.start_hour
        ? block.qty_kg * (newEnd - newStart) / (block.end_hour - block.start_hour)
        : block.qty_kg;
      const gate = supplyGate({ ...block, qty_kg: grownKg }, newStart, newEnd, block.line_name);
      if (gate) return gate;
      setErrorMsg(null);
      clearSupplyBanner();
      actions.resizeBlock(block, newStart, newEnd);
      actions.reportAction(
        `Filled ${block.order_id || block.sku} ${notes.join(" and ")}`,
      );
      queueSupplyCheck(block, [{ lineName: block.line_name, start: newStart, end: newEnd }]);
      return null;
    },
    [schedule, cipWindows, actions, isBlockLocked, lockReason, lockedThroughH, horizon, setupBetween,
     supplyGate, queueSupplyCheck, clearSupplyBanner],
  );

  // Split / remove from the Shift+right-click menu name the piece the menu
  // was opened on; a split's two new pieces are checked by their span.
  const handleMenuSplit = useCallback(
    (_id: string, splitHour: number) => {
      if (!menuRef) return;
      const block = findBlock(schedule, menuRef);
      clearSupplyBanner();
      actions.splitBlock(menuRef, splitHour);
      if (block) {
        queueSupplyCheck(block, [{ lineName: block.line_name, start: block.start_hour, end: block.end_hour }]);
      }
    },
    [menuRef, schedule, actions, queueSupplyCheck, clearSupplyBanner],
  );
  const handleMenuRemove = useCallback(
    (_id: string) => { if (menuRef) actions.removeToHolding(menuRef); },
    [menuRef, actions],
  );
  const handleMenuDetails = useCallback(
    (_id: string) => { if (menuRef) handleBlockClick(menuRef); },
    [menuRef, handleBlockClick],
  );

  // Remaining demand for a SKU by week (popup helper): target minus what
  // the CURRENT board schedules per order, from the same adherence rules.
  const demandLeftForSku = useCallback(
    (sku: string): { week: string; left_kg: number; total_kg: number; scheduled_kg: number }[] => {
      const rows = computeAdherence(schedule, args.demandTargets, args.capabilities, coveredByOrder);
      const bySku = rows.filter((r) => r.sku === sku);
      const out: { week: string; left_kg: number; total_kg: number; scheduled_kg: number }[] = [];
      // (iso_year, iso_week) keys — W53-2026 is not "before" W1-2027
      // (fix FE / audit time-8, C24).
      const nowKey = isoWeekKey(new Date());
      for (const r of bySku) {
        const m = /-W(\d+)$/.exec(r.order_id);
        const k = m ? parseInt(m[1], 10) : null;
        if (k !== null && demandWeekKey(k, anchor) < nowKey) continue; // past weeks are misses, not plan items
        const wk = k !== null ? `W${isoWeekLabel(k, anchor)}` : "—";
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
    const nowKey = isoWeekKey(new Date());   // (year, week) key, see layout.ts
    for (const r of rows) {
      const m = /-W(\d+)$/.exec(r.order_id);
      if (!m) continue;
      const k = parseInt(m[1], 10);
      if (demandWeekKey(k, anchor) < nowKey) continue; // past weeks are misses, not plan
      const wk = `W${isoWeekLabel(k, anchor)}`;
      const d = (dem[wk] ??= { sched: 0, board: 0, target: 0 });
      // Credit caps at the order's own target — overproduction on one order
      // cannot raise the week's fulfillment (same cap as weekly_breakdown).
      d.sched += weekFulfillmentCredit(r);
      const tgt = orderTarget(r.qty_min, r.qty_max);
      d.board += Math.min(boardByOrder[r.order_id] ?? 0, tgt);
      d.target += tgt;
    }
    // Changeovers per ISO week from the ONE port of score_changeovers'
    // weekly rows (kpi.countChangeoversByWeek — fix FE / audit ui-5): the
    // same CIP waiver, cip_req rule and machine flags as the KPI bar and
    // the scorecard's week table, bucketed into the week the incoming
    // block starts. Week marks = hour 0 (the anchor's own, possibly
    // partial, week) + the Monday boundaries inside the horizon — the
    // scorecard's _iso_week_bounds, labelled by the ISO week 1 h in.
    const marks = [0, ...mondayBoundaries(anchor, 0, horizon).filter((h) => h > 1e-9)];
    const weekly = countChangeoversByWeek(
      schedule, args.kpis?.co_pairs ?? {}, args.kpis?.co_default ?? undefined, cipWindows, marks, horizon);
    for (const w of weekly) {
      const wk = `W${isoWeekAtHour(anchor, marks[w.idx] + 1)}`;
      const st = (stats[wk] ??= { boardPct: null, madePct: null, tl: 0, ffs: 0, cp: 0, ttp: 0, cipReq: 0, cipGap: null as { lineName: string; lineId: number; start: number } | null, board: 0, credit: 0, target: 0 });
      st.tl += w.topload_changes;
      st.ffs += w.ffs_changes;
      st.cp += w.casepacker_changes;
      st.ttp += w.ttp_changes;
      st.cipReq += w.cip_req_violations;
      if (!st.cipGap && w.cip_gap) st.cipGap = w.cip_gap;
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
  }, [schedule, cipWindows, args.demandTargets, args.capabilities, args.kpis, anchor, coveredByOrder, horizon]);

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
    const key = JSON.stringify({ schedule, cipWindows, holdingArea, holdingDismissed, lastAction });
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
  }, [schedule, cipWindows, holdingArea, holdingDismissed, lastAction]);

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
  const lastSeenServerHolding = useRef(JSON.stringify({ h: args.holdingArea ?? [], d: args.holdingDismissed ?? [] }));
  useEffect(() => {
    const incoming = JSON.stringify({ h: args.holdingArea ?? [], d: args.holdingDismissed ?? [] });
    const changed = incoming !== lastSeenServerHolding.current;
    lastSeenServerHolding.current = incoming;
    if (!changed) return;
    if (dirty) return; // retry once the edits are pushed
    adoptingHolding.current = true;
    actions.setHoldingFromServer(args.holdingArea ?? [], args.holdingDismissed ?? []);
  }, [args.holdingArea, args.holdingDismissed, dirty, actions]);
  // ONE push path. A Save carries a saveRequest — Python writes the board it
  // just received (disk, or a named version) and never a stale one. A
  // separate Streamlit Save button could only see the LAST pushed state, so
  // edits made since the last "Refresh checks" silently missed the save —
  // the #1 trap on the planner's main screen (2026-09-11).
  const pushState = useCallback(
    (saveRequest: { kind: "disk" | "version"; nonce: number } | null = null) => {
      // Save guard (fix FE / audit writeback-8): never hand Python a board
      // where one block_id names two different runs — pin /
      // delete key on block_id and would hit both. Split pieces of one MO
      // may share an id; two unrelated blocks may not. Guards every push,
      // so a Save can never write such a board either.
      const dupes = findIdCollisions([...schedule, ...cipWindows]);
      if (dupes.length) {
        reject(`Duplicate block id(s) on the board: ${dupes.slice(0, 5).join(", ")}`
               + `${dupes.length > 5 ? ` (+${dupes.length - 5} more)` : ""} — split or re-add the affected blocks before pushing`);
        return;
      }
      lastPushed.current = JSON.stringify({ schedule, cipWindows, holdingArea, holdingDismissed, lastAction });
      setDirty(false);
      setComponentValue({ schedule, cipWindows, holdingArea, holdingDismissed, lastAction, saveRequest });
    },
    [schedule, cipWindows, holdingArea, holdingDismissed, lastAction, reject],
  );
  const pushRefresh = useCallback(() => pushState(null), [pushState]);
  // The nonce is wall-clock time: Python remembers the last nonce it acted
  // on, so a rerun that re-reads the same component value never saves twice,
  // and a remount (fresh counter) can never collide with an old nonce.
  const pushSave = useCallback(
    (kind: "disk" | "version") => pushState({ kind, nonce: Date.now() }),
    [pushState],
  );
  const pushSaveRef = useRef(pushSave);
  useEffect(() => { pushSaveRef.current = pushSave; });

  useEffect(() => {
    const h = containerRef.current?.scrollHeight ?? 800;
    setFrameHeight(h + 20);
  });

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === "z") { e.preventDefault(); actions.undo(); }
      if (e.ctrlKey && e.key === "y") { e.preventDefault(); actions.redo(); }
      if (e.ctrlKey && (e.key === "s" || e.key === "S")) { e.preventDefault(); pushSaveRef.current("disk"); }
      if (e.key === "Escape") { closeMenu(); setPopover(null); setPicker(null); setHoldMenu(null); setHighlightSku(null); setErrorMsg(null); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [actions, closeMenu]);

  const ghostBg = activeDragBlock
    ? skuColor(activeDragBlock.sku, activeDragBlock.block_type)
    : T.accent;
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
      style={{ fontFamily: FONT_SANS, color: T.ink }}
      onClick={() => { closeMenu(); setPopover(null); setPicker(null); setHoldMenu(null); }}
    >
      {/* Board toolbar: state chips (+ the supply pill) on the left, the
          three actions on the right. Save always pushes the live board
          first (see pushState). */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 4 }}>
        <div style={{ flex: 1, minWidth: 200, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {dirty ? (
            <span style={chipStyle("warn")} title="Edits are on the board but not yet checked or saved">● unsaved edits</span>
          ) : (
            <span style={chipStyle("neutral")} title="The board matches the last check / save">✓ checked</span>
          )}
          {kpis.overlaps.length > 0 && (
            <span style={chipStyle("bad")} title={kpis.overlaps.join("\n")}>
              ⚠ {kpis.overlaps.length} overlap(s): {kpis.overlaps[0]}
            </span>
          )}
          {stockEnabled && stock && supply && (
            <SupplyPill counts={supply.counts} asOf={stock.as_of} feedState={stock.feed_state} />
          )}
        </div>
        <button
          onClick={pushRefresh}
          title="Rescore, rebuild holding and rerun every check on the current board (nothing is written to disk)"
          style={{
            ...BTN_BASE,
            borderColor: dirty ? "#F1CFA9" : T.rule,
            background: dirty ? T.warnSoft : T.surface,
            color: dirty ? T.warn : T.ink2,
          }}
        >
          ⟳ Refresh checks
        </button>
        <button
          onClick={() => pushSave("version")}
          title="Save the board as a named version (name in the box above the board) — your edits are pushed first"
          style={BTN_BASE}
        >
          Save as version
        </button>
        <button
          onClick={() => pushSave("disk")}
          title="Save the board to calendar_blocks.csv — your edits are pushed first (Ctrl+S)"
          style={BTN_PRIMARY}
        >
          💾 Save
        </button>
      </div>

      {errorMsg && (
        <div
          style={{
            background: T.badSoft,
            color: T.bad,
            border: "1px solid #EFBDBB",
            borderRadius: 8,
            padding: "6px 10px",
            fontSize: 12.5,
            fontWeight: 600,
            marginBottom: 6,
          }}
        >
          {errorMsg}
        </div>
      )}
      {warnMsg && (
        <div
          style={{
            background: T.warnSoft,
            color: T.warn,
            border: "1px solid #F1CFA9",
            borderRadius: 8,
            padding: "6px 10px",
            fontSize: 12.5,
            fontWeight: 600,
            marginBottom: 6,
            cursor: "pointer",
          }}
          title="Click to dismiss"
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
                    ...TILE,
                    flex: 1, padding: "6px 12px",
                    display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap",
                  }}>
                    <span style={{ ...TILE_LABEL, fontSize: 12, color: T.ink2 }}>{wk}</span>
                    {ws.boardPct !== null && (
                      <span style={{ fontWeight: 800, fontSize: 16, fontVariantNumeric: "tabular-nums",
                                     color: ws.boardPct < 90 ? T.bad : T.ok }}>
                        {ws.boardPct}%<span style={{ fontSize: 10, fontWeight: 600, color: T.ink3 }}> on board</span>
                      </span>
                    )}
                    {ws.madePct !== null && ws.madePct > 0 && (
                      <span style={{ fontWeight: 700, fontSize: 12, color: T.ink2 }}>
                        + {ws.madePct}%<span style={{ fontSize: 10, fontWeight: 600, color: T.ink3 }}> already made</span>
                      </span>
                    )}
                    <span style={{ fontSize: 11.5, color: T.ink2, fontWeight: 600 }}>
                      TL {ws.tl} · FFS {ws.ffs} · CP {ws.cp} · TTP {ws.ttp}
                    </span>
                    {ws.cipReq > 0 && (
                      <span
                        title={`${ws.cipReq} transition${ws.cipReq === 1 ? "" : "s"} this week require${ws.cipReq === 1 ? "s" : ""} a CIP between the SKUs but no CIP block sits in the gap — schedule a clean or resequence`}
                        style={chipStyle("bad")}>
                        CIP req {ws.cipReq}
                      </span>
                    )}
                    {ws.cipGap && (
                      <button
                        title={`Insert a clean at the first violating transition (${ws.cipGap.lineName} at ${hourToStamp(ws.cipGap.start, anchor)}); later projected CIPs re-forecast from it`}
                        style={{ ...chipStyle("accent"), cursor: "pointer", fontFamily: FONT_SANS }}
                        onClick={() => handleWeekCipFix(ws.cipGap!)}
                      >
                        + CIP
                      </button>
                    )}
                  </div>
                ))}
            </div>
            <div
              style={{ fontSize: 11, color: T.ink3, margin: "0 0 4px 50px" }}
              title={"On board % = kg of calendar blocks vs the week's demand target (per-order credit capped at target). "
                + "'+ already made' = extra coverage from completed MOs the board hides — shown separately, never summed into the headline. "
                + "Hover a card for the kg split."}
            >
              On-board % of each week&rsquo;s demand target · TL/FFS/CP/TTP = changeovers by machine ·
              live on every edit, computed {weekStats.computedAt}
            </div>
          </>
        )}
        {/* Provider, not a prop: GanttChart owns the axis mounts (see
            TimeAxis.ReceiptDaysContext); null without a stock payload. */}
        <ReceiptDaysContext.Provider value={receiptDays}>
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
          supplyFor={stockEnabled ? supplyFor : null}
          onResizeStart={guardedStartResize}
          onContextMenu={handleContextMenu}
          onEmptyContextMenu={pickerEnabled ? handleEmptyContextMenu : undefined}
          onBlockClick={handleBlockClick}
          onZoomIn={zoomIn}
          onZoomOut={zoomOut}
          onResetZoom={resetZoom}
        />
        </ReceiptDaysContext.Provider>
        <Legend pickerEnabled={pickerEnabled} hasLock={lockedThroughH != null} />

        <div style={{ marginTop: 8 }}>
          <HoldingArea blocks={holdingArea} anchor={anchor} skuFormats={args.skuFormats ?? {}}
            highlightSku={highlightSku}
            stock={stockEnabled ? stock : null}
            supplyTimelines={supply?.timelines ?? null}
            caps={caps}
            lockedThroughH={lockedThroughH}
            nowH={holdingNowH}
            demandTargets={args.demandTargets}
            supplyStamp={supplyStamp}
            onCardRemove={(block) => actions.dismissFromHolding(block)}
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
                  border: dragPreview && !dragPreview.valid ? `2px solid ${T.bad}` : `2px solid ${T.ink}`,
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
        <div style={{ ...TILE_LABEL, fontSize: 11.5, marginBottom: 4 }}>
          Demand adherence (live)
          <span style={{ fontWeight: 500, textTransform: "none", letterSpacing: 0, color: T.ink3 }}>
            {" "}· click a row to highlight that SKU on the board · &ldquo;+&rdquo; adds the missing tonnage to holding (a card&rsquo;s &times; removes it again)
          </span>
        </div>
        <AdherenceTable
          rows={currentAdherenceRows}
          formatOrder={(id) => displayOrderId(id, anchor)}
          highlightSku={highlightSku}
          onSkuClick={setHighlightSku}
          rateFor={(sku) => meanCapableRate(caps, sku)}
          onAddToHolding={(row, missingKg, runHours) => {
            // runHours = missing kg / mean capable rate (AdherenceTable, fix
            // FE / audit ui-8); 0.5 h floor only when no line is capable.
            actions.addToHolding(row.order_id, row.sku, Math.max(0.5, runHours), missingKg);
          }}
        />
      </div>

      <ContextMenu
        menu={menu}
        onSplit={handleMenuSplit}
        onRemove={handleMenuRemove}
        onDetails={handleMenuDetails}
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
          stock={stockEnabled ? stock : null}
          supplyTimelines={supply?.timelines ?? null}
          supplyStamp={supplyStamp}
          dueEndH={args.demandTargets.find((d) => d.order_id === holdMenu.block.order_id)?.due_end_hour ?? null}
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
          stock={stockEnabled ? stock : null}
          supplyTimelines={supply?.timelines ?? null}
          supplyStamp={supplyStamp}
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
          onRemove={isBlockLocked(popover.block) ? undefined : (b) => {
            actions.removeToHolding(b);
            setPopover(null);
          }}
          onAddCip={handlePopoverAddCip}
          demandLeft={popover.block.block_type === "sku"
            ? demandLeftForSku(popover.block.sku) : undefined}
          supply={stockEnabled ? (supply?.verdicts.get(blockKey(popover.block)) ?? null) : null}
          stock={stockEnabled ? stock : null}
          supplyStamp={supplyStamp}
          acknowledged={ackedKeys.has(blockKey(popover.block))}
          onAcknowledge={stockEnabled ? acknowledgeKey : undefined}
          onOpenSupplyDetail={stockEnabled ? (b) => setSupplyDetailFor(b) : undefined}
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

      {stockEnabled && stock && supply && supplyDetailFor && (() => {
        // Resolve by piece each render: an edit may have replaced the object
        // (or removed the block — then there is nothing to detail).
        const b = findBlock(schedule, supplyDetailFor);
        const s = b ? supply.verdicts.get(blockKey(b)) : undefined;
        if (!b || !s) return null;
        return (
          <SupplyDetailPanel
            block={b}
            supply={s}
            timelines={supply.timelines}
            stock={stock}
            schedule={schedule}
            anchor={anchor}
            anchorStamp={supplyStamp}
            rules={rulesOf(stock)}
            onClose={() => setSupplyDetailFor(null)}
          />
        );
      })()}

      {lastAction && (
        <div style={{ fontSize: 11, color: lastAction.startsWith("Rejected") ? T.bad : T.ink3, marginTop: 4 }}>
          Last action: {lastAction}
        </div>
      )}
    </div>
  );
};
