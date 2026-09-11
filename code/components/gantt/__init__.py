# components/gantt — Multi-type plant calendar Streamlit component (forked from gantt_sandbox).
#
# Build:
#   cd frontend && npm install && npm run build

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit.components.v1 as components

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_DIST = _FRONTEND_DIR / "dist"
_DEV_URL = "http://localhost:5173"

if _DIST.exists() and (_DIST / "index.html").exists():
    _component_func = components.declare_component("gantt_calendar", path=str(_DIST))
else:
    _component_func = components.declare_component("gantt_calendar", url=_DEV_URL)


def gantt_calendar(
    schedule: List[Dict[str, Any]],
    cip_windows: List[Dict[str, Any]],
    capabilities: Dict[str, Dict[str, float]],
    changeovers: Dict[str, Dict[str, float]],
    demand_targets: List[Dict[str, Any]],
    lines: List[Dict[str, Any]],
    holding_area: Optional[List[Dict[str, Any]]] = None,
    holding_dismissed: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
    side_downtime: Optional[Dict[str, List[List[float]]]] = None,
    sku_formats: Optional[Dict[str, str]] = None,
    kpis: Optional[Dict[str, Any]] = None,
    line_capable_skus: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    co_flags: Optional[Dict[str, int]] = None,
    sku_descriptions: Optional[Dict[str, str]] = None,
    stock: Optional[Dict[str, Any]] = None,
    focus_block: Optional[str] = None,
    height: int = 800,
    key: str = "gantt_calendar",
) -> Optional[Dict[str, Any]]:
    """Mount the React plant-calendar Gantt.

    `side_downtime` maps a line or double-line side name (e.g. "P17A") to a list
    of [start_hour, end_hour] windows, so the drag preview can stretch a block
    across the hours where a Bossar line runs one-sided at half rate.

    `kpis` is the canonical KPI payload from helpers.scorecard_engine.gantt_kpis
    (computed with scorecard rules). The frontend renders it verbatim until the
    user edits, then recomputes live with the same rules via its co_pairs map.

    `line_capable_skus` / `co_flags` / `sku_descriptions` feed the blank-space
    SKU picker (right-click a gap): candidates per line and the changeover-type
    bitmask per SKU pair — build with helpers.calendar_io.build_line_capable_skus
    / build_co_flags.

    `stock` is the StockArgs payload (helpers.calendar_io.build_stock_payload)
    behind the per-block supply chips; None hides every supply surface.
    `focus_block` is a block id to scroll to and highlight on mount
    (Reconcile finding -> ?focus=<block_id>).

    Returns None until interaction, then schedule / cipWindows / holdingArea / holdingDismissed / lastAction.
    """
    return _component_func(
        schedule=schedule,
        cipWindows=cip_windows,
        capabilities=capabilities,
        changeovers=changeovers,
        demandTargets=demand_targets,
        lines=lines,
        holdingArea=holding_area or [],
        holdingDismissed=holding_dismissed or [],
        sideDowntime=side_downtime or {},
        skuFormats=sku_formats or {},
        kpis=kpis,
        lineCapableSkus=line_capable_skus or {},
        coFlags=co_flags or {},
        skuDescriptions=sku_descriptions or {},
        stock=stock,
        focusBlock=focus_block,
        config=config or {
            "planning_anchor": "2026-02-15 00:00:00",
            "cip_duration_h": 6,
            "min_run_hours": 4,
            "horizon_hours": 336,
        },
        height=height,
        key=key,
        default=None,
    )
