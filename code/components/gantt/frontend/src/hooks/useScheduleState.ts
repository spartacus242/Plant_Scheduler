// useScheduleState.ts — Central state management for the sandbox.

import { useState, useCallback, useMemo, useRef } from "react";
import { sameAutoCards } from "../utils/holdingDerive";
import type { ScheduleBlock, SandboxArgs } from "../types";
import { isWindowBlock } from "../types";
import { hourToStamp } from "../utils/layout";

let _nextId = 1;
function ensureId(b: ScheduleBlock): ScheduleBlock {
  if (!b.id) return { ...b, id: `blk_${_nextId++}` };
  return b;
}

export interface ScheduleStateActions {
  updateBlock: (id: string, patch: Partial<ScheduleBlock>) => void;
  /** Insert-between: move `id` to [newStart, newStart+dur] on `lineName` and
   * shift every listed block right by `deltaH` - ONE undo step. */
  insertShift: (
    id: string, lineName: string, lineId: number, newStart: number, dur: number,
    shiftIds: string[], deltaH: number,
  ) => void;
  moveBlock: (id: string, newLine: string, newLineId: number, newStart: number, newDuration: number) => void;
  resizeBlock: (id: string, newStart: number, newEnd: number) => void;
  splitBlock: (id: string, splitHour: number) => void;
  removeToHolding: (id: string) => void;
  restoreFromHolding: (id: string, lineName: string, lineId: number, startHour: number, duration: number) => void;
  /** Resize-to-fit restore (user rule 2026-09-01): place only placedH of the
   * card (its full on-line duration would be fullDurOnLine) and keep the
   * remainder in holding, kg apportioned by hour share so card + block
   * always sum back to the original tonnage. */
  restorePartialFromHolding: (
    id: string, lineName: string, lineId: number, startHour: number,
    placedH: number, fullDurOnLine: number,
  ) => void;
  addToHolding: (orderId: string, sku: string, runHours: number, qtyKg?: number) => void;
  /** Blank-space SKU picker: place a NEW production run as contiguous
   * per-order segments (ONE undo step). Order ids must be real demand orders
   * so the kg credits adherence and the next holding rebuild. */
  addProduction: (
    lineName: string, lineId: number, sku: string, skuDescription: string,
    segments: { orderId: string; startHour: number; durationH: number; qtyKg: number }[],
  ) => void;
  addCip: (lineName: string, lineId: number, startHour: number, duration: number) => void;
  addTrial: (lineName: string, lineId: number, sku: string, startHour: number, duration: number) => void;
  /** Planner CIP + re-forecast (2026-09-01): one undo step that (a) slides
   * shiftIds right by shiftH to open the gap, (b) places the clean, and
   * (c) regenerates the line's LATER projected cleans start-to-start every
   * intervalH from it (mirrors current_state.project_cips), snapping a
   * slot that lands inside a block to that block's end and skipping slots
   * within 1h of a surviving CIP. Regenerated cleans carry
   * attrs planner:cip_projected — editable forecasts, staged as committed
   * windows by the solver. */
  addCipReforecast: (opts: {
    lineName: string; lineId: number; startHour: number; duration: number;
    intervalH: number; horizonH: number; shiftIds: string[]; shiftH: number;
  }) => void;
  reportAction: (msg: string) => void;
  /** Adopt the SERVER-derived holding area (rebuilt after every Refresh
   * push). Not a user edit: no undo entry — the caller keeps the dirty
   * flag honest. */
  setHoldingFromServer: (blocks: ScheduleBlock[]) => void;
  /** Replace the AUTO (hold_*) cards with a freshly derived set; planner-
   * parked cards keep their place. No undo step: derived state, not an
   * edit. Returns prev unchanged when nothing differs (render-loop guard). */
  replaceAutoHolding: (cards: ScheduleBlock[]) => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
}

export interface ScheduleStateData {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  holdingArea: ScheduleBlock[];
  lastAction: string;
}

interface Snapshot {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  holdingArea: ScheduleBlock[];
}

export function useScheduleState(args: SandboxArgs | null): [ScheduleStateData, ScheduleStateActions] {
  const [schedule, setSchedule] = useState<ScheduleBlock[]>(() =>
    (args?.schedule ?? []).map(ensureId),
  );
  const [cipWindows, setCipWindows] = useState<ScheduleBlock[]>(() =>
    (args?.cipWindows ?? []).map(ensureId),
  );
  const [holdingArea, setHoldingArea] = useState<ScheduleBlock[]>(() =>
    (args?.holdingArea ?? []).map(ensureId),
  );
  const [lastAction, setLastAction] = useState("");

  // Action messages report wall-clock moments, not raw horizon offsets.
  const anchor = useMemo(
    () => new Date(args?.config?.planning_anchor ?? "2026-02-15 00:00:00"),
    [args?.config?.planning_anchor],
  );
  const stamp = useCallback((h: number) => hourToStamp(h, anchor), [anchor]);

  // Undo/redo stacks
  const undoStack = useRef<Snapshot[]>([]);
  const redoStack = useRef<Snapshot[]>([]);

  const snapshot = useCallback((): Snapshot => ({
    schedule: [...schedule],
    cipWindows: [...cipWindows],
    holdingArea: [...holdingArea],
  }), [schedule, cipWindows, holdingArea]);

  const pushUndo = useCallback(() => {
    undoStack.current.push(snapshot());
    redoStack.current = [];
    if (undoStack.current.length > 50) undoStack.current.shift();
  }, [snapshot]);

  const undo = useCallback(() => {
    const snap = undoStack.current.pop();
    if (!snap) return;
    redoStack.current.push(snapshot());
    setSchedule(snap.schedule);
    setCipWindows(snap.cipWindows);
    setHoldingArea(snap.holdingArea);
    setLastAction("Undo");
  }, [snapshot]);

  const redo = useCallback(() => {
    const snap = redoStack.current.pop();
    if (!snap) return;
    undoStack.current.push(snapshot());
    setSchedule(snap.schedule);
    setCipWindows(snap.cipWindows);
    setHoldingArea(snap.holdingArea);
    setLastAction("Redo");
  }, [snapshot]);

  const updateBlock = useCallback((id: string, patch: Partial<ScheduleBlock>) => {
    pushUndo();
    setSchedule((prev) => prev.map((b) => (b.id === id ? { ...b, ...patch } : b)));
    setCipWindows((prev) => prev.map((b) => (b.id === id ? { ...b, ...patch } : b)));
  }, [pushUndo]);

  const insertShift = useCallback((
    id: string, lineName: string, lineId: number, newStart: number, dur: number,
    shiftIds: string[], deltaH: number,
  ) => {
    pushUndo();
    const shift = new Set(shiftIds);
    const apply = (b: ScheduleBlock): ScheduleBlock => {
      if (b.id === id) {
        return { ...b, line_name: lineName, line_id: lineId,
                 start_hour: newStart, end_hour: newStart + dur, run_hours: dur };
      }
      if (shift.has(b.id) && deltaH > 0) {
        return { ...b, start_hour: b.start_hour + deltaH, end_hour: b.end_hour + deltaH };
      }
      return b;
    };
    setSchedule((prev) => prev.map(apply));
    setCipWindows((prev) => prev.map(apply));
    setLastAction(
      `Inserted at ${stamp(newStart)} - ${shiftIds.length} block(s) slid ${deltaH.toFixed(1)}h right`,
    );
  }, [pushUndo, stamp]);

  const moveBlock = useCallback((id: string, newLine: string, newLineId: number, newStart: number, newDuration: number) => {
    pushUndo();
    setSchedule((prev) =>
      prev.map((b) =>
        b.id === id
          ? { ...b, line_name: newLine, line_id: newLineId, start_hour: newStart, end_hour: newStart + newDuration, run_hours: newDuration }
          : b,
      ),
    );
    setCipWindows((prev) =>
      prev.map((b) =>
        b.id === id
          ? { ...b, line_name: newLine, line_id: newLineId, start_hour: newStart, end_hour: newStart + newDuration, run_hours: newDuration }
          : b,
      ),
    );
    setLastAction(`Moved ${id} to ${newLine} at ${stamp(newStart)}`);
  }, [pushUndo, stamp]);

  const resizeBlock = useCallback((id: string, newStart: number, newEnd: number) => {
    pushUndo();
    const dur = newEnd - newStart;
    // Produced kg follows the duration the user sets: scale proportionally so
    // a resized run stays honest (rate x hours), never a stale carry-over.
    // Unknown kg (null/undefined/0) stays unknown - never invent a number.
    const scaledKg = (b: ScheduleBlock): number | undefined => {
      const oldDur = b.end_hour - b.start_hour;
      if (!b.qty_kg || oldDur <= 0) return b.qty_kg;
      return Math.round(((b.qty_kg * dur) / oldDur) * 10) / 10;
    };
    setSchedule((prev) =>
      prev.map((b) => (b.id === id ? { ...b, start_hour: newStart, end_hour: newEnd, run_hours: dur, qty_kg: scaledKg(b) } : b)),
    );
    setCipWindows((prev) =>
      prev.map((b) => (b.id === id ? { ...b, start_hour: newStart, end_hour: newEnd, run_hours: dur } : b)),
    );
    setLastAction(`Resized ${id} to ${stamp(newStart)} - ${stamp(newEnd)} (${dur}h)`);
  }, [pushUndo, stamp]);

  const splitBlock = useCallback((id: string, splitHour: number) => {
    pushUndo();
    setSchedule((prev) => {
      const idx = prev.findIndex((b) => b.id === id);
      if (idx < 0) return prev;
      const b = prev[idx];
      // Apportion produced kg by duration share - a plain spread would give
      // BOTH segments the full kg and double-count production. Segment B takes
      // the exact remainder so the two always sum back to the original.
      // Unknown kg (null/undefined/0) stays unknown on both segments.
      const total = b.end_hour - b.start_hour;
      const fracA = total > 0 ? (splitHour - b.start_hour) / total : 0.5;
      const kgA = b.qty_kg ? Math.round(b.qty_kg * fracA * 10) / 10 : b.qty_kg;
      const kgB = b.qty_kg ? Math.round((b.qty_kg - (kgA as number)) * 10) / 10 : b.qty_kg;
      const segA: ScheduleBlock = { ...b, id: `blk_${_nextId++}`, end_hour: splitHour, run_hours: splitHour - b.start_hour, qty_kg: kgA };
      const segB: ScheduleBlock = { ...b, id: `blk_${_nextId++}`, start_hour: splitHour, run_hours: b.end_hour - splitHour, qty_kg: kgB };
      const next = [...prev];
      next.splice(idx, 1, segA, segB);
      return next;
    });
    setLastAction(`Split block at ${stamp(splitHour)}`);
  }, [pushUndo, stamp]);

  const removeToHolding = useCallback((id: string) => {
    // Find the block FIRST from current state before queueing updates
    const found = schedule.find((b) => b.id === id) ?? cipWindows.find((b) => b.id === id);
    if (!found) return;
    pushUndo();
    setSchedule((prev) => prev.filter((b) => b.id !== id));
    setCipWindows((prev) => prev.filter((b) => b.id !== id));
    setHoldingArea((prev) => [...prev, found]);
    setLastAction(`Removed ${found.order_id} to holding`);
  }, [pushUndo, schedule, cipWindows]);

  const restorePartialFromHolding = useCallback((
    id: string, lineName: string, lineId: number, startHour: number,
    placedH: number, fullDurOnLine: number,
  ) => {
    const found = holdingArea.find((b) => b.id === id);
    if (!found || placedH <= 0 || fullDurOnLine <= 0 || placedH >= fullDurOnLine) return;
    pushUndo();
    // splitBlock's apportioning rule: the placed segment takes its hour
    // share of the card's kg, the card keeps the EXACT remainder. Shares
    // are in TARGET-line hours (fullDurOnLine) — the card's own run_hours
    // may be priced at a different line's rate.
    const frac = placedH / fullDurOnLine;
    const kgPlaced = found.qty_kg ? Math.round(found.qty_kg * frac * 10) / 10 : found.qty_kg;
    const kgRest = found.qty_kg ? Math.round((found.qty_kg - (kgPlaced as number)) * 10) / 10 : found.qty_kg;
    const remCardH = Math.max(0.1, Math.round(found.run_hours * (1 - frac) * 10) / 10);
    const placed: ScheduleBlock = {
      ...found,
      id: `blk_${_nextId++}`,
      line_name: lineName,
      line_id: lineId,
      start_hour: startHour,
      end_hour: startHour + placedH,
      run_hours: placedH,
      qty_kg: kgPlaced,
    };
    setSchedule((prev) => [...prev, placed]);
    setHoldingArea((prev) => prev.map((b) => b.id === id
      ? { ...b, start_hour: 0, end_hour: remCardH, run_hours: remCardH, qty_kg: kgRest }
      : b));
    setLastAction(
      `Placed ${placedH.toFixed(1)}h of ${found.order_id} on ${lineName} — `
      + `${remCardH.toFixed(1)}h stays in holding`);
  }, [pushUndo, holdingArea]);

  const restoreFromHolding = useCallback((id: string, lineName: string, lineId: number, startHour: number, duration: number) => {
    const found = holdingArea.find((b) => b.id === id);
    if (!found) return;
    pushUndo();
    const b: ScheduleBlock = {
      ...found,
      line_name: lineName,
      line_id: lineId,
      start_hour: startHour,
      end_hour: startHour + duration,
      run_hours: duration,
    };
    setHoldingArea((prev) => prev.filter((bl) => bl.id !== id));
    if (isWindowBlock(b.block_type)) {
      setCipWindows((prev) => [...prev, b]);
    } else {
      setSchedule((prev) => [...prev, b]);
    }
    setLastAction(`Restored ${found.order_id} to ${lineName}`);
  }, [pushUndo, holdingArea]);

  const addToHolding = useCallback((orderId: string, sku: string, runHours: number, qtyKg?: number) => {
    pushUndo();
    const b: ScheduleBlock = {
      id: `hold_${orderId}`,
      line_id: 0,
      line_name: "",
      order_id: orderId,
      sku,
      start_hour: 0,
      end_hour: runHours,
      run_hours: runHours,
      is_trial: false,
      block_type: "sku",
      label: `${orderId} (${runHours.toFixed(1)}h)`,
      qty_kg: qtyKg ?? 0,
    };
    setHoldingArea((prev) => [...prev, b]);
    setLastAction(`Added ${orderId} to holding (${runHours.toFixed(1)}h)`);
  }, [pushUndo]);

  const addProduction = useCallback((
    lineName: string, lineId: number, sku: string, skuDescription: string,
    segments: { orderId: string; startHour: number; durationH: number; qtyKg: number }[],
  ) => {
    if (!segments.length) return;
    pushUndo();
    const blocks: ScheduleBlock[] = segments.map((s) => ({
      id: `blk_${_nextId++}`,
      line_id: lineId,
      line_name: lineName,
      order_id: s.orderId,
      sku,
      sku_description: skuDescription,
      start_hour: s.startHour,
      end_hour: s.startHour + s.durationH,
      run_hours: s.durationH,
      is_trial: false,
      block_type: "sku",
      qty_kg: s.qtyKg > 0 ? s.qtyKg : undefined,
    }));
    setSchedule((prev) => [...prev, ...blocks]);
    const totalH = segments.reduce((t, s) => t + s.durationH, 0);
    const totalKg = segments.reduce((t, s) => t + s.qtyKg, 0);
    setLastAction(
      `Placed ${sku} on ${lineName} at ${stamp(segments[0].startHour)} ` +
        `(${totalH.toFixed(1)}h, ${Math.round(totalKg).toLocaleString()} kg ` +
        `across ${segments.length} order(s))`,
    );
  }, [pushUndo, stamp]);

  const addCip = useCallback((lineName: string, lineId: number, startHour: number, duration: number) => {
    pushUndo();
    const b: ScheduleBlock = {
      id: `blk_${_nextId++}`,
      line_id: lineId,
      line_name: lineName,
      order_id: "CIP",
      sku: "CIP",
      start_hour: startHour,
      end_hour: startHour + duration,
      run_hours: duration,
      is_trial: false,
      block_type: "cip",
      label: "CIP",
    };
    setCipWindows((prev) => [...prev, b]);
    setLastAction(`Added CIP on ${lineName} at ${stamp(startHour)}`);
  }, [pushUndo, stamp]);

  const addTrial = useCallback((lineName: string, lineId: number, sku: string, startHour: number, duration: number) => {
    pushUndo();
    const b: ScheduleBlock = {
      id: `blk_${_nextId++}`,
      line_id: lineId,
      line_name: lineName,
      order_id: `TRIAL-${sku}-L${lineName}`,
      sku,
      start_hour: startHour,
      end_hour: startHour + duration,
      run_hours: duration,
      is_trial: true,
      block_type: "trial",
    };
    setSchedule((prev) => [...prev, b]);
    setLastAction(`Added trial ${sku} on ${lineName}`);
  }, [pushUndo]);

  const addCipReforecast = useCallback((opts: {
    lineName: string; lineId: number; startHour: number; duration: number;
    intervalH: number; horizonH: number; shiftIds: string[]; shiftH: number;
  }) => {
    const { lineName, lineId, startHour, duration, intervalH, horizonH, shiftIds, shiftH } = opts;
    pushUndo();
    const shift = new Set(shiftIds);
    const slide = (b: ScheduleBlock): ScheduleBlock =>
      shift.has(b.id) && shiftH > 0
        ? { ...b, start_hour: b.start_hour + shiftH, end_hour: b.end_hour + shiftH }
        : b;
    const isProjectedHere = (b: ScheduleBlock) =>
      b.block_type === "cip" && b.line_name === lineName &&
      (b.attrs ?? "").includes("cip_projected") && b.start_hour > startHour + 1e-6;
    const sched2 = schedule.map(slide);
    const wins2 = cipWindows.map(slide).filter((b) => !isProjectedHere(b));
    const cip: ScheduleBlock = {
      id: `blk_${_nextId++}`, line_id: lineId, line_name: lineName,
      order_id: "CIP", sku: "CIP", start_hour: startHour, end_hour: startHour + duration,
      run_hours: duration, is_trial: false, block_type: "cip", label: "CIP",
      attrs: "planner:cip",
    };
    const onLine = [...sched2, ...wins2]
      .filter((b) => b.line_name === lineName && !String(b.id).startsWith("cipinfo_"))
      .sort((a, b) => a.start_hour - b.start_hour);
    const added: ScheduleBlock[] = [cip];
    let t = startHour + intervalH;
    let guard = 0;
    while (t < horizonH && guard++ < 60) {
      const inside = onLine.find((b) => b.start_hour < t - 1e-6 && b.end_hour > t + 1e-6);
      const s0 = inside ? inside.end_hour : t;
      const near = [...wins2, ...added].some(
        (c) => c.block_type === "cip" && c.line_name === lineName && Math.abs(c.start_hour - s0) < 1.0);
      if (!near && s0 < horizonH) {
        const e0 = Math.min(horizonH, s0 + duration);
        added.push({ ...cip, id: `blk_${_nextId++}`, start_hour: s0, end_hour: e0,
                     run_hours: e0 - s0, label: "CIP (projected)", attrs: "planner:cip_projected" });
      }
      t = s0 + intervalH;
    }
    setSchedule(sched2);
    setCipWindows([...wins2, ...added]);
    setLastAction(
      `Added CIP on ${lineName} at ${stamp(startHour)}`
      + (shiftH > 0 ? ` (${shiftIds.length} block(s) slid ${shiftH.toFixed(1)}h)` : "")
      + `; ${added.length - 1} later clean(s) re-forecast`);
  }, [pushUndo, stamp, schedule, cipWindows]);

  const reportAction = useCallback((msg: string) => {
    setLastAction(msg);
  }, []);

  const replaceAutoHolding = useCallback((cards: ScheduleBlock[]) => {
    setHoldingArea((prev) => {
      const isAuto = (b: ScheduleBlock) => String(b.id).startsWith("hold_");
      const cur = prev.filter(isAuto);
      if (sameAutoCards(cur, cards)) return prev;
      return [...prev.filter((b) => !isAuto(b)), ...cards];
    });
  }, []);

  const setHoldingFromServer = useCallback((blocks: ScheduleBlock[]) => {
    setHoldingArea(blocks.map(ensureId));
  }, []);

  const data: ScheduleStateData = { schedule, cipWindows, holdingArea, lastAction };
  const actions: ScheduleStateActions = {
    updateBlock, insertShift, moveBlock, resizeBlock, splitBlock,
    removeToHolding, restoreFromHolding, restorePartialFromHolding,
    addToHolding, addProduction, addCipReforecast,
    addCip, addTrial, reportAction, setHoldingFromServer, replaceAutoHolding,
    undo, redo,
    canUndo: undoStack.current.length > 0,
    canRedo: redoStack.current.length > 0,
  };

  return [data, actions];
}
