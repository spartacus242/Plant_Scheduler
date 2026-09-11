// TimeAxis.tsx — Date labels + shift lines rendered inside the SVG.
// Zoom buttons are rendered as a separate HTML component by GanttChart.

import React, { useContext } from "react";
import { hourToX, isoWeekAtHour, mondayBoundaries, naiveDate, LINE_LABEL_WIDTH, HEADER_HEIGHT } from "../utils/layout";
import type { Receipt } from "../utils/stockRisk";
import { BTN_SMALL, CHART, T } from "../utils/theme";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Gated receipts per calendar day (supplyGlue.receiptsByDay) for the
 * header trucks (contract 2026-09-01 §9). A context rather than a prop:
 * GanttChart owns both TimeAxisSvg mounts while the payload lives in
 * GanttSandbox, which wraps the chart in the provider. null = no stock
 * payload = no trucks. */
export const ReceiptDaysContext = React.createContext<Map<number, Receipt[]> | null>(null);

interface SvgProps {
  viewStart: number;
  viewEnd: number;
  hourWidth: number;
  anchor: Date;
  svgWidth: number;
  svgHeight: number;
  /** "body" = gridlines through the chart rows (scrolls with content);
   *  "header" = the label band only (kept sticky by GanttChart);
   *  "all" = legacy single-layer rendering. */
  layer?: "all" | "body" | "header";
}

/** SVG group rendered INSIDE the <svg> element. */
export const TimeAxisSvg: React.FC<SvgProps> = ({
  viewStart, viewEnd, hourWidth, anchor, svgWidth, svgHeight, layer = "all",
}) => {
  const showBody = layer !== "header";
  const showHeader = layer !== "body";
  const dayPixels = 24 * hourWidth;
  const receiptDays = useContext(ReceiptDaysContext);

  // ── Week ticks ──
  // ISO week boundaries at true Monday-00:00 wall-clock positions (the
  // rolling anchor is any weekday, so k*168 stepping is wrong). Label =
  // the ISO week containing the boundary; sampled 1h in so a float-fuzzed
  // boundary can never date-normalize into the prior week.
  const weekTicks: { hour: number; label: string }[] = mondayBoundaries(
    anchor, viewStart, viewEnd,
  ).map((h) => ({ hour: h, label: `W${isoWeekAtHour(anchor, h + 1)}` }));
  const dayTicks: { hour: number; label: string }[] = [];
  const firstDay = Math.floor(viewStart / 24) * 24;
  for (let h = firstDay; h <= viewEnd; h += 24) {
    if (h >= viewStart) {
      // Naive wall clock (layout.ts convention): day k is [24k, 24k+24)
      // from the anchor on both sides of a DST switch, like Python.
      const d = naiveDate(anchor, h);
      dayTicks.push({ hour: h, label: `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}` });
    }
  }

  // ── Shift lines at 7AM and 7PM ──
  const shiftLines: { hour: number; isAm: boolean }[] = [];
  for (let h = firstDay + 7; h <= viewEnd; h += 12) {
    if (h >= viewStart) {
      const hourOfDay = ((h % 24) + 24) % 24;
      if (hourOfDay === 7 || hourOfDay === 19) {
        shiftLines.push({ hour: h, isAm: hourOfDay === 7 });
      }
    }
  }

  return (
    <g className="time-axis">
      {/* Header background */}
      {showHeader && (
        <>
          <rect x={0} y={0} width={svgWidth} height={HEADER_HEIGHT} fill={CHART.headerBand} />
          <line x1={LINE_LABEL_WIDTH} y1={HEADER_HEIGHT} x2={svgWidth} y2={HEADER_HEIGHT} stroke={CHART.headerRule} />
        </>
      )}

      {/* Opaque fills FIRST: the alternating day band must never paint over
          axis text, so every <text> renders in a later layer. A week
          boundary is 7 days (odd), so consecutive boundaries alternate band
          parity — interleaved rendering hid every other week label. */}
      {showHeader && dayTicks.map((t, i) => {
        if (i % 2 !== 1) return null;
        const x = hourToX(t.hour, viewStart, hourWidth);
        const colW = hourToX(t.hour + 24, viewStart, hourWidth) - x;
        return <rect key={`band_${t.hour}`} x={x} y={0} width={colW} height={HEADER_HEIGHT} fill={CHART.headerDayBand} />;
      })}

      {/* Gridlines */}
      {showBody && dayTicks.map((t) => {
        const x = hourToX(t.hour, viewStart, hourWidth);
        return <line key={`day_${t.hour}`} x1={x} y1={HEADER_HEIGHT} x2={x} y2={svgHeight} stroke={CHART.dayLine} strokeWidth={1} />;
      })}
      {weekTicks.map((t) => {
        const wx0 = hourToX(t.hour, viewStart, hourWidth);
        return (
          <g key={`wk_${t.hour}`}>
            {showBody && (
              <line x1={wx0} y1={HEADER_HEIGHT} x2={wx0} y2={svgHeight}
                    stroke={CHART.weekLine} strokeWidth={1.5} strokeDasharray="8 4" />
            )}
            {showHeader && (
              <line x1={wx0} y1={0} x2={wx0} y2={HEADER_HEIGHT}
                    stroke={CHART.weekLine} strokeWidth={1.5} strokeDasharray="8 4" />
            )}
          </g>
        );
      })}

      {/* Shift change lines: 7AM and 7PM */}
      {showBody && shiftLines.map((s) => {
        const x = hourToX(s.hour, viewStart, hourWidth);
        return (
          <line
            key={`shift_${s.hour}`}
            x1={x}
            y1={HEADER_HEIGHT}
            x2={x}
            y2={svgHeight}
            stroke={s.isAm ? CHART.shiftAm : CHART.shiftPm}
            strokeDasharray="4,4"
            strokeWidth={1}
            opacity={0.45}
          />
        );
      })}

      {/* Labels LAST — nothing may paint over them. A week whose Monday is
          left of the viewport keeps its label pinned at the viewport edge. */}
      {showHeader && weekTicks.map((t) => {
        const wx0 = hourToX(t.hour, viewStart, hourWidth);
        return (
          <text key={`wklbl_${t.hour}`}
                x={Math.max(wx0, hourToX(viewStart, viewStart, hourWidth)) + 6}
                y={11} fontSize={11} fontWeight={700} fill={T.ink2}>
            {t.label}
          </text>
        );
      })}
      {showHeader && dayPixels > 30 && dayTicks.map((t) => {
        const x = hourToX(t.hour, viewStart, hourWidth);
        const colW = hourToX(t.hour + 24, viewStart, hourWidth) - x;
        return (
          <text
            key={`daylbl_${t.hour}`}
            x={x + colW / 2}
            y={HEADER_HEIGHT / 2 + 1}
            textAnchor="middle"
            dominantBaseline="middle"
            fontSize={dayPixels >= 55 ? 11 : 9}
            fontWeight={600}
            fill={T.ink}
          >
            {t.label}
          </text>
        );
      })}

      {/* Supply trucks (§9): one 🚚 per VISIBLE day with a gated receipt, in
          the header's bottom strip under the day label; the tooltip lists
          the receipts (PO, item, qty, tier from the payload label). Days
          left of the viewport are simply not in dayTicks; a stale feed has
          been gated to no receipts upstream, so nothing draws. */}
      {showHeader && receiptDays && dayPixels >= 14 && dayTicks.map((t) => {
        const rs = receiptDays.get(Math.round(t.hour / 24));
        if (!rs || rs.length === 0) return null;
        const x = hourToX(t.hour, viewStart, hourWidth);
        const colW = hourToX(t.hour + 24, viewStart, hourWidth) - x;
        const title = `${rs.length} receipt${rs.length === 1 ? "" : "s"} ${t.label}\n`
          + rs.map((r) => r.label || `PO ${r.po8} · ${r.qty} · ${r.tier}`).join("\n");
        return (
          <g key={`rcpt_${t.hour}`} data-testid="receipt-day" style={{ cursor: "help" }}>
            <title>{title}</title>
            <rect x={x} y={HEADER_HEIGHT - 15} width={colW} height={14} fill="#fff" fillOpacity={0} />
            <text x={x + colW / 2} y={HEADER_HEIGHT - 4} textAnchor="middle"
                  fontSize={dayPixels >= 40 ? 11 : 9}>
              🚚
            </text>
          </g>
        );
      })}
    </g>
  );
};

/** HTML zoom controls rendered OUTSIDE the <svg>. */
export const ZoomControls: React.FC<{
  onZoomIn: () => void;
  onZoomOut: () => void;
  onResetZoom: () => void;
}> = ({ onZoomIn, onZoomOut, onResetZoom }) => (
  <div style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
    <button onClick={onZoomIn} style={btnStyle} title="Zoom in">+</button>
    <button onClick={onZoomOut} style={btnStyle} title="Zoom out">−</button>
    <button onClick={onResetZoom} style={{ ...btnStyle, width: "auto", fontSize: 11 }} title="Fit the whole horizon">Fit</button>
  </div>
);

const btnStyle: React.CSSProperties = {
  ...BTN_SMALL,
  width: 28,
  height: 24,
  padding: 0,
  fontSize: 14,
  fontWeight: 700,
  lineHeight: "22px",
  color: T.ink2,
};
