# helpers/agent_policy.py — the planning agent's input policies (pure).
#
# Dry-run 1 (2026-08-14) showed the CP-SAT solver is component-blind: it
# poured 1,093 t across 49 blocks into SKUs the stock check marks
# DO_NOT_SCHEDULE. These policies are the agent's approved macro adjustments
# (user sign-off 2026-08-14): they rewrite the solver's WORK-DIR demand copy
# — never data/reference — and every change is returned as a note so the
# proposal's reasoning shows exactly what the agent excluded and why.
#
# Slice 3 (2026-09-15, user sign-off "build slice 3"): the trim now reads the
# stock report's per-order PROJECTION (stockcheck.projection — the same
# netted curves the Gantt grades, receipts included) instead of the flat
# worst-week ratio, and every order that depends on a receipt gets an
# EARLIEST START (ready + buffer) the solver honours as a hard floor
# (demand_plan.csv column `earliest_start_hour`). The flat trim stays as the
# fallback for a report saved before the projection existed.

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

EARLIEST_COL = "earliest_start_hour"


def dns_ratios(stock_report: dict) -> dict[str, float]:
    """SKU -> worst achievable ratio, for demand rows stock marks
    DO_NOT_SCHEDULE. A missing/None/inf ratio reads as 0.0 (unknown supply
    is treated as none — the conservative direction for a trim policy)."""
    out: dict[str, float] = {}
    for d in stock_report.get("demand_view", []):
        if d.get("status") != "DO_NOT_SCHEDULE":
            continue
        sku = str(d.get("sku", ""))
        r = d.get("achievable_ratio")
        r = 0.0 if r is None or (isinstance(r, float) and math.isinf(r)) else max(0.0, float(r))
        out[sku] = min(out.get(sku, r), r)
    return out


def trim_dns_demand(
    demand: pd.DataFrame, dns: dict[str, float]
) -> tuple[pd.DataFrame, list[str]]:
    """Trim component-blocked demand in the solver's demand copy (pure).

    For every order whose SKU is in `dns`:
      * qty_min drops to 0 (lower_pct = 0) — the solver must never be FORCED
        to schedule a run the components cannot support;
      * qty_max is capped at the achievable ratio (upper_pct =
        min(upper_pct, achievable)) — it MAY still fill spare capacity up to
        what stock can actually build, but no further.

    Returns (new_frame, notes). Untouched orders are returned as-is.

    Netted rows (fix C58 / netting-6): after Scenario F staging a row that
    received committed credit carries explicit qty_min/qty_max, blank pct
    columns and a `credit_kg` column (demand_coverage.apply_ledger,
    explicit_bounds). The stock check computes `achievable` against the
    GROSS demand row, so the cap belongs on the gross too:
    ``qty_max = min(qty_max, max(0, achievable x gross - credit))`` with
    gross = qty_target + credit_kg. Applying the ratio to the NET target
    let 120437-W0 keep a 5,196 kg cap although components supported only
    ~1,127 kg more after the committed MO.
    """
    if demand is None or demand.empty or not dns:
        return demand, []
    df = demand.copy()
    notes: list[str] = []
    has_explicit = "qty_max" in df.columns
    if has_explicit:
        for col in ("qty_min", "qty_max"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    for idx, r in df.iterrows():
        sku = str(r.get("sku", ""))
        if sku not in dns:
            continue
        achievable = dns[sku]
        target = float(r.get("qty_target", 0) or 0)
        lo_raw, hi_raw = r.get("lower_pct"), r.get("upper_pct")
        pct_mode = pd.notna(lo_raw) and pd.notna(hi_raw) if has_explicit \
            else True
        if pct_mode:
            old_lo = float(lo_raw if pd.notna(lo_raw) else 0.9)
            old_hi = float(hi_raw if pd.notna(hi_raw) else 1.1)
            new_hi = round(min(old_hi, achievable), 4)
            df.at[idx, "lower_pct"] = 0.0
            df.at[idx, "upper_pct"] = new_hi
            notes.append(
                f"{r.get('order_id')}: components support {achievable:.0%} — "
                f"qty_min {target * old_lo:,.0f}→0 kg, "
                f"qty_max {target * old_hi:,.0f}→{target * new_hi:,.0f} kg")
            continue
        # explicit (netted) bounds: cap on the GROSS, minus the credit
        credit = float(pd.to_numeric(pd.Series([r.get("credit_kg")]),
                                     errors="coerce").fillna(0.0).iloc[0])
        gross = target + credit
        old_min = float(r.get("qty_min")) if pd.notna(r.get("qty_min")) else target
        old_max = float(r.get("qty_max")) if pd.notna(r.get("qty_max")) else target
        new_max = round(min(old_max, max(0.0, achievable * gross - credit)), 1)
        df.at[idx, "qty_min"] = 0.0
        df.at[idx, "qty_max"] = new_max
        notes.append(
            f"{r.get('order_id')}: components support {achievable:.0%} of "
            f"{gross:,.0f} kg gross ({credit:,.0f} kg already committed) — "
            f"qty_min {old_min:,.0f}→0 kg, qty_max {old_max:,.0f}→{new_max:,.0f} kg")
    return df, notes


# ---------------------------------------------------------------------------
# Slice 3 — projected (time-phased, PO-aware) per-order policy
# ---------------------------------------------------------------------------

def projected_caps(stock_report: dict) -> dict[str, dict]:
    """order_id -> projection record for every demand row that carries one
    (stockcheck.projection). Empty when the report predates slice 3, which
    is how callers pick the flat fallback. Records are keyed by order_id;
    a `(sku, week_index)` alias rides along under `_by_week` for rows the
    staging renamed."""
    out: dict[str, dict] = {}
    by_week: dict[tuple[str, int], str] = {}
    for d in (stock_report or {}).get("demand_view", []) or []:
        p = d.get("projected")
        if not isinstance(p, dict):
            continue
        oid = str(d.get("order_id", ""))
        rec = dict(p)
        rec["sku"] = str(d.get("sku", ""))
        rec["week_index"] = d.get("week_index")
        out[oid] = rec
        try:
            by_week[(rec["sku"], int(d.get("week_index")))] = oid
        except (TypeError, ValueError):
            pass
    if out:
        out["_by_week"] = by_week  # type: ignore[assignment]
    return out


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def trim_projected_demand(
    demand: pd.DataFrame, caps: dict[str, dict], *, shift_h: float = 0.0,
    earliest_start: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    """Per-order trim from the projection (pure).

    For every demand row with a projection record:
      * cap: when the record carries `cap_kg` (statuses LIFTED / DNS /
        COVERED) and it is below the row's qty_max, qty_max drops to it and
        qty_min to 0 (never FORCE a run the components cannot support).
        Netted rows (explicit bounds, `credit_kg`) are capped on their NET
        target directly — the projection already netted the board's own
        draws; pct rows are capped on the gross: the board's kg in the week
        plus the cap, as a share of the target.
      * floor: when the record carries `earliest_start_h` (the order needs
        a receipt: ready + buffer) the column `earliest_start_hour` is
        written in the WORK frame (`shift_h` = report anchor - work anchor,
        in hours); `earliest_start=False` skips the floors (config knob
        [stock] solver_earliest_start).
    Rows without a record are untouched. Returns (new_frame, notes).
    """
    if demand is None or demand.empty or not caps:
        return demand, []
    df = demand.copy()
    notes: list[str] = []
    by_week = caps.get("_by_week") or {}
    has_explicit = "qty_max" in df.columns
    for col in ("qty_min", "qty_max"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    if EARLIEST_COL not in df.columns:
        df[EARLIEST_COL] = float("nan")
    df[EARLIEST_COL] = pd.to_numeric(df[EARLIEST_COL], errors="coerce").astype(float)
    n_floor = 0
    for idx, r in df.iterrows():
        oid = str(r.get("order_id", ""))
        rec = caps.get(oid)
        if rec is None:
            try:
                alias = by_week.get((str(r.get("sku", "")), int(r.get("week_index"))))
            except (TypeError, ValueError):
                alias = None
            rec = caps.get(alias) if alias else None
        if not isinstance(rec, dict):
            continue
        target = float(r.get("qty_target", 0) or 0)
        cap = _num(rec.get("cap_kg"))
        status = str(rec.get("status", ""))
        lo_raw, hi_raw = r.get("lower_pct"), r.get("upper_pct")
        pct_mode = (pd.notna(lo_raw) and pd.notna(hi_raw)) if has_explicit else True
        touched = []
        if cap is not None and status in ("LIFTED", "DNS", "COVERED"):
            if pct_mode:
                old_lo = float(lo_raw if pd.notna(lo_raw) else 0.9)
                old_hi = float(hi_raw if pd.notna(hi_raw) else 1.1)
                board_kg = _num(rec.get("board_kg")) or 0.0
                allowed = (board_kg + cap) / target if target > 0 else 0.0
                if allowed < old_hi:
                    new_hi = round(max(0.0, allowed), 4)
                    df.at[idx, "lower_pct"] = 0.0
                    df.at[idx, "upper_pct"] = new_hi
                    touched.append(
                        f"qty_min {target * old_lo:,.0f}→0 kg, "
                        f"qty_max {target * old_hi:,.0f}→{target * new_hi:,.0f} kg")
            else:
                old_min = float(r.get("qty_min")) if pd.notna(r.get("qty_min")) else target
                old_max = float(r.get("qty_max")) if pd.notna(r.get("qty_max")) else target
                if cap < old_max:
                    new_max = round(max(0.0, cap), 1)
                    df.at[idx, "qty_min"] = 0.0
                    df.at[idx, "qty_max"] = new_max
                    touched.append(
                        f"qty_min {old_min:,.0f}→0 kg, "
                        f"qty_max {old_max:,.0f}→{new_max:,.0f} kg")
        floor = _num(rec.get("earliest_start_h"))
        if floor is not None and earliest_start:
            floor_w = max(0.0, floor + float(shift_h or 0.0))
            df.at[idx, EARLIEST_COL] = round(floor_w, 1)
            n_floor += 1
            de = _num(r.get("due_end_hour"))
            tail = ""
            if de is not None and floor_w >= de + 1.0:
                tail = " (after the due window — nothing can be placed this run)"
            touched.append(f"earliest start h{floor_w:,.0f}{tail}")
        if touched:
            why = str(rec.get("text") or status)
            notes.append(f"{oid}: {why} — " + ", ".join(touched))
    if n_floor == 0 and df[EARLIEST_COL].isna().all():
        df = df.drop(columns=[EARLIEST_COL])
    return df, notes


@dataclass
class PolicyResult:
    mode: str                                   # "projected" | "flat" | "off"
    notes: list[str] = field(default_factory=list)
    capped_orders: dict[str, float] = field(default_factory=dict)  # order_id -> cap kg
    capped_skus: set[str] = field(default_factory=set)
    n_floors: int = 0
    shift_h: float = 0.0

    def summary(self) -> str:
        if self.mode == "off":
            return "stock policy off"
        if self.mode == "flat":
            return (f"DNS trim (flat fallback): {len(self.notes)} order(s) "
                    f"adjusted ({len(self.capped_skus)} component-blocked SKU(s))")
        return (f"stock policy (projected): {len(self.capped_orders)} order(s) "
                f"capped, {self.n_floors} earliest-start floor(s) "
                f"({len(self.capped_skus)} SKU(s))")


def policy_signature(stock_report: dict) -> dict[str, float]:
    """What the policy would cap, as {key: cap} — order_id -> cap_kg under
    the projection, SKU -> ratio under the flat fallback. The overnight
    batch diffs consecutive signatures to report "components ran short /
    freed up" between rounds."""
    caps = projected_caps(stock_report)
    if caps:
        return {oid: float(rec.get("cap_kg"))
                for oid, rec in caps.items()
                if oid != "_by_week" and isinstance(rec, dict)
                and rec.get("cap_kg") is not None
                and rec.get("status") in ("LIFTED", "DNS")}
    return dns_ratios(stock_report)


def _work_anchor_shift_h(work: Path, stock_report: dict) -> float:
    """Hours to add to a report-frame hour to express it in the work-dir
    frame: report anchor - work anchor. 0 when either anchor is unknown."""
    from helpers.timefmt import parse_anchor, planning_anchor
    rep_anchor = stock_report.get("anchor")
    if not rep_anchor:
        return 0.0
    tp = Path(work) / "flowstate.toml"
    try:
        import tomllib
        with open(tp, "rb") as fh:
            cfg = tomllib.load(fh)
        work_anchor = planning_anchor(cfg)
    except Exception:  # noqa: BLE001 — no work toml: same frame
        return 0.0
    return (parse_anchor(rep_anchor) - work_anchor).total_seconds() / 3600.0


def apply_stock_policy(work: Path, stock_report: dict, *,
                       cfg: dict | None = None) -> PolicyResult:
    """The ONE stock-policy patch for every solver caller (Generate page,
    agent_propose, overnight batch): rewrite the work dir's demand_plan.csv
    from the stock report and return what changed.

    [stock] solver_policy: "projected" (default) uses the per-order
    projection when the report carries one and falls back to the flat DNS
    trim otherwise; "flat" forces the fallback; "off" changes nothing.
    [stock] solver_earliest_start (default true) writes the receipt floors.
    """
    from helpers.config import stock_config
    sc = stock_config(cfg) if cfg is not None else stock_config()
    mode = str(sc.get("solver_policy", "projected") or "projected").lower()
    want_floors = bool(sc.get("solver_earliest_start", True))
    res = PolicyResult(mode="off")
    if mode == "off":
        res.notes.append("stock policy off ([stock] solver_policy = \"off\")")
        return res
    dem_path = Path(work) / "demand_plan.csv"
    if not dem_path.exists():
        res.notes.append("stock policy: no work demand_plan.csv — nothing to trim")
        return res
    if not stock_report or stock_report.get("error"):
        res.notes.append("stock policy: stock report unavailable"
                         + (f" ({stock_report.get('error')})" if stock_report else "")
                         + " — solving without it")
        return res
    dem = pd.read_csv(dem_path, dtype={"sku": str})
    caps = projected_caps(stock_report) if mode == "projected" else {}
    if caps:
        res.mode = "projected"
        res.shift_h = _work_anchor_shift_h(Path(work), stock_report)
        trimmed, notes = trim_projected_demand(
            dem, caps, shift_h=res.shift_h, earliest_start=want_floors)
        for oid, rec in caps.items():
            if oid == "_by_week" or not isinstance(rec, dict):
                continue
            if rec.get("cap_kg") is not None and rec.get("status") in ("LIFTED", "DNS"):
                res.capped_orders[oid] = float(rec["cap_kg"])
                res.capped_skus.add(str(rec.get("sku", "")))
        if EARLIEST_COL in trimmed.columns:
            res.n_floors = int(trimmed[EARLIEST_COL].notna().sum())
        head = [res.summary()]
        if res.shift_h:
            head.append(f"floors shifted {res.shift_h:+.0f}h (report anchor → staging anchor)")
        if not want_floors:
            head.append("earliest-start floors off ([stock] solver_earliest_start = false)")
        res.notes = head + notes
    else:
        res.mode = "flat"
        dns = dns_ratios(stock_report)
        trimmed, notes = trim_dns_demand(dem, dns)
        res.capped_skus = set(dns)
        res.notes = [res.summary()
                     + ("" if mode == "flat" else
                        " — report has no per-order projection (refresh Stock Check)")] + notes
    trimmed.to_csv(dem_path, index=False)
    return res
