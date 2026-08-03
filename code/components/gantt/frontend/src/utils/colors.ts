// colors.ts — Block colors by type + SKU palette for production.

const PLOTLY_PALETTE = [
  "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
  "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
];

const TYPE_COLORS: Record<string, string> = {
  cip: "#888888",
  trial: "#D4A017",
  maintenance: "#2E86AB",
  contractor: "#6C3483",
  line_down: "#922B21",
};

const IDLE_COLOR = "#f0f0f0";
const _cache = new Map<string, string>();

export function skuColor(sku: string, blockType: string): string {
  if (TYPE_COLORS[blockType]) return TYPE_COLORS[blockType];
  if (!_cache.has(sku)) {
    _cache.set(sku, PLOTLY_PALETTE[_cache.size % PLOTLY_PALETTE.length]);
  }
  return _cache.get(sku)!;
}

export function skuTextColor(bgColor: string): string {
  const hex = bgColor.replace("#", "");
  const r = parseInt(hex.substring(0, 2), 16);
  const g = parseInt(hex.substring(2, 4), 16);
  const b = parseInt(hex.substring(4, 6), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.5 ? "#000" : "#fff";
}

export function blockLabel(blockType: string, sku: string, label?: string): string {
  if (blockType === "cip") return "CIP";
  if (blockType === "maintenance") return label || "MAINT";
  if (blockType === "contractor") return label || "CONTRACT";
  if (blockType === "line_down") return label || "DOWN";
  if (blockType === "trial") return `T:${sku}`;
  return sku;
}

export { TYPE_COLORS, IDLE_COLOR };
