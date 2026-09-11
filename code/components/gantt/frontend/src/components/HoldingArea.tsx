// HoldingArea.tsx — Collapsible panel with draggable holding-area cards,
// grouped into one vertical column per ISO week (planner request
// 2026-08-14): W34 orders stack under a W34 header, biggest tonnage first,
// card text "SKU-W34: 6X12X90, 37.8h" (format from sku_info).

import React, { useState } from "react";
import { useDroppable, useDraggable } from "@dnd-kit/core";
import type { ScheduleBlock } from "../types";
import { skuColor } from "../utils/colors";
import { displayOrderId, isoWeekLabel } from "../utils/layout";
import { CHART, T } from "../utils/theme";

interface Props {
  blocks: ScheduleBlock[];
  anchor: Date;
  skuFormats: Record<string, string>;
  /** Right-click on a card: plain = toggle the SKU highlight, Shift = the
   * placement menu (same convention as calendar blocks, 2026-09-01). */
  onCardContextMenu?: (block: ScheduleBlock, x: number, y: number, shiftKey: boolean) => void;
  /** SKU currently highlighted on the chart: matching cards get the same
   * gold ring, the rest dim — one highlight, both surfaces. */
  highlightSku?: string | null;
}

const HoldingCard: React.FC<{
  block: ScheduleBlock; anchor: Date; skuFormats: Record<string, string>;
  onCardContextMenu?: (block: ScheduleBlock, x: number, y: number, shiftKey: boolean) => void;
  highlightSku?: string | null;
}> = ({ block, anchor, skuFormats, onCardContextMenu, highlightSku }) => {
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
  const isHighlighted = Boolean(highlightSku) && block.sku === highlightSku;
  const isDimmed = Boolean(highlightSku) && block.sku !== highlightSku;

  return (
    <div
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      onContextMenu={(e) => {
        // Right-click: plain toggles the SKU highlight, Shift opens the
        // placement menu — the sandbox decides (user requests 2026-09-01).
        // preventDefault also keeps the browser menu away; dnd-kit's
        // PointerSensor ignores non-primary buttons, so no drag conflict.
        if (!onCardContextMenu) return;
        e.preventDefault();
        e.stopPropagation();
        onCardContextMenu(block, e.clientX, e.clientY, e.shiftKey);
      }}
      style={{
        position: "relative",
        display: "block",
        width: "100%",
        borderRadius: 6,
        border: `1.5px solid ${T.ink2}`,
        background: T.surface2,
        overflow: "hidden",
        cursor: "grab",
        opacity: isDragging ? 0.5 : isDimmed ? 0.3 : 1,
        // Same ring as the chart's highlighted blocks (GanttBlock).
        boxShadow: isHighlighted ? `0 0 0 3px ${CHART.highlightRing}` : undefined,
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
          color: T.ink,
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

export const HoldingArea: React.FC<Props> = ({ blocks, anchor, skuFormats, onCardContextMenu, highlightSku }) => {
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
        border: `2px dashed ${isOver ? T.accent : T.rule}`,
        borderRadius: 10,
        padding: 8,
        background: isOver ? T.accentSoft : T.bg,
        transition: "background 0.15s, border-color 0.15s",
      }}
    >
      <div
        style={{ display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer", marginBottom: expanded ? 6 : 0 }}
        onClick={() => setExpanded(!expanded)}
      >
        <strong style={{ fontSize: 12.5, color: T.ink2, letterSpacing: 0.3 }}>
          HOLDING AREA <span style={{ fontWeight: 500, color: T.ink3 }}>· {blocks.length} card{blocks.length === 1 ? "" : "s"} — demand not yet on the board; drag a card onto a line, or drop a block here to take it off</span>
        </strong>
        <span style={{ fontSize: 11, color: T.ink3 }}>{expanded ? "▲ collapse" : "▼ expand"}</span>
      </div>
      {expanded && (
        <div style={{ display: "flex", gap: 16, alignItems: "flex-start", overflowX: "auto",
                      maxHeight: 300, overflowY: "auto", paddingRight: 4 }}>
          {blocks.length === 0 && (
            <span style={{ fontSize: 12, color: T.ink3, fontStyle: "italic" }}>
              Every demand order is on the board. Drag blocks here to take them off the schedule.
            </span>
          )}
          {weeks.map((wk) => (
            <div key={wk} style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 170 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: T.ink2, borderBottom: `1px solid ${T.rule}`, paddingBottom: 2 }}>
                W{isoWeekLabel(wk, anchor)}
                <span style={{ fontWeight: 400, color: T.ink3 }}> · {byWeek.get(wk)!.length}</span>
              </div>
              {byWeek.get(wk)!.sort(qtyDesc).map((b) => (
                <HoldingCard key={b.id} block={b} anchor={anchor} skuFormats={skuFormats}
                             onCardContextMenu={onCardContextMenu} highlightSku={highlightSku} />
              ))}
            </div>
          ))}
          {loose.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 170 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: T.ink2, borderBottom: `1px solid ${T.rule}`, paddingBottom: 2 }}>
                Unassigned
                <span style={{ fontWeight: 400, color: T.ink3 }}> · {loose.length}</span>
              </div>
              {loose.sort(qtyDesc).map((b) => (
                <HoldingCard key={b.id} block={b} anchor={anchor} skuFormats={skuFormats}
                             onCardContextMenu={onCardContextMenu} highlightSku={highlightSku} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
