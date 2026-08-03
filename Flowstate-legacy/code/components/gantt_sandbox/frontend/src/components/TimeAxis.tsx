// TimeAxis.tsx — Date labels + shift lines rendered inside the SVG.
// Zoom buttons are rendered as a separate HTML component by GanttChart.

import React from "react";
import { hourToX, LINE_LABEL_WIDTH, HEADER_HEIGHT } from "../utils/layout";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

interface SvgProps {
  viewStart: number;
  viewEnd: number;
  hourWidth: number;
  anchor: Date;
  svgWidth: number;
  svgHeight: number;
}

/** SVG group rendered INSIDE the <svg> element. */
export const TimeAxisSvg: React.FC<SvgProps> = ({
  viewStart, viewEnd, hourWidth, anchor, svgWidth, svgHeight,
}) => {
  const dayPixels = 24 * hourWidth;

  // ── Day ticks ──
  const dayTicks: { hour: number; label: string }[] = [];
  const firstDay = Math.floor(viewStart / 24) * 24;
  for (let h = firstDay; h <= viewEnd; h += 24) {
    if (h >= viewStart) {
      const d = new Date(anchor.getTime() + h * 3600000);
      dayTicks.push({ hour: h, label: `${MONTHS[d.getMonth()]} ${d.getDate()}` });
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
      <rect x={0} y={0} width={svgWidth} height={HEADER_HEIGHT} fill="#fafafa" />
      <line x1={LINE_LABEL_WIDTH} y1={HEADER_HEIGHT} x2={svgWidth} y2={HEADER_HEIGHT} stroke="#ccc" />

      {/* Day columns */}
      {dayTicks.map((t, i) => {
        const x = hourToX(t.hour, viewStart, hourWidth);
        const nextX = hourToX(t.hour + 24, viewStart, hourWidth);
        const colW = nextX - x;
        return (
          <g key={t.hour}>
            {/* Alternating band */}
            {i % 2 === 1 && (
              <rect x={x} y={0} width={colW} height={HEADER_HEIGHT} fill="#eef0f5" />
            )}
            {/* Day boundary line through chart body */}
            <line x1={x} y1={HEADER_HEIGHT} x2={x} y2={svgHeight} stroke="#d0d0d0" strokeWidth={1} />
            {/* Day label centered in column */}
            {dayPixels > 30 && (
              <text
                x={x + colW / 2}
                y={HEADER_HEIGHT / 2 + 1}
                textAnchor="middle"
                dominantBaseline="middle"
                fontSize={dayPixels >= 55 ? 11 : 9}
                fontWeight={600}
                fill="#333"
              >
                {t.label}
              </text>
            )}
          </g>
        );
      })}

      {/* Shift change lines: 7AM and 7PM */}
      {shiftLines.map((s) => {
        const x = hourToX(s.hour, viewStart, hourWidth);
        return (
          <line
            key={`shift_${s.hour}`}
            x1={x}
            y1={HEADER_HEIGHT}
            x2={x}
            y2={svgHeight}
            stroke={s.isAm ? "#d4a020" : "#6a8dbf"}
            strokeDasharray="4,4"
            strokeWidth={1}
            opacity={0.5}
          />
        );
      })}

      {/* Week boundary at hour 168 */}
      {168 >= viewStart && 168 <= viewEnd && (
        <line
          x1={hourToX(168, viewStart, hourWidth)}
          y1={0}
          x2={hourToX(168, viewStart, hourWidth)}
          y2={svgHeight}
          stroke="#AB63FA"
          strokeDasharray="6,3"
          strokeWidth={2}
        />
      )}
    </g>
  );
};

/** HTML zoom controls rendered OUTSIDE the <svg>. */
export const ZoomControls: React.FC<{
  onZoomIn: () => void;
  onZoomOut: () => void;
  onResetZoom: () => void;
}> = ({ onZoomIn, onZoomOut, onResetZoom }) => (
  <div style={{ display: "flex", gap: 4, padding: "4px 0", alignItems: "center" }}>
    <button onClick={onZoomIn} style={btnStyle} title="Zoom in">+</button>
    <button onClick={onZoomOut} style={btnStyle} title="Zoom out">−</button>
    <button onClick={onResetZoom} style={{ ...btnStyle, fontSize: 11 }} title="Fit 2 weeks">Fit</button>
  </div>
);

const btnStyle: React.CSSProperties = {
  width: 28,
  height: 24,
  border: "1px solid #ccc",
  borderRadius: 4,
  background: "#fff",
  cursor: "pointer",
  fontSize: 14,
  fontWeight: 700,
  lineHeight: "22px",
};
