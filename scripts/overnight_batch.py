#!/usr/bin/env python3
"""Overnight batch engine — Phase 1 of "Continuously Optimize".

One nightly invocation (Task Scheduler, 19:00) runs a sequential portfolio of
Scenario F solves, scores every candidate with the frozen overnight_score v1
(helpers/overnight_score.py), maintains a per-generation leaderboard under
data/optimizer/<gen_id>/, publishes the top-1 and runner-up as fixed-slug
versions (overnight_best / overnight_runner_up), waits for ~05:00, runs one
final champion consolidation on the freshest data, and writes the morning
brief (data/optimizer/brief.md) plus the data/optimizer/latest.json pointer.
NOTHING here ever promotes to the official board — publishing means sandbox
versions the planner can inspect on Compare & Promote.

Task Scheduler registration (do this from the MAIN checkout, after merge):
    powershell -ExecutionPolicy Bypass -File scripts\\register_overnight.ps1
creates the task "Flowstate Overnight Optimizer": daily 19:00, venv python
running this script, working dir = repo root. Remove it with:
    powershell -ExecutionPolicy Bypass -File scripts\\unregister_overnight.ps1

Design decisions (user-approved, final):
  * machine stays on all night; solves run SEQUENTIALLY via run_scenario's
    headless path, each in its OWN work dir (data/_overnight_work/
    F_overnight_<label>) so a planner's daytime run in _scenario_work/F is
    never clobbered — the scenario id drives the work dir in run_scenario,
    and run_scenario's work_root argument moves the whole batch out of the
    shared _scenario_work root the UI and the solved-dir probes scan (an
    overnight arm must never read as "the run that solved last");
  * the batch process drops itself to BELOW_NORMAL priority; on Windows a
    child created by a BELOW_NORMAL/IDLE parent INHERITS that priority class
    (CreateProcess docs), so every solver subprocess run_scenario spawns runs
    BELOW_NORMAL too and the box stays usable;
  * pass 1 and pass 2 get SEPARATE budgets: pass 1 rides run_scenario's
    time_limit, pass 2 rides scheduler.time_limit_pass2 written into the work
    toml by the per-arm input patch (phase2_scheduler reads it);
  * EVERY solve round ingests a FRESH stock check first (stockcheck.api via
    the same resolver the agent uses) — a SKU whose components ran short
    mid-night gets its demand capped in every later round (trim_dns_demand);
  * generations: gen_id = "<YYYYMMDD-HHMM>-<8 hex of sha1(inputs signature)>".
    The signature (generation_signature: scorecard_engine's shared
    scoring_inputs_signature PLUS the board and the week lock, which the fill
    staging also reads) is re-checked after every arm; a change — or a
    rolling-anchor roll at midnight — closes the generation and stages a new
    one (fresh staging + stock check + capacity bound). Old candidates stay in
    their own generation dir;
  * the fill denominator (min(net demand, capacity bound)) and the scoring
    frame (gates, ISO week marks) are computed ONCE per generation at staging
    and stored in the leaderboard — every candidate in a generation divides
    by the same bound;
  * data/optimizer/steering.json (optional, <24h fresh) replaces the two
    EXPLORATION arms; champion + noise arms always run regardless;
  * publishing uses fixed slugs so updates never consume rolling version
    slots; on first-ever creation at full capacity the error is caught and
    reported in the brief instead of crashing;
  * any arm failing records a failed candidate and the batch continues — it
    never dies mid-night on one bad arm. Timestamped log:
    data/optimizer/batch.log.

latest.json paths are relative to data/optimizer/.

Usage:
    python scripts/overnight_batch.py                 # the real nightly run
    python scripts/overnight_batch.py --dry-run       # tiny budgets (60/120s),
                                                      # small portfolio, no
                                                      # publish unless --publish
    python scripts/overnight_batch.py --skip-pull     # no fs-live-pull (tests)
    python scripts/overnight_batch.py --pass1-s 120 --pass2-s 300
                                                      # override EVERY arm's
                                                      # budgets (small runs)
    python scripts/overnight_batch.py --no-publish    # never publish, beats
                                                      # every other flag
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

import pandas as pd  # noqa: E402

from helpers.agent_policy import dns_ratios, trim_dns_demand  # noqa: E402
from helpers.calendar_io import load_calendar, save_calendar  # noqa: E402
from helpers.config import load_toml  # noqa: E402
from helpers.horizon import resolve as resolve_horizon  # noqa: E402
from helpers.overnight_score import (capacity_bound_kg, demand_orders,  # noqa: E402
                                     iso_week_marks, overnight_score,
                                     solver_fill_blocks, _normalize)
from helpers.plan_fill import pinned_blocks  # noqa: E402
from helpers.reconcile_engine import stock_report_inputs  # noqa: E402
from helpers.safe_io import safe_write_json  # noqa: E402
from helpers.scenario_runner import (SCENARIOS, _overlay_fill,  # noqa: E402
                                     _prepare_work_dir,
                                     _set_work_scheduler_flag, run_scenario)
from helpers.scorecard_engine import (_co_lookup, _load_changeovers,  # noqa: E402
                                      score_calendar,
                                      scoring_inputs_signature)
from helpers.version_manager import upsert_version  # noqa: E402
from helpers.week_lock import locked_through_h, read_lock  # noqa: E402
from stockcheck.api import stock_check_report  # noqa: E402

DATA = ROOT / "data"
OPT_DIR = DATA / "optimizer"
# The batch owns a work root of its OWN, outside the shared _scenario_work the
# UI and the solved-dir probes scan ("*/schedule_phase2.csv", "*/mo_changes.csv").
# Sharing that root made every overnight arm look like "the run that solved
# last": Compare's plant write-back picker defaulted to a 3am sandbox solve and
# the constraint probes asserted against it instead of the planner's scenario.
WORK_ROOT = "_overnight_work"
STAGING_ID = "F_overnight_stage"
BEST_SLUG = "overnight_best"
RUNNER_SLUG = "overnight_runner_up"
CHAMPION_BUDGETS = {"pass1_s": 600, "pass2_s": 2400}
DRY_BUDGETS = {"pass1_s": 60, "pass2_s": 120}
STEERING_FRESH_H = 24.0
# subprocess kill ceiling: pass1 + anchor solve (<=120s) + pass2, plus slack
# for staging, model builds (two of them) and output writing.
TIMEOUT_SLACK_S = 600.0

F_SCENARIO = next(s for s in SCENARIOS if s["id"] == "F")


# ---------------------------------------------------------------------------
# small pure helpers (unit-tested in tests/test_overnight_batch.py)
# ---------------------------------------------------------------------------

def gen_id_for(sig: tuple, now: datetime) -> str:
    """"<YYYYMMDD-HHMM>-<first 8 hex of sha1 of the scoring signature>"."""
    digest = hashlib.sha1(repr(tuple(sig)).encode("utf-8")).hexdigest()[:8]
    return f"{now:%Y%m%d-%H%M}-{digest}"


# Files the fill staging reads that scoring_inputs_signature does NOT cover.
# scoring_inputs_signature watches data/reference/* + flowstate.toml, but a
# generation's frame is also built from the planner's own board: _overlay_fill
# nets committed/pinned board blocks out of demand and derives the per-line
# gates, and the pin guard compares against the live board. A planner still
# editing at 19:30 would otherwise leave the generation staged against a board
# that no longer exists — every later arm scored on stale netting and judged
# against pins its staging never saw.
_EXTRA_SIGNATURE_FILES = ("calendar_blocks.csv", "lock_state.json")


def generation_signature(data_dir: Path) -> tuple[float, ...]:
    """The rotation signature: the shared scoring inputs plus the board and
    the week lock (see _EXTRA_SIGNATURE_FILES)."""
    sig = list(scoring_inputs_signature(data_dir))
    for name in _EXTRA_SIGNATURE_FILES:
        try:
            sig.append((Path(data_dir) / name).stat().st_mtime)
        except OSError:
            sig.append(0.0)
    return tuple(sig)


def parse_steering(raw: Any, now: datetime) -> tuple[list[dict], list[str]]:
    """steering.json -> (replacement exploration arms, notes).

    Empty list = nothing usable (absent/stale/malformed rows). Only rows with
    a label and dict params survive; budgets default to the champion's.
    """
    notes: list[str] = []
    if not isinstance(raw, dict):
        return [], ["steering.json ignored: not an object"]
    at_raw = str(raw.get("at", "")).strip()
    try:
        at = datetime.fromisoformat(at_raw)
    except ValueError:
        return [], [f"steering.json ignored: unparseable 'at' ({at_raw!r})"]
    age_h = (now - at).total_seconds() / 3600.0
    if age_h > STEERING_FRESH_H or age_h < 0:
        return [], [f"steering.json ignored: stale ({age_h:.1f}h old)"]
    arms: list[dict] = []
    for row in raw.get("portfolio_overrides") or []:
        if not isinstance(row, dict) or not str(row.get("label", "")).strip():
            notes.append("steering row skipped: missing label")
            continue
        params = row.get("params")
        if not isinstance(params, dict):
            notes.append(f"steering row {row.get('label')} skipped: params "
                         "not a dict")
            continue
        b = row.get("budgets") or {}
        arms.append({
            "label": f"steer_{str(row['label']).strip()}"[:48],
            "kind": "exploration",
            "params": dict(params),
            "budgets": {
                "pass1_s": int(b.get("pass1_s") or CHAMPION_BUDGETS["pass1_s"]),
                "pass2_s": int(b.get("pass2_s") or CHAMPION_BUDGETS["pass2_s"]),
            },
        })
    if arms:
        notes.append(
            f"steering.json fresh ({age_h:.1f}h, by "
            f"{raw.get('generated_by', '?')}): {len(arms)} exploration arm(s) "
            "replaced")
    return arms, notes


def effective_co_weights(cfg: dict) -> dict[str, int]:
    """The changeover weights a default Scenario F solve actually runs with:
    flowstate.toml [changeover] overlaid with F's built-in overrides. The
    DAILY defaults in flowstate.toml are never modified — exploration arms
    only vary their own work-dir copies."""
    from helpers.scenario_runner import SOLVER_DEFAULTS
    keys = ("base_changeover_weight", "topload_weight", "ttp_weight",
            "ffs_weight", "casepacker_weight", "conv_org_weight",
            "cinn_weight", "flavor_weight")
    co_cfg = cfg.get("changeover") or {}
    out = {k: int(co_cfg.get(k, SOLVER_DEFAULTS.get(f"changeover.{k}", 0)))
           for k in keys}
    for k, v in (F_SCENARIO.get("overrides") or {}).items():
        if k in keys:
            out[k] = int(v)
    return out


def default_portfolio(cfg: dict, *, dry_run: bool) -> list[dict]:
    """The default arm list (2026-08-21 redesign: diversity of STARTING
    POINTS is the lever, not budgets or weights).

    Champion = current config defaults (Scenario F's own overrides on top of
    the toml — no extra params). The champion params kept reproducing the
    board because the board came from the same solver + same seed path, so
    the portfolio varies where the search STARTS:
      * champion x2 ("noise") — the noise floor;
      * seed x2               — champion params, CP-SAT random_seed 2 / 3
                                (scheduler.solver_random_seed, applied to
                                every solve pass);
      * cold x1               — champion params, warm start fully disabled:
                                no greedy seed, carried prev files removed;
      * chained x1            — warm-starts from the best candidate SO FAR
                                in the same generation; runs LAST so every
                                other arm is a possible donor.
    Ladder arms are RETIRED by default: last night (gen 20260820-1505)
    measured 600/3600s vs 300/1800s within the champion noise floor — the
    doubled budget never paid. Exploration arms exist only when
    steering.json asks (apply_steering appends them). The dry-run portfolio
    is the same composition minus one seed arm."""
    ch = dict(DRY_BUDGETS if dry_run else CHAMPION_BUDGETS)
    arms = [
        {"label": "champion_n1", "kind": "noise", "params": {},
         "budgets": dict(ch)},
        {"label": "champion_n2", "kind": "noise", "params": {},
         "budgets": dict(ch)},
        {"label": "seed_2", "kind": "seed",
         "params": {"solver_random_seed": 2}, "budgets": dict(ch)},
    ]
    if not dry_run:
        arms.append({"label": "seed_3", "kind": "seed",
                     "params": {"solver_random_seed": 3},
                     "budgets": dict(ch)})
    arms += [
        {"label": "cold_start", "kind": "cold", "params": {},
         "budgets": dict(ch), "warm_start": "none"},
        {"label": "chained_best", "kind": "chained", "params": {},
         "budgets": dict(ch), "warm_start": "chained"},
    ]
    return arms


def apply_steering(arms: list[dict], steering: list[dict]) -> list[dict]:
    """Append the steering exploration arms (the default portfolio carries
    none since the 2026-08-21 redesign); champion/noise, seed, cold and
    chained arms always run regardless."""
    if not steering:
        return arms
    return [a for a in arms if a["kind"] != "exploration"] + steering


def apply_budget_overrides(arms: list[dict], pass1_s: int | None,
                           pass2_s: int | None) -> list[dict]:
    """--pass1-s / --pass2-s override the budgets of EVERY arm — the
    small-run testing lever. None = that pass keeps its per-arm budget, so
    the flag-less nightly run is byte-identical."""
    if pass1_s is None and pass2_s is None:
        return arms
    for arm in arms:
        b = arm["budgets"]
        if pass1_s is not None:
            b["pass1_s"] = int(pass1_s)
        if pass2_s is not None:
            b["pass2_s"] = int(pass2_s)
    return arms


def best_donor(candidates: list[dict]) -> dict | None:
    """The best scored candidate SO FAR — the chained arm's warm-start
    donor. Guards are deliberately not required: a hint only steers the
    search (the solver's trust gates + CP-SAT hint repair own correctness),
    so the highest composite is the most informative starting point either
    way."""
    pool = [c for c in candidates if c.get("overnight_score")]
    if not pool:
        return None
    return max(pool, key=lambda c: float(c["overnight_score"]["composite"]))


def plant_chain_seed(work: Path, donor_work: Path) -> list[str]:
    """Copy the donor arm's SOLVED schedule into the chained arm's work dir
    as its warm start (prev_schedule.csv). feasibility_report.json rides
    along as prev_feasibility.json so the donor's input_sig (md5 of ITS
    staged inputs) and relax level reach the solver's trust gates
    unchanged: same generation = same staged bytes, so the gate accepts;
    if anything really moved between the arms the signatures differ and
    the solver honestly solves cold. The signature is CARRIED, never
    faked."""
    import shutil as _sh
    src_sched = donor_work / "schedule_phase2.csv"
    src_feas = donor_work / "feasibility_report.json"
    if not src_sched.exists():
        return [f"chain seed unavailable ({src_sched.name} missing in "
                f"{donor_work.name}) — solving cold"]
    _sh.copy2(src_sched, work / "prev_schedule.csv")
    if src_feas.exists():
        _sh.copy2(src_feas, work / "prev_feasibility.json")
        return [f"chain seed planted from {donor_work.name} "
                "(schedule + feasibility with the donor's input_sig)"]
    # Without the donor's report the solver treats the previous signature
    # as unknown-changed and skips the hints — reported, never papered over.
    return [f"chain seed planted from {donor_work.name} WITHOUT a "
            "feasibility report — the solver's signature gate will skip "
            "the hints"]


def count_overlaps(calendar: pd.DataFrame) -> int:
    """Overlapping (production/trial/cip) block pairs per line — guard 0."""
    if calendar is None or calendar.empty:
        return 0
    blocks = calendar[calendar["block_type"].astype(str).isin(
        ["production", "trial", "cip"])]
    overlaps = 0
    for _, grp in blocks.groupby(blocks["line_name"].astype(str).str.upper()):
        rows = grp.sort_values("start_h")[["start_h", "end_h"]].to_numpy()
        prev_end = None
        for s, e in rows:
            if prev_end is not None and float(s) < float(prev_end) - 1e-6:
                overlaps += 1
            prev_end = max(float(e), prev_end or float(e))
    return overlaps


def pins_match(calendar: pd.DataFrame, pinned: pd.DataFrame,
               tol_h: float = 0.51) -> bool:
    """Every planner-pinned board block appears in the candidate at the same
    line/SKU/position (staging holds them FIXED, so a miss means the pin was
    dropped or moved)."""
    if pinned is None or not len(pinned):
        return True
    if calendar is None or calendar.empty:
        return False
    cand = calendar[calendar["block_type"].astype(str) == "production"]
    for _, p in pinned.iterrows():
        ln = str(p["line_name"]).strip().upper()
        sku = str(p["sku"])
        s, e = float(p["start_h"]), float(p["end_h"])
        m = cand[
            (cand["line_name"].astype(str).str.strip().str.upper() == ln)
            & (cand["sku"].astype(str) == sku)
            & ((pd.to_numeric(cand["start_h"], errors="coerce") - s).abs() <= tol_h)
            & ((pd.to_numeric(cand["end_h"], errors="coerce") - e).abs() <= tol_h)
        ]
        if not len(m):
            return False
    return True


def noise_floor(candidates: list[dict]) -> dict | None:
    """{"runs": n, "spread_composite": max-min} over completed noise arms."""
    comps = [c["overnight_score"]["composite"] for c in candidates
             if c.get("kind") == "noise" and c.get("overnight_score")]
    if len(comps) < 2:
        return None
    return {"runs": len(comps),
            "spread_composite": round(max(comps) - min(comps), 2)}


def ladder_verdict(candidates: list[dict], floor: dict | None) -> str:
    """Deterministic 'did 3600s beat 1800s beyond noise?' line for the brief."""
    def _comp(label: str) -> float | None:
        for c in candidates:
            if c.get("label") == label and c.get("overnight_score"):
                return float(c["overnight_score"]["composite"])
        return None
    lo, hi = _comp("ladder_300_1800"), _comp("ladder_600_3600")
    if lo is None or hi is None:
        return "Budget ladder: incomplete (one or both ladder arms missing)."
    delta = hi - lo
    spread = (floor or {}).get("spread_composite")
    if spread is None:
        return (f"Budget ladder: 600/3600s scored {delta:+.2f} vs 300/1800s "
                "(no noise floor to compare against).")
    beyond = "YES — beyond noise" if abs(delta) > spread else \
        "NO — within noise"
    return (f"Budget ladder: 600/3600s scored {delta:+.2f} vs 300/1800s; "
            f"noise spread {spread:.2f} -> did the bigger budget pay? "
            f"{beyond}.")


def next_occurrence(hhmm: str, after: datetime) -> datetime:
    """First HH:MM strictly after `after` (computed ONCE at batch start, so
    an overrunning portfolio consolidates immediately instead of waiting a
    day)."""
    h, m = (int(x) for x in hhmm.split(":"))
    t = after.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= after:
        t += timedelta(days=1)
    return t


# ---------------------------------------------------------------------------
# IO shell
# ---------------------------------------------------------------------------

class Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def set_below_normal_priority(log: Log) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        k32 = ctypes.windll.kernel32
        # Without explicit types ctypes truncates the pseudo-handle (-1) to
        # 32 bits and SetPriorityClass silently fails on 64-bit Python.
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        if k32.SetPriorityClass(k32.GetCurrentProcess(),
                                BELOW_NORMAL_PRIORITY_CLASS):
            log("process priority set to BELOW_NORMAL (solver subprocesses "
                "inherit it — box stays usable)")
        else:
            log("WARNING: SetPriorityClass failed; solvers run at NORMAL")
    except Exception as exc:  # noqa: BLE001 — priority is comfort, not correctness
        log(f"WARNING: priority drop failed: {exc}")


def live_pull(log: Log) -> None:
    """scripts/fs-live-pull.py --once: sync the bridge repo into
    data/reference and re-derive demand_plan.csv. On failure: log loudly,
    continue with current data."""
    script = ROOT / "scripts" / "fs-live-pull.py"
    try:
        r = subprocess.run([sys.executable, str(script), "--once"],
                           capture_output=True, text=True, timeout=600)
        for stream in (r.stdout, r.stderr):
            for line in (stream or "").strip().splitlines():
                log(f"[pull] {line}")
        if r.returncode != 0:
            log(f"[pull] FAILED (rc={r.returncode}) — continuing with "
                "current data")
    except Exception as exc:  # noqa: BLE001 — the batch must survive a dead pull
        log(f"[pull] FAILED ({exc}) — continuing with current data")


def fresh_stock_check(log: Log) -> tuple[dict[str, float], list[str]]:
    """The agent's stock-check path: stockcheck.api report via the
    stock_report_inputs resolver -> DNS achievable ratios."""
    try:
        vif, toggles = stock_report_inputs(DATA)
        report = stock_check_report(DATA, vif, toggles)
        if report.get("error"):
            log(f"[stock] report error: {report['error']} — no DNS caps "
                "this round")
            return {}, [f"stock check errored: {report['error']}"]
        dns = dns_ratios(report)
        log(f"[stock] fresh check: {len(dns)} component-blocked SKU(s)")
        return dns, []
    except Exception as exc:  # noqa: BLE001 — a dead stock feed must not kill the night
        log(f"[stock] FAILED ({exc}) — no DNS caps this round")
        return {}, [f"stock check FAILED: {exc}"]


@dataclass
class Generation:
    gen_id: str
    sig: tuple
    created: str
    anchor: datetime
    horizon_h: float
    week_marks: list
    gates: dict[str, float]
    demand: pd.DataFrame
    net_demand_kg: float
    capacity_bound: float
    capacity_detail: dict
    co_map: dict
    board_baseline: dict | None
    board_note: str
    staging_notes: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return OPT_DIR / self.gen_id


def stage_generation(log: Log) -> Generation:
    """Fresh staging pass (no solve): the generation's scoring frame.

    Uses the same _prepare_work_dir + _overlay_fill machinery every F run
    uses, in its own STAGING work dir, so gates / netting / blocked windows
    are exactly what the arms will see."""
    sig = generation_signature(DATA)
    now = datetime.now()
    gid = gen_id_for(sig, now)
    log(f"[gen] staging generation {gid}")
    work = DATA / WORK_ROOT / STAGING_ID
    _prepare_work_dir(DATA.resolve(), work)
    notes = _overlay_fill(work, DATA)
    for n in notes[:8]:
        log(f"[gen]   {n}")

    hz = resolve_horizon(load_toml())
    demand = pd.read_csv(work / "demand_plan.csv", dtype={"sku": str})
    gates_raw = json.loads(
        (work / "fill_gates.json").read_text(encoding="utf-8"))["gates"]
    gates = {str(k).upper(): float(v) for k, v in gates_raw.items()}

    # staged blocked windows (committed MOs + trials + projected CIPs + real
    # line-downs — already merged by the overlay)
    blocked: dict[str, list[tuple[float, float]]] = {}
    dt = pd.read_csv(work / "downtimes.csv")
    for _, r in dt.iterrows():
        blocked.setdefault(str(r["line_name"]).upper(), []).append(
            (float(r["start_hour"]), float(r["end_hour"])))

    # flat line rates (the live use_sku_rates=false convention)
    lr = pd.read_csv(work / "line_rates.csv")
    name_col = next(c for c in ("line_name", "Line", "line") if c in lr.columns)
    rate_col = ("rate_kgph" if "rate_kgph" in lr.columns else "calc_rate_kgph")
    line_rates = {str(r[name_col]).upper(): float(r.get(rate_col) or 0)
                  for _, r in lr.iterrows()}
    caps = pd.read_csv(work / "capabilities_rates.csv", dtype={"sku": str})
    capable: dict[str, set[str]] = {}
    for _, r in caps.iterrows():
        if int(pd.to_numeric(r.get("capable"), errors="coerce") or 0) == 1:
            capable.setdefault(
                str(r["line_name"]).upper(), set()).add(str(r["sku"]))

    demand_skus = set(demand["sku"].astype(str)) if len(demand) else set()
    bound, detail = capacity_bound_kg(
        gates, blocked, line_rates, capable, demand_skus, float(hz.hours))
    orders = demand_orders(demand)
    net_demand = sum(o["target"] for o in orders)
    marks = iso_week_marks(hz.anchor, float(hz.hours))
    co_map = _co_lookup(_load_changeovers(DATA / "reference"))

    # Official board baseline: same function, same frame. No solver-placed
    # blocks in the board's fill region -> no comparable region -> null.
    board = load_calendar(DATA / "calendar_blocks.csv")
    board_note = ""
    baseline: dict | None = None
    demand_ids = {o["order_id"] for o in orders}
    if len(solver_fill_blocks(_normalize(board), demand_ids)):
        b_score, _b_det = overnight_score(
            board, demand, co_map, capacity_bound=bound,
            week_marks=marks, horizon_h=float(hz.hours))
        baseline = {"overnight_score": b_score}
        log(f"[gen] board baseline composite {b_score['composite']}")
    else:
        board_note = ("the official board has no comparable fill region "
                      "(no solver-placed blocks matching the staged demand) "
                      "— board_baseline is null")
        log(f"[gen] {board_note}")

    gen = Generation(
        gen_id=gid, sig=tuple(sig),
        created=now.isoformat(timespec="seconds"),
        anchor=hz.anchor, horizon_h=float(hz.hours), week_marks=marks,
        gates=gates, demand=demand, net_demand_kg=net_demand,
        capacity_bound=bound, capacity_detail=detail, co_map=co_map,
        board_baseline=baseline, board_note=board_note,
        staging_notes=[str(n) for n in notes],
    )
    gen.dir.mkdir(parents=True, exist_ok=True)
    (gen.dir / "candidates").mkdir(exist_ok=True)
    log(f"[gen] {gid}: net demand {net_demand:,.0f} kg, capacity bound "
        f"{bound:,.0f} kg, {len(gates)} gate(s), anchor {hz.anchor:%Y-%m-%d}")
    write_leaderboard(gen)
    return gen


def write_leaderboard(gen: Generation) -> None:
    safe_write_json({
        "generation": gen.gen_id,
        "created": gen.created,
        "inputs_signature": list(gen.sig),
        "noise_floor": noise_floor(gen.candidates),
        "board_baseline": gen.board_baseline,
        # per-generation scoring constants (shared contract extension —
        # every candidate divides by the same bound)
        "net_demand_kg": round(gen.net_demand_kg, 1),
        "capacity_bound_kg": round(gen.capacity_bound, 1),
        "frame": {"anchor": f"{gen.anchor:%Y-%m-%d %H:%M:%S}",
                  "horizon_h": gen.horizon_h},
        "candidates": [_public_cand(c) for c in gen.candidates],
    }, gen.dir / "leaderboard.json")


def append_history(gen: Generation, record: dict) -> None:
    try:
        with open(gen.dir / "history.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def compute_guards(calendar: pd.DataFrame, gen: Generation,
                   log: Log) -> dict[str, Any]:
    """Zero overlaps, pins at identical positions, lock boundary respected,
    CIP intervals ok (fill-window scorecard's overdue counter)."""
    overlaps = count_overlaps(calendar)
    pins_ok = pins_match(
        calendar, pinned_blocks(load_calendar(DATA / "calendar_blocks.csv")))
    lock_ok = True
    lock_h = locked_through_h(read_lock(DATA), gen.anchor)
    if lock_h is not None and lock_h > 0:
        orders = demand_orders(gen.demand)
        fills = solver_fill_blocks(
            _normalize(calendar), {o["order_id"] for o in orders})
        if len(fills):
            starts = pd.to_numeric(fills["start_h"], errors="coerce")
            lock_ok = bool((starts >= lock_h - 1e-6).all())
    try:
        windowed = score_calendar(
            calendar, week_label="guards", data_dir=DATA,
            fill_gates=gen.gates)
        cip_ok = int(windowed.cip.get("cip_overdue", 0) or 0) == 0
    except Exception as exc:  # noqa: BLE001 — a broken guard is a failed guard
        log(f"[guards] cip check failed: {exc}")
        cip_ok = False
    return {"overlaps": int(overlaps), "pins_ok": bool(pins_ok),
            "lock_ok": bool(lock_ok), "cip_ok": bool(cip_ok)}


def run_arm(arm: dict, gen: Generation, dns: dict[str, float],
            log: Log) -> dict:
    """One sequential solve: own work dir, fresh DNS caps, separate pass
    budgets, overnight_score + guards, leaderboard + history append."""
    label = arm["label"]
    budgets = arm["budgets"]
    run_id = f"{label}_{datetime.now():%H%M%S}"
    scenario = dict(F_SCENARIO)
    scenario["id"] = f"F_overnight_{label}"  # own work dir — never F's
    merged = dict(F_SCENARIO.get("overrides") or {})
    merged.update(arm.get("params") or {})
    log(f"[arm {label}] start: budgets {budgets['pass1_s']}/"
        f"{budgets['pass2_s']}s, params {arm.get('params') or 'champion'}")

    # ── Warm-start mode (portfolio redesign 2026-08-21) ──────────────────
    # "none"   -> cold arm: run_scenario skips the greedy seed and removes
    #             carried prev files, so the solver logs a true cold start;
    # "chained"-> resolve the best scored candidate SO FAR in THIS
    #             generation as donor; its solved schedule (+ feasibility
    #             report carrying its input_sig) is planted by the patch
    #             below and run_scenario skips the greedy seed ("prev").
    #             No donor yet (first arms, or every earlier arm failed)
    #             falls back to the default greedy seed — an arm that
    #             cannot chain still earns its slot.
    donor_work: Path | None = None
    ws_mode = arm.get("warm_start")
    if ws_mode == "chained":
        donor = best_donor(gen.candidates)
        dwork = (DATA / WORK_ROOT / f"F_overnight_{donor['label']}"
                 if donor else None)
        if dwork is not None and (dwork / "schedule_phase2.csv").exists():
            scenario["warm_start"] = "prev"
            donor_work = dwork
            cand_chained_from = donor["run_id"]
            log(f"[arm {label}] chained warm start from {donor['run_id']} "
                f"(composite "
                f"{donor['overnight_score']['composite']})")
        else:
            cand_chained_from = None
            log(f"[arm {label}] chained: no solved donor in this "
                "generation yet — greedy seed fallback")
    else:
        cand_chained_from = None
        if ws_mode:
            scenario["warm_start"] = ws_mode

    def patch(work: Path) -> list[str]:
        notes: list[str] = []
        if donor_work is not None:
            notes.extend(plant_chain_seed(work, donor_work))
        dem_path = work / "demand_plan.csv"
        dem = pd.read_csv(dem_path, dtype={"sku": str})
        trimmed, tnotes = trim_dns_demand(dem, dns)
        trimmed.to_csv(dem_path, index=False)
        if tnotes:
            notes.append(f"DNS trim: {len(tnotes)} order(s) capped "
                         f"({len(dns)} component-blocked SKU(s))")
        _set_work_scheduler_flag(
            work / "flowstate.toml", "time_limit_pass2",
            int(budgets["pass2_s"]))
        notes.append(f"pass 2 budget {budgets['pass2_s']}s "
                     "(scheduler.time_limit_pass2)")
        return notes

    t0 = time.monotonic()
    cand: dict[str, Any] = {
        "run_id": run_id, "label": label, "kind": arm.get("kind", ""),
        "params": merged, "budgets": dict(budgets),
        "overnight_score": None, "guards": None, "gap_pct_end": None,
        "wall_s": 0.0, "version_slug": None, "published": None,
    }
    if ws_mode == "chained":
        # honest provenance: which run's schedule seeded this search
        # (None = no donor was available and the greedy seed ran instead)
        cand["chained_from"] = cand_chained_from
    try:
        result = run_scenario(
            scenario, DATA, time_limit=int(budgets["pass1_s"]),
            overrides=merged, work_dir_patch=patch, work_root=WORK_ROOT,
            timeout_s=float(budgets["pass1_s"]) + float(budgets["pass2_s"])
            + 120.0 + TIMEOUT_SLACK_S)
    except Exception:  # noqa: BLE001 — one bad arm never kills the night
        cand["wall_s"] = round(time.monotonic() - t0, 1)
        cand["error"] = traceback.format_exc(limit=3)
        log(f"[arm {label}] CRASHED: {cand['error'].splitlines()[-1]}")
        gen.candidates.append(cand)
        write_leaderboard(gen)
        append_history(gen, cand)
        return cand
    cand["wall_s"] = round(time.monotonic() - t0, 1)

    work = DATA / WORK_ROOT / scenario["id"]
    try:
        prog = json.loads(
            (work / "solver_progress.json").read_text(encoding="utf-8"))
        gap = (prog.get("solver_stats") or {}).get("gap_pct")
        cand["gap_pct_end"] = float(gap) if gap is not None else None
    except (OSError, ValueError, TypeError):
        cand["gap_pct_end"] = None

    if not result.get("ok") or result.get("calendar") is None:
        cand["error"] = (result.get("log") or "")[-1500:]
        log(f"[arm {label}] solve FAILED (rc={result.get('returncode')})")
        gen.candidates.append(cand)
        write_leaderboard(gen)
        append_history(gen, cand)
        return cand

    calendar = result["calendar"]
    score, details = overnight_score(
        calendar, gen.demand, gen.co_map,
        capacity_bound=gen.capacity_bound, week_marks=gen.week_marks,
        horizon_h=gen.horizon_h)
    cand["overnight_score"] = score
    cand["guards"] = compute_guards(calendar, gen, log)

    # persist the schedule + the classic scorecard for publish/audit
    cdir = gen.dir / "candidates"
    save_calendar(calendar, cdir / f"{run_id}.calendar.csv")
    classic = score_calendar(
        calendar, week_label=f"overnight {run_id}", data_dir=DATA)
    safe_write_json(classic.to_dict(), cdir / f"{run_id}.scorecard.json")
    cand["_calendar_path"] = str(cdir / f"{run_id}.calendar.csv")
    cand["_scorecard_path"] = str(cdir / f"{run_id}.scorecard.json")
    cand["_fill_gates"] = result.get("fill_gates") or gen.gates
    cand["_gen_id"] = gen.gen_id

    log(f"[arm {label}] done in {cand['wall_s']:.0f}s: composite "
        f"{score['composite']} (fill {score['fill']}, co "
        f"{score['changeovers']}, campaign {score['campaign']}, on_time "
        f"{score['on_time']}), gap {cand['gap_pct_end']}, guards "
        f"{cand['guards']}, details placed {details.get('placed_h')}h")
    gen.candidates.append(cand)
    write_leaderboard(gen)
    append_history(gen, {k: v for k, v in cand.items()
                         if not k.startswith("_")})
    return cand


def _public_cand(cand: dict) -> dict:
    """Leaderboard shape: strip the private plumbing keys."""
    return {k: v for k, v in cand.items() if not k.startswith("_")}


# Two of the four subscores (changeovers, campaign — 45% of the composite)
# are PER PLACED HOUR, so an arm that simply places less can score well by
# doing less. The frozen v1 formula stays as-is; instead an under-filling
# candidate is barred from PUBLICATION — it still appears on the leaderboard
# with its real score, but the planner's version slots only ever receive a
# schedule that actually filled the tail. Threshold: within this fraction of
# the best fill subscore seen among guard-passing candidates.
PUBLISH_FILL_FLOOR_RATIO = 0.85


def publishable(pool: list[dict]) -> tuple[list[dict], list[dict]]:
    """(eligible, barred) split of guard-passing candidates on the fill floor."""
    if not pool:
        return [], []
    best_fill = max(float(c["overnight_score"]["fill"]) for c in pool)
    floor = best_fill * PUBLISH_FILL_FLOOR_RATIO
    eligible = [c for c in pool
                if float(c["overnight_score"]["fill"]) >= floor]
    barred = [c for c in pool if c not in eligible]
    return eligible, barred


def publish_top2(generations: list[Generation], log: Log,
                 notes: list[str]) -> None:
    """Cross-generation top-1 + runner-up (guards must pass) -> fixed-slug
    upserts. Updates never consume rolling slots; a first-ever creation at
    full capacity is caught and reported in the brief."""
    pool = [c for g in generations for c in g.candidates
            if c.get("overnight_score") and c.get("guards")
            and c["guards"]["overlaps"] == 0 and c["guards"]["pins_ok"]
            and c["guards"]["lock_ok"] and c["guards"]["cip_ok"]]
    if not pool:
        notes.append("nothing published: no candidate passed all guards")
        log("[publish] " + notes[-1])
        return
    pool, barred = publishable(pool)
    for c in barred:
        c["publish_barred"] = "fill below the night's best fill"
    if barred:
        notes.append(
            f"{len(barred)} candidate(s) scored well on per-hour ratios but "
            "filled too little to publish (they stay on the leaderboard): "
            + ", ".join(f"{c.get('label', c['run_id'])} "
                        f"(fill {float(c['overnight_score']['fill']):.1f})"
                        for c in barred[:4]))
        log("[publish] " + notes[-1])
    if not pool:
        notes.append("nothing published: every candidate was under-filled")
        log("[publish] " + notes[-1])
        return
    pool.sort(key=lambda c: -float(c["overnight_score"]["composite"]))
    now = datetime.now()
    cur_anchor = resolve_horizon(load_toml()).anchor
    slots = [("best", BEST_SLUG, "Overnight best"),
             ("runner_up", RUNNER_SLUG, "Overnight runner-up")]
    for (tag, slug, title), cand in zip(slots, pool[:2]):
        gen = next(g for g in generations if g.gen_id == cand["_gen_id"])
        cal = load_calendar(Path(cand["_calendar_path"]))
        gates = {str(k).upper(): float(v)
                 for k, v in (cand["_fill_gates"] or {}).items()}
        # Publish in the CURRENT frame: a candidate solved before midnight
        # carries hours in its generation's anchor frame; upsert_version
        # stamps the publish-time anchor, so shift the hours (and gates)
        # into it to keep wall-clock positions honest.
        shift = (gen.anchor - cur_anchor).total_seconds() / 3600.0
        if abs(shift) > 1e-6:
            for col in ("start_h", "end_h"):
                cal[col] = pd.to_numeric(cal[col], errors="coerce") + shift
            gates = {k: v + shift for k, v in gates.items()}
            log(f"[publish] {slug}: re-based {shift:+.0f}h into the current "
                "anchor frame")
        score = cand["overnight_score"]
        name = f"{title} {now:%y-%m-%d %H:%M} · score {score['composite']}"
        try:
            classic = json.loads(Path(cand["_scorecard_path"]).read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            classic = {}
        try:
            upsert_version(
                slug, name, cal, classic, DATA,
                notes=(f"Overnight batch {cand['_gen_id']} / "
                       f"{cand['run_id']}: overnight_score "
                       f"{json.dumps(score)}; params {cand['params']}; "
                       f"budgets {cand['budgets']}"),
                source="agent:overnight",
                extra_meta={"fill_gates": gates,
                            "overnight_score": score,
                            "generation": cand["_gen_id"]},
            )
        except ValueError as exc:
            # first-ever creation with all 5 slots full — report, don't die
            notes.append(f"publish {slug} skipped: {exc}")
            log(f"[publish] {notes[-1]}")
            continue
        cand["version_slug"] = slug
        cand["published"] = tag
        write_leaderboard(gen)
        notes.append(f"published {slug}: {name} (run {cand['run_id']})")
        log(f"[publish] {notes[-1]}")


def write_brief(generations: list[Generation], notes: list[str],
                stock_events: list[str], started: datetime,
                log: Log) -> None:
    """Deterministic, stats-based morning summary. The future agent
    overwrites this file with its own narrative."""
    all_cands = [c for g in generations for c in g.candidates]
    scored = [c for c in all_cands if c.get("overnight_score")]
    scored.sort(key=lambda c: -float(c["overnight_score"]["composite"]))
    floor = noise_floor(all_cands)
    lines = [
        f"# Overnight optimizer brief — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"Batch started {started:%Y-%m-%d %H:%M}; "
        f"{len(generations)} generation(s), {len(all_cands)} arm(s) tried, "
        f"{len(scored)} scored, {len(all_cands) - len(scored)} failed.",
        "",
        "## Generations",
        "",
        "| generation | created | arms | net demand kg | capacity bound kg |",
        "|---|---|---|---|---|",
    ]
    for g in generations:
        lines.append(
            f"| {g.gen_id} | {g.created} | {len(g.candidates)} | "
            f"{g.net_demand_kg:,.0f} | {g.capacity_bound:,.0f} |")
    lines += ["", "## Noise floor", ""]
    if floor:
        lines.append(f"{floor['runs']} champion runs, composite spread "
                     f"{floor['spread_composite']}.")
    else:
        lines.append("Not measured (fewer than 2 completed noise runs).")
    lines += ["", "## Top candidates", ""]
    if scored:
        lines += [
            "| run | composite | fill | changeovers | campaign | on_time | "
            "guards ok | published |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for c in scored[:5]:
            s = c["overnight_score"]
            gok = (c.get("guards") and c["guards"]["overlaps"] == 0
                   and c["guards"]["pins_ok"] and c["guards"]["lock_ok"]
                   and c["guards"]["cip_ok"])
            lines.append(
                f"| {c['run_id']} | {s['composite']} | {s['fill']} | "
                f"{s['changeovers']} | {s['campaign']} | {s['on_time']} | "
                f"{'yes' if gok else 'NO'} | {c.get('published') or ''} |")
    else:
        lines.append("No candidate produced a schedule.")
    lines += ["", "## vs your board", ""]
    last = generations[-1]
    if last.board_baseline:
        b = last.board_baseline["overnight_score"]["composite"]
        if scored:
            top = scored[0]["overnight_score"]["composite"]
            lines.append(f"Board fill region scores {b}; best candidate "
                         f"{top} ({top - b:+.2f} vs your board).")
        else:
            lines.append(f"Board fill region scores {b}.")
    else:
        lines.append(last.board_note or "No board baseline.")
    lines += ["", "## Budget ladder", "",
              ladder_verdict(all_cands, floor)]
    lines += ["", "## Stock check", ""]
    if stock_events:
        lines += [f"- {e}" for e in stock_events]
    else:
        lines.append("- no component-availability changes noticed overnight")
    lines += ["", "## Notes & failures", ""]
    fails = [c for c in all_cands if c.get("error")]
    for c in fails:
        first = str(c["error"]).strip().splitlines()
        lines.append(f"- arm {c['run_id']} FAILED: "
                     f"{first[-1][:200] if first else 'unknown'}")
    for n in notes:
        lines.append(f"- {n}")
    if not fails and not notes:
        lines.append("- clean night: nothing failed")
    (OPT_DIR / "brief.md").write_text("\n".join(lines) + "\n",
                                      encoding="utf-8")
    log(f"[brief] written ({len(lines)} lines)")


def write_latest(gen: Generation) -> None:
    safe_write_json({
        "generation": gen.gen_id,
        "leaderboard": f"{gen.gen_id}/leaderboard.json",
        "brief": "brief.md",
    }, OPT_DIR / "latest.json")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def ensure_generation(gen: Generation | None, generations: list[Generation],
                      log: Log) -> Generation:
    """Rotate the generation when the scoring inputs signature OR the
    rolling anchor changed (midnight roll shifts every staged hour offset —
    scores across frames would be lies)."""
    sig = generation_signature(DATA)
    anchor = resolve_horizon(load_toml()).anchor
    if gen is not None and sig == gen.sig and anchor == gen.anchor:
        return gen
    if gen is not None:
        why = "inputs signature changed" if sig != gen.sig else \
            "planning anchor rolled"
        log(f"[gen] closing {gen.gen_id}: {why}")
        write_leaderboard(gen)
    new_gen = stage_generation(log)
    generations.append(new_gen)
    return new_gen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="tiny budgets (60/120s), 5-arm portfolio "
                         "(champion x2, seed, cold, chained), no publish "
                         "unless --publish, no 5am wait")
    ap.add_argument("--publish", action="store_true",
                    help="publish even in --dry-run")
    ap.add_argument("--no-publish", action="store_true",
                    help="never publish, regardless of every other flag "
                         "(beats --publish)")
    ap.add_argument("--skip-pull", action="store_true",
                    help="skip fs-live-pull (sandbox/test runs)")
    ap.add_argument("--pass1-s", type=int, default=None, metavar="N",
                    help="override EVERY arm's pass-1 budget in seconds "
                         "(small-run testing); default: per-arm budgets")
    ap.add_argument("--pass2-s", type=int, default=None, metavar="N",
                    help="override EVERY arm's pass-2 budget in seconds "
                         "(small-run testing); default: per-arm budgets")
    ap.add_argument("--consolidate-at", default="05:00",
                    help="HH:MM for the final champion run (default 05:00)")
    args = ap.parse_args()

    OPT_DIR.mkdir(parents=True, exist_ok=True)
    log = Log(OPT_DIR / "batch.log")
    started = datetime.now()
    log(f"=== overnight batch start (dry_run={args.dry_run}) ===")
    set_below_normal_priority(log)
    consolidate_target = next_occurrence(args.consolidate_at, started)

    if not args.skip_pull:
        live_pull(log)

    cfg = load_toml()
    arms = default_portfolio(cfg, dry_run=args.dry_run)
    steering_path = OPT_DIR / "steering.json"
    if steering_path.exists():
        try:
            raw = json.loads(steering_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raw, note = None, f"steering.json unreadable: {exc}"
            log(f"[steering] {note}")
        steer_arms, steer_notes = parse_steering(raw, datetime.now())
        for n in steer_notes:
            log(f"[steering] {n}")
        arms = apply_steering(arms, steer_arms)
    arms = apply_budget_overrides(arms, args.pass1_s, args.pass2_s)
    if args.pass1_s is not None or args.pass2_s is not None:
        log(f"[portfolio] budgets overridden for every arm: "
            f"pass1={args.pass1_s or 'per-arm'}s "
            f"pass2={args.pass2_s or 'per-arm'}s")
    log(f"[portfolio] {len(arms)} arm(s): "
        + ", ".join(a["label"] for a in arms))

    generations: list[Generation] = []
    gen: Generation | None = None
    brief_notes: list[str] = []
    stock_events: list[str] = []
    prev_dns: dict[str, float] | None = None

    for arm in arms:
        try:
            gen = ensure_generation(gen, generations, log)
        except Exception:  # noqa: BLE001 — staging must not kill the night
            log("[gen] staging FAILED:\n" + traceback.format_exc(limit=5))
            brief_notes.append("generation staging FAILED — arm "
                               f"{arm['label']} skipped")
            continue
        dns, sc_notes = fresh_stock_check(log)
        stock_events.extend(sc_notes)
        if prev_dns is not None and dns != prev_dns:
            gained = sorted(set(dns) - set(prev_dns))
            freed = sorted(set(prev_dns) - set(dns))
            if gained:
                stock_events.append(
                    f"{datetime.now():%H:%M} components ran short for: "
                    + ", ".join(gained))
            if freed:
                stock_events.append(
                    f"{datetime.now():%H:%M} components freed up for: "
                    + ", ".join(freed))
        prev_dns = dns
        run_arm(arm, gen, dns, log)

    # ── ~5am champion consolidation on the freshest data ──────────────────
    if not args.dry_run:
        now = datetime.now()
        if now < consolidate_target:
            wait_s = (consolidate_target - now).total_seconds()
            log(f"[consolidate] waiting until {consolidate_target:%H:%M} "
                f"({wait_s / 3600:.1f}h)")
            while datetime.now() < consolidate_target:
                time.sleep(min(300.0,
                               (consolidate_target
                                - datetime.now()).total_seconds() + 1))
        if not args.skip_pull:
            live_pull(log)
        try:
            gen = ensure_generation(gen, generations, log)
            dns, sc_notes = fresh_stock_check(log)
            stock_events.extend(sc_notes)
            champion = {"label": "champion_5am", "kind": "champion",
                        "params": {}, "budgets": dict(CHAMPION_BUDGETS)}
            # --pass1-s/--pass2-s override EVERY arm, the 5am one included
            apply_budget_overrides([champion], args.pass1_s, args.pass2_s)
            run_arm(champion, gen, dns, log)
        except Exception:  # noqa: BLE001
            log("[consolidate] FAILED:\n" + traceback.format_exc(limit=5))
            brief_notes.append("5am champion consolidation FAILED")

    if generations:
        if args.no_publish:
            brief_notes.append("publish skipped (--no-publish)")
            log("[publish] skipped (--no-publish)")
        elif not args.dry_run or args.publish:
            publish_top2(generations, log, brief_notes)
        else:
            brief_notes.append("dry run: publish skipped (use --publish)")
            log("[publish] skipped (dry run)")
        # leaderboards carry the final noise floor + published flags
        for g in generations:
            write_leaderboard(g)
        write_brief(generations, brief_notes, stock_events, started, log)
        write_latest(generations[-1])
    else:
        log("no generation was staged — nothing to publish or brief")

    log("=== overnight batch done ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
