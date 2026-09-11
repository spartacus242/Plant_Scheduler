# helpers/theme.py — Flowstate light design system: tokens, Streamlit CSS, chips.
#
# ONE place for the app's look. Every page calls nothing but the small helpers
# below (chips, page_header, section_label); app.py injects the stylesheet once
# per run so it applies to every page st.navigation mounts.
#
# Palette: a light, low-glare workbench for a planner who lives on the Plant
# Calendar all day — soft grey page, white surfaces, one teal accent for
# actions, and a fixed status set (ok / warn / bad / info / neutral) that is
# NEVER reused for anything decorative. The Gantt frontend mirrors these hex
# values in code/components/gantt/frontend/src/utils/theme.ts — keep both in
# step when you change a token here. Fonts are the system UI stack (Segoe UI
# on the plant PCs): no web-font download, so the app renders the same on a
# machine without internet access.

from __future__ import annotations

import html
from typing import Iterable

import streamlit as st

TOKENS: dict[str, str] = {
    # surfaces
    "bg": "#F3F5F7",
    "surface": "#FFFFFF",
    "surface_2": "#E8ECF0",
    "rule": "#D3D9DF",
    # ink
    "ink": "#1E2530",
    "ink_2": "#4E5A67",
    "ink_3": "#7A8592",
    # accent (actions, selected state)
    "accent": "#0F6E6E",
    "accent_2": "#0B5757",
    "accent_soft": "#DCEFEE",
    # status — reserved, never decorative
    "ok": "#2E7D4F",
    "ok_soft": "#E2F1E7",
    "warn": "#B9640F",
    "warn_soft": "#FBEBDD",
    "bad": "#B3322E",
    "bad_soft": "#F8E1E0",
    "info": "#2F6DB5",
    "info_soft": "#E1ECF9",
    "neutral": "#6B7A8C",
    "neutral_soft": "#E9EDF1",
}

FONT_SANS = ('"Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, '
             'sans-serif')
FONT_MONO = 'Consolas, "SFMono-Regular", Menlo, "Liberation Mono", monospace'

# Chip kinds → (text color, background, border)
_CHIP: dict[str, tuple[str, str, str]] = {
    "ok": (TOKENS["ok"], TOKENS["ok_soft"], "#BFE0CC"),
    "warn": (TOKENS["warn"], TOKENS["warn_soft"], "#F1CFA9"),
    "bad": (TOKENS["bad"], TOKENS["bad_soft"], "#EFBDBB"),
    "info": (TOKENS["info"], TOKENS["info_soft"], "#BBD3F0"),
    "neutral": (TOKENS["neutral"], TOKENS["neutral_soft"], TOKENS["rule"]),
    "accent": (TOKENS["accent_2"], TOKENS["accent_soft"], "#A9D6D3"),
}

# data_health states and stock-check statuses → chip kind. One mapping so a
# STALE feed and a TIGHT component read as the same amber everywhere.
STATE_KIND: dict[str, str] = {
    # data health
    "OK": "ok", "STALE": "warn", "MISSING": "bad", "ERROR": "bad",
    "NOT_APPLICABLE": "neutral",
    # reconcile / stock check
    "BLOCKING": "bad", "WARN": "warn", "INFO": "info",
    "TIGHT": "warn", "AT_RISK": "bad", "DO_NOT_SCHEDULE": "bad",
    "NO_BOM": "neutral", "UNK": "neutral", "NOT_TRACKED": "neutral",
    # adherence
    "MET": "ok", "UNDER": "bad", "OVER": "warn",
}


def kind_for(state: str | None) -> str:
    return STATE_KIND.get(str(state or "").upper(), "neutral")


def _css() -> str:
    t = TOKENS
    vars_ = "\n".join(f"  --fs-{k.replace('_', '-')}: {v};" for k, v in t.items())
    chips = "\n".join(
        f".fs-chip--{k} {{ color: {c}; background: {bg}; border-color: {bd}; }}"
        for k, (c, bg, bd) in _CHIP.items()
    )
    return f"""<style>
:root {{
{vars_}
  --fs-sans: {FONT_SANS};
  --fs-mono: {FONT_MONO};
}}
/* ── base ─────────────────────────────────────────────────────────── */
html, body, .stApp, [data-testid="stAppViewContainer"] {{
  font-family: var(--fs-sans);
  color: var(--fs-ink);
}}
[data-testid="stAppViewContainer"] {{ background: var(--fs-bg); }}
[data-testid="stHeader"] {{ background: var(--fs-bg); }}
[data-testid="stAppDeployButton"] {{ display: none; }}
.block-container, [data-testid="stMainBlockContainer"] {{
  max-width: 100% !important;
  /* 60px Streamlit header bar (opaque) + breathing room */
  padding: 4.4rem 1.4rem 3rem 1.4rem !important;
}}
h1, h2, h3, h4 {{ font-family: var(--fs-sans) !important; color: var(--fs-ink) !important; }}
h1 {{ font-size: 1.6rem !important; font-weight: 700 !important; letter-spacing: -0.01em; padding: 0.2rem 0 0.35rem !important; }}
h2 {{ font-size: 1.18rem !important; font-weight: 700 !important; padding: 0.35rem 0 0.25rem !important; }}
h3 {{ font-size: 1.0rem !important; font-weight: 700 !important; padding: 0.3rem 0 0.2rem !important; }}
[data-testid="stCaptionContainer"], .stCaption {{ color: var(--fs-ink-3); }}
a {{ color: var(--fs-accent); }}
code {{ font-family: var(--fs-mono); background: var(--fs-surface-2); color: var(--fs-ink-2);
        border-radius: 4px; padding: 0 4px; }}
hr {{ border-color: var(--fs-rule) !important; margin: 0.8rem 0 !important; }}

/* ── sidebar: quiet, white, the loop reads as a list ──────────────── */
section[data-testid="stSidebar"] {{
  background: var(--fs-surface);
  border-right: 1px solid var(--fs-rule);
}}
section[data-testid="stSidebar"] [data-testid="stNavSectionHeader"] {{
  font-size: 0.68rem; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--fs-ink-3); font-weight: 700; margin-top: 0.4rem;
}}
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"] {{
  border-radius: 6px; color: var(--fs-ink-2);
}}
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"]:hover {{
  background: var(--fs-surface-2); color: var(--fs-ink);
}}
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"][aria-current="page"] {{
  background: var(--fs-accent-soft); color: var(--fs-accent-2);
}}
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"][aria-current="page"] span {{
  color: var(--fs-accent-2) !important; font-weight: 600;
}}

/* ── metrics as tiles ─────────────────────────────────────────────── */
[data-testid="stMetric"] {{
  background: var(--fs-surface); border: 1px solid var(--fs-rule);
  border-radius: 10px; padding: 0.55rem 0.85rem 0.6rem;
}}
[data-testid="stMetricLabel"] {{
  font-size: 0.72rem !important; text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--fs-ink-3) !important; font-weight: 600;
}}
[data-testid="stMetricLabel"] p {{ font-size: 0.72rem !important; }}
[data-testid="stMetricValue"] {{
  font-size: 1.55rem !important; font-weight: 700; color: var(--fs-ink);
  font-variant-numeric: tabular-nums; line-height: 1.2;
}}
[data-testid="stMetricDelta"] {{ font-size: 0.78rem; }}

/* ── cards, expanders, tabs ───────────────────────────────────────── */
[data-testid="stVerticalBlockBorderWrapper"] {{
  background: var(--fs-surface); border-color: var(--fs-rule) !important;
  border-radius: 10px;
}}
[data-testid="stExpander"] details {{
  background: var(--fs-surface); border: 1px solid var(--fs-rule) !important;
  border-radius: 8px;
}}
[data-testid="stExpander"] summary {{ font-weight: 600; color: var(--fs-ink-2); }}
[data-testid="stExpander"] summary:hover {{ color: var(--fs-accent-2); }}
button[data-baseweb="tab"] {{ font-weight: 600; color: var(--fs-ink-2); }}
button[data-baseweb="tab"][aria-selected="true"] {{ color: var(--fs-accent-2); }}
[data-baseweb="tab-highlight"] {{ background-color: var(--fs-accent); }}
[data-testid="stAlert"] {{ border-radius: 8px; }}

/* ── buttons ──────────────────────────────────────────────────────── */
.stButton > button, [data-testid="stDownloadButton"] > button,
[data-testid="stFormSubmitButton"] > button, [data-testid="stPageLink"] a {{
  border-radius: 7px; font-weight: 600;
}}
button[data-testid="stBaseButton-secondary"],
[data-testid="stDownloadButton"] button[data-testid="stBaseButton-secondary"] {{
  background: var(--fs-surface); border: 1px solid var(--fs-rule); color: var(--fs-ink);
}}
button[data-testid="stBaseButton-secondary"]:hover {{
  border-color: var(--fs-accent); color: var(--fs-accent-2); background: var(--fs-surface);
}}
button[data-testid="stBaseButton-primary"] {{
  background: var(--fs-accent); border: 1px solid var(--fs-accent); color: #FFFFFF;
}}
button[data-testid="stBaseButton-primary"]:hover {{
  background: var(--fs-accent-2); border-color: var(--fs-accent-2); color: #FFFFFF;
}}
button[data-testid="stBaseButton-primary"]:disabled {{ opacity: 0.45; }}

/* ── tables ───────────────────────────────────────────────────────── */
[data-testid="stTable"] table {{ font-size: 0.85rem; }}
[data-testid="stTable"] th {{ background: var(--fs-surface-2); color: var(--fs-ink-2);
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; }}
[data-testid="stTable"] td, [data-testid="stTable"] th {{ border-color: var(--fs-rule) !important; }}

/* ── flowstate helpers ────────────────────────────────────────────── */
.fs-chips {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 2px 0 6px; }}
.fs-chip {{
  display: inline-flex; align-items: center; gap: 5px; padding: 1px 9px;
  border-radius: 999px; font-size: 12px; font-weight: 600; line-height: 18px;
  border: 1px solid transparent; white-space: nowrap; font-family: var(--fs-sans);
}}
{chips}
.fs-chip small {{ font-weight: 500; opacity: 0.85; }}
.fs-eyebrow {{
  font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--fs-ink-3); font-weight: 700; margin: 8px 0 2px;
}}
.fs-header {{ display: flex; align-items: flex-end; justify-content: space-between;
  gap: 16px; flex-wrap: wrap; margin: 0 0 6px; }}
.fs-header h1 {{ margin: 0; padding: 0 !important; }}
.fs-header .fs-sub {{ color: var(--fs-ink-2); font-size: 0.92rem; margin-top: 2px; }}
.fs-note {{ color: var(--fs-ink-3); font-size: 12.5px; }}
.fs-kv {{ display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; font-size: 13px; }}
.fs-kv b {{ color: var(--fs-ink-2); font-weight: 600; }}
/* sticky action bar: st.container(key="…sticky…") gets class st-key-<key> */
[class*="st-key-fs-sticky"] {{
  position: sticky; top: 3.75rem; z-index: 80;
  background: var(--fs-bg); padding: 6px 0 8px; margin-bottom: 4px;
  border-bottom: 1px solid var(--fs-rule);
}}
/* step rows (Command Center) */
.fs-step {{ display: grid; grid-template-columns: 200px 140px 1fr; gap: 12px; align-items: center;
  padding: 8px 12px; border: 1px solid var(--fs-rule); border-radius: 8px; background: var(--fs-surface); }}
.fs-step .t {{ font-weight: 700; color: var(--fs-ink); }}
.fs-step .d {{ color: var(--fs-ink-2); font-size: 13px; }}
</style>"""


def inject_css() -> None:
    """Inject the Flowstate stylesheet. Call once per run (app.py)."""
    st.markdown(_css(), unsafe_allow_html=True)


def chip(label: str, kind: str = "neutral", *, icon: str = "",
         sub: str = "") -> str:
    """One status chip as HTML. `kind` ∈ ok/warn/bad/info/neutral/accent."""
    k = kind if kind in _CHIP else "neutral"
    ic = f"<span>{html.escape(icon)}</span>" if icon else ""
    extra = f" <small>{html.escape(sub)}</small>" if sub else ""
    return (f'<span class="fs-chip fs-chip--{k}">{ic}{html.escape(label)}'
            f'{extra}</span>')


def state_chip(state: str, label: str | None = None) -> str:
    """Chip colored by a data-health / reconcile / stock status word."""
    return chip(label or str(state).replace("_", " "), kind_for(state))


def chips_html(items: Iterable[str]) -> str:
    return '<div class="fs-chips">' + "".join(items) + "</div>"


def render_chips(items: Iterable[str]) -> None:
    st.markdown(chips_html(items), unsafe_allow_html=True)


def page_header(title: str, subtitle: str | None = None,
                right: Iterable[str] | None = None) -> None:
    """Page title row: title + one-line subtitle, chips on the right."""
    sub = f'<div class="fs-sub">{html.escape(subtitle)}</div>' if subtitle else ""
    rhs = chips_html(right) if right else ""
    st.markdown(
        f'<div class="fs-header"><div><h1>{html.escape(title)}</h1>{sub}</div>'
        f'{rhs}</div>',
        unsafe_allow_html=True,
    )


def section_label(text: str) -> None:
    """Small uppercase eyebrow that names a block of the page."""
    st.markdown(f'<div class="fs-eyebrow">{html.escape(text)}</div>',
                unsafe_allow_html=True)


def note(text: str) -> None:
    """Quiet one-line explanation under a control (HTML-escaped)."""
    st.markdown(f'<div class="fs-note">{html.escape(text)}</div>',
                unsafe_allow_html=True)
