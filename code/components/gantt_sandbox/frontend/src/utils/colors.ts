// colors.ts — SKU color map (matches Plotly qualitative palette).

const PLOTLY_PALETTE = [
  "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
  "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
];

const CIP_COLOR = "#888888";
const TRIAL_COLOR = "#D4A017";
const IDLE_COLOR = "#f0f0f0";

const _cache = new Map<string, string>();

export function skuColor(sku: string, blockType: string): string {
  if (blockType === "cip") return CIP_COLOR;
  if (blockType === "trial") return TRIAL_COLOR;
  if (!_cache.has(sku)) {
    _cache.set(sku, PLOTLY_PALETTE[_cache.size % PLOTLY_PALETTE.length]);
  }
  return _cache.get(sku)!;
}

export function skuTextColor(bgColor: string): string {
  // Simple luminance check for white vs dark text
  const hex = bgColor.replace("#", "");
  const r = parseInt(hex.substring(0, 2), 16);
  const g = parseInt(hex.substring(2, 4), 16);
  const b = parseInt(hex.substring(4, 6), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.5 ? "#000" : "#fff";
}

export { CIP_COLOR, TRIAL_COLOR, IDLE_COLOR };
