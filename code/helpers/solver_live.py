# helpers/solver_live.py — planner-facing narration for a running solve.
#
# Pure formatting over the solver's own telemetry (solver_progress.json,
# written by code/solver/solver_progress.py, and the solver_error.txt log —
# misleading name, it IS the run log). Every number shown comes straight
# from those files; nothing here estimates or invents. generate.py calls
# these from its ~2s poll on BOTH the attended path (run_scenario) and the
# reattach path (resume_scenario), so the two render identically.

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from solver.solver_progress import fmt_kg  # single source for kg formatting

__all__ = [
    "fmt_kg", "fmt_dur", "stage_banner", "data_chips", "live_caption",
    "solution_feed", "trust_lines", "journal_lines", "journal_from_dir",
]


def fmt_dur(seconds: float) -> str:
    """Compact duration: 45 -> '45s', 993 -> '16m 33s', 4080 -> '1h 08m'."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _active_stage(prog: dict | None) -> dict | None:
    for s in (prog or {}).get("stages") or []:
        if s.get("status") == "active":
            return s
    return None


def stage_banner(prog: dict | None, fill_mode: bool = False) -> str:
    """One planner-language sentence for what the solver is doing right now.

    Fill-mode (Scenario F) copy states the pass mechanics the pipeline
    actually implements: pass 1's two-tier reward + week gradient, the
    anchor step, pass 2's fill floor (the floor pct is read from the stage
    detail the solver wrote — never assumed).
    """
    stages = (prog or {}).get("stages") or []
    act = _active_stage(prog)
    if act is None:
        if stages and all(s.get("status") == "done" for s in stages):
            return "Solver finished — collecting and scoring the result."
        err = next((s for s in stages if s.get("status") == "error"), None)
        if err is not None:
            d = err.get("detail", "")
            return (f"{err.get('label', 'A stage')} hit a problem"
                    + (f" — {d}" if d else "") + ".")
        return "Waiting for the solver to start writing telemetry…"
    sid = str(act.get("id", ""))
    detail = str(act.get("detail", ""))
    if sid == "loading_data":
        return "Reading the staged inputs — lines, demand, rates, changeover matrix."
    if sid.startswith("building_model"):
        return ("Building the optimization model"
                + (f" — {detail}" if detail else "") + ".")
    if sid.startswith("solving"):
        if fill_mode:
            d = detail.lower()
            if "anchor" in d:
                return "Locking pass 1's plan in as the starting point for pass 2."
            if "pass 2" in d:
                m = re.search(r"floored at ([\d.]+%)", detail)
                floor = m.group(1) if m else "pass 1's level"
                return (f"Pass 2 — same fill (floored at {floor}), now "
                        "re-sequencing to cut FFS & topload changeovers.")
            return ("Pass 1 — filling open line-time with netted demand: "
                    "most kg on the right ISO week, nearest weeks first.")
        label = act.get("label") or "Solving"
        return f"{label}" + (f" — {detail}" if detail else "") + "."
    if sid == "writing_output":
        return "Writing the schedule and KPIs to disk."
    if sid == "validating":
        return ("Validating the plan against the hard rules — capacity, "
                "changeovers, CIP windows.")
    label = act.get("label") or sid
    return f"{label}" + (f" — {detail}" if detail else "") + "."


def data_chips(prog: dict | None, fill_mode: bool = False) -> str:
    """'14 lines · 144 orders · 72 SKUs · 3.33M kg netted demand · …' from
    the solver's own data_summary (empty string until it exists)."""
    ds = (prog or {}).get("data_summary") or {}
    bits: list[str] = []
    if ds.get("lines"):
        bits.append(f"{ds['lines']} lines")
    if ds.get("orders"):
        bits.append(f"{ds['orders']} orders")
    if ds.get("skus"):
        bits.append(f"{ds['skus']} SKUs")
    if ds.get("total_demand_kg"):
        noun = "netted demand" if fill_mode else "demand"
        bits.append(f"{fmt_kg(ds['total_demand_kg'])} {noun}")
    if ds.get("horizon_h"):
        h = int(ds["horizon_h"])
        bits.append(f"{h // 168}-week horizon ({h}h)" if h % 168 == 0
                    else f"{h}h horizon")
    if ds.get("changeover_pairs"):
        bits.append(f"{ds['changeover_pairs']:,} changeover pairs")
    return " · ".join(bits)


def live_caption(prog: dict | None, elapsed_s: float) -> str:
    """The one-line status caption (kept from the original UI): elapsed ·
    active stage · gap · latest solution label."""
    bits = [f"{fmt_dur(elapsed_s)} elapsed"]
    if prog:
        act = _active_stage(prog)
        if act:
            d = act.get("detail", "")
            bits.append(act.get("label", "") + (f" — {d}" if d else ""))
        stats = prog.get("solver_stats") or {}
        if stats.get("gap_pct") is not None:
            bits.append(f"gap {stats['gap_pct']}%")
        sols = prog.get("solutions") or []
        if sols:
            bits.append(str(sols[-1].get("label", "")))
    return " · ".join(b for b in bits if b)


def solution_feed(prog: dict | None, limit: int = 8) -> list[str]:
    """Newest-first feed lines: clock time + time-into-search + the writer's
    pass-aware label. wall_time restarts per pass; the label says which."""
    sols = (prog or {}).get("solutions") or []
    out: list[str] = []
    for s in reversed(sols[-limit:]):
        head_bits = []
        ts = str(s.get("ts") or "")
        if len(ts) >= 19:
            head_bits.append(ts[11:19])
        wt = s.get("wall_time")
        if wt is not None:
            head_bits.append(f"t+{fmt_dur(wt)}")
        head = " · ".join(head_bits)
        label = str(s.get("label", ""))
        out.append(f"{head} — {label}" if head else label)
    return out


def trust_lines(prog: dict | None, now: datetime | None = None) -> list[str]:
    """Trust indicators derived from existing stats — never invented.

    - time since the last incumbent (each solution entry carries a wall-clock
      ts written by the solver);
    - the bound in words. gap_pct is 100*|obj-bound|/max(1,|obj|): in a
      maximize pass the bound is a CEILING (no plan more than gap% better
      can exist), in a minimize pass a FLOOR. A huge gap means the PROOF is
      loose — CP-SAT hasn't tightened the bound — not that the plan is bad,
      and the wording must say exactly that.
    """
    out: list[str] = []
    stats = (prog or {}).get("solver_stats") or {}
    sols = (prog or {}).get("solutions") or []
    status = str(stats.get("status") or "")
    now = now or datetime.now()
    if sols and status in ("SOLVING", "STARTING"):
        ts = sols[-1].get("ts")
        age = None
        if ts:
            try:
                age = (now - datetime.fromisoformat(str(ts))).total_seconds()
            except (TypeError, ValueError):
                age = None
        if age is not None and age >= 0:
            if age >= 60:
                out.append("still searching — best plan unchanged "
                           f"for {fmt_dur(age)}")
            else:
                out.append(f"improving — better plan found {fmt_dur(age)} ago")
    if status == "OPTIMAL":
        out.append("proven optimal — no better plan exists under these "
                   "rules and inputs")
        return out
    gap = stats.get("gap_pct")
    direction = str(stats.get("direction") or "")
    if gap is None or stats.get("best_objective") is None or status != "SOLVING":
        return out
    g = float(gap)
    if direction == "max":
        if g <= 50:
            out.append(f"proof ceiling: no plan more than {g:.0f}% better "
                       "than this one exists")
        else:
            out.append(f"proof ceiling still loose ({g:.0f}%) — the solver "
                       "can't yet say how close to perfect this plan is; "
                       "that's about the proof, not the plan")
    elif direction == "min":
        noun = (" on changeovers" if str(stats.get("pass_id") or "") == "co"
                else "")
        if g <= 50:
            out.append(f"proof floor: no plan more than {g:.0f}% "
                       f"cheaper{noun} exists")
        else:
            out.append(f"proof floor still loose ({g:.0f}%) — the "
                       "theoretical best isn't pinned down yet; the plan "
                       "itself only ever improves from here")
    return out


# Log lines a planner can follow: data load, model size, warm start, relax
# ladder, two-pass floor/anchor/adopt, validation, final status.
_JOURNAL_MARKERS = (
    "[data]", "[model]", "[warm-start]", "[auto-relax]", "[two-pass]",
    "[two-phase]", "[soft-demand]", "[cross-week]", "[greedy", "[validate]",
    "[current state]", "[input patch]", "SOLVER level=", "START ",
    "Idle KPIs:", "Status:", "FATAL",
)


def journal_lines(text: str, limit: int = 40) -> list[str]:
    """Tail of the solver log filtered to the meaningful machinery lines."""
    keep = [ln.rstrip() for ln in (text or "").splitlines()
            if ln.strip() and any(m in ln for m in _JOURNAL_MARKERS)]
    return keep[-limit:]


def journal_from_dir(work_dir: Path, limit: int = 40) -> list[str]:
    """journal_lines over the work dir's solver_error.txt (the run log)."""
    p = Path(work_dir) / "solver_error.txt"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return journal_lines(text, limit)
