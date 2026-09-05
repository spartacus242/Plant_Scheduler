// cipReforecast.ts — planner CIP + re-forecast of a line's later cleans.
//
// Pure port of the two rules current_state uses to stage the committed
// layer (fix FE / audit cip-14, C53), so a clean the planner places on the
// board yields the SAME grid the solver is later staged against:
//
//   1. project_cips: cleans are spaced start-to-start every `intervalH`
//      from the last clean ON THE WALL-CLOCK GRID — the grid never
//      re-phases from a snapped slot — and a slot within 1 h of a clean
//      already on the line (committed row, planner clean, cip_info
//      overlay) is skipped, the grid marching on regardless.
//   2. _clip_prod_around_cips: a slot that lands inside a production block
//      SPLITS the block around the clean; the tail (and every block queued
//      behind it, start = max(own start, previous piece end)) is PUSHED by
//      the clean's duration — the MO keeps every hour it needs. A slot that
//      overlaps a block's START pushes the block (no piece cut off). Kg is
//      apportioned by hour share (segment B takes the exact remainder, like
//      splitBlock).
//
// The old frontend moved a slot that landed inside a block to that block's
// END and spaced the next clean from there, so later cleans drifted by up
// to a block length between the board and the staged layer.
//
// One deliberate difference, stated: Python may split/push a committed MO
// (only a CIP overlapping the START of a RUNNING block is dropped); the
// board cannot move committed, pinned or lock-window blocks, so a slot that
// would have to is SKIPPED here (`skipped` counts them) — the next slot on
// the grid still lands where Python puts it. A pushed piece that runs into
// a LATER window block is not re-split; the overlap diagnostic shows it.
//
// No React, no DOM: tests/test_fix_FE.py compiles this file with the
// frontend's tsc and pins the hand-derived cases under node.

import type { ScheduleBlock } from "../types";
import { isWindowBlock } from "../types";

const EPS = 1e-6;
const round1 = (v: number): number => Math.round(v * 10) / 10;

export interface ReforecastOpts {
  lineName: string;
  lineId: number;
  /** The planner's clean: [startHour, startHour + duration). */
  startHour: number;
  duration: number;
  /** MaxHoursBetweenCIP for the line (cip_info; config default when the
   * line is missing — the same fallback current_state uses). */
  intervalH: number;
  horizonH: number;
  /** The board AFTER the planner clean's own room was opened (the caller
   * slides the blocks that overlapped [startHour, startHour + duration)). */
  schedule: ScheduleBlock[];
  windows: ScheduleBlock[];
  /** Blocks the planner may not move (committed MOs, pinned, lock window). */
  immovable?: (b: ScheduleBlock) => boolean;
  /** Fresh block id per new piece / clean (blockIdentity.mintBlockId). */
  mintId: () => string;
}

export interface ReforecastResult {
  schedule: ScheduleBlock[];
  windows: ScheduleBlock[];
  /** The planner clean first, then every projected clean added. */
  added: ScheduleBlock[];
  /** Grid slots dropped because they would have moved an immovable block. */
  skipped: number;
}

/**
 * Production on `lineName` split around a clean at [cs, cs + dur) with the
 * queue behind pushed (rule 2). Returns the new schedule, the same array
 * when the slot is free, or null when a block that must change is
 * immovable.
 */
export function insertCleanAt(
  schedule: ScheduleBlock[],
  lineName: string,
  cs: number,
  dur: number,
  immovable: (b: ScheduleBlock) => boolean,
  mintId: () => string,
): ScheduleBlock[] | null {
  const onLine = schedule
    .filter((b) => b.line_name === lineName && !isWindowBlock(b.block_type))
    .sort((a, b) => a.start_hour - b.start_hour || a.end_hour - b.end_hour);
  const ce = cs + dur;
  let cursor = ce; // blocks starting before the cursor are in the pushed queue
  const changed = new Map<ScheduleBlock, ScheduleBlock[]>();
  for (const b of onLine) {
    if (b.end_hour <= cs + EPS) continue; // wholly before the clean
    if (b.start_hour < cs - EPS && b.end_hour > cs + EPS) {
      // Spans the clean: piece A up to the clean, piece B after it, pushed
      // by the clean's duration so the run keeps every hour.
      if (immovable(b)) return null;
      const total = b.end_hour - b.start_hour;
      const fracA = total > 0 ? (cs - b.start_hour) / total : 0.5;
      const kgA = b.qty_kg ? round1(b.qty_kg * fracA) : b.qty_kg;
      const kgB = b.qty_kg ? round1(b.qty_kg - (kgA as number)) : b.qty_kg;
      const tail = b.end_hour - cs;
      const a: ScheduleBlock = { ...b, id: mintId(), end_hour: cs, run_hours: cs - b.start_hour, qty_kg: kgA };
      const c: ScheduleBlock = { ...b, id: mintId(), start_hour: ce, end_hour: ce + tail, run_hours: tail, qty_kg: kgB };
      changed.set(b, [a, c]);
      cursor = c.end_hour;
      continue;
    }
    if (b.start_hour < cursor - EPS) {
      // Starts inside the clean or inside the pushed queue: slide right.
      if (immovable(b)) return null;
      const shift = cursor - b.start_hour;
      const nb: ScheduleBlock = { ...b, start_hour: b.start_hour + shift, end_hour: b.end_hour + shift };
      changed.set(b, [nb]);
      cursor = nb.end_hour;
      continue;
    }
    break; // a gap ends the chain (sorted by start, cursor only grows)
  }
  if (changed.size === 0) return schedule;
  const out: ScheduleBlock[] = [];
  for (const b of schedule) {
    const rep = changed.get(b);
    if (rep) out.push(...rep);
    else out.push(b);
  }
  return out;
}

export function reforecastCips(o: ReforecastOpts): ReforecastResult {
  const { lineName, lineId, startHour, duration, intervalH, horizonH } = o;
  const immovable = o.immovable ?? (() => false);
  // The line's previous forecast (later projected cleans) is replaced.
  const isProjectedHere = (b: ScheduleBlock): boolean =>
    b.block_type === "cip" && b.line_name === lineName &&
    (b.attrs ?? "").includes("cip_projected") && b.start_hour > startHour + EPS;
  const wins = o.windows.filter((b) => !isProjectedHere(b));
  const cip: ScheduleBlock = {
    id: o.mintId(), line_id: lineId, line_name: lineName,
    order_id: "CIP", sku: "CIP", start_hour: startHour, end_hour: startHour + duration,
    run_hours: duration, is_trial: false, block_type: "cip", label: "CIP",
    attrs: "planner:cip",
  };
  const added: ScheduleBlock[] = [cip];
  // project_cips' `taken`: the new clean plus every other clean already on
  // the line (a committed manprg CIP row, another planner clean, the
  // cip_info ScheduledCIP overlay) — never the downtime windows.
  const taken: number[] = [startHour];
  for (const b of wins) {
    if (b.block_type === "cip" && b.line_name === lineName) taken.push(b.start_hour);
  }
  let schedule = o.schedule;
  let skipped = 0;
  let t = startHour + intervalH;
  let guard = 0;
  while (intervalH > 0 && t < horizonH && guard++ < 64) {
    if (taken.some((s) => Math.abs(s - t) <= 1.0)) {
      t += intervalH; // a clean already sits here: the grid marches on
      continue;
    }
    const slotEnd = Math.min(horizonH, t + duration);
    const next = insertCleanAt(schedule, lineName, t, slotEnd - t, immovable, o.mintId);
    if (next === null) {
      skipped += 1;
      t += intervalH;
      continue;
    }
    schedule = next;
    added.push({
      ...cip, id: o.mintId(), start_hour: t, end_hour: slotEnd, run_hours: slotEnd - t,
      label: "CIP (projected)", attrs: "planner:cip_projected",
    });
    taken.push(t);
    t += intervalH;
  }
  return { schedule, windows: [...wins, ...added], added, skipped };
}
