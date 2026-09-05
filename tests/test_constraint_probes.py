# tests/test_constraint_probes.py -- charter section 5 constraint probes P1..P9.
#
# P2 dispatch 4. The charter's section-5 table lists every constraint the solver
# claims to enforce. Rows 4 (NoOverlap) and 5 (CIP spacing) were already covered
# by contract tests C3/C6 in tests/test_solver_contracts.py; rows 1, 2, 3, 6, 7,
# 8, 9 had no automated probe at all. This module gives every row a probe that
# asserts the constraint on a REAL solve output, so a future solver change that
# silently violates one turns the suite red.
#
# Probe -> charter row
#   P1  row 1  min run time         block duration and per-(line,order) run hours
#   P2  row 2  batch size           qty_min <= produced <= qty_max
#   P3  row 3  capability           scheduled (line, sku) is capable
#   P4  row 4  NoOverlap            SEE C3 in tests/test_solver_contracts.py --
#                                   deliberately no probe here (no duplication)
#   P5  row 5  CIP max deadline     hard compliance deadline on every CIP start
#   P6  row 6  changeovers          gaps respect changeovers.csv setup_hours
#   P7  row 7  max lines per order  distinct lines per order <= max_lines_per_order
#   P8  row 8  due dates            due-window start floor and due+1 end cap
#   P9  row 9  demand total         no phantom tonnage per SKU
#
# Design notes (same shape as tests/test_solver_contracts.py)
#   * Since 2026-09-03 (fix T-1, audit tests-1) these assert on the
#     deterministic fixture data/test_fixtures/solver_tiny/ solved once per
#     session by tests/conftest.py (one ~2 s CP-SAT solve, 2 workers); the
#     live work dir under data/_scenario_work/ is an opt-in extra
#     (FLOWSTATE_LIVE_WORK). Before that every probe skipped on a clean
#     checkout and otherwise judged "whatever solved last".
#   * Config is read from the WORK DIR's flowstate.toml -- the config the solve
#     actually used -- never hard-coded.
#   * Several section-5 constraints are RELAX-LEVEL DEPENDENT. The auto-relax
#     ladder (code/solver/phase2_scheduler.py:355-366) is
#         0 hard | 1 relax_demand | 2 +soft_due | 3 +ignore_co
#     and cross-week mode replaces the hard due window with a weighted
#     preference (model_builder.py:136-169). A probe that asserted the level-0
#     contract against a level-3 artifact would be asserting something the
#     solver never promised, so each probe reads relax_mode / cross_week from
#     feasibility_report.json and asserts the contract that was actually in
#     force, skipping loudly (never silently) when the constraint was relaxed.
#     Row-by-row coverage under relaxation is reported in the dispatch summary.

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SOLVER_DIR = ROOT / "code" / "solver"
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))
from conftest import resolve_solver_work, solver_work_params  # noqa: E402

WORK_ROOT = ROOT / "data" / "_scenario_work"
TOL = 1e-6

# Plant rule for the early-fill window (charter row 8). Until 2026-09-03 this
# file MIRRORED the model's own Monday-anchor constants (due_start > 167 ->
# window opens at 120), so P1b/P8 could only agree with the model, never
# catch it; fix T restated the rule then in force (only the SECOND demand
# week, 48 h early). PLANT DECISION 2026-09-04 (the user): "early fill can go
# as far into the previous weeks as needed, except cannot be placed before an
# already scheduled MO". Stated here from that decision and the work-dir
# toml -- nothing is imported from the solver:
#   * allow_week1_in_week0 off, current MOs, trials -> the order's own due_start;
#   * [scheduler] early_fill_hours absent / "unbounded" / "none" -> the due
#     window contributes NO start floor (0): only the line's gate, committed
#     windows and committed MOs bound the start (C1 / C2 / P8c);
#   * early_fill_hours = h -> every demand order may open h hours before its
#     own due_start (48 = the pre-decision value, no longer second-week-only).
# The due END (due_end + 1) stays hard (P8b). Producing in the right week is
# a SOFT preference (week gradient + week_deviation_weight), not probed here.
EARLY_FILL_HOURS = 48   # documented legacy value only (see above)


def _early_fill_hours(raw) -> int | None:
    """Parse the toml value the way the plant states it: absent / "unbounded"
    / "none" -> None (no floor); a number -> whole hours."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "unbounded", "none", "unlimited", "inf"):
            return None
        return int(round(float(s)))
    return int(round(float(raw)))


def _second_week_start(orders: dict) -> int | None:
    """The second distinct due_start among the DEMAND orders (current MOs
    excluded -- their windows are plant fact), or None with a single week.
    LEGACY since 2026-09-04 (the floor no longer privileges the second week);
    kept for the frame tests in tests/test_fix_T.py."""
    starts = sorted({int(o["due_start"]) for o in orders.values() if not o.get("current_mo")})
    return starts[1] if len(starts) > 1 else None


def _window_open(o: dict, orders: dict, cfg: dict) -> int:
    """Hour at which `o` may start producing (the due-window floor, hard at
    every relax level). `cfg["early_fill_hours"]` absent -> unbounded (the
    plant decision's default); `orders` is kept for signature compatibility."""
    ds_raw = int(o["due_start"])
    if o.get("current_mo") or not cfg["allow_week1_in_week0"]:
        return ds_raw
    efh = cfg.get("early_fill_hours")
    if efh is None:
        return 0
    return max(0, ds_raw - int(efh))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _solved_work_dirs() -> list[Path]:
    if not WORK_ROOT.exists():
        return []
    return sorted(
        (d for d in WORK_ROOT.iterdir()
         if d.is_dir() and (d / "schedule_phase2.csv").exists()),
        key=lambda d: (d / "schedule_phase2.csv").stat().st_mtime,
        reverse=True,
    )


def _read(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:  # noqa: BLE001
        return None
    return df if not df.empty else None


def _norm(v) -> str:
    return str(v).strip().upper()


def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(f) else f


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module", params=solver_work_params())
def work(request, solver_tiny_work: Path) -> Path:
    """The artifact under test -- see tests/test_solver_contracts.py::work.

    Fix T-1 (audit tests-1, 2026-09-03): the deterministic fixture
    data/test_fixtures/solver_tiny/ solved once per session by conftest.py
    (relax level 0, hard demand, current MO, cip_req pair, downtime, committed
    CIP window), plus the live work dir only when FLOWSTATE_LIVE_WORK is set.
    """
    return resolve_solver_work(request.param, solver_tiny_work)


@pytest.fixture(scope="module")
def cfg(work: Path) -> dict:
    """[scheduler] / [cip] settings as the solve saw them (work-dir toml)."""
    toml_path = work / "flowstate.toml"
    if not toml_path.exists():
        pytest.skip(f"{work.name}: no flowstate.toml in the work dir")
    import tomllib

    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    sch = raw.get("scheduler", {})
    cip = raw.get("cip", {})
    horizon = _num(sch.get("horizon_hours")) or _num(sch.get("horizon_weeks")) * 168
    return {
        "horizon_h": horizon,
        # data_loader.Params defaults apply when the key is absent.
        "min_run_hours": int(_num(sch.get("min_run_hours"), 4)),
        "min_run_pct_of_qty": _num(sch.get("min_run_pct_of_qty"), 0.5),
        "max_lines_per_order": int(_num(sch.get("max_lines_per_order"), 3)),
        "allow_week1_in_week0": bool(sch.get("allow_week1_in_week0", True)),
        # plant decision 2026-09-04: absent = unbounded early fill
        "early_fill_hours": _early_fill_hours(sch.get("early_fill_hours")),
        # absent = hard due weeks (the shipped default since 2026-09-04 night;
        # "soft" is the opt-in of plant decision #2: a late finish inside the
        # horizon becomes a priced trade-off instead of a wall)
        "due_week_policy": str(sch.get("due_week_policy", "hard") or "hard").strip().lower(),
        "use_sku_rates": bool(sch.get("use_sku_rates", False)),
        "planning_start_date": str(sch.get("planning_start_date", "")),
        "cip_interval_h": int(_num(cip.get("interval_h"), 120)),
    }


@pytest.fixture(scope="module")
def relax(work: Path) -> dict:
    """Which section-5 constraints were actually in force for this solve.

    relax_mode strings come from phase2_scheduler.RELAX_LABELS; level 0 is
    "hard" and carries none of the substrings below.
    """
    rep = work / "feasibility_report.json"
    if not rep.exists():
        pytest.skip(f"{work.name}: no feasibility_report.json -- relax level unknown")
    try:
        data = json.loads(rep.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pytest.skip(f"{work.name}: unreadable feasibility_report.json")
    mode = str(data.get("relax_mode", ""))
    soft = data.get("soft_demand")
    if soft is None:
        # Older artifacts predate the report stamp - the work dir's own
        # flowstate.toml carries the flag the solve actually ran with.
        soft = False
        toml_p = work / "flowstate.toml"
        if toml_p.exists():
            try:
                import tomllib
                soft = bool(tomllib.loads(
                    toml_p.read_text(encoding="utf-8"))
                    .get("scheduler", {}).get("soft_demand", False))
            except Exception:  # noqa: BLE001
                soft = False
    return {
        "level": data.get("relax_level"),
        "mode": mode,
        "soft_demand": bool(soft),
        "relax_demand": "relax_demand" in mode,
        "relax_due": "soft_due" in mode,
        "ignore_co": "ignore_co" in mode,
        "cross_week": bool(data.get("cross_week", False)),
    }


@pytest.fixture(scope="module")
def schedule(work: Path) -> pd.DataFrame:
    """schedule_phase2.csv, one row per production BLOCK (seg_a / seg_b)."""
    df = _read(work / "schedule_phase2.csv")
    if df is None:
        pytest.skip(f"{work.name}: empty schedule_phase2.csv")
    for col in ("start_hour", "end_hour", "run_hours"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["start_hour", "end_hour"])
    df["line_id"] = pd.to_numeric(df["line_id"], errors="coerce").fillna(-1).astype(int)
    df["sku"] = df["sku"].astype(str)
    df["order_id"] = df["order_id"].astype(str)
    trials = df.get("is_trial")
    df["trial"] = (trials.astype(str).str.lower() == "true") if trials is not None else False
    # data_loader._parse_current_mo mints order ids as "<mo>|CUR".
    df["current_mo"] = df["order_id"].str.endswith("|CUR")
    df["duration"] = df["end_hour"] - df["start_hour"]
    return df


@pytest.fixture(scope="module")
def orders(work: Path) -> dict:
    """Solver orders rebuilt from the WORK-DIR inputs (never from the solver).

    Mirrors data_loader._parse_demand (pct bounds -> floor/ceil) and
    _parse_current_mo (remaining_kg is both floor and ceiling).
    """
    out: dict[str, dict] = {}
    dem = _read(work / "demand_plan.csv")
    if dem is None:
        pytest.skip(f"{work.name}: no demand_plan.csv in the work dir")
    for _, r in dem.iterrows():
        target = _num(r.get("qty_target"))
        lower, upper = r.get("lower_pct"), r.get("upper_pct")
        if pd.notna(lower) and pd.notna(upper):
            qmin = int(math.floor(target * float(lower)))
            qmax = int(math.ceil(target * float(upper)))
        else:
            qmin = int(_num(r.get("qty_min")))
            qmax = int(_num(r.get("qty_max")))
        out[str(r["order_id"])] = dict(
            sku=str(r["sku"]),
            qty_min=qmin,
            qty_max=qmax,
            due_start=int(_num(r.get("due_start_hour"))),
            due_end=int(_num(r.get("due_end_hour"), 335)),
            current_mo=False,
        )
    cmo = _read(work / "current_mo.csv")
    if cmo is not None:
        # Rate of the MO's locked line for the C61 bracket below. Rebuilt
        # from the work-dir inputs (never from the solver): per-SKU
        # capabilities rates when use_sku_rates is on, else the flat
        # line_rates.csv value for the line.
        caps_df = _read(work / "capabilities_rates.csv")
        line_rate: dict[tuple[str, str], float] = {}
        if caps_df is not None:
            rcol = next((c for c in ("calc_rate_kgph", "rate_kgph", "rate_uph")
                         if c in caps_df.columns), None)
            if rcol is not None:
                for cr in caps_df.itertuples():
                    line_rate[(_norm(cr.line_name), str(cr.sku))] = _num(getattr(cr, rcol))
        flat: dict[str, float] = {}
        lr_df = _read(work / "line_rates.csv")
        if lr_df is not None and "rate_kgph" in lr_df.columns:
            name_col = "Line" if "Line" in lr_df.columns else "line_name"
            for lr in lr_df.itertuples():
                flat[_norm(getattr(lr, name_col))] = _num(lr.rate_kgph)
        toml_p = work / "flowstate.toml"
        use_sku_rates = False
        if toml_p.exists():
            import tomllib
            use_sku_rates = bool(tomllib.loads(toml_p.read_text(encoding="utf-8"))
                                 .get("scheduler", {}).get("use_sku_rates", False))
        for _, r in cmo.iterrows():
            remaining = _num(r.get("remaining_kg"))
            if remaining <= 0:
                continue  # fully produced -- data_loader drops it too
            # Fix SA-5 / audit C61 (2026-09-03): the loader brackets the
            # remaining kg at WHOLE HOURS of the locked line's rounded rate,
            # qty_min = ir*floor(rem/ir), qty_max = ir*ceil(rem/ir) (>= 1 h),
            # because produced = round(rate) x integer hours can equal the raw
            # remaining kg only when it is a whole multiple of the rate
            # (23,000 @ 737 made level 0 structurally INFEASIBLE). The old
            # mirror here (qty_min == qty_max == remaining) pinned that
            # defect (audit tests-7) and would flag the honest 13,000 kg
            # bracket of a 12,500 kg MO @ 1000 kg/h as phantom tonnage in P9.
            # Hand check: 12,500 / 1000 -> floor 12, ceil 13 -> 12,000..13,000.
            ln = _norm(r.get("line_name"))
            sku = str(r["sku"]).strip()
            rate = (line_rate.get((ln, sku), 0.0) if use_sku_rates
                    else flat.get(ln, line_rate.get((ln, sku), 0.0)))
            ir = int(round(rate))
            if ir > 0:
                n_lo = max(1, int(math.floor(remaining / ir)))
                n_hi = max(n_lo, int(math.ceil(remaining / ir)))
                qmin, qmax = ir * n_lo, ir * n_hi
            else:
                qmin = qmax = int(round(remaining))
            out[f"{str(r['mo']).strip()}|CUR"] = dict(
                sku=sku,
                qty_min=qmin,
                qty_max=qmax,
                due_start=int(_num(r.get("due_start_h"))),
                due_end=int(_num(r.get("due_end_h"), 335)),
                current_mo=True,
            )
    return out


@pytest.fixture(scope="module")
def capabilities(work: Path) -> pd.DataFrame:
    caps = _read(work / "capabilities_rates.csv")
    if caps is None:
        pytest.skip(f"{work.name}: no capabilities_rates.csv in the work dir")
    caps["line_id"] = pd.to_numeric(caps["line_id"], errors="coerce").fillna(-1).astype(int)
    caps["sku"] = caps["sku"].astype(str)
    caps["capable"] = pd.to_numeric(caps.get("capable", 0), errors="coerce").fillna(0).astype(int)
    return caps


@pytest.fixture(scope="module")
def rates(work: Path, cfg: dict, capabilities: pd.DataFrame) -> dict | None:
    """(line_id, sku) -> kg/h, or None when the rate basis is ambiguous.

    data_loader.Data.load overrides the per-SKU rates with the flat monthly
    line_rates.csv unless use_sku_rates is set. When that override could have
    applied we return None so rate-dependent probes skip instead of asserting
    against the wrong basis.
    """
    if not cfg["use_sku_rates"] and (work / "line_rates.csv").exists():
        return None
    col = next(
        (c for c in ("calc_rate_kgph", "rate_kgph", "rate_uph")
         if c in capabilities.columns), None)
    if col is None:
        return None
    vals = pd.to_numeric(capabilities[col], errors="coerce").fillna(0.0)
    return {
        (int(r.line_id), str(r.sku)): float(v)
        for r, v in zip(capabilities.itertuples(), vals)
    }


def _min_run_hours(o: dict, rate: float, cfg: dict, orders: dict | None = None) -> int:
    """Replicate the model's min-run floor for one (line, order) pair.

    Deliberately NOT imported from the solver (spec: replicate, do not import).
    The window opens at _window_open (the plant's early-fill policy, see the
    top of this file); max_len = min(H, due_end + 1) - that hour.
    """
    horizon = cfg["horizon_h"]
    ds_eff = _window_open(o, orders or {}, cfg)
    max_len = max(0, min(horizon, o["due_end"] + 1) - max(0, ds_eff))
    floor_h = cfg["min_run_hours"]
    if o["current_mo"]:
        # model_builder.py:257-... -- qty_min == qty_max == remaining, so the
        # floor applies only when the remaining kg supports it
        # (remaining/rate >= floor_h); otherwise no floor (nearly-done MO).
        # rate <= 0 -> cannot verify the qty cap -> treat as no floor.
        max_hours_qty = (o["qty_max"] / rate) if rate > 0 else 0.0
        if max_hours_qty >= floor_h:
            return int(min(max_len, max(1, floor_h)))
        return 0
    pct = 0
    if rate > 0:
        pct = math.ceil(cfg["min_run_pct_of_qty"] * o["qty_min"] / rate)
    return int(min(max_len, max(1, floor_h, pct)))


# --------------------------------------------------------------------------
# P1 -- charter row 1: minimum run time
# --------------------------------------------------------------------------
def test_p1_every_production_block_meets_the_min_run_floor(schedule, cfg):
    """model_builder.py:269-274 -- seg_a_run and seg_b_run are each floored at
    [scheduler] min_run_hours, so no BLOCK may be shorter than that.

    Current-state MOs are checked separately in P1c, which states the same
    floor against the current-MO branch of the run-bound block.
    """
    floor_h = cfg["min_run_hours"]
    prod = schedule[~schedule["trial"] & ~schedule["current_mo"]]
    short = prod[prod["duration"] < floor_h - TOL]
    violations = [
        f"{r.line_name} {r.order_id} {r.start_hour:g}-{r.end_hour:g}h "
        f"= {r.duration:g}h < min_run_hours {floor_h}h"
        for r in short.itertuples()
    ]
    assert not violations, (
        f"{len(violations)} production block(s) below the min-run floor: "
        + "; ".join(violations[:10])
    )


def test_p1b_per_line_order_run_hours_meet_the_replicated_min_run(
    schedule, cfg, orders, rates
):
    """model_builder.py:266 -- run_h (seg_a + seg_b on ONE line) is floored at
    min(max_len, max(1, min_run_hours, ceil(min_run_pct_of_qty * qty_min / rate))).
    """
    if rates is None:
        pytest.skip("rate basis ambiguous (line_rates.csv present, use_sku_rates off)")
    prod = schedule[~schedule["trial"] & ~schedule["current_mo"]]
    unknown, violations = [], []
    for (line_id, order_id), grp in prod.groupby(["line_id", "order_id"]):
        o = orders.get(order_id)
        if o is None:
            unknown.append(order_id)
            continue
        rate = rates.get((int(line_id), o["sku"]), 0.0)
        floor_h = _min_run_hours(o, rate, cfg, orders)
        total = float(grp["run_hours"].sum())
        if total < floor_h - TOL:
            violations.append(
                f"{grp.iloc[0]['line_name']} {order_id}: {total:g}h run "
                f"< min_run {floor_h}h (qty_min={o['qty_min']}, rate={rate:g})"
            )
    assert not unknown, (
        "schedule contains order_id(s) absent from demand_plan.csv / "
        f"current_mo.csv: {sorted(set(unknown))[:10]}"
    )
    assert not violations, (
        f"{len(violations)} (line, order) pair(s) below the min-run floor: "
        + "; ".join(violations[:10])
    )


def test_p1c_current_mo_blocks_meet_the_min_run_floor(schedule, cfg, orders, rates):
    """model_builder.py:236-247 -- a current-state MO on its locked line has
    presence forced and then FALLS THROUGH to the run-bound block, so the
    current-MO floor applies to committed work.

    This was dispatch-4 finding 1: the old code `continue`d out of the
    run-bound block, leaving the floor unreachable and letting committed work
    be chopped into 1-3 h stubs. Fixed in dispatch 5 (user-approved, 4 h
    floor). The contract: an MO whose remaining kg supports a full floor
    (remaining/rate >= min_run_hours) must run at least that many hours in
    TOTAL. Nearly-done MOs have no floor by design, and individual segments
    may be shorter (forced CIP splits) -- the order TOTAL is the contract.
    """
    floor_h = cfg["min_run_hours"]
    cur = schedule[schedule["current_mo"] & ~schedule["trial"]]
    if cur.empty:
        pytest.skip("no current-state MOs in this work dir (not a scenario-E solve)")

    total_violations = []
    for (line_id, order_id), grp in cur.groupby(["line_id", "order_id"]):
        o = orders.get(order_id)
        if o is None:
            continue
        rate = rates.get((line_id, o["sku"])) if rates else None
        if rate is None:
            continue  # rate basis ambiguous -- cannot verify the qty cap
        want = _min_run_hours(o, rate, cfg, orders)
        total = float(grp["run_hours"].sum())
        if total < want - TOL:
            total_violations.append(
                f"{grp.iloc[0]['line_name']} {order_id}: {total:g}h run < "
                f"{want}h floor (rate {rate:g})"
            )
    assert not total_violations, (
        f"current-state MOs below their min-run floor -- "
        f"{len(total_violations)} short (line, order) total(s). "
        "The floor is posted by the run-bound block at model_builder.py:257-286; "
        "nearly-done MOs (remaining < floor_h x rate) have no floor by design. "
        + "; ".join(total_violations[:12])
    )


# --------------------------------------------------------------------------
# P2 -- charter row 2: batch size (qty_min <= produced <= qty_max)
# --------------------------------------------------------------------------
def test_p2_produced_stays_within_the_demand_bounds(work, relax):
    """model_builder.py:321-324. `prod <= qty_max` is HARD at every relax level;
    `prod >= qty_min` becomes 0 when relax_demand is on (ladder level 1+).

    Reuses code/solver/validate_schedule.check_produced_vs_bounds rather than
    re-implementing the comparison (dispatch spec rule 3).
    """
    if str(SOLVER_DIR) not in sys.path:
        sys.path.insert(0, str(SOLVER_DIR))
    try:
        from validate_schedule import check_produced_vs_bounds  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot import validate_schedule: {exc}")

    issues = check_produced_vs_bounds(
        work / "produced_vs_bounds.csv", work / "demand_plan.csv"
    )
    missing = [i for i in issues if i.startswith("MISSING")]
    if missing:
        pytest.skip("; ".join(missing))

    over = [i for i in issues if i.startswith("OVER:")]
    assert not over, (
        f"{len(over)} order(s) produced MORE than qty_max -- this bound is hard "
        "at every relax level: " + "; ".join(over[:10])
    )
    not_reported = [i for i in issues if i.startswith("BOUNDS:") and "missing" in i]
    assert not not_reported, "; ".join(not_reported)

    under = [i for i in issues if i.startswith("UNDER:")]
    if relax.get("soft_demand"):
        pytest.skip(
            f"soft-demand artifact (Scenario F): {len(under)} order(s) short "
            "of qty_min BY CONTRACT (every short kg is penalized in the "
            "objective and reported) -- upper bound and reporting "
            "completeness asserted above"
        )
    if relax["relax_demand"]:
        pytest.skip(
            f"qty_min floor relaxed by relax level {relax['level']} "
            f"({relax['mode']}): {len(under)} order(s) short of qty_min -- "
            "upper bound and reporting completeness asserted above"
        )
    assert not under, (
        f"{len(under)} order(s) produced LESS than qty_min at relax level "
        f"{relax['level']} ({relax['mode']}): " + "; ".join(under[:10])
    )


# --------------------------------------------------------------------------
# P3 -- charter row 3: capability
# --------------------------------------------------------------------------
def test_p3_every_scheduled_line_sku_pair_is_capable(schedule, capabilities):
    """model_builder.py:257-261 -- a (line, sku) pair with capable == 0, no rate
    or a non-positive rate is forced present == 0.

    Trials are excluded: they are PINNED to their line (model_builder.py:197-227)
    and skip the capability branch entirely. Current-state MOs are INCLUDED even
    though the gate deliberately does not apply to them (manprg is ground truth,
    so a committed MO is never zeroed by the capabilities table) -- running a
    committed MO on an incapable line is still a real data defect, so the probe
    covers the hole the solver leaves open by design.
    """
    capable = {
        (int(r.line_id), str(r.sku)): int(r.capable)
        for r in capabilities.itertuples()
    }
    prod = schedule[~schedule["trial"]]
    violations = []
    for r in prod.itertuples():
        flag = capable.get((int(r.line_id), r.sku))
        if flag != 1:
            violations.append(
                f"{r.line_name} sku {r.sku} ({r.order_id}) capable="
                + ("absent" if flag is None else str(flag))
            )
    assert not violations, (
        f"{len(violations)} block(s) on a non-capable (line, sku) pair: "
        + "; ".join(sorted(set(violations))[:10])
    )


# --------------------------------------------------------------------------
# P4 -- charter row 4: NoOverlap
# --------------------------------------------------------------------------
# No probe here on purpose. Row 4 is fully covered by contract C3
# (tests/test_solver_contracts.py::test_c3_no_overlapping_blocks_on_a_line),
# which already asserts that production and CIP blocks never collide on a line.
# Duplicating it here would give two failures for one defect.


# --------------------------------------------------------------------------
# P5 -- charter row 5: CIP max-interval deadline (food safety / compliance)
# --------------------------------------------------------------------------
def test_p5_cip_starts_respect_the_hard_max_interval_deadline(work, cfg, schedule):
    """model_builder.py CIP section -- the HARD, never-relaxed deadline.

    Three parts, all keyed on the line's max_cip_hrs from line_cip_hrs.csv
    (data/reference/cip_info.csv MaxHoursBetweenCIP is the upstream source of
    that column; PreviousCIP arrives in the work dir as the initial_states
    carryover / last_cip_end_datetime pair the solver actually reads):

      a. PLANT RULE (fix SB-3 / audit C43, 2026-09-03): the clean is due
         `interval` WALL-CLOCK hours after the previous one ended, i.e. at
         horizon hour (interval - carryover) -- committed and idle hours
         count. Until 2026-09-03 this probe mirrored the model's own
         deadline `available_from + (interval - carryover)`, which let the
         first CIP slide by the whole availability gate (live E-mode lines
         15-160 h late vs cip_info) and therefore proved nothing about the
         plant rule. The one documented exception: when that hour is
         already behind the line's gate the model pins the clean AT the gate
         (as early as its downtime rows allow) and emits a warning, so the
         probe accepts max(deadline, gate) there.
      b. when initial_states carries an absolute last_cip_end_datetime (a
         clean BEFORE the anchor is a valid, negative hour), CIP 1 also
         starts by that hour + interval.
      c. CIP k starts by CIP k-1's end + interval. C6 in
         test_solver_contracts.py covers this gap too; kept here so P5 reads
         as one complete row-5 statement.

    Plus the operational tail: production may not run more than `interval`
    hours past the last CIP on the line. That one is IMPLIED by the clock-span
    CIP trigger (model_builder.py:763-778) rather than posted directly, so a
    failure here is a genuine compliance finding, not a stale assertion.
    """
    cip = _read(work / "cip_windows.csv")
    lim = _read(work / "line_cip_hrs.csv")
    init = _read(work / "initial_states.csv")
    if cip is None or lim is None or init is None:
        pytest.skip(f"{work.name}: no cip_windows / line_cip_hrs / initial_states")
    for col in ("start_hour", "end_hour"):
        cip[col] = pd.to_numeric(cip[col], errors="coerce")
    cip = cip.dropna(subset=["start_hour", "end_hour"])

    intervals = {
        _norm(r["line_name"]): _num(r.get("max_cip_hrs"), cfg["cip_interval_h"])
        for _, r in lim.iterrows()
    }
    gates, carries, last_cip = {}, {}, {}
    anchor = None
    if cfg["planning_start_date"]:
        try:
            anchor = datetime.strptime(cfg["planning_start_date"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            anchor = None
    for _, r in init.iterrows():
        line = _norm(r["line_name"])
        gates[line] = _num(r.get("available_from_hour"))
        carries[line] = _num(r.get("carryover_run_hours_since_last_cip_at_t0"))
        raw = str(r.get("last_cip_end_datetime", "") or "").strip()
        if anchor is not None and raw and raw.lower() not in ("nan", "null"):
            try:
                hour = (datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                        - anchor).total_seconds() / 3600.0
            except ValueError:
                hour = None
            if hour is not None and hour < cfg["horizon_h"]:
                last_cip[line] = hour   # negative = clean before the anchor

    last_prod = {
        line: float(g["end_hour"].max())
        for line, g in schedule.groupby(schedule["line_name"].map(_norm))
    }

    violations = []
    for line, grp in cip.groupby(cip["line_name"].map(_norm)):
        interval = intervals.get(line, cfg["cip_interval_h"])
        if interval <= 0:
            continue
        windows = grp.sort_values("start_hour")[["start_hour", "end_hour"]]
        windows = [(float(s), float(e)) for s, e in windows.itertuples(index=False)]
        if not windows:
            continue

        # (a) first CIP by the plant's wall clock: interval - carryover hours
        #     into the horizon; a line already past that at its gate gets
        #     the clean at the gate (model warning "already past its CIP
        #     interval at the gate").
        due = interval - carries.get(line, 0.0)
        deadline = max(due, gates.get(line, 0.0))
        if windows[0][0] > deadline + TOL:
            violations.append(
                f"{line}: first CIP starts {windows[0][0]:g}h, due "
                f"{due:g}h (interval {interval:g} - carry "
                f"{carries.get(line, 0.0):g}; gate {gates.get(line, 0.0):g})"
            )
        # (b) absolute cross-phase deadline from the previous CIP's clock time
        if line in last_cip and windows[0][0] > last_cip[line] + interval + TOL:
            violations.append(
                f"{line}: first CIP starts {windows[0][0]:g}h but last CIP ended "
                f"{last_cip[line]:g}h -- absolute deadline {last_cip[line] + interval:g}h"
            )
        # (c) each later CIP within one interval of the previous CIP's end
        for (_, prev_end), (nxt_start, _) in zip(windows, windows[1:]):
            if nxt_start > prev_end + interval + TOL:
                violations.append(
                    f"{line}: {nxt_start - prev_end:g}h between CIPs "
                    f"(max {interval:g}h) at h{nxt_start:g}"
                )
        # tail: production after the final CIP
        tail = last_prod.get(line)
        if tail is not None and tail > windows[-1][1] + interval + TOL:
            violations.append(
                f"{line}: production runs to h{tail:g}, {tail - windows[-1][1]:g}h "
                f"after the last CIP ended h{windows[-1][1]:g} (max {interval:g}h)"
            )

    assert not violations, (
        "CIP max-interval deadline breached (HARD compliance constraint, never "
        "relaxed at any level): " + "; ".join(violations[:10])
    )


# --------------------------------------------------------------------------
# P6 -- charter row 6: changeover setup times
# --------------------------------------------------------------------------
def test_p6_gaps_between_runs_respect_changeover_setup_times(work, relax):
    """model_builder.py:361-370 -- the changeover block (pairwise ordering plus
    setup times) is built only when the phase is sanity3/full AND ignore_co is
    off. Relax-ladder level 3 turns ignore_co ON, which removes the constraint
    from the model entirely, so the probe skips loudly at that level instead of
    asserting a promise the solve never made.

    Reuses code/solver/validate_schedule.check_changeover_timing (spec rule 3).
    The checker itself is exercised unconditionally by the two tests below, so
    row 6 keeps real coverage even when the newest work dir is a level-3 solve.
    """
    if str(SOLVER_DIR) not in sys.path:
        sys.path.insert(0, str(SOLVER_DIR))
    try:
        from validate_schedule import check_changeover_timing  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot import validate_schedule: {exc}")

    if relax["ignore_co"]:
        pytest.skip(
            f"changeover constraints absent from the model at relax level "
            f"{relax['level']} ({relax['mode']}) -- model_builder.py:370. "
            "Row 6 is UNVERIFIED for this work dir; re-run a level<3 solve to "
            "exercise it."
        )

    issues = check_changeover_timing(
        work / "schedule_phase2.csv", work / "changeovers.csv"
    )
    missing = [i for i in issues if i.startswith("MISSING")]
    if missing:
        pytest.skip("; ".join(missing))
    problems = [i for i in issues if i.startswith("CHANGEOVER:")]
    assert not problems, (
        f"{len(problems)} gap(s) shorter than the required setup time at relax "
        f"level {relax['level']} ({relax['mode']}): " + "; ".join(problems[:10])
    )


def test_p6_checker_flags_a_gap_shorter_than_the_setup_time(tmp_path):
    """The reused checker gets its own test so row 6 is never silently
    uncovered when the newest solve happens to be an ignore_co one."""
    if str(SOLVER_DIR) not in sys.path:
        sys.path.insert(0, str(SOLVER_DIR))
    from validate_schedule import check_changeover_timing

    (tmp_path / "schedule_phase2.csv").write_text(
        "line_id,line_name,order_id,sku,start_hour,end_hour\n"
        "0,P09,A,111,0,10\n"
        "0,P09,B,222,11,20\n",
        encoding="utf-8",
    )
    (tmp_path / "changeovers.csv").write_text(
        "from_sku,to_sku,setup_hours\n111,222,4\n", encoding="utf-8"
    )
    issues = check_changeover_timing(
        tmp_path / "schedule_phase2.csv", tmp_path / "changeovers.csv"
    )
    problems = [i for i in issues if i.startswith("CHANGEOVER:")]
    assert len(problems) == 1
    assert "needs 4h setup but gap is 1h" in problems[0]


def test_p6_checker_is_quiet_when_the_gap_covers_the_setup_time(tmp_path):
    if str(SOLVER_DIR) not in sys.path:
        sys.path.insert(0, str(SOLVER_DIR))
    from validate_schedule import check_changeover_timing

    (tmp_path / "schedule_phase2.csv").write_text(
        "line_id,line_name,order_id,sku,start_hour,end_hour\n"
        "0,P09,A,111,0,10\n"
        "0,P09,B,222,14,20\n",
        encoding="utf-8",
    )
    (tmp_path / "changeovers.csv").write_text(
        "from_sku,to_sku,setup_hours\n111,222,4\n", encoding="utf-8"
    )
    issues = check_changeover_timing(
        tmp_path / "schedule_phase2.csv", tmp_path / "changeovers.csv"
    )
    assert [i for i in issues if i.startswith("CHANGEOVER:")] == []


# --------------------------------------------------------------------------
# P7 -- charter row 7: max lines per order
# --------------------------------------------------------------------------
def test_p7_no_order_is_split_across_too_many_lines(schedule, cfg):
    """model_builder.py:325-327 -- sum(present) <= max_lines_per_order, hard at
    every relax level. Trials are pinned to exactly one line and skip it."""
    limit = cfg["max_lines_per_order"]
    if limit <= 0:
        pytest.skip("no max_lines_per_order configured")
    prod = schedule[~schedule["trial"]]
    counts = prod.groupby("order_id")["line_id"].nunique()
    over = counts[counts > limit]
    violations = [
        f"{oid} on {n} lines ("
        + ",".join(sorted(set(prod[prod["order_id"] == oid]["line_name"].map(_norm))))
        + ")"
        for oid, n in over.items()
    ]
    assert not violations, (
        f"max_lines_per_order={limit} exceeded: " + "; ".join(violations[:10])
    )


# --------------------------------------------------------------------------
# P8 -- charter row 8: due dates
# --------------------------------------------------------------------------
def test_p8_orders_start_no_earlier_than_their_due_window(schedule, cfg, orders, relax):
    """Charter row 8 -- no production before the order's window opens. The
    window is the order's due_start, except that with allow_week1_in_week0
    the plant's early-fill policy applies (plant decision 2026-09-04, see
    _window_open and the note at the top of this file): unbounded (the
    default -- the floor is 0 and this probe then only guards current MOs)
    or `early_fill_hours` before its own due_start for every demand order.
    Stated from the plant rule, not copied from the model. The start floor
    is hard at EVERY relax level, but cross-week mode replaces the whole due
    block with a weighted preference, so the probe skips loudly there."""
    if relax["cross_week"]:
        pytest.skip(
            "cross_week mode: the due window is a weighted preference, not a "
            "hard wall (model_builder.py:136-169). The start floor is "
            "UNVERIFIED for this work dir."
        )
    prod = schedule[~schedule["trial"]]
    violations = []
    for (line_id, order_id), grp in prod.groupby(["line_id", "order_id"]):
        o = orders.get(order_id)
        if o is None:
            continue
        ds_eff = _window_open(o, orders, cfg)
        first = float(grp["start_hour"].min())
        if first < ds_eff - TOL:
            violations.append(
                f"{grp.iloc[0]['line_name']} {order_id} starts h{first:g} "
                f"but its window opens at h{ds_eff:g}"
            )
    assert not violations, (
        "production before the due-window start: " + "; ".join(violations[:10])
    )


def test_p8c_no_demand_block_starts_before_a_committed_mo_on_its_line(schedule, work):
    """Plant decision 2026-09-04 -- "early fill ... cannot be placed before an
    already scheduled MO". In an E-style work dir the scheduled MOs are the
    rows of current_mo.csv, written back as "<mo>|CUR" blocks on their locked
    line (model_builder committed-MO floor; validator EARLY_BEFORE_COMMITTED).
    Every demand block on such a line must start at or after the END of every
    committed block there, at every relax level (the floor is a physical
    commitment, not a preference). The solver_tiny fixture carries MO1 on
    L1, so this probe is live there; a work dir without current_mo.csv skips."""
    if not (work / "current_mo.csv").exists():
        pytest.skip("no current_mo.csv (not a scenario-E work dir)")
    committed = schedule[schedule["current_mo"] & ~schedule["trial"]]
    if committed.empty:
        pytest.skip("no committed-MO rows in the schedule")
    demand = schedule[~schedule["current_mo"] & ~schedule["trial"]]
    violations = []
    for line_id, cgrp in committed.groupby("line_id"):
        c_end = float(cgrp["end_hour"].max())
        for _, r in demand[demand["line_id"] == line_id].iterrows():
            if float(r["start_hour"]) < c_end - TOL:
                violations.append(
                    f"{r['line_name']} {r['order_id']} starts h{float(r['start_hour']):g} "
                    f"before committed MO(s) {sorted(set(cgrp['order_id']))} end h{c_end:g}"
                )
    assert not violations, (
        "demand placed before an already scheduled MO: " + "; ".join(violations[:10])
    )


def test_p8b_orders_end_by_their_due_date_cap(schedule, cfg, orders, relax):
    """model_builder.py -- eff_end <= due_end + 1 is the HARD cap under
    [scheduler] due_week_policy = "hard". Ladder level 2+ (soft_due) replaces
    it with due_end + 1 + lateness where lateness <= H - (due_end + 1), i.e.
    the horizon; cross-week mode drops the end constraint too; and the
    OPT-IN policy due_week_policy = "soft" (plant decision 2026-09-04 #2,
    "week numbers are preferential order, not hard borders") prices a demand
    order's lateness per kg-week at EVERY level instead of walling it. The
    key absent means "hard" (the shipped default since 2026-09-04 night). In
    the relaxed modes the probe asserts the bound that IS in force (the
    horizon) and the count of late orders is reported by the dispatch
    summary rather than silently swallowed."""
    horizon = cfg["horizon_h"]
    soft_weeks = cfg.get("due_week_policy", "hard") == "soft"
    relaxed = relax["relax_due"] or relax["cross_week"] or soft_weeks
    prod = schedule[~schedule["trial"]]
    hard_violations, late = [], []
    for order_id, grp in prod.groupby("order_id"):
        o = orders.get(order_id)
        if o is None:
            continue
        end = float(grp["end_hour"].max())
        cap = o["due_end"] + 1
        if end > cap + TOL:
            late.append(f"{order_id} ends h{end:g} (due+1 = h{cap:g})")
            if not relaxed:
                hard_violations.append(late[-1])
        if horizon > 0 and end > horizon + TOL:
            hard_violations.append(
                f"{order_id} ends h{end:g}, past the {horizon:g}h horizon"
            )
    assert not hard_violations, (
        f"due-date cap breached at relax level {relax['level']} "
        f"({relax['mode']}, cross_week={relax['cross_week']}): "
        + "; ".join(hard_violations[:10])
    )
    if relaxed and late:
        pytest.skip(
            f"{len(late)} order(s) finish past due+1, permitted by relax level "
            f"{relax['level']} ({relax['mode']}, cross_week={relax['cross_week']}, "
            f"due_week_policy={'soft' if soft_weeks else 'hard'}); "
            "the horizon bound was asserted instead. Row 8's hard cap is "
            "UNVERIFIED for this work dir"
            + (" (soft due weeks: lateness is a priced trade-off, see the "
               "validator's DUE_WINDOW WARN rows)" if soft_weeks else "")
            + ". First few: " + "; ".join(late[:5])
        )


# --------------------------------------------------------------------------
# P9 -- charter row 9: demand total (no phantom tonnage)
# --------------------------------------------------------------------------
def test_p9_no_sku_is_produced_beyond_its_total_demand(work, schedule, orders, rates):
    """Two independent statements of "no phantom tonnage".

    a. Per SKU, total produced kg <= total demand kg, where demand is rebuilt
       from the work-dir INPUTS (demand_plan qty_max + current_mo remaining_kg)
       rather than from produced_vs_bounds.csv, so the check does not lean on
       the artifact it is auditing. Trials are excluded from BOTH sides: a
       trial is pinned production, not demand.
    b. Reported produced kg <= what the schedule can physically make
       (run_hours x rate_kgph), so tonnage cannot be claimed without hours
       behind it.

    Tolerance: the solver works in whole kg and rounds each block, so allow
    1 kg per block plus 1 kg. Anything larger is a real discrepancy, not
    rounding.
    """
    pvb = _read(work / "produced_vs_bounds.csv")
    if pvb is None:
        pytest.skip(f"{work.name}: no produced_vs_bounds.csv")
    pvb["order_id"] = pvb["order_id"].astype(str)
    pvb["sku"] = pvb["sku"].astype(str)
    pvb["produced"] = pd.to_numeric(pvb["produced"], errors="coerce").fillna(0.0)

    trial_ids = set(schedule[schedule["trial"]]["order_id"])
    audited = pvb[~pvb["order_id"].isin(trial_ids)]

    demand_by_sku: dict[str, float] = {}
    for o in orders.values():
        demand_by_sku[o["sku"]] = demand_by_sku.get(o["sku"], 0.0) + float(o["qty_max"])

    blocks_by_order = schedule.groupby("order_id").size().to_dict()
    over = []
    for sku, grp in audited.groupby("sku"):
        produced = float(grp["produced"].sum())
        cap = demand_by_sku.get(sku, 0.0)
        n_blocks = sum(blocks_by_order.get(oid, 0) for oid in grp["order_id"])
        tol = 1.0 + float(n_blocks)
        if produced > cap + tol:
            over.append(
                f"sku {sku}: produced {produced:g} kg > demand {cap:g} kg "
                f"(excess {produced - cap:g})"
            )
    assert not over, (
        "phantom tonnage -- more produced than demanded: " + "; ".join(over[:10])
    )

    if rates is None:
        pytest.skip(
            "part (a) asserted; rate basis ambiguous (line_rates.csv present, "
            "use_sku_rates off) so the hours-behind-the-kg check is skipped"
        )
    derived: dict[str, float] = {}
    for r in schedule.itertuples():
        derived[r.order_id] = derived.get(r.order_id, 0.0) + (
            rates.get((int(r.line_id), r.sku), 0.0) * float(r.run_hours)
        )
    unbacked = []
    for r in audited.itertuples():
        made = derived.get(r.order_id, 0.0)
        tol = 1.0 + float(blocks_by_order.get(r.order_id, 0))
        if float(r.produced) > made + tol:
            unbacked.append(
                f"{r.order_id}: reported {float(r.produced):g} kg but the "
                f"schedule only supports {made:g} kg"
            )
    assert not unbacked, (
        "produced kg not backed by scheduled hours x rate: "
        + "; ".join(unbacked[:10])
    )
