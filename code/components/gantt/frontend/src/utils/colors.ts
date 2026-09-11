// colors.ts — Block colors by type + SKU palette for production.
//
// SKU fills: seven categorical hues validated for a white surface (adjacent
// CVD ΔE ≥ 9, normal-vision ΔE ≥ 19; see docs/ui-redesign-2026-09.md). A
// SKU's identity is always carried by its label text — the color only groups
// same-SKU blocks at a glance, so cycling past seven SKUs is acceptable.
// Red is deliberately absent: it is reserved for downtime and status.
//
// Window blocks (CIP / trial / downtime) use their own fixed fills so a
// constraint never looks like a production run.

const SKU_PALETTE = [
  "#2A78D6", // blue
  "#EB6834", // orange
  "#1BAF7A", // aqua
  "#EDA100", // yellow
  "#E87BA4", // magenta
  "#008300", // green
  "#4A3AA7", // violet
];

const TYPE_COLORS: Record<string, string> = {
  cip: "#6B7A8C",         // slate — a scheduled clean
  trial: "#C9962A",       // gold
  maintenance: "#5B7FA6", // steel blue (hatched on the chart)
  contractor: "#8E6BB5",  // purple (hatched on the chart)
  line_down: "#B3322E",   // red (hatched on the chart)
};

/** Projected (forecast) cleans read lighter than scheduled ones. */
const CIP_PROJECTED_COLOR = "#B9C2CB";
const IDLE_COLOR = "#F3F5F7";
const _cache = new Map<string, string>();

export function skuColor(sku: string, blockType: string): string {
  if (TYPE_COLORS[blockType]) return TYPE_COLORS[blockType];
  if (!_cache.has(sku)) {
    _cache.set(sku, SKU_PALETTE[_cache.size % SKU_PALETTE.length]);
  }
  return _cache.get(sku)!;
}

/** Fill for a block, honouring the projected-CIP distinction. */
export function blockFill(sku: string, blockType: string, attrs?: string): string {
  if (blockType === "cip" && (attrs ?? "").includes("cip_projected")) return CIP_PROJECTED_COLOR;
  return skuColor(sku, blockType);
}

/** Ink for text drawn ON a fill: dark ink on light/mid fills, white on dark. */
export function skuTextColor(bgColor: string): string {
  const hex = bgColor.replace("#", "");
  const r = parseInt(hex.substring(0, 2), 16);
  const g = parseInt(hex.substring(2, 4), 16);
  const b = parseInt(hex.substring(4, 6), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.45 ? "#1E2530" : "#FFFFFF";
}

/** Window types that draw as hatched constraints (not runs). */
export function isHatchedType(blockType: string): boolean {
  return blockType === "line_down" || blockType === "maintenance" || blockType === "contractor";
}

export function blockLabel(blockType: string, sku: string, label?: string): string {
  if (blockType === "cip") return label && label.startsWith("CIP") ? label : "CIP";
  if (blockType === "maintenance") return label || "MAINT";
  if (blockType === "contractor") return label || "CONTRACT";
  if (blockType === "line_down") return label || "DOWN";
  if (blockType === "trial") return `T:${sku}`;
  return sku;
}

export { TYPE_COLORS, IDLE_COLOR, SKU_PALETTE, CIP_PROJECTED_COLOR };
