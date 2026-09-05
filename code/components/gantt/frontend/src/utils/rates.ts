// rates.ts — the ONE mean-capable-rate rule (fix FE / audit ui-4).
//
// helpers/holding_builder.average_rate_per_sku averages calc_rate_kgph over
// the capable ROWS of capabilities_rates.csv — one row per LINE, keyed by
// the double-line GROUP ("P17"). pages/calendar.py then hands the Gantt
// expand_caps_with_groups(caps), which adds the sides P17A/P17B at half
// rate beside the group entry. Averaging every key of that map counted a
// double line three times (R + R/2 + R/2 over 3 entries = 2R/3 of its
// weight), so 112 of 153 live holding cards re-priced themselves on the
// first edit (280103: server 1152.2 kg/h, client 925.1). The mean here is
// over DISTINCT LINES: every key collapses to its group (abLines.groupOf)
// and the line's whole rate is the group entry when present, else the sum
// of its sides — so the answer is the same whichever way the map is keyed.
// kpi.ts (avg_rate_kgph) and holdingDerive.ts (card hours) both use it.

import { groupOf, groupRate } from "./abLines";

export type Caps = Record<string, Record<string, number>>;

/** Mean whole-line rate over the distinct lines that can run `sku` (kg/h),
 * 0 when none can. */
export function meanCapableRate(caps: Caps | null | undefined, sku: string): number {
  if (!caps) return 0;
  const seen = new Set<string>();
  let sum = 0;
  let n = 0;
  for (const line of Object.keys(caps)) {
    const g = groupOf(line);
    if (seen.has(g)) continue;
    seen.add(g);
    const r = Number(groupRate(caps, g, sku) || 0);
    if (r > 0) { sum += r; n += 1; }
  }
  return n ? sum / n : 0;
}
