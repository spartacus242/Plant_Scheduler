// SupplyDetailPanel.tsx — the "Supply details" modal (planner feedback
// 2026-09-02).
//
// The popover lists only the items that can bite; this panel tells the rest
// of the story for those items: every block drawing them from the stock
// snapshot through this block's end with the running balance (where it runs
// out), the open POs with dates, quantities and lead, and the untracked
// consumables. Rows come from supplyGlue.supplyDetail (pure, pinned in
// tests/test_supply_detail.py); this file only formats.
//
// A fixed overlay INSIDE the Gantt iframe. The iframe is sized to the board
// and is often taller than the window, so the card sits near the top of the
// iframe viewport (a vertical centre could be off-screen), centred across.

import React, { useEffect, useMemo, useRef } from "react";
import type { ScheduleBlock, StockArgs } from "../types";
import { displayOrderId } from "../utils/layout";
import type { Rules, Supply, Timelines } from "../utils/stockRisk";
import {
  casesFromKg, fmtQty, leadText, supplyDetail, type DetailItem, type StampFn,
} from "../utils/supplyGlue";
import { VerdictPill } from "./BlockPopover";

interface Props {
  block: ScheduleBlock;
  supply: Supply;
  timelines: Timelines;
  stock: StockArgs;
  schedule: readonly ScheduleBlock[];
  /** Real-minute stamp for supply hours (supplyGlue.stampFor). */
  anchorStamp: StampFn;
  rules: Rules;
  /** Planning anchor for planner-facing order ids; raw ids without one. */
  anchor?: Date;
  onClose: () => void;
}

const TH: React.CSSProperties = {
  textAlign: "left", color: "#78909c", fontWeight: 600, padding: "3px 10px 3px 0",
  borderBottom: "1px solid #e0e0e5", whiteSpace: "nowrap",
};
const TD: React.CSSProperties = {
  padding: "3px 10px 3px 0", borderBottom: "1px solid #f0f0f3", verticalAlign: "top", whiteSpace: "nowrap",
};
const NUM: React.CSSProperties = { ...TD, textAlign: "right", fontVariantNumeric: "tabular-nums" };
const TABLE: React.CSSProperties = { borderCollapse: "collapse", marginTop: 4, fontSize: 11 };
const H: React.CSSProperties = { fontWeight: 700, color: "#455a64", marginTop: 10 };

const TAG_STYLE: Record<string, React.CSSProperties> = {
  "this block": { background: "#fff3e0", color: "#e65100" },
  running: { background: "#e3f2fd", color: "#1565c0" },
  "MO locked": { background: "#eceff1", color: "#455a64" },
  planner: { background: "#f5f5f5", color: "#777" },
};

const Tag: React.FC<{ t: string }> = ({ t }) => (
  <span style={{ fontSize: 9.5, fontWeight: 700, borderRadius: 6, padding: "0 5px",
                 ...(TAG_STYLE[t] ?? TAG_STYLE.planner) }}>
    {t}
  </span>
);

/** A balance; red once the pooled curve is below zero. */
const Bal: React.FC<{ v: number }> = ({ v }) => (
  <span style={{ color: v < -1e-6 ? "#b71c1c" : "#333", fontWeight: v < -1e-6 ? 700 : 400 }}>{fmtQty(v)}</span>
);

const ItemSection: React.FC<{
  d: DetailItem; stock: StockArgs; stamp: StampFn; rules: Rules; anchor?: Date; snapshotH: number;
}> = ({ d, stock, stamp, rules, anchor, snapshotH }) => {
  const L = 24 * Number(rules.min_days_after_delivery);
  const minDays = Number(rules.min_days_after_delivery);
  const refWord = rules.lead_measured_from === "depletion" ? "it runs out" : "start";
  const alts = d.members.slice(1);
  const oid = (id: string) => (anchor ? displayOrderId(id, anchor) : id);
  return (
    <section data-testid={`supply-detail-item-${d.item}`} style={{ marginTop: 14 }}>
      <div style={{ fontSize: 12.5 }}>
        <VerdictPill e={d} />
        <b>{d.item}</b>
        {d.designation && <span style={{ color: "#777" }}> {d.designation}</span>}
        <span style={{ color: "#999" }}> · {d.unit}</span>
        <span style={{ color: "#333" }}> · need {fmtQty(d.need)} {d.unit}</span>
      </div>
      <div style={{ color: "#555", marginTop: 2 }}>
        {d.sentence}
        {d.safe_from_h != null && d.verdict !== "OK" && ` · safe from ${stamp(d.safe_from_h)}`}
      </div>

      <div style={H}>
        Blocks drawing {d.item} from the stock snapshot ({stamp(snapshotH)}) through this block's end
      </div>
      {alts.length > 0 && (
        <div style={{ color: "#999" }}>pooled with alternate{alts.length === 1 ? "" : "s"} {alts.join(", ")}</div>
      )}
      <div style={{ overflowX: "auto" }}>
        <table style={TABLE} data-testid={`draw-table-${d.item}`}>
          <thead>
            <tr>
              <th style={TH}>Line</th>
              <th style={TH}>Block</th>
              <th style={TH}>Window</th>
              <th style={{ ...TH, textAlign: "right" }}>Draw</th>
              <th style={{ ...TH, textAlign: "right" }}>Balance before → after</th>
              <th style={TH}></th>
            </tr>
          </thead>
          <tbody>
            {d.draws.map((row) => (
              <tr key={`${row.key}|${row.item}`}
                  style={row.tag === "this block" ? { background: "#fff8e1" } : undefined}>
                <td style={TD}>{row.line_name}</td>
                <td style={TD}>
                  {oid(row.order_id)} {row.sku}
                  {row.is_alt && <span style={{ color: "#999" }}> (alt {row.item})</span>}
                </td>
                <td style={TD}>{stamp(row.a)} → {stamp(row.e)}</td>
                <td style={NUM}>{fmtQty(row.qty)} {d.unit}</td>
                <td style={NUM}><Bal v={row.balance_before} /> → <Bal v={row.balance_after} /></td>
                <td style={TD}><Tag t={row.tag} /></td>
              </tr>
            ))}
            {d.draws.length === 0 && (
              <tr><td style={{ ...TD, color: "#777" }} colSpan={6}>No block draws this item in the window.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={H}>
        Open POs for {d.item}{alts.length > 0 && ` (and ${alts.join(", ")})`}
      </div>
      {stock.feed_state !== "ok" && (
        <div style={{ color: "#8d6e00" }}>
          PO feed {stock.feed_state}: receipts unknown — shortfalls read "?" instead of short.
        </div>
      )}
      {d.receipts.length === 0 ? (
        <div style={{ color: "#777" }}>
          No open PO for this item in the feed (window to {stamp(stock.receipts_window_end_h)}).
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={TABLE} data-testid={`po-table-${d.item}`}>
            <thead>
              <tr>
                <th style={TH}>PO</th>
                <th style={{ ...TH, textAlign: "right" }}>Qty</th>
                <th style={TH}>Receipt date</th>
                <th style={TH}>Ready</th>
                <th style={TH}>Tier</th>
                <th style={TH}>Lead vs this block</th>
                <th style={TH}></th>
              </tr>
            </thead>
            <tbody>
              {d.receipts.map((r, i) => {
                // Same hours/days form as the popover row (supplyGlue.leadText).
                const leadTxt = r.lead_h >= 0
                  ? `${leadText(r.lead_h)} ${r.lead_h < L ? "<" : "≥"} ${minDays} d before ${refWord}`
                  : `lands ${leadText(r.lead_h)} after ${refWord} (mid-run)`;
                return (
                  <tr key={`${r.po8}-${i}`} style={r.binding ? { fontWeight: 600 } : undefined}>
                    <td style={TD}>PO {r.po8}{r.is_alt && <span style={{ color: "#999" }}> (alt {r.item})</span>}</td>
                    <td style={NUM}>+{fmtQty(r.qty)} {r.unit}</td>
                    <td style={TD}>{r.receipt_date ?? "—"}</td>
                    <td style={TD}>{stamp(r.ready_h)}</td>
                    <td style={TD}>{r.tier === "appt" ? "dock appt" : "ERP date"}</td>
                    <td style={TD}>{leadTxt}</td>
                    <td style={TD}>
                      {r.counted && <span style={{ color: "#2e7d32" }}>counted</span>}
                      {r.binding && <span style={{ fontWeight: 700 }}> ◀ binding</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
};

export const SupplyDetailPanel: React.FC<Props> = ({
  block, supply, timelines, stock, schedule, anchorStamp, rules, anchor, onClose,
}) => {
  const cardRef = useRef<HTMLDivElement>(null);
  // Keyboard focus moves into the dialog so Escape / tab land here.
  useEffect(() => { cardRef.current?.focus(); }, []);
  useEffect(() => {
    // Capture phase + stopPropagation: the sandbox's own window Escape
    // handler would otherwise also close the popover underneath.
    const h = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", h, true);
    return () => window.removeEventListener("keydown", h, true);
  }, [onClose]);

  const detail = useMemo(
    () => supplyDetail(block, supply, timelines, stock, schedule, rules, anchorStamp),
    [block, supply, timelines, stock, schedule, rules, anchorStamp],
  );
  const snapshotOf = (item: string): number => timelines.items[item]?.snapshot_h ?? 0;
  const oid = anchor ? displayOrderId(block.order_id, anchor) : block.order_id;
  const asOf = stock.as_of;

  return (
    <div
      data-testid="supply-detail-backdrop"
      style={{ position: "fixed", inset: 0, zIndex: 2000, background: "rgba(38,50,56,0.45)",
               display: "flex", justifyContent: "center", alignItems: "flex-start",
               paddingTop: "6vh", boxSizing: "border-box" }}
      onClick={(e) => { e.stopPropagation(); onClose(); }}
    >
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Supply details for ${block.sku} on ${block.line_name}`}
        tabIndex={-1}
        data-testid="supply-detail-panel"
        style={{ background: "#fff", borderRadius: 10, boxShadow: "0 12px 40px rgba(0,0,0,0.3)",
                 width: "min(760px, calc(100vw - 32px))", maxHeight: "80vh",
                 display: "flex", flexDirection: "column", fontSize: 11.5, outline: "none",
                 fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start",
                      gap: 12, padding: "12px 16px 8px", borderBottom: "1px solid #e0e0e5" }}>
          <div>
            <div style={{ fontSize: 13.5, fontWeight: 700 }}>
              {block.sku}
              {block.sku_description && <span style={{ fontWeight: 400, color: "#555" }}> {block.sku_description}</span>}
              <span style={{ color: "#999", fontWeight: 400 }}>
                {" · "}{block.line_name}{" · "}{anchorStamp(block.start_hour)} → {anchorStamp(block.end_hour)}{" · "}
              </span>
              <VerdictPill e={supply} />
            </div>
            <div style={{ color: "#888", marginTop: 2 }}>{oid} · supply detail for this piece</div>
          </div>
          <button
            aria-label="Close"
            title="Close (Esc)"
            onClick={onClose}
            style={{ border: "none", background: "transparent", fontSize: 18, lineHeight: 1,
                     color: "#888", cursor: "pointer", padding: "0 4px" }}
          >
            ✕
          </button>
        </div>

        <div style={{ overflowY: "auto", padding: "2px 16px 12px" }}>
          {detail.items.length === 0 ? (
            <div style={{ marginTop: 12, fontWeight: 700, color: supply.verdict === "NO_DATA" ? "#546e7a" : "#2e7d32" }}>
              {supply.verdict === "NO_DATA" && supply.item == null
                ? "? No recipe or quantity data for this block — nothing to grade."
                : "All items covered by on-hand stock for this block."}
            </div>
          ) : (
            detail.items.map((d) => (
              <ItemSection key={d.item} d={d} stock={stock} stamp={anchorStamp} rules={rules}
                           anchor={anchor} snapshotH={snapshotOf(d.item)} />
            ))
          )}
          {detail.ok_items.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <div style={{ fontWeight: 700, color: "#455a64" }}>
                {detail.items.length > 0
                  ? `${detail.ok_items.length} item${detail.ok_items.length === 1 ? "" : "s"} covered by on hand`
                  : "Items"}
              </div>
              {detail.ok_items.map((o) => (
                <div key={o.item} style={{ color: "#666", paddingLeft: 10, lineHeight: 1.5 }}>
                  ✅ {o.item}
                  {o.designation && <span style={{ color: "#999" }}> {o.designation}</span>}
                </div>
              ))}
            </div>
          )}
        </div>

        <div style={{ borderTop: "1px solid #e0e0e5", padding: "8px 16px 10px", color: "#999",
                      fontSize: 10.5, lineHeight: 1.5 }}>
          <div>
            Stock as of {asOf.stock_rm}
            {asOf.stock_pkg && asOf.stock_pkg !== asOf.stock_rm && ` / pkg ${asOf.stock_pkg}`}
            {" · POs as of "}{asOf.po || "—"}
            {" (window to "}{anchorStamp(stock.receipts_window_end_h)}{")"}
            {stock.feed_state !== "ok" && ` · PO feed ${stock.feed_state}`}
          </div>
          <div>
            {supply.verdict === "NO_DATA" && supply.item == null
              ? "No quantity basis for this block."
              : <>
                  Need is based on {casesFromKg(block, stock) ? "this block's kg (qty ÷ kg/case)" : "rate × hours (no kg on the block)"};
                  balances net every block on the board, live for this edit state.
                </>}
          </div>
          {detail.untracked.length > 0 && (
            <div>Untracked consumables are never graded: {detail.untracked.join(", ")}</div>
          )}
        </div>
      </div>
    </div>
  );
};
