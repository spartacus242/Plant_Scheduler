# helpers/demand_coverage.py — SKU×week ledger: demand plan vs committed MOs.
#
# THE convention (locked 2026-08-16, validated on live data — 280480 has
# 731 t of committed MOs landing in W34 vs 397+77+264 = 738 t of cumulative
# W34–W36 demand: the plant pre-builds future weeks and the demand plan
# stays GROSS):
#
#   * the demand plan is GROSS weekly requirements — the planner never nets
#     out MOs by hand; Flowstate does ALL subtraction;
#   * committed kg is PRO-RATED across the ISO weeks the block spans, by
#     time-overlap share (a block running 60h in W34 and 40h in W35 credits
#     60% / 40%). The previous midpoint rule flipped a block's ENTIRE kg
#     across the Monday boundary whenever a re-forecast stretched it — live
#     2026-08-21 the W35 committed credit read 1.91M kg against ~1.1M kg of
#     total weekly plant capacity;
#   * per SKU, weeks are consumed oldest-first and surplus carries FORWARD
#     only (this week's extra production is next week's inventory, never
#     last week's); deficits never carry;
#   * kg already MADE (completed MOs — dropped from the calendar by
#     current_state) still count: they enter the week they were produced,
#     net that week's demand first, and only true surplus rolls forward.
#
# One engine, three consumers:
#   * Scenario F staging (plan_fill.subtract_committed / the overlay) —
#     reduces fill targets so committed work is never re-planned;
#   * Reconcile — shows whether a demand week is already covered BEFORE
#     planning (the "is 280480-W34 actually done?" question);
#   * Plant Calendar holding — committed kg credits the holding cards.
#
# Weeks are keyed by TRUE ISO calendar week (year*100+week) whenever an
# anchor datetime is available. The previous week_index keying silently
# no-opped the whole netting when the demand file anchored at an older ISO
# week than the staging frame (live: W32-anchored file + W33 today-anchor
# → every key off by two, found 2026-08-16). week_index is only a fallback
# for demand rows that carry no due window at all.
#
# Pure module — no Streamlit; the only IO lives in build_ledger_from_data.

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

_EPS = 0.5          # kg — below this, a residual is rounding noise
_NON_SKUS = ("", "CIP", "TRIALS")

# A SKU is OVERCOMMITTED when committed+produced supply exceeds its total
# demand (through the last demand week on file) by more than BOTH bounds —
# the murky case: either the demand plan was already netted by hand, or the
# MOs pre-build weeks beyond the demand file. Flag, never guess.
OVERCOMMIT_TOL_PCT = 0.10
OVERCOMMIT_TOL_KG = 1000.0

COVERED = "COVERED"
PARTIAL = "PARTIAL"
UNCOVERED = "UNCOVERED"


@dataclass(frozen=True)
class CoverageRow:
    order_id: str
    sku: str
    week_key: int        # ISO year*100+week, or a bucket index (no anchor)
    week_label: str      # "WW34" (ISO) / "W+1" (bucket)
    gross_kg: float      # the demand plan's requirement (never mutated)
    committed_kg: float  # planned MO kg pro-rated into this week by overlap
    produced_kg: float   # completed-MO kg made in this week (actuals)
    carry_in_kg: float   # surplus arriving from earlier weeks
    applied_kg: float    # kg credited against this order
    net_kg: float        # gross − applied: what still needs planning
    status: str          # COVERED | PARTIAL | UNCOVERED


@dataclass
class CoverageLedger:
    rows: list[CoverageRow] = field(default_factory=list)
    # sku -> {surplus_kg, total_gross_kg, total_supply_kg, last_week_label}
    overcommitted: dict[str, dict] = field(default_factory=dict)
    no_demand: dict[str, float] = field(default_factory=dict)
    unknown_kg_blocks: int = 0
    produced_total_kg: float = 0.0
    anchor_week_key: int | None = None
    notes: list[str] = field(default_factory=list)
    # (sku, week_key) -> kg made in a PAST week with neither a live nor a
    # history demand row to settle against; excluded from the carry unless
    # build_ledger(carry_unsettled_past=True). See C54 / netting-2.
    unsettled_past: dict[tuple[str, int], float] = field(default_factory=dict)

    def applied_by_order(self) -> dict[str, float]:
        return {r.order_id: r.applied_kg for r in self.rows if r.applied_kg > _EPS}

    def to_frame(self) -> pd.DataFrame:
        cols = ["week_label", "order_id", "sku", "gross_kg", "committed_kg",
                "produced_kg", "carry_in_kg", "applied_kg", "net_kg",
                "status", "week_key"]
        if not self.rows:
            return pd.DataFrame(columns=cols)
        df = pd.DataFrame([{
            "week_label": r.week_label, "order_id": r.order_id, "sku": r.sku,
            "gross_kg": round(r.gross_kg, 1),
            "committed_kg": round(r.committed_kg, 1),
            "produced_kg": round(r.produced_kg, 1),
            "carry_in_kg": round(r.carry_in_kg, 1),
            "applied_kg": round(r.applied_kg, 1),
            "net_kg": round(r.net_kg, 1),
            "status": r.status, "week_key": r.week_key,
        } for r in self.rows])
        return df.sort_values(["week_key", "sku"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# week keying
# ---------------------------------------------------------------------------

def iso_week_key(dt: datetime) -> int:
    """ISO (year, week) as one sortable int: 2026-W34 -> 202634."""
    y, w, _ = pd.Timestamp(dt).isocalendar()
    return int(y) * 100 + int(w)


def _key_label(key: int, iso_mode: bool) -> str:
    return f"WW{key % 100:02d}" if iso_mode else f"W+{key}"


def _hour_keyer(anchor: datetime | None, week_bounds: list[float] | None):
    """hour-offset -> week key, in whichever frame the caller has.

    ISO mode (anchor given) is exact: the hour becomes a wall-clock moment
    and takes its true ISO week — no bucket grid to drift out of phase.
    """
    if anchor is not None:
        a = pd.Timestamp(anchor)
        return lambda h: iso_week_key(a + timedelta(hours=float(h)))
    if week_bounds:
        return lambda h: max(0, bisect.bisect_right(week_bounds, float(h)) - 1)
    return lambda h: max(0, int(float(h) // 168))


def demand_week_grid(
    demand: pd.DataFrame,
    anchor: datetime | None = None,
    week_bounds: list[float] | None = None,
) -> dict[tuple[str, int], float]:
    """(sku, week_key) -> gross kg for a demand_plan frame.

    Keyed by the due window's midpoint (due windows are ≤168h and clipping
    only ever trims the past/beyond-horizon edge, so the midpoint always
    stays inside the true week). Rows with no due window fall back to
    week_index — only sound when the demand frame and the block frame share
    an anchor, which is exactly the legacy-test situation.
    """
    out: dict[tuple[str, int], float] = {}
    if demand is None or demand.empty:
        return out
    keyer = _hour_keyer(anchor, week_bounds)
    has_due = ("due_start_hour" in demand.columns
               and "due_end_hour" in demand.columns)
    for _, r in demand.iterrows():
        sku = str(r.get("sku") or "").strip()
        if not sku:
            continue
        kg = pd.to_numeric(pd.Series([r.get("qty_target")]),
                           errors="coerce").iloc[0]
        if pd.isna(kg) or kg <= 0:
            continue
        key = _row_week_key(r, keyer, has_due)
        if key is None:
            continue
        out[(sku, key)] = out.get((sku, key), 0.0) + float(kg)
    return out


def _row_week_key(r, keyer, has_due: bool) -> int | None:
    if has_due and pd.notna(r.get("due_start_hour")) \
            and pd.notna(r.get("due_end_hour")):
        mid = (float(r["due_start_hour"]) + float(r["due_end_hour"])) / 2.0
        return keyer(mid)
    wk = pd.to_numeric(pd.Series([r.get("week_index")]), errors="coerce").iloc[0]
    return None if pd.isna(wk) else int(wk)


# ---------------------------------------------------------------------------
# supply sides
# ---------------------------------------------------------------------------

def _week_overlap_shares(
    start_h: float,
    end_h: float,
    anchor: datetime | None,
    week_bounds: list[float] | None,
) -> dict[int, float]:
    """week_key -> share of a block's duration falling in that week.

    A block's kg is production spread over its whole run, so a boundary-
    straddling block credits each week by TIME-OVERLAP share (60h in W34 +
    40h in W35 -> 60% / 40%). The previous midpoint rule flipped the entire
    kg across the Monday boundary when a re-forecast stretched the block:
    live 2026-08-21, W35 read 1.91M kg of committed credit against ~1.1M kg
    of total weekly plant capacity. Shares always sum to 1.0.
    """
    dur = float(end_h) - float(start_h)
    keyer = _hour_keyer(anchor, week_bounds)
    if dur <= 0:
        return {keyer((float(start_h) + float(end_h)) / 2.0): 1.0}
    # week boundaries (hour offsets) strictly inside (start_h, end_h)
    cuts: list[float] = []
    if anchor is not None:
        a = pd.Timestamp(anchor)
        ts = a + timedelta(hours=float(start_h))
        monday = (ts.normalize() - timedelta(days=int(ts.weekday()))
                  + timedelta(weeks=1))     # first ISO Monday 00:00 after ts
        while True:
            h = (monday - a).total_seconds() / 3600.0
            if h >= float(end_h):
                break
            if h > float(start_h):
                cuts.append(h)
            monday += timedelta(weeks=1)
    elif week_bounds:
        cuts = [b for b in week_bounds if float(start_h) < b < float(end_h)]
    else:
        b = (float(start_h) // 168.0) * 168.0 + 168.0
        while b < float(end_h):
            if b > float(start_h):
                cuts.append(b)
            b += 168.0
    edges = [float(start_h), *cuts, float(end_h)]
    shares: dict[int, float] = {}
    for lo, hi in zip(edges, edges[1:]):
        key = keyer((lo + hi) / 2.0)
        shares[key] = shares.get(key, 0.0) + (hi - lo) / dur
    return shares


def _supply_from_blocks(
    blocks: pd.DataFrame | None,
    anchor: datetime | None,
    week_bounds: list[float] | None,
) -> tuple[dict[tuple[str, int], float], int]:
    """Planned MO kg (running + queued + pinned), pro-rated by week overlap."""
    supply: dict[tuple[str, int], float] = {}
    unknown = 0
    if blocks is None or not len(blocks):
        return supply, unknown
    prod = blocks[blocks["block_type"] == "production"]
    for _, b in prod.iterrows():
        sku = str(b.get("sku") or "").strip()
        if sku.upper() in _NON_SKUS:
            continue
        kg = pd.to_numeric(pd.Series([b.get("qty_kg")]), errors="coerce").iloc[0]
        if pd.isna(kg) or kg <= 0:
            unknown += 1
            continue
        shares = _week_overlap_shares(float(b["start_h"]), float(b["end_h"]),
                                      anchor, week_bounds)
        for key, share in shares.items():
            supply[(sku, key)] = supply.get((sku, key), 0.0) + float(kg) * share
    return supply, unknown


def _supply_from_completed(
    completed: list[dict] | None,
    anchor: datetime | None,
    lookback_weeks: int,
) -> tuple[dict[tuple[str, int], float], float, list[str]]:
    """Actuals: kg already MADE, by the week they were produced.

    current_state drops completed MOs from the calendar (the board stays
    forward-looking), so without this leg every mid-week plan re-plans kg
    that is already in the warehouse — the Thursday trap. `made_kg` is the
    truth when present (a superseded MO never finishes its Fct qty); a
    truly-complete MO with a blank made column falls back to its Fct kg.
    """
    supply: dict[tuple[str, int], float] = {}
    notes: list[str] = []
    total = 0.0
    if not completed:
        return supply, total, notes
    if anchor is None:
        notes.append(f"{len(completed)} completed MO(s) ignored: no anchor "
                     "to place their production week (ISO mode only)")
        return supply, total, notes
    a = pd.Timestamp(anchor)
    floor = (a - timedelta(days=a.weekday())
             - timedelta(weeks=max(0, int(lookback_weeks))))
    for r in completed:
        sku = str(r.get("item") or "").strip()
        if sku.upper() in _NON_SKUS:
            continue
        start = r.get("start_dt")
        if start is None or pd.isna(start):
            continue
        hours = float(r.get("hours") or 0.0)
        mid = pd.Timestamp(start) + timedelta(hours=hours / 2.0)
        if mid < floor:
            continue
        made = float(r.get("made_kg") or 0.0)
        kg = made if made > 0 else float(r.get("qty_kg") or 0.0)
        if kg <= 0:
            continue
        key = iso_week_key(mid)
        supply[(sku, key)] = supply.get((sku, key), 0.0) + kg
        total += kg
    if total > 0:
        notes.append(f"completed MOs contribute {total:,.0f} kg of "
                     "already-made production to the netting")
    return supply, total, notes


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------

def build_ledger(
    demand: pd.DataFrame,
    blocks: pd.DataFrame | None,
    *,
    completed: list[dict] | None = None,
    anchor: datetime | None = None,
    demand_anchor: datetime | None = None,
    week_bounds: list[float] | None = None,
    history_demand: dict[tuple[str, int], float] | None = None,
    lookback_weeks: int = 4,
    carry_unsettled_past: bool = False,
) -> CoverageLedger:
    """Per SKU×week: gross demand vs committed+produced supply, carry-forward.

    `anchor` is the frame of `blocks` hour offsets (and enables ISO keying);
    `demand_anchor` is the frame of the demand due windows (defaults to
    `anchor` — they coincide in solver staging, differ on the Reconcile
    path where the reference demand file keeps its own anchor).

    `history_demand` is a (sku, week_key) -> gross kg grid for weeks the
    caller's demand frame no longer carries (rebase drops fully-past weeks
    as misses). Past production nets against ITS OWN week's demand there
    first, so a completed pre-build never double-credits surviving weeks.
    Entries colliding with a live demand row are skipped.

    `carry_unsettled_past` (fix C54 / netting-2): kg MADE in a week BEFORE
    the anchor week for which neither a live nor a history demand row
    exists cannot be told apart from last week's own consumption — the live
    demand feed starts at the current week, so every completed MO of the
    prior week used to roll 100% into this week's targets, silently. The
    default excludes such kg from the carry and reports it (ledger.notes
    carries a WARNING string, `unsettled_past` the per-week detail); True
    restores the old carry-forward.
    """
    ledger = CoverageLedger()
    if anchor is not None:
        ledger.anchor_week_key = iso_week_key(anchor)
    if demand is None or demand.empty:
        return ledger

    iso_mode = anchor is not None
    dem_keyer = _hour_keyer(demand_anchor or anchor, week_bounds)
    has_due = ("due_start_hour" in demand.columns
               and "due_end_hour" in demand.columns)

    planned, unknown = _supply_from_blocks(blocks, anchor, week_bounds)
    produced, produced_total, prod_notes = _supply_from_completed(
        completed, anchor, lookback_weeks)
    ledger.unknown_kg_blocks = unknown
    ledger.produced_total_kg = produced_total
    ledger.notes.extend(prod_notes)
    if unknown:
        ledger.notes.append(f"{unknown} committed block(s) carry unknown kg "
                            "and credit nothing — true coverage may be higher")

    # demand rows grouped by (sku, key); original row order kept inside a key
    dem_rows: dict[str, list[tuple[int, str, float]]] = {}
    live_keys: set[tuple[str, int]] = set()
    for _, r in demand.iterrows():
        sku = str(r.get("sku") or "").strip()
        if not sku:
            continue
        kg = pd.to_numeric(pd.Series([r.get("qty_target")]),
                           errors="coerce").iloc[0]
        key = _row_week_key(r, dem_keyer, has_due)
        if key is None:
            continue
        gross = 0.0 if pd.isna(kg) else max(0.0, float(kg))
        dem_rows.setdefault(sku, []).append(
            (key, str(r.get("order_id") or f"{sku}-{key}"), gross))
        live_keys.add((sku, key))

    history: dict[tuple[str, int], float] = {
        k: v for k, v in (history_demand or {}).items() if k not in live_keys}

    if not carry_unsettled_past and ledger.anchor_week_key is not None:
        for (sku, key), kg in list(produced.items()):
            if (key < ledger.anchor_week_key and (sku, key) not in live_keys
                    and (sku, key) not in (history_demand or {})):
                ledger.unsettled_past[(sku, key)] = round(kg, 1)
                del produced[(sku, key)]
        if ledger.unsettled_past:
            tot = sum(ledger.unsettled_past.values())
            detail = ", ".join(
                f"{s} {kg:,.0f} kg in {_key_label(k, iso_mode)}"
                for (s, k), kg in sorted(ledger.unsettled_past.items(),
                                         key=lambda kv: -kv[1])[:5])
            ledger.notes.append(
                f"WARNING: {tot:,.0f} kg of past production has no demand week "
                f"to settle against and is NOT carried forward ({detail}) — "
                "import the prior week's demand or accept it as inventory")

    skus = (set(dem_rows)
            | {s for s, _ in planned} | {s for s, _ in produced}
            | {s for s, _ in history})
    for sku in sorted(skus):
        rows = sorted(dem_rows.get(sku, []), key=lambda t: t[0])
        keys = sorted({k for k, _, _ in rows}
                      | {k for s, k in planned if s == sku}
                      | {k for s, k in produced if s == sku}
                      | {k for s, k in history if s == sku})
        carry = 0.0
        total_gross = sum(g for _, _, g in rows)
        total_supply = 0.0
        for key in keys:
            plan_kg = planned.get((sku, key), 0.0)
            made_kg = produced.get((sku, key), 0.0)
            carry_in = carry
            carry += plan_kg + made_kg
            total_supply += plan_kg + made_kg
            hist = history.get((sku, key), 0.0)
            if hist > 0:
                carry -= min(hist, carry)
            for k2, oid, gross in rows:
                if k2 != key:
                    continue
                applied = min(gross, carry)
                carry -= applied
                net = gross - applied
                if net <= _EPS:
                    status = COVERED
                elif applied > _EPS:
                    status = PARTIAL
                else:
                    status = UNCOVERED
                ledger.rows.append(CoverageRow(
                    order_id=oid, sku=sku, week_key=key,
                    week_label=_key_label(key, iso_mode),
                    gross_kg=gross, committed_kg=plan_kg, produced_kg=made_kg,
                    carry_in_kg=carry_in, applied_kg=applied, net_kg=net,
                    status=status))
                carry_in = 0.0  # only the first row at a key shows the carry
        if total_gross <= _EPS and total_supply > _EPS:
            ledger.no_demand[sku] = round(total_supply, 1)
        elif carry > max(OVERCOMMIT_TOL_PCT * total_gross, OVERCOMMIT_TOL_KG):
            ledger.overcommitted[sku] = {
                "surplus_kg": round(carry, 1),
                "total_gross_kg": round(total_gross, 1),
                "total_supply_kg": round(total_supply, 1),
                "last_week_label": _key_label(keys[-1], iso_mode) if keys else "",
            }
    return ledger


def apply_ledger(
    demand: pd.DataFrame,
    ledger: CoverageLedger,
    *,
    explicit_bounds: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """Reduce qty_target by each order's applied kg (never below zero).

    Default (`explicit_bounds=False`): the pct bounds stay untouched, so
    qty_min/qty_max scale with the reduced target — the contract
    subtract_committed always had (the scorecard's fill-window residual
    still relies on it).

    `explicit_bounds=True` (fix C56 / netting-4, the F staging path): the
    planner's band is [lower_pct x GROSS, upper_pct x GROSS]; the committed
    credit C is production that already sits inside that band, so the
    residual order's bounds are ``max(0, pct x gross - C)`` — NOT ``pct x
    (gross - C)``, which shrank the tolerance with the residual (live: 20
    non-DNS orders asked for 28 t MORE minimum fill and were allowed 479 t
    LESS headroom than the planner's band). Rows that received credit get
    explicit qty_min/qty_max and BLANK pct columns (data_loader prefers pct
    when both are present), plus a `credit_kg` column for later policies
    (agent_policy.trim_dns_demand, C58). Residuals <= 0.5 kg are zeroed
    (netting-7: a 0.4 kg target became a phantom qmin 0 / qmax 1 order).
    """
    notes: list[str] = []
    if demand is None or demand.empty or not ledger.rows:
        return demand, notes
    df = demand.copy()
    # qty_target may arrive int64 from a fresh bridge export; writing a
    # rounded float back into an int column raises since pandas 2.x — and
    # that crash silently degraded F staging to UNSUBTRACTED demand
    # (found 2026-08-14 via an in-process staging reproduction).
    df["qty_target"] = pd.to_numeric(df["qty_target"], errors="coerce").astype(float)
    applied = ledger.applied_by_order()
    if not applied:
        return df, notes
    if explicit_bounds:
        for col in ("qty_min", "qty_max", "lower_pct", "upper_pct", "credit_kg"):
            if col not in df.columns:
                df[col] = float("nan")
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        df["credit_kg"] = df["credit_kg"].fillna(0.0)
    oid_series = df["order_id"].astype(str)
    for oid, kg in applied.items():
        hits = df.index[oid_series == oid]
        if not len(hits):
            continue
        idx = hits[0]
        target = float(df.at[idx, "qty_target"] or 0)
        consumed = min(target, kg)
        if consumed <= 0:
            continue
        residual = round(target - consumed, 1)
        if residual <= _EPS:
            residual = 0.0
        df.at[idx, "qty_target"] = residual
        if explicit_bounds:
            lo, hi = df.at[idx, "lower_pct"], df.at[idx, "upper_pct"]
            if pd.notna(lo) and pd.notna(hi):
                gmin, gmax = target * float(lo), target * float(hi)
            else:
                qmin_raw, qmax_raw = df.at[idx, "qty_min"], df.at[idx, "qty_max"]
                gmin = float(qmin_raw) if pd.notna(qmin_raw) else target
                gmax = float(qmax_raw) if pd.notna(qmax_raw) else target
            df.at[idx, "qty_min"] = round(max(0.0, gmin - consumed), 1)
            df.at[idx, "qty_max"] = round(max(0.0, gmax - consumed), 1)
            if residual <= 0.0:
                df.at[idx, "qty_min"] = 0.0
            df.at[idx, "lower_pct"] = float("nan")
            df.at[idx, "upper_pct"] = float("nan")
            df.at[idx, "credit_kg"] = round(consumed, 1)
        notes.append(
            f"{oid}: committed plan already makes {consumed:,.0f} kg - "
            f"fill target {target:,.0f}->{residual:,.0f} kg")
    return df, notes


# ---------------------------------------------------------------------------
# IO shell — the Reconcile / Plan-page composition
# ---------------------------------------------------------------------------

def demand_source_anchor(dd: Path) -> datetime | None:
    """The demand file's own anchor (demand_plan.source.json), if recorded."""
    import json
    meta = Path(dd) / "reference" / "demand_plan.source.json"
    if not meta.exists():
        return None
    try:
        raw = str(json.loads(meta.read_text(encoding="utf-8")).get("anchor") or "")
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S") if raw else None
    except (OSError, ValueError):
        return None


def keep_completed_row(row: dict, exclude_mos: set[str],
                       board_attrs: dict[str, str] | None) -> bool:
    """made_only credit rule for one completed-leg row (INTEGRATE, agent CA
    handoff to CB, fix CA-3). A row whose MO is NOT on the board is always
    credited. A row whose MO IS on the board is dropped (the block carries
    its kg) — EXCEPT a `made_part` row (kg a RUNNING MO made BEFORE the
    anchor, which no board block represents once the running block holds
    only the remaining kg): it is kept when no board attrs are known, or
    when the MO's board block is a post-fix block (`made_kg=` token). A
    board saved BEFORE CA-3 still draws the running block at FULL Fct kg;
    crediting made_part on top would double count, so that row is dropped
    until the board is rebuilt."""
    mo = str(row.get("mo") or "")
    if mo not in exclude_mos:
        return True
    if str(row.get("kind") or "") != "made_part":
        return False
    if board_attrs is None:
        return True
    return "made_kg=" in str(board_attrs.get(mo) or "")


def build_ledger_from_data(
    data_dir: Path | str,
    cfg: dict | None = None,
    *,
    state=None,
    lookback_weeks: int = 4,
    made_only: bool = False,
    exclude_mos: set[str] | None = None,
    board_attrs: dict[str, str] | None = None,
) -> CoverageLedger | None:
    """Ledger for the live data dir: reference demand vs manprg current state.

    Returns None when there is no demand plan. `state` lets a page reuse an
    already-built CurrentState instead of parsing manprg twice.

    `made_only=True` — THE CALENDAR-PAGE INVARIANT (user mandate 2026-08-21):
    a surface that shows calendar_blocks.csv must count board kg FROM THE
    BOARD and take ledger credit ONLY for kg no visible block represents.
    Committed MOs ARE board blocks after a rebuild, so crediting them here
    too is double counting by construction. In this mode the supply side is
    completed (hidden) MOs' made kg ONLY — no committed blocks, no pinned
    calendar blocks — and `exclude_mos` (the order_ids visible on the board)
    drops any completed MO whose block still IS on the board. The SOLVER's
    netting must keep the full supply (a queued MO's kg is real future
    production it must not re-plan): never pass made_only on a netting path.
    """
    from helpers import horizon as _hz
    from helpers.config import datasources_config, load_toml
    from helpers.plan_fill import pinned_blocks

    dd = Path(data_dir)
    ref = dd / "reference"
    dem_path = ref / "demand_plan.csv"
    if not dem_path.exists():
        return None
    cfg = cfg or load_toml()
    hz = _hz.resolve(cfg)
    demand = pd.read_csv(dem_path, dtype={"sku": str})

    if state is None:
        from helpers.current_state import build_current_state
        ds = datasources_config(cfg)
        mp_paths = [p.strip() for p in str(ds.get("manprg_files", "")).split(";")
                    if p.strip()] or [str(ref / "manprg.txt"),
                                      str(ref / "manprg2.txt")]
        cip_path = str(ds.get("cip_info_csv", "")).strip() or str(
            ref / "cip_info.csv")
        state = build_current_state(
            hz, manprg_paths=[p for p in mp_paths if Path(p).exists()],
            cip_path=cip_path if Path(cip_path).exists() else None, cfg=cfg,
            caps_path=ref / "capabilities_rates.csv")

    completed = getattr(state, "completed", None)
    if made_only:
        blocks = None
        if completed and exclude_mos:
            # `made_part` rows (fix CA-3) carry the kg a RUNNING MO made
            # BEFORE the anchor; the board block of that MO holds only the
            # REMAINING kg (its attrs carry a `made_kg=` token). Dropping
            # the row because the MO is on the board under-credited holding
            # by the made share (272 t on the snapshot) — INTEGRATE, agent
            # CA handoff to CB. Rule: a made_part row is kept when no board
            # attrs are known (post-fix boards) or when the board block for
            # that MO is a post-fix block (`made_kg=` in attrs); a board
            # saved BEFORE CA-3 still draws the running block at FULL Fct
            # kg, so crediting made_part on top would double count — such a
            # row is dropped until the board is rebuilt.
            completed = [r for r in completed
                         if keep_completed_row(r, exclude_mos, board_attrs)]
    else:
        blocks = state.blocks
        cal_path = dd / "calendar_blocks.csv"
        if cal_path.exists():
            from helpers.calendar_io import load_calendar
            pinned = pinned_blocks(load_calendar(cal_path))
            if pinned is not None and len(pinned):
                blocks = pd.concat([blocks, pinned], ignore_index=True)

    # Reference demand keeps its own frame (hour 0 = the file's earliest ISO
    # Monday) — the blocks live in the horizon frame. ISO keying absorbs the
    # difference as long as each side converts with ITS OWN anchor.
    demand_anchor = demand_source_anchor(dd) or hz.anchor
    return build_ledger(
        demand, blocks,
        completed=completed,
        anchor=hz.anchor, demand_anchor=demand_anchor,
        lookback_weeks=lookback_weeks)
