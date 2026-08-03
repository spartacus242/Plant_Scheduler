// HoldingArea.tsx — Collapsible panel with draggable holding-area cards.

import React, { useState } from "react";
import { useDroppable, useDraggable } from "@dnd-kit/core";
import type { ScheduleBlock } from "../types";
import { skuColor, skuTextColor } from "../utils/colors";

interface Props {
  blocks: ScheduleBlock[];
}

const HoldingCard: React.FC<{ block: ScheduleBlock }> = ({ block }) => {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `holding_${block.id}`,
    data: { block, fromHolding: true },
  });
  const bg = skuColor(block.sku, block.block_type);
  const fg = skuTextColor(bg);

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
    >
      {block.block_type === "cip" ? "CIP" : block.order_id}: {block.sku}, {block.run_hours}h
    </div>
  );
};

export const HoldingArea: React.FC<Props> = ({ blocks }) => {
  const [expanded, setExpanded] = useState(true);
  const { setNodeRef, isOver } = useDroppable({ id: "holding_area" });

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
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {blocks.length === 0 && (
            <span style={{ fontSize: 12, color: "#999", fontStyle: "italic" }}>
              Drag blocks here to remove from schedule
            </span>
          )}
          {blocks.map((b) => (
            <HoldingCard key={b.id} block={b} />
          ))}
        </div>
      )}
    </div>
  );
};
