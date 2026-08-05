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
