// layout.ts — Hour-to-pixel conversions and snap-to-grid utilities.

export const LINE_HEIGHT = 40;
export const HEADER_HEIGHT = 48; // taller header for two-row day/date labels
export const LINE_LABEL_WIDTH = 50;
export const MIN_HOUR_WIDTH = 1;
export const MAX_HOUR_WIDTH = 30;

export function hourToX(hour: number, viewStart: number, hourWidth: number): number {
  return LINE_LABEL_WIDTH + (hour - viewStart) * hourWidth;
}

export function xToHour(x: number, viewStart: number, hourWidth: number): number {
  return viewStart + (x - LINE_LABEL_WIDTH) / hourWidth;
}

export function snapToHour(hour: number): number {
  return Math.round(hour);
}

export function lineToY(lineIndex: number): number {
  return HEADER_HEIGHT + lineIndex * LINE_HEIGHT;
}

export function yToLineIndex(y: number): number {
  return Math.floor((y - HEADER_HEIGHT) / LINE_HEIGHT);
}

/** Format hour offset as day-of-week label (given anchor). */
export function hourToDateLabel(hour: number, anchor: Date): string {
  const d = new Date(anchor.getTime() + hour * 3600000);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return `${days[d.getDay()]} ${d.getMonth() + 1}/${d.getDate()}`;
}

export function hourToTimeLabel(hour: number, anchor: Date): string {
  const d = new Date(anchor.getTime() + hour * 3600000);
  return `${d.getHours().toString().padStart(2, "0")}:00`;
}

/**
 * Full wall-clock stamp for an hour offset, e.g. "Wed 2/18 11:00".
 * Use this anywhere an hour offset is shown to a user as a moment in time.
 * Durations stay in hours - only moments get stamped.
 */
export function hourToStamp(hour: number, anchor: Date): string {
  return `${hourToDateLabel(hour, anchor)} ${hourToTimeLabel(hour, anchor)}`;
}

/** Short stamp for tight spaces, e.g. "2/18 11:00" (no day-of-week). */
export function hourToShortStamp(hour: number, anchor: Date): string {
  const d = new Date(anchor.getTime() + hour * 3600000);
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, "0")}:00`;
}

/**
 * Compute hourWidth that fits the full horizon into a container.
 * Leaves a small margin so labels aren't clipped.
 */
export function fitToWidth(containerWidth: number, horizonHours: number): number {
  const available = containerWidth - LINE_LABEL_WIDTH - 10;
  return Math.max(MIN_HOUR_WIDTH, available / horizonHours);
}


/** ISO-8601 week number of a date (planner-facing: W33, W34...). */
export function isoWeek(d: Date): number {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
}

export function isoWeekAtHour(anchor: Date, hour: number): number {
  return isoWeek(new Date(anchor.getTime() + hour * 3600_000));
}

/** ISO week of the DEMAND anchor (demand_plan.source.json anchor_iso_week).
 * Order-id week indexes count from the demand file's own anchor week, NOT
 * the calendar anchor — converting k via anchor + k*168h mislabels every
 * order as soon as the two anchors differ (found 2026-08-15: W32-anchored
 * demand made a true-W33 order read "W34"). Set once per mount from config;
 * null falls back to the legacy hour math. */
let demandBaseWeek: number | null = null;
export function setDemandBaseWeek(w: number | null | undefined): void {
  demandBaseWeek = typeof w === "number" && w > 0 ? w : null;
}

/** Planner-facing ISO week for a demand week INDEX (0 = demand anchor week). */
export function isoWeekLabel(weekIndex: number, anchor: Date): number {
  if (demandBaseWeek !== null) {
    return ((demandBaseWeek + weekIndex - 1) % 52) + 1; // 52-wrap approximation
  }
  return isoWeekAtHour(anchor, weekIndex * 168 + 1);
}

/** Planner-facing order id: the internal -W0/-W1/-W2 horizon suffix becomes
 * the ISO week (-W33/-W34/-W35). Display only - never stored. */
export function displayOrderId(orderId: string, anchor: Date): string {
  return String(orderId ?? "").replace(/-W(\d+)$/, (_m, k) =>
    `-W${isoWeekLabel(parseInt(k, 10), anchor)}`);
}
