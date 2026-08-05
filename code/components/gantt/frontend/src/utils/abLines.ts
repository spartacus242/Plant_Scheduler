// abLines.ts - A/B double-line model for the Bossar lines P17-P22.
// Mirror of code/helpers/lines_model.py. Keep the two in step: the drag preview
// must agree with what the backend computes on save.
//
// Domain truth: P09-P16 are single "Volpak" lines. P17-P22 are "Bossar" DOUBLE
// lines, physically two parallel sides A and B that share upstream/downstream
// equipment and therefore always run the same SKU. Either side can be down on
// its own; the line then produces at EXACTLY HALF rate. Both sides down = zero.

export const DOUBLE_GROUPS = ["P17", "P18", "P19", "P20", "P21", "P22"] as const;
export const SIDES = ["A", "B"] as const;

/** name -> list of [startHour, endHour) windows where that line/side is down. */
export type SideDowntime = Record<string, Array<[number, number]> | number[][]>;

const SIDE_RE = /^([A-Za-z]*\d+)([AB])$/;

/** "P17A" -> "P17"; "P17" -> "P17"; "P09" -> "P09". */
export function groupOf(line: string): string {
  const name = (line ?? "").trim().toUpperCase();
  const m = SIDE_RE.exec(name);
  if (m && (DOUBLE_GROUPS as readonly string[]).includes(m[1])) return m[1];
  return name;
}

/** "P17A" -> "A"; "P17"/"P09" -> null. */
export function sideOf(line: string): string | null {
  const name = (line ?? "").trim().toUpperCase();
  const m = SIDE_RE.exec(name);
  if (m && (DOUBLE_GROUPS as readonly string[]).includes(m[1])) return m[2];
  return null;
}

/** True for a double-line group or either of its sides. */
export function isDouble(line: string): boolean {
  return (DOUBLE_GROUPS as readonly string[]).includes(groupOf(line));
}

/** ["P17A","P17B"] for a double line; ["P09"] for a single line. */
export function sidesOf(group: string): string[] {
  const g = groupOf(group);
  if (isDouble(g)) return SIDES.map((s) => `${g}${s}`);
  return [g];
}

export function nSides(group: string): number {
  return isDouble(group) ? 2 : 1;
}

/** Rate of ONE side: half the whole-line rate on a double line. */
export function perSideRate(lineRate: number, group = ""): number {
  const r = lineRate || 0;
  if (group && !isDouble(group)) return r;
  return r / 2;
}

function covers(windows: Array<[number, number]> | number[][] | undefined, hour: number): boolean {
  if (!windows) return false;
  for (const w of windows) {
    if (hour >= w[0] && hour < w[1]) return true;
  }
  return false;
}

/** How many sides of `group` are down at `hour`. */
export function sidesDownAt(group: string, hour: number, downtime: SideDowntime): number {
  const g = groupOf(group);
  const wholeDown = covers(downtime[g], hour);
  let down = 0;
  for (const ln of sidesOf(g)) {
    if (wholeDown || covers(downtime[ln], hour)) down += 1;
  }
  return down;
}

/**
 * Throughput of `group` at `hour`: full rate with both sides up, exactly half
 * with one side down, zero with both down. Single lines are all-or-nothing.
 */
export function effectiveRate(
  group: string,
  hour: number,
  downtime: SideDowntime,
  fullRate: number,
): number {
  const rate = fullRate || 0;
  if (rate <= 0) return 0;
  const g = groupOf(group);
  const total = nSides(g);
  const up = total - sidesDownAt(g, hour, downtime);
  if (up <= 0) return 0;
  return rate * (up / total);
}

/**
 * Hours needed to make `qtyKg` on `group` starting at `startHour`, integrating
 * the available capacity hour by hour. Returns null when the line can never
 * make the quantity (no rate, or down for the whole guard window).
 */
export function hoursForQty(
  group: string,
  qtyKg: number,
  startHour: number,
  downtime: SideDowntime,
  fullRate: number,
  maxHours = 20000,
): number | null {
  const rate = fullRate || 0;
  if (rate <= 0) return null;
  let remaining = qtyKg || 0;
  if (remaining <= 0) return 0;
  let elapsed = 0;
  let guard = 0;
  while (remaining > 1e-9) {
    guard += 1;
    if (guard > maxHours) return null;
    const cap = effectiveRate(group, startHour + elapsed, downtime, rate);
    if (cap > 0) {
      if (cap >= remaining) {
        elapsed += remaining / cap;
        remaining = 0;
        break;
      }
      remaining -= cap;
    }
    elapsed += 1;
  }
  return Math.ceil(elapsed);
}

/** Kg producible on `group` between startHour and endHour. */
export function qtyOverWindow(
  group: string,
  startHour: number,
  endHour: number,
  downtime: SideDowntime,
  fullRate: number,
): number {
  const rate = fullRate || 0;
  if (rate <= 0 || endHour <= startHour) return 0;
  let total = 0;
  let h = startHour;
  while (h < endHour) {
    const step = Math.min(1, endHour - h);
    total += effectiveRate(group, h, downtime, rate) * step;
    h += step;
  }
  return total;
}

/**
 * Whole-line rate for (line, sku) from a line_name -> sku -> rate map.
 * Works whether the map is keyed by the group or by the individual sides.
 */
export function groupRate(
  caps: Record<string, Record<string, number>>,
  line: string,
  sku: string,
): number {
  const g = groupOf(line);
  const direct = caps[g]?.[sku] ?? 0;
  if (direct > 0) return direct;
  let total = 0;
  for (const ln of sidesOf(g)) total += caps[ln]?.[sku] ?? 0;
  return total;
}

/** True when any hour in [start,end) runs one-sided (half rate). */
export function hasOneSidedStretch(
  group: string,
  startHour: number,
  endHour: number,
  downtime: SideDowntime,
): boolean {
  if (!isDouble(group)) return false;
  for (let h = Math.floor(startHour); h < endHour; h++) {
    if (sidesDownAt(group, h, downtime) === 1) return true;
  }
  return false;
}
