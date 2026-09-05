# helpers/overnight_score.py — overnight_score v2: the frozen cross-generation
# composite for the nightly optimizer (scripts/overnight_batch.py).
#
# Pure functions only — no file IO, no config reads. The IO shell (staging a
# generation, loading the frames, computing the capacity bound once per
# generation) lives in the batch script. The formula is FROZEN per version:
# every SCORE dict carries {"version": ...} so leaderboards from different
# nights stay comparable; a formula change must bump the version.
#
# v2 (fix Q / C30, changeover-2, 2026-09-03) — the changeover rule is now the
# SAME as the scorecard's (scorecard_engine.score_changeovers / kpi.ts):
#   * a transition whose gap fully contains a CIP block on that line is
#     WAIVED (retooling happens during the clean) — v1 charged it in full;
#   * a pair with NO standards row, or a row with no machine/recipe flag
#     set, is a recipe-only change costing CO_SCORE_RECIPE_ONLY_WEIGHT —
#     v1 priced an unknown pair at 0 (absence of standards read as free).
# Leaderboards scored under v1 are not comparable with v2 ones.
#
# composite = 0.40*fill + 0.30*changeovers + 0.15*campaign + 0.15*on_time
# (all subscores 0-100).
#
# Definitions (v2, exact — identical to v1 except the changeover rule noted
# above):
#   solver-placed block  production block whose order_id is a staged demand
#                        order id, excluding blocks whose attrs carry the
#                        exact token 'pinned' or any 'current_state:' token.
#                        Pinned/committed kg was already netted out of the
#                        staged demand at staging — crediting those blocks
#                        again would double-count.
#   order target         (qty_min+qty_max)/2 when qty_min>0 and
#                        qty_max>=qty_min, else max(qty_min, qty_max) — the
#                        weekly-fulfillment capping convention
#                        (scorecard_engine.weekly_breakdown / kpi.ts).
#                        qty_min/qty_max derive from qty_target*lower_pct/
#                        upper_pct when absent (load_demand convention).
#   fill                 sum(min(placed kg per order, target)) divided by
#                        min(net demand kg, capacity bound kg). Net demand =
#                        sum of targets of the STAGED post-netting demand
#                        (the generation work dir's demand_plan.csv, before
#                        any stock-check trim). Capacity bound: see
#                        capacity_bound_kg below — a per-generation constant.
#   changeovers          weighted CO load per 100 h of placed production,
#                        mapped to 0-100 by the fixed cap below. Transition
#                        pairs are consecutive production blocks per line in
#                        start order whose SKUs differ and whose INCOMING
#                        block is solver-placed (the committed->fill boundary
#                        swap is charged to the candidate, matching
#                        _apply_fill_window's changeover-base rule). Pairs
#                        classify via the changeovers.csv flags exactly like
#                        the co_pairs machinery; a pair with no standards row
#                        (or no flag set) is a recipe-only change and costs
#                        CO_SCORE_RECIPE_ONLY_WEIGHT. A pair whose gap fully
#                        contains a CIP block is waived (not a transition).
#   campaign             a campaign is a maximal run of consecutive
#                        solver-placed blocks with the same SKU on one line
#                        (time order; gaps ignored — a projected CIP splitting
#                        a run does not end the campaign). Score =
#                        clamp(avg placed hours per campaign / 40h) * 100.
#   on_time              per order: credit = min(kg placed in blocks whose
#                        midpoint falls in the order's demanded ISO week,
#                        target); score = 100 * sum(credits) / sum(targets).
#                        Weeks are TRUE ISO Monday marks in the generation
#                        anchor frame; the demanded week holds the order's
#                        due-window midpoint. Orders due beyond the horizon
#                        are excluded (weekly_breakdown convention).
#
# Edge rules (unchanged since v1): net demand <= 0 -> fill=100, on_time=100. No solver-placed
# production while demand > 0 -> all four subscores 0. Placed production with
# zero transitions -> changeovers = 100.

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

OVERNIGHT_SCORE_VERSION = "v2"

COMPOSITE_WEIGHTS = {
    "fill": 0.40,
    "changeovers": 0.30,
    "campaign": 0.15,
    "on_time": 0.15,
}

# SCORING weights for the changeover load — deliberately separate constants
# from the solver's objective weights (never read from flowstate.toml). The
# ordering mirrors the plant ranking: FFS and topload swaps hurt most.
CO_SCORE_WEIGHTS = {
    "ffs_change": 10.0,
    "topload_change": 8.0,
    "conv_to_org_change": 6.0,
    "casepacker_change": 4.0,
    "cinn_to_non": 3.0,
    "ttp_change": 1.0,
}
CO_SCORE_FLAVOR_WEIGHT = 0.5     # per added flavor, clamped at >= 0
# v2: a transition with NO flag set (or no standards row) is a recipe-only
# change — the cheapest kind, priced like the cheapest machine (TTP), the
# scorecard's co_weight_recipe_only convention. Never 0: absence of a
# standards row must not read as a free changeover.
CO_SCORE_RECIPE_ONLY_WEIGHT = 1.0
CO_LOAD_CAP_PER_100H = 80.0      # weighted CO points per 100 placed hours -> 0
CAMPAIGN_CAP_H = 40.0            # avg campaign run-hours scoring 100


def order_target(qty_min: float, qty_max: float) -> float:
    """The weekly-fulfillment capping convention for one order's target."""
    qty_min = float(qty_min or 0)
    qty_max = float(qty_max or 0)
    if qty_min > 0 and qty_max >= qty_min:
        return (qty_min + qty_max) / 2.0
    return max(qty_min, qty_max)


def demand_orders(demand: pd.DataFrame) -> list[dict[str, Any]]:
    """Staged demand rows -> [{order_id, sku, target, due_mid_h}].

    qty_min/qty_max derive from qty_target * lower_pct/upper_pct when the
    explicit columns are absent — the load_demand convention.
    """
    if demand is None or demand.empty:
        return []
    out: list[dict[str, Any]] = []
    for _, d in demand.iterrows():
        tgt = float(pd.to_numeric(d.get("qty_target"), errors="coerce") or 0)
        lo = float(pd.to_numeric(d.get("lower_pct"), errors="coerce") or 0.9)
        hi = float(pd.to_numeric(d.get("upper_pct"), errors="coerce") or 1.1)
        qmin = pd.to_numeric(d.get("qty_min"), errors="coerce")
        qmax = pd.to_numeric(d.get("qty_max"), errors="coerce")
        qmin = float(qmin) if pd.notna(qmin) else tgt * lo
        qmax = float(qmax) if pd.notna(qmax) else tgt * hi
        ds = float(pd.to_numeric(d.get("due_start_hour"), errors="coerce") or 0)
        de = float(pd.to_numeric(d.get("due_end_hour"), errors="coerce") or 0)
        out.append({
            "order_id": str(d.get("order_id", "")),
            "sku": str(d.get("sku", "")),
            "target": order_target(qmin, qmax),
            "due_mid_h": (ds + de) / 2.0,
        })
    return out


def iso_week_marks(anchor: datetime, horizon_h: float) -> list[tuple[float, int]]:
    """[(start_hour, iso_week)] — TRUE ISO Monday marks in the anchor frame.

    marks[0] is hour 0 (the anchor's own, possibly partial, week) — same
    convention as scorecard_engine._iso_week_bounds.
    """
    out = [(0.0, anchor.isocalendar()[1])]
    mon0 = anchor - timedelta(days=anchor.weekday())
    b = (mon0 + timedelta(days=7) - anchor).total_seconds() / 3600.0
    while b < horizon_h:
        wk_dt = anchor + timedelta(hours=b + 1)
        out.append((b, wk_dt.isocalendar()[1]))
        b += 168.0
    return out


def _week_index(marks: list[tuple[float, int]], hour: float) -> int:
    import bisect
    starts = [m for m, _ in marks]
    return max(0, bisect.bisect_right(starts, hour) - 1)


def _normalize(calendar: pd.DataFrame) -> pd.DataFrame:
    """Same dtype normalization as score_calendar (str skus, numeric hours)."""
    cal = calendar.copy()
    if "sku" in cal.columns:
        cal["sku"] = cal["sku"].astype(str).str.replace(r"\.0$", "", regex=True)
    if "order_id" in cal.columns:
        cal["order_id"] = cal["order_id"].astype(str)
    for c in ("start_h", "end_h", "qty_kg"):
        if c in cal.columns:
            cal[c] = pd.to_numeric(cal[c], errors="coerce")
    return cal


def _is_committed_attrs(attrs: Any) -> bool:
    tokens = str(attrs or "").split(";")
    return "pinned" in tokens or any(
        t.startswith("current_state:") for t in tokens)


def solver_fill_blocks(
    calendar: pd.DataFrame, demand_ids: set[str]
) -> pd.DataFrame:
    """The solver-placed production blocks (see module header)."""
    if calendar is None or calendar.empty:
        return calendar if calendar is not None else pd.DataFrame()
    cal = calendar
    mask = (
        (cal["block_type"].astype(str) == "production")
        & cal["order_id"].astype(str).isin(demand_ids)
    )
    if "attrs" in cal.columns:
        mask &= ~cal["attrs"].apply(_is_committed_attrs)
    return cal[mask]


def placed_kg_by_order(fill_blocks: pd.DataFrame) -> dict[str, float]:
    """order_id -> solver-placed kg (a block without qty_kg credits 0)."""
    out: dict[str, float] = {}
    for _, b in fill_blocks.iterrows():
        kg = b.get("qty_kg")
        kg = 0.0 if kg is None or pd.isna(kg) else float(kg)
        oid = str(b.get("order_id", ""))
        out[oid] = out.get(oid, 0.0) + kg
    return out


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def fill_subscore(
    placed: dict[str, float],
    orders: list[dict[str, Any]],
    capacity_bound: float | None,
) -> tuple[float, dict[str, Any]]:
    """Capped-credit fill vs min(net demand, capacity bound)."""
    net_demand = sum(o["target"] for o in orders)
    credit = sum(
        min(placed.get(o["order_id"], 0.0), o["target"]) for o in orders)
    denom = net_demand
    if capacity_bound is not None and capacity_bound > 0:
        denom = min(net_demand, float(capacity_bound))
    detail = {
        "credited_kg": round(credit, 1),
        "net_demand_kg": round(net_demand, 1),
        "capacity_bound_kg": (round(float(capacity_bound), 1)
                              if capacity_bound is not None else None),
        "denominator_kg": round(denom, 1),
    }
    if denom <= 0:
        return 100.0, detail
    return _clamp01(credit / denom) * 100.0, detail


def transition_cost(flags: dict | None) -> float:
    """v2 scoring cost of one SKU transition (frozen constants above).

    Same missing-pair rule as the scorecard: no standards row, or a row
    with no flag set, is a recipe-only change (CO_SCORE_RECIPE_ONLY_WEIGHT),
    never 0. Only call this for a pair the CIP waiver did not remove
    (weighted_co_load applies the waiver).
    """
    if not flags:
        return CO_SCORE_RECIPE_ONLY_WEIGHT
    cost = sum(
        w for key, w in CO_SCORE_WEIGHTS.items()
        if int(flags.get(key, 0) or 0) == 1)
    # added_flavors can be negative (removing flavors); clamp like the
    # model's pair_cost so a removal is never a reward here either.
    cost += CO_SCORE_FLAVOR_WEIGHT * max(
        0, int(flags.get("added_flavors", 0) or 0))
    if cost <= 0:
        return CO_SCORE_RECIPE_ONLY_WEIGHT
    return cost


def weighted_co_load(
    calendar: pd.DataFrame,
    fill_index: pd.Index,
    co_map: dict[tuple[str, str], dict],
) -> tuple[float, int]:
    """(weighted load, transition count) over pairs whose incoming block is
    solver-placed. Pairs walk consecutive production blocks per line in
    start order — the co_pairs counting convention. A pair whose gap fully
    contains a CIP block on the same line is WAIVED (v2; the scorecard /
    kpi.ts rule: the clean subsumes the changeover work) — it is neither a
    transition nor a cost."""
    prod = calendar[calendar["block_type"].astype(str) == "production"]
    load = 0.0
    transitions = 0
    key = "line_name" if "line_name" in prod.columns else "line_id"
    cips = calendar[calendar["block_type"].astype(str) == "cip"]
    cips_by_line: dict[str, list[tuple[float, float]]] = {}
    for _, c in cips.iterrows():
        try:
            cips_by_line.setdefault(str(c.get(key, "")).upper(), []).append(
                (float(c["start_h"]), float(c["end_h"])))
        except (TypeError, ValueError):
            continue

    def _cip_between(line: str, a_end: float, b_start: float) -> bool:
        return any(cs >= a_end - 1e-6 and ce <= b_start + 1e-6
                   for cs, ce in cips_by_line.get(line, ()))

    for line, grp in prod.sort_values([key, "start_h"]).groupby(
            prod[key].astype(str).str.upper()):
        idx = list(grp.index)
        rows = grp.to_dict("records")
        for i in range(1, len(rows)):
            frm, to = str(rows[i - 1].get("sku", "")), str(rows[i].get("sku", ""))
            if frm == to or idx[i] not in fill_index:
                continue
            if _cip_between(str(line), float(rows[i - 1]["end_h"]),
                            float(rows[i]["start_h"])):
                continue
            transitions += 1
            load += transition_cost(co_map.get((frm, to)))
    return load, transitions


def changeover_subscore(
    load: float, placed_h: float
) -> tuple[float, dict[str, Any]]:
    per_100h = (load / placed_h * 100.0) if placed_h > 0 else 0.0
    detail = {"weighted_co_load": round(load, 2),
              "co_load_per_100h": round(per_100h, 2)}
    if placed_h <= 0:
        return 0.0, detail
    return (1.0 - _clamp01(per_100h / CO_LOAD_CAP_PER_100H)) * 100.0, detail


def campaign_runs(fill_blocks: pd.DataFrame) -> list[float]:
    """Placed hours of each campaign (maximal same-SKU run per line)."""
    if fill_blocks is None or fill_blocks.empty:
        return []
    runs: list[float] = []
    key = "line_name" if "line_name" in fill_blocks.columns else "line_id"
    for _, grp in fill_blocks.sort_values([key, "start_h"]).groupby(
            fill_blocks[key].astype(str).str.upper()):
        cur_sku: str | None = None
        cur_h = 0.0
        for _, b in grp.sort_values("start_h").iterrows():
            sku = str(b.get("sku", ""))
            dur = max(0.0, float(b["end_h"]) - float(b["start_h"]))
            if sku != cur_sku:
                if cur_sku is not None:
                    runs.append(cur_h)
                cur_sku, cur_h = sku, dur
            else:
                cur_h += dur
        if cur_sku is not None:
            runs.append(cur_h)
    return runs


def campaign_subscore(runs: list[float]) -> tuple[float, dict[str, Any]]:
    if not runs:
        return 0.0, {"campaigns": 0, "avg_campaign_h": None}
    avg = sum(runs) / len(runs)
    return (_clamp01(avg / CAMPAIGN_CAP_H) * 100.0,
            {"campaigns": len(runs), "avg_campaign_h": round(avg, 2)})


def on_time_subscore(
    fill_blocks: pd.DataFrame,
    orders: list[dict[str, Any]],
    week_marks: list[tuple[float, int]],
    horizon_h: float,
) -> tuple[float, dict[str, Any]]:
    """Capped credit for kg landing in the order's demanded ISO week."""
    in_scope = [o for o in orders if o["due_mid_h"] < horizon_h]
    total_target = sum(o["target"] for o in in_scope)
    detail: dict[str, Any] = {"orders_in_scope": len(in_scope)}
    if total_target <= 0:
        detail["on_time_kg"] = 0.0
        return 100.0, detail
    placed_wk: dict[tuple[str, int], float] = {}
    for _, b in fill_blocks.iterrows():
        kg = b.get("qty_kg")
        kg = 0.0 if kg is None or pd.isna(kg) else float(kg)
        if kg <= 0:
            continue
        mid = (float(b["start_h"]) + float(b["end_h"])) / 2.0
        key = (str(b.get("order_id", "")), _week_index(week_marks, mid))
        placed_wk[key] = placed_wk.get(key, 0.0) + kg
    credit = 0.0
    for o in in_scope:
        wk = _week_index(week_marks, o["due_mid_h"])
        credit += min(placed_wk.get((o["order_id"], wk), 0.0), o["target"])
    detail["on_time_kg"] = round(credit, 1)
    return _clamp01(credit / total_target) * 100.0, detail


def capacity_bound_kg(
    gates: dict[str, float],
    blocked: dict[str, list[tuple[float, float]]],
    line_rates: dict[str, float],
    capable_skus: dict[str, set[str]],
    demand_skus: set[str],
    horizon_h: float,
) -> tuple[float, dict[str, Any]]:
    """kg the open fill windows could physically hold (per-generation constant).

    Per line: flat rate (line_rates.csv — the live use_sku_rates=false
    convention) x total free hours from the staged gate to the horizon,
    minus every staged blocked window. In the Scenario F staging those
    windows already contain the committed MOs, trials, PROJECTED CIPs at
    each line's max interval and the real line-downs — so CIP deductions
    are inherent, not re-derived. A line counts only when its flat rate is
    positive and it is capable of at least one staged demand SKU. Segments
    under 1 h are unusable (free_segments convention, shared with the
    greedy filler).
    """
    from helpers.greedy_fill import free_segments

    total = 0.0
    per_line: dict[str, dict[str, float]] = {}
    for line, rate in line_rates.items():
        ln = str(line).upper()
        if rate <= 0:
            continue
        if not (capable_skus.get(ln, set()) & demand_skus):
            continue
        segs = free_segments(
            float(gates.get(ln, 0.0)), blocked.get(ln, []), float(horizon_h))
        free_h = sum(e - s for s, e in segs)
        kg = free_h * float(rate)
        per_line[ln] = {"free_h": round(free_h, 1), "kg": round(kg, 1)}
        total += kg
    return total, {"per_line": per_line}


def overnight_score(
    calendar: pd.DataFrame,
    demand: pd.DataFrame,
    co_map: dict[tuple[str, str], dict],
    *,
    capacity_bound: float | None,
    week_marks: list[tuple[float, int]],
    horizon_h: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Score one candidate calendar against a generation's staged frame.

    Returns (SCORE, details). SCORE is the leaderboard dict:
    {"version", "composite", "fill", "changeovers", "campaign", "on_time"}.
    """
    orders = demand_orders(demand)
    demand_ids = {o["order_id"] for o in orders}
    cal = _normalize(calendar if calendar is not None else pd.DataFrame())
    fill_blocks = solver_fill_blocks(cal, demand_ids)
    placed = placed_kg_by_order(fill_blocks)
    placed_h = float(
        (fill_blocks["end_h"] - fill_blocks["start_h"]).clip(lower=0).sum()
    ) if len(fill_blocks) else 0.0
    net_demand = sum(o["target"] for o in orders)

    if net_demand <= 0:
        fill, fill_det = 100.0, {"credited_kg": 0.0, "net_demand_kg": 0.0,
                                 "capacity_bound_kg": capacity_bound,
                                 "denominator_kg": 0.0}
        on_time, ot_det = 100.0, {"orders_in_scope": 0, "on_time_kg": 0.0}
        load, transitions = weighted_co_load(cal, fill_blocks.index, co_map)
        co, co_det = changeover_subscore(load, placed_h)
        camp, camp_det = campaign_subscore(campaign_runs(fill_blocks))
    elif placed_h <= 0:
        # Demand existed and nothing was placed: an empty fill must not
        # collect 100s on the ratio subscores.
        fill, fill_det = fill_subscore(placed, orders, capacity_bound)
        co, co_det = 0.0, {"weighted_co_load": 0.0, "co_load_per_100h": 0.0}
        camp, camp_det = 0.0, {"campaigns": 0, "avg_campaign_h": None}
        on_time, ot_det = 0.0, {"orders_in_scope": len(orders),
                                "on_time_kg": 0.0}
        transitions = 0
    else:
        fill, fill_det = fill_subscore(placed, orders, capacity_bound)
        load, transitions = weighted_co_load(cal, fill_blocks.index, co_map)
        co, co_det = changeover_subscore(load, placed_h)
        camp, camp_det = campaign_subscore(campaign_runs(fill_blocks))
        on_time, ot_det = on_time_subscore(
            fill_blocks, orders, week_marks, horizon_h)

    composite = (COMPOSITE_WEIGHTS["fill"] * fill
                 + COMPOSITE_WEIGHTS["changeovers"] * co
                 + COMPOSITE_WEIGHTS["campaign"] * camp
                 + COMPOSITE_WEIGHTS["on_time"] * on_time)
    score = {
        "version": OVERNIGHT_SCORE_VERSION,
        "composite": round(composite, 2),
        "fill": round(fill, 2),
        "changeovers": round(co, 2),
        "campaign": round(camp, 2),
        "on_time": round(on_time, 2),
    }
    details = {
        "placed_h": round(placed_h, 1),
        "fill_blocks": int(len(fill_blocks)),
        "sku_transitions": transitions,
        **fill_det, **co_det, **camp_det, **ot_det,
    }
    return score, details
