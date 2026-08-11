"""Verify the downtime-horizon fix with a REAL solve: no production may be
placed on a line that is down for the whole horizon (P11/P13).

Usage: python scripts/verify_downtime_horizon.py [scenario_id]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import pandas as pd

from helpers.paths import data_dir
from helpers.scenario_runner import run_scenario

dd = data_dir()
sc = {"id": "dt_verify", "name": "downtime horizon verify",
      "objective": "balanced", "two_phase": False}
res = run_scenario(sc, dd, time_limit=int(sys.argv[1]) if len(sys.argv) > 1 else 240,
                   python_exe=str(ROOT / ".venv" / "Scripts" / "python.exe"))

print("ok:", res["ok"], "rc:", res["returncode"], "relax:", res.get("relax_level"))
head = [ln for ln in (res["log"] or "").splitlines()[:3]]
print("notes:", " | ".join(head))

work = dd / "_scenario_work" / "dt_verify"
dt = pd.read_csv(work / "downtimes.csv", encoding="utf-8-sig")
print("\ndowntime windows in work dir:")
print(dt.to_string(index=False))

sched_p = work / "schedule_phase2.csv"
if not sched_p.exists():
    print("\nNO SCHEDULE -- log tail:\n", (res["log"] or "")[-2000:])
    raise SystemExit(1)

s = pd.read_csv(sched_p, encoding="utf-8-sig")
print(f"\nblocks: {len(s)}  max_end_hour: {s['end_hour'].max()}")
bad = []
for _, d in dt.iterrows():
    ln, ds, de = str(d["line_name"]).upper(), float(d["start_hour"]), float(d["end_hour"])
    hit = s[(s["line_name"].astype(str).str.upper() == ln)
            & (s["start_hour"] < de) & (s["end_hour"] > ds)]
    for _, b in hit.iterrows():
        bad.append(f"{ln} sku {b['sku']} {b['start_hour']}-{b['end_hour']}h")
print("production inside a downtime window:", bad or "NONE  <-- contract holds")
raise SystemExit(1 if bad else 0)
