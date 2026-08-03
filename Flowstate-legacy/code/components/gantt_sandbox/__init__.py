# components/gantt_sandbox — Custom Streamlit v1 component for drag-and-drop Gantt.
#
# Build:
#   cd frontend && npm install && npm run build
#
# Python usage:
#   from components.gantt_sandbox import gantt_sandbox
#   state = gantt_sandbox(schedule=..., cipWindows=..., capabilities=..., ...)

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit.components.v1 as components

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_DIST = _FRONTEND_DIR / "dist"
_DEV_URL = "http://localhost:5173"

# Use dist/ build if it exists, otherwise fall back to dev server
if _DIST.exists() and (_DIST / "index.html").exists():
    _component_func = components.declare_component("gantt_sandbox", path=str(_DIST))
else:
    _component_func = components.declare_component("gantt_sandbox", url=_DEV_URL)


def gantt_sandbox(
    schedule: List[Dict[str, Any]],
    cip_windows: List[Dict[str, Any]],
    capabilities: Dict[str, Dict[str, float]],
    changeovers: Dict[str, Dict[str, float]],
    demand_targets: List[Dict[str, Any]],
    lines: List[Dict[str, Any]],
    holding_area: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
    height: int = 800,
    key: str = "gantt_sandbox",
) -> Optional[Dict[str, Any]]:
    """Mount the React Gantt sandbox component.

    Returns None until the first user interaction, then a dict with:
      - schedule: updated schedule blocks
      - cipWindows: updated CIP blocks
      - holdingArea: blocks moved to holding
      - lastAction: description of last user action
    """
    state = _component_func(
        schedule=schedule,
        cipWindows=cip_windows,
        capabilities=capabilities,
        changeovers=changeovers,
        demandTargets=demand_targets,
        lines=lines,
        holdingArea=holding_area or [],
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
    return state
