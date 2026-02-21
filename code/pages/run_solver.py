# pages/run_solver.py — Configure settings and launch the scheduling optimizer.

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
st.caption("Configure parameters and launch the scheduling optimizer.")

dd = data_dir()
scheduler_py = BASE_DIR / "phase2_scheduler.py"
python_exe = sys.executable

# ── TOML helpers ─────────────────────────────────────────────────────────
def _load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _save_toml(path: Path, cfg: dict) -> None:
    import tomli_w
    with open(path, "wb") as f:
        tomli_w.dump(cfg, f)


toml_path = dd.parent / "flowstate.toml"
if not toml_path.exists():
    toml_path = dd / "flowstate.toml"

cfg = _load_toml(toml_path)
sched = cfg.get("scheduler", {})
cip_cfg = cfg.get("cip", {})
obj = cfg.get("objective", {})
co_cfg = cfg.get("changeover", {})

# ── Input summary ─────────────────────────────────────────────────────────
def _count_csv(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return max(0, len(pd.read_csv(path)))
    except Exception:
        return 0

dem_count = _count_csv(dd / "demand_plan.csv")
trial_count = _count_csv(dd / "trials.csv")
dt_count = _count_csv(dd / "downtimes.csv")
caps_path = dd / "capabilities_rates.csv"
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

# ── Settings form (inline) ────────────────────────────────────────────────
with st.expander("Scheduler settings", expanded=True):
    st.caption("Changes are saved to `flowstate.toml` when you click **Run Solver**.")

    st.subheader("Solver")
    c1, c2, c3 = st.columns(3)
    with c1:
        time_limit = st.number_input(
            "Time limit (s)",
            value=sched.get("time_limit", 120),
            min_value=10, max_value=3600, step=10,
            help=(
                "How long the solver searches before returning the best solution found. "
                "**120 s** is a good default for most problems. "
                "Increase to **300–600 s** for large or complex schedules where solution quality matters. "
                "Longer runs rarely improve a solution that has already found the global optimum."
            ),
        )
        min_run = st.number_input(
            "Min run hours",
            value=sched.get("min_run_hours", 4),
            min_value=1, max_value=24, step=1,
            help=(
                "Minimum consecutive production hours the solver will assign to a line/order pair. "
                "**Suggested: 4 h.** Lower values (e.g. 2 h) allow more flexibility but produce more "
                "changeovers. Higher values (e.g. 8 h) consolidate runs but may leave some demand unmet "
                "if capacity is tight."
            ),
        )
    with c2:
        max_lines = st.number_input(
            "Max lines per order",
            value=sched.get("max_lines_per_order", 3),
            min_value=1, max_value=14, step=1,
            help=(
                "Maximum number of lines that can simultaneously produce the same SKU. "
                "**Suggested: 2–3.** Higher values spread load across lines but generate more changeovers. "
                "Set to 1 to force each SKU onto a single line."
            ),
        )
        planning_start = st.text_input(
            "Planning start date",
            value=sched.get("planning_start_date", "2026-02-15 00:00:00"),
            help=(
                "Anchor timestamp for hour 0 of the planning horizon (format: YYYY-MM-DD HH:MM:SS). "
                "All start/end hours in every CSV are offsets from this point. "
                "The line rates table is also filtered by the month of this date."
            ),
        )
    with c3:
        validate_after = st.checkbox(
            "Validate after solve",
            value=sched.get("validate", True),
            help=(
                "Run post-solve checks (demand bounds, no overlaps, CIP spacing, changeover timing) "
                "and write a `validation_report.txt`. Recommended — adds only a few seconds."
            ),
        )

    st.subheader("Rate source")
    use_sku_rates = st.toggle(
        "Enable SKU-specific rates",
        value=sched.get("use_sku_rates", False),
    )
    if use_sku_rates:
        st.warning(
            "SKU-specific rates are based on the historical running rate for each line "
            "and each SKU. This may affect the feasibility of meeting the demand plan, "
            "but it may also be more accurate to 'real life'."
        )
    else:
        st.caption(
            "Using Demand Planning line rates (`line_rates.csv`) by default. "
            "Enable the toggle above to use per-SKU rates from `capabilities_rates.csv`."
        )

    st.subheader("CIP")
    c4, c5 = st.columns(2)
    with c4:
        cip_interval = st.number_input(
            "CIP interval — global fallback (hours)",
            value=cip_cfg.get("interval_h", 120),
            min_value=24, max_value=336, step=1,
            help=(
                "Maximum consecutive production hours before a CIP is required, used for any line "
                "not listed in `line_cip_hrs.csv`. "
                "**Typical range: 96–144 h.** Per-line intervals in `line_cip_hrs.csv` take priority. "
                "Shorter intervals mean more CIPs and less available production time."
            ),
        )
    with c5:
        cip_duration = st.number_input(
            "CIP duration (hours)",
            value=cip_cfg.get("duration_h", 6),
            min_value=1, max_value=24, step=1,
            help=(
                "Length of each CIP block. **Typical: 4–8 h.** "
                "Every scheduled CIP removes this many hours from a line's available production time. "
                "This setting applies to all lines — adjust per-line cleaning needs in `line_cip_hrs.csv`."
            ),
        )

    st.subheader("Objective weights")
    st.caption(
        "Weights control the trade-offs in the solver's optimization. "
        "All weights are unit-less scalars — only their ratios matter. "
        "Doubling one weight is equivalent to halving all others."
    )
    c6, c7, c8 = st.columns(3)
    with c6:
        w_makespan = st.number_input(
            "Makespan weight",
            value=obj.get("makespan_weight", 1),
            min_value=0, max_value=10000, step=1,
            help=(
                "Penalty (per hour) on the time from the first start to the last end across all lines. "
                "**Suggested: 1–5.** Raise this to compress the schedule and finish all production sooner. "
                "Setting to 0 ignores makespan entirely (useful when changeover reduction is the sole goal)."
            ),
        )
    with c7:
        w_changeover = st.number_input(
            "Changeover weight",
            value=obj.get("changeover_weight", 100),
            min_value=0, max_value=10000, step=10,
            help=(
                "Multiplier on the total weighted changeover cost across all lines. "
                "**Suggested: 50–200.** Higher values strongly discourage SKU switches. "
                "At 100 a single changeover costs as much as 100 extra hours of makespan — "
                "increase to 500+ if reducing changeovers is the primary goal."
            ),
        )
    with c8:
        w_cip_defer = st.number_input(
            "CIP defer weight",
            value=obj.get("cip_defer_weight", 10),
            min_value=0, max_value=10000, step=1,
            help=(
                "Reward (per hour of CIP start time) for pushing CIPs as late as the interval allows. "
                "**Suggested: 5–20.** Higher values bunch CIPs near their deadline so production runs "
                "are longer. Set to 0 if CIP placement doesn't matter."
            ),
        )

    st.subheader("Machine changeover weights")
    st.caption(
        "Per-changeover cost is built from these weights based on which machine components "
        "change between two consecutive SKUs. The **Changeover weight** above is a global "
        "multiplier on the sum of all per-changeover costs."
    )
    with st.expander("How the changeover cost is calculated"):
        st.markdown(
            "```\n"
            "Per-changeover cost =\n"
            "  base_changeover_weight\n"
            "  + topload_weight  x  topload_change\n"
            "  + ttp_weight      x  ttp_change\n"
            "  + ffs_weight      x  ffs_change\n"
            "  + casepacker_weight x casepacker_change\n"
            "  + conv_org_weight x  conv_to_org_change\n"
            "  + cinn_weight     x  cinn_to_non\n"
            "  + flavor_weight   x  added_flavors\n"
            "\n"
            "Total objective cost =\n"
            "  changeover_weight  x  SUM(all per-changeover costs)\n"
            "```\n\n"
            "Set **base_changeover_weight = 0** to only penalize transitions "
            "that involve an actual machine component change (topload, TTP, FFS, "
            "or casepacker). Leave it > 0 to add a flat cost for every SKU-to-SKU switch."
        )

    cm1, cm2, cm3, cm4, cm5 = st.columns(5)
    with cm1:
        w_base_co = st.number_input(
            "Base changeover",
            value=co_cfg.get("base_changeover_weight", 5),
            min_value=0, max_value=10000, step=1,
            help=(
                "Flat cost added to every SKU-to-SKU transition regardless of type. "
                "Set to 0 to only penalize machine-specific changes."
            ),
        )
    with cm2:
        w_topload = st.number_input(
            "Topload weight",
            value=co_cfg.get("topload_weight", 50),
            min_value=0, max_value=10000, step=5,
            help="Penalty for topload format changes (heaviest mechanical change).",
        )
    with cm3:
        w_ttp = st.number_input(
            "TTP weight",
            value=co_cfg.get("ttp_weight", 10),
            min_value=0, max_value=10000, step=5,
            help="Penalty for TTP station changes.",
        )
    with cm4:
        w_ffs = st.number_input(
            "FFS weight",
            value=co_cfg.get("ffs_weight", 10),
            min_value=0, max_value=10000, step=5,
            help="Penalty for form-fill-seal changes.",
        )
    with cm5:
        w_casepacker = st.number_input(
            "Casepacker weight",
            value=co_cfg.get("casepacker_weight", 10),
            min_value=0, max_value=10000, step=5,
            help="Penalty for casepacker changes.",
        )

    st.subheader("Special changeover penalties")
    st.caption(
        "Additional penalties for specific product transitions "
        "flagged in `changeovers.csv`."
    )
    c9, c10, c11 = st.columns(3)
    with c9:
        w_conv_org = st.number_input(
            "Conv → Organic penalty",
            value=obj.get("co_conv_org_weight", co_cfg.get("conv_org_weight", 30)),
            min_value=0, max_value=10000, step=5,
            help=(
                "Extra penalty when `conv_to_org_change = 1` in `changeovers.csv` "
                "(conventional → organic recipe, requires flush & rinse, typically 1–2 h). "
                "**Suggested: 20–50.**"
            ),
        )
    with c10:
        w_cinn = st.number_input(
            "Cinnamon → Non-Cinn penalty",
            value=obj.get("co_cinn_weight", co_cfg.get("cinn_weight", 20)),
            min_value=0, max_value=10000, step=5,
            help=(
                "Extra penalty when `cinn_to_non = 1` in `changeovers.csv` "
                "(cinnamon → non-cinnamon flavor, requires flush, typically ~1 h). "
                "**Suggested: 15–30.**"
            ),
        )
    with c11:
        w_flavor = st.number_input(
            "Added-flavor penalty (per flavor)",
            value=obj.get("co_flavor_weight", co_cfg.get("flavor_weight", 5)),
            min_value=0, max_value=1000, step=1,
            help=(
                "Penalty per unit of `added_flavors` in `changeovers.csv`. "
                "**Suggested: 3–10.** At 5, going from 1 to 4 flavors adds 15 penalty "
                "(3 additional flavors x 5)."
            ),
        )

# ── Solve options ─────────────────────────────────────────────────────────
st.divider()
col_a, col_b = st.columns(2)
with col_a:
    two_phase = st.checkbox(
        "Two-phase solve (Week-0 then Week-1)",
        value=True,
        help="Solve Week-0 first, then use the resulting line states as the starting point for Week-1. Recommended — produces tighter schedules than solving both weeks at once.",
    )
with col_b:
    objective = st.selectbox(
        "Objective mode",
        ["balanced", "min-changeovers", "spread-load"],
        index=0,
        help=(
            "**balanced** — minimize makespan + weighted changeovers (recommended). "
            "**min-changeovers** — minimize SKU switches above all else. "
            "**spread-load** — equalize production hours across lines."
        ),
    )

# ── Run button ────────────────────────────────────────────────────────────
if st.button("Run Solver", type="primary", use_container_width=True):
    # Save settings to TOML before launching
    cfg["scheduler"] = {
        "time_limit": int(time_limit),
        "min_run_hours": int(min_run),
        "max_lines_per_order": int(max_lines),
        "planning_start_date": planning_start,
        "validate": validate_after,
        "use_sku_rates": use_sku_rates,
    }
    cfg["cip"] = {
        "interval_h": int(cip_interval),
        "duration_h": int(cip_duration),
    }
    cfg["objective"] = {
        "makespan_weight": int(w_makespan),
        "changeover_weight": int(w_changeover),
        "cip_defer_weight": int(w_cip_defer),
        "co_conv_org_weight": int(w_conv_org),
        "co_cinn_weight": int(w_cinn),
        "co_flavor_weight": int(w_flavor),
    }
    cfg["changeover"] = {
        "base_changeover_weight": int(w_base_co),
        "topload_weight": int(w_topload),
        "ttp_weight": int(w_ttp),
        "ffs_weight": int(w_ffs),
        "casepacker_weight": int(w_casepacker),
        "conv_org_weight": int(w_conv_org),
        "cinn_weight": int(w_cinn),
        "flavor_weight": int(w_flavor),
    }
    _save_toml(toml_path, cfg)

    cmd = [
        python_exe, str(scheduler_py),
        "--data-dir", str(dd),
        "--objective", objective,
        "--config", str(toml_path),
    ]
    if two_phase:
        cmd.append("--two-phase")
    if validate_after:
        cmd.append("--validate")

    import json as _json

    err_file = dd / "solver_error.txt"
    kpi_file = dd / "solver_kpis.txt"
    progress_file = dd / "solver_progress.json"

    for f in [err_file, kpi_file, progress_file]:
        if f.exists():
            f.unlink()

    st.session_state["schedule_source"] = "solver"
    for key in ["sb_schedule", "sb_cips", "sb_holding"]:
        st.session_state.pop(key, None)

    # ── Status icons for the pipeline graphic ────────────────────────────
    _STAGE_ICON = {
        "pending": "\u23F3",   # hourglass
        "active":  "\U0001F7E1",  # yellow circle
        "done":    "\u2705",   # green check
        "error":   "\u274C",   # red X
    }

    def _read_progress() -> dict:
        if not progress_file.exists():
            return {}
        try:
            return _json.loads(progress_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _render_pipeline(container, stages: list[dict]) -> None:
        if not stages:
            return
        cols = container.columns(len(stages))
        for col, s in zip(cols, stages):
            icon = _STAGE_ICON.get(s.get("status", "pending"), "\u23F3")
            label = s.get("label", s.get("id", ""))
            detail = s.get("detail", "")
            if s.get("status") == "active":
                col.markdown(f"**{icon} {label}**")
            else:
                col.markdown(f"{icon} {label}")
            if detail:
                col.caption(detail)

    def _render_metrics(container, stats: dict, data_summary: dict) -> None:
        elapsed = stats.get("elapsed_s", 0)
        best_obj = stats.get("best_objective")
        bound = stats.get("best_bound")
        gap = stats.get("gap_pct")
        tl = stats.get("time_limit_s", 0)

        m1, m2, m3, m4 = container.columns(4)
        elapsed_str = f"{int(elapsed)}s" if elapsed else "—"
        if tl:
            elapsed_str += f" / {int(tl)}s"
        m1.metric("Elapsed", elapsed_str)
        m2.metric("Best Objective", f"{best_obj:,.0f}" if best_obj is not None else "—")
        m3.metric("Bound", f"{bound:,.0f}" if bound is not None else "—")
        m4.metric("Gap", f"{gap:.1f}%" if gap is not None else "—")

        if data_summary:
            info_parts = []
            if data_summary.get("lines"):
                info_parts.append(f"{data_summary['lines']} lines")
            if data_summary.get("orders"):
                info_parts.append(f"{data_summary['orders']} orders")
            if data_summary.get("skus"):
                info_parts.append(f"{data_summary['skus']} SKUs")
            if data_summary.get("horizon_h"):
                info_parts.append(f"{data_summary['horizon_h']}h horizon")
            if info_parts:
                container.caption("Data: " + ", ".join(info_parts))

    def _render_solutions(container, solutions: list[dict]) -> None:
        if not solutions:
            return
        container.markdown("**Solutions found**")
        rows = []
        for s in solutions:
            rows.append({
                "Time (s)": s.get("wall_time", 0),
                "Objective": f"{s.get('objective', 0):,.0f}",
                "Note": s.get("label", ""),
            })
        container.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )

    # ── Launch subprocess and poll ───────────────────────────────────────
    with st.status("Solving...", expanded=True) as status:
        st.caption(f"`{' '.join(cmd)}`")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(BASE_DIR),
        )

        pipeline_ph = st.empty()
        metrics_ph = st.empty()
        solutions_ph = st.empty()
        raw_ph = st.empty()

        log_lines: list[str] = []
        prev_progress: dict = {}

        while proc.poll() is None:
            time.sleep(1.5)

            # Read structured progress
            prog = _read_progress()
            if prog and prog != prev_progress:
                prev_progress = prog
                with pipeline_ph.container():
                    _render_pipeline(st, prog.get("stages", []))
                with metrics_ph.container():
                    _render_metrics(st, prog.get("solver_stats", {}), prog.get("data_summary", {}))
                with solutions_ph.container():
                    _render_solutions(st, prog.get("solutions", []))

            # Read raw log
            if err_file.exists():
                try:
                    lines = err_file.read_text(encoding="utf-8").strip().split("\n")
                    if lines != log_lines:
                        log_lines = lines
                        with raw_ph.container():
                            with st.expander("Raw solver log", expanded=False):
                                st.code("\n".join(log_lines), language="text")
                except Exception:
                    pass

        # Final read after process exits
        rc = proc.returncode
        prog = _read_progress()
        if prog:
            with pipeline_ph.container():
                _render_pipeline(st, prog.get("stages", []))
            with metrics_ph.container():
                _render_metrics(st, prog.get("solver_stats", {}), prog.get("data_summary", {}))
            with solutions_ph.container():
                _render_solutions(st, prog.get("solutions", []))

        if err_file.exists():
            log_lines = err_file.read_text(encoding="utf-8").strip().split("\n")
            with raw_ph.container():
                with st.expander("Raw solver log", expanded=False):
                    st.code("\n".join(log_lines), language="text")

        if rc == 0:
            status.update(label="Solver finished", state="complete")
        else:
            status.update(label=f"Solver exited with code {rc}", state="error")

    if kpi_file.exists():
        kpi_text = kpi_file.read_text(encoding="utf-8").strip()
        st.info(f"**Result:** {kpi_text}")

    val_path = dd / "validation_report.txt"
    if validate_after and val_path.exists():
        with st.expander("Validation report", expanded=True):
            st.code(val_path.read_text(encoding="utf-8"), language="text")

    schedule_path = dd / "schedule_phase2.csv"
    if schedule_path.exists():
        st.success("Schedule generated. Go to **Schedule Viewer** to see results.")
