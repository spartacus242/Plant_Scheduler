# helpers/scorecard_engine.py — Operational truth model (Phase 0).
#
# Same functions score the current schedule, DnD what-if calendars, and solver scenarios.
# Draft v0 formulas — thresholds live in flowstate.toml [scorecard].

from __future__ import annotations

import json
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
        "scoring": "score = clamp(100 * (1 - total_co_hours / cap_co_hours), 0, 100)",
        "category": "changeovers",
        "why": (
            "This is the headline number: hours of line capacity consumed by changing "
            "over rather than running. It converts directly to lost cases."
        ),
    },
    # --- cip ---------------------------------------------------------------
    "cip_count": {
        "definition": "Number of CIP blocks in the horizon.",
        "formula": "count(blocks where block_type == 'cip')",
        "direction": "lower",
        "cap_key": "cap_cip_count",
        "scoring": "score = clamp(100 * (1 - cip_count / cap_cip_count), 0, 100)",
        "category": "cip",
        "why": (
            "Each CIP is a full stop on the line plus chemical and water cost. Running "
            "the same recipe longer between cleans means fewer of them."
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


def _load_changeovers(ref: Path) -> pd.DataFrame:
    path = ref / "changeovers.csv"
    if not path.exists():
        # legacy filename
        alt = ref / "Changeovers.csv"
        path = alt if alt.exists() else path
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
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


def score_changeovers(calendar: pd.DataFrame, cfg: dict, co_map: dict) -> dict[str, Any]:
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
    for _, grp in prod.sort_values(["line_id", "start_h"]).groupby("line_id"):
        rows = grp.to_dict("records")
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            from_sku, to_sku = str(a.get("sku", "")), str(b.get("sku", ""))
            if from_sku == to_sku:
                continue
            transitions += 1
            flags = co_map.get((from_sku, to_sku))
            if _is_recipe_change(flags, from_sku, to_sku):
                recipe += 1
            if _is_format_change(flags):
                fmt += 1
            touched = False
            for flag_key, name in (("topload_change", "topload"),
                                   ("ffs_change", "ffs"),
                                   ("casepacker_change", "casepacker"),
                                   ("ttp_change", "ttp")):
                if flags and int(flags.get(flag_key, 0) or 0) == 1:
                    machine[name] += 1
                    touched = True
            if not touched:
                recipe_only += 1
            elif flags is None:
                # no standards row — assume format unknown; count base hours only
                pass
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
                hours += h
            else:
                hours += float(cfg["default_co_hours_base"])
                if _is_format_change(flags):
                    hours += float(cfg["default_co_hours_format"]) - float(cfg["default_co_hours_base"])
                hours += float(cfg["default_co_hours_recipe"])
    weighted = (
        float(cfg.get("co_weight_topload", 3.0)) * machine["topload"]
        + float(cfg.get("co_weight_ffs", 3.0)) * machine["ffs"]
        + float(cfg.get("co_weight_casepacker", 2.0)) * machine["casepacker"]
        + float(cfg.get("co_weight_ttp", 1.0)) * machine["ttp"]
        + float(cfg.get("co_weight_recipe_only", 1.0)) * recipe_only
    )
    return {
        "recipe_changes": recipe,
        "format_changes": fmt,
        "topload_changes": machine["topload"],
        "ffs_changes": machine["ffs"],
        "casepacker_changes": machine["casepacker"],
        "ttp_changes": machine["ttp"],
        "recipe_only_changes": recipe_only,
        "weighted_co": round(weighted, 1),
        "total_co_hours": round(hours, 2),
        "sku_transitions": transitions,
    }


def score_cip(
    calendar: pd.DataFrame,
    cfg: dict,
    intervals: dict[str, float],
    rates: dict[str, float] | None = None,
) -> dict[str, Any]:
    """CIP discipline. `rates` maps line_name -> average kg/h (see
    _load_line_avg_rates); forfeited dirty-time hours are converted to
    forfeited KILOGRAMS of lost production at that line's average run rate.
    A missing rate contributes 0 kg rather than raising.
    """
    rates = rates or {}
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
        last_cip_end = 0.0  # assume clean at horizon start
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
            last_cip_end = float(cip["end_h"])

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
        last_cip_end = 0.0
        cip_idx = 0
        cip_starts = [float(c["start_h"]) for _, c in line_cips.iterrows()]
        cip_ends = [float(c["end_h"]) for _, c in line_cips.iterrows()]
        for _, p in line_prod.iterrows():
            ps, pe = float(p["start_h"]), float(p["end_h"])
            # apply any CIPs that start before this production block
            while cip_idx < len(cip_starts) and cip_starts[cip_idx] <= ps:
                last_cip_end = cip_ends[cip_idx]
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


def _trials_input_present(data_dir: Path | None) -> bool:
    """True when reference/trials.csv exists AND has at least one data row.

    A header-only (or unreadable) trials.csv counts as absent: there is no
    trial demand to schedule, so the trials category has nothing to measure.
    """
    if data_dir is None:
        return False
    path = reference_dir(data_dir) / "trials.csv"
    if not path.exists():
        return False
    try:
        return len(pd.read_csv(path)) > 0
    except Exception:
        return False


def score_trials(
    calendar: pd.DataFrame, co_map: dict, data_dir: Path | None = None
) -> dict[str, Any]:
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
    # Availability, not performance. With no trial blocks in the calendar AND
    # no reference/trials.csv there is nothing to score: trial_hours=0 then
    # means "no trial data", not "a perfectly trial-free week", and scoring it
    # 100 is a silent-100 on absent data. category_scores turns available=False
    # into a None (n/a) category instead.
    return {
        "trial_hours": round(hours, 2),
        "trial_disruptions": int(disruptions),
        "available": bool(len(trials)) or _trials_input_present(data_dir),
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

    # Severity-weighted changeover score (2026-08-14): FFS/topload count 3x,
    # casepacker 2x, TTP 1x, recipe-only 1x. Falls back to the legacy
    # recipe/format pair for scorecards saved before weighted_co existed.
    if co.get("weighted_co") is not None:
        co_s = [
            _score_lower_better(
                co["weighted_co"], float(cfg.get("cap_weighted_co") or 120)),
            _score_lower_better(co["total_co_hours"], cfg["cap_co_hours"]),
        ]
    else:
        co_s = [
            _score_lower_better(co["recipe_changes"], cfg["cap_recipe_changes"]),
            _score_lower_better(co["format_changes"], cfg["cap_format_changes"]),
            _score_lower_better(co["total_co_hours"], cfg["cap_co_hours"]),
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
    cip_s = [
        _score_lower_better(
            cip.get("cip_overdue"), float(cfg.get("cap_cip_overdue") or 1)
        ),
    ]
    # Trials score only when there IS trial data (calendar blocks or a
    # non-empty reference/trials.csv). Without it the category is None -- the
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
        "changeovers": avg(co_s),
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
    return df


def score_calendar(
    calendar: pd.DataFrame,
    *,
    week_label: str = "current",
    data_dir: Path | None = None,
    cfg: dict | None = None,
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

    raw = {
        "changeovers": score_changeovers(calendar, cfg, co_map),
        "cip": score_cip(calendar, cfg, intervals, rates),
        "trials": score_trials(calendar, co_map, data_dir),
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
        ("cip", "cip_count", "CIPs"),
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
            ("total_co_hours", "cap_co_hours"),
        ],
        "cip": [
            ("cip_count", "cap_cip_count"),
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

