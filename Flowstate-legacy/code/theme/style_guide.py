"""
Flowstate Design System — Single source of truth for all design tokens.

Reference this module from any page or component instead of hardcoding
colors, fonts, spacing, or other visual constants.

Palette inspired by Black Dashboard Pro merged with Pom'Potes / GoGo squeeZ
brand identity.
"""

# ── Colors ──────────────────────────────────────────────────────────────

COLORS = {
    # Backgrounds
    "bg_main":        "#1e1e2f",
    "bg_card":        "#27293d",
    "bg_sidebar":     "#1a1a2e",
    "bg_input":       "#2d2d44",

    # Accent palette
    "primary":        "#00f2c3",   # neon green  (GoGo squeeZ)
    "secondary":      "#e14eca",   # vibrant pink (Pom'Potes)
    "tertiary":       "#1d8cf8",   # electric blue (data / charts)
    "warning":        "#ff8d72",   # warm coral
    "success":        "#00f2c3",
    "error":          "#fd5d93",
    "info":           "#1d8cf8",

    # Text
    "text_primary":   "#ffffff",
    "text_secondary": "#9a9a9a",
    "text_tertiary":  "#6c6c8a",
    "text_on_accent": "#1e1e2f",

    # Borders / dividers
    "border":         "#2d2d44",
    "border_light":   "#3a3a52",
}

# ── Gradients ───────────────────────────────────────────────────────────

GRADIENTS = {
    "primary":    "linear-gradient(135deg, #00f2c3, #1d8cf8)",
    "secondary":  "linear-gradient(135deg, #e14eca, #1d8cf8)",
    "warm":       "linear-gradient(135deg, #ff8d72, #e14eca)",
    "hero":       "linear-gradient(135deg, #00f2c3 0%, #1d8cf8 50%, #e14eca 100%)",
}

# ── Typography ──────────────────────────────────────────────────────────

FONTS = {
    "family":       "'Inter', 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "family_mono":  "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    "weight_regular":  400,
    "weight_medium":   500,
    "weight_semibold": 600,
    "weight_bold":     700,
    "size_hero":    "2.8rem",
    "size_h1":      "2.0rem",
    "size_h2":      "1.5rem",
    "size_h3":      "1.2rem",
    "size_h4":      "1.0rem",
    "size_body":    "0.95rem",
    "size_caption":  "0.8rem",
    "size_metric":   "2.2rem",
    "line_height":   1.6,
}

# ── Spacing ─────────────────────────────────────────────────────────────

SPACING = {
    "xs":  "0.25rem",   # 4px
    "sm":  "0.5rem",    # 8px
    "md":  "1.0rem",    # 16px
    "lg":  "1.5rem",    # 24px
    "xl":  "2.0rem",    # 32px
    "2xl": "3.0rem",    # 48px
}

# ── Radii ───────────────────────────────────────────────────────────────

RADII = {
    "card":   "12px",
    "button": "8px",
    "badge":  "20px",
    "input":  "8px",
    "sm":     "6px",
}

# ── Shadows ─────────────────────────────────────────────────────────────

SHADOWS = {
    "card":       "0 4px 20px rgba(0, 0, 0, 0.25)",
    "card_hover": "0 8px 30px rgba(0, 0, 0, 0.35)",
    "glow_green": "0 0 20px rgba(0, 242, 195, 0.35)",
    "glow_pink":  "0 0 20px rgba(225, 78, 202, 0.35)",
    "glow_blue":  "0 0 20px rgba(29, 140, 248, 0.35)",
    "glow_coral": "0 0 20px rgba(255, 141, 114, 0.35)",
    "button":     "0 2px 10px rgba(0, 0, 0, 0.2)",
}

# ── Transitions ─────────────────────────────────────────────────────────

TRANSITIONS = {
    "default":   "all 0.3s ease-in-out",
    "fast":      "all 0.15s ease-in-out",
    "slow":      "all 0.5s ease-in-out",
}

# ── Accent ordering (for card borders, chart series, etc.) ──────────────

ACCENT_SEQUENCE = [
    COLORS["primary"],
    COLORS["tertiary"],
    COLORS["secondary"],
    COLORS["warning"],
]
