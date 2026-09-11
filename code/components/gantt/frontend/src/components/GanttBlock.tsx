// GanttBlock.tsx — Single draggable/resizable block (SVG rect).
//
// Callbacks hand back the BLOCK OBJECT, never a bare id: split MO pieces
// share one id (calendar_blocks.csv `;split`), so the dnd-kit draggable id,
// the resize target, the popover and the menu all key on the piece
// (blockIdentity.blockKey = id|start_hour). Supply chip / hatch / tick
// (contract 2026-09-01 §9) render only when a `supply` verdict is passed —
// the parent passes none without a stock payload or once acknowledged.

import React, { useCallback } from "react";
import { useDraggable } from "@dnd-kit/core";
import type { ScheduleBlock } from "../types";
import { hourToX, LINE_HEIGHT, hourToStamp } from "../utils/layout";
import { blockFill, skuTextColor, blockLabel, isHatchedType } from "../utils/colors";
import { blockKey } from "../utils/blockIdentity";
import type { Supply } from "../utils/stockRisk";
import { chipFor, humanText, stampFor } from "../utils/supplyGlue";
import { CHART, T } from "../utils/theme";

interface Props {
  block: ScheduleBlock;
  lineIndex: number;
  viewStart: number;
  hourWidth: number;
  /** Planning anchor, so the hover tooltip reads as date + time. */
  anchor: Date;
  isResizing: boolean;
  previewStart?: number;
  previewEnd?: number;
  isHighlighted: boolean;
  /** True when ANOTHER SKU is highlighted: this block fades back so the
   * highlighted campaign stands out (user request 2026-08-14). */
  isDimmed?: boolean;
  /** Vertical offset within the row (0 unless the block sits on one side). */
  slotY?: number;
  /** Height of the slot; the full row height unless one-sided. */
  slotHeight?: number;
  /** "A" / "B" when the block occupies a single side of a double line. */
  side?: string | null;
  /** Supply verdict for THIS piece (null: no payload, plain OK is still a
   * Supply, acknowledged keys come through as null). */
  supply?: Supply | null;
  onResizeStart: (block: ScheduleBlock, edge: "left" | "right", startH: number, endH: number, clientX: number, hourWidth: number) => void;
  onContextMenu: (e: React.MouseEvent, block: ScheduleBlock) => void;
  onClick: (block: ScheduleBlock) => void;
  /** Committed MO / pinned / locked-window: click opens the popup, but the
   * block cannot be picked up at all (cursor shows not-allowed). */
  immovable?: boolean;
}

/** Rough pixel width of chip text at 11px: emoji ~13px, glyphs ~6.2px. */
function chipTextWidth(text: string): number {
  let w = 0;
  for (const ch of text) w += ch.codePointAt(0)! > 0x2000 ? 13 : 6.2;
  return w;
}

const CHIP_STYLE = {
  warn: { bg: "#ef6c00", fg: "#fff", stroke: "none" },
  crit: { bg: "#c62828", fg: "#fff", stroke: "none" },
  muted: { bg: "rgba(255,255,255,0.8)", fg: "#455a64", stroke: "#90a4ae" },
} as const;

export const GanttBlock: React.FC<Props> = ({
  block, lineIndex, viewStart, hourWidth, anchor, isResizing, previewStart, previewEnd,
  isHighlighted, isDimmed = false, slotY = 0, slotHeight, side = null, supply = null,
  onResizeStart, onContextMenu, onClick, immovable = false,
}) => {
  const key = blockKey(block);
  const { attributes, listeners, setNodeRef: setDragRef, transform, isDragging } = useDraggable({
    // The piece, not the shared id: two pieces registered under one id
    // would collide in dnd-kit's registry.
    id: key,
    data: { block },
    disabled: immovable,
  });
  const setNodeRef = setDragRef as unknown as React.Ref<SVGGElement>;

  const startH = isResizing ? (previewStart ?? block.start_hour) : block.start_hour;
  const endH = isResizing ? (previewEnd ?? block.end_hour) : block.end_hour;
  const x = hourToX(startH, viewStart, hourWidth);
  const w = (endH - startH) * hourWidth;
  // A one-sided block is inset into its half of the row; a full-row block keeps
  // the original 4px padding top and bottom.
  const rowH = slotHeight ?? LINE_HEIGHT;
  const pad = rowH >= LINE_HEIGHT ? 4 : 2;
  const y = slotY + pad;
  const h = Math.max(6, rowH - pad * 2);
  // Completed manprg history renders grey and read-only (user 2026-08-14).
  // Downtime / maintenance / contractor windows are hatched constraints
  // (pattern defs live in GanttChart); projected cleans are outlined and
  // lighter than scheduled ones so a forecast never reads as plant fact.
  const hatched = isHatchedType(block.block_type);
  const isProjectedCip = block.block_type === "cip" && (block.attrs ?? "").includes("cip_projected");
  const bg = block.completed ? "#B9C2CB" : blockFill(block.sku, block.block_type, block.attrs);
  const fg = block.completed ? "#4E5A67" : hatched ? T.ink : skuTextColor(bg);

  const handleLeftResize = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
      e.preventDefault();
      onResizeStart(block, "left", block.start_hour, block.end_hour, e.clientX, hourWidth);
    },
    [block, hourWidth, onResizeStart],
  );

  const handleRightResize = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
      e.preventDefault();
      onResizeStart(block, "right", block.start_hour, block.end_hour, e.clientX, hourWidth);
    },
    [block, hourWidth, onResizeStart],
  );

  const handleContext = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      // Never bubble to the chart background — that would ALSO open the
      // blank-space SKU picker on top of the block menu.
      e.stopPropagation();
      onContextMenu(e, block);
    },
    [block, onContextMenu],
  );

  const handleClick = useCallback(
    (e: React.MouseEvent) => { e.stopPropagation(); onClick(block); },
    [block, onClick],
  );

  const dragX = isDragging && transform ? transform.x : 0;
  const dragY = isDragging && transform ? transform.y : 0;

  const rawDesc = block.sku_description || "";
  const desc = /^nan$/i.test(rawDesc.trim()) ? "" : rawDesc;  // defensive: never render 'nan'
  // Committed manprg MOs (current_state overlay) read "MO <sku>" and render
  // slightly muted, so already-planned plant work is visually distinct from
  // blocks placed by the seed/solver (user request 2026-08-14).
  const isCommittedMo = (block.attrs ?? "").includes("current_state:")
    && block.block_type === "sku";
  const isPinned = Boolean(block.pinned);
  const baseLabel0 = blockLabel(block.block_type, block.sku, block.label);
  // 📌 prefix: pinned blocks read as fixed at a glance, like "MO " does for
  // committed manprg work.
  const baseLabel = (isPinned ? "📌 " : "")
    + (isCommittedMo ? `MO ${baseLabel0}` : baseLabel0);
  const hoursTxt = (Number.isFinite(block.run_hours) ? block.run_hours : 0).toFixed(1);
  // Production reads TONNAGE, not hours (planner request 2026-09-11): the
  // hours depend on the line's rate, the kg need does not — and kg adds up
  // against the week cards. Hours stay in the tooltip and the popup. A block
  // with no known kg falls back to hours rather than showing 0.0t.
  const kgNum = Number(block.qty_kg);
  const hasKg = Number.isFinite(kgNum) && kgNum > 0;
  const sizeTxt = hasKg ? `${(kgNum / 1000).toFixed(1)}t` : `${hoursTxt}h`;

  // Supply chip right of the label (§9); it takes its width out of the
  // label's budget and is dropped on blocks too narrow to hold it.
  const chip = block.block_type === "sku" ? chipFor(supply) : null;
  const chipW = chip ? Math.ceil(chipTextWidth(chip.text)) + 10 : 0;
  const showChip = chip !== null && w >= chipW + 28;
  const supplyLine = supply && block.block_type === "sku" ? humanText(supply, stampFor(anchor)) : "";

  // "i" badge (planner request 2026-09-11): a CIP that carries a cip_info
  // comment shows a small i at its right end. The comment itself is in the
  // hover tooltip (desc, above) and in the block popup's Description row.
  const infoBadge = block.block_type === "cip" && desc.trim().length > 0;
  // Cleans are short (6 h ≈ 13 px at "fit 3 weeks"): the badge shows from
  // 11 px, shrinks to the block, and takes precedence over the label, which
  // is dropped when both no longer fit (charBudget below).
  const badgeW = infoBadge ? 16 : 0;
  const showBadge = infoBadge && w >= 11;

  // Estimate available characters from pixel width (~6.5px per char at 11px font)
  const charBudget = Math.floor((w - 12 - (showChip ? chipW + 4 : 0) - (showBadge ? badgeW : 0)) / 6.5);
  let label: string;
  if (block.block_type === "cip" || block.block_type === "line_down" || block.block_type === "maintenance" || block.block_type === "contractor") {
    label = charBudget <= 0 ? "" : (baseLabel.length <= charBudget ? baseLabel : baseLabel.slice(0, Math.max(charBudget - 1, 1)) + "…");
  } else if (charBudget <= 0) {
    label = "";
  } else {
    const withHours = `${baseLabel} (${sizeTxt})`;
    const withDesc = desc ? `${baseLabel} ${desc} (${sizeTxt})` : withHours;
    if (withDesc.length <= charBudget) {
      label = withDesc;
    } else if (withHours.length <= charBudget) {
      label = withHours;
    } else if (baseLabel.length <= charBudget) {
      label = baseLabel;
    } else {
      label = baseLabel.slice(0, Math.max(charBudget - 1, 1)) + "…";
    }
  }

  // Hover tooltip: wall-clock start/end, duration, live completion, cases left.
  const tooltip = [
    baseLabel,
    desc,
    side ? `${block.line_name} (side ${side} only - half rate)` : `${block.line_name}`,
    `${hourToStamp(startH, anchor)} -> ${hourToStamp(endH, anchor)} (${(endH - startH).toFixed(1)}h)`,
    hasKg && block.block_type === "sku" ? `${Math.round(kgNum).toLocaleString()} kg (${(kgNum / 1000).toFixed(1)} t)` : "",
    typeof block.completion_pct === "number"
      ? `Completion: ${block.completion_pct.toFixed(1)}%${typeof block.cases_left === "number" ? ` · ${block.cases_left.toLocaleString()} cases left` : ""}`
      : "",
    isPinned ? "Pinned for the solver — unpin in the block popup to move it" : "",
    supplyLine,
  ].filter(Boolean).join("\n");

  // Pinned: a firm dark outline so fixed blocks read distinct from free ones.
  const strokeColor = isDragging ? T.ink : isPinned ? T.ink2 : isProjectedCip ? T.ink3 : hatched ? bg : "none";
  const strokeW = isDragging ? 2 : isPinned ? 1.8 : (isProjectedCip || hatched) ? 1 : 0;

  // SHORT: body solid up to the hour the component runs out, hatched after.
  // DEPENDENT: a tick where the binding truck lands when that is mid-run
  // (a truck landing before the start has nothing to mark on the body).
  const hatchFrom = supply && supply.verdict === "SHORT" && supply.depletion_h != null
    && supply.depletion_h < endH
    ? Math.max(startH, supply.depletion_h) : null;
  const tickAt = supply && supply.verdict === "DEPENDENT" && supply.binding
    && supply.binding.ready_h > startH && supply.binding.ready_h < endH
    ? supply.binding.ready_h : null;
  const hatchId = `sup-hatch-${key.replace(/[^A-Za-z0-9_-]/g, "_")}`;

  return (
    <g
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      data-block-id={block.id}
      data-block-key={key}
      transform={`translate(${dragX}, ${dragY})`}
      style={{
        cursor: immovable ? "not-allowed" : isDragging ? "grabbing" : "grab",
        opacity: isDragging ? 0.6 : isDimmed ? 0.25 : 1,
        // committed manprg MOs: muted so new (seed/solver/planner) blocks pop
        filter: isCommittedMo && !isDragging && !isDimmed
          ? "saturate(0.45) brightness(1.06)" : undefined,
        transition: "opacity 120ms ease",
      }}
      onContextMenu={handleContext}
      onClick={handleClick}
    >
      <title>{tooltip}</title>
      {/* Highlight glow */}
      {isHighlighted && (
        <rect
          x={x - 2}
          y={y - 2}
          width={Math.max(w, 2) + 4}
          height={h + 4}
          rx={6}
          fill="none"
          stroke={CHART.highlightRing}
          strokeWidth={2.5}
          opacity={0.9}
        />
      )}
      <rect
        x={x}
        y={y}
        width={Math.max(w, 2)}
        height={h}
        rx={4}
        fill={hatched ? `url(#fs-hatch-${block.block_type})` : bg}
        stroke={strokeColor}
        strokeWidth={strokeW}
        strokeDasharray={isProjectedCip ? "4 3" : undefined}
      />
      {/* Live completion fill (manprg): left-to-right progress on production bars */}
      {typeof block.completion_pct === "number" && block.completion_pct > 0 && (() => {
        const pct = Math.min(Math.max(block.completion_pct, 0), 100);
        const fw = Math.max((w * pct) / 100, 0);
        return (
          <>
            <rect
              x={x}
              y={y}
              width={fw}
              height={h}
              rx={4}
              fill="rgba(255,255,255,0.35)"
              pointerEvents="none"
            />
            <line x1={x + fw} y1={y} x2={x + fw} y2={y + h} stroke="rgba(255,255,255,0.7)" strokeWidth={1.5} pointerEvents="none" />
          </>
        );
      })()}
      {hatchFrom !== null && (() => {
        const hx = hourToX(hatchFrom, viewStart, hourWidth);
        const hw = Math.max(0, x + Math.max(w, 2) - hx);
        if (hw <= 0) return null;
        return (
          <>
            <defs>
              <pattern id={hatchId} patternUnits="userSpaceOnUse" width={6} height={6} patternTransform="rotate(45)">
                <rect width={6} height={6} fill="rgba(183,28,28,0.16)" />
                <line x1={0} y1={0} x2={0} y2={6} stroke="#b71c1c" strokeWidth={2} opacity={0.75} />
              </pattern>
            </defs>
            <rect x={hx} y={y} width={hw} height={h} rx={hw < w ? 0 : 4} fill={`url(#${hatchId})`} pointerEvents="none" />
            <line x1={hx} y1={y} x2={hx} y2={y + h} stroke="#b71c1c" strokeWidth={1.5} pointerEvents="none" />
          </>
        );
      })()}
      {tickAt !== null && (() => {
        const tx = hourToX(tickAt, viewStart, hourWidth);
        return (
          <g pointerEvents="none">
            <line x1={tx} y1={y - 2} x2={tx} y2={y + h + 2} stroke="#ef6c00" strokeWidth={2} />
            <circle cx={tx} cy={y - 1} r={2.5} fill="#ef6c00" />
          </g>
        );
      })()}
      {w > 20 && label && (
        <text
          x={x + 6}
          y={y + h / 2 + 1}
          textAnchor="start"
          dominantBaseline="middle"
          fontSize={h < LINE_HEIGHT / 2 ? 8 : w > 60 ? 11 : 9}
          fill={fg}
          fontWeight={600}
          pointerEvents="none"
          style={{ userSelect: "none" }}
        >
          {label}
        </text>
      )}
      {showChip && chip && (() => {
        const st = CHIP_STYLE[chip.tone];
        const ch = Math.max(12, Math.min(18, h - 6));
        const cx = x + Math.max(w, 2) - chipW - 4;
        const cy = y + (h - ch) / 2;
        return (
          <g pointerEvents="none" data-testid="supply-chip" data-tone={chip.tone}>
            <rect x={cx} y={cy} width={chipW} height={ch} rx={ch / 2} fill={st.bg}
                  stroke={st.stroke} strokeWidth={st.stroke === "none" ? 0 : 1} />
            <text x={cx + chipW / 2} y={cy + ch / 2 + 1} textAnchor="middle" dominantBaseline="middle"
                  fontSize={h < LINE_HEIGHT / 2 ? 8 : 10} fontWeight={700} fill={st.fg}
                  style={{ userSelect: "none" }}>
              {chip.text}
            </text>
          </g>
        );
      })()}
      {showBadge && (() => {
        const bw = Math.max(w, 2);
        const r = Math.max(4, Math.min(7, (h - 6) / 2, (bw - 3) / 2));
        const bx = bw < 2 * r + 10 ? x + bw / 2 : x + bw - r - 4;
        const by = y + h / 2;
        return (
          <g pointerEvents="none" data-testid="cip-info">
            <circle cx={bx} cy={by} r={r} fill="#FFFFFF" stroke={T.ink2} strokeWidth={1} />
            <text x={bx} y={by + 0.5} textAnchor="middle" dominantBaseline="middle"
                  fontSize={r * 1.6} fontWeight={700} fontStyle="italic" fontFamily="Georgia, 'Times New Roman', serif"
                  fill={T.ink} style={{ userSelect: "none" }}>
              i
            </text>
          </g>
        );
      })()}
      {/* Resize handles: adaptive width — generous on wide blocks (easier to
          grab than a fixed 8px sliver), but never more than a third of a
          narrow block so its body stays draggable. */}
      {!immovable && (() => {
        const hw = Math.min(14, Math.max(5, Math.max(w, 2) / 3));
        return (
          <>
            <rect x={x} y={y} width={hw} height={h} fill="transparent" style={{ cursor: "ew-resize" }} onPointerDown={handleLeftResize} />
            <rect x={x + Math.max(w, 2) - hw} y={y} width={hw} height={h} fill="transparent" style={{ cursor: "ew-resize" }} onPointerDown={handleRightResize} />
          </>
        );
      })()}
    </g>
  );
};
