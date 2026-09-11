// useScheduleState.ts — Central state management for the sandbox.

import { useState, useCallback, useMemo, useRef } from "react";
import { sameAutoCards } from "../utils/holdingDerive";
import {
  findBlock, findBlockIndex, mintBlockId, patchOne, refId, removeOne, resolveIndices, type BlockRef,
} from "../utils/blockIdentity";
import { reforecastCips } from "../utils/cipReforecast";
import type { ScheduleBlock, SandboxArgs } from "../types";
import { isWindowBlock } from "../types";
import { hourToStamp } from "../utils/layout";

// Ids are minted collision-free (utils/blockIdentity.mintBlockId: time +
// random, never one already in `taken`). The old module-scope `_nextId`
// counter restarted at 1 on every mount and was never seeded from the
// board, so a reload re-minted blk_1, blk_2 ... and calendar_blocks.csv
// carried two unrelated rows per id (fix FE / audit writeback-8).
function ensureId(b: ScheduleBlock): ScheduleBlock {
  if (!b.id) return { ...b, id: mintBlockId("blk") };
  return b;
}

function idsOf(...lists: readonly ScheduleBlock[][]): Set<string> {
  const s = new Set<string>();
  for (const l of lists) for (const b of l) s.add(String(b.id));
  return s;
}

export type { BlockRef } from "../utils/blockIdentity";

/** Every edit targets ONE block, named by a `BlockRef`: the state object
 * (identity), an {id, start_hour} pair, or — legacy — a bare id, which
 * resolves to the FIRST piece carrying it. Split MO pieces share an id
 * (calendar_blocks.csv `;split`), so a bare id is ambiguous for them and
 * callers that hold the block should pass it (utils/blockIdentity). */
export interface ScheduleStateActions {
  updateBlock: (target: BlockRef, patch: Partial<ScheduleBlock>) => void;
  /** Insert-between: move `target` to [newStart, newStart+dur] on `lineName`
   * and shift each block in `shiftIds` right by `deltaH` - ONE undo step.
   * Every ref shifts exactly one piece (bare id = first piece). */
  insertShift: (
    target: BlockRef, lineName: string, lineId: number, newStart: number, dur: number,
    shiftIds: BlockRef[], deltaH: number,
  ) => void;
  moveBlock: (target: BlockRef, newLine: string, newLineId: number, newStart: number, newDuration: number) => void;
  /** Bare id shared by several pieces: the piece keeping one edge fixed
   * (start == newStart or end == newEnd) is the one resized. */
  resizeBlock: (target: BlockRef, newStart: number, newEnd: number) => void;
  splitBlock: (target: BlockRef, splitHour: number) => void;
  removeToHolding: (target: BlockRef) => void;
  /** `qtyKg` (optional): the kg the placed block may claim — the caller
   * prices it on the TARGET line over the placed window
   * (validation.placedQtyKg); absent = keep the card's kg. */
  restoreFromHolding: (
    target: BlockRef, lineName: string, lineId: number, startHour: number, duration: number,
    qtyKg?: number,
  ) => void;
  /** Resize-to-fit restore (user rule 2026-09-01): place only placedH of the
   * card (its full on-line duration would be fullDurOnLine) and keep the
   * remainder in holding, kg apportioned by hour share so card + block
   * always sum back to the original tonnage. */
  restorePartialFromHolding: (
    target: BlockRef, lineName: string, lineId: number, startHour: number,
    placedH: number, fullDurOnLine: number,
  ) => void;
  addToHolding: (orderId: string, sku: string, runHours: number, qtyKg?: number) => void;
  /** The card's "×" (planner request 2026-09-11): drop the card and remember
   * its order as dismissed, so neither the live auto-derivation nor the
   * server rebuild brings it straight back. One undo step. addToHolding
   * (the adherence table's "+") un-dismisses the order. */
  dismissFromHolding: (target: BlockRef) => void;
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
   * windows by the solver. shiftIds are BlockRefs (one piece each). */
  addCipReforecast: (opts: {
    lineName: string; lineId: number; startHour: number; duration: number;
    intervalH: number; horizonH: number; shiftIds: BlockRef[]; shiftH: number;
    /** Blocks the planner may not move: a projected slot that would have
     * to split/push one is skipped (utils/cipReforecast). */
    immovable?: (b: ScheduleBlock) => boolean;
  }) => void;
  reportAction: (msg: string) => void;
  /** Adopt the SERVER-derived holding area (rebuilt after every Refresh
   * push). Not a user edit: no undo entry — the caller keeps the dirty
   * flag honest. */
  setHoldingFromServer: (blocks: ScheduleBlock[], dismissed?: string[]) => void;
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
  holdingDismissed: string[];
  lastAction: string;
}

interface Snapshot {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  holdingArea: ScheduleBlock[];
  holdingDismissed: string[];
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
  const [holdingDismissed, setHoldingDismissed] = useState<string[]>(() =>
    (args?.holdingDismissed ?? []).map(String),
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
    holdingDismissed: [...holdingDismissed],
  }), [schedule, cipWindows, holdingArea, holdingDismissed]);

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
    setHoldingDismissed(snap.holdingDismissed);
    setLastAction("Undo");
  }, [snapshot]);

  const redo = useCallback(() => {
    const snap = redoStack.current.pop();
    if (!snap) return;
    undoStack.current.push(snapshot());
    setSchedule(snap.schedule);
    setCipWindows(snap.cipWindows);
    setHoldingArea(snap.holdingArea);
    setHoldingDismissed(snap.holdingDismissed);
    setLastAction("Redo");
  }, [snapshot]);

  // Every mutation below resolves its target to ONE index per list
  // (utils/blockIdentity) — never "every block with this id", which hit
  // both pieces of a split MO.
  const updateBlock = useCallback((target: BlockRef, patch: Partial<ScheduleBlock>) => {
    pushUndo();
    setSchedule((prev) => patchOne(prev, target, patch));
    setCipWindows((prev) => patchOne(prev, target, patch));
  }, [pushUndo]);

  const insertShift = useCallback((
    target: BlockRef, lineName: string, lineId: number, newStart: number, dur: number,
    shiftIds: BlockRef[], deltaH: number,
  ) => {
    pushUndo();
    const apply = (list: ScheduleBlock[]): ScheduleBlock[] => {
      const moved = findBlockIndex(list, target);
      const shift = resolveIndices(list, shiftIds);
      if (moved < 0 && shift.size === 0) return list;
      return list.map((b, i) => {
        if (i === moved) {
          return { ...b, line_name: lineName, line_id: lineId,
                   start_hour: newStart, end_hour: newStart + dur, run_hours: dur };
        }
        if (shift.has(i) && deltaH > 0) {
          return { ...b, start_hour: b.start_hour + deltaH, end_hour: b.end_hour + deltaH };
        }
        return b;
      });
    };
    setSchedule(apply);
    setCipWindows(apply);
    setLastAction(
      `Inserted at ${stamp(newStart)} - ${shiftIds.length} block(s) slid ${deltaH.toFixed(1)}h right`,
    );
  }, [pushUndo, stamp]);

  const moveBlock = useCallback((target: BlockRef, newLine: string, newLineId: number, newStart: number, newDuration: number) => {
    pushUndo();
    const moved: Partial<ScheduleBlock> = {
      line_name: newLine, line_id: newLineId,
      start_hour: newStart, end_hour: newStart + newDuration, run_hours: newDuration,
    };
    setSchedule((prev) => patchOne(prev, target, moved));
    setCipWindows((prev) => patchOne(prev, target, moved));
    setLastAction(`Moved ${refId(target)} to ${newLine} at ${stamp(newStart)}`);
  }, [pushUndo, stamp]);

  const resizeBlock = useCallback((target: BlockRef, newStart: number, newEnd: number) => {
    pushUndo();
    const dur = newEnd - newStart;
    // A resize keeps one edge: that edge picks the piece when a bare id is
    // shared (useBlockResize commits by id only).
    const edges: [number, number] = [newStart, newEnd];
    // Produced kg follows the duration the user sets: scale proportionally so
    // a resized run stays honest (rate x hours), never a stale carry-over.
    // Unknown kg (null/undefined/0) stays unknown - never invent a number.
    const scaledKg = (b: ScheduleBlock): number | undefined => {
      const oldDur = b.end_hour - b.start_hour;
      if (!b.qty_kg || oldDur <= 0) return b.qty_kg;
      return Math.round(((b.qty_kg * dur) / oldDur) * 10) / 10;
    };
    setSchedule((prev) => patchOne(prev, target,
      (b) => ({ ...b, start_hour: newStart, end_hour: newEnd, run_hours: dur, qty_kg: scaledKg(b) }),
      { edges }));
    setCipWindows((prev) => patchOne(prev, target,
      { start_hour: newStart, end_hour: newEnd, run_hours: dur }, { edges }));
    setLastAction(`Resized ${refId(target)} to ${stamp(newStart)} - ${stamp(newEnd)} (${dur}h)`);
  }, [pushUndo, stamp]);

  const splitBlock = useCallback((target: BlockRef, splitHour: number) => {
    pushUndo();
    setSchedule((prev) => {
      const idx = findBlockIndex(prev, target);
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
      const taken = idsOf(prev);
      const idA = mintBlockId("blk", taken);
      taken.add(idA);
      const segA: ScheduleBlock = { ...b, id: idA, end_hour: splitHour, run_hours: splitHour - b.start_hour, qty_kg: kgA };
      const segB: ScheduleBlock = { ...b, id: mintBlockId("blk", taken), start_hour: splitHour, run_hours: b.end_hour - splitHour, qty_kg: kgB };
      const next = [...prev];
      next.splice(idx, 1, segA, segB);
      return next;
    });
    setLastAction(`Split block at ${stamp(splitHour)}`);
  }, [pushUndo, stamp]);

  const removeToHolding = useCallback((target: BlockRef) => {
    // Find the block FIRST from current state before queueing updates, then
    // remove exactly that piece: the old id filter dropped a split MO's
    // sibling pieces from the board without parking them.
    const found = findBlock(schedule, target) ?? findBlock(cipWindows, target);
    if (!found) return;
    pushUndo();
    setSchedule((prev) => removeOne(prev, found));
    setCipWindows((prev) => removeOne(prev, found));
    setHoldingArea((prev) => [...prev, found]);
    setLastAction(`Removed ${found.order_id} to holding`);
  }, [pushUndo, schedule, cipWindows]);

  const restorePartialFromHolding = useCallback((
    target: BlockRef, lineName: string, lineId: number, startHour: number,
    placedH: number, fullDurOnLine: number,
  ) => {
    const found = findBlock(holdingArea, target);
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
      id: mintBlockId("blk", idsOf(schedule, cipWindows, holdingArea)),
      line_name: lineName,
      line_id: lineId,
      start_hour: startHour,
      end_hour: startHour + placedH,
      run_hours: placedH,
      qty_kg: kgPlaced,
    };
    setSchedule((prev) => [...prev, placed]);
    setHoldingArea((prev) => patchOne(prev, found,
      { start_hour: 0, end_hour: remCardH, run_hours: remCardH, qty_kg: kgRest }));
    setLastAction(
      `Placed ${placedH.toFixed(1)}h of ${found.order_id} on ${lineName} — `
      + `${remCardH.toFixed(1)}h stays in holding`);
  }, [pushUndo, holdingArea, schedule, cipWindows]);

  const restoreFromHolding = useCallback((
    target: BlockRef, lineName: string, lineId: number, startHour: number, duration: number,
    qtyKg?: number,
  ) => {
    const found = findBlock(holdingArea, target);
    if (!found) return;
    pushUndo();
    // The kg follows the placement (fix FE / audit ui-14): the caller
    // prices the card on the target line over the placed window; the
    // card's own kg is only kept when no price was given.
    const b: ScheduleBlock = {
      ...found,
      line_name: lineName,
      line_id: lineId,
      start_hour: startHour,
      end_hour: startHour + duration,
      run_hours: duration,
      ...(qtyKg !== undefined ? { qty_kg: qtyKg } : {}),
    };
    setHoldingArea((prev) => removeOne(prev, found));
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
    // "+" is the way back for a dismissed order (the card's "×").
    setHoldingDismissed((prev) => (prev.includes(orderId) ? prev.filter((o) => o !== orderId) : prev));
    setLastAction(`Added ${orderId} to holding (${runHours.toFixed(1)}h)`);
  }, [pushUndo]);

  const dismissFromHolding = useCallback((target: BlockRef) => {
    const found = findBlock(holdingArea, target);
    if (!found) return;
    pushUndo();
    setHoldingArea((prev) => removeOne(prev, found));
    const oid = String(found.order_id ?? "").trim();
    if (oid) setHoldingDismissed((prev) => (prev.includes(oid) ? prev : [...prev, oid]));
    setLastAction(`Removed ${found.order_id} from holding`);
  }, [pushUndo, holdingArea]);

  const addProduction = useCallback((
    lineName: string, lineId: number, sku: string, skuDescription: string,
    segments: { orderId: string; startHour: number; durationH: number; qtyKg: number }[],
  ) => {
    if (!segments.length) return;
    pushUndo();
    const taken = idsOf(schedule, cipWindows, holdingArea);
    const blocks: ScheduleBlock[] = segments.map((s) => ({
      id: (() => { const id = mintBlockId("blk", taken); taken.add(id); return id; })(),
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
  }, [pushUndo, stamp, schedule, cipWindows, holdingArea]);

  const addCip = useCallback((lineName: string, lineId: number, startHour: number, duration: number) => {
    pushUndo();
    const b: ScheduleBlock = {
      id: mintBlockId("blk", idsOf(schedule, cipWindows, holdingArea)),
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
  }, [pushUndo, stamp, schedule, cipWindows, holdingArea]);

  const addTrial = useCallback((lineName: string, lineId: number, sku: string, startHour: number, duration: number) => {
    pushUndo();
    const b: ScheduleBlock = {
      id: mintBlockId("blk", idsOf(schedule, cipWindows, holdingArea)),
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
  }, [pushUndo, schedule, cipWindows, holdingArea]);

  const addCipReforecast = useCallback((opts: {
    lineName: string; lineId: number; startHour: number; duration: number;
    intervalH: number; horizonH: number; shiftIds: BlockRef[]; shiftH: number;
    immovable?: (b: ScheduleBlock) => boolean;
  }) => {
    const { lineName, lineId, startHour, duration, intervalH, horizonH, shiftIds, shiftH } = opts;
    pushUndo();
    const slide = (b: ScheduleBlock, on: boolean): ScheduleBlock =>
      on && shiftH > 0
        ? { ...b, start_hour: b.start_hour + shiftH, end_hour: b.end_hour + shiftH }
        : b;
    const shiftS = resolveIndices(schedule, shiftIds);
    const shiftW = resolveIndices(cipWindows, shiftIds);
    const sched2 = schedule.map((b, i) => slide(b, shiftS.has(i)));
    const wins2 = cipWindows.map((b, i) => slide(b, shiftW.has(i)));
    // The clean + the line's later cleans on current_state.project_cips'
    // wall-clock grid, production split around them and pushed like
    // _clip_prod_around_cips (utils/cipReforecast — fix FE / audit cip-14).
    const taken = idsOf(schedule, cipWindows, holdingArea);
    const mintId = (): string => { const id = mintBlockId("blk", taken); taken.add(id); return id; };
    const res = reforecastCips({
      lineName, lineId, startHour, duration, intervalH, horizonH,
      schedule: sched2, windows: wins2, immovable: opts.immovable, mintId,
    });
    setSchedule(res.schedule);
    setCipWindows(res.windows);
    setLastAction(
      `Added CIP on ${lineName} at ${stamp(startHour)}`
      + (shiftH > 0 ? ` (${shiftIds.length} block(s) slid ${shiftH.toFixed(1)}h)` : "")
      + `; ${res.added.length - 1} later clean(s) re-forecast`
      + (res.skipped > 0 ? ` (${res.skipped} slot(s) skipped: committed block in the way)` : ""));
  }, [pushUndo, stamp, schedule, cipWindows, holdingArea]);

  const reportAction = useCallback((msg: string) => {
    setLastAction(msg);
  }, []);

  const replaceAutoHolding = useCallback((cards: ScheduleBlock[]) => {
    // A dismissed order (card "×") gets no auto card until "+" un-dismisses it.
    const skip = new Set(holdingDismissed);
    const kept = skip.size ? cards.filter((c) => !skip.has(String(c.order_id ?? ""))) : cards;
    setHoldingArea((prev) => {
      const isAuto = (b: ScheduleBlock) => String(b.id).startsWith("hold_");
      const cur = prev.filter(isAuto);
      if (sameAutoCards(cur, kept)) return prev;
      return [...prev.filter((b) => !isAuto(b)), ...kept];
    });
  }, [holdingDismissed]);

  const setHoldingFromServer = useCallback((blocks: ScheduleBlock[], dismissed?: string[]) => {
    setHoldingArea(blocks.map(ensureId));
    if (dismissed) setHoldingDismissed(dismissed.map(String));
  }, []);

  const data: ScheduleStateData = { schedule, cipWindows, holdingArea, holdingDismissed, lastAction };
  const actions: ScheduleStateActions = {
    updateBlock, insertShift, moveBlock, resizeBlock, splitBlock,
    removeToHolding, restoreFromHolding, restorePartialFromHolding,
    addToHolding, dismissFromHolding, addProduction, addCipReforecast,
    addCip, addTrial, reportAction, setHoldingFromServer, replaceAutoHolding,
    undo, redo,
    canUndo: undoStack.current.length > 0,
    canRedo: redoStack.current.length > 0,
  };

  return [data, actions];
}
