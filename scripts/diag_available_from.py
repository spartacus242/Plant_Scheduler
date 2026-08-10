"""Diagnose whether the solver honours available_from_hour, keyed by line_id
(not line_name) straight off the solver's own output files."""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
work = ROOT / "data" / "_scenario_work" / "cs_smoke"

init = pd.read_csv(work / "initial_states.csv")
sched = pd.read_csv(work / "schedule_phase2.csv")
print("schedule cols:", list(sched.columns))
print(sched.head(3).to_string(index=False))

gate = {int(r["line_id"]): int(r["available_from_hour"]) for _, r in init.iterrows()}
lname = {int(r["line_id"]): r["line_name"] for _, r in init.iterrows()}

lid_col = "line_id" if "line_id" in sched.columns else None
start_col = next((c for c in ("start_hour", "start_h", "start") if c in sched.columns), None)
print("using", lid_col, start_col)

bad = []
for lid, grp in sched.groupby(lid_col):
    s = float(grp[start_col].min())
    g = gate.get(int(lid), 0)
    mark = "VIOLATION" if s < g - 1e-6 else "ok"
    print(f"  line_id={lid} ({lname.get(int(lid),'?')}) first_start={s:8.1f} gate={g:6d}  {mark}")
    if mark == "VIOLATION":
        bad.append((lid, s, g))
print("violations:", len(bad))
