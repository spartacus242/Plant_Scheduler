// AdherenceTable.tsx — Live-updating sorted table. Click a row to highlight that SKU.

import React from "react";
import type { AdherenceRow } from "../types";

interface Props {
  rows: AdherenceRow[];
  highlightSku: string | null;
  onSkuClick: (sku: string | null) => void;
}

const statusColors: Record<string, string> = {
  MET: "#00CC96",
  UNDER: "#EF553B",
  OVER: "#FFA15A",
};

export const AdherenceTable: React.FC<Props> = ({ rows, highlightSku, onSkuClick }) => {
  return (
    <div style={{ maxHeight: 260, overflowY: "auto", border: "1px solid #e0e0e5", borderRadius: 8 }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr style={{ background: "#f7f7fa", position: "sticky", top: 0 }}>
            <th style={th}>SKU</th>
            <th style={th}>Order</th>
            <th style={th}>Min Qty</th>
            <th style={th}>Sched Qty</th>
            <th style={th}>% Adh</th>
            <th style={th}>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isSelected = highlightSku === r.sku;
            return (
              <tr
                key={r.order_id}
                style={{
                  borderBottom: "1px solid #f0f0f0",
                  background: isSelected ? "#fff8dc" : "transparent",
                  cursor: "pointer",
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onSkuClick(isSelected ? null : r.sku);
                }}
              >
                <td style={{ ...td, fontWeight: isSelected ? 700 : 400 }}>{r.sku}</td>
                <td style={td}>{r.order_id}</td>
                <td style={tdRight}>{r.qty_min.toLocaleString()}</td>
                <td style={tdRight}>{r.scheduled_qty.toLocaleString()}</td>
                <td style={tdRight}>{r.pct_adherence}%</td>
                <td style={{ ...td, color: statusColors[r.status] ?? "#333", fontWeight: 600 }}>
                  {r.status}
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
