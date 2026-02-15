# pages/run_solver.py — Execute scheduler and show progress / KPIs.

from __future__ import annotations
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir

st.header("Run Solver")
st.caption("Review inputs and launch the scheduling optimizer.")

dd = data_dir()
scheduler_py = BASE_DIR / "phase2_scheduler.py"
python_exe = sys.executable

# ── Input summary ───────────────────────────────────────────────────────
def _count_csv(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return max(0, len(pd.read_csv(path)))
    except Exception:
        return 0

dem_count = _count_csv(dd / "DemandPlan.csv")
trial_count = _count_csv(dd / "trials.csv")
dt_count = _count_csv(dd / "Downtimes.csv")
caps_path = dd / "Capabilities & Rates.csv"
line_count = 0
if caps_path.exists():
    try:
        line_count = pd.read_csv(caps_path)["line_id"].nunique()
    except Exception:
        pass

col1, col2, col3, col4 = st.columns(4)
col1.metric("Orders", dem_count)
col2.metric("Lines", line_count)
col3.metric("Trials", trial_count)
col4.metric("Downtimes", dt_count)

st.divider()

# ── Solver options ──────────────────────────────────────────────────────
col_a, col_b = st.columns(2)
with col_a:
    two_phase = st.checkbox("Two-phase solve (Week-0 then Week-1)", value=True)
    validate = st.checkbox("Run validation after solve", value=True)
with col_b:
    objective = st.selectbox("Objective mode", ["balanced", "min-changeovers", "spread-load"], index=0)

# ── Run button ──────────────────────────────────────────────────────────
if st.button("Run Solver", type="primary", use_container_width=True):
    toml_path = dd.parent / "flowstate.toml"
    if not toml_path.exists():
        toml_path = dd / "flowstate.toml"
    cmd = [
        python_exe, str(scheduler_py),
        "--data-dir", str(dd),
        "--objective", objective,
        "--config", str(toml_path),
    ]
    if two_phase:
        cmd.append("--two-phase")
    if validate:
        cmd.append("--validate")

    err_file = dd / "solver_error.txt"
    kpi_file = dd / "solver_kpis.txt"

    # Clear old files
    for f in [err_file, kpi_file]:
        if f.exists():
            f.unlink()

    st.session_state["schedule_source"] = "solver"

    with st.status("Solving...", expanded=True) as status:
        st.write(f"Command: `{' '.join(cmd)}`")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(BASE_DIR),
        )
        output_area = st.empty()
        log_lines: list[str] = []

        # Poll for completion
        while proc.poll() is None:
            time.sleep(2)
            # Read solver_error.txt for progress
            if err_file.exists():
                try:
                    lines = err_file.read_text(encoding="utf-8").strip().split("\n")
                    if lines != log_lines:
                        log_lines = lines
                        output_area.code("\n".join(log_lines), language="text")
                except Exception:
                    pass

        # Final read
        rc = proc.returncode
        if err_file.exists():
            log_lines = err_file.read_text(encoding="utf-8").strip().split("\n")
            output_area.code("\n".join(log_lines), language="text")

        if rc == 0:
            status.update(label="Solver finished", state="complete")
        else:
            status.update(label=f"Solver exited with code {rc}", state="error")

    # Show KPIs
    if kpi_file.exists():
        kpi_text = kpi_file.read_text(encoding="utf-8").strip()
        st.info(f"**Result:** {kpi_text}")

    # Show validation report
    val_path = dd / "validation_report.txt"
    if validate and val_path.exists():
        with st.expander("Validation report", expanded=True):
            st.code(val_path.read_text(encoding="utf-8"), language="text")

    schedule_path = dd / "schedule_phase2.csv"
    if schedule_path.exists():
        st.success("Schedule generated. Go to **Schedule Viewer** to see results.")
