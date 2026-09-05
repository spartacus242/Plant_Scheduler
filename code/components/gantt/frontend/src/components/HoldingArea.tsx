// HoldingArea.tsx — Collapsible panel with draggable holding-area cards,
// grouped into one vertical column per ISO week (planner request
// 2026-08-14): W34 orders stack under a W34 header, biggest tonnage first,
// card text "SKU-W34: 6X12X90, 37.8h" (format from sku_info).

import React, { useMemo, useState } from "react";
import { useDroppable, useDraggable } from "@dnd-kit/core";
import type { DemandTarget, ScheduleBlock, StockArgs } from "../types";
import { skuColor } from "../utils/colors";
import { displayOrderId, isoWeekLabel } from "../utils/layout";
import { meanCapableRate } from "../utils/holdingDerive";
import { blockKey } from "../utils/blockIdentity";
import type { Timelines } from "../utils/stockRisk";
import { earliestSafeStartFor, safeStartPill, type SafeStartPill, type StampFn } from "../utils/supplyGlue";
import { SafeStartTag } from "./SkuPickerPopover";

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
  /** Supply timeline (contract 2026-09-01 §9): with a payload every
   * production card carries a before-placement pill — the earliest clear
   * start for the card's kg at its mean rate, judged alone against the
   * placed board. stock null/absent = no pill anywhere. */
  stock?: StockArgs | null;
  supplyTimelines?: Timelines | null;
  caps?: Record<string, Record<string, number>>;
  lockedThroughH?: number | null;
  /** "Now" in board hours (sandbox-supplied, refreshed with the board): the
   * pills judge from max(now, lock) like the Place popover's rows, never
   * from a lock boundary already in the past. */
  nowH?: number | null;
  demandTargets?: DemandTarget[];
  supplyStamp?: StampFn | null;
}

const HoldingCard: React.FC<{
  block: ScheduleBlock; anchor: Date; skuFormats: Record<string, string>;
  onCardContextMenu?: (block: ScheduleBlock, x: number, y: number, shiftKey: boolean) => void;
  highlightSku?: string | null;
  pill?: SafeStartPill | null;
}> = ({ block, anchor, skuFormats, onCardContextMenu, highlightSku, pill = null }) => {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    // The piece key, not the id: two parked pieces of one split MO share
    // an id and would collide in dnd-kit's registry. The drop resolves the
    // card from the carried `block`, never from this id.
    id: `holding_${blockKey(block)}`,
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
        border: "2px solid #000",
        background: "#f1f3f5",
        overflow: "hidden",
        cursor: "grab",
        opacity: isDragging ? 0.5 : isDimmed ? 0.3 : 1,
        // Same gold ring as the chart's highlighted blocks (GanttBlock).
        boxShadow: isHighlighted ? "0 0 0 3px #FFD700" : undefined,
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
      {pill && (
        <div style={{ position: "relative", padding: "0 10px 4px", lineHeight: "16px" }}>
          <SafeStartTag pill={pill} />
        </div>
      )}
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

export const HoldingArea: React.FC<Props> = ({
  blocks, anchor, skuFormats, onCardContextMenu, highlightSku,
  stock = null, supplyTimelines = null, caps, lockedThroughH = null, demandTargets, supplyStamp = null,
  nowH = null,
}) => {
  const [expanded, setExpanded] = useState(true);
  const { setNodeRef, isOver } = useDroppable({ id: "holding_area" });

  // One pill per production card, judged alone against the placed board:
  // the card's kg at its mean capable rate (the basis of its hours; the
  // card's own kg/h when no line is capable), from the first plannable
  // hour (max of now and the lock boundary, else hour 0 — the Place
  // popover's base, so card and rows never disagree), with "after due
  // window" against the order's due_end_hour. Recomputed only when the
  // cards, the board's timelines or the payload change.
  const pills = useMemo<Map<string, SafeStartPill> | null>(() => {
    if (!stock || !supplyTimelines || !supplyStamp) return null;
    const dueEnd = new Map<string, number>();
    for (const d of demandTargets ?? []) {
      if (d.due_end_hour != null) dueEnd.set(d.order_id, d.due_end_hour);
    }
    const fromH = Math.max(0, nowH ?? 0, lockedThroughH ?? 0);
    // Keyed by piece (blockKey): two parked pieces of one split MO share
    // an id but carry different kg, so each needs its own pill.
    const out = new Map<string, SafeStartPill>();
    for (const b of blocks) {
      if (b.block_type !== "sku") continue;
      const kg = b.qty_kg ?? 0;
      const rate = meanCapableRate(caps ?? {}, b.sku)
        || (kg > 0 && b.run_hours > 0 ? kg / b.run_hours : 0);
      const res = earliestSafeStartFor(b.sku, kg, rate, fromH, supplyTimelines, stock,
                                       { fallbackHours: b.run_hours });
      out.set(blockKey(b), safeStartPill(res, supplyStamp, dueEnd.get(b.order_id) ?? null));
    }
    return out;
  }, [blocks, stock, supplyTimelines, caps, lockedThroughH, nowH, demandTargets, supplyStamp]);

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
                <HoldingCard key={blockKey(b)} block={b} anchor={anchor} skuFormats={skuFormats}
                             onCardContextMenu={onCardContextMenu} highlightSku={highlightSku}
                             pill={pills?.get(blockKey(b)) ?? null} />
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
                <HoldingCard key={blockKey(b)} block={b} anchor={anchor} skuFormats={skuFormats}
                             onCardContextMenu={onCardContextMenu} highlightSku={highlightSku}
                             pill={pills?.get(blockKey(b)) ?? null} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
