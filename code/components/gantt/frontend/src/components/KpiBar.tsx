// KpiBar.tsx — 4-metric bar at top of the sandbox.

import React from "react";
import type { KpiData } from "../types";

interface Props {
  kpis: KpiData;
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

export const KpiBar: React.FC<Props> = ({ kpis }) => {
  const adhColor = kpis.pctAdherence >= 100 ? "#00CC96" : kpis.pctAdherence >= 80 ? "#FFA15A" : "#EF553B";
  const overlapColor = kpis.overlaps.length > 0 ? "#EF553B" : "#00CC96";

  return (
    <div style={{ display: "flex", gap: 12, padding: "8px 0", flexWrap: "wrap" }}>
      <div style={cardStyle}>
        <span style={labelStyle}>Adherence</span>
        <span style={{ ...valueStyle, color: adhColor }}>{kpis.pctAdherence}%</span>
      </div>
      <div style={cardStyle}>
        <span style={labelStyle}>Orders Met</span>
        <span style={valueStyle}>
          {kpis.ordersMet}/{kpis.ordersTotal}
        </span>
      </div>
      <div style={cardStyle}>
        <span style={labelStyle}>Changeovers</span>
        <span style={valueStyle}>{kpis.totalChangeovers}</span>
      </div>
      <div style={cardStyle}>
        <span style={labelStyle}>Overlaps</span>
        <span style={{ ...valueStyle, color: overlapColor }}>{kpis.overlaps.length}</span>
      </div>
    </div>
  );
};
