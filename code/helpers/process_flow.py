# helpers/process_flow.py — pipeline model for the Command Center Home page.
#
# Pure data + state derivation, no rendering. The Home page draws the SVG;
# this module decides the ORDER of stages, their labels, and how a stage's
# health is derived from data_health.HealthStatus entries.

from __future__ import annotations

from dataclasses import dataclass

from helpers.data_health import HealthStatus, OK, STALE, MISSING, ERROR, NOT_APPLICABLE


@dataclass(frozen=True)
class Stage:
    id: str
    title: str
    subtitle: str
    page: str                     # streamlit page target for the deep link
    health_keys: tuple[str, ...]  # data_health keys that feed this stage


STAGES: tuple[Stage, ...] = (
    Stage("vif", "VIF / ERP data", "Daily component + BOM exports",
          "pages/stock_check.py",
          ("ediact 3.csv", "jestkexp.csv", "azapart.csv", "rmpkitems.csv", "vif")),
    Stage("demand", "Demand plan (AZAP)", "Corporate weekly demand",
          "pages/data.py",
          ("demand_plan", "demand_week")),
    Stage("calendar", "Plant Calendar", "The schedule of record (digital twin)",
          "pages/calendar.py",
          ("calendar_blocks", "calendar_anchor", "calendar_cip", "calendar_maint", "lines")),
    Stage("score", "Schedule Scorecard", "Weekly score snapshot",
          "pages/scorecard.py",
          ("scorecard", "calendar_blocks")),
    Stage("stock", "Stock Check", "Component coverage vs the board",
          "pages/stock_check.py",
          ("vif",)),
    Stage("optimize", "Generate Scenarios", "Solver alternatives",
          "pages/generate.py",
          ("demand_plan", "calendar_blocks", "rate_mode", "versions")),
    Stage("promote", "Promote & roll", "Version Compare → official",
          "pages/compare.py",
          ("versions", "calendar_blocks")),
)

_STATE_RANK = {OK: 0, NOT_APPLICABLE: 0, STALE: 1, ERROR: 2, MISSING: 2}


def stage_state(stage_id: str, health: list[HealthStatus]) -> str:
    """Worst state among the health entries feeding a stage."""
    stage = next((s for s in STAGES if s.id == stage_id), None)
    if stage is None:
        return NOT_APPLICABLE
    hits = [h for h in health if h.key in stage.health_keys]
    if not hits:
        return OK  # no rule fired -> nothing wrong known
    worst = max(hits, key=lambda h: _STATE_RANK.get(h.state, 0))
    return worst.state


def stage_detail(stage_id: str, health: list[HealthStatus]) -> str:
    """Human detail line for a stage (worst entry's detail, else subtitle)."""
    stage = next((s for s in STAGES if s.id == stage_id), None)
    if stage is None:
        return ""
    hits = [h for h in health if h.key in stage.health_keys]
    if not hits:
        return stage.subtitle
    worst = max(hits, key=lambda h: _STATE_RANK.get(h.state, 0))
    return worst.detail


def by_id(stage_id: str) -> Stage | None:
    return next((s for s in STAGES if s.id == stage_id), None)
