# Flowstate — Week-32 Handoff (pick up here next week)

**Repo:** `C:\Users\jbdil\Flowstate\Plant_Scheduler` · **Branch:** `feature/handoff-ww32` (off main `c8cdd51`)
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
1. ✅ **Calendar starts at TODAY** — branch `feature/handoff-ww32`, commit `8dd451a`.
   `code/helpers/horizon.py` resolves the window from the wall clock
   (`[scheduler] anchor_mode = "today"|"fixed"`, `horizon_weeks = 3`). Plant Calendar
   opens on the current day with caption `Horizon: Fri 2026-08-07 → Fri 2026-08-28
   (WW32/WW33/WW34, rolling — starts today)`, shows a stale-anchor banner and a
   one-click **Roll calendar to today** (backs up `calendar_blocks.csv`, shifts every
   block by the anchor delta, rewrites `planning_start_date`). Finished blocks are
   hidden by default and merged back on save, so nothing is destroyed.
   9 new tests in `tests/test_horizon.py`; suite **37/37 green**. Browser-verified:
   banner rendered, roll executed, anchor moved 2026-08-03 → 2026-08-07, hours shifted
   −96, no traceback.
   ⚠️ **Caveat found while rolling** (fix in item 2/4): `demand_plan.csv` uses
   `week_index` buckets that assume a **Monday** anchor. Rolling to a mid-week day
   (Fri 8/7) silently re-defines week 0 as Fri→Thu. The horizon start should stay
   "today" while the *week grid* stays ISO-Monday-aligned — decouple them before the
   solver consumes the rolled anchor.
2. ✅ **Current state from manprg/manprg2 + cip_info** — branch `feature/handoff-ww32`, commit `dfc2dcf`.
   New module `code/helpers/current_state.py` (374 lines) + 18 tests (`tests/test_current_state.py`,
   all pass including real-data golden). Exposed in the Plant Calendar as a collapsible expander
   "🏭 Rebuild calendar from current plant state (manprg + cip_info)" that previews the resulting
   blocks and replaces the calendar on click (backing up the old one first). Classification:
   - Currently-running MO (latest start with cases made >0, not complete) → **locked**, runs to its
     estimated end via pro-rata remaining work, clamped to now+0.25 h.
   - Future MOs already in manprg → placed sequentially by start date, unlocked.
   - Completed MOs → dropped. CIP pseudo-MOs in manprg → treated as CIP blocks.
   - CIP: last-performed + next-scheduled from cip_info; future unscheduled CIPs spaced at
     MaxHoursBetweenCIP (120/144, per-line authoritative) through weeks 2-3.
   Suite **55/55** green. Browser-verified: expander renders, replace-button works.
3. ✅ **Solver fills remaining demand starting after each line's locked running MO.**
   `scenario_runner._overlay_current_state()` patches the solver's work-dir
   `initial_states.csv` before every solve with `available_from_hour` (from
   `current_state.line_free_h`), `initial_sku` (the SKU actually running, so
   changeovers are costed against reality rather than `CLEAN`) and
   `carryover_run_hours_since_last_cip_at_t0`. **12 of 14 lines** get a nonzero
   available_from (P09=83h, P10=231h, P22=210h…).

   ⚠️ **Three defects were found and fixed while *actually verifying* this
   against solver output — writing the file is not the same as the solver
   obeying it.** Commits `3213599`, `87d322a`.
   1. **The overlay was a silent no-op.** It imported `load_lines` from
      `helpers.lines_model` (it lives in `helpers.calendar_io`) inside a
      `try: … except ImportError: return`, so it returned before doing anything
      and the solver kept planning from the seeded fixture. The overlay now
      imports at the top level and **always returns a note** that is prefixed to
      the scenario log (`[current state] current state overlaid: 12 line(s)
      gated…`), so a future no-op is visible instead of silent.
   2. **The solver ignored `available_from` at relax level 3.** The gate lived
      *inside* `if (phase in ("sanity3","full")) and (not ignore_co):` in
      `model_builder.py`, so escalating the ladder to level 3 (`ignore_co`)
      discarded it. Measured before the fix: **12 of 12 gated lines started
      before their gate** (P10 started at hour 0 against a 231h gate — i.e. the
      solver planned straight over the running MO). A line's free hour is a
      physical fact, not a changeover preference, so it is now a hard
      constraint applied unconditionally at every relax level.
      After: **0 real violations** (the single remaining one is a trial pinned
      to an explicit start hour, which is deliberately exempt) and production
      blocks went *up*, 37 → 47.
   3. **The overlay made two-phase solves INFEASIBLE at every relax level.**
      Real `PreviousCIP` values gave carryovers up to 185h against a 120h
      `MaxHoursBetweenCIP` — "a CIP was already overdue before hour 0", which
      the CIP constraints cannot satisfy. Carryover is now clamped to
      `MaxHoursBetweenCIP - 1` (the same trick `phase2_scheduler` already used
      for week-1 states). Two-phase went **INFEASIBLE → ok, 155 blocks / 128
      production**.

   Repeatable checks: `scripts/check_solver_current_state.py` (runs a real
   scenario, asserts no line starts before its gate) and
   `scripts/diag_available_from.py` (per-`line_id` gate audit of a work dir).
   Suite **60/60** green.

4. ✅ **Demand source going forward: `demand_plan_summary.csv`.**
   `code/helpers/demand_summary_import.py` (new) reads Week/Product/kg_tons (UTF-8 BOM, comma),
   maps ISO weeks to anchor–relative week_index via `date.fromisocalendar()`, and emits the
   canonical `demand_plan.csv` schema. **101 orders, 63 SKUs, 3 weeks** (W0/W1/W2 = WW33/34/35),
   5,293 total tons. Wired into the Data Files page as "Import a cleaned demand plan summary (CSV)"
   — preview, write to `demand_plan.csv`, provenance saved. The file `data/reference/demand_plan_summary.csv`
   is in the repo. Suite **59/59** green.

## Open solver concern (investigate next week)
Single-phase full-horizon solves are slow/weak at the default 60s time limit (skill pitfall 9: needs 300–600s). 51 orders short of qmin at 300s on current data. Revisit time-limit defaults and whether the UI should warn/raise the limit when cross-week is on.

## Raw input analysis & tool ideas
_(added 2026-08-07 by the WW32 cron agent; refresh each run)_

### What the tool actually eats today (`data/reference/`)
| File | Shape | Friction |
|---|---|---|
| `manprg.txt` / `manprg2.txt` | `;`-delimited, header `Start date;Start time;Line;MO No.;Item;Designation;Pal;Type;Hours;Fct qty (Cas);Qty made (Cas);Fct qty [Kg];;Qty made [Kg];;Left (Cas)` | Two **unnamed** columns (the `Kg` unit labels) — a blind `read_csv` gets `Unnamed: 12/14`. Date+time split across two columns. Line is `LMH-P09`, everywhere else it's `P09`. Empty `Qty made` = not started (not zero). CIP appears as a pseudo-MO with `Item=CIP` and a fake `Fct qty (Cas)` — it is **not** production and must never be counted as output. |
| `cip_info.csv` | UTF-8 **BOM**, `ID,LineEquipment,PreviousCIP,MaxHoursBetweenCIP,ScheduledCIP,Notes` | Literal string `NULL` (not empty) in 3 of 6 cols. `MaxHoursBetweenCIP` is 120 or 144 per line — this is real constraint data that the solver should read instead of `[cip] interval_h = 120`. `Notes` carries work-order text (`WO#569452`) — useful, currently discarded. |
| `demand_plan.csv` | Derived, `order_id,sku,week_index,qty_target,lower_pct,upper_pct,due_start_hour,due_end_hour,priority` | `week_index`/`due_*_hour` are anchor-relative — **breaks silently when the anchor moves** (see item-1 caveat). No units column; kg is implicit. |
| `changeovers.csv` | 44,310 rows | A dense 211×211 matrix stored as a long CSV — ~1.4 MB parsed on every page load. |
| `capabilities_rates.csv` | 2,954 rows | Two rate columns (`nominal_rate_kgph`, `calc_rate_kgph`) with no documented precedence; code silently prefers `calc_`. |
| PDFs (`Week 32 2026 production schedule.pdf`) | The planner's real artifact | Parsed heuristically; no checksum/provenance on what was extracted. |

### Refresh — 2026-08-10 run (what this week's real data actually showed)
Numbers below are measured off the live exports, not estimated.

| Finding | Evidence | Why it matters |
|---|---|---|
| **`manprg` carries stale "started" MOs** | P12 has **5** MOs with `Qty made > 0` besides the real current one (29842/29843/29846/29847/29868, superseded by 29844); P14 has 1 | "latest start with made>0" is not sufficient on its own. `current_state` now keeps only the latest and warns about the rest — but the *export itself* has no "closed" flag, so the ambiguity is unresolvable from the file alone. **Ask the plant if VIF can emit an MO status column** (open/closed/suspended); it removes a whole class of guessing. |
| **`PreviousCIP` can be older than the line's own limit** | carryover up to **185 h** against `MaxHoursBetweenCIP = 120` | Fed straight into the solver this is "a CIP was overdue before hour 0" → **INFEASIBLE at every relax level**. Any field that feeds a solver bound needs a documented clamp. 3 of 14 lines have `PreviousCIP = NULL` entirely — that is *unknown*, not *just cleaned*, and currently silently becomes "clean at the anchor". |
| **`ScheduledCIP` can be in the past** | P15 `ScheduledCIP = 2026-08-05 23:00` with `PreviousCIP = 2026-08-06 17:03` — the "scheduled" CIP predates the last performed one | The column is not maintained after the CIP happens. Treat `ScheduledCIP < PreviousCIP` as stale and ignore it, and surface it as a data-quality warning. |
| **`initial_states.csv` is now a derived file, not an input** | the overlay rewrites `available_from_hour`, `initial_sku`, `carryover_…` in the work dir on every solve | The checked-in `data/reference/initial_states.csv` is a *fallback fixture*. It should say so in a header comment, or be renamed `initial_states.fallback.csv`, so nobody hand-edits it expecting an effect. |
| **`available_from_hour` had no test and no enforcement** | loaded in `data_loader` (line 258), used in *one* conditional branch of `model_builder`, referenced in `diagnostics` — and violated 12/12 times | Any column that crosses the app→solver boundary needs a round-trip assertion. See the tool idea below. |

### New tool ideas from this run
- **A solver *contract test* ("does the solver obey its inputs?").** The single
  highest-value thing found this week. For each input column that is supposed to
  constrain the solve (`available_from_hour`, `line_down` windows, `locked`
  blocks, due windows, `MaxHoursBetweenCIP`), run one small scenario and assert
  the output honours it. Two of these would have caught both solver defects
  above instantly. `scripts/check_solver_current_state.py` is the first one;
  generalise it into `tests/test_solver_contracts.py` (slow-marked).
- **Ban silent `except ImportError: return` in data-path code.** The no-op
  overlay shipped because a wrong import path was swallowed. A one-line grep in
  CI (`except (ImportError|Exception):\s*\n\s*return\b`) plus the "always return
  a note" convention makes this class of bug self-reporting.
- **A `data-quality` panel** that lists exactly the anomalies above (stale
  ScheduledCIP, NULL PreviousCIP, superseded started MOs, carryover past the
  limit) with a per-row "what Flowstate assumed instead". The planner then
  corrects the source system rather than the schedule.
- **Clamp registry.** Every place the code silently clamps a real-world value
  into a solver-legal range (`min(carryover, limit-1)`, `max(start, anchor)`,
  `qmin=0` for unproducible orders) should record the clamp in the run report.
  Today these are invisible, and an invisible clamp is how a schedule quietly
  stops matching the plant.

### Cleanliness fixes worth doing (cheap → valuable)
1. **One `LineId` vocabulary.** `LMH-P09` (manprg) vs `P09` (cip_info, lines.csv) vs `line_id` ints in `calendar_blocks.csv` (and `int(line[1:]) - 9` arithmetic hard-coded in `calendar.py`). One `helpers/lines_model.normalize_line()` at every import boundary, and drop the arithmetic.
2. **`NULL`/blank sentinels in one place.** A shared `helpers/nullish.py` (`NULL`, `""`, `nan`, `NaT`, `"None"` → `None`) instead of per-importer guards. `Qty made` empty ≠ 0 must be preserved as *not started*.
3. **Kill the `.0` float-string bug at the source** (`sku` `"280351.0"`): read all code columns with `dtype=str` in every importer, not with a display-time strip.
4. **Stamp provenance on every import.** `demand_plan.source.json` already does this — extend the same `<file>.source.json` (source path, mtime, sha256, row counts, anchor) to manprg, cip_info and PDF imports. Then the UI can say "live data is 3 h stale" instead of the planner guessing.
5. **Make CIP interval per-line authoritative.** `cip_info.MaxHoursBetweenCIP` (120/144) should override `[cip] interval_h`; today the two can silently disagree.
6. **Store wall-clock alongside hours.** Keep `start_h`/`end_h` for the solver but also write `start_iso`/`end_iso` to `calendar_blocks.csv`. Re-anchoring then becomes verifiable (and the item-1 rebase self-checks).
7. **Compress `changeovers.csv`** to a parquet/feather cache keyed by mtime, or a sparse "different-from-default" form — page loads currently re-parse 44 k rows.

### Tool ideas that would make Flowstate a better decision-support tool
- **A "Data freshness" strip on Home** — per source: age, row count, sha, and a red badge past a per-source SLA (manprg every 15 min, VIF exports daily). Prevents planning on yesterday's truth.
- **`flowstate doctor` CLI** — one command that validates every input against a schema (columns, dtypes, line vocabulary, orphan SKUs, BOM coverage, anchor consistency) and prints a pass/fail table. Cheap to build with the existing importers, and gives the cron agent a regression gate.
- **Schedule diff viewer** — "what changed since the plan of record": added/moved/dropped MOs with hours delta, not just two Gantts side by side.
- **Reason codes on solver output** — for each unmet order, the binding constraint (no capable line, line down, component short, week wall). Turns INFEASIBLE/short into an actionable list for the planner.
- **What-if lock levels** — beyond `locked` bool: `frozen` (running MO), `pinned to line`, `pinned to week`, `free`. Item 2 needs at least the first.
- **Component-availability overlay on the Gantt** — colour a block amber when the stock check says its components run out before it ends. Joins two features the tool already has.
- **Daily snapshot + replay** — write a dated snapshot of every input each morning; lets the planner (and the agent) answer "why did last Tuesday's schedule look like that".

## Housekeeping
- Temp worktree husks under `%TEMP%\fs-wt-*` persist (locked by zombie processes) — harmless, cleared on reboot; `git worktree prune` already run.
- `data/_scenario_work/`, `data/_backups/`, `data/versions/scenario_*` are QA churn — gitignored or restored; don't commit them.
- Design/plan docs live in `.hermes/plans/` (stock-check-design.md, live-ops-plan.md).
