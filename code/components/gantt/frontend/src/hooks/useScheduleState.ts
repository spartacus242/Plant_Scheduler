// useScheduleState.ts — Central state management for the sandbox.

import { useState, useCallback, useMemo, useRef } from "react";
import type { BlockType, ScheduleBlock, SandboxArgs } from "../types";
import { isWindowBlock } from "../types";
import { hourToStamp } from "../utils/layout";

let _nextId = 1;
function ensureId(b: ScheduleBlock): ScheduleBlock {
  if (!b.id) return { ...b, id: `blk_${_nextId++}` };
  return b;
}

export interface ScheduleStateActions {
  updateBlock: (id: string, patch: Partial<ScheduleBlock>) => void;
  moveBlock: (id: string, newLine: string, newLineId: number, newStart: number, newDuration: number) => void;
  resizeBlock: (id: string, newStart: number, newEnd: number) => void;
  splitBlock: (id: string, splitHour: number) => void;
  removeToHolding: (id: string) => void;
  restoreFromHolding: (id: string, lineName: string, lineId: number, startHour: number, duration: number) => void;
  addToHolding: (orderId: string, sku: string, runHours: number, qtyKg?: number) => void;
  addCip: (lineName: string, lineId: number, startHour: number, duration: number) => void;
  addTrial: (lineName: string, lineId: number, sku: string, startHour: number, duration: number) => void;
  addWindowBlock: (
    blockType: BlockType,
    lineName: string,
    lineId: number,
    startHour: number,
    duration: number,
    label: string,
  ) => void;
  reportAction: (msg: string) => void;
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

  const addWindowBlock = useCallback((
    blockType: BlockType,
    lineName: string,
    lineId: number,
    startHour: number,
    duration: number,
    label: string,
  ) => {
    pushUndo();
    const tag = label || blockType.toUpperCase();
    const b: ScheduleBlock = {
      id: `blk_${_nextId++}`,
      line_id: lineId,
      line_name: lineName,
      order_id: tag,
      sku: tag,
      start_hour: startHour,
      end_hour: startHour + duration,
      run_hours: duration,
      is_trial: false,
      block_type: blockType,
      label: tag,
    };
    setCipWindows((prev) => [...prev, b]);
    setLastAction(`Added ${blockType} on ${lineName} at ${stamp(startHour)}`);
  }, [pushUndo, stamp]);

  const reportAction = useCallback((msg: string) => {
    setLastAction(msg);
  }, []);

  const data: ScheduleStateData = { schedule, cipWindows, holdingArea, lastAction };
  const actions: ScheduleStateActions = {
    updateBlock, moveBlock, resizeBlock, splitBlock,
    removeToHolding, restoreFromHolding, addToHolding, addCip, addTrial, addWindowBlock,
    reportAction,
    undo, redo,
    canUndo: undoStack.current.length > 0,
    canRedo: redoStack.current.length > 0,
  };

  return [data, actions];
}
