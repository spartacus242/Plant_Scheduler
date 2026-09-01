// holdingDerive.ts — client-side derivation of the AUTO holding cards.
//
// Exact port of helpers/holding_builder.build_holding as the calendar page
// drives it (one compute_adherence pass with the made-only credit map):
//   card iff board credit < qty_min; qty = qty_min - credit;
//   run_hours = qty / mean capable-line rate; id = "hold_<order_id>";
//   past demand weeks never hold (they are misses, not cards);
//   current-state rows (order_id ending "|CUR") never hold.
// Until 2026-09-01 the client only updated holding on a drag FROM holding —
// every other edit (SKU picker, resize, split, trash) waited for the
// server rebuild behind "Refresh checks". Deriving here keeps the cards
// live on ANY schedule change; the server rebuild then agrees by
// construction (same math, same ids).

import type { DemandTarget, ScheduleBlock } from "../types";
import { computeAdherence } from "./kpi";
import { isoWeekAtHour, isoWeekLabel } from "./layout";

type Caps = Record<string, Record<string, number>>;

function meanCapableRate(caps: Caps, sku: string): number {
  let sum = 0;
  let n = 0;
  for (const line of Object.keys(caps)) {
    const r = Number(caps[line]?.[sku] ?? 0);
    if (r > 0) { sum += r; n += 1; }
  }
  return n ? sum / n : 0;
}

const round1 = (v: number) => Math.round(v * 10) / 10;

export function deriveAutoHolding(
  schedule: ScheduleBlock[],
  demand: DemandTarget[],
  caps: Caps,
  coveredByOrder: Record<string, number> | undefined,
  anchor: Date,
  skuDescriptions: Record<string, string>,
): ScheduleBlock[] {
  const rows = computeAdherence(schedule, demand, caps, coveredByOrder);
  const nowIso = isoWeekAtHour(new Date(), 0);
  const out: ScheduleBlock[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    const oid = String(r.order_id ?? "");
    const sku = String(r.sku ?? "").trim();
    if (!oid || !sku || oid.endsWith("|CUR") || seen.has(oid)) continue;
    seen.add(oid);
    const m = /-W(\d+)$/.exec(oid);
    if (m) {
      const wk = isoWeekLabel(parseInt(m[1], 10), anchor);
      if (wk !== null && wk < nowIso) continue; // a week that is over is a miss
    }
    const qtyMin = Number(r.qty_min ?? 0);
    if (qtyMin <= 0) continue;
    const prod = Number(r.scheduled_qty ?? 0);
    if (prod >= qtyMin) continue;
    const missing = qtyMin - prod;
    const rate = meanCapableRate(caps, sku);
    const hours = rate > 0 ? missing / rate : 0;
    out.push({
      id: `hold_${oid}`,
      line_id: 0,
      line_name: "",
      order_id: oid,
      sku,
      sku_description: skuDescriptions[sku] ?? "",
      start_hour: 0,
      end_hour: round1(hours),
      run_hours: round1(hours),
      is_trial: false,
      block_type: "sku",
      qty_kg: round1(missing),
    });
  }
  return out;
}

/** True when two auto-card lists carry the same ids/hours/kg — lets the
 * state setter return `prev` unchanged and avoid render loops. */
export function sameAutoCards(a: ScheduleBlock[], b: ScheduleBlock[]): boolean {
  if (a.length !== b.length) return false;
  const key = (c: ScheduleBlock) => `${c.id}|${c.run_hours}|${c.qty_kg ?? ""}`;
  const sa = a.map(key).sort();
  const sb = b.map(key).sort();
  return sa.every((k, i) => k === sb[i]);
}
