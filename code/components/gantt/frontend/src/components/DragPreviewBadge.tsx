// DragPreviewBadge.tsx - Rate-aware readout shown next to the drag ghost.
// Reports the placement the drop would produce: line, hour range, wall-clock
// date/time, duration and the rate (kg/h) used to derive that duration.

import React from "react";
import type { DragPreview } from "../utils/dragPreview";
import { hourToDateLabel, hourToTimeLabel } from "../utils/layout";

interface Props {
  preview: DragPreview;
  anchor: Date;
}

const rowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: 12,
  lineHeight: "15px",
};

const keyStyle: React.CSSProperties = { opacity: 0.65 };

export const DragPreviewBadge: React.FC<Props> = ({ preview, anchor }) => {
  const { targetLine, startHour, endHour, hours, rate, sourceHours, valid, reason } = preview;
  const startLabel = `${hourToDateLabel(startHour, anchor)} ${hourToTimeLabel(startHour, anchor)}`;
  const endLabel = `${hourToDateLabel(endHour, anchor)} ${hourToTimeLabel(endHour, anchor)}`;
  const delta = hours - sourceHours;
  const deltaLabel = delta === 0 ? "" : delta > 0 ? ` (+${delta}h)` : ` (${delta}h)`;

  return (
    <div
      data-testid="drag-preview-badge"
      style={{
        marginTop: 6,
        background: valid ? "rgba(28,32,38,0.94)" : "rgba(140,20,20,0.95)",
        color: "#fff",
        border: `1px solid ${valid ? "#3a4250" : "#ff8a80"}`,
        borderRadius: 6,
        padding: "6px 8px",
        fontSize: 11,
        fontFamily: "'Segoe UI', Roboto, sans-serif",
        boxShadow: "0 6px 18px rgba(0,0,0,0.35)",
        whiteSpace: "nowrap",
        minWidth: 210,
        pointerEvents: "none",
      }}
    >
      <div style={{ ...rowStyle, fontWeight: 700, marginBottom: 2 }}>
        <span>{targetLine}</span>
        <span>
          h{startHour} - h{endHour}
        </span>
      </div>
      <div style={rowStyle}>
        <span style={keyStyle}>Start</span>
        <span>{startLabel}</span>
      </div>
      <div style={rowStyle}>
        <span style={keyStyle}>End</span>
        <span>{endLabel}</span>
      </div>
      <div style={rowStyle}>
        <span style={keyStyle}>Duration</span>
        <span>
          {hours}h{deltaLabel}
        </span>
      </div>
      <div style={rowStyle}>
        <span style={keyStyle}>Rate</span>
        <span>{rate > 0 ? `${Math.round(rate)} kg/h` : "n/a"}</span>
      </div>
      {!valid && reason && (
        <div style={{ marginTop: 3, fontWeight: 700 }}>{reason}</div>
      )}
    </div>
  );
};
