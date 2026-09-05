// DragPreviewBadge.tsx - Rate-aware readout shown next to the drag ghost.
// Reports the placement the drop would produce: line, hour range, wall-clock
// date/time, duration and the rate (kg/h) used to derive that duration.

import React from "react";
import type { DragPreview } from "../utils/dragPreview";
import { hourToStamp, hourToShortStamp } from "../utils/layout";
import { chipFor } from "../utils/supplyGlue";

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
  const { targetLine, startHour, endHour, hours, rate, sourceHours, valid, reason, oneSided } = preview;
  const startLabel = hourToStamp(startHour, anchor);
  const endLabel = hourToStamp(endHour, anchor);
  const delta = hours - sourceHours;
  const deltaLabel = delta === 0 ? "" : delta > 0 ? ` (+${delta}h)` : ` (${delta}h)`;
  // Supply row (contract §9): orange when the run leans on a truck or runs
  // dry, grey when it is only informational (backed / minor / no data);
  // nothing for a plain OK. The ghost never turns red for supply — only a
  // hard_block refusal does, and that arrives through `valid`.
  const chip = chipFor(preview.supply);
  const supplyColor = chip === null ? null
    : chip.tone === "crit" ? "#ff8a80"
      : chip.tone === "warn" ? "#ffcc80"
        : "#b0bec5";

  return (
    <div
      data-testid="drag-preview-badge"
      style={{
        // Above the ghost chip, not below (user report 2026-09-01): the
        // badge sat exactly over the dotted landing cell and hid it.
        position: "absolute",
        bottom: "calc(100% + 6px)",
        left: 0,
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
          {hourToShortStamp(startHour, anchor)} -&gt; {hourToShortStamp(endHour, anchor)}
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
          {typeof hours === "number" ? hours.toFixed(1) : hours}h{deltaLabel}
        </span>
      </div>
      <div style={rowStyle}>
        <span style={keyStyle}>Rate</span>
        <span>{rate > 0 ? `${Math.round(rate)} kg/h` : "n/a"}</span>
      </div>
      {oneSided && (
        <div style={{ ...rowStyle, color: "#ffcc80", fontWeight: 700 }}>
          <span>One-sided</span>
          <span>half rate - stretched</span>
        </div>
      )}
      {chip && supplyColor && preview.supplyText && (
        <div
          data-testid="drag-preview-supply"
          style={{
            marginTop: 3, paddingTop: 3, borderTop: "1px solid rgba(255,255,255,0.18)",
            color: supplyColor, fontWeight: chip.tone === "muted" ? 500 : 700,
            whiteSpace: "normal", maxWidth: 360, lineHeight: "14px",
          }}
        >
          {preview.supplyText}
        </div>
      )}
      {!valid && reason && (
        <div style={{ marginTop: 3, fontWeight: 700 }}>{reason}</div>
      )}
    </div>
  );
};
