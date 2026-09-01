// SkuPickerPopover.tsx — blank-space "add a SKU" table.
//
// Opens on RIGHT-CLICK in an empty gap on a line row (plain left-click keeps
// its existing deselect behavior). One row per demand-plan SKU the line can
// run and that still has remaining demand, sorted by remaining kg DESC.
// Shows the changeover types the placement would create against the previous
// and next block (chips: ttp / ffs / tpld / cspkr / conv-org / cin-non) and
// the snap-left placement (start, hours, kg). Rows that cannot fit render
// disabled with the reason.

import React, { useState } from "react";
import { hourToStamp } from "../utils/layout";
import type { PlacementPlan } from "../utils/skuPicker";

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
  onClose: () => void;
}

const TH: React.CSSProperties = {
  textAlign: "left", padding: "3px 8px", color: "#78909c",
  fontSize: 10.5, fontWeight: 700, position: "sticky", top: 0,
  background: "#fff", borderBottom: "1px solid #e0e0e5", whiteSpace: "nowrap",
};
const TD: React.CSSProperties = {
  padding: "4px 8px", fontSize: 12, borderBottom: "1px solid #f2f2f5",
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
    return <span style={{ color: "#bbb" }}>{label}</span>;
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
            borderRadius: 3, background: "#eceff1", color: "#607d8b",
            fontSize: 10.5, fontWeight: 700, lineHeight: "16px",
          }}
        >
          ?
        </span>
      ) : flags.length === 0 ? (
        <span style={{ color: "#2e7d32", fontWeight: 600 }}>clean</span>
      ) : (
        flags.map((f) => (
          <span
            key={f}
            style={{
              display: "inline-block", padding: "0 5px", marginRight: 3,
              borderRadius: 3, background: "#fff3e0", color: "#e65100",
              fontSize: 10.5, fontWeight: 700, lineHeight: "16px",
            }}
          >
            {f}
          </span>
        ))
      )}
      {setupH > 0 && <span style={{ color: "#888", fontSize: 11 }}> +{setupH}h</span>}
    </span>
  );
};

export const SkuPickerPopover: React.FC<Props> = ({
  lineName, hour, x, y, anchor, rows, onPlace, onClose, onAddCip, onAddTrial,
}) => {
  const [trialSku, setTrialSku] = useState("");
  const [trialH, setTrialH] = useState("8");
  const [err, setErr] = useState<string | null>(null);
  // Keep the table on screen: it is wide, so pull it left/up near the edges.
  const left = Math.max(8, Math.min(x, (window.innerWidth || 1200) - 700));
  const top = Math.max(8, Math.min(y, (window.innerHeight || 800) - 420));
  return (
    <div
      style={{
        position: "fixed", left, top, zIndex: 1000,
        background: "#fff", border: "1px solid #ccc", borderRadius: 8,
        boxShadow: "0 4px 16px rgba(0,0,0,0.18)", padding: "10px 12px",
        maxWidth: 690,
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
              style={{ fontSize: 11.5, padding: "3px 10px", borderRadius: 4, fontWeight: 700,
                       border: "1px solid #1565c0", background: "#e3f2fd", cursor: "pointer" }}
              onClick={() => { const e = onAddCip(); setErr(e); if (!e) onClose(); }}
            >
              🧼 CIP here
            </button>
          )}
          <span style={{ cursor: "pointer", fontWeight: 700, color: "#888" }} onClick={onClose}>
            ×
          </span>
        </span>
      </div>
      {onAddTrial && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 11.5, margin: "4px 0 2px" }}>
          <span style={{ color: "#607d8b", fontWeight: 600 }}>Trial run:</span>
          <input value={trialSku} onChange={(e) => setTrialSku(e.target.value)} placeholder="SKU"
                 style={{ width: 90, fontSize: 11.5, padding: "2px 4px", border: "1px solid #ccc", borderRadius: 4 }} />
          <input value={trialH} onChange={(e) => setTrialH(e.target.value)} placeholder="h"
                 style={{ width: 44, fontSize: 11.5, padding: "2px 4px", border: "1px solid #ccc", borderRadius: 4 }} />
          <button
            style={{ fontSize: 11.5, padding: "2px 8px", borderRadius: 4, fontWeight: 700,
                     border: "1px solid #7b1fa2", background: "#f3e5f5", cursor: "pointer" }}
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
      {err && <div style={{ fontSize: 11.5, color: "#b71c1c", margin: "2px 0" }}>{err}</div>}
      <div style={{ fontSize: 11, color: "#888", margin: "2px 0 6px" }}>
        Snaps left against the previous block with the changeover setup
        respected. Demand-plan SKUs this line can run, most open demand first.
      </div>
      {rows.length === 0 ? (
        <div style={{ fontSize: 12, color: "#888", padding: "8px 0" }}>
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
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
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
                          border: blocked ? "1px solid #ccc" : "1px solid #0a8",
                          background: blocked ? "#f5f5f5" : "#00c896",
                          color: blocked ? "#999" : "#fff",
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
                        <div style={{ fontSize: 10.5, color: "#888", maxWidth: 170,
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
                    <td style={{ ...TD, fontSize: 11, color: blocked ? "#b71c1c" : "#455a64" }}>
                      {blocked
                        ? r.plan.reason
                        : `${hourToStamp(r.plan.startHour, anchor)} · ` +
                          `${r.plan.durationH.toFixed(1)}h · ` +
                          `${Math.round(r.plan.qtyKg).toLocaleString()} kg`}
                    </td>
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
