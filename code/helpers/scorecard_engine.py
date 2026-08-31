# helpers/scorecard_engine.py — Operational truth model (Phase 0).
#
# Same functions score the current schedule, DnD what-if calendars, and solver scenarios.
# Draft v0 formulas — thresholds live in flowstate.toml [scorecard].

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from helpers.config import load_toml, scorecard_config
from helpers.paths import reference_dir, scorecards_dir
from helpers.safe_io import safe_write_json


# ---------------------------------------------------------------------------
# Metric reference (documentation only - no scoring math lives here).
#
# METRIC_DOCS is the single source of truth for the in-app "How these metrics
# are calculated" reference. Every entry mirrors what the scoring functions
# below ACTUALLY do; if you change a formula, update the matching entry.
#
#   definition  - short prose (this is what FORMULA_HELP exposes, unchanged)
#   formula     - exact math as implemented, in ASCII
#   direction   - "lower" | "higher" | "symmetric" (how the sub-score is built)
#   cap_key     - flowstate.toml [scorecard] key holding the cap/target, or None
#   scoring     - exact normalization applied to produce the 0-100 sub-score
#   category    - which of the 6 categories this sub-metric averages into
#   why         - plain English: why a production planner should care
# ---------------------------------------------------------------------------

CATEGORY_WEIGHT_KEYS = {
    "service": "weight_service",
    "changeovers": "weight_changeovers",
    "cip": "weight_cip",
    "campaigns": "weight_campaigns",
    "trials": "weight_trials",
}

CATEGORY_ORDER = ["service", "changeovers", "cip", "campaigns", "trials"]

CATEGORY_DOCS = {
    "service": (
        "Did we make what the customer ordered, on time? Scored only when "
        "data/reference/demand_plan.csv exists - otherwise the whole category is "
        "n/a and its weight is redistributed across the remaining categories."
    ),
    "changeovers": (
        "How much saleable time the week burns switching the line between recipes "
        "and pack formats. Every changeover is capacity you paid for and did not "
        "sell."
    ),
    "cip": (
        "Clean-in-place discipline: how often we clean, how long it takes, and how "
        "much of the allowed dirty-time budget we throw away by cleaning early."
    ),
    "campaigns": (
        "Run-length discipline. Long, consolidated campaigns are efficient; a week "
        "chopped into short runs bleeds changeover and ramp time. Scored "
        "longer-is-better against campaign_run_floor_h - long runs are never "
        "penalised."
    ),
    "trials": (
        "The cost of R&D / trial work on the plant floor: the hours it consumes and "
        "the production it interrupts."
    ),
}

# Known distortions in draft v0 - surfaced in the UI so nobody over-trusts a number.
KNOWN_LIMITATIONS = [
    (
        "Forfeited CIP is an estimate, not a measurement",
        "cip_forfeited_kg values each forfeited dirty-time hour at the line's "
        "AVERAGE kg/h across every SKU it is capable of running. The actual SKU "
        "that would have run in that hour may be faster or slower, so treat the "
        "kg figure as an order-of-magnitude cost, not an exact tonnage. If "
        "capabilities_rates.csv is missing the metric reads 0 kg and the CIP "
        "category quietly looks better than it is."
    ),
    (
        "Service is all-or-nothing",
        "Without demand_plan.csv the service category is None. It is dropped from "
        "the composite and its 0.30 weight is renormalized across the other five "
        "categories, which materially changes what the composite means."
    ),
    (
        "Excess inventory kg may be estimated, not measured",
        "When the calendar carries no qty_kg (a hand-built or pre-P3 schedule) the "
        "produced kg behind excess_inventory_kg are estimated as run_hours x the "
        "line's AVERAGE kg/h, the same caveat as forfeited CIP kg. The scored "
        "result sets excess_inventory_kg_estimated=true and the page marks the "
        "number '(estimated)'. Scoring an estimate is still more honest than the "
        "old behaviour, which dropped the kg component silently."
    ),
]

METRIC_DOCS: dict[str, dict[str, Any]] = {
    # --- changeovers -------------------------------------------------------
    "weighted_co": {
        "definition": (
            "Severity-weighted changeover load. Each transition is counted per "
            "machine it touches: FFS x3, topload x3, casepacker x2, TTP x1; a "
            "recipe-only change (no machine flags) counts x1."
        ),
        "formula": (
            "weighted_co = 3*ffs_changes + 3*topload_changes + "
            "2*casepacker_changes + 1*ttp_changes + 1*recipe_only_changes "
            "(weights configurable: co_weight_* in [scorecard])."
        ),
        "direction": "lower",
        "cap_key": "cap_weighted_co",
        "scoring": "score = clamp(100 * (1 - weighted_co / cap_weighted_co), 0, 100)",
        "category": "changeovers",
        "why": (
            "Not all changeovers cost the same (plant ranking 2026-08-14): FFS and "
            "topload are the expensive ones, casepacker is moderate, TTP is cheap. "
            "130 TTP/recipe swaps can be a GOOD schedule; 100 topload swaps never are. "
            "This is the scored changeover metric; the per-machine counts are shown "
            "alongside it."
        ),
    },
    "recipe_changes": {
        "definition": (
            "Count of adjacent production transitions on the same line where recipe/SKU "
            "family differs (changeover flags: flavor/organic/cinnamon; else SKU change "
            "that is not format-only)."
        ),
        "formula": (
            "For each line, sort production blocks by start_h; for each adjacent pair "
            "(a, b) with a.sku != b.sku, count 1 if the changeover-standards row flags "
            "conv_to_org_change=1 or cinn_to_non=1 or added_flavors>0, or the pair is "
            "not a pure format change, or no standards row exists."
        ),
        "direction": "lower",
        "cap_key": "cap_recipe_changes",
        "scoring": "score = clamp(100 * (1 - recipe_changes / cap_recipe_changes), 0, 100)",
        "category": "changeovers",
        "why": (
            "Every recipe switch means flushing, re-dosing and quality holds. Fewer "
            "recipe changes means more of the week is spent producing saleable product."
        ),
    },
    "format_changes": {
        "definition": (
            "Count of adjacent production transitions with packaging format change "
            "(topload / TTP / FFS / casepacker flags from changeover standards)."
        ),
        "formula": (
            "Same adjacent-pair scan; count 1 when any of topload_change, ttp_change, "
            "ffs_change, casepacker_change equals 1 in the changeover-standards row."
        ),
        "direction": "lower",
        "cap_key": "cap_format_changes",
        "scoring": "score = clamp(100 * (1 - format_changes / cap_format_changes), 0, 100)",
        "category": "changeovers",
        "why": (
            "Format changes are the mechanical ones - tooling, guide rails, case "
            "packer setup. They are usually the longest changeovers on the floor and "
            "the most likely to overrun."
        ),
    },
    "total_co_hours": {
        "definition": (
            "Sum of estimated changeover hours per transition from standards table, "
            "else configurable defaults by CO type. Idle gaps alone are not counted."
        ),
        "formula": (
            "Per transition: use setup_hours from the standards row when > 0; if the "
            "row exists but setup_hours <= 0, use default_co_hours_format (if format "
            "change) + default_co_hours_recipe (if recipe change), falling back to "
            "default_co_hours_base when both are 0. With no standards row: "
            "default_co_hours_base + default_co_hours_recipe (+ format uplift if "
            "flagged). Sum over all transitions."
        ),
        "direction": "lower",
        "cap_key": "cap_co_hours",
        "scoring": (
            "Reported only — NOT scored (2026-08-25): the setup_hours "
            "standards in changeovers.csv aren't trusted yet, so the "
            "changeovers category scores on weighted_co alone."
        ),
        "category": "changeovers",
        "why": (
            "This is the headline number: hours of line capacity consumed by changing "
            "over rather than running. It converts directly to lost cases."
        ),
    },
    # --- cip ---------------------------------------------------------------
    "cip_count": {
        "definition": (
            "Number of CIP blocks in the horizon. REPORTED ONLY - every CIP is "
            "mandated by cip_info, so the count is the same on any compliant "
            "schedule and cannot be optimized."
        ),
        "formula": "count(blocks where block_type == 'cip')",
        "direction": "lower",
        "cap_key": None,
        "scoring": (
            "Not scored. Operational context only - the CIP category scores "
            "overdue compliance (cip_overdue), and rewarding a lower count "
            "rewarded a schedule that never cleaned a line at all."
        ),
        "category": "cip",
        "why": (
            "Useful for reading the board (how many stops the week carries), but "
            "the cleans are a hygiene mandate, not a lever the planner can pull."
        ),
    },
    "cip_hours": {
        "definition": "Sum of CIP block durations (end_h - start_h).",
        "formula": "sum(max(0, end_h - start_h)) over blocks where block_type == 'cip'",
        "direction": "lower",
        "cap_key": "cap_cip_hours",
        "scoring": "score = clamp(100 * (1 - cip_hours / cap_cip_hours), 0, 100)",
        "category": "cip",
        "why": (
            "Total downtime handed to cleaning. Two short CIPs and one long one are "
            "not the same thing; this metric prices the duration, not just the count."
        ),
    },
    "cip_forfeited_h": {
        "definition": (
            "For each CIP: max(0, line_cip_interval_h - run_hours_since_last_cip). "
            "Cleaning earlier than the interval forfeits unused dirty-time budget. "
            "REPORTED ONLY - the scored version of this metric is cip_forfeited_kg."
        ),
        "formula": (
            "Per line, walk CIPs in start_h order with last_cip_end starting at 0. "
            "run_since = sum of production overlap in (last_cip_end, cip_start). "
            "forfeited += max(0, interval - run_since), where interval comes from "
            "reference/line_cip_hrs.csv (max_cip_hrs) or cip_interval_fallback_h. "
            "Then last_cip_end = cip.end_h."
        ),
        "direction": "lower",
        "cap_key": None,
        "scoring": (
            "Not scored. Kept as a diagnostic so the raw hour count stays visible; "
            "the CIP category scores cip_forfeited_kg instead."
        ),
        "category": "cip",
        "why": (
            "The raw hour count behind the kg figure. Useful for seeing WHERE the "
            "waste is in time terms, but hours alone treat a slow line and a fast "
            "line as equally costly, which is why the scored metric is in kg."
        ),
    },
    "cip_forfeited_kg": {
        "definition": (
            "Forfeited CIP dirty-time converted to KILOGRAMS of lost production, "
            "valuing each forfeited hour at that line's average run rate (kg/h)."
        ),
        "formula": (
            "Same per-line CIP walk as cip_forfeited_h; for each CIP add "
            "max(0, interval - run_since) * avg_rate_kgph(line). "
            "avg_rate_kgph(line) = mean of calc_rate_kgph over rows in "
            "reference/capabilities_rates.csv where capable == 1 for that "
            "line_name (then the overall line mean, then 0 if the file is absent)."
        ),
        "direction": "lower",
        "cap_key": "cap_cip_forfeited_kg",
        "scoring": (
            "score = clamp(100 * (1 - cip_forfeited_kg / cap_cip_forfeited_kg), 0, 100)"
        ),
        "category": "cip",
        "why": (
            "An hour lost on a 1,240 kg/h line costs more than twice an hour lost "
            "on a 540 kg/h line. Measuring the forfeited dirty-time budget in "
            "kilograms prices CIP discipline in the only unit the plant sells in, "
            "so cleaning early on a fast line is correctly flagged as the more "
            "expensive mistake."
        ),
    },
    "cip_req_violations": {
        "definition": (
            "SKU-to-SKU transitions flagged cip_req_after in changeovers.csv "
            "(protein hygiene) that run WITHOUT a CIP block between them."
        ),
        "formula": (
            "Per line, for each adjacent production pair whose changeovers.csv "
            "row has cip_req_after == 1: violation unless a CIP block lies "
            "fully inside the gap between the two runs. A transition at a CIP "
            "is fully waived from all changeover metrics (retooling happens "
            "during the clean)."
        ),
        "direction": "lower",
        "cap_key": None,
        "scoring": (
            "Compliance GATES the CIP category: any cip_overdue or "
            "cip_req_violations event -> category 0. When compliant, the "
            "category is the cip_forfeited_kg score (the capacity price of "
            "early cleans)."
        ),
        "category": "cip",
        "why": (
            "Running a flagged pair without a clean is a hygiene violation, "
            "not a preference. The fix is sequencing to an existing CIP "
            "boundary, or pulling the next CIP forward - which costs "
            "forfeited kilograms, priced by the other half of this category."
        ),
    },
    # --- trials ------------------------------------------------------------
    "trial_hours": {
        "definition": "Sum of trial block durations.",
        "formula": "sum(max(0, end_h - start_h)) over blocks where block_type == 'trial'",
        "direction": "lower",
        "cap_key": "cap_trial_hours",
        "scoring": (
            "score = clamp(100 * (1 - trial_hours / cap_trial_hours), 0, 100). "
            "Not scored at all when the week has no trial data (no trial blocks "
            "and no reference/trials.csv) - the category is n/a instead of 100."
        ),
        "category": "trials",
        "why": (
            "Trials are necessary but they occupy commercial assets. Knowing the hour "
            "count makes the trade-off with R&D visible instead of invisible."
        ),
    },
    "trial_disruptions": {
        "definition": (
            "Production blocks immediately before/after a trial on the same line that "
            "require a changeover (SKU differs) or are split by the trial."
        ),
        "formula": (
            "Per trial: add the number of production blocks on the same line that "
            "overlap the trial window (start_h < trial_end and end_h > trial_start). "
            "Then, if production exists both before and after and their SKUs differ, "
            "add 1; if production exists on only one side, add 1."
        ),
        "direction": "lower",
        "cap_key": "cap_trial_disruptions",
        "scoring": (
            "score = clamp(100 * (1 - trial_disruptions / cap_trial_disruptions), "
            "0, 100). Not scored when the week has no trial data (see trial_hours)."
        ),
        "category": "trials",
        "why": (
            "A trial dropped in the middle of a campaign costs far more than its own "
            "hours - it forces an extra changeover on each side. Scheduling trials at "
            "a natural break makes this number zero."
        ),
    },
    # --- campaigns ---------------------------------------------------------
    "avg_run_h": {
        "definition": "Mean duration (hours) of production blocks.",
        "formula": "mean(max(0, end_h - start_h)) over blocks where block_type == 'production'",
        "direction": "higher",
        "cap_key": "campaign_run_floor_h",
        "scoring": (
            "score = clamp(100 * avg_run_h / campaign_run_floor_h, 0, 100). "
            "Monotonic longer-is-better: the score ramps up from 0 to 100 as the "
            "average run grows from 0 to the floor and stays at 100 above it. "
            "Only SHORT runs are penalised; long campaigns are never punished. "
            "(The old symmetric target_avg_run_h remains in config but is unused.)"
        ),
        "category": "campaigns",
        "why": (
            "Long, consolidated campaigns are what the plant wants: every extra "
            "hour of uninterrupted running amortises the startup and changeover "
            "you already paid for. A week averaging under the floor is being "
            "chopped up; anything at or above it is running the way it should."
        ),
    },
    "short_run_count": {
        "definition": "Production blocks with duration < short_run_h (default 4h).",
        "formula": "count(production blocks where (end_h - start_h) < short_run_h)",
        "direction": "lower",
        "cap_key": "cap_short_runs",
        "scoring": "score = clamp(100 * (1 - short_run_count / cap_short_runs), 0, 100)",
        "category": "campaigns",
        "why": (
            "A sub-4-hour run barely clears startup and ramp. These are the runs that "
            "quietly destroy OEE and are the first candidates to consolidate."
        ),
    },
    # --- service -----------------------------------------------------------
    "orders_at_risk": {
        "definition": (
            "Orders whose scheduled completion is within at_risk_h of due end but still "
            "on time. n/a if demand data missing."
        ),
        "formula": (
            "For each demand order, scheduled_end = max(end_h) of its production "
            "blocks. Count orders where scheduled_end <= due_end_hour and "
            "scheduled_end >= due_end_hour - at_risk_h."
        ),
        "direction": "lower",
        "cap_key": "cap_orders_at_risk",
        "scoring": "score = clamp(100 * (1 - orders_at_risk / cap_orders_at_risk), 0, 100)",
        "category": "service",
        "why": (
            "These orders make the date on paper with no buffer. One breakdown or one "
            "slow changeover and they are late. This is your early-warning list."
        ),
    },
    "orders_late": {
        "definition": (
            "Orders with scheduled completion after due end, or unmet qty below min. "
            "n/a if demand data missing."
        ),
        "formula": (
            "Count demand orders where scheduled_end > due_end_hour, plus every order "
            "with no production block scheduled at all (scheduled_end is None)."
        ),
        "direction": "lower",
        "cap_key": "cap_orders_late",
        "scoring": "score = clamp(100 * (1 - orders_late / cap_orders_late), 0, 100)",
        "category": "service",
        "why": (
            "The one number the customer sees. Any schedule that looks efficient while "
            "missing dates is not a good schedule."
        ),
    },
    "excess_inventory_kg": {
        "definition": (
            "Sum of max(0, produced_qty - qty_max) across orders. n/a if no max bound."
        ),
        "formula": (
            "For each order with a qty_max (explicit, or qty_target * upper_pct): "
            "excess += max(0, produced_kg(order) - qty_max). produced_kg is the "
            "sum of qty_kg over the order's production blocks when the calendar "
            "carries real kg; when it does not (column absent, all-NaN or all-"
            "zero) it is ESTIMATED as sum(run_hours * the line's average kg/h) "
            "and excess_inventory_kg_estimated is set True. Returns None only "
            "when there is no production data and no rate table at all."
        ),
        "direction": "lower",
        "cap_key": "cap_excess_kg",
        "scoring": (
            "score = clamp(100 * (1 - excess_inventory_kg / cap_excess_kg), 0, 100). "
            "Omitted from the service average when None."
        ),
        "category": "service",
        "why": (
            "Overproducing to fill a convenient run is not free - it is cash and shelf "
            "life sitting in the warehouse. This keeps 'run it longer' honest."
        ),
    },
}

# Backward compatible: other modules (and saved scorecard JSON) import this flat
# {metric: prose} mapping. Derived from METRIC_DOCS so the two cannot drift.
FORMULA_HELP = {k: v["definition"] for k, v in METRIC_DOCS.items()}


def metric_docs(cfg: dict | None = None) -> dict[str, dict[str, Any]]:
    """METRIC_DOCS merged with the LIVE configured caps/targets and weights.

    Documentation only - reads config, never scores anything. Each entry gains:
      cap_value       - the configured number behind cap_key (None if no cap)
      cap_label       - "cap_x = 40" style display string
      category_weight - the live composite weight for the owning category
    """
    cfg = cfg or scorecard_config()
    out: dict[str, dict[str, Any]] = {}
    for key, doc in METRIC_DOCS.items():
        entry = dict(doc)
        cap_key = doc.get("cap_key")
        cap_value = cfg.get(cap_key) if cap_key else None
        entry["cap_value"] = cap_value
        if cap_key and cap_value is not None:
            entry["cap_label"] = f"{cap_key} = {cap_value:g}"
        else:
            entry["cap_label"] = "n/a"
        wkey = CATEGORY_WEIGHT_KEYS.get(doc["category"])
        entry["category_weight"] = float(cfg.get(wkey, 0.0)) if wkey else 0.0
        out[key] = entry
    return out


def category_weights(cfg: dict | None = None) -> dict[str, float]:
    """Live composite weights per category, in display order."""
    cfg = cfg or scorecard_config()
    return {k: float(cfg[CATEGORY_WEIGHT_KEYS[k]]) for k in CATEGORY_ORDER}


def metric_reference_rows(
    result: "ScorecardResult | dict[str, Any] | None" = None,
    cfg: dict | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Rows for the in-app metric reference, grouped by category.

    When `result` is supplied each row also carries the metric's live value and a
    saturation flag (value at/above its cap scores 0). Pure reporting - it never
    recomputes or alters a score.
    """
    cfg = cfg or scorecard_config()
    docs = metric_docs(cfg)
    data: dict[str, Any] = {}
    if result is not None:
        data = result.to_dict() if isinstance(result, ScorecardResult) else dict(result)

    grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORY_ORDER}
    for key, doc in docs.items():
        cat = doc["category"]
        value = (data.get(cat) or {}).get(key) if data else None
        saturated = False
        cap_value = doc.get("cap_value")
        if value is not None and cap_value is not None:
            try:
                if doc["direction"] == "lower":
                    saturated = float(value) >= float(cap_value)
                elif doc["direction"] == "symmetric":
                    saturated = abs(float(value) - float(cap_value)) >= float(cap_value)
            except (TypeError, ValueError):
                saturated = False
        note = ""
        if saturated and doc["direction"] == "lower":
            note = f"{key} {float(value):g} >= cap {float(cap_value):g} -> scores 0"
        elif saturated:
            note = (
                f"{key} {float(value):g} is >= 2x from target {float(cap_value):g} "
                "-> scores 0"
            )
        grouped[cat].append({
            "metric": key,
            "value": value,
            "formula": doc["formula"],
            "cap_or_target": doc["cap_label"],
            "how_scored": doc["scoring"],
            "why_it_matters": doc["why"],
            "direction": doc["direction"],
            "category_weight": doc["category_weight"],
            "saturated": saturated,
            "saturation_note": note,
        })
    return grouped


@dataclass
class ScorecardResult:
    week_label: str
    scored_at: str
    changeovers: dict[str, Any] = field(default_factory=dict)
    cip: dict[str, Any] = field(default_factory=dict)
    trials: dict[str, Any] = field(default_factory=dict)
    campaigns: dict[str, Any] = field(default_factory=dict)
    service: dict[str, Any] = field(default_factory=dict)
    category_scores: dict[str, float | None] = field(default_factory=dict)
    composite: float | None = None
    formulas: dict[str, str] = field(default_factory=lambda: dict(FORMULA_HELP))
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScorecardResult":
        return cls(
            week_label=str(data.get("week_label") or "unknown"),
            scored_at=str(data.get("scored_at") or ""),
            changeovers=dict(data.get("changeovers") or {}),
            cip=dict(data.get("cip") or {}),
            trials=dict(data.get("trials") or {}),
            campaigns=dict(data.get("campaigns") or {}),
            service=dict(data.get("service") or {}),
            category_scores=dict(data.get("category_scores") or {}),
            composite=data.get("composite"),
            formulas=dict(data.get("formulas") or FORMULA_HELP),
            notes=list(data.get("notes") or []),
        )


# The plant's own export names several flag columns differently (observed in
# the 2026-08-26 bridge file that introduced cip_req_after — without this
# rename, topload/casepacker/conv/cinn flags silently read 0). KEEP IN SYNC
# with solver/changeover_cache._COLUMN_ALIASES (duplicated on purpose — the
# solver stays import-independent of helpers).
CO_COLUMN_ALIASES = {
    "tpld_change": "topload_change",
    "cspkr_change": "casepacker_change",
    "conv_to_org": "conv_to_org_change",
    "cinn_to_non_cinn": "cinn_to_non",
}


def normalize_co_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename plant-export flag columns to the canonical names every reader
    keys on. Canonical columns pass through untouched."""
    ren = {a: c for a, c in CO_COLUMN_ALIASES.items()
           if a in df.columns and c not in df.columns}
    return df.rename(columns=ren) if ren else df


def _load_changeovers(ref: Path) -> pd.DataFrame:
    path = ref / "changeovers.csv"
    if not path.exists():
        # legacy filename
        alt = ref / "Changeovers.csv"
        path = alt if alt.exists() else path
    if not path.exists():
        return pd.DataFrame()
    df = normalize_co_columns(pd.read_csv(path))
    df["from_sku"] = df["from_sku"].astype(str)
    df["to_sku"] = df["to_sku"].astype(str)
    return df


def _co_lookup(co_df: pd.DataFrame) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    if co_df.empty:
        return out
    for _, r in co_df.iterrows():
        out[(str(r["from_sku"]), str(r["to_sku"]))] = r.to_dict()
    return out


def _load_flat_line_rates(ref: Path) -> dict[str, float]:
    """Flat rate_kgph per line from reference/line_rates.csv, keyed by Line name."""
    path = ref / "line_rates.csv"
    mapping: dict[str, float] = {}
    if not path.exists():
        return mapping
    try:
        df = pd.read_csv(path)
    except Exception:
        return mapping
    name_col = "Line" if "Line" in df.columns else "line_name"
    if name_col not in df.columns or "rate_kgph" not in df.columns:
        return mapping
    for _, r in df.iterrows():
        name = str(r.get(name_col, "")).strip()
        if not name:
            continue
        try:
            rate = float(r.get("rate_kgph", 0) or 0)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            mapping[name] = rate
    return mapping


def _load_sku_avg_rates(ref: Path) -> dict[str, float]:
    """Mean calc_rate_kgph per line over the SKUs that line is capable of running."""
    path = ref / "capabilities_rates.csv"
    mapping: dict[str, float] = {}
    try:
        if not path.exists():
            return mapping
        df = pd.read_csv(path)
        if "line_name" not in df.columns:
            return mapping
        if "calc_rate_kgph" not in df.columns and "rate_kgph" in df.columns:
            df = df.rename(columns={"rate_kgph": "calc_rate_kgph"})
        sums: dict[str, list[float]] = {}
        all_lines: set[str] = set()
        for _, r in df.iterrows():
            name = str(r.get("line_name", "")).strip()
            if not name:
                continue
            all_lines.add(name)
            try:
                if int(float(r.get("capable", 0) or 0)) != 1:
                    continue
            except (TypeError, ValueError):
                continue
            rate = 0.0
            try:
                v = float(r.get("calc_rate_kgph", 0) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                rate = v
            if rate > 0:
                sums.setdefault(name, []).append(rate)
        for name, vals in sums.items():
            mapping[name] = round(sum(vals) / len(vals), 2)
        if mapping:
            overall = round(sum(mapping.values()) / len(mapping), 2)
            for name in all_lines:
                mapping.setdefault(name, overall)
    except Exception:
        return {}
    return mapping


def _load_line_avg_rates(ref: Path) -> dict[str, float]:
    """Line -> kg/h used to value forfeited CIP dirty-time.

    Flat-rate mode (flowstate.toml [scheduler] use_sku_rates = false AND
    reference/line_rates.csv exists): each line's flat rate_kgph from
    line_rates.csv. Otherwise (or if the flat file is missing/empty): mean
    calc_rate_kgph per line over the SKUs that line is capable of running
    (capabilities_rates.csv).
    """
    cfg = load_toml()
    use_sku = bool((cfg.get("scheduler") or {}).get("use_sku_rates", True))
    if not use_sku:
        flat = _load_flat_line_rates(ref)
        if flat:
            return flat
    return _load_sku_avg_rates(ref)


def _load_cip_intervals(ref: Path, fallback: float) -> dict[str, float]:
    path = ref / "line_cip_hrs.csv"
    mapping: dict[str, float] = {}
    if path.exists():
        df = pd.read_csv(path)
        for _, r in df.iterrows():
            name = str(r.get("line_name", ""))
            mapping[name] = float(r.get("max_cip_hrs", fallback))
            mapping[str(r.get("line_id", ""))] = float(r.get("max_cip_hrs", fallback))
    return mapping


def cip_last_clean_hours(
    ref: Path, anchor: datetime, max_h: float | None = None,
) -> dict[str, float]:
    """Per-line hours-since-anchor of the last RECORDED clean (cip_info's
    PreviousCIP), for seeding score_cip's clean clock. A clean that lands
    INSIDE the horizon exists only as history here — the projected-CIP grid
    anchors on it but never draws it as a calendar block, so a calendar-only
    walk reads phantom dirty time (2026-08-24: four legal 76-78 schedules
    failed cip_ok on a clean recorded mid-batch by the live pull).

    Cleans at or before the anchor return nothing for that line: the walk's
    legacy "clean at horizon start" seed (0.0) already covers them, and
    seeding negative hours would strictly tighten the guard for real
    pre-anchor dirty carryover — a separate decision, not taken here.

    `max_h` (pass the frame's horizon) drops seeds at or beyond it: a
    "clean" dated past the horizon can't have preceded any block in it, and
    a mis-keyed future PreviousCIP would otherwise excuse every block on
    the line — the seed must never be able to disable the guard.
    """
    from helpers.cip_import import read_cip_info

    out: dict[str, float] = {}
    for line, info in read_cip_info(ref / "cip_info.csv").by_line.items():
        prev = info.previous_cip
        if prev is None or pd.isna(prev):
            continue
        if prev.tzinfo is not None:  # feed writes naive wall time; tolerate
            prev = prev.tz_localize(None)
        h = (prev.to_pydatetime() - anchor).total_seconds() / 3600.0
        if h > 0 and (max_h is None or h < float(max_h)):
            out[line] = h
    return out


def _production(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["block_type"] == "production"].copy()


def _by_type(df: pd.DataFrame, btype: str) -> pd.DataFrame:
    return df[df["block_type"] == btype].copy()


def _is_format_change(flags: dict | None) -> bool:
    if not flags:
        return False
    return any(
        int(flags.get(k, 0) or 0) == 1
        for k in ("topload_change", "ttp_change", "ffs_change", "casepacker_change")
    )


def _is_recipe_change(flags: dict | None, from_sku: str, to_sku: str) -> bool:
    if from_sku == to_sku:
        return False
    if flags:
        if any(
            int(flags.get(k, 0) or 0) == 1
            for k in ("conv_to_org_change", "cinn_to_non")
        ):
            return True
        if float(flags.get("added_flavors", 0) or 0) > 0:
            return True
        # SKU change with no format flags → treat as recipe
        if not _is_format_change(flags):
            return True
        # Both format and recipe possible — recipe if SKU family differs
        return True
    return True


def _round_half_up(x: float, ndigits: int = 0) -> float:
    """Round half AWAY from zero (for x >= 0), matching JS Math.round.

    The Gantt KPI numbers are recomputed client-side after edits; Python's
    banker's rounding would let e.g. 6.25% display as 6.2 here and 6.3 there.
    """
    m = 10.0 ** ndigits
    return math.floor(x * m + 0.5) / m


def _co_transition_hours(flags: dict | None, from_sku: str, to_sku: str, cfg: dict) -> float:
    """Estimated hours for one SKU transition — the ONLY place this rule lives.

    Both score_changeovers and the Gantt KPI payload (gantt_kpis / kpi.ts via
    co_pairs) price a transition through here, so the scorecard and the
    calendar KPI bar cannot disagree on changeover hours.
    """
    if flags and "setup_hours" in flags:
        h = float(flags.get("setup_hours") or 0)
        if h <= 0:
            h = 0.0
            if _is_format_change(flags):
                h += float(cfg["default_co_hours_format"])
            if _is_recipe_change(flags, from_sku, to_sku):
                h += float(cfg["default_co_hours_recipe"])
            if h == 0:
                h = float(cfg["default_co_hours_base"])
        return h
    h = float(cfg["default_co_hours_base"])
    if _is_format_change(flags):
        h += float(cfg["default_co_hours_format"]) - float(cfg["default_co_hours_base"])
    h += float(cfg["default_co_hours_recipe"])
    return h


def score_changeovers(
    calendar: pd.DataFrame,
    cfg: dict,
    co_map: dict,
    week_bounds: list[tuple[float, int]] | None = None,
    horizon_h: float | None = None,
) -> dict[str, Any]:
    """Changeover counts, machine severity, and hours over the calendar.

    `week_bounds` ([(start_hour, iso_week), ...], see _iso_week_bounds) adds
    a "weekly" block bucketing every transition into the ISO week the
    INCOMING block starts (same rule as weekly_breakdown) — the input to
    the per-week category score. Totals are unchanged either way.
    """
    import bisect

    prod = _production(calendar)
    recipe = 0
    fmt = 0
    hours = 0.0
    transitions = 0
    # Per-machine severity breakdown (plant ranking 2026-08-14): FFS and
    # topload changes hurt most, casepacker next, TTP least. A recipe-only
    # change (no machine touched) is the cheapest kind. The scored metric
    # is the WEIGHTED count, so 100 TTP swaps can beat 30 topload swaps.
    machine = {"topload": 0, "ffs": 0, "casepacker": 0, "ttp": 0}
    recipe_only = 0
    per_line: dict[str, int] = {}

    # Per-line CIP intervals: a transition whose gap fully contains a CIP
    # block is WAIVED from every changeover metric (the plant retools during
    # the clean — user rule 2026-08-26), and it satisfies cip_req_after.
    cips_by_line: dict[Any, list[tuple[float, float]]] = {}
    for _, c in _by_type(calendar, "cip").iterrows():
        cips_by_line.setdefault(c["line_id"], []).append(
            (float(c["start_h"]), float(c["end_h"])))

    def _cip_between(line_id: Any, a_end: float, b_start: float) -> bool:
        return any(cs >= a_end - 1e-6 and ce <= b_start + 1e-6
                   for cs, ce in cips_by_line.get(line_id, ()))

    cip_req_violations = 0
    transitions_at_cip = 0
    cip_req_detail: list[dict[str, Any]] = []

    marks: list[float] = [b for b, _ in (week_bounds or [])]
    wk_acc: dict[int, dict[str, float]] = {}

    def _acc(idx: int) -> dict[str, float]:
        return wk_acc.setdefault(idx, {
            "transitions": 0, "recipe": 0, "fmt": 0, "recipe_only": 0,
            "hours": 0.0, "prod_h": 0.0,
            "topload": 0, "ffs": 0, "casepacker": 0, "ttp": 0})

    def _wk_end(idx: int) -> float:
        if idx + 1 < len(marks):
            return marks[idx + 1]
        if horizon_h is not None:
            return float(horizon_h)
        return marks[idx] + 168.0

    for _, grp in prod.sort_values(["line_id", "start_h"]).groupby("line_id"):
        rows = grp.to_dict("records")
        line_name = str(rows[0].get("line_name", "") or "")
        line_count = 0
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            from_sku, to_sku = str(a.get("sku", "")), str(b.get("sku", ""))
            if from_sku == to_sku:
                continue
            flags = co_map.get((from_sku, to_sku))
            cip_req = bool(flags) and int(flags.get("cip_req_after", 0) or 0) == 1
            if _cip_between(a.get("line_id"),
                            float(a["end_h"]), float(b["start_h"])):
                # Fully waived: the clean subsumes the changeover work.
                transitions_at_cip += 1
                continue
            if cip_req:
                cip_req_violations += 1
                if len(cip_req_detail) < 50:
                    cip_req_detail.append({
                        "line": line_name,
                        "from_sku": from_sku,
                        "to_sku": to_sku,
                        "at_h": round(float(b["start_h"]), 2),
                    })
            transitions += 1
            line_count += 1
            wk = (_acc(max(0, bisect.bisect_right(marks, float(b["start_h"])) - 1))
                  if marks else None)
            if wk is not None:
                wk["transitions"] += 1
            if _is_recipe_change(flags, from_sku, to_sku):
                recipe += 1
                if wk is not None:
                    wk["recipe"] += 1
            if _is_format_change(flags):
                fmt += 1
                if wk is not None:
                    wk["fmt"] += 1
            touched = False
            for flag_key, name in (("topload_change", "topload"),
                                   ("ffs_change", "ffs"),
                                   ("casepacker_change", "casepacker"),
                                   ("ttp_change", "ttp")):
                if flags and int(flags.get(flag_key, 0) or 0) == 1:
                    machine[name] += 1
                    if wk is not None:
                        wk[name] += 1
                    touched = True
            if not touched:
                recipe_only += 1
                if wk is not None:
                    wk["recipe_only"] += 1
            t_hours = _co_transition_hours(flags, from_sku, to_sku, cfg)
            hours += t_hours
            if wk is not None:
                wk["hours"] += t_hours
        per_line[line_name] = per_line.get(line_name, 0) + line_count

    if marks:
        # production hours per week (overlap share) — a week with no
        # production is excluded from the per-week score, not gifted 100.
        for _, p in prod.iterrows():
            ps, pe = float(p["start_h"]), float(p["end_h"])
            for i, m in enumerate(marks):
                ov = max(0.0, min(pe, _wk_end(i)) - max(ps, m))
                if ov > 0:
                    _acc(i)["prod_h"] += ov
    def _weighted(top: float, f: float, csp: float, t: float, ronly: float) -> float:
        return (
            float(cfg.get("co_weight_topload", 3.0)) * top
            + float(cfg.get("co_weight_ffs", 3.0)) * f
            + float(cfg.get("co_weight_casepacker", 2.0)) * csp
            + float(cfg.get("co_weight_ttp", 1.0)) * t
            + float(cfg.get("co_weight_recipe_only", 1.0)) * ronly
        )

    weighted = _weighted(machine["topload"], machine["ffs"],
                         machine["casepacker"], machine["ttp"], recipe_only)

    weekly: list[dict[str, Any]] | None = None
    if marks:
        weekly = []
        for i in sorted(wk_acc):
            a = wk_acc[i]
            weekly.append({
                "week": int(week_bounds[i][1]),
                "start_h": round(marks[i], 2),
                "span_h": round(_wk_end(i) - marks[i], 2),
                "prod_h": round(a["prod_h"], 2),
                "sku_transitions": int(a["transitions"]),
                "recipe_changes": int(a["recipe"]),
                "format_changes": int(a["fmt"]),
                "weighted_co": round(_weighted(
                    a["topload"], a["ffs"], a["casepacker"], a["ttp"],
                    a["recipe_only"]), 1),
                "co_hours": _round_half_up(a["hours"], 2),
            })

    return {
        **({"weekly": weekly} if weekly is not None else {}),
        # Hygiene rule: flagged pairs without a CIP in the gap. Detail rows
        # feed the Reconcile finding and the guards.
        "cip_req_violations": cip_req_violations,
        "cip_req_detail": cip_req_detail,
        # Transitions fully waived because a CIP sits in the gap (retooling
        # happens during the clean) — excluded from every metric above.
        "transitions_at_cip": transitions_at_cip,
        "recipe_changes": recipe,
        "format_changes": fmt,
        "topload_changes": machine["topload"],
        "ffs_changes": machine["ffs"],
        "casepacker_changes": machine["casepacker"],
        "ttp_changes": machine["ttp"],
        "recipe_only_changes": recipe_only,
        "weighted_co": round(weighted, 1),
        # Half-up so the client-side recompute (JS Math.round) shows the same
        # number after an edit that changes nothing.
        "total_co_hours": _round_half_up(hours, 2),
        "sku_transitions": transitions,
        "per_line_transitions": per_line,
    }


def score_cip(
    calendar: pd.DataFrame,
    cfg: dict,
    intervals: dict[str, float],
    rates: dict[str, float] | None = None,
    last_clean: dict[str, float] | None = None,
) -> dict[str, Any]:
    """CIP discipline. `rates` maps line_name -> average kg/h (see
    _load_line_avg_rates); forfeited dirty-time hours are converted to
    forfeited KILOGRAMS of lost production at that line's average run rate.
    A missing rate contributes 0 kg rather than raising.

    `last_clean` maps line_name/line_id -> hours-since-anchor of the last
    recorded clean (see cip_last_clean_hours). It seeds each line's clean
    clock so a clean that exists only in cip_info history — never as a
    calendar block — still resets the walk. None/absent lines keep the
    legacy "clean at horizon start" seed of 0.0.
    """
    rates = rates or {}
    last_clean = last_clean or {}

    def _seed(line_name: str, line_id: Any) -> float:
        return float(last_clean.get(line_name)
                     or last_clean.get(str(line_id)) or 0.0)
    cips = _by_type(calendar, "cip").sort_values(["line_id", "start_h"])
    prod = _production(calendar)
    count = len(cips)
    hours = float((cips["end_h"] - cips["start_h"]).clip(lower=0).sum()) if count else 0.0
    forfeited = 0.0
    forfeited_kg = 0.0
    # Overdue-CIP count: a line that RUNS PAST its max interval without a clean
    # is a hygiene failure, not a virtue. The old "fewer CIPs = higher score"
    # rewarded a schedule that never cleaned a line (measured: a 0-CIP rough
    # draft scored 100 while lines ran 500h past a 120h interval). Mirror
    # check_cip_spacing: count each line-segment whose clock-since-last-CIP
    # exceeds the interval. This makes "no CIPs" score ~0 on this component.
    overdue = 0

    for line_id, cip_grp in cips.groupby("line_id"):
        line_name = str(cip_grp["line_name"].iloc[0]) if len(cip_grp) else str(line_id)
        interval = float(
            intervals.get(line_name)
            or intervals.get(str(line_id))
            or cfg["cip_interval_fallback_h"]
        )
        rate = float(rates.get(line_name) or rates.get(str(line_id)) or 0.0)
        line_prod = prod[prod["line_id"] == line_id].sort_values("start_h")
        last_cip_end = _seed(line_name, line_id)
        for _, cip in cip_grp.iterrows():
            cip_start = float(cip["start_h"])
            run_since = 0.0
            for _, p in line_prod.iterrows():
                ps, pe = float(p["start_h"]), float(p["end_h"])
                if pe <= last_cip_end:
                    continue
                if ps >= cip_start:
                    break
                run_since += max(0.0, min(pe, cip_start) - max(ps, last_cip_end))
            lost_h = max(0.0, interval - run_since)
            forfeited += lost_h
            forfeited_kg += lost_h * rate
            # max(): a stale calendar block (drawn from a pre-refresh
            # cip_info) may END before the recorded clean it duplicates.
            last_cip_end = max(last_cip_end, float(cip["end_h"]))

    # Overdue pass: for lines that have production but few/no CIPs, walk the
    # whole horizon and count how many times clock-since-last-CIP exceeds the
    # interval. Production without a following clean before interval is overdue.
    for line_id, line_prod in prod.groupby("line_id"):
        # MUST be time-sorted: the fill-mode calendar stores committed blocks
        # and fill blocks in separate runs of rows, and walking them in file
        # order marched the CIP pointer past early cleans — 90 phantom
        # overdue events zeroed the CIP category (found 2026-08-14).
        line_prod = line_prod.sort_values("start_h")
        line_name = str(line_prod["line_name"].iloc[0]) if len(line_prod) else str(line_id)
        interval = float(
            intervals.get(line_name)
            or intervals.get(str(line_id))
            or cfg["cip_interval_fallback_h"]
        )
        line_cips = cips[cips["line_id"] == line_id].sort_values("start_h")
        last_cip_end = _seed(line_name, line_id)
        cip_idx = 0
        cip_starts = [float(c["start_h"]) for _, c in line_cips.iterrows()]
        cip_ends = [float(c["end_h"]) for _, c in line_cips.iterrows()]
        for _, p in line_prod.iterrows():
            ps, pe = float(p["start_h"]), float(p["end_h"])
            # apply any CIPs that start before this production block
            while cip_idx < len(cip_starts) and cip_starts[cip_idx] <= ps:
                last_cip_end = max(last_cip_end, cip_ends[cip_idx])
                cip_idx += 1
            clock_at_end = pe - last_cip_end
            if clock_at_end > interval + 12:
                overdue += 1

    return {
        "cip_count": int(count),
        "cip_hours": round(hours, 2),
        # Reported for continuity / diagnostics; the scored metric is the kg one.
        "cip_forfeited_h": round(forfeited, 2),
        "cip_forfeited_kg": round(forfeited_kg, 2),
        # Overdue cleanings: lines that ran past their max CIP interval.
        # Drives the "not enough CIPs" side of the CIP score.
        "cip_overdue": int(overdue),
    }


def score_trials(calendar: pd.DataFrame, co_map: dict) -> dict[str, Any]:
    trials = _by_type(calendar, "trial")
    hours = float((trials["end_h"] - trials["start_h"]).clip(lower=0).sum()) if len(trials) else 0.0
    disruptions = 0
    prod = _production(calendar)
    for _, t in trials.iterrows():
        line = t["line_id"]
        ts, te = float(t["start_h"]), float(t["end_h"])
        line_prod = prod[prod["line_id"] == line].sort_values("start_h")
        before = line_prod[line_prod["end_h"] <= ts].tail(1)
        after = line_prod[line_prod["start_h"] >= te].head(1)
        # split: production overlapping trial window
        overlap = line_prod[(line_prod["start_h"] < te) & (line_prod["end_h"] > ts)]
        disruptions += len(overlap)
        if len(before) and len(after):
            if str(before.iloc[0]["sku"]) != str(after.iloc[0]["sku"]):
                disruptions += 1
        elif len(before) or len(after):
            disruptions += 1
    # Availability, not performance. With no trial blocks in the calendar
    # there is nothing to score: trial_hours=0 then means "no trial data",
    # not "a perfectly trial-free week", and scoring it 100 is a silent-100
    # on absent data. category_scores turns available=False into a None (n/a)
    # category instead. (Calendar block_type == "trial" is the only source;
    # reference/trials.csv was retired 2026-08-14 — manprg TRIALS pseudo-MOs
    # became blocked line time.)
    return {
        "trial_hours": round(hours, 2),
        "trial_disruptions": int(disruptions),
        "available": bool(len(trials)),
    }


def score_campaigns(calendar: pd.DataFrame, cfg: dict) -> dict[str, Any]:
    prod = _production(calendar)
    if prod.empty:
        return {"avg_run_h": None, "short_run_count": 0, "production_blocks": 0, "total_prod_h": 0.0}
    durs = (prod["end_h"] - prod["start_h"]).clip(lower=0)
    short = int((durs < float(cfg["short_run_h"])).sum())
    return {
        "avg_run_h": round(float(durs.mean()), 2),
        "short_run_count": short,
        "production_blocks": int(len(prod)),
        "total_prod_h": round(float(durs.sum()), 2),
    }


def _estimated_produced(
    prod: pd.DataFrame,
    data_dir: Path | None,
    rates: dict[str, float] | None,
) -> dict[str, float]:
    """order_id -> estimated produced kg = run_hours x the line's avg kg/h.

    Same avg-rate table (reference/capabilities_rates.csv, via
    _load_line_avg_rates) that values forfeited CIP kg, so the estimate
    carries the same known caveat: an average over every SKU the line can
    run, not the rate of the SKU actually scheduled. Empty dict when no
    rates are reachable -- the caller then reports excess as None.
    """
    if rates is None:
        if data_dir is None:
            return {}
        rates = _load_line_avg_rates(reference_dir(data_dir))
    if not rates or prod.empty:
        return {}
    out: dict[str, float] = {}
    for _, p in prod.iterrows():
        rate = float(
            rates.get(str(p.get("line_name", "")))
            or rates.get(str(p.get("line_id", "")))
            or 0.0
        )
        if rate <= 0:
            continue
        run_h = max(0.0, float(p["end_h"]) - float(p["start_h"]))
        oid = str(p.get("order_id", ""))
        out[oid] = out.get(oid, 0.0) + run_h * rate
    return out


def score_service(
    calendar: pd.DataFrame,
    cfg: dict,
    demand: pd.DataFrame | None,
    data_dir: Path | None = None,
    rates: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Service metrics: lateness, at-risk orders and excess inventory kg.

    Excess kg needs produced kg per order. When the calendar carries real
    `qty_kg` (solver schedules do since P3) that is used as-is and
    `excess_inventory_kg_estimated` is False. When it does not, the kg are
    ESTIMATED as run_hours x the line's average rate (the same avg-rate table
    used to value forfeited CIP kg), and the flag is True -- so the score
    still includes the kg part instead of silently dropping it. `rates` is
    the pre-loaded line -> kg/h map; when absent it is loaded from
    `data_dir/reference/`. With neither, no estimate is possible and the
    previous behaviour (excess None) stands.
    """
    if demand is None or demand.empty:
        return {
            "orders_at_risk": None,
            "orders_late": None,
            "excess_inventory_kg": None,
            "excess_inventory_kg_estimated": False,
            "available": False,
        }
    prod = _production(calendar)
    at_risk = 0
    late = 0
    excess = 0.0
    at_risk_h = float(cfg["at_risk_h"])

    # Aggregate scheduled end and qty by order_id
    estimated = False
    if prod.empty:
        scheduled_end: dict[str, float] = {}
        produced: dict[str, float] = {}
    else:
        scheduled_end = prod.groupby("order_id")["end_h"].max().to_dict()
        has_qty = (
            "qty_kg" in prod.columns
            and pd.to_numeric(prod["qty_kg"], errors="coerce").fillna(0).abs().sum() > 0
        )
        if has_qty:
            produced = (
                pd.to_numeric(prod["qty_kg"], errors="coerce")
                .fillna(0)
                .groupby(prod["order_id"])
                .sum()
                .to_dict()
            )
        else:
            produced = _estimated_produced(prod, data_dir, rates)
            estimated = bool(produced)

    for _, o in demand.iterrows():
        oid = str(o.get("order_id", ""))
        due = float(o.get("due_end_hour", o.get("due_end", 0)) or 0)
        end = scheduled_end.get(oid)
        if end is None:
            late += 1
            continue
        if end > due:
            late += 1
        elif end >= due - at_risk_h:
            at_risk += 1

        qty_max = o.get("qty_max")
        if qty_max is None or (isinstance(qty_max, float) and pd.isna(qty_max)):
            # derive from target * upper_pct if present
            target = o.get("qty_target")
            upper = o.get("upper_pct")
            if target is not None and upper is not None and not pd.isna(target) and not pd.isna(upper):
                qty_max = float(target) * float(upper)
            else:
                qty_max = None
        if qty_max is not None and oid in produced:
            excess += max(0.0, float(produced[oid]) - float(qty_max))

    return {
        "orders_at_risk": at_risk,
        "orders_late": late,
        "excess_inventory_kg": round(excess, 1) if produced else None,
        "excess_inventory_kg_estimated": estimated,
        "available": True,
        "orders_total": int(len(demand)),
    }


def _clamp01(x: float) -> float:
    return max(0.0, min(100.0, x))


def _score_lower_better(value: float | None, cap: float) -> float | None:
    if value is None:
        return None
    if cap <= 0:
        return 100.0
    return _clamp01(100.0 * (1.0 - float(value) / cap))


def _score_higher_better(value: float | None, target: float) -> float | None:
    if value is None:
        return None
    if target <= 0:
        return 100.0
    return _clamp01(100.0 * float(value) / target)


def category_scores(raw: dict[str, dict], cfg: dict) -> dict[str, float | None]:
    co = raw["changeovers"]
    cip = raw["cip"]
    tr = raw["trials"]
    camp = raw["campaigns"]
    svc = raw["service"]

    # Per-ISO-week changeover score (2026-08-25): the caps are ONE-WEEK
    # calibrations, and a multi-week horizon's TOTALS saturate them (112
    # recipe changes vs cap 40 pinned the category at 0 — no discrimination
    # between schedules). Each production week scores against its pro-rated
    # cap (span/168), is floored at co_week_score_floor, and the weeks
    # combine as a decay-weighted geometric product (weight ~ decay^i): the
    # near week dominates and one blown week cannot be averaged away by
    # clean ones. Falls back to flat totals for scorecards saved before the
    # weekly block existed.
    co_week_score: float | None = None
    co_weeks = [w for w in (co.get("weekly") or [])
                if float(w.get("prod_h") or 0.0) > 0.0]
    # total_co_hours is REPORTED but not scored (2026-08-25): it prices
    # transitions from changeovers.csv setup_hours, which the plant does
    # not yet trust. The scored signal is the weighted severity count.
    if co_weeks:
        lam = float(cfg.get("co_week_decay", 0.6) or 0.6)
        floor_pts = 100.0 * float(cfg.get("co_week_score_floor", 0.05) or 0.0)
        cap_w = float(cfg.get("cap_weighted_co") or 120)
        decay = [lam ** i for i in range(len(co_weeks))]
        wsum = sum(decay) or 1.0
        log_score = 0.0
        for i, w in enumerate(co_weeks):
            frac = max(float(w.get("span_h") or 168.0) / 168.0, 1e-6)
            s_w = _score_lower_better(w.get("weighted_co"), cap_w * frac)
            s = max(s_w if s_w is not None else 100.0, floor_pts, 1e-9)
            log_score += (decay[i] / wsum) * math.log(s / 100.0)
        co_week_score = 100.0 * math.exp(log_score)
        co_s: list[float | None] = []
    # Severity-weighted changeover score (2026-08-14): FFS/topload count 3x,
    # casepacker 2x, TTP 1x, recipe-only 1x. Falls back to the legacy
    # recipe/format pair for scorecards saved before weighted_co existed.
    elif co.get("weighted_co") is not None:
        co_s = [
            _score_lower_better(
                co["weighted_co"], float(cfg.get("cap_weighted_co") or 120)),
        ]
    else:
        co_s = [
            _score_lower_better(co["recipe_changes"], cfg["cap_recipe_changes"]),
            _score_lower_better(co["format_changes"], cfg["cap_format_changes"]),
        ]
    # CIPs are MANDATORY and cannot be late/overdue — they are a hard hygiene
    # compliance requirement, not a soft preference. So the CIP category is
    # scored on OVERDUE compliance alone: any line-segment that runs past its
    # max interval without a clean collapses the category toward 0 (cap is 1,
    # so one overdue event → score 0). A fully compliant schedule (every line
    # cleaned within interval) scores 100. cip_count / cip_hours /
    # cip_forfeited_kg are reported for diagnostics but deliberately NOT
    # averaged in, because "fewer CIPs = higher score" rewarded a schedule
    # that never cleaned a line at all.
    #
    # 2026-08-26 (cip_req_after): the category is now COMPLIANCE blended
    # with FORFEITED KG. Compliance = no overdue lines AND no required-CIP
    # violations (a flagged SKU pair run without a clean between). Forfeited
    # kg prices the capacity cost of cleaning early — pulling a CIP forward
    # to satisfy a flagged transition is legal but not free. CIP count stays
    # reported-only (same reason as ever). Old scorecards lack
    # cip_req_violations -> treated as 0, so compliance reduces to the
    # legacy overdue check.
    _viol = int(co.get("cip_req_violations") or 0)
    _overdue = cip.get("cip_overdue")
    if _overdue is not None and (int(_overdue or 0) > 0 or _viol > 0):
        # Compliance GATES the category: hygiene failure -> 0, full stop.
        cip_s: list[float | None] = [0.0]
    else:
        cip_s = [
            _score_lower_better(
                cip.get("cip_forfeited_kg"),
                float(cfg.get("cap_cip_forfeited_kg") or 0)),
            # None forfeited data (very old scorecards) -> compliance alone.
            *([] if cip.get("cip_forfeited_kg") is not None else [100.0]),
        ]
    # Trials score only when there IS trial data (calendar trial blocks).
    # Without it the category is None -- the
    # composite drops it and the page greys it out -- because a 0-hour, 0-
    # disruption week with no trial input is absence of data, not a 100.
    if tr.get("available", True):
        trial_s: list[float | None] = [
            _score_lower_better(tr["trial_hours"], cfg["cap_trial_hours"]),
            _score_lower_better(tr["trial_disruptions"], cfg["cap_trial_disruptions"]),
        ]
    else:
        trial_s = []
    camp_parts = [
        _score_lower_better(camp["short_run_count"], cfg["cap_short_runs"]),
    ]
    if camp.get("avg_run_h") is not None:
        # Monotonic "longer is better" with a floor. The plant confirms long,
        # consolidated runs are always preferable, so ONLY short runs are
        # penalised: the score ramps linearly from 0 to 100 as avg_run_h goes
        # 0 -> campaign_run_floor_h and stays at 100 above the floor. The old
        # symmetric target (target_avg_run_h) is left in config but unused.
        avg = float(camp["avg_run_h"])
        floor = float(cfg.get("campaign_run_floor_h") or 0.0)
        camp_parts.append(_score_higher_better(avg, floor))

    if svc.get("available"):
        svc_parts = [
            _score_lower_better(svc["orders_late"], cfg["cap_orders_late"]),
            _score_lower_better(svc["orders_at_risk"], cfg["cap_orders_at_risk"]),
        ]
        if svc.get("excess_inventory_kg") is not None:
            svc_parts.append(_score_lower_better(svc["excess_inventory_kg"], cfg["cap_excess_kg"]))
        service_score: float | None = sum(svc_parts) / len(svc_parts)
    else:
        service_score = None

    def avg(parts: list[float | None]) -> float:
        vals = [p for p in parts if p is not None]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    return {
        "changeovers": co_week_score if co_week_score is not None else avg(co_s),
        "cip": avg(cip_s),
        "trials": None if not trial_s else avg(trial_s),
        "campaigns": avg(camp_parts),
        "service": None if service_score is None else round(service_score, 1),
    }


def composite_score(cats: dict[str, float | None], cfg: dict) -> float | None:
    """Weighted mean of the category scores present in `cats`.

    Any category missing from `cats` (score is None, e.g. Service with no
    demand_plan.csv, or a category removed from CATEGORY_ORDER entirely) is
    simply excluded from both the numerator and the weight sum (`den`), so
    the remaining categories' weights renormalise to fill the gap.
    """
    weights = {
        "service": float(cfg["weight_service"]),
        "changeovers": float(cfg["weight_changeovers"]),
        "cip": float(cfg["weight_cip"]),
        "campaigns": float(cfg["weight_campaigns"]),
        "trials": float(cfg["weight_trials"]),
    }
    num = 0.0
    den = 0.0
    for k, w in weights.items():
        v = cats.get(k)
        if v is None:
            continue
        num += w * float(v)
        den += w
    if den <= 0:
        return None
    return round(num / den, 1)


def load_demand(ref: Path) -> pd.DataFrame | None:
    path = ref / "demand_plan.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if "qty_max" not in df.columns and "qty_target" in df.columns and "upper_pct" in df.columns:
        df["qty_max"] = df["qty_target"] * df["upper_pct"]
    if "qty_min" not in df.columns and "qty_target" in df.columns and "lower_pct" in df.columns:
        df["qty_min"] = df["qty_target"] * df["lower_pct"]
    # Frame reconciliation (audit 2026-08-15): demand hours are offsets from
    # the demand file's OWN anchor (demand_plan.source.json); calendar hours
    # are offsets from the planning anchor. Comparing them raw made
    # orders_late / at-risk off by the anchor gap. Shift demand into the
    # planning frame; weeks that ended before it are already-history rows
    # the Service metrics should not chase.
    try:
        import json as _json

        from helpers import horizon as _hz
        from helpers.config import load_toml as _lt

        meta_p = ref / "demand_plan.source.json"
        if meta_p.exists():
            _da = str(_json.loads(
                meta_p.read_text(encoding="utf-8")).get("anchor") or "")
            if _da:
                from datetime import datetime as _dt
                shift_h = (_hz.resolve(_lt()).anchor - _dt.strptime(
                    _da, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600.0
                if shift_h:
                    df["due_start_hour"] = (
                        pd.to_numeric(df["due_start_hour"], errors="coerce")
                        - shift_h).clip(lower=0)
                    df["due_end_hour"] = pd.to_numeric(
                        df["due_end_hour"], errors="coerce") - shift_h
                    df = df[df["due_end_hour"] > 0].copy()
    except Exception:  # noqa: BLE001 — scoring must not die on frame meta
        pass
    return df


def _apply_fill_window(
    calendar: pd.DataFrame,
    gates: dict[str, float],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reduce a fill-mode calendar to the region the solver actually decided.

    Scenario F fixes the committed plan (manprg MOs, trials, projected CIPs)
    and only places fill after each line's committed WORK tail — the gate.
    Scoring the whole calendar charges a proposal for a committed layer it
    cannot touch, and compares unequal scopes (a ~2-week board vs a 3-week
    plan). The window keeps, per line:
      - every block past the gate (the fill region),
      - every CIP block regardless of side (the clean history — dropping
        pre-gate CIPs corrupts clock-since-last-clean for the fill region),
      - the LAST pre-gate production block (the changeover base, so the
        committed→fill boundary changeover is charged to the proposal).
    Applied with the SAME gates to the official board, the shared committed
    parts cancel in any delta — what remains is the fill decision itself.

    Returns (windowed calendar, pre-gate committed production) — the latter
    feeds the residual-demand subtraction.
    """
    if calendar.empty:
        return calendar, calendar.iloc[0:0]
    g = {str(k).strip().upper(): float(v) for k, v in (gates or {}).items()}
    ln = calendar["line_name"].astype(str).str.strip().str.upper()
    gate = ln.map(g).fillna(0.0)
    end_h = pd.to_numeric(calendar["end_h"], errors="coerce").fillna(0.0)
    pre = end_h <= gate + 1e-6
    btype = calendar["block_type"].astype(str).str.lower()
    committed_prod = calendar[pre & (btype == "production")]
    keep = (~pre) | (btype == "cip")
    if len(committed_prod):
        base_idx = (
            pd.to_numeric(committed_prod["end_h"], errors="coerce")
            .groupby(ln[committed_prod.index])
            .idxmax()
        )
        keep.loc[base_idx.dropna()] = True
    return calendar[keep].copy(), committed_prod


def _residual_fill_demand(
    demand: pd.DataFrame,
    committed_prod: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """Demand minus committed production — what the fill was ASKED to make.

    Mirrors the staging subtraction (_overlay_fill step 4): committed kg
    bucketed on TRUE ISO-Monday bounds in the resolved planning frame,
    consumed per SKU with surplus carrying forward. qty_min/qty_max are
    re-derived from the reduced target, and orders fully covered by the
    committed plan are DROPPED — they are the plant's work, and counting
    them "late" against the fill was the core unfairness this view fixes.

    Returns (residual demand, number of fully-covered orders dropped).
    """
    from helpers.plan_fill import subtract_committed

    if (demand is None or demand.empty
            or "week_index" not in demand.columns
            or "qty_target" not in demand.columns):
        return demand, 0
    week_bounds: list[float] | None = None
    try:
        from datetime import timedelta as _td

        from helpers import horizon as _hz
        from helpers.config import load_toml as _lt

        hres = _hz.resolve(_lt())
        mon0 = hres.anchor - _td(days=hres.anchor.weekday())
        b = (mon0 + _td(days=7) - hres.anchor).total_seconds() / 3600.0
        week_bounds = [0.0]
        while b < float(hres.hours):
            week_bounds.append(b)
            b += 168.0
    except Exception:  # noqa: BLE001 — scoring must not die on frame meta
        week_bounds = None  # subtract_committed falls back to the 168h grid
    residual, _notes = subtract_committed(
        demand, committed_prod, week_bounds=week_bounds)
    tgt = pd.to_numeric(residual["qty_target"], errors="coerce").fillna(0.0)
    if "upper_pct" in residual.columns:
        residual["qty_max"] = tgt * pd.to_numeric(
            residual["upper_pct"], errors="coerce")
    if "lower_pct" in residual.columns:
        residual["qty_min"] = tgt * pd.to_numeric(
            residual["lower_pct"], errors="coerce")
    covered = tgt <= 0.5
    return residual[~covered].copy(), int(covered.sum())


# ---------------------------------------------------------------------------
# Gantt KPI payload — single source of truth for the calendar KPI bar.
#
# The React Gantt (components/gantt) renders this payload verbatim until the
# user edits the schedule; after an edit its kpi.ts recomputes LIVE with the
# SAME rules, using the co_pairs classification map below so the changeover
# severity/hour rules never have to be re-implemented client-side.
# tests/test_kpi_parity.py holds a golden fixture asserting Python and the
# built TypeScript agree; change rules here and there together.
# ---------------------------------------------------------------------------


def compute_adherence(
    calendar: pd.DataFrame,
    demand_targets: list[dict[str, Any]],
    caps: dict[str, dict[str, float]],
    covered_by_order: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Qty-based order adherence rows for the Gantt adherence table.

    Canonical rules (mirrored exactly by kpi.ts computeAdherence; change
    both or neither — tests/test_kpi_parity.py holds the golden fixture):
      - production blocks only — trials are blocked hours, never tonnage
        (user rule 2026-08-14)
      - the block's own qty_kg (the solver's real decomposition) wins over
        rate x duration; rate x duration is the fallback for unknown kg
      - blocks whose order_id matches no demand order (committed manprg
        MOs) ALWAYS waterfall onto that SKU's orders earliest-due first,
        each order taking at most its qty_max — surplus beyond every open
        order stays uncredited (serves weeks not on the board; the old
        last-order-takes-remainder rule read 1764% once)
      - covered_by_order (order_id -> kg) is NON-BOARD kg ONLY: made kg
        from completed MOs the board hides (the calendar page builds it
        with build_ledger_from_data(made_only=True)). Board kg always
        counts from the board; the credit only ever ADDS kg no visible
        block represents. The old covered mode disabled the waterfall and
        made the ledger the whole number — the card was unfalsifiable
        against the calendar it sat on (user mandate 2026-08-21: the
        metrics must score calendar_blocks.csv). Passing a map that
        includes committed-MO kg double-counts by construction.
      - pct is % of TARGET, the (qty_min+qty_max)/2 midpoint — the
        planner's band is 90-110 of target, not of qty_min
      - MET when qty_min <= scheduled <= qty_max (qty_max <= 0 = unbounded)
    """
    prod = _production(calendar)
    sched: dict[str, float] = {}
    unmatched_by_sku: dict[str, float] = {}
    demand_ids = {str(d.get("order_id", "")) for d in demand_targets}
    for _, b in prod.iterrows():
        line = str(b.get("line_name", "") or "")
        sku = str(b.get("sku", "") or "")
        kg = pd.to_numeric(pd.Series([b.get("qty_kg")]), errors="coerce").iloc[0]
        if pd.isna(kg) or kg <= 0:
            rate = float((caps.get(line) or {}).get(sku, 0) or 0)
            kg = rate * max(0.0, float(b["end_h"]) - float(b["start_h"]))
        oid = str(b.get("order_id", "") or "")
        if oid in demand_ids:
            sched[oid] = sched.get(oid, 0.0) + float(kg)
        else:
            unmatched_by_sku[sku] = unmatched_by_sku.get(sku, 0.0) + float(kg)

    if covered_by_order is not None:
        for oid, kg in covered_by_order.items():
            if str(oid) in demand_ids and kg and kg > 0:
                sched[str(oid)] = sched.get(str(oid), 0.0) + float(kg)

    by_sku: dict[str, list[dict[str, Any]]] = {}
    for d in demand_targets:
        by_sku.setdefault(str(d.get("sku", "")), []).append(d)
    for orders in by_sku.values():
        orders.sort(key=lambda d: float(d.get("due_start_hour", 0) or 0))
    for sku, kg in unmatched_by_sku.items():
        for d in by_sku.get(sku, []):
            if kg <= 1e-9:
                break
            oid = str(d.get("order_id", ""))
            have = sched.get(oid, 0.0)
            cap = max(float(d.get("qty_max", 0) or 0),
                      float(d.get("qty_min", 0) or 0))
            take = min(kg, max(0.0, cap - have))
            if take > 0:
                sched[oid] = have + take
                kg -= take

    rows: list[dict[str, Any]] = []
    for d in demand_targets:
        oid = str(d.get("order_id", ""))
        qty_min = float(d.get("qty_min", 0) or 0)
        qty_max = float(d.get("qty_max", 0) or 0)
        sq = sched.get(oid, 0.0)
        target = ((qty_min + qty_max) / 2.0
                  if qty_min > 0 and qty_max >= qty_min
                  else max(qty_min, qty_max))
        pct = (sq / target) * 100.0 if target > 0 else (999.0 if sq > 0 else 100.0)
        status = "MET"
        if sq < qty_min:
            status = "UNDER"
        elif qty_max > 0 and sq > qty_max:
            status = "OVER"
        rows.append({
            "order_id": oid,
            "sku": str(d.get("sku", "")),
            "qty_min": qty_min,
            "qty_max": qty_max,
            "scheduled_qty": int(_round_half_up(sq)),
            "pct_adherence": _round_half_up(pct, 1),
            "status": status,
        })
    rows.sort(key=lambda r: str(r["sku"]))
    return rows


def gantt_kpis(
    calendar: pd.DataFrame,
    demand_targets: list[dict[str, Any]],
    caps: dict[str, dict[str, float]],
    *,
    cfg: dict | None = None,
    co_map: dict | None = None,
    data_dir: Path | None = None,
    covered_by_order: dict[str, float] | None = None,
) -> dict[str, Any]:
    """KPI payload for gantt_calendar(kpis=...) — computed with scorecard rules.

    `demand_targets` and `caps` must be the SAME objects handed to the Gantt
    component, so the live client-side recompute sees identical inputs.
    Changeover numbers come from score_changeovers, so the KPI bar and the
    scorecard below it are definitionally the same numbers.
    """
    cfg = cfg or scorecard_config()
    if co_map is None:
        ref = reference_dir(data_dir) if data_dir else reference_dir()
        co_map = _co_lookup(_load_changeovers(ref))

    adherence = compute_adherence(calendar, demand_targets, caps,
                                  covered_by_order=covered_by_order)
    met = sum(1 for r in adherence if r["status"] == "MET")
    total = len(adherence)
    pct = _round_half_up(met / total * 100.0, 1) if total else 100.0

    co = score_changeovers(calendar, cfg, co_map)

    # Per-pair classification so the frontend can price/classify a transition
    # by lookup instead of re-implementing the severity rules.
    co_pairs: dict[str, dict[str, Any]] = {}
    for (from_sku, to_sku), flags in co_map.items():
        if from_sku == to_sku:
            continue
        co_pairs[f"{from_sku}|{to_sku}"] = {
            "recipe": 1 if _is_recipe_change(flags, from_sku, to_sku) else 0,
            "format": 1 if _is_format_change(flags) else 0,
            "hours": _round_half_up(_co_transition_hours(flags, from_sku, to_sku, cfg), 4),
            # machine touched (per-week chips + severity display client-side)
            "tl": 1 if int(flags.get("topload_change", 0) or 0) == 1 else 0,
            "ffs": 1 if int(flags.get("ffs_change", 0) or 0) == 1 else 0,
            "cp": 1 if int(flags.get("casepacker_change", 0) or 0) == 1 else 0,
            "ttp": 1 if int(flags.get("ttp_change", 0) or 0) == 1 else 0,
            # a CIP is required between these SKUs (waived at a CIP window)
            "cip_req": 1 if int(flags.get("cip_req_after", 0) or 0) == 1 else 0,
        }
    # A pair with no standards row: recipe by definition, unknown format,
    # base + recipe default hours (exactly the score_changeovers fallback).
    co_default = {
        "recipe": 1,
        "format": 0,
        "hours": _round_half_up(_co_transition_hours(None, "_from", "_to", cfg), 4),
        "tl": 0, "ffs": 0, "cp": 0, "ttp": 0, "cip_req": 0,
    }

    return {
        "adherence": adherence,
        "pct_adherence": pct,
        "orders_met": met,
        "orders_total": total,
        "changeovers": {
            "recipe_changes": co["recipe_changes"],
            "format_changes": co["format_changes"],
            "total_co_hours": co["total_co_hours"],
            "sku_transitions": co["sku_transitions"],
        },
        "per_line_changeovers": co["per_line_transitions"],
        "co_pairs": co_pairs,
        "co_default": co_default,
        # NON-BOARD credit (made kg from hidden completed MOs) forwarded so
        # the client's LIVE recompute (kpi.ts) uses the same baseline —
        # never committed-MO kg: those ARE board blocks and count as such
        "covered_by_order": covered_by_order or {},
    }


def score_calendar(
    calendar: pd.DataFrame,
    *,
    week_label: str = "current",
    data_dir: Path | None = None,
    cfg: dict | None = None,
    fill_gates: dict[str, float] | None = None,
    cip_last_clean: dict[str, float] | None = None,
) -> ScorecardResult:
    cfg = cfg or scorecard_config()
    ref = reference_dir(data_dir) if data_dir else reference_dir()
    co_map = _co_lookup(_load_changeovers(ref))
    intervals = _load_cip_intervals(ref, float(cfg["cip_interval_fallback_h"]))
    rates = _load_line_avg_rates(ref)
    demand = load_demand(ref)

    notes: list[str] = []
    if calendar.empty:
        notes.append("Calendar is empty — all metrics are zero / n/a.")
    if not co_map:
        notes.append("No changeover standards loaded — using default CO hour estimates.")
    if not rates:
        notes.append(
            "No capabilities_rates.csv — forfeited CIP kg cannot be valued and reads 0."
        )
    if demand is None:
        notes.append("No demand_plan.csv — Service metrics are n/a.")

    # Normalize dtypes ONCE. In-memory calendars (scenario reassembly)
    # concatenate committed + fill frames with mixed line_id types (int vs
    # str) — groupby/== then split one line into two, so CIPs "vanish" from
    # their line (90 phantom overdue events, forfeited kg doubled) and
    # committed->fill transitions on the same line are missed. Float-parsed
    # sku ("280323.0") likewise misses every changeover-standards row.
    calendar = calendar.copy()
    if "line_id" in calendar.columns:
        calendar["line_id"] = calendar["line_id"].astype(str).str.replace(
            r"\.0$", "", regex=True)
    if "sku" in calendar.columns:
        calendar["sku"] = calendar["sku"].astype(str).str.replace(
            r"\.0$", "", regex=True)
    for _c in ("start_h", "end_h", "qty_kg"):
        if _c in calendar.columns:
            calendar[_c] = pd.to_numeric(calendar[_c], errors="coerce")

    if fill_gates is not None:
        calendar, _committed_prod = _apply_fill_window(calendar, fill_gates)
        n_covered = 0
        if demand is not None:
            demand, n_covered = _residual_fill_demand(demand, _committed_prod)
        notes.append(
            "Fill-window view: committed blocks before each line's gate are "
            "excluded (changeover base + CIP history kept); demand reduced "
            f"by committed production ({n_covered} fully-covered order(s) "
            "left to the plant's own plan).")

    # ISO-week bounds for the per-week changeover score. Same frame rule as
    # weekly_breakdown (rolling anchor from the live toml); a resolve
    # failure degrades to flat totals, never takes scoring down.
    try:
        from helpers import horizon as _hzmod

        _hz = _hzmod.resolve(load_toml())
        _week_bounds = _iso_week_bounds(_hz.anchor, float(_hz.hours))
        _horizon_h = float(_hz.hours)
    except Exception:  # noqa: BLE001
        _hz, _week_bounds, _horizon_h = None, None, None

    # Default the CIP clean-clock seed from cip_info history. A clean the
    # live pull records mid-day exists only as PreviousCIP — the projected
    # grid anchors on it but draws no calendar block — so a seedless walk
    # reads phantom dirty time, and with the category gated on
    # cip_overdue == 0 that zeroes CIP on every UI rescore. Only
    # compute_guards passed the seed explicitly; every page path scored
    # seedless. An explicit argument (compute_guards, including its {} =
    # "score without seed" failure path) still wins; the seed is a
    # LENIENCY, so any failure here degrades to seedless, never to a
    # failed score.
    if cip_last_clean is None and data_dir is not None and _hz is not None:
        try:
            cip_last_clean = cip_last_clean_hours(
                ref, _hz.anchor, max_h=_horizon_h)
        except Exception:  # noqa: BLE001
            cip_last_clean = None

    raw = {
        "changeovers": score_changeovers(
            calendar, cfg, co_map, _week_bounds, _horizon_h),
        "cip": score_cip(calendar, cfg, intervals, rates, cip_last_clean),
        "trials": score_trials(calendar, co_map),
        "campaigns": score_campaigns(calendar, cfg),
        "service": score_service(calendar, cfg, demand, data_dir, rates),
    }
    cats = category_scores(raw, cfg)
    comp = composite_score(cats, cfg)

    return ScorecardResult(
        week_label=week_label,
        scored_at=datetime.now().isoformat(timespec="seconds"),
        changeovers=raw["changeovers"],
        cip=raw["cip"],
        trials=raw["trials"],
        campaigns=raw["campaigns"],
        service=raw["service"],
        category_scores=cats,
        composite=comp,
        notes=notes,
    )


SCORING_INPUT_FILES = (
    "demand_plan.csv", "demand_plan.source.json", "line_cip_hrs.csv",
    "capabilities_rates.csv", "changeovers.csv",
    # line_rates.csv is the rate source when use_sku_rates = false (the live
    # config) — it prices cip_forfeited_kg, a scored metric (review 2026-08-19).
    "line_rates.csv",
    # cip_info.csv seeds the CIP clean clock (cip_last_clean_hours default in
    # score_calendar) — a live-pull refresh must invalidate cached scores.
    "cip_info.csv",
)


def scoring_inputs_signature(data_dir: Path) -> tuple[float, ...]:
    """st.cache_data key material: mtimes of every live file score_calendar
    reads besides the calendar itself, plus flowstate.toml (caps/weights/
    anchor). Cached scores keyed only by a version's own files would go
    stale when the live feeds move (~30 min); this signature invalidates
    them the moment any scoring input changes."""
    from helpers.paths import toml_path as _tp

    ref = Path(data_dir) / "reference"
    paths = [ref / name for name in SCORING_INPUT_FILES] + [_tp()]
    sig: list[float] = []
    for p in paths:
        try:
            sig.append(p.stat().st_mtime)
        except OSError:
            sig.append(0.0)
    return tuple(sig)


def save_scorecard(result: ScorecardResult, data_dir: Path, filename: str | None = None) -> Path:
    d = scorecards_dir(data_dir)
    name = filename or f"{result.week_label}_{result.scored_at.replace(':', '').replace('-', '')}.json"
    name = name.replace(" ", "_")
    path = d / name
    safe_write_json(result.to_dict(), path)
    # also write latest pointer
    safe_write_json(result.to_dict(), d / "latest.json")
    return path


def list_scorecards(data_dir: Path) -> list[dict[str, Any]]:
    d = scorecards_dir(data_dir)
    items = []
    for p in sorted(d.glob("*.json"), reverse=True):
        if p.name == "latest.json":
            continue
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return items


def delta_narrative(baseline: ScorecardResult, proposed: ScorecardResult) -> list[str]:
    """Human-readable 'show me why' deltas for version / scenario compare."""
    lines: list[str] = []
    pairs = [
        ("changeovers", "topload_changes", "topload changeovers"),
        ("changeovers", "ffs_changes", "FFS changeovers"),
        ("changeovers", "casepacker_changes", "casepacker changeovers"),
        ("changeovers", "ttp_changes", "TTP changeovers"),
        ("changeovers", "recipe_only_changes", "recipe-only changeovers"),
        ("changeovers", "weighted_co", "weighted changeover load"),
        ("changeovers", "total_co_hours", "changeover hours"),
        ("cip", "cip_hours", "CIP hours"),
        ("cip", "cip_forfeited_h", "forfeited CIP hours"),
        ("cip", "cip_forfeited_kg", "forfeited CIP kg"),
        ("trials", "trial_hours", "trial hours"),
        ("trials", "trial_disruptions", "trial disruptions"),
        ("campaigns", "short_run_count", "short runs"),
        ("campaigns", "avg_run_h", "avg run hours"),
        ("service", "orders_late", "late orders"),
        ("service", "orders_at_risk", "orders at risk"),
    ]
    for section, key, label in pairs:
        a = getattr(baseline, section).get(key)
        b = getattr(proposed, section).get(key)
        if a is None or b is None:
            continue
        try:
            da, db = float(a), float(b)
        except (TypeError, ValueError):
            continue
        diff = db - da
        if abs(diff) < 1e-6:
            continue
        better_higher = key in ("avg_run_h",)
        improved = (diff > 0) if better_higher else (diff < 0)
        sign = "+" if diff > 0 else ""
        tag = "better" if improved else "worse"
        lines.append(f"{sign}{diff:g} {label} ({tag})")
    if baseline.composite is not None and proposed.composite is not None:
        d = proposed.composite - baseline.composite
        lines.insert(0, f"Composite {baseline.composite:g} → {proposed.composite:g} ({d:+.1f})")
    return lines


def contribution_breakdown(
    result: ScorecardResult | dict[str, Any],
    cfg: dict | None = None,
) -> list[dict[str, Any]]:
    """Per-category contribution to composite: score × weight, plus cap-saturation notes."""
    cfg = cfg or scorecard_config()
    data = result.to_dict() if isinstance(result, ScorecardResult) else result
    cats = data.get("category_scores") or {}
    raw = {
        "changeovers": data.get("changeovers") or {},
        "cip": data.get("cip") or {},
        "trials": data.get("trials") or {},
        "campaigns": data.get("campaigns") or {},
        "service": data.get("service") or {},
    }
    weights = {
        "service": float(cfg["weight_service"]),
        "changeovers": float(cfg["weight_changeovers"]),
        "cip": float(cfg["weight_cip"]),
        "campaigns": float(cfg["weight_campaigns"]),
        "trials": float(cfg["weight_trials"]),
    }
    # Cap saturation hints
    caps = {
        "changeovers": [
            ("recipe_changes", "cap_recipe_changes"),
            ("format_changes", "cap_format_changes"),
            # total_co_hours dropped 2026-08-25: reported, not scored, so a
            # saturated cap is not a scoring problem worth hinting about.
        ],
        "cip": [
            ("cip_hours", "cap_cip_hours"),
            ("cip_forfeited_kg", "cap_cip_forfeited_kg"),
        ],
        "trials": [
            ("trial_hours", "cap_trial_hours"),
            ("trial_disruptions", "cap_trial_disruptions"),
        ],
        "campaigns": [("short_run_count", "cap_short_runs")],
        "service": [
            ("orders_late", "cap_orders_late"),
            ("orders_at_risk", "cap_orders_at_risk"),
        ],
    }
    rows: list[dict[str, Any]] = []
    active_w = sum(w for k, w in weights.items() if cats.get(k) is not None) or 1.0
    for key, weight in weights.items():
        score = cats.get(key)
        if score is None:
            continue
        points = round(float(score) * weight / active_w, 1)
        sat_notes = []
        for metric, cap_key in caps.get(key, []):
            val = raw.get(key, {}).get(metric)
            cap = cfg.get(cap_key)
            if val is None or cap is None:
                continue
            try:
                if float(val) >= float(cap):
                    sat_notes.append(f"{metric} {val:g} ≥ cap {cap:g}")
            except (TypeError, ValueError):
                continue
        rows.append({
            "category": key,
            "score": round(float(score), 1),
            "weight": weight,
            "contribution": points,
            "cap_saturation": "; ".join(sat_notes) if sat_notes else "",
        })
    return rows
# ---------------------------------------------------------------------------
# Weekly breakdown — every headline metric split by TRUE ISO week, so a
# version can be argued per week: "better for W35 because topload drops 9".
# ---------------------------------------------------------------------------


def _iso_week_bounds(anchor, horizon_h: float) -> list[tuple[float, int]]:
    """[(start_hour, iso_week_number), ...] — Monday marks in the anchor
    frame. bounds[0] is hour 0 (the anchor's own, possibly partial, week)."""
    from datetime import timedelta

    out = [(0.0, anchor.isocalendar()[1])]
    mon0 = anchor - timedelta(days=anchor.weekday())
    b = (mon0 + timedelta(days=7) - anchor).total_seconds() / 3600.0
    while b < horizon_h:
        wk_dt = anchor + timedelta(hours=b + 1)
        out.append((b, wk_dt.isocalendar()[1]))
        b += 168.0
    return out


def weekly_breakdown(
    calendar: pd.DataFrame,
    *,
    data_dir: Path | None = None,
    cfg: dict | None = None,
) -> pd.DataFrame:
    """One row per ISO week: fill kg, fulfillment vs the demand due that
    week, orders met, changeovers by machine type, CO hours, CIP count/
    hours, avg run length, short runs.

    Weeks are TRUE ISO weeks in the resolved planning frame (Monday marks
    from the rolling anchor). Blocks bucket by midpoint; a changeover
    buckets into the week the incoming block STARTS (the swap happens at
    that boundary); demand orders bucket by due-window midpoint after the
    load_demand frame shift. NOTE: a calendar saved in an older anchor
    frame (un-rolled board) will read shifted — roll first.
    """
    import bisect

    from helpers import horizon as _hzmod
    from helpers.config import load_toml as _lt

    cfg = cfg or scorecard_config()
    ref = reference_dir(data_dir) if data_dir else reference_dir()
    co_map = _co_lookup(_load_changeovers(ref))
    demand = load_demand(ref)
    hz = _hzmod.resolve(_lt())
    bounds = _iso_week_bounds(hz.anchor, float(hz.hours))
    marks = [b for b, _ in bounds]
    labels = {i: f"W{wk}" for i, (_, wk) in enumerate(bounds)}

    def _wk_of(hour: float) -> int:
        return max(0, bisect.bisect_right(marks, hour) - 1)

    # normalize exactly like score_calendar
    cal = calendar.copy()
    if "line_id" in cal.columns:
        cal["line_id"] = cal["line_id"].astype(str).str.replace(
            r"\.0$", "", regex=True)
    if "sku" in cal.columns:
        cal["sku"] = cal["sku"].astype(str).str.replace(
            r"\.0$", "", regex=True)
    for _c in ("start_h", "end_h", "qty_kg"):
        if _c in cal.columns:
            cal[_c] = pd.to_numeric(cal[_c], errors="coerce")

    idx = list(labels)
    agg = {i: {"week": labels[i], "prod_kg": 0.0, "run_h": 0.0, "blocks": 0,
               "short_runs": 0, "topload": 0, "ffs": 0, "casepacker": 0,
               "ttp": 0, "recipe_only": 0, "weighted_co": 0.0,
               "co_hours": 0.0, "cip_count": 0, "cip_hours": 0.0,
               "demand_kg": 0.0, "scheduled_kg": 0.0, "orders": 0,
               "orders_met": 0} for i in idx}

    prod = _production(cal)
    short_h = float(cfg.get("short_run_h", 4.0))
    for _, b in prod.iterrows():
        dur = max(0.0, float(b["end_h"]) - float(b["start_h"]))
        wk = _wk_of((float(b["start_h"]) + float(b["end_h"])) / 2.0)
        if wk not in agg:
            continue
        kg = pd.to_numeric(pd.Series([b.get("qty_kg")]), errors="coerce").iloc[0]
        agg[wk]["prod_kg"] += 0.0 if pd.isna(kg) else float(kg)
        agg[wk]["run_h"] += dur
        agg[wk]["blocks"] += 1
        if dur < short_h:
            agg[wk]["short_runs"] += 1

    w_top = float(cfg.get("co_weight_topload", 3.0))
    w_ffs = float(cfg.get("co_weight_ffs", 3.0))
    w_cp = float(cfg.get("co_weight_casepacker", 2.0))
    w_ttp = float(cfg.get("co_weight_ttp", 1.0))
    w_ro = float(cfg.get("co_weight_recipe_only", 1.0))
    for _, grp in prod.sort_values(["line_id", "start_h"]).groupby("line_id"):
        rows = grp.to_dict("records")
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            f_sku, t_sku = str(a.get("sku", "")), str(b.get("sku", ""))
            if f_sku == t_sku:
                continue
            wk = _wk_of(float(b["start_h"]))
            if wk not in agg:
                continue
            flags = co_map.get((f_sku, t_sku))
            touched = False
            for key, name, w in (("topload_change", "topload", w_top),
                                 ("ffs_change", "ffs", w_ffs),
                                 ("casepacker_change", "casepacker", w_cp),
                                 ("ttp_change", "ttp", w_ttp)):
                if flags and int(flags.get(key, 0) or 0) == 1:
                    agg[wk][name] += 1
                    agg[wk]["weighted_co"] += w
                    touched = True
            if not touched:
                agg[wk]["recipe_only"] += 1
                agg[wk]["weighted_co"] += w_ro
            agg[wk]["co_hours"] += _co_transition_hours(flags, f_sku, t_sku, cfg)

    cips = _by_type(cal, "cip")
    for _, c in cips.iterrows():
        wk = _wk_of((float(c["start_h"]) + float(c["end_h"])) / 2.0)
        if wk in agg:
            agg[wk]["cip_count"] += 1
            agg[wk]["cip_hours"] += max(
                0.0, float(c["end_h"]) - float(c["start_h"]))

    if demand is not None and len(demand):
        caps: dict[str, dict[str, float]] = {}
        caps_p = ref / "capabilities_rates.csv"
        if caps_p.exists():
            _cdf = pd.read_csv(caps_p, dtype={"sku": str})
            rate_col = ("calc_rate_kgph" if "calc_rate_kgph" in _cdf.columns
                        else "rate_kgph" if "rate_kgph" in _cdf.columns
                        else None)
            if rate_col:
                for _, r in _cdf.iterrows():
                    if int(pd.to_numeric(r.get("capable"), errors="coerce")
                           or 0) == 1:
                        caps.setdefault(str(r.get("line_name", "")), {})[
                            str(r["sku"])] = float(
                            pd.to_numeric(r.get(rate_col), errors="coerce")
                            or 0)
        targets = []
        for _, d in demand.iterrows():
            t = float(pd.to_numeric(d.get("qty_target"), errors="coerce") or 0)
            lo = float(pd.to_numeric(d.get("lower_pct"), errors="coerce") or 0.9)
            hi = float(pd.to_numeric(d.get("upper_pct"), errors="coerce") or 1.1)
            qmin = d.get("qty_min")
            qmax = d.get("qty_max")
            targets.append({
                "order_id": str(d.get("order_id", "")),
                "sku": str(d.get("sku", "")),
                "qty_min": float(pd.to_numeric(qmin, errors="coerce")
                                 if qmin is not None else t * lo) or t * lo,
                "qty_max": float(pd.to_numeric(qmax, errors="coerce")
                                 if qmax is not None else t * hi) or t * hi,
                "due_start_hour": float(pd.to_numeric(
                    d.get("due_start_hour"), errors="coerce") or 0),
                "due_end_hour": float(pd.to_numeric(
                    d.get("due_end_hour"), errors="coerce") or 0),
            })
        rows_adh = compute_adherence(cal, targets, caps)
        by_order = {r["order_id"]: r for r in rows_adh}
        for t in targets:
            _mid = (t["due_start_hour"] + t["due_end_hour"]) / 2.0
            if _mid >= float(hz.hours):
                continue  # due beyond the horizon — not this plan's problem
            wk = _wk_of(_mid)
            if wk not in agg:
                continue
            r = by_order.get(t["order_id"])
            tgt = ((t["qty_min"] + t["qty_max"]) / 2.0
                   if t["qty_min"] > 0 and t["qty_max"] >= t["qty_min"]
                   else max(t["qty_min"], t["qty_max"]))
            agg[wk]["demand_kg"] += tgt
            agg[wk]["orders"] += 1
            if r:
                # Week credit caps at the order's own target: overproduction
                # on one order cannot serve another order's demand, so excess
                # must not raise the week's fulfilled_pct (audit 2026-08-18:
                # uncapped credit read W35 96% vs 93.7% true). kpi.ts
                # weekFulfillmentCredit applies the identical cap client-side.
                agg[wk]["scheduled_kg"] += min(float(r["scheduled_qty"]), tgt)
                if r["status"] == "MET":
                    agg[wk]["orders_met"] += 1

    out_rows = []
    for i in idx:
        a = agg[i]
        if a["blocks"] == 0 and a["orders"] == 0 and a["cip_count"] == 0:
            continue  # empty tail week — noise, not signal
        a["fulfilled_pct"] = (round(100.0 * a["scheduled_kg"] / a["demand_kg"], 1)
                              if a["demand_kg"] > 0 else None)
        a["avg_run_h"] = (round(a["run_h"] / a["blocks"], 1)
                          if a["blocks"] else 0.0)
        for k in ("prod_kg", "demand_kg", "scheduled_kg"):
            a[k] = round(a[k])
        for k in ("co_hours", "cip_hours", "weighted_co", "run_h"):
            a[k] = round(a[k], 1)
        out_rows.append(a)
    cols = ["week", "prod_kg", "demand_kg", "scheduled_kg", "fulfilled_pct",
            "orders_met", "orders", "topload", "ffs", "casepacker", "ttp",
            "recipe_only", "weighted_co", "co_hours", "cip_count",
            "cip_hours", "avg_run_h", "short_runs", "blocks"]
    return pd.DataFrame(out_rows, columns=cols)

