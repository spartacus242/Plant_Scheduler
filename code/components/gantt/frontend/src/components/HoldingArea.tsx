// HoldingArea.tsx — Collapsible panel with draggable holding-area cards,
// grouped into one vertical column per ISO week (planner request
// 2026-08-14): W34 orders stack under a W34 header, biggest tonnage first,
// card text "SKU-W34: 6X12X90, 37.8h" (format from sku_info).

import React, { useState } from "react";
import { useDroppable, useDraggable } from "@dnd-kit/core";
import type { ScheduleBlock } from "../types";
import { skuColor, skuTextColor } from "../utils/colors";
import { displayOrderId, isoWeekLabel } from "../utils/layout";

interface Props {
  blocks: ScheduleBlock[];
  anchor: Date;
  skuFormats: Record<string, string>;
}

const HoldingCard: React.FC<{ block: ScheduleBlock; anchor: Date; skuFormats: Record<string, string> }> = ({ block, anchor, skuFormats }) => {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `holding_${block.id}`,
    data: { block, fromHolding: true },
  });
  const bg = skuColor(block.sku, block.block_type);
  const fg = skuTextColor(bg);
  const fmt = skuFormats[block.sku] || block.sku;

  return (
    <div
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px",
        borderRadius: 6,
        background: bg,
        color: fg,
        fontSize: 12,
        fontWeight: 600,
        cursor: "grab",
        opacity: isDragging ? 0.5 : 1,
        whiteSpace: "nowrap",
      }}
      title={`${block.sku} ${block.sku_description || ""} — ${(block.qty_kg ?? 0).toLocaleString()} kg`}
    >
      {block.block_type === "cip"
        ? `CIP, ${block.run_hours.toFixed(1)}h`
        : `${displayOrderId(block.order_id, anchor)}: ${fmt}, ${block.run_hours.toFixed(1)}h`}
    </div>
  );
};

/** Solver week index parsed from "<sku>-W<k>" order ids; null when the block
 * was dragged off the board (no week suffix). */
function weekIndexOf(orderId: string): number | null {
  const m = /-W(\d+)$/.exec(String(orderId ?? ""));
  return m ? parseInt(m[1], 10) : null;
}

export const HoldingArea: React.FC<Props> = ({ blocks, anchor, skuFormats }) => {
  const [expanded, setExpanded] = useState(true);
  const { setNodeRef, isOver } = useDroppable({ id: "holding_area" });

  // Group into week columns; unsuffixed blocks land in "Unassigned".
  const byWeek = new Map<number, ScheduleBlock[]>();
  const loose: ScheduleBlock[] = [];
  for (const b of blocks) {
    const wk = weekIndexOf(b.order_id);
    if (wk === null) {
      loose.push(b);
    } else {
      if (!byWeek.has(wk)) byWeek.set(wk, []);
      byWeek.get(wk)!.push(b);
    }
  }
  const qtyDesc = (a: ScheduleBlock, b: ScheduleBlock) =>
    (b.qty_kg ?? 0) - (a.qty_kg ?? 0) || b.run_hours - a.run_hours;
  const weeks = [...byWeek.keys()].sort((a, b) => a - b);

  return (
    <div
      ref={setNodeRef}
      style={{
        border: `2px dashed ${isOver ? "#636EFA" : "#ccc"}`,
        borderRadius: 8,
        padding: 8,
        background: isOver ? "#f0f4ff" : "#fafafa",
        transition: "background 0.15s, border-color 0.15s",
      }}
    >
      <div
        style={{ display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer", marginBottom: expanded ? 6 : 0 }}
        onClick={() => setExpanded(!expanded)}
      >
        <strong style={{ fontSize: 13 }}>
          Holding Area [{blocks.length} items]
        </strong>
        <span style={{ fontSize: 11, color: "#666" }}>{expanded ? "▲ collapse" : "▼ expand"}</span>
      </div>
      {expanded && (
        <div style={{ display: "flex", gap: 16, alignItems: "flex-start", overflowX: "auto" }}>
          {blocks.length === 0 && (
            <span style={{ fontSize: 12, color: "#999", fontStyle: "italic" }}>
              Drag blocks here to remove from schedule
            </span>
          )}
          {weeks.map((wk) => (
            <div key={wk} style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 170 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#455a64", borderBottom: "1px solid #cfd8dc", paddingBottom: 2 }}>
                W{isoWeekLabel(wk, anchor)}
                <span style={{ fontWeight: 400, color: "#90a4ae" }}> · {byWeek.get(wk)!.length}</span>
              </div>
              {byWeek.get(wk)!.sort(qtyDesc).map((b) => (
                <HoldingCard key={b.id} block={b} anchor={anchor} skuFormats={skuFormats} />
              ))}
            </div>
          ))}
          {loose.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 170 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#455a64", borderBottom: "1px solid #cfd8dc", paddingBottom: 2 }}>
                Unassigned
                <span style={{ fontWeight: 400, color: "#90a4ae" }}> · {loose.length}</span>
              </div>
              {loose.sort(qtyDesc).map((b) => (
                <HoldingCard key={b.id} block={b} anchor={anchor} skuFormats={skuFormats} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
