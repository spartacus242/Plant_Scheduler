"""
Flowstate CSS Theme — injectable stylesheet for Streamlit.

Call  get_css()  to retrieve the full CSS string, then inject via:
    st.markdown(get_css(), unsafe_allow_html=True)

All values are derived from style_guide.py tokens.
"""

from __future__ import annotations

from .style_guide import COLORS, FONTS, RADII, SHADOWS, TRANSITIONS, GRADIENTS


def get_css() -> str:
    """Return the complete <style> block for the Flowstate dark dashboard."""
    return f"""
<style>
/* ================================================================
   FLOWSTATE DARK DASHBOARD THEME
   ================================================================ */

/* ── Google Fonts ─────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Poppins:wght@400;500;600;700&display=swap');

/* ── Global Reset & Base ─────────────────────────────────────── */
html, body, [data-testid="stAppViewContainer"] {{
    font-family: {FONTS["family"]};
    color: {COLORS["text_primary"]};
}}

[data-testid="stAppViewContainer"] {{
    background-color: {COLORS["bg_main"]};
}}

[data-testid="stHeader"] {{
    background-color: rgba(30, 30, 47, 0.85);
    backdrop-filter: blur(12px);
}}

/* ── Sidebar ─────────────────────────────────────────────────── */
section[data-testid="stSidebar"] {{
    background-color: {COLORS["bg_sidebar"]};
    border-right: 1px solid {COLORS["border"]};
}}

section[data-testid="stSidebar"] [data-testid="stSidebarNav"] {{
    padding-top: 1rem;
}}

/* Sidebar nav links */
section[data-testid="stSidebar"] a {{
    color: {COLORS["text_secondary"]} !important;
    transition: {TRANSITIONS["default"]};
    border-radius: {RADII["button"]};
}}

section[data-testid="stSidebar"] a:hover {{
    color: {COLORS["text_primary"]} !important;
    background-color: rgba(0, 242, 195, 0.08);
}}

section[data-testid="stSidebar"] a[aria-selected="true"],
section[data-testid="stSidebar"] a[aria-current="page"] {{
    background: {GRADIENTS["primary"]} !important;
    color: {COLORS["text_on_accent"]} !important;
    font-weight: {FONTS["weight_semibold"]};
    box-shadow: {SHADOWS["glow_green"]};
}}

/* Sidebar section headers */
section[data-testid="stSidebar"] [data-testid="stSidebarNavSeparator"] span {{
    color: {COLORS["text_tertiary"]} !important;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: {FONTS["weight_semibold"]};
}}

/* ── Typography ──────────────────────────────────────────────── */
h1, h2, h3, h4, h5, h6,
[data-testid="stHeading"] {{
    font-family: {FONTS["family"]} !important;
    font-weight: {FONTS["weight_bold"]} !important;
    color: {COLORS["text_primary"]} !important;
}}

/* ── Metric Widgets ──────────────────────────────────────────── */
[data-testid="stMetric"] {{
    background-color: {COLORS["bg_card"]};
    border: 1px solid {COLORS["border"]};
    border-radius: {RADII["card"]};
    padding: 1rem 1.25rem;
    transition: {TRANSITIONS["default"]};
}}

[data-testid="stMetric"]:hover {{
    transform: translateY(-2px);
    box-shadow: {SHADOWS["card_hover"]};
    border-color: {COLORS["border_light"]};
}}

[data-testid="stMetric"] label {{
    color: {COLORS["text_secondary"]} !important;
    font-size: {FONTS["size_caption"]} !important;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}}

[data-testid="stMetric"] [data-testid="stMetricValue"] {{
    font-size: {FONTS["size_metric"]} !important;
    font-weight: {FONTS["weight_bold"]} !important;
    color: {COLORS["text_primary"]} !important;
}}

/* ── Buttons ─────────────────────────────────────────────────── */
.stButton > button {{
    background: {GRADIENTS["primary"]};
    color: {COLORS["text_on_accent"]} !important;
    border: none;
    border-radius: {RADII["button"]};
    font-weight: {FONTS["weight_semibold"]};
    padding: 0.55rem 1.5rem;
    transition: {TRANSITIONS["default"]};
    box-shadow: {SHADOWS["button"]};
    letter-spacing: 0.02em;
}}

.stButton > button:hover {{
    transform: scale(1.05);
    box-shadow: {SHADOWS["glow_green"]};
    filter: brightness(1.1);
}}

.stButton > button:active {{
    transform: scale(0.98);
    box-shadow: {SHADOWS["button"]};
}}

/* Secondary / outline button */
.stButton > button[kind="secondary"] {{
    background: transparent;
    border: 1px solid {COLORS["primary"]};
    color: {COLORS["primary"]} !important;
}}

.stButton > button[kind="secondary"]:hover {{
    background: rgba(0, 242, 195, 0.1);
    box-shadow: {SHADOWS["glow_green"]};
}}

/* ── Link Buttons ────────────────────────────────────────────── */
.stLinkButton > a {{
    transition: {TRANSITIONS["default"]};
    border-radius: {RADII["button"]};
}}

.stLinkButton > a:hover {{
    transform: scale(1.05);
}}

/* ── Data Editor / Dataframes ────────────────────────────────── */
[data-testid="stDataFrame"],
[data-testid="stDataEditor"] {{
    border: 1px solid {COLORS["border"]};
    border-radius: {RADII["card"]};
    overflow: hidden;
}}

/* ── Expanders ───────────────────────────────────────────────── */
[data-testid="stExpander"] {{
    background-color: {COLORS["bg_card"]};
    border: 1px solid {COLORS["border"]};
    border-radius: {RADII["card"]};
    transition: {TRANSITIONS["default"]};
}}

[data-testid="stExpander"]:hover {{
    border-color: {COLORS["border_light"]};
}}

[data-testid="stExpander"] summary {{
    color: {COLORS["text_primary"]} !important;
    font-weight: {FONTS["weight_medium"]};
}}

/* ── Tabs ────────────────────────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {{
    gap: 0.5rem;
    border-bottom: 1px solid {COLORS["border"]};
}}

.stTabs [data-baseweb="tab"] {{
    color: {COLORS["text_secondary"]};
    border-radius: {RADII["button"]} {RADII["button"]} 0 0;
    transition: {TRANSITIONS["default"]};
    font-weight: {FONTS["weight_medium"]};
}}

.stTabs [data-baseweb="tab"]:hover {{
    color: {COLORS["text_primary"]};
    background-color: rgba(0, 242, 195, 0.06);
}}

.stTabs [aria-selected="true"] {{
    color: {COLORS["primary"]} !important;
    border-bottom: 2px solid {COLORS["primary"]};
}}

/* ── Inputs (text, number, select) ───────────────────────────── */
.stTextInput > div > div,
.stNumberInput > div > div,
.stSelectbox > div > div {{
    background-color: {COLORS["bg_input"]};
    border-color: {COLORS["border"]};
    border-radius: {RADII["input"]};
    color: {COLORS["text_primary"]};
    transition: {TRANSITIONS["default"]};
}}

.stTextInput > div > div:focus-within,
.stNumberInput > div > div:focus-within,
.stSelectbox > div > div:focus-within {{
    border-color: {COLORS["primary"]};
    box-shadow: 0 0 0 1px {COLORS["primary"]};
}}

/* ── Multiselect ─────────────────────────────────────────────── */
.stMultiSelect > div > div {{
    background-color: {COLORS["bg_input"]};
    border-color: {COLORS["border"]};
    border-radius: {RADII["input"]};
}}

/* ── Sliders ─────────────────────────────────────────────────── */
.stSlider > div > div > div > div {{
    background-color: {COLORS["primary"]};
}}

/* ── Dividers ────────────────────────────────────────────────── */
hr, [data-testid="stDivider"] {{
    border-color: {COLORS["border"]} !important;
}}

/* ── Scrollbar ───────────────────────────────────────────────── */
::-webkit-scrollbar {{
    width: 6px;
    height: 6px;
}}
::-webkit-scrollbar-track {{
    background: {COLORS["bg_main"]};
}}
::-webkit-scrollbar-thumb {{
    background: {COLORS["border_light"]};
    border-radius: 3px;
}}
::-webkit-scrollbar-thumb:hover {{
    background: {COLORS["text_tertiary"]};
}}

/* ── Toast / Alert Messages ──────────────────────────────────── */
[data-testid="stAlert"] {{
    border-radius: {RADII["card"]};
}}

/* ================================================================
   CUSTOM COMPONENT CLASSES  (use with st.markdown)
   ================================================================ */

/* ── Metric Card (custom HTML) ───────────────────────────────── */
.fs-metric-card {{
    background-color: {COLORS["bg_card"]};
    border: 1px solid {COLORS["border"]};
    border-radius: {RADII["card"]};
    padding: 1.5rem;
    transition: {TRANSITIONS["default"]};
    position: relative;
    overflow: hidden;
}}

.fs-metric-card::before {{
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 3px;
    border-radius: {RADII["card"]} {RADII["card"]} 0 0;
}}

.fs-metric-card.accent-green::before  {{ background: {GRADIENTS["primary"]}; }}
.fs-metric-card.accent-blue::before   {{ background: {COLORS["tertiary"]}; }}
.fs-metric-card.accent-pink::before   {{ background: {GRADIENTS["secondary"]}; }}
.fs-metric-card.accent-coral::before  {{ background: {GRADIENTS["warm"]}; }}

.fs-metric-card:hover {{
    transform: translateY(-4px);
    box-shadow: {SHADOWS["card_hover"]};
    border-color: {COLORS["border_light"]};
}}

.fs-metric-card .fs-metric-icon {{
    font-size: 1.6rem;
    margin-bottom: 0.75rem;
    display: inline-block;
}}

.fs-metric-card .fs-metric-value {{
    font-size: {FONTS["size_metric"]};
    font-weight: {FONTS["weight_bold"]};
    color: {COLORS["text_primary"]};
    line-height: 1.2;
    margin-bottom: 0.25rem;
}}

.fs-metric-card .fs-metric-label {{
    font-size: {FONTS["size_caption"]};
    color: {COLORS["text_secondary"]};
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: {FONTS["weight_medium"]};
}}

.fs-metric-card .fs-metric-detail {{
    font-size: {FONTS["size_caption"]};
    color: {COLORS["text_tertiary"]};
    margin-top: 0.5rem;
}}

/* ── Hero Section ────────────────────────────────────────────── */
.fs-hero {{
    padding: 2rem 0 1.5rem 0;
}}

.fs-hero-title {{
    font-size: {FONTS["size_hero"]};
    font-weight: {FONTS["weight_bold"]};
    background: {GRADIENTS["hero"]};
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 0.25rem;
    line-height: 1.1;
}}

.fs-hero-subtitle {{
    font-size: {FONTS["size_h3"]};
    color: {COLORS["text_secondary"]};
    font-weight: {FONTS["weight_regular"]};
    margin-top: 0;
}}

/* ── Quick-action Card ───────────────────────────────────────── */
.fs-action-card {{
    background-color: {COLORS["bg_card"]};
    border: 1px solid {COLORS["border"]};
    border-radius: {RADII["card"]};
    padding: 1.25rem;
    text-align: center;
    transition: {TRANSITIONS["default"]};
    cursor: default;
}}

.fs-action-card:hover {{
    border-color: {COLORS["primary"]};
    box-shadow: {SHADOWS["glow_green"]};
    transform: translateY(-3px);
}}

.fs-action-card .fs-action-icon {{
    font-size: 1.8rem;
    margin-bottom: 0.5rem;
}}

.fs-action-card .fs-action-label {{
    font-size: {FONTS["size_body"]};
    font-weight: {FONTS["weight_semibold"]};
    color: {COLORS["text_primary"]};
}}

.fs-action-card .fs-action-desc {{
    font-size: {FONTS["size_caption"]};
    color: {COLORS["text_tertiary"]};
    margin-top: 0.25rem;
}}

/* ── Workflow Step ───────────────────────────────────────────── */
.fs-step {{
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    padding: 1rem 0;
    position: relative;
}}

.fs-step-num {{
    width: 36px;
    height: 36px;
    min-width: 36px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: {FONTS["weight_bold"]};
    font-size: 0.85rem;
    border: 2px solid {COLORS["border"]};
    color: {COLORS["text_secondary"]};
    background: {COLORS["bg_card"]};
    transition: {TRANSITIONS["default"]};
}}

.fs-step.completed .fs-step-num {{
    background: {GRADIENTS["primary"]};
    color: {COLORS["text_on_accent"]};
    border-color: {COLORS["primary"]};
    box-shadow: {SHADOWS["glow_green"]};
}}

.fs-step.active .fs-step-num {{
    border-color: {COLORS["tertiary"]};
    color: {COLORS["tertiary"]};
    box-shadow: {SHADOWS["glow_blue"]};
}}

.fs-step-content {{
    flex: 1;
}}

.fs-step-title {{
    font-size: {FONTS["size_body"]};
    font-weight: {FONTS["weight_semibold"]};
    color: {COLORS["text_primary"]};
    margin-bottom: 0.15rem;
}}

.fs-step-desc {{
    font-size: {FONTS["size_caption"]};
    color: {COLORS["text_secondary"]};
    line-height: 1.5;
}}

/* ── Info Card ───────────────────────────────────────────────── */
.fs-info-card {{
    background-color: {COLORS["bg_card"]};
    border: 1px solid {COLORS["border"]};
    border-radius: {RADII["card"]};
    padding: 1.25rem 1.25rem 1rem 1.25rem;
    height: 100%;
    transition: {TRANSITIONS["default"]};
    border-top: 3px solid transparent;
}}

.fs-info-card:hover {{
    transform: translateY(-2px);
    box-shadow: {SHADOWS["card_hover"]};
}}

.fs-info-card.accent-green  {{ border-top-color: {COLORS["primary"]}; }}
.fs-info-card.accent-blue   {{ border-top-color: {COLORS["tertiary"]}; }}
.fs-info-card.accent-pink   {{ border-top-color: {COLORS["secondary"]}; }}
.fs-info-card.accent-coral  {{ border-top-color: {COLORS["warning"]}; }}

.fs-info-card .fs-info-title {{
    font-size: {FONTS["size_h4"]};
    font-weight: {FONTS["weight_semibold"]};
    color: {COLORS["text_primary"]};
    margin-bottom: 0.5rem;
}}

.fs-info-card .fs-info-text {{
    font-size: {FONTS["size_caption"]};
    color: {COLORS["text_secondary"]};
    line-height: 1.6;
}}

/* ── Status Badge ────────────────────────────────────────────── */
.fs-badge {{
    display: inline-block;
    padding: 0.2rem 0.65rem;
    border-radius: {RADII["badge"]};
    font-size: 0.7rem;
    font-weight: {FONTS["weight_semibold"]};
    letter-spacing: 0.04em;
    text-transform: uppercase;
}}

.fs-badge-ready {{
    background: rgba(0, 242, 195, 0.15);
    color: {COLORS["primary"]};
    border: 1px solid rgba(0, 242, 195, 0.3);
}}

.fs-badge-missing {{
    background: rgba(108, 108, 138, 0.15);
    color: {COLORS["text_tertiary"]};
    border: 1px solid rgba(108, 108, 138, 0.3);
}}

.fs-badge-warning {{
    background: rgba(255, 141, 114, 0.15);
    color: {COLORS["warning"]};
    border: 1px solid rgba(255, 141, 114, 0.3);
}}

/* ── Section Heading ─────────────────────────────────────────── */
.fs-section-heading {{
    font-size: {FONTS["size_h3"]};
    font-weight: {FONTS["weight_semibold"]};
    color: {COLORS["text_primary"]};
    margin: 2rem 0 1rem 0;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid {COLORS["border"]};
}}

/* ── Glow Pulse Animation ────────────────────────────────────── */
@keyframes glowPulse {{
    0%, 100% {{ opacity: 1; }}
    50%      {{ opacity: 0.7; }}
}}

.fs-glow-pulse {{
    animation: glowPulse 2s ease-in-out infinite;
}}

</style>
"""
