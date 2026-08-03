# helpers/scorecard_engine.py — Operational truth model (Phase 0).
#
# Same functions score AZAP baseline, DnD what-if calendars, and solver scenarios.
# Draft v0 formulas — thresholds live in flowstate.toml [scorecard].

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from helpers.config import scorecard_config
from helpers.paths import reference_dir, scorecards_dir
from helpers.safe_io import safe_write_json


FORMULA_HELP = {
    "recipe_changes": (
        "Count of adjacent production transitions on the same line where recipe/SKU "
        "family differs (changeover flags: flavor/organic/cinnamon; else SKU change "
        "that is not format-only)."
    ),
    "format_changes": (
        "Count of adjacent production transitions with packaging format change "
        "(topload / TTP / FFS / casepacker flags from changeover standards)."
    ),
    "total_co_hours": (
        "Sum of estimated changeover hours per transition from standards table, "
        "else configurable defaults by CO type. Idle gaps alone are not counted."
    ),
    "cip_count": "Number of CIP blocks in the horizon.",
    "cip_hours": "Sum of CIP block durations (end_h − start_h).",
    "cip_forfeited_h": (
        "For each CIP: max(0, line_cip_interval_h − run_hours_since_last_cip). "
        "Cleaning earlier than the interval forfeits unused dirty-time budget."
    ),
    "trial_hours": "Sum of trial block durations.",
    "trial_disruptions": (
        "Production blocks immediately before/after a trial on the same line that "
        "require a changeover (SKU differs) or are split by the trial."
    ),
    "maint_aligned": (
        "Maintenance blocks whose window overlaps a CIP on the same line "
        "(or within ±align_tolerance_h). Higher is better."
    ),
    "maint_conflicts": (
        "Maintenance blocks that overlap production, trial, or contractor on the "
        "same line. Higher is worse."
    ),
    "avg_run_h": "Mean duration (hours) of production blocks.",
    "short_run_count": "Production blocks with duration < short_run_h (default 4h).",
    "orders_at_risk": (
        "Orders whose scheduled completion is within at_risk_h of due end but still "
        "on time. n/a if demand data missing."
    ),
    "orders_late": (
        "Orders with scheduled completion after due end, or unmet qty below min. "
        "n/a if demand data missing."
    ),
    "excess_inventory_kg": (
        "Sum of max(0, produced_qty − qty_max) across orders. n/a if no max bound."
    ),
}


@dataclass
class ScorecardResult:
    week_label: str
    scored_at: str
    changeovers: dict[str, Any] = field(default_factory=dict)
    cip: dict[str, Any] = field(default_factory=dict)
    trials: dict[str, Any] = field(default_factory=dict)
    maintenance: dict[str, Any] = field(default_factory=dict)
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
            maintenance=dict(data.get("maintenance") or {}),
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
    return {
        "recipe_changes": recipe,
        "format_changes": fmt,
        "total_co_hours": round(hours, 2),
        "sku_transitions": transitions,
    }


def score_cip(calendar: pd.DataFrame, cfg: dict, intervals: dict[str, float]) -> dict[str, Any]:
    cips = _by_type(calendar, "cip").sort_values(["line_id", "start_h"])
    prod = _production(calendar)
    count = len(cips)
    hours = float((cips["end_h"] - cips["start_h"]).clip(lower=0).sum()) if count else 0.0
    forfeited = 0.0

    for line_id, cip_grp in cips.groupby("line_id"):
        line_name = str(cip_grp["line_name"].iloc[0]) if len(cip_grp) else str(line_id)
        interval = float(
            intervals.get(line_name)
            or intervals.get(str(line_id))
            or cfg["cip_interval_fallback_h"]
        )
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
            forfeited += max(0.0, interval - run_since)
            last_cip_end = float(cip["end_h"])

    return {
        "cip_count": int(count),
        "cip_hours": round(hours, 2),
        "cip_forfeited_h": round(forfeited, 2),
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
    return {
        "trial_hours": round(hours, 2),
        "trial_disruptions": int(disruptions),
    }


def _windows_near(a_start: float, a_end: float, b_start: float, b_end: float, tol: float) -> bool:
    # expand A by tol and test overlap with B
    return (a_start - tol) < b_end and (a_end + tol) > b_start


def score_maintenance(calendar: pd.DataFrame, cfg: dict) -> dict[str, Any]:
    maint = _by_type(calendar, "maintenance")
    cips = _by_type(calendar, "cip")
    blockers = calendar[calendar["block_type"].isin(["production", "trial", "contractor"])]
    tol = float(cfg["align_tolerance_h"])
    aligned = 0
    aligned_h = 0.0
    conflicts = 0
    for _, m in maint.iterrows():
        ms, me = float(m["start_h"]), float(m["end_h"])
        line = m["line_id"]
        line_cips = cips[cips["line_id"] == line]
        is_aligned = False
        for _, c in line_cips.iterrows():
            if _windows_near(ms, me, float(c["start_h"]), float(c["end_h"]), tol):
                is_aligned = True
                break
        if is_aligned:
            aligned += 1
            aligned_h += max(0.0, me - ms)
        line_blockers = blockers[blockers["line_id"] == line]
        for _, b in line_blockers.iterrows():
            if ms < float(b["end_h"]) and me > float(b["start_h"]):
                conflicts += 1
                break
    return {
        "maint_aligned": int(aligned),
        "maint_aligned_hours": round(aligned_h, 2),
        "maint_conflicts": int(conflicts),
        "maint_count": int(len(maint)),
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


def score_service(calendar: pd.DataFrame, cfg: dict, demand: pd.DataFrame | None) -> dict[str, Any]:
    if demand is None or demand.empty:
        return {
            "orders_at_risk": None,
            "orders_late": None,
            "excess_inventory_kg": None,
            "available": False,
        }
    prod = _production(calendar)
    at_risk = 0
    late = 0
    excess = 0.0
    at_risk_h = float(cfg["at_risk_h"])

    # Aggregate scheduled end and qty by order_id
    if prod.empty:
        scheduled_end: dict[str, float] = {}
        produced: dict[str, float] = {}
    else:
        scheduled_end = prod.groupby("order_id")["end_h"].max().to_dict()
        if "qty_kg" in prod.columns and prod["qty_kg"].notna().any():
            produced = prod.groupby("order_id")["qty_kg"].sum().fillna(0).to_dict()
        else:
            produced = {}

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
    m = raw["maintenance"]
    camp = raw["campaigns"]
    svc = raw["service"]

    co_s = [
        _score_lower_better(co["recipe_changes"], cfg["cap_recipe_changes"]),
        _score_lower_better(co["format_changes"], cfg["cap_format_changes"]),
        _score_lower_better(co["total_co_hours"], cfg["cap_co_hours"]),
    ]
    cip_s = [
        _score_lower_better(cip["cip_count"], cfg["cap_cip_count"]),
        _score_lower_better(cip["cip_hours"], cfg["cap_cip_hours"]),
        _score_lower_better(cip["cip_forfeited_h"], cfg["cap_cip_forfeited"]),
    ]
    trial_s = [
        _score_lower_better(tr["trial_hours"], cfg["cap_trial_hours"]),
        _score_lower_better(tr["trial_disruptions"], cfg["cap_trial_disruptions"]),
    ]
    # maintenance: aligned high good, conflicts low good
    maint_parts = [
        _score_lower_better(m["maint_conflicts"], cfg["cap_maint_conflicts"]),
    ]
    if m.get("maint_count", 0) > 0:
        maint_parts.append(_score_higher_better(m["maint_aligned"], max(1, m["maint_count"])))
    else:
        maint_parts.append(100.0)  # no maint scheduled → neutral-good

    camp_parts = [
        _score_lower_better(camp["short_run_count"], cfg["cap_short_runs"]),
    ]
    if camp.get("avg_run_h") is not None:
        # closer to target is better (symmetric)
        avg = float(camp["avg_run_h"])
        target = float(cfg["target_avg_run_h"])
        camp_parts.append(_clamp01(100.0 * (1.0 - abs(avg - target) / max(target, 1))))

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
        "trials": avg(trial_s),
        "maintenance": avg(maint_parts),
        "campaigns": avg(camp_parts),
        "service": None if service_score is None else round(service_score, 1),
    }


def composite_score(cats: dict[str, float | None], cfg: dict) -> float | None:
    weights = {
        "service": float(cfg["weight_service"]),
        "changeovers": float(cfg["weight_changeovers"]),
        "cip": float(cfg["weight_cip"]),
        "campaigns": float(cfg["weight_campaigns"]),
        "maintenance": float(cfg["weight_maintenance"]),
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
    demand = load_demand(ref)

    notes: list[str] = []
    if calendar.empty:
        notes.append("Calendar is empty — all metrics are zero / n/a.")
    if not co_map:
        notes.append("No changeover standards loaded — using default CO hour estimates.")
    if demand is None:
        notes.append("No demand_plan.csv — Service metrics are n/a.")

    raw = {
        "changeovers": score_changeovers(calendar, cfg, co_map),
        "cip": score_cip(calendar, cfg, intervals),
        "trials": score_trials(calendar, co_map),
        "maintenance": score_maintenance(calendar, cfg),
        "campaigns": score_campaigns(calendar, cfg),
        "service": score_service(calendar, cfg, demand),
    }
    cats = category_scores(raw, cfg)
    comp = composite_score(cats, cfg)

    return ScorecardResult(
        week_label=week_label,
        scored_at=datetime.now().isoformat(timespec="seconds"),
        changeovers=raw["changeovers"],
        cip=raw["cip"],
        trials=raw["trials"],
        maintenance=raw["maintenance"],
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
        ("changeovers", "recipe_changes", "recipe changeovers"),
        ("changeovers", "format_changes", "format changeovers"),
        ("changeovers", "total_co_hours", "changeover hours"),
        ("cip", "cip_count", "CIPs"),
        ("cip", "cip_hours", "CIP hours"),
        ("cip", "cip_forfeited_h", "forfeited CIP hours"),
        ("trials", "trial_hours", "trial hours"),
        ("trials", "trial_disruptions", "trial disruptions"),
        ("maintenance", "maint_aligned", "CIP-aligned maintenance"),
        ("maintenance", "maint_conflicts", "maintenance conflicts"),
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
        # For aligned maintenance, higher is better
        better_higher = key in ("maint_aligned", "avg_run_h")
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
        "maintenance": data.get("maintenance") or {},
        "campaigns": data.get("campaigns") or {},
        "service": data.get("service") or {},
    }
    weights = {
        "service": float(cfg["weight_service"]),
        "changeovers": float(cfg["weight_changeovers"]),
        "cip": float(cfg["weight_cip"]),
        "campaigns": float(cfg["weight_campaigns"]),
        "maintenance": float(cfg["weight_maintenance"]),
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
            ("cip_forfeited_h", "cap_cip_forfeited"),
        ],
        "trials": [
            ("trial_hours", "cap_trial_hours"),
            ("trial_disruptions", "cap_trial_disruptions"),
        ],
        "maintenance": [("maint_conflicts", "cap_maint_conflicts")],
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

