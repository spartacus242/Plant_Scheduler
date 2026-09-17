# helpers/greedy_fill.py — deterministic tail-packing seed for Scenario F.
#
# CP-SAT improves incumbents slowly on the joint fill+changeover structure
# (measured 2026-08-14: W35 fill plateaued at ~240-310t of 1,364t across
# 300s/600s budgets, dead-pair pruning and a fill-first decision strategy).
# So we CONSTRUCT a complete fill greedily — earliest week first, biggest
# orders first, same-SKU adjacency preferred, setup gaps inserted, CIP/
# committed windows respected — and hand it to the solver as a full warm
# start (prev_schedule.csv). The solver then spends its whole budget
# IMPROVING a dense plan instead of discovering one. Hints never change the
# feasible set: a flawed seed costs search time, never correctness.
#
# The seed is only worth anything if EVERY row satisfies the model
# (fix C2, 2026-09-16): phase2_scheduler anchors the hint with every hinted
# variable fixed, and one violating row makes that anchor INFEASIBLE, so
# pass 1 starts cold (the 09-16 board: 8 violating rows -> first pass-1
# solution 0 kg at 28.6 s). The placement rules below therefore mirror
# solver/model_builder.py constraint for constraint:
#   * setup TIME between two fill blocks on a line is PAIRWISE over every
#     two blocks, adjacent or not, whatever sits between them (a committed
#     CIP waives only the changeover PRICE, never the hours):
#         start_j >= end_i + setup(sku_i, sku_j)
#     where a block is the whole SPAN of one (line, order) assignment,
#     [seg_a start, seg_b end) — the model orders spans, never segments;
#   * the FIRST fill block of a line (earliest start) pays the initial
#     changeover from the SKU the line holds at its gate, plus the long-
#     shutdown extra when flagged:
#         start_first >= gate + setup(initial_sku, sku) + extra
#     (initial_sku CLEAN -> no setup). A committed PRODUCTION window after
#     the gate carries no SKU in the model and charges nothing.
#   * setup hours are whole model hours, rounded UP (changeover_cache);
#   * produced kg use the model's integer rate round(rate), and the order's
#     total over all its lines stays <= qty_max;
#   * no row is shorter than min_run_hours (the model's per-(line, order) and
#     per-segment floor).
#
# Three passes (2026-09-16):
#   1. MAIN: every order inside its own due window [due_start, due_end + 1),
#      one row per (line, order), earliest week first, with the seed's share
#      floor (min_run_pct x qty_min / rate per line) on top of min_run_hours.
#   1b. IN-WINDOW REMAINDER: every order still below target — W0 included —
#      gets one more try inside its OWN due window, same week order, on lines
#      it does not use yet (one assignment per (line, order)), at the model's
#      floor: min_run_hours alone under soft demand (the share floor stays
#      when soft demand is off). This runs BEFORE the pre-build so a nearer
#      week keeps its own hours: the model prefers them (+1 % per week in the
#      week gradient, early kg priced per week-step), and without this pass
#      the pre-build filled them with later weeks' orders while the nearer
#      order stayed short (review 2026-09-16).
#   2. PRE-BUILD (planner decision 2026-09-16: "the solver can pre-build the
#      full week 41 into the idle tail as inventory"; plant rule
#      early_fill_hours = "unbounded"): orders still below qty_target get
#      extra runs in time BEFORE their due_start that pass 1 left free, down
#      to the model's own hard start floor (model_builder.effective_due_start:
#      0 under the unbounded policy, due_start - early_fill_hours when
#      bounded, never before the stock-policy earliest_start_hour; the line
#      gate + initial changeover and the committed windows bound it like any
#      other run). The model holds ONE assignment per (line, order) — seg_a
#      plus an optional seg_b behind a clean — so a pre-build run is either
#        - an EXTENSION of the order's row on that line, contiguous and
#          earlier in time (setup re-checked against the blocks on its left),
#        - a seg_a in front of a committed CIP window with the existing row
#          as seg_b (nothing else between them; each segment >= the minimum
#          run), or
#        - a NEW row on a line the order does not use yet (within
#          max_lines_per_order), under every rule of the main pass.
#      Within this pass: nearest due week first, then the largest remaining
#      target (every order's in-window chance came first, in 1 and 1b); per order
#      the candidate with the fewest early week-steps (the model prices early
#      kg per week-step) wins, then the cheapest transition (same SKU next
#      door = free), then the LATEST end, then the fastest line. The pass
#      only adds: no row placed by the main pass loses an hour.
#
# Pure functions — the IO shell lives in scenario_runner.

from __future__ import annotations

import math
from dataclasses import dataclass, field

WEEK_STEP_H = 168     # model_builder.WEEK_STEP_H: early kg are priced per step


@dataclass
class _LineState:
    line_id: int
    segments: list[tuple[float, float]] = field(default_factory=list)
    tail_sku: str = ""          # sku of the last block PLACED (ranking only)
    # (line, order) spans placed so far, (start, end, sku, order_id), sorted
    # by start: the setup floors are derived from these in TIME order, never
    # from tail_sku. A span covers seg_a start .. seg_b end (the model's
    # pairwise ordering uses seg_a_start and eff_end).
    blocks: list[tuple[int, int, str, str]] = field(default_factory=list)
    gate: float = 0.0           # initial_states available_from_hour
    init_sku: str = ""          # SKU the line holds at its gate
    init_extra: float = 0.0     # long-shutdown extra on the initial changeover
    # committed CIP windows [start, end) — the only thing a seg_b may resume
    # behind while the solver's own CIPs are stood down (model_builder
    # fixed_cip_windows: downtime rows whose reason contains "CIP")
    cip_windows: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class _Order:
    d: dict
    oid: str
    sku: str
    week: int
    target: float
    qmin: int
    qmax: int
    ds: float                   # seed start floor inside the due window (whole h)
    de: float                   # exclusive due-window end (whole h)
    model_floor: int            # the model's hard start floor (effective_due_start)
    remaining: float
    placed_model_kg: int = 0    # sum over rows of round(rate) x run_h
    lines: list[str] = field(default_factory=list)


def free_segments(gate_h: float, blocked: list[tuple[float, float]],
                  horizon_h: float) -> list[tuple[float, float]]:
    """Invert blocked windows over [gate, horizon] -> disjoint free spans."""
    s = max(0.0, float(gate_h))
    out: list[tuple[float, float]] = []
    for b0, b1 in sorted((max(0.0, a), min(horizon_h, b)) for a, b in blocked):
        if b1 <= s:
            continue
        if b0 > s:
            out.append((s, min(b0, horizon_h)))
        s = max(s, b1)
        if s >= horizon_h:
            break
    if s < horizon_h:
        out.append((s, horizon_h))
    return [(a, b) for a, b in out if b - a >= 1.0]


def _whole_hours_up(v) -> float:
    """changeover_cache.round_setup_hours: the model reserves whole hours,
    rounded UP (1.75 -> 2); the 1e-9 guard keeps float noise from adding an
    hour. Idempotent on already-rounded values."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f <= 0:
        return 0.0
    return float(math.ceil(f - 1e-9))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def model_start_floor(d: dict, *, allow_early_fill: bool = True,
                      early_fill_hours: int | None = None) -> int:
    """model_builder.effective_due_start for a DEMAND row, from the raw
    demand_plan.csv fields the loader reads: due_start = int(due_start_hour)
    (data_loader num_or_default; blank -> 0), earliest_start = ceil of the
    stock-policy earliest_start_hour (blank -> no floor).
      allow_early_fill False (--no-week1-in-week0, two-phase sub-solves):
                             max(due_start, earliest_start)
      early_fill_hours None (unbounded, the plant rule): earliest_start or 0
      early_fill_hours h:    max(0, due_start - h, earliest_start)"""
    ds_raw = _num(d.get("due_start_hour"))
    ds_m = int(ds_raw) if ds_raw is not None else 0
    es = _num(d.get("earliest_start_hour"))
    floor = 0 if es is None else max(0, int(math.ceil(es)))
    if not allow_early_fill:
        return max(ds_m, floor)
    if early_fill_hours is None:
        return floor
    return max(0, ds_m - int(early_fill_hours), floor)


def build_greedy_fill(
    demand: list[dict],
    rates: dict[tuple[str, str], float],
    setups: dict[str, dict[str, float]],
    line_segments: dict[str, list[tuple[float, float]]],
    line_ids: dict[str, int],
    initial_sku: dict[str, str],
    *,
    co_cost: dict[str, dict[str, float]] | None = None,
    min_run_hours: int = 4,
    min_run_pct: float = 0.5,
    horizon_h: float = 504.0,
    max_lines_per_order: int = 2,
    line_gates: dict[str, float] | None = None,
    initial_extra_hours: dict[str, float] | None = None,
    prebuild: bool = True,
    early_fill_hours: int | None = None,
    cip_windows: dict[str, list[tuple[float, float]]] | None = None,
    soft_demand: bool = True,
) -> tuple[list[dict], dict]:
    """Greedy fill. Returns (schedule_rows, summary).

    demand rows: order_id, sku, week_index, qty_target, lower_pct, upper_pct
    (or explicit qty_min/qty_max when the pct columns are blank),
    due_start_hour, due_end_hour, optional earliest_start_hour. Orders are
    placed earliest week first, largest first; per order the best lines are
    those with the CHEAPEST transition from their current tail SKU, then
    highest rate. Each placement takes the earliest start that satisfies the
    model's setup floors against every fill block already on the line (and
    the initial changeover when it would be the line's first block), and
    ends early enough to leave the setup owed to every block on its right.

    line_gates: per line, the availability gate (initial_states
    available_from_hour) the initial changeover is measured from. Missing
    -> the start of the line's first free segment (never earlier than the
    real gate when the segments come from free_segments, so conservative).
    initial_extra_hours: per line, the long-shutdown extra hours added to
    the initial changeover (0 unless the line's long_shutdown_flag is 1).

    prebuild / early_fill_hours / cip_windows — the PRE-BUILD pass (see the
    module header): prebuild False keeps the main pass only (the model's
    allow_week1_in_week0 off); early_fill_hours is Params.early_fill_hours
    (None = unbounded); cip_windows per line are the committed CIP windows a
    seg_b may resume behind (None -> no seg_a/seg_b pairs, only extensions
    and new rows).

    soft_demand — Params.soft_demand of the model the seed is built for
    (Scenario F: True). It sets the IN-WINDOW REMAINDER pass's run floor:
    min_run_hours alone under soft demand (the model's floor there), the
    main pass's share floor otherwise. The main pass always keeps the share
    floor.

    co_cost is the format-aware transition cost (from_sku -> to_sku ->
    weighted machine cost: FFS/topload expensive, TTP cheap — mirroring the
    solver's [changeover] weights). Ranking by it makes lines develop format
    identities: a topload SKU chains onto a topload tail instead of splitting
    an FFS campaign. Without it (None) the ranking falls back to setup hours,
    which is format-BLIND — a topload swap and a TTP swap both read "2h"
    (measured 2026-08-14: 67-72 topload changes survived the solver because
    the seed baked them in). Physical gap time always comes from `setups`.
    """
    gates = line_gates or {}
    extras = initial_extra_hours or {}
    cipw = cip_windows or {}
    lines: dict[str, _LineState] = {}
    for ln, segs in line_segments.items():
        segs = sorted(segs)
        init = str(initial_sku.get(ln, "") or "")
        gate = gates.get(ln)
        if gate is None:
            gate = segs[0][0] if segs else 0.0
        st = _LineState(line_id=line_ids.get(ln, 0),
                        segments=segs,
                        tail_sku=init,
                        gate=float(gate or 0.0),
                        init_sku=init,
                        init_extra=float(extras.get(ln, 0.0) or 0.0),
                        cip_windows=sorted(
                            (int(a), int(b)) for a, b in (cipw.get(ln) or [])
                            if int(b) > int(a)))
        lines[ln] = st

    def setup_h(frm: str, to: str) -> float:
        """The model's setup-TIME floor data.setup.get((frm, to), 0): a
        missing pair is 0 (same SKU included — the matrix has no diagonal
        rows; a row there would be honoured exactly like the model does)."""
        if not frm:
            return 0.0
        return _whole_hours_up((setups.get(frm) or {}).get(to, 0))

    def initial_setup_h(st: _LineState, to: str) -> float:
        """model_builder: eff = setup(init_sku, sku) [0 when CLEAN] +
        long_shutdown_extra [when flagged]; start_first >= gate + eff."""
        init = st.init_sku
        base = 0.0 if (not init or init == "CLEAN") else setup_h(init, to)
        return base + st.init_extra

    def trans_cost(frm: str, to: str) -> float:
        """Ranking cost of switching a line's tail from `frm` to `to`."""
        if not frm or frm == to:
            return 0.0
        if co_cost is not None:
            c = (co_cost.get(frm) or {}).get(to)
            if c is not None:
                return float(c)
            # pair missing from the standards: assume worse than any known
            # transition (F weights top out ~360 for ffs+extras) but not so
            # absurd that rate ordering vanishes among several unknowns
            return 500.0
        return setup_h(frm, to)

    rows: list[dict] = []
    row_ix: dict[tuple[str, str], list[int]] = {}   # (line, order) -> row indexes
    placed_kg_by_week: dict[int, float] = {}

    orders: list[_Order] = []
    for d in demand:
        target = float(d.get("qty_target", 0) or 0)
        if not target > 0:
            continue
        # Same precedence AND rounding as solver/data_loader._parse_demand:
        # pct bounds when both are present (qmin = floor(target x lo),
        # qmax = ceil(target x hi)), else explicit int(qty_min)/int(qty_max).
        # Netted / partly-covered F rows arrive with blank pct + explicit
        # bounds (demand_coverage C56, plan_fill), and `float(nan) or 0.9` is
        # nan (truthy) — so the old expression would have produced nan bounds
        # and placed nothing. The loader raises when neither pair is
        # complete; the seed stays permissive (target x 0.9 / 1.1).
        lo, hi = _num(d.get("lower_pct")), _num(d.get("upper_pct"))
        qmin_x, qmax_x = _num(d.get("qty_min")), _num(d.get("qty_max"))
        if lo is not None and hi is not None:
            qmin = int(math.floor(target * lo))
            qmax = int(math.ceil(target * hi))
        elif qmin_x is not None and qmax_x is not None:
            qmin, qmax = int(qmin_x), int(qmax_x)
        else:
            qmin = int(qmin_x) if qmin_x is not None else int(math.floor(target * 0.9))
            qmax = int(qmax_x) if qmax_x is not None else int(math.ceil(target * 1.1))
        # Due window in whole model hours, never wider than the model's:
        # start >= due_start, end <= due_end + 1 (blank due_end -> H - 1).
        ds_raw = _num(d.get("due_start_hour"))
        de_raw = _num(d.get("due_end_hour"))
        ds = float(math.ceil(ds_raw)) if ds_raw is not None else 0.0
        de = min(float(horizon_h),
                 float(math.floor(de_raw) if de_raw is not None
                       else horizon_h - 1) + 1.0)
        # Stock-policy floor (slice 3): the seed must not hand CP-SAT a hint
        # that starts an order before its receipt + buffer — the model
        # holds the same floor (rounded UP to a whole hour by the loader).
        es = _num(d.get("earliest_start_hour"))
        if es is not None:
            ds = max(ds, float(math.ceil(es)))
        wk = _num(d.get("week_index"))
        orders.append(_Order(
            d=d, oid=str(d["order_id"]), sku=str(d["sku"]),
            week=int(wk) if wk is not None else 0, target=target,
            qmin=qmin, qmax=qmax, ds=ds, de=de,
            model_floor=model_start_floor(
                d, allow_early_fill=bool(prebuild),
                early_fill_hours=early_fill_hours),
            remaining=target))
    orders.sort(key=lambda o: (o.week, -o.target))

    def _add_week_kg(start: float, end: float, kg: float) -> None:
        wk = int(((start + end) / 2) // 168)
        placed_kg_by_week[wk] = placed_kg_by_week.get(wk, 0.0) + kg

    def _place_in_window(o: _Order, *, share_floor: bool) -> tuple[int, float]:
        """Place order `o` inside its own due window: at most one row per
        line, never on a line the order already uses (the model holds ONE
        assignment per (line, order)), within max_lines_per_order.
        share_floor adds the seed's per-line share floor
        min_run_pct x qty_min / rate on top of min_run_hours.
        Returns (rows placed, kg placed)."""
        n_runs, kg_sum = 0, 0.0
        sku = o.sku
        ds, de, qmin, qmax = o.ds, o.de, o.qmin, o.qmax
        cands = [(ln, rates.get((ln, sku), 0.0)) for ln in lines]
        cands = [(ln, r) for ln, r in cands if r > 0]
        # cheapest tail transition first (format-aware when co_cost given:
        # same SKU = 0, same format cheap, topload/FFS swaps expensive),
        # then physical setup hours, then fastest line
        cands.sort(key=lambda t: (
            trans_cost(lines[t[0]].tail_sku, sku),
            setup_h(lines[t[0]].tail_sku, sku),
            -t[1]))

        for ln, rate in cands:
            if o.remaining <= 0 or len(o.lines) >= max_lines_per_order:
                break
            if ln in o.lines:
                continue
            st = lines[ln]
            ir = int(round(rate))    # the model's integer rate (prod == ir x run_h)
            floor_h = int(min_run_hours)
            if share_floor:
                floor_h = max(floor_h,
                              math.ceil(min_run_pct * qmin / rate) if rate > 0 else 0)
            for i_seg, (a, b) in enumerate(st.segments):
                if o.remaining <= 0:
                    break
                w0, w1 = max(a, ds), min(b, de)
                if w1 - w0 < 1.0:
                    continue
                # Free spans never overlap a placed block, so every block is
                # wholly LEFT (end <= a) or wholly RIGHT (start >= b) of it.
                left = [blk for blk in st.blocks if blk[1] <= a]
                right = [blk for blk in st.blocks if blk[0] >= b]
                if left:
                    # pairwise floor against EVERY earlier fill block
                    s_min = max([w0] + [e + setup_h(sk, sku) for _, e, sk, _ in left])
                else:
                    # would be the line's first block: initial changeover
                    s_min = max(w0, st.gate + initial_setup_h(st, sku))
                # leave the setup owed to EVERY later fill block
                e_max = min([w1] + [s - setup_h(sku, sk) for s, _, sk, _ in right])
                start = int(math.ceil(s_min - 1e-9))
                end_cap = int(math.floor(e_max + 1e-9))
                avail = end_cap - start
                need_h = math.ceil(o.remaining / rate - 1e-9)
                run = min(avail, need_h)
                # never place more kg than qmax allows, summed over the
                # order's lines at the model's integer rate
                if ir > 0:
                    run = min(run, (qmax - o.placed_model_kg) // ir)
                if run < floor_h or run < 1:
                    continue
                end = start + run
                kg = round(run * rate, 1)
                rows.append({
                    "line_id": st.line_id, "line_name": ln,
                    "order_id": o.oid, "sku": sku,
                    "start_hour": int(start), "end_hour": int(end),
                    "run_hours": int(run), "qty_kg": kg,
                })
                row_ix[(ln, o.oid)] = [len(rows) - 1]
                _add_week_kg(start, end, kg)
                o.remaining -= kg
                o.placed_model_kg += ir * run
                st.tail_sku = sku
                st.blocks.append((start, end, sku, o.oid))
                st.blocks.sort()
                # split the span around the placement; the setup gaps stay
                # free (a later block there must pass the same floors)
                pieces = []
                if start - a >= 1.0:
                    pieces.append((a, float(start)))
                if b - end >= 1.0:
                    pieces.append((float(end), b))
                st.segments = sorted(st.segments[:i_seg] + pieces
                                     + st.segments[i_seg + 1:])
                o.lines.append(ln)
                n_runs += 1
                kg_sum += kg
                break
        return n_runs, kg_sum

    # ── Pass 1: MAIN — inside each order's own due window ─────────────────
    for o in orders:
        _place_in_window(o, share_floor=True)

    # ── Pass 1b: IN-WINDOW REMAINDER — nearer weeks keep their own hours ──
    # Every order still below target (W0 included) gets one more try inside
    # its OWN due window, in the same week order, on lines it does not use
    # yet — BEFORE any later week may pre-build into those hours. Under soft
    # demand the model's only run floor is min_run_hours (model_builder:
    # min_run_from_pct = 0), so the seed's share floor is dropped here; it is
    # kept when soft demand is off. Without this pass the pre-build handed a
    # nearer week's window to a later week's order while the nearer order,
    # able to run there, stayed short (review 2026-09-16: P20 280103-W3 over
    # 280103-W2's week, W3 rows in P15's W0 hours).
    rem_runs, rem_kg = 0, 0.0
    for o in [o for o in orders if o.remaining > 0]:
        n, kg = _place_in_window(o, share_floor=not soft_demand)
        rem_runs += n
        rem_kg += kg

    # ── Pass 2: PRE-BUILD — time before due_start left free by pass 1 ─────
    pb = {"runs": 0, "extended": 0, "cip_pairs": 0, "new_rows": 0,
          "kg": 0.0, "kg_by_week_index": {}}
    line_order = {ln: i for i, ln in enumerate(lines)}

    def _tc(frm, to) -> float:
        """Transition ranking cost; CLEAN / nothing on either side = free."""
        if not frm or frm == "CLEAN" or not to or frm == to:
            return 0.0
        return trans_cost(frm, to)

    def _sh(frm, to) -> float:
        if not frm or frm == "CLEAN" or not to:
            return 0.0
        return setup_h(frm, to)

    def _start_floor(st: _LineState, a: float, sku: str, lo: int) -> float:
        """Earliest legal start in the free span beginning at `a`: the
        model's start floor, every left block's end + setup, or the gate +
        initial changeover when nothing is on the left."""
        left = [blk for blk in st.blocks if blk[1] <= a]
        if left:
            return max([a, float(lo)] + [e + setup_h(sk, sku) for _, e, sk, _ in left])
        return max(a, float(lo), st.gate + initial_setup_h(st, sku))

    def _prebuild_candidates(o: _Order, ln: str, st: _LineState) -> list[tuple]:
        rate = rates.get((ln, o.sku), 0.0)
        ir = int(round(rate)) if rate > 0 else 0
        if ir <= 0:
            return []
        want = min(math.ceil(o.remaining / rate - 1e-9),
                   (o.qmax - o.placed_model_kg) // ir)
        if want < 1:
            return []
        lo = o.model_floor
        ds_i = int(math.ceil(o.ds - 1e-9))     # a pre-build run starts < ds_i
        out: list[tuple] = []

        def _cand(kind, start, end, trans, setup_d):
            steps = (math.ceil((ds_i - start) / WEEK_STEP_H)
                     if start < ds_i else 0)
            key = (steps, round(trans, 6), setup_d, -end, -rate,
                   line_order[ln], kind)
            out.append((key, kind, ln, int(start), int(end), rate, ir))

        ix = row_ix.get((ln, o.oid))
        if ix:
            own = next(blk for blk in st.blocks if blk[3] == o.oid)
            s0 = own[0]
            # (a) EXTENSION: the free span ending exactly where the order's
            # first row starts; same SKU next door costs no setup.
            for a, b in st.segments:
                if abs(b - s0) < 1e-6:
                    start_lo = int(math.ceil(_start_floor(st, a, o.sku, lo) - 1e-9))
                    ext = min(s0 - start_lo, want)
                    if ext >= 1:
                        _cand("extend", s0 - ext, s0, 0.0, 0.0)
                    break
            # (b) seg_a in front of a committed CIP window, the row as seg_b:
            # nothing but free time / committed windows between them.
            if len(ix) == 1 and st.cip_windows:
                for a, b in reversed(st.segments):
                    if b > s0 + 1e-6:
                        continue
                    if any(blk[3] != o.oid and b - 1e-6 <= blk[0] < s0
                           for blk in st.blocks):
                        break
                    if not any(s_w >= b - 1e-6 and e_w <= s0 + 1e-6
                               for s_w, e_w in st.cip_windows):
                        continue
                    start_lo = int(math.ceil(_start_floor(st, a, o.sku, lo) - 1e-9))
                    end_hi = int(math.floor(b + 1e-9))
                    run = min(end_hi - start_lo, want)
                    if run < max(1, int(min_run_hours)):
                        continue
                    start = min(end_hi - run, ds_i - 1)
                    if start < start_lo:
                        continue
                    _cand("pair", start, start + run, 0.0, 0.0)
        elif len(o.lines) < max_lines_per_order:
            # (c) NEW row on a line the order does not use yet
            floor_h = max(int(min_run_hours), 1,
                          math.ceil(min_run_pct * o.qmin / rate))
            for a, b in st.segments:
                if a >= ds_i:
                    break
                left = [blk for blk in st.blocks if blk[1] <= a]
                right = [blk for blk in st.blocks if blk[0] >= b]
                start_lo = int(math.ceil(_start_floor(st, a, o.sku, lo) - 1e-9))
                e_max = min([b, o.de] + [s - setup_h(o.sku, sk) for s, _, sk, _ in right])
                end_hi = int(math.floor(e_max + 1e-9))
                run = min(end_hi - start_lo, want)
                if run < floor_h:
                    continue
                start = min(end_hi - run, ds_i - 1)
                if start < start_lo:
                    continue
                l_sku = max(left, key=lambda blk: blk[1])[2] if left else st.init_sku
                r_sku = min(right)[2] if right else ""
                trans = (_tc(l_sku, o.sku) + _tc(o.sku, r_sku) - _tc(l_sku, r_sku))
                setup_d = (_sh(l_sku, o.sku) + _sh(o.sku, r_sku) - _sh(l_sku, r_sku))
                _cand("new", start, start + run, trans, setup_d)
        return out

    def _clip_segments(st: _LineState, c0: float, c1: float) -> None:
        """Remove [c0, c1) from the line's free spans (pieces < 1 h drop)."""
        segs = []
        for a, b in st.segments:
            if b <= c0 or a >= c1:
                segs.append((a, b))
                continue
            if c0 - a >= 1.0:
                segs.append((a, float(c0)))
            if b - c1 >= 1.0:
                segs.append((float(c1), b))
        st.segments = sorted(segs)

    if prebuild:
        todo = [o for o in orders if o.remaining > 0 and o.model_floor < o.ds]
        todo.sort(key=lambda o: (o.week, -o.remaining))
        for o in todo:
            while o.remaining > 0:
                best = None
                for ln, st in lines.items():
                    for c in _prebuild_candidates(o, ln, st):
                        if best is None or c[0] < best[0]:
                            best = c
                if best is None:
                    break
                _, kind, ln, start, end, rate, ir = best
                st = lines[ln]
                hours = end - start
                if kind == "extend":
                    i_row = min(row_ix[(ln, o.oid)],
                                key=lambda i: rows[i]["start_hour"])
                    row = rows[i_row]
                    old_kg = row["qty_kg"]
                    row["start_hour"] = start
                    row["run_hours"] = int(row["run_hours"] + hours)
                    row["qty_kg"] = round(row["run_hours"] * rate, 1)
                    kg = row["qty_kg"] - old_kg
                    pb["extended"] += 1
                else:
                    kg = round(hours * rate, 1)
                    rows.append({
                        "line_id": st.line_id, "line_name": ln,
                        "order_id": o.oid, "sku": o.sku,
                        "start_hour": start, "end_hour": end,
                        "run_hours": hours, "qty_kg": kg,
                    })
                    row_ix.setdefault((ln, o.oid), []).append(len(rows) - 1)
                    pb["cip_pairs" if kind == "pair" else "new_rows"] += 1
                if kind == "new":
                    st.blocks.append((start, end, o.sku, o.oid))
                    _clip_segments(st, start, end)
                    o.lines.append(ln)
                else:
                    # the order's span now opens at `start`; for a pair the
                    # hours up to the old row are the span's own (no other
                    # order may sit between seg_a and seg_b)
                    own = next(blk for blk in st.blocks if blk[3] == o.oid)
                    st.blocks.remove(own)
                    st.blocks.append((start, own[1], own[2], own[3]))
                    _clip_segments(st, start, own[0])
                st.blocks.sort()
                o.remaining -= kg
                o.placed_model_kg += ir * hours
                _add_week_kg(start, end, kg)
                pb["runs"] += 1
                pb["kg"] += kg
                pb["kg_by_week_index"][o.week] = (
                    pb["kg_by_week_index"].get(o.week, 0.0) + kg)

    short_orders = [f"{o.oid} short {o.remaining:,.0f} kg of target"
                    for o in orders
                    if o.remaining > max(0.0, o.target - o.qmin)]
    summary = {
        "rows": len(rows),
        "kg_by_week": {k: round(v) for k, v in sorted(placed_kg_by_week.items())},
        "orders_short": short_orders,
        "remainder": {"runs": rem_runs, "kg": round(rem_kg)},
        "prebuild": {
            "enabled": bool(prebuild),
            "early_fill_hours": early_fill_hours,
            "runs": pb["runs"], "extended": pb["extended"],
            "cip_pairs": pb["cip_pairs"], "new_rows": pb["new_rows"],
            "kg": round(pb["kg"]),
            "kg_by_week_index": {k: round(v) for k, v in
                                 sorted(pb["kg_by_week_index"].items())},
        },
    }
    return rows, summary
