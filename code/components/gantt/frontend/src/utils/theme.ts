// theme.ts — Flowstate light design tokens for the Gantt (mirror of
// code/helpers/theme.py TOKENS — keep the two in step).
//
import type { CSSProperties } from "react";

// Every color the chart chrome uses comes from here: rows, header band,
// gridlines, lock shading, tiles, buttons, chips. Block fills live in
// colors.ts (they are data colors, chosen and validated separately).

export const T = {
  bg: "#F3F5F7",
  surface: "#FFFFFF",
  surface2: "#E8ECF0",
  rule: "#D3D9DF",
  ink: "#1E2530",
  ink2: "#4E5A67",
  ink3: "#7A8592",
  accent: "#0F6E6E",
  accent2: "#0B5757",
  accentSoft: "#DCEFEE",
  ok: "#2E7D4F",
  okSoft: "#E2F1E7",
  warn: "#B9640F",
  warnSoft: "#FBEBDD",
  bad: "#B3322E",
  badSoft: "#F8E1E0",
  info: "#2F6DB5",
  infoSoft: "#E1ECF9",
  neutral: "#6B7A8C",
  neutralSoft: "#E9EDF1",
} as const;

export const FONT_SANS =
  '"Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif';

// ── Chart chrome ────────────────────────────────────────────────────────
export const CHART = {
  rowEven: "#FFFFFF",
  rowOdd: "#F7F9FB",
  headerBand: "#F3F5F7",
  headerDayBand: "#E8ECF0",
  headerRule: "#C9D1D9",
  dayLine: "#E3E8ED",
  weekLine: "#7A8592",
  shiftAm: "#D9A21B",
  shiftPm: "#6A8DBF",
  lockShade: "#6B7A8C",
  lockLine: "#4E5A67",
  labelStrip: "#FFFFFF",
  // drag feedback: capable / incapable rows, and the live target row
  rowCapable: "#E2F1E7",
  rowIncapable: "#F8E1E0",
  rowTargetOk: "#BFE0CC",
  rowTargetBad: "#EFBDBB",
  insert: "#2F6DB5",
  highlightRing: "#F2B705",
} as const;

// ── Shared inline styles (buttons, tiles, chips) ────────────────────────
export const BTN_BASE: CSSProperties = {
  fontFamily: FONT_SANS,
  fontSize: 12.5,
  fontWeight: 600,
  padding: "6px 12px",
  borderRadius: 7,
  border: `1px solid ${T.rule}`,
  background: T.surface,
  color: T.ink,
  cursor: "pointer",
  lineHeight: "16px",
};

export const BTN_PRIMARY: CSSProperties = {
  ...BTN_BASE,
  background: T.accent,
  borderColor: T.accent,
  color: "#FFFFFF",
};

export const BTN_SMALL: CSSProperties = {
  ...BTN_BASE,
  fontSize: 11.5,
  padding: "3px 10px",
  borderRadius: 5,
};

export const TILE: CSSProperties = {
  background: T.surface,
  border: `1px solid ${T.rule}`,
  borderRadius: 10,
  padding: "6px 12px",
};

export const TILE_LABEL: CSSProperties = {
  fontSize: 10.5,
  fontWeight: 700,
  letterSpacing: 0.5,
  textTransform: "uppercase",
  color: T.ink3,
};

export const POPOVER: CSSProperties = {
  position: "fixed",
  background: T.surface,
  border: `1px solid ${T.rule}`,
  borderRadius: 10,
  boxShadow: "0 8px 28px rgba(30,37,48,0.18)",
  zIndex: 1000,
  fontFamily: FONT_SANS,
  color: T.ink,
};

export type ChipKind = "ok" | "warn" | "bad" | "info" | "neutral" | "accent";

const CHIP_COLORS: Record<ChipKind, { fg: string; bg: string; bd: string }> = {
  ok: { fg: T.ok, bg: T.okSoft, bd: "#BFE0CC" },
  warn: { fg: T.warn, bg: T.warnSoft, bd: "#F1CFA9" },
  bad: { fg: T.bad, bg: T.badSoft, bd: "#EFBDBB" },
  info: { fg: T.info, bg: T.infoSoft, bd: "#BBD3F0" },
  neutral: { fg: T.neutral, bg: T.neutralSoft, bd: T.rule },
  accent: { fg: T.accent2, bg: T.accentSoft, bd: "#A9D6D3" },
};

export function chipStyle(kind: ChipKind): CSSProperties {
  const c = CHIP_COLORS[kind];
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "0 8px",
    borderRadius: 999,
    fontSize: 11,
    fontWeight: 700,
    lineHeight: "18px",
    color: c.fg,
    background: c.bg,
    border: `1px solid ${c.bd}`,
    whiteSpace: "nowrap",
  };
}
