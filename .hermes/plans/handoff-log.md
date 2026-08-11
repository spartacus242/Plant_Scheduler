# Flowstate — running handoff log

**Repo:** `C:\Users\jbdil\Flowstate\Plant_Scheduler` · **Working branch:** `feature/handoff`
**Base:** cut from `feature/solver-current-state` @ `adbde36` (24 commits ahead of `main`).
`main` is STALE — it lacks `helpers/horizon.py`, `helpers/current_state.py`, `code/solver/`
and ~80 of the tests, so branching off `main` produces an app that cannot import its own
Plant Calendar. Never re-base this branch onto `main` without re-checking that.

Consolidated 2026-08-10 from `.hermes/plans/handoff-ww32.md` (all still-open items folded in).
Suite: **110 passed**. App: `.venv\Scripts\python.exe`, port 8501, scrub `PYTHONPATH` first.

---

## Task list

1. **DONE** — Calendar starts at TODAY (rolling horizon, `helpers/horizon.py`, `anchor_mode`). — `8dd451a`
2. **DONE** — Current state from manprg/manprg2 + cip_info (`helpers/current_state.py`). — `dfc2dcf`
3. **DONE** — Solver fills demand after each line's locked running MO (work-dir overlay,
   unconditional `available_from` floor, carryover clamp). — `3213599`, `87d322a`
4. **DONE** — `demand_plan_summary.csv` is the sole demand import (self-anchoring, 101 orders / 63 SKUs / 3 weeks).
5. **DONE** — Scenario E: current-state MOs as locked solver orders + `mo_changes.csv` for VIF write-back. — `231ee14`
6. **DONE** — Holding area auto-populate, click-a-SKU block, Line/SKU capability check. — `adbde36`
7. **DONE** — Mode-aware solver time limit on the Generate page: the budget now defaults to
   **300 s** whenever the run will be single-phase (cross-week ON, or a single-phase scenario
   such as E is selected) and shows a hard **warning below 120 s** explaining that CP-SAT
   returns UNKNOWN at relax level 0 and the ladder escalates, so a too-short budget looks
   exactly like an infeasible model. Two-phase runs keep the toml's 60 s. The control was moved
   below the flexibility checkboxes so it can react to them, and it is keyed per mode so
   flipping cross-week re-seeds the recommended default instead of keeping a stale 60.
   Browser-verified on the live app (two-phase → 60 s, no banner; cross-week ON → 300 s +
   "Single-phase run" caption; forced to 60 s → warning renders). — `f0cfc73`
8. **DONE** — **Solver horizon was pinned at 336 h while the app moved to 504 h.** — `57715ec`
   `Params.horizon_h` defaults to 336 and `main()` never read `[scheduler] horizon_hours`;
   Phase 2 of the two-phase driver hard-coded `horizon_h=336`. `demand_plan.csv` holds
   **38 of 101 orders in week 2** with `due_start_hour = 336`, `due_end_hour = 503`, so the
   solver was being asked to fit three weeks of demand into two weeks of calendar. It did not
   drop those orders outright (`allow_week1_in_week0` rewrites their effective start to hour
   120, so they stay representable) — it crammed them into the first two weeks and then came
   up short. Fixed by reading `horizon_hours` (falling back to `horizon_weeks * 168`) into
   `P.horizon_h` and passing `P.horizon_h` into the Phase-2 `Params`.
   **A/B proof** — `scripts/ab_horizon_run.sh`, identical inputs, single-phase, 240 s each,
   both FEASIBLE at relax level 3:

   | | horizon 336 (before) | horizon 504 (after) |
   |---|---|---|
   | blocks placed | 167 | **244** |
   | orders short of qmin | **40** | **4** |
   | late orders | 0 | 14 |
   | lines used | 12 | 14 |
   | max scheduled end hour | 336 (pinned to the wall) | 499 |

   Demand coverage is the headline: 40 → 4 orders short. The 14 "late" orders are week-2
   work now genuinely placed against soft due dates instead of being silently squeezed.
9. **NOT STARTED** — Generalise `scripts/check_solver_current_state.py` into
   `tests/test_solver_contracts.py` (slow-marked): for each input column that is supposed to
   constrain the solve (`available_from_hour`, downtime windows, locked blocks, due windows,
   `MaxHoursBetweenCIP`, locked line for current MOs), assert the OUTPUT honours it. Two of
   these would have caught both 2026-08-10 solver defects instantly.
10. **NOT STARTED** — Warm-start / `AddHint`. `grep -c AddHint code/solver/*.py` = **0**.
    The manprg queued MOs are a ready-made feasible seed for `present`, `seg_a_start`,
    `seg_a_run` — the single highest-leverage CP-SAT change available on this model.
11. **NOT STARTED** — Changeover-matrix compression. `changeovers.csv` is 44,310 rows / 1.39 MB,
    a dense 211×211 matrix stored long. Group SKUs into format families and penalise only
    between-family transitions; cache as parquet keyed by mtime. The Generate page re-parses
    the whole file into a dict on every page load, on top of the solver's own load.
12. **NOT STARTED** — Symmetry breaking on `present` across interchangeable capable lines.
13. **NOT STARTED** — CIP-deadline vs `available_from` conflict: when
    `avail_from + (interval − carry)` leaves no legal CIP window, project an extra CIP
    before `avail_from` rather than letting the model go INFEASIBLE.
14. **NOT STARTED** — Running MO is not a `NoOverlap` interval; only the `available_from`
    floor models it. Add a hard downtime interval so CIP placement sees the blocked time.
15. **NOT STARTED** — Clamp registry: every silent clamp (`min(carryover, limit−1)`,
    `qmin = 0` for unproducible orders, `max(start, anchor)`) should be recorded in
    `feasibility_report.json` and surfaced. An invisible clamp is how a schedule quietly
    stops matching the plant.
16. **NOT STARTED** — Data-quality panel: stale `ScheduledCIP` (P15's predates its own
    `PreviousCIP`), `NULL PreviousCIP` (3 of 14 lines — unknown, not clean), superseded
    started MOs (P12 has 5), carryover past the limit — each with "what Flowstate assumed instead".
17. **NOT STARTED** — One `LineId` vocabulary (`LMH-P09` vs `P09` vs int `line_id`, plus the
    hard-coded `int(line[1:]) - 9` arithmetic in `calendar.py`).
18. **NOT STARTED** — Rename `data/reference/initial_states.csv` → `initial_states.fallback.csv`
    (or add a header comment): it is a derived file now, rewritten in the work dir on every
    solve, and hand-edits to the checked-in copy do nothing.
19. **NOT STARTED** — `flowstate doctor` CLI: validate every input against a schema
    (columns, dtypes, line vocabulary, orphan SKUs, anchor consistency) and print a pass/fail
    table. Gives this cron agent a regression gate.
20. **NOT STARTED** — Workflow simplification: collapse the Home page's 6 steps to the user's
    3 (import demand → build current state → solve), split "Build rough draft" from "Optimize",
    make "Roll calendar to today" automatic, and replace the destructive-looking
    "Hide blocks that already finished" checkbox with a greyed past region on the Gantt.
21. **NOT STARTED (new, this run)** — **Week-grid constants are hard-coded to a 2-week
    Monday grid.** `WEEK0_END = 167` / `WEEK1_START = 168` are duplicated in
    `model_builder.py`, `diagnostics.py` and `phase2_scheduler.py`, and there is no
    `WEEK2_*` at all. With a 3-week horizon the two-phase split is "week 0 vs everything
    else", and with `anchor_mode = "today"` a mid-week anchor makes "week 0" a fragment
    while `demand_plan.week_index` still assumes Monday buckets. Derive the week grid from
    one place (`helpers/horizon`) and pass it into the solver instead of three copies of a
    literal.
22. **NOT STARTED (new, this run)** — **Nothing reports orders the model cannot even
    represent.** An order with `max_len == 0` (due window outside the horizon, or a due
    window shorter than `min_run_hours`) is silently absent from the solution; it is not
    "short of qmin", it is invisible. `feasibility_report.json` should carry an
    `unrepresentable_orders` list with the reason, and the Generate page should show it.
    This is the defect class that hid item 8 for a full week.
23. **NOT STARTED (new, this run)** — `data_loader.py` lines 321/371 default a missing
    `due_end` to the literal `336 - 1`, independent of `P.horizon_h`. Same magic number,
    third location — fold into item 21's single source of truth.

---

## Open solver concerns

- **Time limit vs mode** (mitigated by item 7, not solved): a level-0 `UNKNOWN` caused by a
  short budget is indistinguishable in `feasibility_report.json` from genuine infeasibility.
  The report should record `status_at_level_0` and whether the wall clock was exhausted, so
  the UI can say "ran out of time" instead of "infeasible".
- **Relaxation flags must never hide physical facts.** `available_from` was nested inside the
  changeover block and vanished at relax level 3 (12/12 lines violated). Audit the CIP block,
  the downtime block and the locked-line constraint for the same shape.
- **Two-phase cannot place multi-week current MOs** — that is why scenario E forces
  single-phase. With item 8 raising the horizon to 504 h, single-phase gets meaningfully
  harder; expect to need the full 300–600 s and re-check whether the decomposition can be
  restored as "week 0 hard + weeks 1-2 abstract" instead of one flat model.
- **`validate_schedule`'s "checks passed N/4" is expected to be low on relax-level-3 solves.**
  Only `no_overlaps` failing is a real regression signal.

---

## Raw input analysis & tool ideas

`data/reference/` as of this run:

| File | mtime | Note |
|---|---|---|
| `capabilities_rates.csv` | 08-10 18:23 | fresh; `calc_rate_kgph` is authoritative (`use_sku_rates = true`) |
| `changeovers.csv` | 08-03 | 1.39 MB / 44,310 rows, re-parsed per page load — item 11 |
| `cip_info.csv` | 08-10 17:37 | fresh; `NULL` literals, per-line 120/144 max interval |
| `demand_plan.csv` | 08-10 12:38 | 101 orders, weeks 0/1/2 = 33/30/38, `due_end` out to 503 |
| `initial_states.csv` | **08-07** | stale fallback fixture, overwritten in the work dir — item 18 |
| `downtimes.csv` | 08-07 | check for pre-anchor dates after every re-anchor |
| `trials.csv` | 08-10 14:13 | pinned rows; a trial overlapping a line gate = INFEASIBLE at every level |
| `manprg.txt` / `manprg2.txt` | 08-10 17:38 | fresh; `LMH-Pxx` line vocabulary, two unnamed Kg columns |

Silent-failure paths still open: no provenance on manprg/cip_info imports (only
`demand_plan.source.json` exists); `NULL`/blank sentinels handled per-importer; SKU `.0`
float-string bug fixed at display time rather than at read time.

---

## Research findings

- **Warm start (`AddHint`)** — not used anywhere in this model. Highest-leverage change:
  seed `present` / `seg_a_start` / `seg_a_run` from the manprg queued sequence.
- **Decomposition beats one flat model** at this size. Two-phase already decomposes;
  cross-week and scenario E throw it away. The literature pattern is "solve the near term
  exactly, the far term at bucket-level capacity, then roll" — which is exactly the user's
  own rolling-horizon intent (2 weeks locked, week 3 fluid).
- **Objective structure is sound.** Production scaled ×1000 dominating secondary
  changeover/makespan/idle terms is the recommended lexicographic-by-weight pattern; adding
  more small-weight terms is what causes thrashing.
- **Symmetry** across interchangeable capable lines with `max_lines_per_order = 2` is real
  and unbroken.
- **Solve class**: production scheduling with CIP intervals and sequence-dependent setups is
  a minutes-to-hours problem, not seconds. 60 s was never a defensible default for a
  full-horizon model — item 7 addresses the UI half of that.

---

## Run log

### 2026-08-10 (cron run)
- Created working branch `feature/handoff` off `feature/solver-current-state` @ `adbde36`
  (documented above why not off `main`).
- Consolidated `handoff-ww32.md` into this log; all four WW32 items confirmed DONE, the
  open items became the numbered review queue.
- **Built item 7** — mode-aware solver time limit on the Generate page. Browser-verified all
  three states on the live app at :8501. Suite 110/110 green. Commit `f0cfc73`.
- **Review pass found item 8** (the 336 h vs 504 h horizon pin) by cross-reading
  `flowstate.toml`, `data_loader.Params`, `phase2_scheduler.main()` and the
  `max_len` computation in `model_builder`, then confirming against the real
  `demand_plan.csv` (38 week-2 orders, `due_start_hour = 336`). Implemented the fix and
  proved it with `scripts/ab_horizon_run.sh` — two real 240 s solver runs, 336 vs 504:
  **167 → 244 blocks, 40 → 4 orders short of qmin**, schedule now reaches hour 499 instead
  of stopping dead at 336. Compile + full suite green after the solver edit.
- New review items 21–23 appended.
