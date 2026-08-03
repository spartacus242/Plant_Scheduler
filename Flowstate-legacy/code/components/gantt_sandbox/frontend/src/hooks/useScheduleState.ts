// useScheduleState.ts — Central state management for the sandbox.

import { useState, useCallback, useRef } from "react";
import type { ScheduleBlock, SandboxArgs } from "../types";

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
  addCip: (lineName: string, lineId: number, startHour: number, duration: number) => void;
  addTrial: (lineName: string, lineId: number, sku: string, startHour: number, duration: number) => void;
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
    setLastAction(`Moved ${id} to ${newLine} at h${newStart}`);
  }, [pushUndo]);

  const resizeBlock = useCallback((id: string, newStart: number, newEnd: number) => {
    pushUndo();
    const dur = newEnd - newStart;
    setSchedule((prev) =>
      prev.map((b) => (b.id === id ? { ...b, start_hour: newStart, end_hour: newEnd, run_hours: dur } : b)),
    );
    setCipWindows((prev) =>
      prev.map((b) => (b.id === id ? { ...b, start_hour: newStart, end_hour: newEnd, run_hours: dur } : b)),
    );
    setLastAction(`Resized ${id} to h${newStart}-${newEnd}`);
  }, [pushUndo]);

  const splitBlock = useCallback((id: string, splitHour: number) => {
    pushUndo();
    setSchedule((prev) => {
      const idx = prev.findIndex((b) => b.id === id);
      if (idx < 0) return prev;
      const b = prev[idx];
      const segA: ScheduleBlock = { ...b, id: `blk_${_nextId++}`, end_hour: splitHour, run_hours: splitHour - b.start_hour };
      const segB: ScheduleBlock = { ...b, id: `blk_${_nextId++}`, start_hour: splitHour, run_hours: b.end_hour - splitHour };
      const next = [...prev];
      next.splice(idx, 1, segA, segB);
      return next;
    });
    setLastAction(`Split block at h${splitHour}`);
  }, [pushUndo]);

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
    if (b.block_type === "cip") {
      setCipWindows((prev) => [...prev, b]);
    } else {
      setSchedule((prev) => [...prev, b]);
    }
    setLastAction(`Restored ${found.order_id} to ${lineName}`);
  }, [pushUndo, holdingArea]);

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
    };
    setCipWindows((prev) => [...prev, b]);
    setLastAction(`Added CIP on ${lineName} at h${startHour}`);
  }, [pushUndo]);

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

  const data: ScheduleStateData = { schedule, cipWindows, holdingArea, lastAction };
  const actions: ScheduleStateActions = {
    updateBlock, moveBlock, resizeBlock, splitBlock,
    removeToHolding, restoreFromHolding, addCip, addTrial,
    undo, redo,
    canUndo: undoStack.current.length > 0,
    canRedo: redoStack.current.length > 0,
  };

  return [data, actions];
}
