// KpiBar.tsx — 3-metric tile row at the top of the board.

import React from "react";
import type { KpiData } from "../types";
import { T, TILE, TILE_LABEL } from "../utils/theme";

interface Props {
  kpis: KpiData;
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

export const KpiBar: React.FC<Props> = ({ kpis }) => {
  const overlapColor = kpis.overlaps.length > 0 ? T.bad : T.ok;

  return (
    <div style={{ display: "flex", gap: 10, padding: "6px 0", flexWrap: "wrap" }}>
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
    </div>
  );
};
