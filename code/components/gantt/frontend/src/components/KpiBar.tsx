// KpiBar.tsx — 3-metric tile row at the top of the board, plus the compact
// supply pill (contract 2026-09-01 §9) the sandbox mounts in its header row.

import React from "react";
import type { KpiData, StockArgs } from "../types";
import type { SupplyCounts } from "../utils/supplyGlue";
import { T, TILE, TILE_LABEL } from "../utils/theme";

interface Props {
  kpis: KpiData;
  /** Supply-timeline summary; absent without a stock payload. */
  supply?: SupplyPillProps | null;
}

const cardStyle: React.CSSProperties = {
  ...TILE,
  display: "inline-flex",
  flexDirection: "column",
  alignItems: "flex-start",
  padding: "6px 14px",
  minWidth: 120,
};

const valueStyle: React.CSSProperties = {
  fontSize: 20,
  fontWeight: 700,
  marginTop: 1,
  color: T.ink,
  fontVariantNumeric: "tabular-nums",
};

const subStyle: React.CSSProperties = {
  fontSize: 10.5,
  color: T.ink3,
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
    ? { bg: T.badSoft, fg: T.bad, border: "#EFBDBB" }
    : tone === "warn"
      ? { bg: T.warnSoft, fg: T.warn, border: "#F1CFA9" }
      : { bg: T.neutralSoft, fg: T.neutral, border: T.rule };
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
      <span style={{ fontSize: 10.5, color: T.ink3 }}>
        stock {asOf.stock_rm} · POs {asOf.po}{feedNote}
      </span>
    </span>
  );
};

export const KpiBar: React.FC<Props> = ({ kpis, supply = null }) => {
  const overlapColor = kpis.overlaps.length > 0 ? T.bad : T.ok;

  return (
    <div style={{ display: "flex", gap: 10, padding: "6px 0", flexWrap: "wrap", alignItems: "center" }}>
      <div style={cardStyle}>
        <span style={TILE_LABEL}>Orders met</span>
        <span style={valueStyle}>
          {kpis.ordersMet}/{kpis.ordersTotal}
        </span>
      </div>
      <div style={cardStyle} title="SKU transitions (scorecard rules): recipe / format severity and estimated hours">
        <span style={TILE_LABEL}>Changeovers</span>
        <span style={valueStyle}>{kpis.totalChangeovers}</span>
        <span style={subStyle}>
          {kpis.recipeChanges} recipe · {kpis.formatChanges} format · {kpis.totalCoHours}h
        </span>
      </div>
      <div style={cardStyle}>
        <span style={TILE_LABEL}>Overlaps</span>
        <span style={{ ...valueStyle, color: overlapColor }}>{kpis.overlaps.length}</span>
      </div>
      {supply && <SupplyPill {...supply} />}
    </div>
  );
};
