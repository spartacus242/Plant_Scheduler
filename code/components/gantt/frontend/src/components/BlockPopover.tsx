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
import { displayOrderId, hourToStamp } from "../utils/layout";

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
  /** Commit typed edits. Returns null when accepted (popover closes) or a
   * human-readable rejection reason, shown INSIDE the popover — the chart's
   * top banner is out of sight while the popup has the user's eyes. */
  onApply?: (blockId: string, edit: BlockEdit) => string | null;
  /** Snap flush against the neighbouring block (setup hours respected).
   * Same contract as onApply: null = done, string = why not. */
  onSnap?: (blockId: string, dir: "left" | "right") => string | null;
  /** Pin/unpin for the solver. Present only on production blocks the planner
   * may pin (not locked, not completed, not inside the frozen window). */
  onTogglePin?: (blockId: string, pinned: boolean) => void;
  /** Fill the empty space next to the block (setup hours respected).
   * Same contract as onSnap: null = done, string = why not. */
  onFill?: (blockId: string, dir: "left" | "right" | "both") => string | null;
  /** Remaining demand for this SKU by ISO week (target - board-scheduled),
   * so tonnage edits are made knowing what still needs filling. */
  demandLeft?: { week: string; left_kg: number; total_kg: number }[];
}

const LABEL: React.CSSProperties = { color: "#888", paddingRight: 12 };
const INPUT: React.CSSProperties = {
  width: 178,
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

export const BlockPopover: React.FC<Props> = ({ block, x, y, rate, anchor, onClose, onApply, onSnap, onTogglePin, onFill, demandLeft }) => {
  // Draft field state, (re)seeded whenever a different block is opened.
  const [draft, setDraft] = useState<{ id: string; start: string; dur: string; qty: string } | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);

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
  // ownRate is also what the fill buttons scale tonnage by, so the Rate row
  // shows it when present — displaying the catalog line-rate for a block
  // running at its own rate misstated the maths (finding 12, 2026-08-18).
  const ownRate =
    !isWindow && block.qty_kg && block.run_hours > 0 ? block.qty_kg / block.run_hours : null;
  const impliedRate = ownRate ?? (rate > 0 ? rate : null);

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
    if (startHour === null) {
      setApplyError("Start is not a valid date/time");
      return;
    }
    if (!Number.isFinite(durationH) || durationH <= 0) {
      setApplyError("Duration must be a positive number of hours");
      return;
    }
    if (qtyKg !== null && !Number.isFinite(qtyKg)) {
      setApplyError("Qty must be a number (or empty for unknown)");
      return;
    }
    const err = onApply(block.id, { startHour, durationH, qtyKg: isWindow ? null : qtyKg });
    if (err) {
      setApplyError(err);
    } else {
      setApplyError(null);
      onClose();
    }
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
        minWidth: 272,
        fontSize: 13,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <strong>{block.block_type === "cip" ? "CIP" : displayOrderId(block.order_id, anchor)}</strong>
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
          <tr>
            <td style={LABEL}>Rate</td>
            <td>
              {ownRate !== null
                ? `${round1(ownRate)} kg/h (from block)`
                : rate > 0
                  ? `${rate} kg/h (catalog)`
                  : "N/A"}
            </td>
          </tr>
          <tr><td style={LABEL}>Type</td><td>{block.block_type}</td></tr>
          {block.locked && (
            <tr><td style={LABEL}>Locked</td><td>yes — not editable</td></tr>
          )}
          {block.pinned && (
            <tr>
              <td style={LABEL}>Fixed for solver</td>
              <td>📌 yes — the solver plans around it</td>
            </tr>
          )}
        </tbody>
      </table>
      {editable && applyError && (
        <div
          style={{
            marginTop: 8,
            padding: "6px 8px",
            background: "#fdecea",
            color: "#b71c1c",
            border: "1px solid #f5c6cb",
            borderRadius: 4,
            fontSize: 12,
            maxWidth: 260,
          }}
        >
          {applyError} — the change was NOT applied.
        </div>
      )}
      {onTogglePin && (
        <div style={{ marginTop: 8 }}>
          <button
            title={
              block.pinned
                ? "Let this block move again — drags, edits and the solver may reshuffle it"
                : "Fix this block: it becomes immovable like an MO and the solver must plan the remaining demand around it"
            }
            style={{
              fontSize: 12,
              padding: "4px 10px",
              borderRadius: 4,
              border: block.pinned ? "1px solid #8d6e00" : "1px solid #37474f",
              background: block.pinned ? "#fff8e1" : "#eceff1",
              cursor: "pointer",
              fontWeight: 600,
            }}
            onClick={() => onTogglePin(block.id, !block.pinned)}
          >
            {block.pinned ? "Unpin — let it move" : "📌 Fix for solver"}
          </button>
          <div style={{ fontSize: 11, color: "#888", marginTop: 4, maxWidth: 260 }}>
            {block.pinned
              ? "Pinned: immovable like an MO. The solver treats it as committed line-time and its kg counts toward the demand plan."
              : "Pin when this SKU must run exactly here — the solver fills the rest of the demand around it."}
          </div>
        </div>
      )}
      {editable && onFill && (
        <div style={{ marginTop: 8 }}>
          <div style={{ display: "flex", gap: 8 }}>
            {(["left", "both", "right"] as const).map((d) => (
              <button
                key={d}
                title={
                  d === "both"
                    ? "Grow this block into the empty space on BOTH sides (setup hours respected)"
                    : `Grow this block ${d} into the empty space (setup hours respected)`
                }
                style={{ fontSize: 12, padding: "4px 10px", borderRadius: 4,
                         border: "1px solid #2e7d32", background: "#e8f5e9",
                         cursor: "pointer", fontWeight: 600 }}
                onClick={() => {
                  const err = onFill(block.id, d);
                  setApplyError(err);
                  if (!err) onClose();
                }}
              >
                {d === "left" ? "⬅ Fill left" : d === "right" ? "Fill right ➡" : "↔ Fill both"}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#888", marginTop: 4, maxWidth: 280 }}>
            Fills to the neighbouring block minus the required changeover
            setup; tonnage scales with the new duration.
          </div>
        </div>
      )}
      {demandLeft && demandLeft.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "#555" }}>
            Demand plan — {block.sku} still needs:
          </div>
          <table style={{ fontSize: 11, marginTop: 2, borderCollapse: "collapse" }}>
            <tbody>
              {demandLeft.map((r) => (
                <tr key={r.week}>
                  <td style={{ paddingRight: 10, color: "#888" }}>{r.week}</td>
                  <td style={{ textAlign: "right", paddingRight: 6,
                               fontWeight: 600,
                               color: r.left_kg > 0 ? "#b71c1c" : "#2e7d32" }}>
                    {r.left_kg > 0 ? `${r.left_kg.toLocaleString()} kg left` : "covered"}
                  </td>
                  <td style={{ color: "#aaa" }}>of {r.total_kg.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editable && onSnap && (
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button
            title="Place flush against the previous block on this line, leaving exactly the setup time between the two SKUs"
            style={{ fontSize: 12, padding: "4px 10px", borderRadius: 4, border: "1px solid #78909c", background: "#eceff1", cursor: "pointer" }}
            onClick={() => {
              const err = onSnap(block.id, "left");
              if (err) setApplyError(err); else { setApplyError(null); onClose(); }
            }}
          >
            ⇤ Snap left
          </button>
          <button
            title="Place flush against the next block on this line, leaving exactly the setup time between the two SKUs"
            style={{ fontSize: 12, padding: "4px 10px", borderRadius: 4, border: "1px solid #78909c", background: "#eceff1", cursor: "pointer" }}
            onClick={() => {
              const err = onSnap(block.id, "right");
              if (err) setApplyError(err); else { setApplyError(null); onClose(); }
            }}
          >
            Snap right ⇥
          </button>
        </div>
      )}
      {editable && (
        <div style={{ display: "flex", gap: 8, marginTop: 8, justifyContent: "flex-end" }}>
          <button
            style={{ fontSize: 12, padding: "4px 10px", borderRadius: 4, border: "1px solid #ccc", background: "#f5f5f5", cursor: "pointer" }}
            onClick={() => { setDraft(null); setApplyError(null); }}
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
