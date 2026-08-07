# Flowstate — Week-32 Handoff (pick up here next week)

**Repo:** `C:\Users\jbdil\Flowstate\Plant_Scheduler` · **Branch:** `main` (HEAD `6c784cb`)
**All work merged.** Suite: 28/28 green. App runs on port 8501 (`.venv\Scripts\python.exe`, scrub PYTHONPATH first; use `scripts/fs-launch.ps1` etc.).

## What's on main now
- **Stock Check** (VIF BOM explosion, component coverage, at-risk SKUs, BT001/BT002 in-house).
- **AZAP raw import** (CSV/xlsx, 3-week rolling window, re-anchors calendar, ISO week labels WW32/33/34).
- **Production schedule PDF import** (WW32/WW33 → version or baseline).
- **Live ops:** manprg progress (MO-keyed completion, now-running table, progress fills), CIP overlay from cip_info, optional SQL (NPA), Settings page for data sources.
- **Solver fixes:** maximize_production on single-phase path; unproducible orders (no capable/available line) get qty_min=0 instead of INFEASIBLE; stale pre-anchor trials skipped.
- **Rough-draft-first workflow** on Generate page; scenario Gantt preview (no promote).

## THE NEXT TASK (user's #1 priority): known-good current state → then optimize
The user wants to **stop starting from the old fixture**. Build the calendar from ground truth:
1. **Calendar starts at TODAY** (not a fixed anchor). First visible date = current day; horizon = today + 3 weeks. Nothing in the past is shown.
2. **Current state from manprg/manprg2 + cip_info**, per line:
   - Currently-running MO (latest start with cases made >0, not complete) → **locked**, runs to its estimated end; solver can't move it.
   - Future MOs already in manprg (e.g. P16: 29807, 29918–29922) → placed sequentially by start date.
   - Completed MOs → dropped (don't render).
   - CIP: last-performed + next-scheduled from cip_info placed on calendar; future unscheduled CIPs spaced at MaxHoursBetweenCIP into weeks 2–3.
3. **This is the initial state** — a feasible (unoptimized) schedule. Then the solver fills remaining demand from the demand plan, starting after each line's locked running MO.
4. **Demand source going forward:** `demand_plan_summary.csv` (Week, Product, kg_tons; weeks 33/34/35; NO machine col) — cleaner than the raw AZAP export; the user suspects the raw plan was confusing things. Reconcile the AZAP importer to consume this summary shape (or map raw→summary).

## Open solver concern (investigate next week)
Single-phase full-horizon solves are slow/weak at the default 60s time limit (skill pitfall 9: needs 300–600s). 51 orders short of qmin at 300s on current data. Revisit time-limit defaults and whether the UI should warn/raise the limit when cross-week is on.

## Housekeeping
- Temp worktree husks under `%TEMP%\fs-wt-*` persist (locked by zombie processes) — harmless, cleared on reboot; `git worktree prune` already run.
- `data/_scenario_work/`, `data/_backups/`, `data/versions/scenario_*` are QA churn — gitignored or restored; don't commit them.
- Design/plan docs live in `.hermes/plans/` (stock-check-design.md, live-ops-plan.md).
