// Legend.tsx — what the block colours and outlines mean, plus the mouse and
// keyboard grammar of the board, in one quiet row under the zoom controls.
// Planners asked what "MO", the hatching and the dashed cleans were on
// first sight; the legend answers without a manual.

import React from "react";
import { CIP_PROJECTED_COLOR, TYPE_COLORS, SKU_PALETTE } from "../utils/colors";
import { CHART, T } from "../utils/theme";

interface Props {
  /** Blank-space SKU picker available (calendar page only). */
  pickerEnabled: boolean;
  /** A lock window is set: explain the shaded zone. */
  hasLock: boolean;
}

const Swatch: React.FC<{ fill: string; outline?: string; dashed?: boolean; hatch?: boolean; muted?: boolean }> = ({
  fill, outline, dashed, hatch, muted,
}) => (
  <svg width={22} height={12} style={{ flex: "none" }} aria-hidden="true">
    {hatch && (
      <defs>
        <pattern id={`lg-hatch-${fill.replace("#", "")}`} patternUnits="userSpaceOnUse" width={6} height={6} patternTransform="rotate(45)">
          <rect width={6} height={6} fill="#FFFFFF" />
          <line x1={0} y1={0} x2={0} y2={6} stroke={fill} strokeWidth={2.5} opacity={0.7} />
        </pattern>
      </defs>
    )}
    <rect
      x={0.75} y={0.75} width={20.5} height={10.5} rx={3}
      fill={hatch ? `url(#lg-hatch-${fill.replace("#", "")})` : fill}
      stroke={outline ?? (hatch ? fill : "none")}
      strokeWidth={outline || hatch ? 1.2 : 0}
      strokeDasharray={dashed ? "3 2" : undefined}
      opacity={muted ? 0.55 : 1}
    />
  </svg>
);

const Item: React.FC<{ label: string; children: React.ReactNode; title?: string }> = ({ label, children, title }) => (
  <span title={title} style={{ display: "inline-flex", alignItems: "center", gap: 5, whiteSpace: "nowrap" }}>
    {children}
    <span>{label}</span>
  </span>
);

const Key: React.FC<{ k: string }> = ({ k }) => (
  <kbd style={{
    fontFamily: "inherit", fontSize: 10, fontWeight: 700, color: T.ink2,
    background: T.surface2, border: `1px solid ${T.rule}`, borderRadius: 4,
    padding: "0 5px", lineHeight: "15px",
  }}>{k}</kbd>
);

export const Legend: React.FC<Props> = ({ pickerEnabled, hasLock }) => (
  <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px", alignItems: "center",
                fontSize: 11.5, color: T.ink2, padding: "4px 0 2px" }}>
    <Item label="Production" title="A planned production run; colour groups the same SKU">
      <Swatch fill={SKU_PALETTE[0]} />
    </Item>
    <Item label="Committed MO" title="Already running or queued in the plant (manprg) — the board cannot move it">
      <Swatch fill={SKU_PALETTE[0]} muted />
    </Item>
    <Item label="Pinned" title="Fixed by you for the solver — unpin in the block popup">
      <Swatch fill={SKU_PALETTE[2]} outline={T.ink2} />
    </Item>
    <Item label="CIP" title="Scheduled clean">
      <Swatch fill={TYPE_COLORS.cip} />
    </Item>
    <Item label="Projected CIP" title="Forecast clean at the line's CIP interval — moves when you insert a clean">
      <Swatch fill={CIP_PROJECTED_COLOR} outline={T.ink3} dashed />
    </Item>
    <Item label="Trial" title="Trial run (goes to the ERP with production)">
      <Swatch fill={TYPE_COLORS.trial} />
    </Item>
    <Item label="Down / maintenance" title="Downtime, maintenance or contractor window — a constraint, never scheduled over">
      <Swatch fill={TYPE_COLORS.line_down} hatch />
    </Item>
    {hasLock && (
      <Item label="Locked weeks" title="Committed to the plant — no drag, resize or edit inside the shaded zone">
        <Swatch fill={CHART.lockShade} muted outline={CHART.lockLine} dashed />
      </Item>
    )}
    <span style={{ color: T.ink3 }}>·</span>
    <span style={{ display: "inline-flex", gap: 10, flexWrap: "wrap", color: T.ink3 }}>
      <span>drag to move</span>
      <span>edges resize</span>
      <span>click = details</span>
      <span>right-click = highlight SKU</span>
      <span><Key k="Shift" />+right-click = menu</span>
      {pickerEnabled && <span>right-click a gap = add SKU / CIP</span>}
      <span><Key k="Ctrl" />+<Key k="Z" /> undo</span>
    </span>
  </div>
);
