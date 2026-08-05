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
from helpers.paths import data_dir, legacy_dir
from helpers.scenario_runner import (
    CUSTOM_SCENARIO_ID,
    OBJECTIVE_MODES,
    SCENARIOS,
    make_custom_scenario,
    run_scenario,
    save_scenario_version,
    scenario_knobs,
)
from helpers.scorecard_engine import delta_narrative, score_calendar
from helpers.scorecard_ui import render_scorecard
from helpers.version_manager import list_versions, upsert_version

st.header("Generate Scenarios")
st.caption(
    "Phase 2 - let the solver propose alternatives. Compare each to the current schedule (the plant's own line schedule). "
    "Planners accept 'baseline 62 → proposed 84 — show me why,' not 'the computer says do this.'"
)

dd = data_dir()
if not (legacy_dir() / "code" / "phase2_scheduler.py").exists():
    st.error("Flowstate-legacy solver not found. Keep Flowstate-legacy/ in the repo.")
    st.stop()

cfg = load_toml()
default_tl = int(cfg.get("scheduler", {}).get("time_limit", 60))

baseline_cal = load_calendar(dd / "calendar_blocks.csv")
if baseline_cal.empty:
    st.warning("No current schedule yet. Import the planner's schedule on the Scorecard page, or generate the naive demand-plan baseline below.")
else:
    baseline = score_calendar(baseline_cal, week_label="current schedule", data_dir=dd)
    st.subheader("Current schedule (baseline)")
    st.metric("Composite", f"{baseline.composite:.0f}" if baseline.composite is not None else "n/a")
    with st.expander("Baseline scorecard"):
        render_scorecard(baseline, show_formulas=False)

st.divider()

# -- Naive strawman straight from the demand plan (no solver) -------------
st.subheader("Naive baseline from the demand plan (no solver)")
st.caption(
    "AZAP is the customer / corporate **demand plan**: which SKU, how many kg, which week. "
    "It does not schedule lines. This button takes AZAP literally -- every order runs in the "
    "week it asked for, on the fastest capable line, back to back -- with no changeover, CIP "
    "or optimization logic at all. It is the deliberate strawman: it shows what 'just do what "
    "AZAP said' actually costs. It runs instantly (plain Python, no CP-SAT)."
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
        help="Off by default: the planner's own manual schedule is usually the better base model.",
    )

if st.button("Generate naive baseline", key="gen_naive"):
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
            "multipliers inside the objective branch in Flowstate-legacy/code/model_builder.py."
        )


st.subheader("Solver knobs per scenario")
for _s in PRESETS:
    _render_knobs(_s)

tl = st.number_input("Solver time limit (s) per scenario", min_value=10, max_value=600, value=default_tl, step=10)
selected = st.multiselect(
    "Scenarios to generate",
    options=[s["id"] for s in PRESETS],
    default=["A", "D"],
    format_func=lambda i: next(s["name"] for s in PRESETS if s["id"] == i),
)

st.caption(f"Versions in use: {len(list_versions(dd))} / 5. Generating will replace prior Scenario X slots when needed.")


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
    return " — ".join([base] + extras) if extras else base


def _generate_one(scenario: dict, time_limit: int, overrides: dict | None = None) -> bool:
    """Solve one scenario, render its result, and save it as a version."""
    with st.status(f"Solving {scenario['name']}...", expanded=True) as status:
        st.write(scenario["intent"])
        try:
            result = run_scenario(scenario, dd, time_limit=int(time_limit), overrides=overrides)
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
            slug = save_scenario_version(scenario, result, dd)
        except ValueError as e:
            st.error(str(e))
            status.update(label=str(e), state="error")
            return False
        status.update(label=f"{scenario['name']} → `{slug}`", state="complete")
        feas = result.get("feasibility")
        if feas:
            st.caption("Solver: " + _feasibility_summary(feas))
        sc = result["scorecard"]
        st.metric("Composite", f"{sc.composite:.0f}" if sc.composite is not None else "n/a")
        st.markdown("**vs baseline**")
        for d in delta_narrative(baseline, sc):
            st.write(f"- {d}")
        with st.expander("Raw solver log"):
            st.code((result.get("log") or "")[-4000:], language="text")
        return True


if st.button("Generate selected scenarios", type="primary", disabled=not selected):
    if baseline_cal.empty:
        st.error("Need a baseline calendar first.")
    else:
        made = 0
        for sid in selected:
            scenario = next(s for s in PRESETS if s["id"] == sid)
            if _generate_one(scenario, int(tl)):
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
        help="Bonus per hour of CIP absorbed into an existing gap.",
    )
with oc3:
    w_late = st.number_input(
        "late_weight", min_value=0, max_value=10000,
        value=int(_obj_cfg.get("late_weight", 200)), step=10,
        help="Penalty per hour an order finishes past its due date.",
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

custom_overrides = {
    "makespan_weight": int(w_makespan),
    "changeover_weight": int(w_changeover),
    "cip_defer_weight": int(w_cip),
    "idle_weight": int(w_idle),
    "late_weight": int(w_late),
    "topload_weight": int(w_topload),
    "ffs_weight": int(w_ffs),
    "ttp_weight": int(w_ttp),
    "casepacker_weight": int(w_case),
    "base_changeover_weight": int(w_base),
    "conv_org_weight": int(w_conv),
    "cinn_weight": int(w_cinn),
    "flavor_weight": int(w_flavor),
}

custom_scenario = make_custom_scenario(custom_name, custom_mode, custom_overrides)
_render_knobs(custom_scenario, custom_overrides)

if st.button("Generate custom scenario", type="primary"):
    if baseline_cal.empty:
        st.error("Need a baseline calendar first.")
    elif _generate_one(custom_scenario, int(tl), custom_overrides):
        st.success("Done. Open **Version Compare** to inspect side-by-side and add pros/cons.")

st.divider()
st.subheader("Saved versions")
for v in list_versions(dd):
    sc = (v.get("scorecard") or {}).get("composite")
    st.write(f"- **{display_name(v['slug'], v.get('name'))}** (`{v['slug']}`) · composite={sc} · {v.get('source', '')}")
