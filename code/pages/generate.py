# pages/generate.py — Phase 2: Optimizer scenarios vs AZAP baseline.

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.calendar_io import load_calendar
from helpers.config import load_toml
from helpers.paths import data_dir, legacy_dir
from helpers.scenario_runner import SCENARIOS, run_scenario, save_scenario_version
from helpers.scorecard_engine import delta_narrative, score_calendar
from helpers.scorecard_ui import render_scorecard
from helpers.version_manager import list_versions

st.header("Generate Scenarios")
st.caption(
    "Phase 2 — let the solver propose alternatives. Compare each to the AZAP / official baseline. "
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
    st.warning("Import / score an official calendar on the Scorecard page first (AZAP baseline).")
else:
    baseline = score_calendar(baseline_cal, week_label="AZAP baseline", data_dir=dd)
    st.subheader("AZAP / official baseline")
    st.metric("Composite", f"{baseline.composite:.0f}" if baseline.composite is not None else "n/a")
    with st.expander("Baseline scorecard"):
        render_scorecard(baseline, show_formulas=False)

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

tl = st.number_input("Solver time limit (s) per scenario", min_value=10, max_value=600, value=default_tl, step=10)
selected = st.multiselect(
    "Scenarios to generate",
    options=[s["id"] for s in SCENARIOS],
    default=["A", "D"],
    format_func=lambda i: next(s["name"] for s in SCENARIOS if s["id"] == i),
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


if st.button("Generate selected scenarios", type="primary", disabled=not selected):
    if baseline_cal.empty:
        st.error("Need a baseline calendar first.")
    else:
        results = []
        for sid in selected:
            scenario = next(s for s in SCENARIOS if s["id"] == sid)
            with st.status(f"Solving {scenario['name']}…", expanded=True) as status:
                st.write(scenario["intent"])
                try:
                    result = run_scenario(scenario, dd, time_limit=int(tl))
                except Exception as e:
                    status.update(label=f"{scenario['name']} failed", state="error")
                    st.exception(e)
                    continue
                if not result["ok"]:
                    status.update(label=f"{scenario['name']} — no schedule", state="error")
                    with st.expander("Solver log"):
                        st.code(result.get("log") or "(empty)", language="text")
                    continue
                try:
                    slug = save_scenario_version(scenario, result, dd)
                except ValueError as e:
                    st.error(str(e))
                    status.update(label=str(e), state="error")
                    continue
                status.update(label=f"{scenario['name']} → `{slug}`", state="complete")
                sc = result["scorecard"]
                st.metric("Composite", f"{sc.composite:.0f}" if sc.composite is not None else "n/a")
                st.markdown("**vs baseline**")
                for d in delta_narrative(baseline, sc):
                    st.write(f"- {d}")
                results.append((scenario, sc, slug))
                with st.expander("Raw solver log"):
                    st.code((result.get("log") or "")[-4000:], language="text")

        if results:
            st.success("Done. Open **Version Compare** to inspect side-by-side and add pros/cons.")

st.divider()
st.subheader("Saved versions")
for v in list_versions(dd):
    sc = (v.get("scorecard") or {}).get("composite")
    st.write(f"- **{v.get('name')}** (`{v['slug']}`) · composite={sc} · {v.get('source', '')}")
