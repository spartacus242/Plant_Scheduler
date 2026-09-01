// HoldingArea.tsx — Collapsible panel with draggable holding-area cards,
// grouped into one vertical column per ISO week (planner request
// 2026-08-14): W34 orders stack under a W34 header, biggest tonnage first,
// card text "SKU-W34: 6X12X90, 37.8h" (format from sku_info).

import React, { useState } from "react";
import { useDroppable, useDraggable } from "@dnd-kit/core";
import type { ScheduleBlock } from "../types";
import { skuColor } from "../utils/colors";
import { displayOrderId, isoWeekLabel } from "../utils/layout";

interface Props {
  blocks: ScheduleBlock[];
  anchor: Date;
  skuFormats: Record<string, string>;
  /** Right-click on a card: open the line/placement menu. */
  onCardContextMenu?: (block: ScheduleBlock, x: number, y: number) => void;
}

const HoldingCard: React.FC<{
  block: ScheduleBlock; anchor: Date; skuFormats: Record<string, string>;
  onCardContextMenu?: (block: ScheduleBlock, x: number, y: number) => void;
}> = ({ block, anchor, skuFormats, onCardContextMenu }) => {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `holding_${block.id}`,
    data: { block, fromHolding: true },
  });
  const bg = skuColor(block.sku, block.block_type);
  const fmt = skuFormats[block.sku] || block.sku;
  // Fill width ∝ remaining hours, ceiling 100h = full width (user rule
  // 2026-09-01): the card frame spans the whole column with a bold black
  // border, only the COLOR is proportional, and the text rides the full
  // width in black. Unfilled area stays light so the text remains legible.
  const fillPct = Math.max(2, Math.min(100, (block.run_hours / FILL_CEILING_H) * 100));

  return (
    <div
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      onContextMenu={(e) => {
        // Right-click: line/placement menu (user request 2026-09-01).
        // preventDefault also keeps the browser menu away; dnd-kit's
        // PointerSensor ignores non-primary buttons, so no drag conflict.
        if (!onCardContextMenu) return;
        e.preventDefault();
        e.stopPropagation();
        onCardContextMenu(block, e.clientX, e.clientY);
      }}
      style={{
        position: "relative",
        display: "block",
        width: "100%",
        borderRadius: 6,
        border: "2px solid #000",
        background: "#f1f3f5",
        overflow: "hidden",
        cursor: "grab",
        opacity: isDragging ? 0.5 : 1,
        boxSizing: "border-box",
      }}
      title={`${block.sku} ${block.sku_description || ""} — ${(block.qty_kg ?? 0).toLocaleString()} kg`}
    >
      <div
        style={{
          position: "absolute",
          top: 0, left: 0, bottom: 0,
          width: `${fillPct}%`,
          background: bg,
        }}
      />
      <div
        style={{
          position: "relative",
          padding: "4px 10px",
          fontSize: 12,
          fontWeight: 600,
          color: "#000",
          whiteSpace: "nowrap",
        }}
      >
        {block.block_type === "cip"
          ? `CIP, ${block.run_hours.toFixed(1)}h`
          : `${displayOrderId(block.order_id, anchor)}: ${fmt}, ${block.run_hours.toFixed(1)}h`}
      </div>
    </div>
  );
};

/** Hours at (and beyond) which a holding card's color fill spans the full
 * column width. */
const FILL_CEILING_H = 100;

/** Solver week index parsed from "<sku>-W<k>" order ids; null when the block
 * was dragged off the board (no week suffix). */
function weekIndexOf(orderId: string): number | null {
  const m = /-W(\d+)$/.exec(String(orderId ?? ""));
  return m ? parseInt(m[1], 10) : null;
}

export const HoldingArea: React.FC<Props> = ({ blocks, anchor, skuFormats, onCardContextMenu }) => {
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
                <HoldingCard key={b.id} block={b} anchor={anchor} skuFormats={skuFormats}
                             onCardContextMenu={onCardContextMenu} />
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
                <HoldingCard key={b.id} block={b} anchor={anchor} skuFormats={skuFormats}
                             onCardContextMenu={onCardContextMenu} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
