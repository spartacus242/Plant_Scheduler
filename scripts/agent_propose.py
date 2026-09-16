#!/usr/bin/env python3
"""Agent proposal runner — one reasoned sandbox version per invocation.

The planning agent's loop (charter end-state, dry-run series):
  1. situational input  : reconcile_engine findings + live stock report
  2. approved policies  : the stock policy on the SOLVER'S work-dir demand
                          copy (helpers.agent_policy.apply_stock_policy:
                          per-order caps from the time-phased projection,
                          earliest-start floors after a receipt — slice 3,
                          2026-09-15; flat DNS trim as the fallback,
                          user-approved 2026-08-14) — data/reference is
                          never touched
  3. action             : Scenario E (current state + demand) via
                          scenario_runner, with every input mutation logged
  4. output             : a named sandbox version whose notes carry the
                          reasoning, the policy notes and the post-solve
                          stock cross-check. Promotion stays a HUMAN click
                          on Compare & Promote.

Usage:
    python scripts/agent_propose.py [--time-limit 300] [--name "..."]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

import pandas as pd  # noqa: E402

from helpers.agent_policy import apply_stock_policy  # noqa: E402
from helpers.calendar_io import load_calendar  # noqa: E402
from helpers.reconcile_engine import BLOCKING, assess_plan, summary  # noqa: E402
from helpers.scenario_runner import SCENARIOS, run_scenario  # noqa: E402
from helpers.scorecard_engine import delta_narrative, score_calendar  # noqa: E402
from helpers.version_manager import (delete_version, list_versions,  # noqa: E402
                                     save_version)
from stockcheck.api import stock_check_report  # noqa: E402

DATA = ROOT / "data"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="F",
                    help="F = fill the tail (committed plan fixed, default); "
                         "E = full current-state re-optimization (diagnostic)")
    ap.add_argument("--time-limit", type=int, default=600,
                help="Scenario F solves ONCE at level 0 (soft demand) - "
                     "the budget IS the fill quality; 600s default")
    ap.add_argument("--name", default=f"Agent proposal {datetime.now():%Y-%m-%d %H:%M}")
    ap.add_argument("--out", default=None, help="write the run report JSON here")
    args = ap.parse_args()

    # ── 1. situational input ────────────────────────────────────────────
    stock = stock_check_report(DATA, DATA / "reference")
    findings = assess_plan(DATA, stock_report=stock)
    counts = summary(findings)

    # ── 2+3. approved policy inside the agent seam, then solve ──────────
    policy_notes: list[str] = []
    policy: dict = {"res": None}

    def patch(work: Path) -> list[str]:
        res = apply_stock_policy(work, stock)
        policy["res"] = res
        policy_notes.extend(res.notes[1:])
        return res.notes[:6]

    scenario = next(s for s in SCENARIOS if s["id"] == args.scenario.upper())
    result = run_scenario(scenario, DATA, time_limit=args.time_limit,
                          work_dir_patch=patch)
    if not result["ok"]:
        print("SOLVE FAILED"); print(result["log"][-2000:])
        return 1

    proposal, prop_score = result["calendar"], result["scorecard"]
    # First log line = the staging notes ([current state] ...): committed
    # windows, demand subtraction, greedy seed totals. They belong IN the
    # proposal's reasoning — a silent staging degradation must be visible.
    staging_line = (result.get("log") or "").split("\n", 1)[0][:1500]

    # ── 4a. post-solve stock cross-check (belt and braces) ──────────────
    res = policy["res"]
    capped_skus = set(res.capped_skus) if res is not None else set()
    prod = proposal[proposal["block_type"] == "production"]
    flagged = prod[prod["sku"].astype(str).isin(capped_skus)]
    flagged_kg = float(pd.to_numeric(flagged["qty_kg"], errors="coerce")
                       .fillna(0).sum())

    official = load_calendar(DATA / "calendar_blocks.csv")
    base_score = score_calendar(official, week_label="official", data_dir=DATA)

    # ── 4b. save the sandbox version with the reasoning ─────────────────
    reasons = [
        f"AGENT PROPOSAL (generated {datetime.now():%Y-%m-%d %H:%M}). Scenario "
        f"{args.scenario.upper()} ({scenario['name']}), {args.time_limit}s, "
        f"relax level {result.get('relax_level')}.",
        f"Situational input: {counts.get(BLOCKING, 0)} blocking finding(s) "
        f"on the official board at solve time.",
        ("APPROVED POLICY — " + (res.summary() if res is not None else "stock policy")
         + f" in the solver's input copy ({len(policy_notes)} order(s) touched, "
         f"{len(capped_skus)} SKU(s)): qty_min→0, qty_max capped at what the "
         "components support, earliest start after a receipt + buffer. "
         "Details:\n  " + "\n  ".join(policy_notes))
        if policy_notes else "No component-blocked demand this run.",
        (f"Post-solve stock cross-check: {len(flagged)} block(s) / "
         f"{flagged_kg:,.0f} kg still on DNS SKUs (inside the capped bounds "
         "— capacity the components CAN support)." if len(flagged) else
         "Post-solve stock cross-check: clean — no proposed block uses a "
         "component-blocked SKU beyond its cap."),
        f"STAGING: {staging_line}",
    ]
    for v in list_versions(DATA):
        if str(v.get("source", "")).startswith("agent:"):
            delete_version(v["slug"], DATA)
    slug = save_version(
        args.name, proposal,
        prop_score.to_dict() if hasattr(prop_score, "to_dict") else prop_score,
        DATA,
        pros="Runnable as-is: current MOs preserved, downtimes + CIP intervals hard, component-blocked demand capped at achievable.",
        cons="Review the DNS trims and the composite trade-offs ('show me why') before promoting.",
        notes="\n\n".join(reasons), source="agent:propose",
        extra_meta={"fill_gates": result["fill_gates"]}
        if result.get("fill_gates") else None,
    )

    report = {
        "slug": slug,
        "relax_level": result.get("relax_level"),
        "composite_official": base_score.to_dict().get("composite"),
        "composite_proposal": (prop_score.to_dict()
                               if hasattr(prop_score, "to_dict")
                               else prop_score).get("composite"),
        "narrative": delta_narrative(base_score, prop_score)[:10],
        "blocks": int(len(prod)),
        "dns_orders_trimmed": len(policy_notes),
        "dns_blocks_in_proposal": int(len(flagged)),
        "dns_kg_in_proposal": flagged_kg,
        "stock_policy_mode": res.mode if res is not None else "off",
        "stock_policy_floors": res.n_floors if res is not None else 0,
    }
    # Fill-window verdict (Scenario F): the committed layer cancels out —
    # this is the honest headline for an F proposal (see Compare page).
    if result.get("fill_gates"):
        gates = result["fill_gates"]
        base_w = score_calendar(official, week_label="official (fill window)",
                                data_dir=DATA, fill_gates=gates)
        prop_w = score_calendar(proposal, week_label="proposal (fill window)",
                                data_dir=DATA, fill_gates=gates)
        report["fill_window"] = {
            "composite_official": base_w.composite,
            "composite_proposal": prop_w.composite,
            "narrative": delta_narrative(base_w, prop_w)[:10],
        }
    out = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
