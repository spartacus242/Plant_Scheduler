// KpiBar.tsx — 4-metric bar at top of the sandbox, plus the compact supply
// pill (contract 2026-09-01 §9) the sandbox mounts in its header row.

import React from "react";
import type { KpiData, StockArgs } from "../types";
import type { SupplyCounts } from "../utils/supplyGlue";

interface Props {
  kpis: KpiData;
  /** Supply-timeline summary; absent without a stock payload. */
  supply?: SupplyPillProps | null;
}

const cardStyle: React.CSSProperties = {
  display: "inline-flex",
  flexDirection: "column",
  alignItems: "center",
  padding: "8px 18px",
  borderRadius: 8,
  background: "#f7f7fa",
  border: "1px solid #e0e0e5",
  minWidth: 120,
};

const labelStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#666",
  textTransform: "uppercase",
  letterSpacing: 0.5,
};

const valueStyle: React.CSSProperties = {
  fontSize: 22,
  fontWeight: 700,
  marginTop: 2,
};

const subStyle: React.CSSProperties = {
  fontSize: 10,
  color: "#888",
  marginTop: 1,
};

export interface SupplyPillProps {
  counts: SupplyCounts;
  asOf: StockArgs["as_of"];
  feedState: StockArgs["feed_state"];
}

/** "supply ⛔ N · 🚚 M · ? K" with the as-of caption. Red when anything is
 * SHORT, orange when a run leans on a truck, grey otherwise; a feed that
 * is not "ok" says so — a stale feed can neither rescue nor alarm. */
export const SupplyPill: React.FC<SupplyPillProps> = ({ counts, asOf, feedState }) => {
  const tone = counts.short > 0 ? "crit" : counts.dependent > 0 ? "warn" : "muted";
  const colors = tone === "crit"
    ? { bg: "#fdecea", fg: "#b71c1c", border: "#f5c6cb" }
    : tone === "warn"
      ? { bg: "#fff3e0", fg: "#e65100", border: "#ffcc80" }
      : { bg: "#f7f7fa", fg: "#546e7a", border: "#e0e0e5" };
  const feedNote = feedState === "ok" ? "" : ` · PO feed ${feedState}`;
  return (
    <span
      data-testid="supply-pill"
      title={`Supply timeline over the current board: ${counts.short} short, ${counts.dependent} delivery-dependent, `
        + `${counts.no_data} without inbound data, ${counts.backed} backed by a truck landing in time. `
        + `Stock as of ${asOf.stock_rm} (raw) / ${asOf.stock_pkg} (packaging); POs as of ${asOf.po}${feedNote}.`}
      style={{ display: "inline-flex", alignItems: "baseline", gap: 8, whiteSpace: "nowrap" }}
    >
      <span style={{
        fontSize: 12, fontWeight: 800, color: colors.fg, background: colors.bg,
        border: `1px solid ${colors.border}`, borderRadius: 10, padding: "1px 10px",
      }}>
        supply ⛔ {counts.short} · 🚚 {counts.dependent} · ? {counts.no_data}
      </span>
      <span style={{ fontSize: 10.5, color: "#8a94a0" }}>
        stock {asOf.stock_rm} · POs {asOf.po}{feedNote}
      </span>
    </span>
  );
};

export const KpiBar: React.FC<Props> = ({ kpis, supply = null }) => {
  const overlapColor = kpis.overlaps.length > 0 ? "#EF553B" : "#00CC96";

  return (
    <div style={{ display: "flex", gap: 12, padding: "8px 0", flexWrap: "wrap", alignItems: "center" }}>
      <div style={cardStyle}>
        <span style={labelStyle}>Orders Met</span>
        <span style={valueStyle}>
          {kpis.ordersMet}/{kpis.ordersTotal}
        </span>
      </div>
      <div style={cardStyle} title="SKU transitions (scorecard rules): recipe / format severity and estimated hours">
        <span style={labelStyle}>Changeovers</span>
        <span style={valueStyle}>{kpis.totalChangeovers}</span>
        <span style={subStyle}>
          {kpis.recipeChanges} recipe · {kpis.formatChanges} format · {kpis.totalCoHours}h
        </span>
      </div>
      <div style={cardStyle}>
        <span style={labelStyle}>Overlaps</span>
        <span style={{ ...valueStyle, color: overlapColor }}>{kpis.overlaps.length}</span>
      </div>
      {supply && <SupplyPill {...supply} />}
    </div>
  );
};
