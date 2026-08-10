"""End-to-end: run one real scenario through the CP-SAT solver with the
current-state overlay active, and prove the overlay reached the work dir."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import pandas as pd

from helpers.paths import data_dir
from helpers.scenario_runner import run_scenario

dd = data_dir()
sc = {"id": "cs_smoke", "name": "current-state smoke",
      "objective": "balanced", "two_phase": False}
res = run_scenario(sc, dd, time_limit=60,
                   python_exe=str(ROOT / ".venv" / "Scripts" / "python.exe"))

print("ok:", res["ok"], "rc:", res["returncode"])
print("overlay note:", res["log"].splitlines()[0] if res["log"] else "(none)")

work = dd / "_scenario_work" / "cs_smoke"
init = pd.read_csv(work / "initial_states.csv")
gated = init[init["available_from_hour"].astype(float) > 0]
print(f"gated lines in work dir: {len(gated)}/{len(init)}")
print(init[["line_name", "initial_sku", "available_from_hour",
            "carryover_run_hours_since_last_cip_at_t0"]].head(8).to_string(index=False))

feas = res.get("feasibility") or {}
print("relax_level:", res.get("relax_level"), "feas keys:", sorted(feas)[:8])
if res["ok"]:
    cal = res["calendar"]
    prod = cal[cal["block_type"] == "production"]
    print(f"solved blocks: {len(cal)} ({len(prod)} production)")
    starts = prod.groupby("line_name")["start_h"].min()
    bad = []
    for ln, s in starts.items():
        g = init[init["line_name"].astype(str).str.upper() == str(ln).upper()]
        if len(g):
            gate = float(g.iloc[0]["available_from_hour"])
            if s < gate - 1e-6:
                bad.append((ln, s, gate))
    print("lines starting BEFORE their gate:", bad or "none")
else:
    print("LOG TAIL:\n", res["log"][-1500:])
