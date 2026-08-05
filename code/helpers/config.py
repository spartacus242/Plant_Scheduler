# helpers/config.py — Load flowstate.toml including [scorecard] draft formulas.

from __future__ import annotations

from pathlib import Path
from typing import Any

from helpers.paths import toml_path


def load_toml(path: Path | None = None) -> dict[str, Any]:
    path = path or toml_path()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def scorecard_config(cfg: dict | None = None) -> dict[str, Any]:
    """Return [scorecard] section with v0 defaults applied."""
    cfg = cfg if cfg is not None else load_toml()
    sc = dict(cfg.get("scorecard", {}))
    defaults = {
        "short_run_h": 4.0,
        "align_tolerance_h": 2.0,
        "at_risk_h": 24.0,
        "default_co_hours_recipe": 1.0,
        "default_co_hours_format": 2.0,
        "default_co_hours_base": 0.5,
        "cip_interval_fallback_h": 120,
        "weight_service": 0.30,
        "weight_changeovers": 0.20,
        "weight_cip": 0.15,
        "weight_campaigns": 0.15,
        "weight_maintenance": 0.10,
        "weight_trials": 0.10,
        # Caps for normalizing category scores (worse beyond cap → score 0)
        "cap_recipe_changes": 40,
        "cap_format_changes": 40,
        "cap_co_hours": 80,
        "cap_cip_count": 30,
        "cap_cip_hours": 120,
        "cap_cip_forfeited": 200,
        # Forfeited CIP is scored in kg of lost production (hours x line avg
        # kg/h). Calibrated ~1.6x the seed schedule so it does not clamp to 0.
        "cap_cip_forfeited_kg": 1500000.0,
        "cap_trial_hours": 48,
        "cap_trial_disruptions": 20,
        "cap_maint_conflicts": 10,
        "cap_short_runs": 30,
        "cap_orders_late": 15,
        "cap_orders_at_risk": 20,
        "cap_excess_kg": 50000,
        "target_avg_run_h": 16.0,
        # Deprecated: target_avg_run_h drove a SYMMETRIC campaign run-length
        # score that punished long runs. Longer runs are better, so the score
        # is now monotonic and hits 100 at this floor. Kept separate so the old
        # key's meaning is not silently changed.
        "campaign_run_floor_h": 24.0,
    }
    for k, v in defaults.items():
        sc.setdefault(k, v)
    return sc
