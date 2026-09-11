// SkuPickerPopover.tsx — blank-space "add a SKU" table.
//
// Opens on RIGHT-CLICK in an empty gap on a line row (plain left-click keeps
// its existing deselect behavior). One row per demand-plan SKU the line can
// run and that still has remaining demand, sorted by remaining kg DESC.
// Shows the changeover types the placement would create against the previous
// and next block (chips: ttp / ffs / tpld / cspkr / conv-org / cin-non) and
// the snap-left placement (start, hours, kg). Rows that cannot fit render
// disabled with the reason.

import React, { useMemo, useState } from "react";
import { hourToStamp } from "../utils/layout";
import type { PlacementPlan } from "../utils/skuPicker";
import type { StockArgs } from "../types";
import type { Timelines } from "../utils/stockRisk";
import {
  earliestSafeStartFor, safeStartPill, type PillTone, type SafeStartPill, type StampFn,
} from "../utils/supplyGlue";
import { BTN_BASE, POPOVER, T } from "../utils/theme";

export interface PickerRowData {
  sku: string;
  desc: string;
  remainingKg: number;
  plan: PlacementPlan;
  /** Changeover-type chips prev→sku and sku→next; null = pair absent from
   * coFlags (neighbour off the demand plan) — unknown, NOT clean. */
  inFlags: string[] | null;
  outFlags: string[] | null;
}

interface Props {
  lineName: string;
  hour: number;
  x: number;
  y: number;
  anchor: Date;
  rows: PickerRowData[];
  onPlace: (row: PickerRowData) => void;
  /** "+ CIP here": a clean at this gap's snap-left point; later projected
   * CIPs on the line re-forecast from it. null = done, string = why not. */
  onAddCip?: () => string | null;
  /** Ad-hoc trial run at this gap (trials ARE schedule — they go to the
   * ERP with production, unlike downtimes). null = done, string = why not. */
  onAddTrial?: (sku: string, hours: number) => string | null;
  /** Supply timeline (contract 2026-09-01 §9): with a payload the table
   * grows a "Supply" column — the before-placement pill per placeable
   * row; stock null = no column at all (read-only mounts, old cache). */
  stock?: StockArgs | null;
  supplyTimelines?: Timelines | null;
  supplyStamp?: StampFn | null;
  onClose: () => void;
}

const TH: React.CSSProperties = {
  textAlign: "left", padding: "3px 8px", color: T.ink3,
  fontSize: 10.5, fontWeight: 700, position: "sticky", top: 0,
  textTransform: "uppercase", letterSpacing: 0.4,
  background: T.surface, borderBottom: `1px solid ${T.rule}`, whiteSpace: "nowrap",
};
const TD: React.CSSProperties = {
  padding: "4px 8px", fontSize: 12, borderBottom: `1px solid ${T.surface2}`,
  whiteSpace: "nowrap", verticalAlign: "top",
};

export const Chips: React.FC<{
  flags: string[] | null; setupH: number; against: string | null;
  /** Neighbour block_type when it is a window (CIP etc.) — no setup needed. */
  neighbourType: string | null; arrow: "in" | "out";
}> = ({ flags, setupH, against, neighbourType, arrow }) => {
  if (!against) {
    // Window neighbour (CIP / maintenance / down): the wash absorbs any
    // changeover — say so rather than pretending the line is open.
    const label = neighbourType
      ? (arrow === "in" ? `after ${neighbourType.toUpperCase()}` : neighbourType.toUpperCase())
      : (arrow === "in" ? "line start" : "open");
    return <span style={{ color: T.ink3 }}>{label}</span>;
  }
  return (
    <span>
      {flags === null ? (
        // Pair not in the coFlags table (neighbour off the demand plan):
        // the changeover TYPE is unknown — never imply a clean transition.
        <span
          title="no changeover data for this pair"
          style={{
            display: "inline-block", padding: "0 6px", marginRight: 3,
            borderRadius: 3, background: T.neutralSoft, color: T.neutral,
            fontSize: 10.5, fontWeight: 700, lineHeight: "16px",
          }}
        >
          ?
        </span>
      ) : flags.length === 0 ? (
        <span style={{ color: T.ok, fontWeight: 600 }}>clean</span>
      ) : (
        flags.map((f) => (
          <span
            key={f}
            style={{
              display: "inline-block", padding: "0 5px", marginRight: 3,
              borderRadius: 3, background: T.warnSoft, color: T.warn,
              fontSize: 10.5, fontWeight: 700, lineHeight: "16px",
            }}
          >
            {f}
          </span>
        ))
      )}
      {setupH > 0 && <span style={{ color: T.ink3, fontSize: 11 }}> +{setupH}h</span>}
    </span>
  );
};

const PILL_STYLE: Record<PillTone, React.CSSProperties> = {
  ok: { background: T.neutralSoft, color: T.ink3, border: `1px solid ${T.rule}` },
  muted: { background: T.surface2, color: T.neutral, border: `1px solid ${T.rule}` },
  warn: { background: T.warnSoft, color: T.warn, border: "1px solid #F1CFA9" },
  crit: { background: T.badSoft, color: T.bad, border: "1px solid #EFBDBB" },
};

/** The before-placement supply pill (§9) — one look on holding cards,
 * picker rows and place rows; the title carries the planner sentences. */
export const SafeStartTag: React.FC<{ pill: SafeStartPill | null | undefined }> = ({ pill }) => {
  if (!pill) return null;
  return (
    <span
      data-testid="safe-start-pill"
      data-tone={pill.tone}
      title={pill.title}
      style={{
        display: "inline-block", padding: "0 6px", borderRadius: 8, whiteSpace: "nowrap",
        fontSize: pill.tone === "ok" ? 10 : 10.5, fontWeight: 700, lineHeight: "16px",
        ...PILL_STYLE[pill.tone],
      }}
    >
      {pill.text}
    </span>
  );
};

/** Pill for a candidate row: the run the Place button would create (the
 * plan's kg over the plan's hours, from the plan's start) judged alone
 * against the placed board — so the pill and the post-commit banner agree.
 * null without a stock payload or for a plan that places nothing. */
export function planPill(
  sku: string, plan: { startHour: number; durationH: number; qtyKg: number }, lineName: string,
  stock: StockArgs | null | undefined, timelines: Timelines | null | undefined,
  stamp: StampFn | null | undefined, dueEndH?: number | null,
): SafeStartPill | null {
  if (!stock || !timelines || !stamp || !(plan.durationH > 0)) return null;
  const kg = plan.qtyKg > 0 ? plan.qtyKg : 0;
  const res = earliestSafeStartFor(
    sku, kg, kg > 0 ? kg / plan.durationH : 0, plan.startHour, timelines, stock,
    { lineName, fallbackHours: plan.durationH },
  );
  return safeStartPill(res, stamp, dueEndH);
}

export const SkuPickerPopover: React.FC<Props> = ({
  lineName, hour, x, y, anchor, rows, onPlace, onClose, onAddCip, onAddTrial,
  stock = null, supplyTimelines = null, supplyStamp = null,
}) => {
  const [trialSku, setTrialSku] = useState("");
  const [trialH, setTrialH] = useState("8");
  const [err, setErr] = useState<string | null>(null);
  const showSupply = stock !== null;
  // One pill per placeable row (a blocked row shows its reason instead),
  // recomputed only when the rows or the board's timelines change.
  const pills = useMemo<(SafeStartPill | null)[]>(
    () => rows.map((r) => (r.plan.reason === null
      ? planPill(r.sku, r.plan, lineName, stock, supplyTimelines, supplyStamp)
      : null)),
    [rows, lineName, stock, supplyTimelines, supplyStamp],
  );
  // Keep the table on screen: it is wide, so pull it left/up near the edges
  // (wider still with the Supply column).
  const maxW = showSupply ? 800 : 690;
  const left = Math.max(8, Math.min(x, (window.innerWidth || 1200) - (maxW + 10)));
  const top = Math.max(8, Math.min(y, (window.innerHeight || 800) - 420));
  return (
    <div
      style={{
        ...POPOVER, left, top, padding: "10px 12px",
        maxWidth: maxW,
      }}
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <strong style={{ fontSize: 13 }}>
          ＋ Add a SKU — {lineName}, gap at {hourToStamp(hour, anchor)}
        </strong>
        <span style={{ display: "flex", gap: 10, alignItems: "baseline", marginLeft: 12 }}>
          {onAddCip && (
            <button
              title="Insert a clean at this gap; later projected CIPs on the line re-forecast from it"
              style={{ ...BTN_BASE, fontSize: 11.5, padding: "3px 10px", fontWeight: 700,
                       border: "1px solid #BBD3F0", background: T.infoSoft, color: T.info }}
              onClick={() => { const e = onAddCip(); setErr(e); if (!e) onClose(); }}
            >
              🧼 CIP here
            </button>
          )}
          <span style={{ cursor: "pointer", fontWeight: 700, color: T.ink3 }} onClick={onClose}>
            ×
          </span>
        </span>
      </div>
      {onAddTrial && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 11.5, margin: "4px 0 2px" }}>
          <span style={{ color: T.ink2, fontWeight: 600 }}>Trial run:</span>
          <input value={trialSku} onChange={(e) => setTrialSku(e.target.value)} placeholder="SKU"
                 style={{ width: 90, fontSize: 11.5, padding: "2px 4px", border: `1px solid ${T.rule}`, borderRadius: 4 }} />
          <input value={trialH} onChange={(e) => setTrialH(e.target.value)} placeholder="h"
                 style={{ width: 44, fontSize: 11.5, padding: "2px 4px", border: `1px solid ${T.rule}`, borderRadius: 4 }} />
          <button
            style={{ ...BTN_BASE, fontSize: 11.5, padding: "2px 8px", fontWeight: 700 }}
            onClick={() => {
              const h = Number(trialH);
              const e = !trialSku.trim() ? "enter a SKU" : !(h > 0) ? "hours must be > 0" : onAddTrial(trialSku.trim(), h);
              setErr(e);
              if (!e) onClose();
            }}
          >
            Add trial
          </button>
        </div>
      )}
      {err && <div style={{ fontSize: 11.5, color: T.bad, fontWeight: 600, margin: "2px 0" }}>{err}</div>}
      <div style={{ fontSize: 11, color: T.ink3, margin: "2px 0 6px" }}>
        Snaps left against the previous block with the changeover setup
        respected. Demand-plan SKUs this line can run, most open demand first.
      </div>
      {rows.length === 0 ? (
        <div style={{ fontSize: 12, color: T.ink3, padding: "8px 0" }}>
          No SKU with remaining demand can run on {lineName}.
        </div>
      ) : (
        <div style={{ maxHeight: 340, overflowY: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead>
              <tr>
                {/* Place lives in the FIRST column (user request 2026-09-01):
                    the row can overflow horizontally and the button must be
                    clickable without scrolling. */}
                <th style={TH}></th>
                <th style={TH}>SKU</th>
                <th style={{ ...TH, textAlign: "right" }}>Demand left</th>
                <th style={TH}>← changeover</th>
                <th style={TH}>changeover →</th>
                <th style={TH}>Placement</th>
                {showSupply && <th style={TH}>Supply</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
                const blocked = r.plan.reason !== null;
                return (
                  <tr key={r.sku} style={{ opacity: blocked ? 0.45 : 1 }} title={r.plan.reason ?? undefined}>
                    <td style={TD}>
                      <button
                        disabled={blocked}
                        title={blocked ? r.plan.reason ?? "" : `Place ${r.sku} snapped left`}
                        style={{
                          fontSize: 11.5, padding: "3px 10px", borderRadius: 4,
                          fontWeight: 700,
                          border: blocked ? `1px solid ${T.rule}` : `1px solid ${T.accent}`,
                          background: blocked ? T.surface2 : T.accent,
                          color: blocked ? T.ink3 : "#fff",
                          cursor: blocked ? "not-allowed" : "pointer",
                        }}
                        onClick={() => onPlace(r)}
                      >
                        Place
                      </button>
                    </td>
                    <td style={TD}>
                      <span style={{ fontWeight: 700 }}>{r.sku}</span>
                      {r.desc && (
                        <div style={{ fontSize: 10.5, color: T.ink3, maxWidth: 170,
                                      overflow: "hidden", textOverflow: "ellipsis" }}>
                          {r.desc}
                        </div>
                      )}
                    </td>
                    <td style={{ ...TD, textAlign: "right", fontWeight: 600 }}>
                      {Math.round(r.remainingKg).toLocaleString()} kg
                    </td>
                    <td style={TD}>
                      <Chips flags={r.inFlags} setupH={r.plan.setupBeforeH} against={r.plan.prevSku}
                             neighbourType={r.plan.prevSku ? null : r.plan.prevType} arrow="in" />
                    </td>
                    <td style={TD}>
                      <Chips flags={r.outFlags} setupH={r.plan.setupAfterH} against={r.plan.nextSku}
                             neighbourType={r.plan.nextSku ? null : r.plan.nextType} arrow="out" />
                    </td>
                    <td style={{ ...TD, fontSize: 11, color: blocked ? T.bad : T.ink2 }}>
                      {blocked
                        ? r.plan.reason
                        : `${hourToStamp(r.plan.startHour, anchor)} · ` +
                          `${r.plan.durationH.toFixed(1)}h · ` +
                          `${Math.round(r.plan.qtyKg).toLocaleString()} kg`}
                    </td>
                    {showSupply && (
                      <td style={TD}><SafeStartTag pill={pills[i]} /></td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};
