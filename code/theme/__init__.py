"""
Flowstate Theme — dark dashboard design system.

Usage in any page or app.py:
    from theme import apply_theme
    apply_theme()
"""

from __future__ import annotations

import streamlit as st

from .css import get_css
from .style_guide import (
    COLORS,
    FONTS,
    GRADIENTS,
    RADII,
    SHADOWS,
    SPACING,
    TRANSITIONS,
    ACCENT_SEQUENCE,
)


def apply_theme() -> None:
    """Inject the full CSS theme into the current Streamlit page.

    Safe to call multiple times per session — Streamlit deduplicates
    identical markdown blocks.
    """
    st.markdown(get_css(), unsafe_allow_html=True)


def metric_card(
    icon: str,
    value: str,
    label: str,
    detail: str = "",
    accent: str = "green",
) -> str:
    """Return HTML for a styled metric card.

    Args:
        icon: Emoji or symbol to display.
        value: Primary numeric/text value.
        label: Uppercase label below the value.
        detail: Optional muted detail line.
        accent: One of 'green', 'blue', 'pink', 'coral'.
    """
    detail_html = (
        f'<div class="fs-metric-detail">{detail}</div>' if detail else ""
    )
    return f"""
    <div class="fs-metric-card accent-{accent}">
        <div class="fs-metric-icon">{icon}</div>
        <div class="fs-metric-value">{value}</div>
        <div class="fs-metric-label">{label}</div>
        {detail_html}
    </div>
    """


def action_card(icon: str, label: str, desc: str = "") -> str:
    """Return HTML for a quick-action card."""
    desc_html = (
        f'<div class="fs-action-desc">{desc}</div>' if desc else ""
    )
    return f"""
    <div class="fs-action-card">
        <div class="fs-action-icon">{icon}</div>
        <div class="fs-action-label">{label}</div>
        {desc_html}
    </div>
    """


def workflow_step(
    num: int,
    title: str,
    desc: str,
    state: str = "pending",
) -> str:
    """Return HTML for a single workflow step.

    Args:
        state: 'completed', 'active', or 'pending'.
    """
    return f"""
    <div class="fs-step {state}">
        <div class="fs-step-num">{num}</div>
        <div class="fs-step-content">
            <div class="fs-step-title">{title}</div>
            <div class="fs-step-desc">{desc}</div>
        </div>
    </div>
    """


def info_card(title: str, text: str, accent: str = "green") -> str:
    """Return HTML for a compact info card."""
    return f"""
    <div class="fs-info-card accent-{accent}">
        <div class="fs-info-title">{title}</div>
        <div class="fs-info-text">{text}</div>
    </div>
    """


def badge(text: str, variant: str = "ready") -> str:
    """Return HTML for an inline status badge.

    Args:
        variant: 'ready', 'missing', or 'warning'.
    """
    return f'<span class="fs-badge fs-badge-{variant}">{text}</span>'


def section_heading(text: str) -> str:
    """Return HTML for a styled section heading."""
    return f'<div class="fs-section-heading">{text}</div>'
