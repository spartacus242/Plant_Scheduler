# scripts/ab_rate_dialects.py
#
# Controlled A/B/C of the solver's rate dialect on IDENTICAL staged inputs
# (Scenario F, same frame, same budgets, sequential so CP-SAT never shares
# the CPU):
#   flat      use_sku_rates=false  -> line_rates.csv per line (the pre-2026-09-08 live mode)
#   calc      use_sku_rates=true   -> the plant's calc_rate_kgph per (line, sku)
#   measured  use_sku_rates=true   -> measured kg/h overlay (helpers.effective_rates), the new live mode
#
# Every arm is scored two ways:
#   * overnight_score v1 on the nightly convention (flat-rate capacity bound)
#     so the numbers sit on the same scale as data/optimizer leaderboards;
#   * a re-pricing matrix: the solver-placed hours of each arm valued under
#     EACH dialect. "planned kg" is what the arm believed it made; the
#     measured column is the best estimate of what the plant would actually
#     make from those hours. The gap between them is the promise error of
#     the old dialects.
#
# Outputs: data/_ab_rates/<stamp>/{results.json, report.md, <arm>.calendar.csv}
# Run:  python scripts/ab_rate_dialects.py [--pass1 240] [--pass2 600] [--arms flat,calc,measured]
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402

from helpers.calendar_io import save_calendar  # noqa: E402
from helpers.config import load_toml  # noqa: E402
from helpers.horizon import resolve as resolve_horizon  # noqa: E402
from helpers.overnight_score import (_normalize, capacity_bound_kg,  # noqa: E402
                                     demand_orders, iso_week_marks,
                                     overnight_score, solver_fill_blocks)
from helpers.scenario_runner import (SCENARIOS, _overlay_fill,  # noqa: E402
                                     _prepare_work_dir,
                                     _set_work_scheduler_flag, run_scenario)
from helpers.scorecard_engine import _co_lookup, _load_changeovers  # noqa: E402

DATA = ROOT / "data"
WORK_ROOT = "_ab_rates_work"
OUT_ROOT = DATA / "_ab_rates"
F = next(s for s in SCENARIOS if s["id"] == "F")
VENV = str(ROOT / ".venv" / "Scripts" / "python.exe")
DIALECTS = ("flat", "calc", "measured")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def stage_frame(work: Path) -> dict:
    """One staging pass (no solve) -> the shared scoring frame + rate tables."""
    _prepare_work_dir(DATA.resolve(), work)
    notes = _overlay_fill(work, DATA)
    hz = resolve_horizon(load_toml())
    demand = pd.read_csv(work / "demand_plan.csv", dtype={"sku": str})
    gates = {str(k).upper(): float(v) for k, v in json.loads(
        (work / "fill_gates.json").read_text(encoding="utf-8"))["gates"].items()}
    blocked: dict[str, list[tuple[float, float]]] = {}
    for _, r in pd.read_csv(work / "downtimes.csv").iterrows():
        blocked.setdefault(str(r["line_name"]).upper(), []).append(
            (float(r["start_hour"]), float(r["end_hour"])))
    lr = pd.read_csv(work / "line_rates.csv")
    name_col = next(c for c in ("line_name", "Line", "line") if c in lr.columns)
    rate_col = "rate_kgph" if "rate_kgph" in lr.columns else "calc_rate_kgph"
    flat = {str(r[name_col]).upper(): float(r.get(rate_col) or 0) for _, r in lr.iterrows()}
    caps = pd.read_csv(work / "capabilities_rates.csv", dtype={"sku": str})   # staged = effective
    caps["line_name"] = caps["line_name"].astype(str).str.upper()
    capable: dict[str, set[str]] = {}
    for _, r in caps.iterrows():
        if int(pd.to_numeric(r.get("capable"), errors="coerce") or 0) == 1:
            capable.setdefault(r["line_name"], set()).add(str(r["sku"]))
    demand_skus = set(demand["sku"].astype(str))
    bound_flat, _ = capacity_bound_kg(gates, blocked, flat, capable, demand_skus, float(hz.hours))
    # measured-dialect bound: per line, mean effective rate over capable demand SKUs
    eff_line = (caps[(caps["capable"] == 1) & caps["sku"].isin(demand_skus) & (caps["calc_rate_kgph"] > 0)]
                .groupby("line_name")["calc_rate_kgph"].mean().to_dict())
    bound_meas, _ = capacity_bound_kg(gates, blocked, {k: float(v) for k, v in eff_line.items()},
                                      capable, demand_skus, float(hz.hours))
    rates = {
        "flat": {(ln, sku): flat.get(ln, 0.0) for (ln, sku) in zip(caps["line_name"], caps["sku"])},
        "calc": {(ln, sku): float(v) for ln, sku, v in zip(caps["line_name"], caps["sku"], caps["calc_rate_kgph_model"])},
        "measured": {(ln, sku): float(v) for ln, sku, v in zip(caps["line_name"], caps["sku"], caps["calc_rate_kgph"])},
    }
    return {
        "hz": hz, "demand": demand, "orders": demand_orders(demand),
        "marks": iso_week_marks(hz.anchor, float(hz.hours)),
        "co_map": _co_lookup(_load_changeovers(DATA / "reference")),
        "bound_flat": bound_flat, "bound_measured": bound_meas, "rates": rates,
        "rate_sources": caps.loc[caps["capable"] == 1, "rate_source"].value_counts().to_dict(),
        "notes": [str(n) for n in notes],
    }


def make_patch(arm: str, pass2: int):
    def patch(work: Path) -> list[str]:
        notes = []
        toml = work / "flowstate.toml"
        _set_work_scheduler_flag(toml, "time_limit_pass2", int(pass2))
        if arm == "flat":
            _set_work_scheduler_flag(toml, "use_sku_rates", False)
            notes.append("use_sku_rates=false: flat line_rates.csv")
        elif arm == "calc":
            _set_work_scheduler_flag(toml, "use_sku_rates", True)
            p = work / "capabilities_rates.csv"
            caps = pd.read_csv(p, dtype={"sku": str})
            caps["calc_rate_kgph"] = caps["calc_rate_kgph_model"]
            caps["rate_source"] = "model"
            caps.to_csv(p, index=False)
            notes.append("staged capabilities reverted to the plant's calc_rate_kgph")
        else:
            _set_work_scheduler_flag(toml, "use_sku_rates", True)
            notes.append("measured overlay kept (live mode)")
        return notes
    return patch


def reprice(calendar: pd.DataFrame, frame: dict) -> dict:
    """Solver-placed hours valued under every dialect."""
    cal = _normalize(calendar)
    ids = {o["order_id"] for o in frame["orders"]}
    fb = solver_fill_blocks(cal, ids)
    hours = (fb["end_h"] - fb["start_h"]).astype(float)
    out = {"placed_h": round(float(hours.sum()), 1), "planned_kg": round(float(fb["qty_kg"].fillna(0).sum()), 0)}
    for d in DIALECTS:
        tbl = frame["rates"][d]
        kg = sum(h * tbl.get((str(ln).upper(), str(sku)), 0.0)
                 for h, ln, sku in zip(hours, fb["line_name"], fb["sku"]))
        out[f"kg_at_{d}"] = round(kg, 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass1", type=int, default=240)
    ap.add_argument("--pass2", type=int, default=600)
    ap.add_argument("--arms", default=",".join(DIALECTS))
    a = ap.parse_args()
    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out = OUT_ROOT / stamp
    out.mkdir(parents=True, exist_ok=True)

    log(f"staging frame -> {out}")
    frame = stage_frame(DATA / WORK_ROOT / "stage")
    log(f"frame: anchor {frame['hz'].anchor:%Y-%m-%d} horizon {frame['hz'].hours}h, "
        f"net demand {sum(o['target'] for o in frame['orders']):,.0f} kg, "
        f"bound flat {frame['bound_flat']:,.0f} kg / measured {frame['bound_measured']:,.0f} kg, "
        f"rate sources {frame['rate_sources']}")

    results = {"stamp": stamp, "budgets": {"pass1_s": a.pass1, "pass2_s": a.pass2},
               "frame": {"anchor": str(frame["hz"].anchor), "horizon_h": frame["hz"].hours,
                         "net_demand_kg": round(sum(o["target"] for o in frame["orders"]), 0),
                         "bound_flat_kg": round(frame["bound_flat"], 0),
                         "bound_measured_kg": round(frame["bound_measured"], 0),
                         "rate_sources": frame["rate_sources"]},
               "arms": {}}
    for arm in arms:
        log(f"ARM {arm} start ({a.pass1}/{a.pass2}s)")
        t0 = time.monotonic()
        res = run_scenario(F, DATA, time_limit=a.pass1, python_exe=VENV,
                           work_dir_patch=make_patch(arm, a.pass2), work_root=WORK_ROOT,
                           timeout_s=float(a.pass1 + a.pass2) + 600.0)
        wall = round(time.monotonic() - t0, 1)
        rec: dict = {"wall_s": wall, "ok": bool(res.get("ok")), "rc": res.get("returncode"),
                     "relax_level": res.get("relax_level")}
        if not res.get("ok") or res.get("calendar") is None:
            rec["error"] = (res.get("log") or "")[-1500:]
            log(f"ARM {arm} FAILED rc={res.get('returncode')} in {wall}s")
            results["arms"][arm] = rec
            continue
        cal = res["calendar"]
        save_calendar(cal, out / f"{arm}.calendar.csv")
        try:
            prog = json.loads((DATA / WORK_ROOT / "F" / "solver_progress.json").read_text(encoding="utf-8"))
            rec["gap_pct_end"] = (prog.get("solver_stats") or {}).get("gap_pct")
        except Exception:  # noqa: BLE001
            rec["gap_pct_end"] = None
        s_flat, d_flat = overnight_score(cal, frame["demand"], frame["co_map"], capacity_bound=frame["bound_flat"],
                                         week_marks=frame["marks"], horizon_h=float(frame["hz"].hours))
        s_meas, _ = overnight_score(cal, frame["demand"], frame["co_map"], capacity_bound=frame["bound_measured"],
                                    week_marks=frame["marks"], horizon_h=float(frame["hz"].hours))
        rec["score_flat_bound"] = s_flat
        rec["score_measured_bound"] = s_meas
        rec["details"] = {k: v for k, v in d_flat.items() if not isinstance(v, (list, dict))} if isinstance(d_flat, dict) else {}
        rec["reprice"] = reprice(cal, frame)
        results["arms"][arm] = rec
        log(f"ARM {arm} done in {wall}s: composite {s_flat['composite']} "
            f"(fill {s_flat['fill']}, co {s_flat['changeovers']}, campaign {s_flat['campaign']}, "
            f"on_time {s_flat['on_time']}), gap {rec['gap_pct_end']}, reprice {rec['reprice']}")
        (out / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    (out / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(report(results), encoding="utf-8")
    log("DONE")
    return 0


def report(r: dict) -> str:
    f = r["frame"]
    L = [f"# Rate dialect A/B — {r['stamp']}", "",
         f"Scenario F, budgets {r['budgets']['pass1_s']}/{r['budgets']['pass2_s']} s, sequential, identical staged inputs.",
         f"Frame: anchor {f['anchor']}, {f['horizon_h']} h; net demand {f['net_demand_kg']:,.0f} kg; "
         f"capacity bound flat {f['bound_flat_kg']:,.0f} kg / measured {f['bound_measured_kg']:,.0f} kg; "
         f"capable-row rate sources {f['rate_sources']}.", "",
         "## overnight_score v1 (flat-bound convention, comparable to nightly leaderboards)", "",
         "| arm | composite | fill | changeovers | campaign | on_time | gap % | wall s |", "|---|---|---|---|---|---|---|---|"]
    for arm, rec in r["arms"].items():
        s = rec.get("score_flat_bound")
        if not s:
            L.append(f"| {arm} | FAILED | | | | | | {rec.get('wall_s')} |"); continue
        L.append(f"| {arm} | {s['composite']} | {s['fill']} | {s['changeovers']} | {s['campaign']} | {s['on_time']} | "
                 f"{rec.get('gap_pct_end')} | {rec.get('wall_s')} |")
    L += ["", "## Re-pricing: solver-placed hours valued under each dialect (kg)", "",
          "| arm | placed h | planned kg | at flat | at calc | at measured | promise error vs measured |", "|---|---|---|---|---|---|---|"]
    for arm, rec in r["arms"].items():
        p = rec.get("reprice")
        if not p:
            continue
        err = (p["planned_kg"] - p["kg_at_measured"]) / p["kg_at_measured"] * 100 if p["kg_at_measured"] else float("nan")
        L.append(f"| {arm} | {p['placed_h']:,.0f} | {p['planned_kg']:,.0f} | {p['kg_at_flat']:,.0f} | {p['kg_at_calc']:,.0f} | "
                 f"{p['kg_at_measured']:,.0f} | {err:+.1f}% |")
    L += ["", "## Same schedules scored with the measured-rate capacity bound", "",
          "| arm | composite | fill |", "|---|---|---|"]
    for arm, rec in r["arms"].items():
        s = rec.get("score_measured_bound")
        if s:
            L.append(f"| {arm} | {s['composite']} | {s['fill']} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
