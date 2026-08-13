// BlockPopover.tsx — Click-to-view detail popover with editable fields.
//
// The planner can TYPE exact values instead of pixel-dragging: start (wall
// clock), duration (h) and tonnage (kg). Duration and tonnage are linked
// through the block's implied rate (qty_kg / run_hours, falling back to the
// capability-table rate): edit one and the other follows, then either can be
// overridden before Apply. Apply commits the displayed values literally —
// validation (overlap, locked, min-run) happens in the GanttSandbox handler,
// which keeps the popover open on rejection so the values can be corrected.

import React, { useMemo, useState } from "react";
import type { ScheduleBlock } from "../types";
import { isWindowBlock } from "../types";
import { hourToStamp } from "../utils/layout";

export interface BlockEdit {
  startHour: number;
  durationH: number;
  /** kg for the edited block; null = unknown (window blocks never carry kg). */
  qtyKg: number | null;
}

interface Props {
  block: ScheduleBlock | null;
  x: number;
  y: number;
  rate: number;
  /** Planning anchor, so hour offsets render as wall-clock date + time. */
  anchor: Date;
  onClose: () => void;
  /** Commit typed edits. Returns true when accepted (popover closes). */
  onApply?: (blockId: string, edit: BlockEdit) => boolean;
}

const LABEL: React.CSSProperties = { color: "#888", paddingRight: 12 };
const INPUT: React.CSSProperties = {
  width: 130,
  fontSize: 12,
  padding: "2px 4px",
  border: "1px solid #ccc",
  borderRadius: 4,
  boxSizing: "border-box",
};

function toLocalInput(anchor: Date, hour: number): string {
  const d = new Date(anchor.getTime() + hour * 3600_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInput(anchor: Date, value: string): number | null {
  const t = new Date(value).getTime();
  if (Number.isNaN(t)) return null;
  return (t - anchor.getTime()) / 3600_000;
}

const round1 = (v: number) => Math.round(v * 10) / 10;

export const BlockPopover: React.FC<Props> = ({ block, x, y, rate, anchor, onClose, onApply }) => {
  // Draft field state, (re)seeded whenever a different block is opened.
  const [draft, setDraft] = useState<{ id: string; start: string; dur: string; qty: string } | null>(null);

  const seeded = useMemo(() => {
    if (!block) return null;
    return {
      id: block.id,
      start: toLocalInput(anchor, block.start_hour),
      dur: String(round1(block.run_hours)),
      qty: block.qty_kg ? String(round1(block.qty_kg)) : "",
    };
  }, [block, anchor]);

  if (!block || !seeded) return null;
  const d = draft && draft.id === block.id ? draft : seeded;

  const isWindow = isWindowBlock(block.block_type);
  const editable = Boolean(onApply) && !block.locked;

  // Implied kg/h linking duration <-> tonnage: prefer the block's own numbers
  // (they carry the solver's real decomposition), fall back to the table rate.
  const impliedRate =
    block.qty_kg && block.run_hours > 0 ? block.qty_kg / block.run_hours : rate > 0 ? rate : null;

  const setDur = (v: string) => {
    const dur = parseFloat(v);
    const next = { ...d, dur: v };
    if (!isWindow && impliedRate && Number.isFinite(dur) && dur > 0) {
      next.qty = String(round1(dur * impliedRate));
    }
    setDraft(next);
  };
  const setQty = (v: string) => {
    const kg = parseFloat(v);
    const next = { ...d, qty: v };
    if (impliedRate && Number.isFinite(kg) && kg > 0) {
      next.dur = String(round1(kg / impliedRate));
    }
    setDraft(next);
  };

  const apply = () => {
    if (!onApply) return;
    const startHour = fromLocalInput(anchor, d.start);
    const durationH = parseFloat(d.dur);
    const qtyKg = d.qty.trim() === "" ? null : parseFloat(d.qty);
    if (startHour === null || !Number.isFinite(durationH) || durationH <= 0) return;
    if (qtyKg !== null && !Number.isFinite(qtyKg)) return;
    const ok = onApply(block.id, { startHour, durationH, qtyKg: isWindow ? null : qtyKg });
    if (ok) onClose();
  };

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
        minWidth: 220,
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
          <tr><td style={LABEL}>Line</td><td>{block.line_name}</td></tr>
          <tr><td style={LABEL}>SKU</td><td>{block.sku}</td></tr>
          {block.sku_description && (
            <tr><td style={LABEL}>Description</td><td>{block.sku_description}</td></tr>
          )}
          {editable ? (
            <>
              <tr>
                <td style={LABEL}>Start</td>
                <td>
                  <input
                    type="datetime-local"
                    style={INPUT}
                    value={d.start}
                    onChange={(e) => setDraft({ ...d, start: e.target.value })}
                  />
                </td>
              </tr>
              <tr>
                <td style={LABEL}>Duration (h)</td>
                <td>
                  <input
                    type="number"
                    min={0.5}
                    step={0.5}
                    style={INPUT}
                    value={d.dur}
                    onChange={(e) => setDur(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && apply()}
                  />
                </td>
              </tr>
              {!isWindow && (
                <tr>
                  <td style={LABEL}>Qty (kg)</td>
                  <td>
                    <input
                      type="number"
                      min={0}
                      step={100}
                      style={INPUT}
                      value={d.qty}
                      placeholder="unknown"
                      onChange={(e) => setQty(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && apply()}
                    />
                  </td>
                </tr>
              )}
              <tr>
                <td style={LABEL}>End</td>
                <td>
                  {(() => {
                    const s = fromLocalInput(anchor, d.start);
                    const dur = parseFloat(d.dur);
                    return s !== null && Number.isFinite(dur)
                      ? hourToStamp(s + dur, anchor)
                      : "—";
                  })()}
                </td>
              </tr>
            </>
          ) : (
            <>
              <tr><td style={LABEL}>Start</td><td>{hourToStamp(block.start_hour, anchor)}</td></tr>
              <tr><td style={LABEL}>End</td><td>{hourToStamp(block.end_hour, anchor)}</td></tr>
              <tr><td style={LABEL}>Duration</td><td>{block.run_hours.toFixed(1)}h</td></tr>
              {!isWindow && (
                <tr>
                  <td style={LABEL}>Qty (kg)</td>
                  <td>{block.qty_kg ? round1(block.qty_kg).toLocaleString() : "unknown"}</td>
                </tr>
              )}
            </>
          )}
          {typeof block.completion_pct === "number" && (
            <tr><td style={LABEL}>Completion</td><td>{block.completion_pct.toFixed(1)}%</td></tr>
          )}
          {typeof block.cases_left === "number" && (
            <tr><td style={LABEL}>Cases left</td><td>{block.cases_left.toLocaleString()}</td></tr>
          )}
          <tr><td style={LABEL}>Rate</td><td>{rate > 0 ? `${rate} kg/h` : "N/A"}</td></tr>
          <tr><td style={LABEL}>Type</td><td>{block.block_type}</td></tr>
          {block.locked && (
            <tr><td style={LABEL}>Locked</td><td>yes — not editable</td></tr>
          )}
        </tbody>
      </table>
      {editable && (
        <div style={{ display: "flex", gap: 8, marginTop: 8, justifyContent: "flex-end" }}>
          <button
            style={{ fontSize: 12, padding: "4px 10px", borderRadius: 4, border: "1px solid #ccc", background: "#f5f5f5", cursor: "pointer" }}
            onClick={() => setDraft(null)}
          >
            Reset
          </button>
          <button
            style={{ fontSize: 12, padding: "4px 12px", borderRadius: 4, border: "1px solid #0a8", background: "#00c896", color: "#fff", fontWeight: 600, cursor: "pointer" }}
            onClick={apply}
          >
            Apply
          </button>
        </div>
      )}
    </div>
  );
};
