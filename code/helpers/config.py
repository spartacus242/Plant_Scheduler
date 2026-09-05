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


def datasources_config(cfg: dict | None = None) -> dict[str, Any]:
    """Return [datasources] section with defaults applied. Every importer
    reads its path/connection from here so Carsten can point Flowstate at his
    own files/SQL without code edits."""
    cfg = cfg if cfg is not None else load_toml()
    ds = dict(cfg.get("datasources", {}))
    defaults = {
        "vif_folder": r"\\usnpa-appfs\DATA\vif-export\auto editions",
        "manprg_files": "",                   # two paths, ';'-separated
        "cip_info_csv": "",
        "demand_summary_csv": "",             # weekly AZAP baseline (Week, Product, kg_tons)
        "po_report_path": "",                 # IT's open-PO extract (xlsx/csv); "" = bridge file
        "sql_enabled": False,
        "sql_dsn": "",                        # ODBC connection string for NPA
    }
    for k, v in defaults.items():
        ds.setdefault(k, v)
    return ds


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
        "weight_trials": 0.10,
        # Caps for normalizing category scores (worse beyond cap → score 0)
        "cap_recipe_changes": 40,
        "cap_format_changes": 40,
        "cap_co_hours": 80,
        # Severity-weighted changeover load (plant ranking 2026-08-14):
        # FFS & topload changes are the most expensive, casepacker next,
        # TTP least; a recipe-only change touches no machine. The scored
        # changeover metric is sum(weight x count) against cap_weighted_co.
        "co_weight_topload": 3.0,
        "co_weight_ffs": 3.0,
        "co_weight_casepacker": 2.0,
        "co_weight_ttp": 1.0,
        "co_weight_recipe_only": 1.0,
        "cap_weighted_co": 150.0,
        # Per-ISO-week changeover scoring (2026-08-25): cap_weighted_co is
        # a ONE-WEEK calibration. Each production week scores weighted_co
        # against its pro-rated cap (span/168), floored at
        # co_week_score_floor, and the weeks combine as a geometric product
        # weighted by co_week_decay^i — the near week dominates, one blown
        # week can't be averaged away. total_co_hours is reported but NOT
        # scored (setup_hours standards not yet trusted); cap_co_hours is
        # retained for display only.
        "co_week_decay": 0.6,
        "co_week_score_floor": 0.05,
        "cap_cip_hours": 120,
        "cap_cip_forfeited": 200,
        # Forfeited CIP is scored in kg of lost production (hours x line avg
        # kg/h). Calibrated ~1.6x the seed schedule so it does not clamp to 0.
        "cap_cip_forfeited_kg": 1500000.0,
        # Overdue CIPs (line ran past its max interval without a clean). CIPs
        # are MANDATORY and cannot be late/overdue — this is the ONLY scored
        # component of the CIP category. Cap = 1: a single overdue event drops
        # the CIP category to 0; zero overdue = 100 (fully compliant).
        "cap_cip_overdue": 1,
        "cap_trial_hours": 48,
        "cap_trial_disruptions": 20,
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


def stock_config(cfg: dict | None = None) -> dict[str, Any]:
    """Return [stock] section with supply-timeline defaults applied.

    Rules for the open-PO receipt gate and the per-block supply verdict
    (contracts 2026-09-01 §1). Hours are floats; the buffer L is
    24 x min_days_after_delivery. Lists are copied so a caller mutating
    them never edits the defaults.
    """
    cfg = cfg if cfg is not None else load_toml()
    sc = dict(cfg.get("stock", {}))
    defaults = {
        # Buffer L (h) = 24 x this: a receipt must land this many days before
        # the block's reference hour to count as reliable ("OK · backed").
        "min_days_after_delivery": 4,
        # "block_start" measures the lead to the block start; "depletion"
        # measures it to the hour on-hand runs out (later, more lenient).
        "lead_measured_from": "block_start",
        # A packaging receipt is usable from HH:00 local on its receipt date.
        "receipt_ready_hour": 16,
        # Raw arrival areas add a QC release delay before the lot is usable.
        "raw_qc_offset_h": 72,
        # When a dock appointment is joined (po8 within +/-1 d), ready =
        # appointment time + this.
        "appt_ready_offset_h": 2,
        # Feed older than this (by content: max receipt date vs today) is stale.
        "po_ignore_after_h": 168,
        # Landed rule: lots with a batch date >= receipt_date - 3 d summing to
        # >= this fraction of the PO qty mean the line already arrived.
        "landed_match_frac": 0.95,
        # Dependent share below this reads as minor (grey/info, not orange).
        "dependent_frac_floor": 0.05,
        # Warn-only by default; true rejects only SHORT with zero on-hand and
        # zero inbound on a fresh feed.
        "hard_block": False,
        # Block cases from the board's qty_kg when present, else rate x hours.
        "use_board_qty_kg": True,
        "raw_areas": ["RB1", "AMB", "RC1"],
        # Count only from a joined "SL3 TRANSFER" appointment, else excluded.
        "offsite_areas": ["SL3"],
    }
    for k, v in defaults.items():
        sc.setdefault(k, list(v) if isinstance(v, list) else v)
    return sc
