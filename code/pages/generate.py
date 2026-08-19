# pages/generate.py - Phase 2: Optimizer scenarios vs the current schedule.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.calendar_io import load_calendar, save_calendar
from helpers.config import load_toml
from helpers.labels import display_name
from helpers.naive_baseline import (
    NAIVE_VERSION_NAME,
    NAIVE_VERSION_SLUG,
    build_naive_calendar,
)
from helpers.paths import data_dir, solver_dir
from helpers.scenario_runner import (
    CUSTOM_SCENARIO_ID,
    OBJECTIVE_MODES,
    SCENARIOS,
    clear_pending_manifest,
    list_pending_runs,
    make_custom_scenario,
    pending_is_stale,
    resume_scenario,
    run_scenario,
    save_scenario_version,
    scenario_knobs,
)
from helpers.scorecard_engine import (
    delta_narrative,
    score_calendar,
    scoring_inputs_signature,
)
from helpers.solver_rules import render_solver_rulebook
from helpers import solver_live
from helpers.scorecard_ui import render_scorecard
from helpers.version_manager import MAX_VERSIONS, list_versions, upsert_version
from solver.changeover_cache import load_changeover_setup_nested

st.header("Generate Scenarios")
st.caption(
    "Phase 2 - let the solver propose alternatives. Compare each to the current schedule (the plant's own line schedule). "
    "Planners accept 'baseline 62 → proposed 84 — show me why,' not 'the computer says do this.'"
)

dd = data_dir()
if not (solver_dir() / "phase2_scheduler.py").exists():
    st.error("CP-SAT solver not found at code/solver/phase2_scheduler.py.")
    st.stop()

cfg = load_toml()
sched_cfg = cfg.get("scheduler", {})
default_tl = int(sched_cfg.get("time_limit", 60))
cip_cfg = cfg.get("cip", {})

# Structures the scenario Gantt preview needs (same shapes the calendar builds).
import pandas as _pd
from helpers.lines_model import expand_caps_with_groups, is_double, side_of
from helpers.paths import reference_dir as _ref_dir

caps: dict = {}
_caps_p = _ref_dir(dd) / "capabilities_rates.csv"
if _caps_p.exists():
    _cap_df = _pd.read_csv(_caps_p)
    if "calc_rate_kgph" not in _cap_df.columns and "rate_kgph" in _cap_df.columns:
        _cap_df = _cap_df.rename(columns={"rate_kgph": "calc_rate_kgph"})
    for _, r in _cap_df.iterrows():
        if int(r.get("capable", 0) or 0) != 1:
            continue
        caps.setdefault(str(r["line_name"]), {})[str(r["sku"])] = float(
            r.get("calc_rate_kgph") or 0)
caps = expand_caps_with_groups(caps)

changeovers: dict = {}
_co_p = _ref_dir(dd) / "changeovers.csv"
if _co_p.exists():
    changeovers = load_changeover_setup_nested(_co_p)

demand_targets: list = []
_dem_p = _ref_dir(dd) / "demand_plan.csv"
if _dem_p.exists():
    for _, r in _pd.read_csv(_dem_p).iterrows():
        t = float(r.get("qty_target", 0) or 0)
        demand_targets.append({
            "order_id": str(r["order_id"]), "sku": str(r["sku"]),
            "qty_min": t * float(r.get("lower_pct", 0.9) or 0.9),
            "qty_max": t * float(r.get("upper_pct", 1.1) or 1.1),
        })

lines: list = []
_lp = dd / "lines.csv"
if _lp.exists():
    _ldf = _pd.read_csv(_lp)
    if "active" in _ldf.columns:
        _ldf = _ldf[_ldf["active"] != False]
    _lcols = [c for c in ("line_id", "line_name", "line_group", "side", "is_double") if c in _ldf.columns]
    lines = _ldf[_lcols].to_dict("records")
    for _l in lines:
        _nm = str(_l.get("line_name", ""))
        _l["line_group"] = str(_l.get("line_group") or "") or (_nm[:-1] if side_of(_nm) else _nm)
        _l["side"] = side_of(_nm) or str(_l.get("side") or "")
        _l["is_double"] = bool(is_double(_nm))

# -- Netting preview: what the solver will actually be asked to plan ------
# Same ledger Scenario F stages with (helpers.demand_coverage) — a demand
# week already covered by committed MOs is NOT re-planned.
try:
    from helpers.demand_coverage import build_ledger_from_data
    _ledger = build_ledger_from_data(dd, cfg)
except Exception as _lexc:  # noqa: BLE001
    _ledger = None
    st.caption(f"Netting preview unavailable: {_lexc}")
if _ledger is not None and _ledger.rows:
    _ldf = _ledger.to_frame()
    _cur = _ledger.anchor_week_key
    if _cur is not None:
        _ldf = _ldf[_ldf["week_key"] >= _cur]
    if len(_ldf):
        st.subheader("Demand after netting (committed MOs credited)")
        _wk = _ldf.groupby("week_label", sort=False).agg(
            demand_kg=("gross_kg", "sum"),
            covered_kg=("applied_kg", "sum"),
            net_kg=("net_kg", "sum"),
            orders=("order_id", "count"),
            covered_orders=("status", lambda s: int((s == "COVERED").sum())),
        ).reset_index()
        st.dataframe(
            _wk.rename(columns={
                "week_label": "Week", "demand_kg": "Demand kg",
                "covered_kg": "Covered by committed MOs",
                "net_kg": "Net for the solver", "orders": "Orders",
                "covered_orders": "Fully covered"}),
            use_container_width=True, hide_index=True)
        st.caption(
            f"{_wk['covered_kg'].sum():,.0f} kg of "
            f"{_wk['demand_kg'].sum():,.0f} kg is already in committed MOs "
            "— Scenario F subtracts it before solving, so covered weeks are "
            "never re-planned. Full per-order detail: Reconcile → Demand "
            "coverage.")
        with st.expander("Per-order detail"):
            st.dataframe(
                _ldf.drop(columns=["week_key"]).rename(columns={
                    "week_label": "Week", "order_id": "Order", "sku": "SKU",
                    "gross_kg": "Demand kg", "committed_kg": "Committed kg",
                    "produced_kg": "Made kg", "carry_in_kg": "Carry-in kg",
                    "applied_kg": "Covered kg", "net_kg": "Net to plan",
                    "status": "Status"}),
                use_container_width=True, hide_index=True)

baseline_cal = load_calendar(dd / "calendar_blocks.csv")


# Rerun speed (2026-08-19): scoring the whole board on every Streamlit rerun
# cost seconds per widget click. Keyed by the board file's mtime plus the
# live scoring inputs — never recomputed until one of them changes.
@st.cache_data(show_spinner=False)
def _board_score_cached(dd_str: str, cal_mtime: float, live_sig: tuple,
                        _cal: pd.DataFrame):
    return score_calendar(_cal, week_label="current schedule",
                          data_dir=Path(dd_str))


def _board_mtime() -> float:
    try:
        return (dd / "calendar_blocks.csv").stat().st_mtime
    except OSError:
        return 0.0


if baseline_cal.empty:
    st.info(
        "**No current schedule.** That's the normal starting point now — AZAP is "
        "the source of truth. Build a **rough draft schedule from the demand plan** "
        "below to get a working base, then refine it in the Plant Calendar or let "
        "the solver improve it.")
else:
    baseline = _board_score_cached(
        str(dd), _board_mtime(), scoring_inputs_signature(dd), baseline_cal)
    st.subheader("Current schedule (baseline)")
    st.metric("Composite", f"{baseline.composite:.0f}" if baseline.composite is not None else "n/a")
    with st.expander("Baseline scorecard"):
        render_scorecard(baseline, show_formulas=False)

st.divider()

def _feasibility_summary(feas: dict) -> str:
    """One-line human summary of a solver feasibility_report."""
    lvl = feas.get("relax_level", 0)
    mode = feas.get("relax_mode", "hard")
    status = feas.get("status", "?")
    if status == "INFEASIBLE":
        return f"INFEASIBLE at max relax level {lvl} ({mode}) — see report below."
    if lvl == 0:
        base = "Solved at relax level 0 (hard constraints)"
    else:
        base = f"Solved at relax level {lvl} ({mode})"
    late = feas.get("late_orders") or []
    short = feas.get("orders_short_of_qmin") or []
    extras = []
    if late:
        ids = ", ".join(str(o.get("order_id")) for o in late[:4])
        extras.append(f"{len(late)} order(s) late ({ids})")
    if short:
        ids = ", ".join(str(o.get("order_id")) for o in short[:4])
        extras.append(f"{len(short)} order(s) short of min ({ids})")
    moved = feas.get("week_moved_orders") or []
    if moved:
        ids = ", ".join(str(o.get("order_id")) for o in moved[:4])
        extras.append(f"{len(moved)} order(s) moved out of AZAP's week ({ids})")
    elif feas.get("cross_week"):
        extras.append("cross-week on, no order left its AZAP week")
    return " — ".join([base] + extras) if extras else base


def _generate_one(
    scenario: dict,
    time_limit: int,
    overrides: dict | None = None,
    *,
    cross_week: bool = False,
    cip_flex: bool = False,
    work_dir_patch=None,
    resume: bool = False,
) -> bool:
    """Solve one scenario (or reattach to a running one), render its result,
    and save it as a version."""
    scenario = dict(scenario)
    if not resume:
        scenario["cross_week"] = bool(cross_week)
        scenario["cip_flex"] = bool(cip_flex)
    _verb = "Reattached to" if resume else "Solving"
    with st.status(f"{_verb} {scenario['name']}...", expanded=True) as status:
        st.write(scenario.get("intent", ""))
        if cross_week or cip_flex:
            modes = []
            if cross_week:
                modes.append("cross-week (AZAP week is a preference)")
            if cip_flex:
                modes.append("flexible CIP timing (earlier only)")
            st.caption("Flexibility: " + "; ".join(modes))
        from datetime import datetime as _dtnow

        _started = _dtnow.now()
        st.caption(f"🕐 Solver {'reattached' if resume else 'started'} **{_started:%H:%M:%S}**")
        # Live narration (user request 2026-08-19): stage banner in planner
        # language, data chips, solution feed, trust indicators and a solver
        # journal — all fed from the solver's OWN telemetry (progress json +
        # run log). Placeholders are created once so the journal expander
        # keeps its open/closed state across the ~2s polls; both the
        # attended and the reattach path render through this same closure.
        _banner = st.empty()
        _chips = st.empty()
        _bar = st.progress(0.0)
        st.caption("Leaving this page won't kill the solve — it will "
                   "reattach when you come back.")
        _live = st.empty()
        _trust = st.empty()
        _feed = st.empty()
        with st.expander("Solver journal — the machinery, line by line"):
            _journal = st.empty()
        _is_fill = bool(scenario.get("fill_mode"))
        _work_dir = dd / "_scenario_work" / str(scenario["id"])
        # Expected wall time: two-pass F runs pass 1 + anchor + pass 2 (each
        # bounded by the budget); single-pass scenarios may climb the relax
        # ladder. The bar is wall-clock vs this estimate (clamped at 99% —
        # the caption carries the REAL stage/gap telemetry).
        _expected = float(time_limit) * (
            2.2 if scenario.get("two_pass_co") else 1.3) + 60.0

        def _on_progress(prog, elapsed):
            _bar.progress(min(0.99, elapsed / _expected))
            _banner.markdown(
                f"**{solver_live.stage_banner(prog, fill_mode=_is_fill)}**")
            _c = solver_live.data_chips(prog, fill_mode=_is_fill)
            if _c:
                _chips.caption(_c)
            _live.caption(solver_live.live_caption(prog, elapsed))
            _t = solver_live.trust_lines(prog)
            if _t:
                _trust.caption(" · ".join(_t))
            _f = solver_live.solution_feed(prog)
            if _f:
                _feed.markdown(
                    "**Live solution feed** (newest first)\n"
                    + "\n".join(f"- {x}" for x in _f))
            _j = solver_live.journal_from_dir(_work_dir)
            _journal.code(
                "\n".join(_j) if _j
                else "(waiting for the solver's first log lines)",
                language="text")

        try:
            if resume:
                result = resume_scenario(scenario, dd,
                                         progress_cb=_on_progress)
            else:
                result = run_scenario(scenario, dd, time_limit=int(time_limit),
                                      overrides=overrides,
                                      work_dir_patch=work_dir_patch,
                                      progress_cb=_on_progress)
            _bar.progress(1.0)
            _live.caption(
                f"Done in {(_dtnow.now() - _started).seconds // 60}m "
                f"{(_dtnow.now() - _started).seconds % 60:02d}s "
                f"(started {_started:%H:%M:%S})")
        except Exception as e:
            status.update(label=f"{scenario['name']} failed", state="error")
            st.exception(e)
            return False
        if not result["ok"]:
            feas = result.get("feasibility")
            summary = _feasibility_summary(feas) if feas else "no schedule"
            status.update(label=f"{scenario['name']} — {summary}", state="error")
            with st.expander("Solver log"):
                if feas:
                    st.markdown("**Feasibility report**")
                    st.json(feas)
                blockages = (result.get("diag_blockages") or "").strip()
                if blockages:
                    st.markdown("**Blockages diagnostic**")
                    st.code(blockages, language="text")
                st.code(result.get("log") or "(empty)", language="text")
            return False
        try:
            saved = save_scenario_version(scenario, result, dd)
        except ValueError as e:
            st.error(str(e))
            status.update(label=str(e), state="error")
            return False
        if saved.get("evicted"):
            st.info(
                "Version slots were full — auto-evicted the oldest scenario "
                "save(s): " + ", ".join(f"`{s}`" for s in saved["evicted"])
                + ". User-named versions are never evicted.")
        status.update(label=f"{saved['name']} → `{saved['slug']}`",
                      state="complete")
        feas = result.get("feasibility")
        if feas:
            st.caption("Solver: " + _feasibility_summary(feas))
        sc = result["scorecard"]
        st.metric("Composite", f"{sc.composite:.0f}" if sc.composite is not None else "n/a")
        st.markdown("**vs baseline**")
        for d in delta_narrative(baseline, sc):
            st.write(f"- {d}")

        # Preview the generated schedule as a Gantt WITHOUT promoting it to the
        # official calendar. Read-only view of the solver's calendar.
        cal = result.get("calendar")
        if cal is not None and not cal.empty:
            with st.expander(f"Preview {scenario['name']} schedule (Gantt — not the official calendar)", expanded=True):
                try:
                    from components.gantt import gantt_calendar
                    from helpers.calendar_io import calendar_to_gantt_payload
                    from helpers.scorecard_engine import gantt_kpis
                    _sched, _win = calendar_to_gantt_payload(cal)
                    gantt_calendar(
                        schedule=_sched,
                        cip_windows=_win,
                        capabilities=caps,
                        changeovers=changeovers,
                        demand_targets=demand_targets,
                        lines=lines,
                        holding_area=[],
                        side_downtime={},
                        kpis=gantt_kpis(cal, demand_targets, caps, data_dir=dd),
                        config={
                            "planning_anchor": sched_cfg.get("planning_start_date", "2026-02-15 00:00:00"),
                            "cip_duration_h": int(cip_cfg.get("duration_h", 6)),
                            "min_run_hours": int(sched_cfg.get("min_run_hours", 4)),
                            "horizon_hours": int(sched_cfg.get("horizon_hours", 336)),
                            "read_only": True,
                        },
                        height=600,
                        key=f"gantt_preview_{scenario['id']}",
                    )
                except Exception as _e:
                    st.warning(f"Gantt preview unavailable: {_e}")
        with st.expander("Raw solver log"):
            st.code((result.get("log") or "")[-4000:], language="text")
        return True



# ═══ The daily driver: Fill the tail (Scenario F) ════════════════════════
st.subheader("▶ Fill the tail — the daily run (Scenario F)")
st.caption(
    "The committed plan (manprg + cip_info) stays FIXED; the solver only "
    "fills the remaining line-time with netted demand. Pipeline: live "
    "staging → netting (committed MOs credited) → format-aware greedy seed "
    "→ pass 1 (maximize fill, nearest week first) → pass 2 (minimize "
    "FFS/topload changeovers, fill held ≥ 99%). Identical engine and stock "
    "policy to the agent's proposals — running it here IS the manual process."
)
_fc1, _fc2 = st.columns([1, 2])
with _fc1:
    _f_tl = st.selectbox(
        "Solve budget", [300, 600, 1200], index=1,
        format_func=lambda s: f"{s // 60} min per pass", key="f_budget",
        help="Two passes each get this budget (plus a short anchor step).")
with _fc2:
    _f_dns = st.checkbox(
        "Cap component-blocked SKUs (stock policy)", value=True, key="f_dns",
        help="The approved agent policy: DO-NOT-SCHEDULE SKUs get qty_min→0 "
             "and qty_max capped at what components actually support.")
if st.button("Run Fill the tail", type="primary", key="run_fill_tail"):
    _f_patch = None
    if _f_dns:
        try:
            from helpers.agent_policy import dns_ratios as _dnsr
            from helpers.agent_policy import trim_dns_demand as _trim
            from stockcheck.api import stock_check_report as _stockrep
            _dns_map = _dnsr(_stockrep(dd, dd / "reference"))

            def _f_patch(work):
                _dp = work / "demand_plan.csv"
                _dm = _pd.read_csv(_dp, dtype={"sku": str})
                _tr, _pnotes = _trim(_dm, _dns_map)
                _tr.to_csv(_dp, index=False)
                return [f"DNS trim: {len(_pnotes)} order(s) adjusted "
                        f"({len(_dns_map)} component-blocked SKU(s))"] + _pnotes[:5]
        except Exception as _pe:  # noqa: BLE001
            st.warning(f"Stock policy unavailable — solving without it: {_pe}")
            _f_patch = None
    _f_scn = next(_s for _s in SCENARIOS if _s["id"] == "F")
    _generate_one(_f_scn, int(_f_tl), work_dir_patch=_f_patch)

# ── Reattach to an abandoned solve (walkthrough finding 6) ────────────────
# Navigating away mid-solve kills this script but not the solver subprocess;
# the runner leaves run_pending.json in its work dir. On load: still running
# → resume the polling UI; finished but never saved → collect + save now;
# stale/corrupt → offer to discard.
for _pend in list_pending_runs(dd):
    _pwork = Path(_pend["work_dir"])
    _pscn = _pend.get("scenario") or {}
    if pending_is_stale(_pend):
        st.info(
            f"Found a stale or unreadable solver-run manifest in "
            f"`{_pwork.name}` (started {_pend.get('started_at', 'unknown')}). "
            "Its result was never saved and it is over a day old.")
        if st.button("Discard it", key=f"discard_pending_{_pwork.name}"):
            clear_pending_manifest(_pwork)
            st.rerun()
        continue
    st.info(
        f"**{_pscn.get('name', _pwork.name)}** kept solving while you were "
        f"away (started {_pend.get('started_at', '')}) — reattaching.")
    _generate_one(_pscn, int(_pend.get("time_limit") or default_tl),
                  resume=True)

st.divider()
st.subheader("Saved versions")
for v in list_versions(dd):
    if v.get("orphan"):
        st.write(f"- `{v['slug']}` · orphaned folder (no metadata) — holds a "
                 "slot; delete it in Version Compare")
        continue
    sc = (v.get("scorecard") or {}).get("composite")
    st.write(f"- **{display_name(v['slug'], v.get('name'))}** (`{v['slug']}`) · composite={sc} · {v.get('source', '')}")


st.divider()
_show_diag = st.toggle(
    "Show diagnostics & experiments (A–E scenarios, rough draft, custom weights)",
    value=False, key="show_diag",
    help="Everything below is for digging into solver behavior — the daily "
         "process is the Fill-the-tail run above.")
if not _show_diag:
    st.stop()

render_solver_rulebook(cfg)


# -- Rough-draft schedule straight from the demand plan (no solver) --------
st.subheader("Rough draft schedule from the demand plan (no solver)")
st.caption(
    "AZAP is the customer / corporate **demand plan**: which SKU, how many kg, which week. "
    "It does not schedule lines. This builds a **rough draft**: every order runs in the "
    "week it asked for, on the fastest capable line, back to back — no changeover, CIP "
    "or optimization logic. Use it as the starting point when there is no manual schedule "
    "to import, or as a strawman to score against. Runs instantly (plain Python, no CP-SAT)."
)

nc1, nc2 = st.columns([1, 2])
with nc1:
    naive_strategy = st.selectbox(
        "Line pick",
        options=["fastest", "least_loaded"],
        format_func=lambda s: "Fastest capable line" if s == "fastest" else "Least-loaded capable line",
        key="naive_strategy",
    )
with nc2:
    naive_set_base = st.checkbox(
        "Also write it to data/calendar_blocks.csv (make it the current schedule)",
        value=False,
        key="naive_set_base",
        help="Turn the rough draft into the base model you refine in the Plant Calendar.",
    )

if st.button("Generate rough draft schedule", key="gen_naive"):
    horizon = int(cfg.get("scheduler", {}).get("horizon_hours", 336))
    nres = build_naive_calendar(dd, horizon_hours=horizon, strategy=naive_strategy)
    for note in nres.notes:
        st.caption(note)
    if not nres.ok:
        st.error("Could not build a naive baseline. See the notes above.")
    else:
        nsc = score_calendar(nres.calendar, week_label=NAIVE_VERSION_NAME, data_dir=dd)
        m1, m2, m3 = st.columns(3)
        m1.metric("Composite", f"{nsc.composite:.0f}" if nsc.composite is not None else "n/a")
        m2.metric("Orders placed", len(nres.placed))
        m3.metric("Unplaced", len(nres.unplaced))
        try:
            slug = upsert_version(
                NAIVE_VERSION_SLUG,
                NAIVE_VERSION_NAME,
                nres.calendar,
                nsc.to_dict(),
                dd,
                source="naive_demand_plan",
                notes=(
                    "Strawman: demand plan laid out as-is (AZAP week honoured, "
                    f"{naive_strategy} capable line, no changeover/CIP/optimization)."
                ),
            )
            st.success(f"Saved version `{slug}` - {NAIVE_VERSION_NAME}")
        except ValueError as e:
            st.warning(f"Could not save the naive version: {e}")
        if naive_set_base:
            save_calendar(nres.calendar, dd / "calendar_blocks.csv")
            st.success("Written to data/calendar_blocks.csv as the current schedule.")
        if nres.unplaced:
            st.warning(f"{len(nres.unplaced)} order(s) could not be placed:")
            st.dataframe(pd.DataFrame(nres.unplaced), use_container_width=True, hide_index=True)
        with st.expander("Naive placement detail"):
            st.dataframe(pd.DataFrame(nres.placed), use_container_width=True, hide_index=True)
        with st.expander("Naive scorecard"):
            render_scorecard(nsc, show_formulas=False)

st.divider()
st.markdown(
    """
| Scenario | Intent |
| --- | --- |
| **A** | Minimum changeovers |
| **B** | Maximum throughput (spread-load proxy) |
| **C** | Maintenance friendly (balanced + twin CIP alignment) |
| **D** | Balanced |
"""
)

PRESETS = [s for s in SCENARIOS if s["id"] != CUSTOM_SCENARIO_ID]
_obj_cfg_flex = cfg.get("objective", {})


def _render_knobs(scenario: dict, overrides: dict | None = None) -> None:
    """Expander showing the solver knobs + objective formula for a scenario."""
    label = f"What this scenario tunes — {scenario['name']}"
    with st.expander(label):
        st.caption(f"Legacy objective mode: `{scenario['objective']}` (two-phase solve)")
        st.markdown("**Objective the solver minimizes**")
        st.code(scenario.get("objective_formula", "(not documented)"), language="text")
        st.markdown("**Knobs**")
        st.dataframe(
            scenario_knobs(scenario, cfg, overrides),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            "Values come from flowstate.toml. Rows without a config path are hard-coded "
            "multipliers inside the objective branch in code/solver/model_builder.py."
        )


st.subheader("Solver knobs per scenario")
for _s in PRESETS:
    _render_knobs(_s)

selected = st.multiselect(
    "Scenarios to generate",
    options=[s["id"] for s in PRESETS],
    default=["A", "D"],
    format_func=lambda i: next(s["name"] for s in PRESETS if s["id"] == i),
)

st.markdown("**Planning flexibility (applies to every scenario generated below)**")
fx1, fx2 = st.columns(2)
with fx1:
    cross_week_on = st.checkbox(
        "Allow moving SKUs between week 1 and week 2",
        value=False,
        help=(
            "AZAP's week becomes a preference, not a hard rule. The solver looks "
            "at the whole two-week (336h) horizon at once and may pull a week-2 "
            "SKU into week 1 (or push a week-1 SKU into week 2) to build a longer "
            "run and avoid a changeover. Total two-week demand still has to be "
            "met in full. Off = today's behaviour (each week solved separately)."
        ),
    )
with fx2:
    cip_flex_on = st.checkbox(
        "Let CIPs move earlier to save a changeover",
        value=False,
        help=(
            "A wash can be pulled FORWARD when doing so removes a changeover. "
            "It can never be pushed past the line's maximum allowable CIP "
            "interval - that limit stays a hard rule at every relax level "
            "(food safety). Off = today's behaviour (CIPs sit as late as legal)."
        ),
    )
if cross_week_on:
    st.info(
        "Cross-week is ON: scenarios solve as a single 336h model instead of "
        "week-0-then-week-1, so orders can merge across AZAP's week boundary. "
        f"Deviation costs {int(_obj_cfg_flex.get('week_deviation_weight', 40))} "
        "per order-hour outside the requested week."
    )

# -- Solver time limit -----------------------------------------------------
# A single-phase full-horizon model (336h in one CP-SAT model) is a much harder
# search than the two-phase week-0/week-1 decomposition: measured on this data,
# tl=120 gave 46 blocks/32 SKUs while tl=300 gave 81 blocks/46 SKUs on the SAME
# constraints. A level-0 UNKNOWN from a short time limit is indistinguishable in
# feasibility_report.json from a genuine infeasibility, so recommend 300s
# whenever the run will be single-phase and warn loudly below 120s.
SINGLE_PHASE_TL = 300
TL_WARN_BELOW = 120

_single_phase_ids = sorted(
    s["id"] for s in PRESETS if not s.get("two_phase", True) and s["id"] in (selected or [])
)
single_phase_run = bool(cross_week_on or _single_phase_ids)
_reason = (
    "cross-week is ON" if cross_week_on
    else "scenario " + "/".join(_single_phase_ids) + " is single-phase"
) if single_phase_run else ""

_tl_default = SINGLE_PHASE_TL if single_phase_run else default_tl
tl = st.number_input(
    "Solver time limit (s) per scenario",
    min_value=10, max_value=600, value=_tl_default, step=10,
    # Key changes with the mode so flipping cross-week / picking a single-phase
    # scenario re-seeds the recommended default instead of keeping a stale 60.
    key=f"solver_time_limit_{'sp' if single_phase_run else 'tp'}",
    help=(
        "Per-scenario CP-SAT budget. Two-phase solves each week separately and is "
        f"usually fine at {default_tl}s; a single-phase full-horizon solve needs "
        f"{SINGLE_PHASE_TL}s or more to return a good schedule."
    ),
)
if single_phase_run:
    if int(tl) < TL_WARN_BELOW:
        st.warning(
            f"**Time limit {int(tl)}s is too short for this run** ({_reason}, so the "
            "solver builds one full-horizon model instead of two weekly ones). Below "
            f"{TL_WARN_BELOW}s CP-SAT typically returns UNKNOWN at relax level 0 and the "
            "ladder escalates — you get a poor schedule that *looks* like an infeasible "
            f"model. Raise it to {SINGLE_PHASE_TL}s."
        )
    else:
        st.caption(
            f"Single-phase run ({_reason}) — budgeting {int(tl)}s per scenario. "
            f"Recommended minimum is {SINGLE_PHASE_TL}s."
        )

st.caption(
    f"Versions in use: {len(list_versions(dd))} / {MAX_VERSIONS}. Every run "
    "saves a new timestamped version; when full, the oldest auto-saved "
    "scenario version is evicted (user-named versions never are).")


if st.button("Generate selected scenarios", type="primary", disabled=not selected):
    if baseline_cal.empty:
        st.error("Need a baseline calendar first.")
    else:
        made = 0
        for sid in selected:
            scenario = next(s for s in PRESETS if s["id"] == sid)
            if _generate_one(
                scenario, int(tl),
                cross_week=cross_week_on, cip_flex=cip_flex_on,
            ):
                made += 1
        if made:
            st.success("Done. Open **Version Compare** to inspect side-by-side and add pros/cons.")

st.divider()
st.subheader("Custom scenario")
st.caption(
    "Pick the objective mode and override the weights the solver uses. Values below default to "
    "flowstate.toml; anything you change is written into the scenario's own solver config "
    "(the repo flowstate.toml is not modified)."
)

_obj_cfg = cfg.get("objective", {})
_co_cfg = cfg.get("changeover", {})

custom_name = st.text_input("Scenario name", value="Custom scenario")
custom_mode = st.selectbox(
    "Objective mode",
    options=list(OBJECTIVE_MODES),
    index=list(OBJECTIVE_MODES).index("balanced"),
    help="balanced uses makespan_weight / changeover_weight; the other two use fixed multipliers.",
)

st.markdown("**Objective weights**")
oc1, oc2, oc3 = st.columns(3)
with oc1:
    w_makespan = st.number_input(
        "makespan_weight", min_value=0, max_value=1000,
        value=int(_obj_cfg.get("makespan_weight", 6)), step=1,
        help="Balanced mode only. Weight on total schedule length.",
    )
    w_idle = st.number_input(
        "idle_weight", min_value=0, max_value=1000,
        value=int(_obj_cfg.get("idle_weight", 0)), step=1,
        help="Penalty per idle hour on a line.",
    )
with oc2:
    w_changeover = st.number_input(
        "changeover_weight", min_value=0, max_value=10000,
        value=int(_obj_cfg.get("changeover_weight", 120)), step=5,
        help="Balanced mode only. Multiplier on the weighted changeover cost.",
    )
    w_cip = st.number_input(
        "cip_defer_weight", min_value=0, max_value=1000,
        value=int(_obj_cfg.get("cip_defer_weight", 5)), step=1,
        help="Reward per hour a CIP starts LATER (cleans drift toward "
             "their legal deadline instead of hour 0). All modes; the max "
             "CIP interval itself stays hard.",
    )
with oc3:
    w_late = st.number_input(
        "late_weight", min_value=0, max_value=10000,
        value=int(_obj_cfg.get("late_weight", 200)), step=10,
        help="Penalty per hour an order finishes past its due date. Only "
             "active once the relax ladder reaches level 2 (soft due "
             "dates); ignored in cross-week mode.",
    )
    w_week_dev = st.number_input(
        "week_deviation_weight", min_value=0, max_value=10000,
        value=int(_obj_cfg.get("week_deviation_weight", 40)), step=5,
        help=(
            "Cross-week mode only. Cost per hour an order runs outside AZAP's "
            "requested week. Lower = more willing to move a SKU between week 1 "
            "and week 2 to build a longer run."
        ),
    )
    w_cip_flex = st.number_input(
        "cip_flex_weight", min_value=0, max_value=100,
        value=int(_obj_cfg.get("cip_flex_weight", 20)), step=5,
        help=(
            "CIP flexibility mode only. Percent the CIP-deferral reward is "
            "scaled to, so a CIP can be pulled earlier to absorb a changeover. "
            "The line's max allowable CIP interval always stays hard."
        ),
    )

st.markdown("**Per-machine changeover weights**")
cc1, cc2, cc3, cc4 = st.columns(4)
with cc1:
    w_topload = st.slider("topload_weight", 0, 200, int(_co_cfg.get("topload_weight", 50)))
    w_conv = st.slider("conv_org_weight", 0, 200, int(_co_cfg.get("conv_org_weight", 30)))
with cc2:
    w_ffs = st.slider("ffs_weight", 0, 200, int(_co_cfg.get("ffs_weight", 75)))
    w_cinn = st.slider("cinn_weight", 0, 200, int(_co_cfg.get("cinn_weight", 20)))
with cc3:
    w_ttp = st.slider("ttp_weight", 0, 200, int(_co_cfg.get("ttp_weight", 5)))
    w_flavor = st.slider("flavor_weight", 0, 200, int(_co_cfg.get("flavor_weight", 5)))
with cc4:
    w_case = st.slider("casepacker_weight", 0, 200, int(_co_cfg.get("casepacker_weight", 20)))
    w_base = st.slider("base_changeover_weight", 0, 200, int(_co_cfg.get("base_changeover_weight", 5)))

st.markdown("**Solve rules (hard constraints, not weights)**")
sr1, sr2, sr3 = st.columns(3)
with sr1:
    w_min_run = st.number_input(
        "min_run_hours", min_value=1, max_value=48,
        value=int(sched_cfg.get("min_run_hours", 4)), step=1,
        help="Hard floor: any run a line gets must last at least this many "
             "hours (each segment of a CIP-split run too). Also steers the "
             "Scenario F greedy seed.",
    )
with sr2:
    w_min_run_pct = st.number_input(
        "min_run_pct_of_qty", min_value=0.0, max_value=1.0,
        value=float(sched_cfg.get("min_run_pct_of_qty", 0.5)), step=0.05,
        format="%.2f",
        help="Hard floor: a line assigned to an order must run at least "
             "this fraction of the hours the order's minimum quantity "
             "needs. Only bites when an order splits across lines — at "
             "0.50 each of two lines must take a real half, not a 1h stub.",
    )
with sr3:
    w_mlpo = st.number_input(
        "max_lines_per_order", min_value=1, max_value=6,
        value=int(sched_cfg.get("max_lines_per_order", 3)), step=1,
        help="Cap on how many lines may run the SAME ORDER simultaneously. "
             "Per ORDER (one demand row = one SKU-week), not per SKU: the "
             "same SKU in two different week-orders may use more lines in "
             "total.",
    )

custom_overrides = {
    "min_run_hours": int(w_min_run),
    "min_run_pct_of_qty": float(w_min_run_pct),
    "max_lines_per_order": int(w_mlpo),
    "makespan_weight": int(w_makespan),
    "changeover_weight": int(w_changeover),
    "cip_defer_weight": int(w_cip),
    "idle_weight": int(w_idle),
    "late_weight": int(w_late),
    "week_deviation_weight": int(w_week_dev),
    "cip_flex_weight": int(w_cip_flex),
    "topload_weight": int(w_topload),
    "ffs_weight": int(w_ffs),
    "ttp_weight": int(w_ttp),
    "casepacker_weight": int(w_case),
    "base_changeover_weight": int(w_base),
    "conv_org_weight": int(w_conv),
    "cinn_weight": int(w_cinn),
    "flavor_weight": int(w_flavor),
}

custom_scenario = make_custom_scenario(
    custom_name,
    custom_mode,
    custom_overrides,
    cross_week=cross_week_on,
    cip_flex=cip_flex_on,
)
_render_knobs(custom_scenario, custom_overrides)

if st.button("Generate custom scenario", type="primary"):
    if baseline_cal.empty:
        st.error("Need a baseline calendar first.")
    elif _generate_one(
        custom_scenario, int(tl), custom_overrides,
        cross_week=cross_week_on, cip_flex=cip_flex_on,
    ):
        st.success("Done. Open **Version Compare** to inspect side-by-side and add pros/cons.")
