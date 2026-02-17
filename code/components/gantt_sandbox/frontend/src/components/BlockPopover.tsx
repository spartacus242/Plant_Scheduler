// BlockPopover.tsx — Click-to-view detail popover.

import React from "react";
import type { ScheduleBlock } from "../types";

interface Props {
  block: ScheduleBlock | null;
  x: number;
  y: number;
  rate: number;
  onClose: () => void;
}

export const BlockPopover: React.FC<Props> = ({ block, x, y, rate, onClose }) => {
  if (!block) return null;

  const qty = rate > 0 ? Math.round(rate * block.run_hours) : "—";

  return (
    <div
      style={{
        position: "fixed",
        left: x,
        top: y,
        background: "#fff",
        border: "1px solid #ccc",
        borderRadius: 8,
        padding: "10px 14px",
        boxShadow: "0 4px 16px rgba(0,0,0,0.15)",
        zIndex: 1000,
        minWidth: 180,
        fontSize: 13,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <strong>{block.block_type === "cip" ? "CIP" : block.order_id}</strong>
        <span style={{ cursor: "pointer", fontWeight: 700, color: "#888" }} onClick={onClose}>
          ×
        </span>
      </div>
      <table style={{ fontSize: 12, lineHeight: 1.8 }}>
        <tbody>
          <tr><td style={{ color: "#888", paddingRight: 12 }}>Line</td><td>{block.line_name}</td></tr>
          <tr><td style={{ color: "#888", paddingRight: 12 }}>SKU</td><td>{block.sku}</td></tr>
          <tr><td style={{ color: "#888", paddingRight: 12 }}>Start</td><td>h{block.start_hour}</td></tr>
          <tr><td style={{ color: "#888", paddingRight: 12 }}>End</td><td>h{block.end_hour}</td></tr>
          <tr><td style={{ color: "#888", paddingRight: 12 }}>Duration</td><td>{block.run_hours}h</td></tr>
          <tr><td style={{ color: "#888", paddingRight: 12 }}>Rate</td><td>{rate > 0 ? `${rate} UPH` : "N/A"}</td></tr>
          <tr><td style={{ color: "#888", paddingRight: 12 }}>Est. Qty</td><td>{qty}</td></tr>
          <tr><td style={{ color: "#888", paddingRight: 12 }}>Type</td><td>{block.block_type}</td></tr>
        </tbody>
      </table>
    </div>
  );
};
