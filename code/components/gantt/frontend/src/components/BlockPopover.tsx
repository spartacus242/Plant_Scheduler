// BlockPopover.tsx — Click-to-view detail popover with editable fields.
//
// The planner can TYPE exact values instead of pixel-dragging: start (wall
// clock), duration (h) and tonnage (kg). Duration and tonnage are linked
// through the block's implied rate (qty_kg / run_hours, falling back to the
// capability-table rate): edit one and the other follows, then either can be
// overridden before Apply. Apply commits the displayed values literally —
// validation (overlap, locked, min-run) happens in the GanttSandbox handler,
// which keeps the popover open on rejection so the values can be corrected.
//
// Every callback hands back the BLOCK object (split MO pieces share an id,
// so an id alone cannot name the piece being edited — 2026-09-01).
//
// "Supply" section (contract 2026-09-01 §9, mock in the plan §4): the live
// client verdict for this piece — rendered only when the page sent a stock
// payload. Planner feedback 2026-09-02: only the rows that can bite (SHORT /
// DEPENDENT / NO_DATA / backed) are listed; plain-OK items sit behind the
// "N more OK" toggle; the untracked consumables and WHICH other blocks draw
// an item moved to the "Supply details" modal (SupplyDetailPanel) — here
// only a count. [Acknowledge for this session] hides the block-face chip
// for this key; the text here stays.

import React, { useMemo, useState } from "react";
import type { ScheduleBlock, StockArgs } from "../types";
import { isWindowBlock } from "../types";
import { displayOrderId, hourToStamp, naiveDate, naiveMs } from "../utils/layout";
import type { Supply, SupplyItem } from "../utils/stockRisk";
import { supplyRank } from "../utils/stockRisk";
import {
  atRiskCount, casesFromKg, chipFor, countedReceipts, fmtQty, itemSentence, leadText, rulesOf,
  sortedItems, type StampFn,
} from "../utils/supplyGlue";
import { blockKey } from "../utils/blockIdentity";
import { BTN_BASE, BTN_PRIMARY, POPOVER, T } from "../utils/theme";

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
  onApply?: (block: ScheduleBlock, edit: BlockEdit) => string | null;
  /** Snap flush against the neighbouring block (setup hours respected).
   * Same contract as onApply: null = done, string = why not. */
  onSnap?: (block: ScheduleBlock, dir: "left" | "right") => string | null;
  /** Pin/unpin for the solver. Present only on production blocks the planner
   * may pin (not locked, not completed, not inside the frozen window). */
  onTogglePin?: (block: ScheduleBlock, pinned: boolean) => void;
  /** Remove the block to the holding area; absent when the block is locked. */
  onRemove?: (block: ScheduleBlock) => void;
  /** Insert a CIP flush before/after this block (later projected cleans on
   * the line re-forecast from it). Same contract as onSnap: null = done,
   * string = why not. Offered on committed blocks too — the clean goes
   * around the plant's run, never through it. */
  onAddCip?: (block: ScheduleBlock, dir: "before" | "after") => string | null;
  /** Fill the empty space next to the block (setup hours respected).
   * Same contract as onSnap: null = done, string = why not. */
  onFill?: (block: ScheduleBlock, dir: "left" | "right" | "both") => string | null;
  /** Remaining demand for this SKU by ISO week (target - board-scheduled),
   * so tonnage edits are made knowing what still needs filling. */
  demandLeft?: { week: string; left_kg: number; total_kg: number; scheduled_kg?: number }[];
  /** Supply timeline (all absent without a stock payload). */
  supply?: Supply | null;
  stock?: StockArgs | null;
  /** Anchor-aware stamp for supply hours (real minutes). */
  supplyStamp?: StampFn;
  acknowledged?: boolean;
  onAcknowledge?: (key: string) => void;
  /** Open the Supply details modal for this piece (SupplyDetailPanel). */
  onOpenSupplyDetail?: (block: ScheduleBlock) => void;
}

const LABEL: React.CSSProperties = { color: T.ink3, paddingRight: 12 };
const INPUT: React.CSSProperties = {
  width: 178,
  fontSize: 12,
  padding: "3px 6px",
  border: `1px solid ${T.rule}`,
  borderRadius: 5,
  boxSizing: "border-box",
  color: T.ink,
  fontFamily: "inherit",
};

// datetime-local <-> board hours on the NAIVE wall clock (layout.ts
// convention, fix FE / audit time-7): a typed "2026-11-02T00:00" is
// exactly 168 h after a Monday 10-26 anchor on both sides, never 169.
function toLocalInput(anchor: Date, hour: number): string {
  const d = naiveDate(anchor, hour);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

function fromLocalInput(anchor: Date, value: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(String(value ?? ""));
  if (!m) return null;
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] ?? 0));
  if (Number.isNaN(t)) return null;
  return (t - naiveMs(anchor)) / 3600_000;
}

const round1 = (v: number) => Math.round(v * 10) / 10;

const VERDICT_WORD: Record<string, string> = {
  OK: "OK", DEPENDENT: "DELIVERY-DEPENDENT", SHORT: "SHORT", NO_DATA: "NO DATA",
};

export const VerdictPill: React.FC<{ e: { verdict: string; backed?: boolean; minor?: boolean } }> = ({ e }) => {
  const r = supplyRank(e);
  const st = r === 4 ? { bg: "#c62828", fg: "#fff" }
    : r === 3 ? { bg: "#ef6c00", fg: "#fff" }
      : r === 2 ? { bg: "#eceff1", fg: "#546e7a" }
        : r === 1 ? { bg: "#e0f2f1", fg: "#00695c" }
          : { bg: "#e8f5e9", fg: "#2e7d32" };
  const text = r === 4 ? "SHORT" : r === 3 ? "DEPENDENT" : r === 2 ? "?" : r === 1 ? "OK·backed" : "OK";
  return (
    <span style={{ fontSize: 9.5, fontWeight: 800, color: st.fg, background: st.bg,
                   borderRadius: 8, padding: "0 6px", marginRight: 6 }}>
      {text}
    </span>
  );
};

/** One flagged recipe item of the Supply section; `others` = how many OTHER
 * blocks draw it in this window (who they are is in the details modal). */
const SupplyItemRow: React.FC<{
  e: SupplyItem; block: ScheduleBlock; stock: StockArgs; stamp: StampFn; others?: number;
}> = ({ e, block, stock, stamp, others = 0 }) => {
  const R = rulesOf(stock);
  const L = 24 * Number(R.min_days_after_delivery);
  const minDays = Number(R.min_days_after_delivery);
  const ref = R.lead_measured_from === "depletion" ? (e.depletion_h ?? block.start_hour) : block.start_hour;
  const receipts = countedReceipts(stock, block.sku, e.item, block.end_hour);
  const desig = stock.designations?.[e.item] ?? "";
  const sub: React.CSSProperties = { paddingLeft: 10, color: "#555" };
  return (
    <div style={{ marginTop: 4, lineHeight: 1.5 }}>
      <div>
        <VerdictPill e={e} />
        <b>{e.item}</b>
        {desig && <span style={{ color: "#777" }}> {desig}</span>}
        <span style={{ color: "#333" }}> · need {fmtQty(e.need)} {e.unit}</span>
        {others > 0 && (
          <span style={{ color: "#999", fontSize: 10 }}>
            {" · "}{others} other block{others === 1 ? " draws" : "s draw"} this
          </span>
        )}
      </div>
      <div style={sub}>{itemSentence(e, stamp)}</div>
      {receipts.map((r, i) => {
        const lead = ref - r.ready_h;
        const isBinding = e.binding !== null && e.binding.po8 === r.po8
          && Math.abs(e.binding.ready_h - r.ready_h) < 1e-9;
        // Same hours/days form as the block sentence (supplyGlue.leadText).
        const leadTxt = lead >= 0
          ? `lead ${leadText(lead)} ${lead < L ? "<" : "≥"} ${minDays} d`
          : `lands ${leadText(lead)} after ${R.lead_measured_from === "depletion" ? "it runs out" : "start"} (mid-run)`;
        return (
          <div key={`${r.po8}-${i}`} style={{ ...sub, color: isBinding ? "#333" : "#666" }}>
            PO {r.po8} +{fmtQty(r.qty)} {e.unit} · {stamp(r.ready_h)}
            <span style={{ color: "#999" }}> ({r.tier === "appt" ? "dock appt" : "ERP date"})</span>
            {" · "}{leadTxt}
            {isBinding && <span style={{ fontWeight: 700 }}> ◀ binding</span>}
          </div>
        );
      })}
      <div style={{ ...sub, fontWeight: 700, color: supplyRank(e) >= 3 ? "#b71c1c" : "#455a64" }}>
        ▶ {VERDICT_WORD[e.verdict] ?? e.verdict}
        {e.backed && " · backed"}
        {e.minor && " · minor share"}
        {e.mid_run && " · mid-run"}
        {e.safe_from_h != null && e.verdict !== "OK" && ` · safe from ${stamp(e.safe_from_h)}`}
      </div>
    </div>
  );
};

export const BlockPopover: React.FC<Props> = ({
  block, x, y, rate, anchor, onClose, onApply, onSnap, onTogglePin, onFill, onRemove, onAddCip, demandLeft,
  supply = null, stock = null, supplyStamp, acknowledged = false, onAcknowledge, onOpenSupplyDetail,
}) => {
  // Draft field state, (re)seeded whenever a different block is opened.
  const [draft, setDraft] = useState<{ id: string; start: string; dur: string; qty: string } | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [showAllOk, setShowAllOk] = useState(false);

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
    const err = onApply(block, { startHour, durationH, qtyKg: isWindow ? null : qtyKg });
    if (err) {
      setApplyError(err);
    } else {
      setApplyError(null);
      onClose();
    }
  };

  // Supply section inputs (nothing rendered without a payload).
  const showSupply = stock !== null && supply !== null && block.block_type === "sku";
  const stamp: StampFn = supplyStamp ?? ((h) => hourToStamp(h, anchor));

  return (
    <div
      style={{
        ...POPOVER,
        left: x,
        top: y,
        padding: "10px 14px",
        minWidth: 272,
        maxWidth: 520,
        maxHeight: "80vh",
        overflowY: "auto",
        fontSize: 13,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <strong>{block.block_type === "cip" ? "CIP" : displayOrderId(block.order_id, anchor)}</strong>
        <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {/* Remove-to-holding (user request 2026-09-01: plain right-click
              now toggles the SKU highlight, so delete lives here). Same
              action as the Shift+right-click menu: the block leaves the
              board and its kg lands back in the holding area, undoable. */}
          {onRemove && (
            <span
              style={{ cursor: "pointer", fontSize: 14 }}
              title="Remove this block — its tonnage returns to the holding area (undoable)"
              onClick={() => onRemove(block)}
            >
              🗑
            </span>
          )}
          <span style={{ cursor: "pointer", fontWeight: 700, color: T.ink3 }} onClick={onClose}>
            ×
          </span>
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
            background: T.badSoft,
            color: T.bad,
            border: "1px solid #EFBDBB",
            borderRadius: 4,
            fontSize: 12,
            maxWidth: 260,
          }}
        >
          {applyError} — the change was NOT applied.
        </div>
      )}
      {showSupply && stock && supply && (() => {
        const items = sortedItems(supply);
        // Only rows that can bite (planner feedback 2026-09-02): SHORT /
        // DEPENDENT / NO_DATA / backed. Plain OK stays behind the toggle.
        const flagged = items.filter((e) => supplyRank(e) > 0);
        const plainOk = items.filter((e) => supplyRank(e) === 0);
        const atRisk = atRiskCount(supply);
        const coCount = (item: string): number =>
          Object.prototype.hasOwnProperty.call(supply.co_consumers ?? {}, item)
            ? supply.co_consumers[item].length : 0;
        const chip = chipFor(supply);
        const key = blockKey(block);
        return (
          <div data-testid="supply-section"
               style={{ marginTop: 10, paddingTop: 6, borderTop: "1px solid #e0e0e5", fontSize: 11, maxWidth: 480 }}>
            <div style={{ fontWeight: 700, color: "#455a64" }}>
              Supply · stock as of {stock.as_of.stock_rm}
              {stock.as_of.stock_pkg && stock.as_of.stock_pkg !== stock.as_of.stock_rm && ` / pkg ${stock.as_of.stock_pkg}`}
              {" · POs as of "}{stock.as_of.po || "—"}
              {" (window to "}{stamp(stock.receipts_window_end_h)}{")"}
            </div>
            {stock.feed_state !== "ok" && (
              <div style={{ color: "#8d6e00", marginTop: 2 }}>
                PO feed {stock.feed_state}: receipts unknown — shortfalls read "?" instead of short.
              </div>
            )}
            {items.length === 0 && (
              <div style={{ color: "#777", marginTop: 4 }}>
                {supply.verdict === "NO_DATA"
                  ? "? no recipe or quantity data for this block — nothing to grade"
                  : "No tracked components to check."}
              </div>
            )}
            {flagged.map((e) => (
              <SupplyItemRow key={e.item} e={e} block={block} stock={stock} stamp={stamp}
                             others={coCount(e.item)} />
            ))}
            {onOpenSupplyDetail && (items.length > 0 || supply.untracked.length > 0) && (
              <div style={{ marginTop: 6 }}>
                <button
                  data-testid="supply-details"
                  title="Every block drawing the at-risk items with the running balance, the open POs with dates and quantities, and the untracked consumables"
                  style={{ fontSize: 11, padding: "3px 10px", borderRadius: 4, cursor: "pointer", fontWeight: 600,
                           border: atRisk > 0 ? "1px solid #ef6c00" : "1px solid #78909c",
                           background: atRisk > 0 ? "#fff3e0" : "#eceff1" }}
                  onClick={() => onOpenSupplyDetail(block)}
                >
                  {atRisk > 0 ? `Supply details (${atRisk} item${atRisk === 1 ? "" : "s"} at risk)` : "Supply details…"}
                </button>
              </div>
            )}
            {plainOk.length > 0 && (
              <div style={{ color: "#777", marginTop: 4 }}>
                <span style={{ cursor: "pointer" }} onClick={() => setShowAllOk((v) => !v)}>
                  {showAllOk ? "▾ hide" : "▸"} {plainOk.length} more OK
                </span>
                {showAllOk && plainOk.map((e) => (
                  <div key={e.item} style={{ paddingLeft: 10, color: "#888", lineHeight: 1.4 }}>
                    ✅ {e.item}
                    {stock.designations?.[e.item] && <span style={{ color: "#aaa" }}> {stock.designations[e.item]}</span>}
                  </div>
                ))}
              </div>
            )}
            <div style={{ color: "#999", marginTop: 4 }}>
              {/* NO_DATA with no worst item = the engine had no cases at all
                  (no recipe, or no kg and no rate): there is no need to base
                  on anything, so "rate × hours" would be a lie. */}
              {supply.verdict === "NO_DATA" && supply.item == null
                ? "No quantity basis for this block."
                : <>
                    Need is based on {casesFromKg(block, stock) ? "this block's kg (qty ÷ kg/case)" : "rate × hours (no kg on the block)"};
                    on-hand nets every block on the board, live for this edit state.
                  </>}
            </div>
            {onAcknowledge && chip !== null && (
              <div style={{ marginTop: 6 }}>
                {acknowledged ? (
                  <span style={{ color: "#777" }}>Acknowledged for this session — chip hidden on the block.</span>
                ) : (
                  <button
                    title="Hide the supply chip on this block until the page reloads; the verdict stays here and in Reconcile"
                    style={{ fontSize: 11, padding: "3px 10px", borderRadius: 4,
                             border: "1px solid #78909c", background: "#eceff1", cursor: "pointer", fontWeight: 600 }}
                    onClick={() => onAcknowledge(key)}
                  >
                    Acknowledge for this session
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })()}
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
              border: block.pinned ? "1px solid #F1CFA9" : `1px solid ${T.ink2}`,
              background: block.pinned ? T.warnSoft : T.surface2,
              color: block.pinned ? T.warn : T.ink,
              cursor: "pointer",
              fontWeight: 600,
            }}
            onClick={() => onTogglePin(block, !block.pinned)}
          >
            {block.pinned ? "Unpin — let it move" : "📌 Fix for solver"}
          </button>
          <div style={{ fontSize: 11, color: T.ink3, marginTop: 4, maxWidth: 260 }}>
            {block.pinned
              ? "Pinned: immovable like an MO. The solver treats it as committed line-time and its kg counts toward the demand plan."
              : "Pin when this SKU must run exactly here — the solver fills the rest of the demand around it."}
          </div>
        </div>
      )}
      {onAddCip && block.block_type !== "cip" && (
        <div style={{ marginTop: 8 }}>
          <div style={{ display: "flex", gap: 8 }}>
            {(["before", "after"] as const).map((d) => (
              <button
                key={d}
                title={`Insert a clean flush ${d === "before" ? "BEFORE" : "AFTER"} this block; later projected CIPs on the line re-forecast from it`}
                style={{ fontSize: 12, padding: "4px 10px", borderRadius: 5,
                         border: "1px solid #BBD3F0", background: T.infoSoft, color: T.info,
                         cursor: "pointer", fontWeight: 600 }}
                onClick={() => {
                  const err = onAddCip(block, d);
                  setApplyError(err);
                  if (!err) onClose();
                }}
              >
                {d === "before" ? "🧼 CIP before" : "CIP after 🧼"}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 11, color: T.ink3, marginTop: 4, maxWidth: 260 }}>
            Adds a clean next to this block and re-forecasts the line's later
            projected CIPs from it; blocks slide right if the gap is short.
          </div>
        </div>
      )}
      {/* Fill grows a PRODUCTION run into the empty space beside it; a CIP or
          downtime window has no tonnage to scale, so it keeps only Start /
          Duration and the Snap buttons (planner request 2026-09-11). */}
      {editable && onFill && !isWindow && (
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
                style={{ fontSize: 12, padding: "4px 10px", borderRadius: 5,
                         border: "1px solid #BFE0CC", background: T.okSoft, color: T.ok,
                         cursor: "pointer", fontWeight: 600 }}
                onClick={() => {
                  const err = onFill(block, d);
                  setApplyError(err);
                  if (!err) onClose();
                }}
              >
                {d === "left" ? "⬅ Fill left" : d === "right" ? "Fill right ➡" : "↔ Fill both"}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 11, color: T.ink3, marginTop: 4, maxWidth: 280 }}>
            Fills to the neighbouring block minus the required changeover
            setup; tonnage scales with the new duration.
          </div>
        </div>
      )}
      {demandLeft && demandLeft.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: T.ink2 }}>
            Demand plan — {block.sku} still needs:
          </div>
          <table style={{ fontSize: 11, marginTop: 2, borderCollapse: "collapse" }}>
            <tbody>
              {demandLeft.map((r) => {
                const sched = r.scheduled_kg ?? Math.max(0, r.total_kg - r.left_kg);
                const pct = r.total_kg > 0 ? Math.round((sched / r.total_kg) * 100) : 100;
                const over = sched - r.total_kg;
                return (
                <tr key={r.week}>
                  <td style={{ paddingRight: 10, color: T.ink3 }}>{r.week}</td>
                  <td style={{ textAlign: "right", paddingRight: 6,
                               fontWeight: 600,
                               color: r.left_kg > 0 ? T.bad : T.ok }}>
                    {r.left_kg > 0
                      ? `${r.left_kg.toLocaleString()} kg left · ${pct}%`
                      : over > 0
                        ? `+${Math.round(over).toLocaleString()} kg over · ${pct}%`
                        : "covered · 100%"}
                  </td>
                  <td style={{ color: T.ink3 }}>of {r.total_kg.toLocaleString()}</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {editable && onSnap && (
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button
            title="Place flush against the previous block on this line, leaving exactly the setup time between the two SKUs"
            style={{ ...BTN_BASE, fontSize: 12, padding: "4px 10px" }}
            onClick={() => {
              const err = onSnap(block, "left");
              if (err) setApplyError(err); else { setApplyError(null); onClose(); }
            }}
          >
            ⇤ Snap left
          </button>
          <button
            title="Place flush against the next block on this line, leaving exactly the setup time between the two SKUs"
            style={{ ...BTN_BASE, fontSize: 12, padding: "4px 10px" }}
            onClick={() => {
              const err = onSnap(block, "right");
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
            style={{ ...BTN_BASE, fontSize: 12, padding: "4px 10px" }}
            onClick={() => { setDraft(null); setApplyError(null); }}
          >
            Reset
          </button>
          <button
            style={{ ...BTN_PRIMARY, fontSize: 12, padding: "4px 12px" }}
            onClick={apply}
          >
            Apply
          </button>
        </div>
      )}
    </div>
  );
};
