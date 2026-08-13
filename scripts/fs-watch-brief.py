#!/usr/bin/env python3
"""Flowstate Step-1 Watch/Compare/Brief loop.

READ-ONLY. The agent does NOT modify any schedule or input. It ingests the
live data, checks freshness, compares the current (baseline) schedule to the
demand plan, runs a stock check, and prints a structured brief for Carsten
(human in the loop) or a follow-up agent to act on.

Prints a concise brief to stdout when there is something worth flagging, and
a short "all quiet" line otherwise. Cron can run this on a cadence and it is
safe (never writes, never solves, never locks).

Exit 0 always unless a hard error occurs.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Repo root: resolve explicitly so the script works both from the repo's
# scripts/ dir and when copied into the hermes scripts dir (where parents[1]
# would resolve wrong). Prefer FLOWSTATE_REPO env var, else fall back to a
# known dev-box location.
_ENV_REPO = os.environ.get("FLOWSTATE_REPO", "").strip()
_REPO = Path(_ENV_REPO) if _ENV_REPO else None
if _REPO is None:
    _KNOWN = Path(r"C:\Users\jbdil\Flowstate\Plant_Scheduler")
    if (_KNOWN / "code").exists() and (_KNOWN / "data").exists():
        _REPO = _KNOWN
if _REPO is None:
    _cand = Path(__file__).resolve()
    for _ in range(4):
        if (_cand / "code").exists() and (_cand / "data").exists():
            _REPO = _cand
            break
        _cand = _cand.parent
if _REPO is None:
    raise SystemExit("fs-watch-brief: cannot locate Flowstate repo "
                      "(set FLOWSTATE_REPO env var)")

# Re-exec into the repo venv if the current interpreter lacks the deps.
_VENV_PY = _REPO / ".venv" / "Scripts" / "python.exe"
if _VENV_PY.exists():
    try:
        import pandas  # noqa: F401  (probe deps)
    except Exception:  # noqa: BLE001
        import subprocess

        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
        env["FLOWSTATE_REPO"] = str(_REPO)
        sys.exit(subprocess.call([str(_VENV_PY), str(Path(__file__).resolve())],
                                 env=env))

sys.path.insert(0, str(_REPO / "code"))

from helpers.data_health import assess, summary  # noqa: E402
from helpers.paths import data_dir  # noqa: E402
from stockcheck.api import stock_check_report  # noqa: E402

DATA = Path(data_dir())
REF = DATA / "reference"
VIF = DATA / "stockcheck" / "dev_vif"
# "Current schedule" = the most recently saved baseline version to compare
# against the demand plan. naive_demand_plan is the strawman laid out as-is.
CURRENT_VERSION = DATA / "versions" / "naive_demand_plan"


def _fmt_t(kgh: float) -> str:
    return f"{kgh/1000:.1f} t"


def demand_by_week() -> dict[int, float]:
    import pandas as pd

    df = pd.read_csv(REF / "demand_plan.csv")
    return df.groupby("week_index")["qty_target"].sum().to_dict()


def current_schedule_tonnage() -> dict[int, float]:
    import pandas as pd

    blocks = pd.read_csv(CURRENT_VERSION / "calendar_blocks.csv")
    prod = blocks[blocks["block_type"] == "production"]
    out: dict[int, float] = {0: 0.0, 1: 0.0, 2: 0.0}
    for _, r in prod.iterrows():
        w = int(float(r["start_h"]) // 168)
        out[w] = out.get(w, 0.0) + float(r["qty_kg"])
    return out


def main() -> None:
    issues: list[str] = []
    brief: list[str] = ["## Flowstate watch — brief", ""]

    # 1. Data freshness (reuses data_health.assess)
    try:
        health = assess(DATA)
        s = summary(health)
        brief.append(f"**Health:** {s.get('OK',0)} ok · {s.get('STALE',0)} stale · "
                     f"{s.get('MISSING',0)} missing · {s.get('ERROR',0)} error")
        stale = [h for h in health if h.state in ("STALE", "ERROR", "MISSING")]
        for h in stale[:6]:
            brief.append(f"- ⚠️ {h.name}: {h.detail}")
            issues.append(f"health:{h.key}={h.state}")
    except Exception as e:  # noqa: BLE001
        brief.append(f"- ❌ health assess failed: {e}")

    # 2. Demand vs current schedule tonnage
    try:
        dem = demand_by_week()
        sched = current_schedule_tonnage()
        brief.append("")
        brief.append("**Demand vs current schedule (tonnage):**")
        brief.append("| week | demand | scheduled | gap |")
        brief.append("|---|---|---|---|")
        for w in sorted(set(dem) | set(sched)):
            d = dem.get(w, 0.0)
            sc = sched.get(w, 0.0)
            gap = (sc - d) / d * 100 if d else 0.0
            flag = " ⚠️" if abs(gap) > 5 else ""
            brief.append(f"| W{w} | {_fmt_t(d)} | {_fmt_t(sc)} | {gap:+.0f}%{flag} |")
            if abs(gap) > 5:
                issues.append(f"week{w} gap {gap:+.0f}%")
    except Exception as e:  # noqa: BLE001
        brief.append(f"- ❌ demand-vs-schedule compare failed: {e}")

    # 3. Stock check (reuses stock_check_report against dev VIF snapshot)
    try:
        if VIF.exists() and (VIF / "ediact 3.csv").exists():
            rep = stock_check_report(DATA, VIF)
            if "error" in rep:
                brief.append("")
                brief.append(f"- ❌ stock check: {rep['error']}")
            else:
                brief.append("")
                brief.append("**Stock check:**")
                sv = rep.get("schedule_view", [])
                dv = rep.get("demand_view", [])
                from collections import Counter

                s_counts = Counter(b["status"] for b in sv)
                d_counts = Counter(d["status"] for d in dv)
                brief.append(f"- schedule blocks: {len(sv)} → "
                             + ", ".join(f"{k}={v}" for k, v in sorted(s_counts.items())))
                brief.append(f"- demand orders: {len(dv)} → "
                             + ", ".join(f"{k}={v}" for k, v in sorted(d_counts.items())))
                no_bom = rep.get("no_bom_skus", [])
                if no_bom:
                    brief.append(f"- ⚠️ {len(no_bom)} SKU(s) with no BOM: {', '.join(map(str, no_bom[:8]))}")
                    issues.append(f"stock_no_bom:{len(no_bom)}")
                bad = {k: v for k, v in d_counts.items()
                       if k in ("SHORT", "DO_NOT_SCHEDULE", "CRITICAL")}
                if bad:
                    brief.append(f"- ⚠️ demand coverage problems: {bad}")
                    issues.append("stock_coverage")
        else:
            brief.append("")
            brief.append("- ℹ️ no VIF snapshot at dev_vif — stock check skipped")
    except Exception as e:  # noqa: BLE001
        brief.append(f"- ❌ stock check failed: {e}")

    brief.append("")
    brief.append(f"_generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    # Print: full brief always (so cron sees the latest state), but tag quiet
    print("\n".join(brief))
    if not issues:
        print("\n**All quiet** — no critical data/stock/demand gaps flagged.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"flowstate-watch: ERROR {e}", file=sys.stderr)
        sys.exit(1)
