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
3. ✅ **Solver fills remaining demand from the demand plan, starting after each line's locked running MO.**
   `scenario_runner._overlay_current_state()` (new) patches the solver's work-dir `initial_states.csv`
   with `available_from_hour` (from `current_state.line_free_h`) and `initial_sku` (the running SKU)
   before every solve. **12 of 14 lines** now have a nonzero available_from. The solver is blocked
   from scheduling anything on a line before that hour; running MOs are effectively locked. Verified:
   the overlay writes correct hours (e.g. P09=83h, P10=231h, P22=210h) and correct SKUs into the
   work dir. Best-effort: if the manprg feeds are unavailable, the solver falls back cleanly.
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
