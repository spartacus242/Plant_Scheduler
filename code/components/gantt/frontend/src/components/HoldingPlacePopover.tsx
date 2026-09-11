// HoldingPlacePopover.tsx — right-click a HOLDING card: which lines can run
// this order, with a snap-left placement suggestion per line and a Place
// button (the reverse of the blank-space SKU picker; user request
// 2026-09-01). One row per capable line, sorted by earliest start. An
// oversized card places what fits — the remainder stays in holding (the
// Place pathway reuses the resize-to-fit split).

import React from "react";
import { displayOrderId, hourToStamp } from "../utils/layout";
import type { PlacementPlan } from "../utils/skuPicker";
import type { ScheduleBlock } from "../types";
import { Chips } from "./SkuPickerPopover";
import { BTN_PRIMARY, POPOVER, T } from "../utils/theme";

export interface HoldingPlaceRow {
  lineName: string;
  lineId: number;
  plan: PlacementPlan;
  inFlags: string[] | null;
  outFlags: string[] | null;
}

interface Props {
  block: ScheduleBlock;
  x: number;
  y: number;
  rows: HoldingPlaceRow[];
  anchor: Date;
  onPlace: (row: HoldingPlaceRow) => void;
  onClose: () => void;
}

const TH: React.CSSProperties = {
  textAlign: "left", fontSize: 10.5, color: T.ink3, fontWeight: 700,
  textTransform: "uppercase", letterSpacing: 0.4,
  padding: "2px 8px 4px 8px", borderBottom: `1px solid ${T.rule}`,
  whiteSpace: "nowrap",
};
const TD: React.CSSProperties = {
  padding: "5px 8px", borderBottom: `1px solid ${T.surface2}`,
  whiteSpace: "nowrap", verticalAlign: "middle",
};

export const HoldingPlacePopover: React.FC<Props> = ({
  block, x, y, rows, anchor, onPlace, onClose,
}) => (
  <div
    style={{
      ...POPOVER,
      left: Math.min(x, Math.max(40, window.innerWidth - 640)),
      top: Math.min(y, Math.max(40, window.innerHeight - 320)),
      zIndex: 1200,
      padding: "10px 12px",
      minWidth: 460,
      fontSize: 12.5,
    }}
    onClick={(e) => e.stopPropagation()}
    onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); }}
  >
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
      <strong>
        Place {displayOrderId(block.order_id, anchor)} — {block.run_hours.toFixed(1)}h
        {block.qty_kg ? ` · ${Math.round(block.qty_kg).toLocaleString()} kg` : ""}
      </strong>
      <span style={{ cursor: "pointer", fontWeight: 700, color: T.ink3 }} onClick={onClose}>×</span>
    </div>
    <div style={{ fontSize: 11, color: T.ink3, marginBottom: 6 }}>
      Lines that can run {block.sku}, earliest snap-left gap first. An
      oversized card places what fits; the rest stays in holding.
    </div>
    {rows.length === 0 ? (
      <div style={{ padding: "8px 2px", color: T.bad, fontWeight: 600 }}>
        No line has an open gap for {block.sku} right now.
      </div>
    ) : (
      <div style={{ maxHeight: 300, overflowY: "auto" }}>
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead>
            <tr>
              <th style={TH}></th>
              <th style={TH}>Line</th>
              <th style={TH}>← changeover</th>
              <th style={TH}>changeover →</th>
              <th style={TH}>Placement</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.lineName}>
                <td style={TD}>
                  <button
                    title={`Place ${block.sku} on ${r.lineName} snapped left`}
                    style={{ ...BTN_PRIMARY, fontSize: 11.5, padding: "3px 10px", fontWeight: 700 }}
                    onClick={() => onPlace(r)}
                  >
                    Place
                  </button>
                </td>
                <td style={{ ...TD, fontWeight: 700 }}>{r.lineName}</td>
                <td style={TD}>
                  <Chips flags={r.inFlags} setupH={r.plan.setupBeforeH} against={r.plan.prevSku}
                         neighbourType={r.plan.prevSku ? null : r.plan.prevType} arrow="in" />
                </td>
                <td style={TD}>
                  <Chips flags={r.outFlags} setupH={r.plan.setupAfterH} against={r.plan.nextSku}
                         neighbourType={r.plan.nextSku ? null : r.plan.nextType} arrow="out" />
                </td>
                <td style={{ ...TD, fontSize: 11, color: T.ink2 }}>
                  {`${hourToStamp(r.plan.startHour, anchor)} · ` +
                   `${r.plan.durationH.toFixed(1)}h · ` +
                   `${Math.round(r.plan.qtyKg).toLocaleString()} kg`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )}
  </div>
);
