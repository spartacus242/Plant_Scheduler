# helpers/plan_fill.py — Scenario F: committed plan fixed, fill the tail.
#
# The user's process (design doc scenario-f-fill-the-tail-2026-08-14):
# manprg + cip_info ARE the plan for the committed stretch — laid out as
# FIXED, non-negotiable line-time. The solver's only job is placing
# demand_plan orders into what remains: right tonnage on the right ISO week,
# minimum changeovers, tails packed. Nothing here ever rewrites the plant's
# own plan — there is no current_mo.csv and no mo_changes in Scenario F.
#
# Pure functions; the IO shell lives in scenario_runner._overlay_fill.

from __future__ import annotations

import math
from typing import Any

import pandas as pd

# Layer-1 CIPs are projected at the line's real max interval through the
# whole horizon, so consecutive cleans are never further apart than that
# interval — any fill production between them is bounded by construction.
# The solver's own CIP generator must therefore stand down: a huge interval
# means its dirty-clock never forces a clean of its own (which would double
# up with the projected ones).
SOLVER_CIP_INTERVAL_STANDDOWN_H = 100_000


def pinned_blocks(calendar: pd.DataFrame) -> pd.DataFrame:
    """Planner-pinned production blocks: demand orders fixed to a line/time
    on the Plant Calendar (attrs token 'pinned', toggled in the block popup).

    They join the committed layer — blocked windows the solver must plan
    around, and their kg credits the demand targets — but they must NEVER
    move the fill gate or the changeover base: a block pinned deep in W35
    would otherwise block fill of the whole line before it (the same trap
    projected CIPs hit; see line_free_from).

    Exact-token match on the ';'-separated attrs — a substring test would
    also match future tokens like 'unpinned'. Blocks carrying a
    current_state:* token are EXCLUDED even if pinned: they ARE manprg MOs,
    already committed via build_current_state — staging them again would
    double-block the window and double-credit their kg against demand.
    """
    if calendar is None or calendar.empty or "attrs" not in calendar.columns:
        return (calendar.iloc[0:0] if calendar is not None
                else pd.DataFrame())
    attrs = calendar["attrs"].astype(str)
    mask = (
        attrs.str.split(";").apply(lambda ts: "pinned" in ts)
        & ~attrs.str.contains("current_state:", regex=False)
        & (calendar["block_type"].astype(str) == "production")
    )
    return calendar[mask]


def planner_cip_blocks(calendar: pd.DataFrame) -> pd.DataFrame:
    """CIP blocks the PLANNER placed on the Plant Calendar (block popup /
    gap picker, 2026-09-01) — including the re-forecast projected cleans
    they trigger (attrs token planner:cip_projected). Committed windows the
    solver plans around, exactly like pinned production.

    POSITIVE match on a ``planner:`` attrs token (fix C41 / cip-1, audit
    2026-09-02). The previous filter was purely negative (not current_state,
    not cipinfo_/dt_) and therefore also selected the cleans the pipeline
    itself draws (materialize_required_cips, attrs ``solver:cip_req``) and
    solver cip_windows.csv imports (attrs ''). Those were re-staged as the
    planner's re-forecast, which then deleted the plant-projected CIP grid
    for the line while the solver's own CIP generator stood down — live P12
    ran 213h dirty against a 120h interval and validation said OK. App-
    generated cleans must be RE-DERIVED each run, never inherited.
    """
    if calendar is None or calendar.empty or "attrs" not in calendar.columns:
        return (calendar.iloc[0:0] if calendar is not None
                else pd.DataFrame())
    attrs = calendar["attrs"].astype(str)
    ids = calendar["block_id"].astype(str)
    has_planner = attrs.str.split(";").apply(
        lambda ts: any(str(t).strip().startswith("planner:") for t in ts))
    mask = (
        (calendar["block_type"].astype(str) == "cip")
        & has_planner
        & ~attrs.str.contains("current_state:", regex=False)
        & ~attrs.str.contains("solver:", regex=False)
        & ~ids.str.startswith(("cipinfo_", "dt_"))
    )
    return calendar[mask]


def rebase_board(calendar: pd.DataFrame, shift_h: float) -> tuple[pd.DataFrame, int]:
    """Move stored board hours into the staging frame (fix C55 / netting-3
    / staging-6). calendar_blocks.csv hours are offsets from the CONFIG
    anchor ([scheduler] planning_start_date); staging works in hz.anchor
    hours. Rolling the board is a manual button, so an un-rolled board
    (the overnight batch runs after midnight, before anyone rolls) staged
    pinned blocks and planner CIPs `shift_h` hours late (measured: 24h).
    Returns (rebased frame, number of blocks dropped as fully past)."""
    if calendar is None or calendar.empty or not shift_h:
        return calendar, 0
    from helpers.horizon import rebase_calendar

    out = rebase_calendar(calendar, float(shift_h))
    past = pd.to_numeric(out["end_h"], errors="coerce").fillna(0.0) <= 0.0
    return out[~past].copy(), int(past.sum())


def filter_known_lines(
    blocks: pd.DataFrame, known: set[str] | None,
) -> tuple[pd.DataFrame, list[str]]:
    """Drop blocks on lines that lines.csv does not know (fix adversarial-11).

    current_state.line_id_for invents a line_id for ANY name (LMH-P99 ->
    90) — staged into downtimes/committed windows that phantom line id
    reaches the solver. With `known` (upper-case names from lines.csv) the
    unknown lines are skipped and REPORTED. An empty/None `known` means
    lines.csv is absent: nothing is filtered."""
    if blocks is None or not len(blocks) or not known:
        return blocks, []
    names = blocks["line_name"].astype(str).str.strip().str.upper()
    bad = ~names.isin({str(k).upper() for k in known})
    if not bad.any():
        return blocks, []
    return blocks[~bad].copy(), sorted(set(names[bad]))


def materialize_required_cips(
    calendar: pd.DataFrame,
    co_map: dict,
    cip_h: float,
    *,
    attrs: str = "solver:cip_req",
) -> tuple[pd.DataFrame, list[str]]:
    """Draw the clean the solver left room for (user rule 2026-09-01).

    For every adjacent production pair on a line whose changeovers.csv row
    is cip_req_after == 1 and whose gap holds no CIP block, insert a cip
    block at the earlier run's end. The solver's setup floor (data_loader)
    guarantees the slot; a gap that still comes up short (committed
    boundaries, rounding) is REPORTED, never filled with an overlapping
    block. Gap containment mirrors score_changeovers' waiver rule exactly,
    so what this draws is precisely what the scorecard then waives.
    Returns (calendar with the new rows, human notes).
    """
    import uuid

    notes: list[str] = []
    if calendar is None or calendar.empty:
        return calendar, notes
    cal = calendar.copy()
    # Group by LINE NAME, not line_id (fix C90): the committed layer is read
    # back with dtype=str (line_id "6") while fill rows carry int 6, so a
    # groupby on line_id split every committed->fill boundary into two
    # groups and no cip_req transition across it was ever seen (live P15
    # 280611 -> 280104 at h198: 6.8h gap, no clean drawn).
    lname = cal["line_name"].astype(str).str.strip().str.upper()
    btype = cal["block_type"].astype(str).str.lower()
    cips: dict[str, list[tuple[float, float]]] = {}
    for ln, c in zip(lname[btype == "cip"], cal[btype == "cip"].to_dict("records")):
        cips.setdefault(ln, []).append((float(c["start_h"]), float(c["end_h"])))
    # Occupancy of EVERYTHING else on the line (fix C32 / changeover-4 /
    # cip-6): trials, maintenance, line-downs, contractor windows and other
    # production. A clean is never drawn over one of those — reported instead.
    occupied: dict[str, list[tuple[float, float, str]]] = {}
    for ln, r in zip(lname[btype != "cip"], cal[btype != "cip"].to_dict("records")):
        occupied.setdefault(ln, []).append(
            (float(r["start_h"]), float(r["end_h"]),
             f"{r.get('block_type')} {r.get('order_id') or r.get('label') or ''}".strip()))
    prod_mask = btype == "production"
    prod = cal[prod_mask].assign(_ln=lname[prod_mask]).sort_values(["_ln", "start_h"])
    tol = 0.5   # hours — a clean touching the gap within this counts as covering it
    new_rows: list[dict] = []

    def _blocker(ln: str, s: float, e: float) -> str | None:
        for os_, oe_, what in occupied.get(ln, []):
            if os_ < e - 1e-6 and oe_ > s + 1e-6:
                return what
        return None

    for ln, grp in prod.groupby("_ln"):
        rows = grp.to_dict("records")
        line_cips = list(cips.get(ln, []))
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            fs, ts = str(a.get("sku", "")), str(b.get("sku", ""))
            if fs == ts:
                continue
            flags = co_map.get((fs, ts))
            if not flags or int(flags.get("cip_req_after", 0) or 0) != 1:
                continue
            a_end, b_start = float(a["end_h"]), float(b["start_h"])
            # An existing CIP that intersects the gap (with tolerance) already
            # separates the two runs — never duplicate it. The old test was
            # strict containment, so a clean at 235.9-241.9 next to a run
            # ending at 236.0 got a second, overlapping clean drawn.
            if any(cs <= b_start + tol and ce >= a_end - tol
                   for cs, ce in line_cips):
                continue
            gap = b_start - a_end
            if gap + 1e-6 < cip_h:
                notes.append(
                    f"{a.get('line_name')}: {fs}->{ts} at {a_end:.1f}h needs a "
                    f"{cip_h:g}h clean but the gap is {gap:.1f}h — left for the "
                    "planner")
                continue
            # Preferred slot flush at the earlier run's end; fall back to
            # flush before the later run; otherwise report (never overlap).
            slot = None
            for s0 in (a_end, b_start - float(cip_h)):
                e0 = s0 + float(cip_h)
                if s0 < a_end - 1e-6 or e0 > b_start + 1e-6:
                    continue
                if _blocker(ln, s0, e0) is None:
                    slot = (s0, e0)
                    break
            if slot is None:
                what = _blocker(ln, a_end, b_start) or "another block"
                notes.append(
                    f"{a.get('line_name')}: {fs}->{ts} at {a_end:.1f}h needs a "
                    f"{cip_h:g}h clean but the gap holds {what} — left for the "
                    "planner")
                continue
            s0, e0 = slot
            new_rows.append({
                "block_id": "cip_" + uuid.uuid4().hex[:10],
                "block_type": "cip",
                "line_id": a.get("line_id"),
                "line_name": a.get("line_name"),
                "start_h": s0,
                "end_h": e0,
                "label": "CIP",
                "order_id": "",
                "sku": "CIP",
                "sku_description": f"required clean {fs}->{ts}",
                "qty_kg": None,
                "locked": False,
                "attrs": attrs,
            })
            line_cips.append((s0, e0))
            occupied.setdefault(ln, []).append((s0, e0, "cip"))
            notes.append(f"{a.get('line_name')}: CIP drawn at {s0:.1f}h "
                         f"for {fs}->{ts}")
    if new_rows:
        cal = pd.concat([cal, pd.DataFrame(new_rows, columns=cal.columns)],
                        ignore_index=True)
    return cal, notes


COMMITTED_CIP_REASON = "Committed CIP"


def cip_grid_gaps(
    blocks: pd.DataFrame,
    intervals: dict[str, float],
    horizon_h: float,
    *,
    default_interval_h: float = 120.0,
    tol_h: float = 1.0,
) -> list[str]:
    """Staging assertion (fix C41): every line with committed production must
    carry a clean grid through the horizon — consecutive CIP starts no
    further apart than the line's max interval, the first clean no later
    than one interval, the last one within an interval of the horizon end.
    Returns loud human notes (empty = grid intact). Never raises: the note
    goes to the run log where the planner sees it."""
    notes: list[str] = []
    if blocks is None or not len(blocks):
        return notes
    names = blocks["line_name"].astype(str).str.strip().str.upper()
    btype = blocks["block_type"].astype(str).str.lower()
    for ln in sorted(set(names[btype == "production"])):
        iv = float(intervals.get(ln, default_interval_h) or default_interval_h)
        cips = sorted(
            float(s) for s, t in zip(blocks["start_h"][names == ln],
                                     btype[names == ln])
            if t == "cip" and 0.0 <= float(s) < float(horizon_h))
        if not cips:
            notes.append(
                f"CIP GRID MISSING on {ln}: committed production but no clean "
                f"in [0, {horizon_h:g}h) — the line would run dirty for the "
                f"whole horizon (interval {iv:g}h)")
            continue
        worst = max([cips[0]] + [b - a for a, b in zip(cips, cips[1:])]
                    + [float(horizon_h) - cips[-1]])
        if worst > iv + tol_h:
            notes.append(
                f"CIP GRID GAP on {ln}: {worst:.0f}h between cleans exceeds the "
                f"{iv:g}h interval (cleans at "
                f"{', '.join(f'{c:.0f}' for c in cips)}h)")
    return notes


def gate_warnings(
    gates: dict[str, float],
    blocks: pd.DataFrame,
    horizon_h: float,
    *,
    warn_frac: float = 0.5,
) -> list[str]:
    """Loud notes for lines whose fill gate swallows most/all of the horizon
    (fix staging-10). A gate at H means ZERO fill capacity on that line —
    almost always a data problem (a stale re-forecast, cf. C01) — and used
    to be reported only as '{n} line(s) gated'. Names the block responsible
    (the committed work block that ends last on that line)."""
    notes: list[str] = []
    H = float(horizon_h)
    last: dict[str, str] = {}
    if blocks is not None and len(blocks):
        work = blocks[blocks["block_type"].astype(str).isin(["production", "trial"])]
        for ln, grp in work.groupby(work["line_name"].astype(str).str.upper()):
            b = grp.sort_values("end_h").iloc[-1]
            last[ln] = (f"{b.get('block_type')} {b.get('order_id') or ''} "
                        f"{b.get('sku') or ''}").strip()
    for ln in sorted(gates):
        g = float(gates[ln])
        who = last.get(str(ln).upper(), "committed work")
        if g >= H:
            notes.append(
                f"LINE {ln} FULLY BLOCKED: fill gate {g:.0f}h >= horizon "
                f"{H:.0f}h — no fill possible on this line ({who} ends at "
                f"or beyond the horizon; check the re-forecast)")
        elif g >= warn_frac * H:
            notes.append(
                f"line {ln}: fill gate {g:.0f}h blocks {g / H:.0%} of the "
                f"horizon ({who})")
    return notes


def committed_windows(blocks: pd.DataFrame, horizon_h: float) -> list[dict]:
    """Every committed block (production, trial, cip) as a blocked window.

    Rows in downtimes.csv shape. Clipped to [0, horizon]; windows are rounded
    OUTWARD (floor start, ceil end) so a fill block can never shave an hour
    off committed work. Fully-past windows are dropped.
    """
    out: list[dict] = []
    if blocks is None or not len(blocks):
        return out
    for _, b in blocks.iterrows():
        s = math.floor(max(0.0, float(b["start_h"])))
        e = math.ceil(min(float(horizon_h), float(b["end_h"])))
        if e <= 0 or e <= s:
            continue
        label = str(b.get("block_type", "")).upper()
        ref = str(b.get("order_id") or b.get("sku") or "")
        # A committed clean is emitted with the PURE reason "Committed CIP"
        # (fix C29 staging side): coalesce_windows(keep_cips_separate=True)
        # keeps such rows apart from production so the solver's fixed-window
        # waiver and the validator's CIP_INTERVAL check can see the clean's
        # exact end instead of one merged 'PRODUCTION ... + CIP' blob.
        reason = (COMMITTED_CIP_REASON if label == "CIP"
                  else f"Committed {label} {ref}".strip())
        out.append({
            "line_id": int(b.get("line_id", 0) or 0),
            "line_name": str(b["line_name"]).upper(),
            "start_hour": int(s),
            "end_hour": int(e),
            "reason": reason,
        })
    return out


def last_sku_per_line(blocks: pd.DataFrame) -> dict[str, str]:
    """SKU of the LAST committed production block per line (changeover base
    for the first fill block). Trials/CIPs have no changeover identity."""
    out: dict[str, str] = {}
    if blocks is None or not len(blocks):
        return out
    prod = blocks[blocks["block_type"] == "production"]
    for line, grp in prod.groupby(prod["line_name"].astype(str).str.upper()):
        last = grp.sort_values("end_h").iloc[-1]
        sku = str(last.get("sku") or "").strip()
        if sku and sku.upper() not in ("CIP", "TRIALS"):
            out[line] = sku
    return out


def line_free_from(blocks: pd.DataFrame, horizon_h: float) -> dict[str, float]:
    """First hour each line is free of committed WORK (production + trials),
    clipped to [0, horizon]. Fill may not start before this — gaps INSIDE the
    committed stretch belong to the plant team, not the solver.

    Projected CIPs are deliberately EXCLUDED: they are spaced through the
    whole horizon, so counting them gated every line at ~504h, clamped every
    order's window capacity to zero and made an EMPTY schedule "OPTIMAL"
    (measured on F run 2). Future CIPs block fill via their windows instead.
    """
    out: dict[str, float] = {}
    if blocks is None or not len(blocks):
        return out
    work = blocks[blocks["block_type"].isin(["production", "trial"])]
    for line, grp in work.groupby(work["line_name"].astype(str).str.upper()):
        out[line] = float(min(horizon_h, max(0.0, grp["end_h"].max())))
    return out


def rebase_demand(
    demand: pd.DataFrame,
    shift_h: float,
    horizon_h: float,
) -> tuple[pd.DataFrame, list[str]]:
    """Shift demand due windows from the demand file's anchor frame into the
    staging frame (staging hour 0 = shift_h hours after the demand anchor).

    The demand import is self-anchoring (Monday of its earliest ISO week),
    but the rolling horizon anchors at TODAY. Staged unshifted, every due
    window read late by the gap — with a Friday anchor the whole demand grid
    slid +4 days, so "this week's" leftovers looked placeable through next
    Thursday and next week's demand looked due a week out (user report
    2026-08-14: holding buckets inverted vs reality).

    Weeks whose shifted window ends at/before hour 0 are OVER — their unmet
    tonnage is a miss, not a plan item — and are dropped with a note.
    """
    notes: list[str] = []
    if demand is None or demand.empty:
        return demand, notes
    raw_end = pd.to_numeric(demand["due_end_hour"], errors="coerce")
    raw_start = pd.to_numeric(demand["due_start_hour"], errors="coerce")
    if not shift_h and not bool((raw_end >= horizon_h).any()):
        return demand, notes      # zero shift, nothing clipped: identity
    df = demand.copy()
    # inclusive-hour width of the ORIGINAL due window (168 for a full week)
    full_h = (raw_end - raw_start + 1.0).clip(lower=1.0)
    df["due_start_hour"] = (raw_start - shift_h).clip(lower=0)
    df["due_end_hour"] = raw_end - shift_h
    past = df["due_end_hour"] <= 0
    if past.any():
        gone = df[past]
        notes.append(
            f"{int(past.sum())} order(s) dropped: their demand week ended "
            f"before the horizon starts ({gone['qty_target'].sum():,.0f} kg "
            "unmet is a MISS, not a plan item)")
        df = df[~past].copy()
        full_h = full_h[~past]
    beyond = pd.to_numeric(df["due_start_hour"], errors="coerce") >= horizon_h
    if beyond.any():
        notes.append(f"{int(beyond.sum())} order(s) dropped: due week starts "
                     "beyond the horizon")
        df = df[~beyond].copy()
        full_h = full_h[~beyond]
    # A due week only PARTLY inside the horizon keeps only the share of its
    # target that its covered hours can carry (fix C20 / time-3): W3 staged
    # as [456, 503] = 48h of 168h used to keep the whole 1,715 t, 243% of
    # what every line together could make in 48h — the solver chased an
    # impossible week and the service score punished it. The deferred
    # remainder is REPORTED (it is next run's plan item, not lost demand).
    clipped = df["due_end_hour"] > horizon_h - 1
    if clipped.any():
        covered_h = (float(horizon_h) - df.loc[clipped, "due_start_hour"]).clip(lower=0.0)
        frac = (covered_h / full_h[clipped]).clip(lower=0.0, upper=1.0)
        tgt = pd.to_numeric(df.loc[clipped, "qty_target"], errors="coerce").fillna(0.0)
        deferred = float((tgt * (1.0 - frac)).sum())
        df["qty_target"] = pd.to_numeric(df["qty_target"], errors="coerce").astype(float)
        df.loc[clipped, "qty_target"] = (tgt * frac).round(1)
        for col in ("qty_min", "qty_max"):
            if col in df.columns:
                v = pd.to_numeric(df.loc[clipped, col], errors="coerce")
                if v.notna().any():
                    df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
                    df.loc[clipped, col] = (v * frac).round(1)
        weeks = sorted({str(w) for w in df.loc[clipped, "week_index"]}) \
            if "week_index" in df.columns else []
        notes.append(
            f"{int(clipped.sum())} order(s) in week(s) {', '.join(weeks) or '?'} "
            f"only partly inside the horizon: targets pro-rated to the covered "
            f"hours ({float(covered_h.iloc[0]):.0f}h of {float(full_h[clipped].iloc[0]):.0f}h); "
            f"{deferred:,.0f} kg DEFERRED to the next run, not dropped")
    df["due_end_hour"] = df["due_end_hour"].clip(upper=horizon_h - 1)
    return df, notes


def subtract_committed(
    demand: pd.DataFrame,
    blocks: pd.DataFrame,
    week_bounds: list[float] | None = None,
    *,
    anchor=None,
    completed: list[dict] | None = None,
    history_demand: dict[tuple[str, int], float] | None = None,
    lookback_weeks: int = 4,
    explicit_bounds: bool = False,
    carry_unsettled_past: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """Reduce demand targets by what the committed plan already produces.

    Thin wrapper over helpers.demand_coverage — the SKU×week ledger is the
    single netting engine (Reconcile and the holding cards read the same
    math). Pass `anchor` (the frame of the block/demand hour offsets) to
    key by TRUE ISO weeks; `week_bounds` / the 168h grid are the anchorless
    fallback. `completed` (current_state's dropped completed-MO rows) adds
    kg already MADE; `history_demand` lets past production settle against
    its own week's demand first. Targets never go below zero. With the
    default `explicit_bounds=False` the pct bounds stay (legacy contract:
    qty_min/qty_max scale with the reduced target); `explicit_bounds=True`
    writes the planner band minus credit instead (see apply_ledger, C56).
    `carry_unsettled_past` — see build_ledger (C54).
    """
    if demand is None or demand.empty:
        return demand, []
    from helpers.demand_coverage import apply_ledger, build_ledger

    ledger = build_ledger(
        demand, blocks, completed=completed, anchor=anchor,
        week_bounds=week_bounds, history_demand=history_demand,
        lookback_weeks=lookback_weeks,
        carry_unsettled_past=carry_unsettled_past)
    return apply_ledger(demand, ledger, explicit_bounds=explicit_bounds)


def _is_pure_cip_row(r: dict) -> bool:
    return str(r.get("reason", "")).strip().lower() == COMMITTED_CIP_REASON.lower()


def coalesce_windows(rows: list[dict], *, keep_cips_separate: bool = False) -> list[dict]:
    """Union of blocked windows per line -> disjoint rows.

    Committed MO windows can OVERLAP existing line-downs (the plant plans
    onto P11/P13 even while downtimes.csv says they are down - the exact
    inconsistency Reconcile flags). Two overlapping FIXED intervals make
    NoOverlap instantly infeasible at every relax level (measured on the
    first F run), so everything blocked is merged into one interval union.

    Hours are integers rounded OUTWARD — floor(start), ceil(end) — so the
    solver can never plan into a live outage (fix C11 / staging-7: the old
    int()/round() shaved up to an hour off a downtime ending at x.983).

    `keep_cips_separate=True` (fix C29 staging side): rows whose reason is
    exactly "Committed CIP" are unioned among themselves and emitted as
    their own rows; every other window is unioned and then CUT around the
    cleans so no two fixed intervals overlap. The solver's fixed-window
    waiver and the validator's CIP_INTERVAL check then see each clean's
    real start/end instead of a 'PRODUCTION ... + CIP' blob.
    """
    def _union(rs_in: list[dict]) -> list[dict]:
        by_line: dict[str, list[dict]] = {}
        for r in rs_in:
            by_line.setdefault(str(r["line_name"]).upper(), []).append(r)
        merged: list[dict] = []
        for line, rs in sorted(by_line.items()):
            rs = sorted(rs, key=lambda r: (float(r["start_hour"]), float(r["end_hour"])))
            cur = None
            for r in rs:
                s0, e0 = float(r["start_hour"]), float(r["end_hour"])
                if cur is None:
                    cur = dict(r)
                    continue
                if s0 <= float(cur["end_hour"]) + 1e-9:   # overlap or touch: merge
                    cur["end_hour"] = max(float(cur["end_hour"]), e0)
                    if str(r["reason"]) not in str(cur["reason"]):
                        cur["reason"] = f"{cur['reason']} + {r['reason']}"[:120]
                else:
                    merged.append(cur)
                    cur = dict(r)
            if cur is not None:
                merged.append(cur)
        for r in merged:
            r["start_hour"] = int(math.floor(float(r["start_hour"])))
            r["end_hour"] = int(math.ceil(float(r["end_hour"])))
        return merged

    if not keep_cips_separate:
        return _union(list(rows))

    cip_rows = _union([r for r in rows if _is_pure_cip_row(r)])
    other_rows = _union([r for r in rows if not _is_pure_cip_row(r)])
    cips_by_line: dict[str, list[tuple[int, int]]] = {}
    for c in cip_rows:
        cips_by_line.setdefault(str(c["line_name"]).upper(), []).append(
            (int(c["start_hour"]), int(c["end_hour"])))
    out: list[dict] = []
    for w in other_rows:
        pieces = [(int(w["start_hour"]), int(w["end_hour"]))]
        for cs, ce in cips_by_line.get(str(w["line_name"]).upper(), []):
            nxt: list[tuple[int, int]] = []
            for s, e in pieces:
                if ce <= s or cs >= e:
                    nxt.append((s, e))
                    continue
                if s < cs:
                    nxt.append((s, cs))
                if ce < e:
                    nxt.append((ce, e))
            pieces = nxt
        for s, e in pieces:
            if e > s:
                out.append({**w, "start_hour": s, "end_hour": e})
    out.extend(cip_rows)
    out.sort(key=lambda r: (str(r["line_name"]).upper(),
                            int(r["start_hour"]), int(r["end_hour"])))
    return out
