// AdherenceTable.tsx — Live-updating sorted table. Click a row to highlight that SKU;
// the "+" button on each row creates a holding block for the missing tonnage.

import React from "react";
import type { AdherenceRow } from "../types";

interface Props {
  rows: AdherenceRow[];
  /** Display-only order-id formatter (ISO week suffix). */
  formatOrder?: (id: string) => string;
  highlightSku: string | null;
  onSkuClick: (sku: string | null) => void;
  onAddToHolding: (row: AdherenceRow, missingKg: number, runHours: number) => void;
  /** Mean capable-line rate (kg/h) for a SKU — the auto holding card's
   * duration basis (utils/rates.meanCapableRate). Server rows carry no
   * avg_rate_kgph, so without this the '+' button has no rate at all. */
  rateFor?: (sku: string) => number;
}

const statusColors: Record<string, string> = {
  MET: "#00CC96",
  UNDER: "#EF553B",
  OVER: "#FFA15A",
};

export const AdherenceTable: React.FC<Props> = ({ rows, highlightSku, onSkuClick, onAddToHolding, formatOrder, rateFor }) => {
  return (
    <div style={{ maxHeight: 260, overflowY: "auto", border: "1px solid #e0e0e5", borderRadius: 8, background: "#ffffff" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, color: "#333" }}>
        <thead>
          <tr style={{ background: "#f7f7fa", position: "sticky", top: 0 }}>
            <th style={th}>SKU</th>
            <th style={th}>Order</th>
            <th style={th}>Min Qty</th>
            <th style={th}>Sched Qty</th>
            <th style={th}>% Adh</th>
            <th style={th}>Status</th>
            <th style={{ ...th, width: 32 }}></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isSelected = highlightSku === r.sku;
            const missingKg = Math.max(0, r.qty_min - r.scheduled_qty);
            return (
              <tr
                key={r.order_id}
                style={{
                  borderBottom: "1px solid #eee",
                  background: isSelected ? "#fff8dc" : "#ffffff",
                  cursor: "pointer",
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onSkuClick(isSelected ? null : r.sku);
                }}
              >
                <td style={{ ...td, fontWeight: isSelected ? 700 : 400 }}>{r.sku}</td>
                <td style={td}>{formatOrder ? formatOrder(r.order_id) : r.order_id}</td>
                <td style={tdRight}>{r.qty_min.toLocaleString()}</td>
                <td style={tdRight}>{r.scheduled_qty.toLocaleString()}</td>
                <td style={tdRight}>{r.pct_adherence}%</td>
                <td style={{ ...td, color: statusColors[r.status] ?? "#333", fontWeight: 600 }}>
                  {r.status}
                </td>
                <td style={{ ...td, cursor: "pointer" }}>
                  <button
                    title={missingKg > 0 ? `Add missing ${missingKg.toLocaleString()} kg to holding` : "Order already met"}
                    disabled={missingKg <= 0}
                    onClick={(e) => {
                      e.stopPropagation();
                      // The card is sized like an AUTO holding card: missing
                      // tonnage at the SKU's mean capable rate — no 24 h cap,
                      // no `|| 1` divisor (fix FE / audit ui-8: a 4,000 kg
                      // gap and a 196,000 kg gap both became 24 h cards
                      // before the first edit). 0 when no line is capable.
                      const rate = (rateFor ? rateFor(r.sku) : 0) || r.avg_rate_kgph || 0;
                      const runHours = rate > 0 ? missingKg / rate : 0;
                      onAddToHolding(r, missingKg, runHours);
                    }}
                    style={{
                      border: "1px solid #c9d4e8",
                      borderRadius: 4,
                      background: missingKg > 0 ? "#636EFA" : "#e5e5e5",
                      color: missingKg > 0 ? "#fff" : "#999",
                      fontSize: 11,
                      fontWeight: 700,
                      width: 22,
                      height: 22,
                      lineHeight: "18px",
                      cursor: missingKg > 0 ? "pointer" : "not-allowed",
                      padding: 0,
                    }}
                  >
                    +
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};

const th: React.CSSProperties = {
  textAlign: "left",
  padding: "6px 10px",
  fontSize: 11,
  fontWeight: 600,
  color: "#666",
  textTransform: "uppercase",
  letterSpacing: 0.5,
};
const td: React.CSSProperties = { padding: "4px 10px" };
const tdRight: React.CSSProperties = { ...td, textAlign: "right", fontVariantNumeric: "tabular-nums" };
