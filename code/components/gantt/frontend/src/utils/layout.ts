// layout.ts — Hour-to-pixel conversions, snap-to-grid utilities and the
// board's clock.
//
// TIME CONVENTION (fix FE / audit time-7, C23): board hours are NAIVE
// wall-clock hours from the anchor, exactly like every Python side
// (helpers.timefmt: `parse_anchor(anchor) + timedelta(hours=h)` on a naive
// datetime; scorecard/_iso_week_bounds `b += 168.0`). A week is always 168
// h and Monday 00:00 one week after a Monday anchor is h168 — also across
// the US DST switch (2026-11-01), where local-time Date arithmetic made it
// h169 and shifted every stamp, week line and KPI week bucket by an hour
// against the Python scorecards. So: the anchor's LOCAL wall-clock fields
// are read once into a UTC millisecond value (naiveMs), hours are added as
// plain milliseconds, and every field is read back with the UTC getters.
// Only "now" (new Date()) is a real instant; it enters the board frame
// through nowHour(), which compares wall clocks the same way.

export const LINE_HEIGHT = 40;
export const HEADER_HEIGHT = 48; // two rows: week label, day labels
export const LINE_LABEL_WIDTH = 50;
export const MIN_HOUR_WIDTH = 1;
export const MAX_HOUR_WIDTH = 30;

const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

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

// ── naive clock ───────────────────────────────────────────────────────────

/** A Date's LOCAL wall-clock fields as a UTC millisecond value — the naive
 * datetime Python works with. Hours added to it never cross a DST edge. */
export function naiveMs(d: Date): number {
  return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate(),
    d.getHours(), d.getMinutes(), d.getSeconds(), d.getMilliseconds());
}

/** The wall clock `hour` hours after the anchor. READ IT WITH THE UTC
 * GETTERS (getUTCDay / getUTCHours ...): its UTC fields hold the naive
 * wall-clock values. */
export function naiveDate(anchor: Date, hour: number): Date {
  return new Date(naiveMs(anchor) + hour * HOUR_MS);
}

/** "Now" in board hours: wall clock minus wall clock, so it agrees with
 * Python's (now - anchor) on a naive anchor across a DST switch. */
export function nowHour(anchor: Date, now: Date = new Date()): number {
  return (naiveMs(now) - naiveMs(anchor)) / HOUR_MS;
}

const pad2 = (n: number): string => String(n).padStart(2, "0");

/** Format hour offset as day-of-week label (given anchor). */
export function hourToDateLabel(hour: number, anchor: Date): string {
  const d = naiveDate(anchor, hour);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return `${days[d.getUTCDay()]} ${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
}

export function hourToTimeLabel(hour: number, anchor: Date): string {
  const d = naiveDate(anchor, hour);
  return `${pad2(d.getUTCHours())}:00`;
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
  const d = naiveDate(anchor, hour);
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()} ${pad2(d.getUTCHours())}:00`;
}

/**
 * Compute hourWidth that fits the full horizon into a container.
 * Leaves a small margin so labels aren't clipped.
 */
export function fitToWidth(containerWidth: number, horizonHours: number): number {
  const available = containerWidth - LINE_LABEL_WIDTH - 10;
  return Math.max(MIN_HOUR_WIDTH, available / horizonHours);
}

// ── ISO weeks ─────────────────────────────────────────────────────────────

/** ISO year + week of a calendar date given as (year, month0, day). */
function isoParts(y: number, m0: number, day: number): { year: number; week: number } {
  const t = new Date(Date.UTC(y, m0, day));
  const dow = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - dow);       // the Thursday of that week
  const year = t.getUTCFullYear();
  const yearStart = Date.UTC(year, 0, 1);
  const week = Math.ceil(((t.getTime() - yearStart) / DAY_MS + 1) / 7);
  return { year, week };
}

/** ISO parts of a naive board date (UTC fields = wall clock). */
function isoPartsNaive(d: Date): { year: number; week: number } {
  return isoParts(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

/** ISO-8601 week number of a REAL instant's local calendar date (planner-
 * facing: W33, W34...) — for `new Date()`; board hours go through
 * isoWeekAtHour. */
export function isoWeek(d: Date): number {
  return isoParts(d.getFullYear(), d.getMonth(), d.getDate()).week;
}

/** Sortable (iso_year, iso_week) key of a real instant's local date:
 * year*100 + week. Compare keys, never bare week numbers — W53-2026 <
 * W1-2027 (fix FE / audit time-8, C24). */
export function isoWeekKey(d: Date): number {
  const p = isoParts(d.getFullYear(), d.getMonth(), d.getDate());
  return p.year * 100 + p.week;
}

export function isoWeekAtHour(anchor: Date, hour: number): number {
  return isoPartsNaive(naiveDate(anchor, hour)).week;
}

/** year*100 + week of the board hour (see isoWeekKey). */
export function isoWeekKeyAtHour(anchor: Date, hour: number): number {
  const p = isoPartsNaive(naiveDate(anchor, hour));
  return p.year * 100 + p.week;
}

/** Anchor-relative hour offsets of ISO week boundaries (Monday 00:00
 * wall-clock) covering [viewStart, viewEnd): the last Monday at or before
 * viewStart, then every following Monday. The rolling anchor is TODAY at
 * midnight — any weekday — so boundaries must come from the calendar, not
 * from `k*168` parity (hour-0 stepping put every "week" line on a Friday
 * after a Friday roll). Naive arithmetic: every week is exactly 168 h,
 * like scorecard_engine._iso_week_bounds (`b += 168.0`). */
export function mondayBoundaries(anchor: Date, viewStart: number, viewEnd: number): number[] {
  const a0 = naiveMs(anchor);
  const start = new Date(a0 + viewStart * HOUR_MS);
  const d = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate()));
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7)); // back to Monday (Mon=0)
  const out: number[] = [];
  while ((d.getTime() - a0) / HOUR_MS < viewEnd) {
    out.push((d.getTime() - a0) / HOUR_MS);
    d.setUTCDate(d.getUTCDate() + 7);
  }
  return out;
}

// ── demand week labels ────────────────────────────────────────────────────
//
// Order-id week indexes (-W0/-W1/...) count from the DEMAND file's own
// anchor week (demand_plan.source.json anchor_iso_week), NOT the calendar
// anchor — converting k via anchor + k*168h mislabels every order as soon
// as the two anchors differ (found 2026-08-15: W32-anchored demand made a
// true-W33 order read "W34"). Labels are REAL ISO weeks of real dates (fix
// FE / audit time-8, C24): the old `((base + k - 1) % 52) + 1` assumed
// 52-week years, so with demand anchored W52-2026 (2026 has 53 ISO weeks)
// index 1 read "W1" instead of W53 and every later week was off by one —
// and the holding area then judged W53 orders "past" against W1. The
// demand anchor's Monday is recovered from the base week (the ISO year
// whose week <base> lies closest to the calendar anchor — the two anchors
// are at most a few weeks apart) or taken from the payload's demand
// anchor date when the page sends one (config.demand_anchor).

let demandBaseWeek: number | null = null;
let demandMondayFixedMs: number | null = null;

function isoWeeksInYear(year: number): number {
  return isoParts(year, 11, 28).week;   // Dec 28 is always in the last ISO week
}

/** Naive ms of the Monday that starts ISO week `week` of ISO year `year`. */
function mondayOfIsoWeek(year: number, week: number): number {
  const jan4 = Date.UTC(year, 0, 4);              // always in ISO week 1
  const dow = new Date(jan4).getUTCDay() || 7;
  return jan4 - (dow - 1) * DAY_MS + (week - 1) * 7 * DAY_MS;
}

/** Monday 00:00 (naive ms) of the calendar date `s` ("YYYY-MM-DD[ HH:MM..]"
 * or a Date). null when unparseable. */
function mondayOfDate(s: string | Date): number | null {
  let ms: number | null = null;
  if (s instanceof Date) {
    ms = Number.isNaN(s.getTime()) ? null : naiveMs(s);
  } else {
    const m = /^\s*(\d{4})-(\d{1,2})-(\d{1,2})/.exec(String(s));
    if (m) ms = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  }
  if (ms === null) return null;
  const d = new Date(ms);
  const day = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  return day - ((new Date(day).getUTCDay() + 6) % 7) * DAY_MS;
}

/** Set once per mount from config. `w` = anchor_iso_week of the demand
 * file; `demandAnchor` (optional) = the demand file's anchor date, which
 * pins the week's year outright. null/undefined falls back to the legacy
 * hour math (anchor + k*168h). */
export function setDemandBaseWeek(w: number | null | undefined, demandAnchor?: string | Date | null): void {
  demandBaseWeek = typeof w === "number" && w > 0 ? w : null;
  demandMondayFixedMs = demandAnchor != null ? mondayOfDate(demandAnchor) : null;
}

/** Naive ms of the demand anchor week's Monday, or null (legacy path). */
function demandMondayMs(anchor: Date): number | null {
  if (demandMondayFixedMs !== null) return demandMondayFixedMs;
  if (demandBaseWeek === null) return null;
  const a = naiveMs(anchor);
  const y = isoPartsNaive(new Date(a)).year;
  let best: number | null = null;
  for (const yy of [y - 1, y, y + 1]) {
    if (demandBaseWeek > isoWeeksInYear(yy)) continue;
    const m = mondayOfIsoWeek(yy, demandBaseWeek);
    if (best === null || Math.abs(m - a) < Math.abs(best - a)) best = m;
  }
  return best;
}

function demandWeekParts(weekIndex: number, anchor: Date): { year: number; week: number } {
  const m0 = demandMondayMs(anchor);
  if (m0 !== null) return isoPartsNaive(new Date(m0 + weekIndex * 7 * DAY_MS));
  // Legacy fallback: k weeks after the calendar anchor (1 h in, so a
  // float-fuzzed boundary never normalizes into the prior week).
  return isoPartsNaive(naiveDate(anchor, weekIndex * 168 + 1));
}

/** Planner-facing ISO week for a demand week INDEX (0 = demand anchor week). */
export function isoWeekLabel(weekIndex: number, anchor: Date): number {
  return demandWeekParts(weekIndex, anchor).week;
}

/** year*100 + week of a demand week index — the key to compare against
 * isoWeekKey(now) / isoWeekKeyAtHour (never bare week numbers). */
export function demandWeekKey(weekIndex: number, anchor: Date): number {
  const p = demandWeekParts(weekIndex, anchor);
  return p.year * 100 + p.week;
}

/** True when a demand order's -W<k> suffix maps to an ISO week before now's.
 * Past demand weeks are misses for the Reconcile page, not planning-board
 * rows — the holding area (_holding_is_current in pages/calendar.py) and the
 * popup demand list apply the same rule. Orders without a -W suffix are
 * never "past" (they cannot be dated). `now` is injectable for tests. */
export function isPastDemandWeek(orderId: string, anchor: Date, now: Date = new Date()): boolean {
  const m = /-W(\d+)$/.exec(String(orderId ?? ""));
  if (!m) return false;
  return demandWeekKey(parseInt(m[1], 10), anchor) < isoWeekKey(now);
}

/** Planner-facing order id: the internal -W0/-W1/-W2 horizon suffix becomes
 * the ISO week (-W33/-W34/-W35). Display only - never stored. */
export function displayOrderId(orderId: string, anchor: Date): string {
  return String(orderId ?? "").replace(/-W(\d+)$/, (_m, k) =>
    `-W${isoWeekLabel(parseInt(k, 10), anchor)}`);
}
