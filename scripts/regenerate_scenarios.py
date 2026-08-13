#!/usr/bin/env python3
"""Regenerate all Flowstate scenarios A-E with the fixed solver/overlay logic
and write a comparison table + per-scenario scorecards. Safe to run in
parallel across scenarios (each writes to its own data/_scenario_work/<id>/)."""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from concurrent.futures import ProcessPoolExecutor, as_completed
from helpers.scenario_runner import run_scenario, SCENARIOS
from helpers.paths import data_dir

ROOT = Path(__file__).resolve().parents[1]
VENV = str(ROOT / ".venv" / "Scripts" / "python.exe")
IDS = ["A", "B", "C", "D", "E"]
OUT = ROOT / "data" / "_scenario_regeneration"


def run_one(sid: str) -> dict:
    sc = next(s for s in SCENARIOS if s["id"] == sid)
    t0 = time.time()
    res = run_scenario(sc, data_dir(), time_limit=180, python_exe=VENV)
    f = res.get("feasibility") or {}
    scd = res.get("scorecard")
    row = {
        "id": sid,
        "name": sc["name"],
        "ok": bool(res.get("ok")),
        "rc": res.get("returncode"),
        "relax_level": f.get("relax_level"),
        "status": f.get("status"),
        "mode": f.get("relax_mode"),
        "short": len(f.get("orders_short_of_qmin") or []),
        "late": len(f.get("late_orders") or []),
        "composite": round(scd.composite, 2) if scd else None,
        "cats": dict(scd.category_scores) if scd else {},
        "seconds": round(time.time() - t0, 1),
    }
    return row


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[regenerate] running scenarios {IDS} (2 parallel)", flush=True)
    results = {}
    with ProcessPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(run_one, sid): sid for sid in IDS}
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001
                r = {"id": sid, "ok": False, "error": str(exc), "seconds": 0}
            results[sid] = r
            ok = "OK " if r.get("ok") else "FAIL"
            print(f"[{r.get('seconds',0):>6.1f}s] {ok} {sid} "
                  f"lvl={r.get('relax_level')} {r.get('status')} "
                  f"comp={r.get('composite')} short={r.get('short')} "
                  f"late={r.get('late')}", flush=True)

    # Write JSON + a readable markdown table
    (OUT / "results.json").write_text(
        json.dumps({k: {**v, "cats": {str(a): b for a, b in v.get("cats", {}).items()}}
                   for k, v in results.items()}, indent=2, default=str),
        encoding="utf-8")

    lines = ["# Scenario regeneration (fixed logic)", ""]
    lines.append("| id | ok | relax | status | composite | CIP | changeovers | service | short | late | sec |")
    lines.append("|---|----|-------|--------|-----------|-----|-------------|---------|-------|------|-----|")
    for sid in IDS:
        r = results.get(sid, {})
        c = r.get("cats", {})
        lines.append(
            f"| {sid} | {'Y' if r.get('ok') else 'N'} | {r.get('relax_level')} "
            f"| {r.get('status')} | {r.get('composite')} | {c.get('cip')} "
            f"| {c.get('changeovers')} | {c.get('service')} | {r.get('short')} "
            f"| {r.get('late')} | {r.get('seconds')} |")
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n[regenerate] done ->", OUT / "report.md", flush=True)


if __name__ == "__main__":
    main()
