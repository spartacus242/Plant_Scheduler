# Flowstate — Build Progress

**Updated:** 2026-08-13 · **Branch:** `develop` (the one dedicated branch — all work happens here; `main` stays frozen until go/no-go)
**Source of truth:** [Charter v3 — DECISIONS LOCKED](.hermes/plans/2026-08-12-charter.md) · **Suite:** ✅ **196 passed / 0 failed / 3 skipped / 8 deselected** (skips = P6/P8 need a relax ≤2 artifact; see P2 note)

## Where to look

| What | Where |
|---|---|
| This progress board | `PROGRESS.md` (repo root) |
| The charter (goal, measurables, decisions) | `.hermes/plans/2026-08-12-charter.md` + `2026-08-12-flow-diagram.html` |
| Per-phase work plans ("dispatch docs") | `.hermes/plans/p1-dispatch1-…`, `p2-dispatch1-…`, `p2-dispatch2a-…` |
| Live data feeds (what the plant is doing now) | GitHub repo **`spartacus242/flowstate-live-data`** (pushed by the file-drop bridge) |
| Daily scorecard runs | `data/scorecards/Week-2026-08-13_*.json` (new file each run) |
| Code history | `git log` on `develop` |

## Phase status (from the charter)

| Phase | What it is | Status | Evidence |
|---|---|---|---|
| **P0** | Sign-off, tidy tree, charter committed | ✅ **DONE** | `6afd246` (charter+diagram), `5297b29` (WW33 baseline + CIP test) |
| **P1** | Live data links (manprg/CIP/stock/AZAP) | ✅ **DONE** | `5117c8d` demand path + staleness; `845dcf6`/`49b904f` GitHub bridge; demand link = `demand_plan_summary.csv`. **Stock check live link landed 2026-08-14:** user pushed the VIF set (ediact 3/4, jestkexp/2, azapart, rmpkitems + receiving xlsm) to the bridge repo; pull conf + .gitignore extended; Stock Check/Reconcile default to `data/reference/`; VIF freshness rule in data_health (26 h). **Three live-format quirks fixed in vif_import** (each silently broke the engine): thousands-comma quantities (8,403/15,267 BOM qtys were NaN → all UNK), mixed date conventions in one export (ediact day-first, jestkexp BBD month-first — per-column auto-detect), headerless azapart (first SKU eaten). Reconcile stock mapping fixed to the real status vocabulary (AT_RISK blocking / TIGHT warn — 'SHORT' never existed). Stock page anchored to the real planning anchor (was showing Feb dates). **Live results:** 70/97 blocks covered, 24 AT_RISK, 3 TIGHT; 24 demand SKUs DO_NOT_SCHEDULE; 1,220 receiving appointments parsed. Suite 232 green. |
| **P2** | Runnable plan core: flat rates, maintenance out, constraint audit | 🟡 **DONE (2 notes)** | `6ecb0bc` flat rates ON; `f3bf013` maintenance out; `d646fd7` probes P1–P9; `e778e6d` min-run floor for current MOs (user-approved, qty-aware). **Notes:** (a) P6/P8 probes need a relax ≤2 artifact — level 0 is hard-INFEASIBLE on this dataset (P11/P13 down + demand) and scenario-E skips levels 1–2; run an A–D solve to exercise them; (b) TRIALS present-but-0-kg oddity open |
| **P3** | Honest scorecard | ✅ **DONE** | `c462769` Service kg real (solver qty_kg → calendar → score, browser-verified 1.8 kg real excess vs fake 0 before); estimated fallback flagged "(estimated)"; trials N/A when no input; CIP gate regression-tested; guard test fixed (was inert). **Open:** Plant Calendar save wipes qty_kg (frontend TS change, P4 scope) |
| **P4** | Daily screens: Reconcile → bidirectional sandbox → lock/export → track | ⬜ PENDING | Sandbox survives; screens not rebuilt yet |
| **P5** | 3-week daily trial with Carsten | ⬜ PENDING | |
| **P6** | Go / no-go | ⬜ PENDING | |

## Gate — the suite right now

✅ **165 passed / 0 failed / 8 deselected** (2026-08-13) — the 5 live-data test failures are fixed: tests now read the pinned snapshot `data/test_fixtures/live_2026-08-13/` (committed baseline), and `tests/test_no_live_data_reads.py` guards against future live-path reads.

## Next up (in order)

1. ~~Stabilize the live-data tests~~ ✅ DONE — suite green (165 passed)
2. **P2 finish:** ✅ dispatch 5 done — min-run floor verified (suite 175 green). **Notes:** P6/P8 coverage needs an A–D solve at relax ≤2 (small follow-up); TRIALS oddity open
3. **P3:** ✅ DONE (`c462769`, suite 196 green, browser-verified). ~~Open: Plant Calendar save wipes qty_kg~~ ✅ fixed (P4 slice 1)
4. **P4:** IN PROGRESS — plan: `.hermes/plans/p4-daily-loop-plan-2026-08-13.md` (4 slices: qty_kg honesty → Reconcile screen → week-lock + mo_changes export → loop framing/weekly roll)
   - **Slice 1 ✅ DONE:** qty_kg survives sandbox edits. Root cause: Python payload legs were already fixed in P3; the real bugs were the frontend mutations — split gave BOTH segments the full kg (double-count), resize kept stale kg. Fixed in `useScheduleState.ts` (split apportions by duration share, segment B takes the exact remainder; resize scales kg proportionally; unknown kg stays unknown; cross-line moves already correct — kg is the invariant, duration re-integrates). Bundle rebuilt. Browser-verified end-to-end on the live app: split 80,000 kg @ 56/111.7h → 40,095.9 + 39,904.1 (sums exact); resize 38.5h→18.5h → 56,800 → 27,300.4 kg (exact proportional). Suite 196 green.
   - **Finding (pre-existing, for slice 3):** saving the calendar persists the display-only cip_info overlay windows ("CIP (sched)") into calendar_blocks.csv — display overlay leaks into the data model on save. Not new; surfaced during slice-1 QA.
   - **Slice 1b ✅ DONE (user request, `e620bc3`):** editable block fields — click a block, type start (datetime) / duration / tonnage; qty↔duration linked via the block's implied rate; block resizes to fit typed kg; same guards as drag (locked/overlap/min-run reject, popover stays open to correct). Adaptive resize-handle widths (5–14px). Browser-verified: typed 40,000 kg auto-resized 111.7h→55.9h and saved exactly; 2h rejected with clear message. Suite 196 green.
   - **Slice 3 ✅ DONE (`c434021`):** 2-week lock window (lock line + shaded zone on the Gantt; drag/resize/edit refused inside; nothing movable INTO the frozen zone; exact h336 boundary verified), resize-handles-ignore-locks bug fixed, saves strip display-only cip_info overlays (was writing phantom CIPs), Lock & Export section on the calendar, Plant write-back section on Compare (mo_changes.csv review + CSV download, live: 40/40 MOs, −247,305 kg delta). Suite 226 green.
   - **Slice 4 ✅ DONE:** nav restructured to walk the daily loop (Home · 1 Connect · 2 Reconcile · 3 Plan · 4 Lock & Export · 5 Track · Setup) per user request "UI should follow our process"; weekly roll advances the lock with the anchor (one action).
   - **Slice 4b ✅ DONE (user-approved "all 3"):** page decluttering. Home: 7-stage SVG pipeline (predated Reconcile) replaced by a live 5-step "Today" walk of the loop (Connect feed freshness · Reconcile blocking/attention counts from the engine · Plan block count + anchor state · Lock state · Track scorecard week), data-status table collapsed. Plant Calendar: one "Start of day" strip (downtime editor + view options + reload), duplicate STEP-1 expander removed, Now-running collapsed, version-name field moved beside its Save button — the Gantt is the page. Data Files → "Connect — Data Files": downtime editor removed (lives on the Calendar), per-file in-browser cell-editing grid dropped (Excel + re-upload; every write still backed up), upload/preview/download kept. Suite 226 green.
   - **Slice 2 ✅ DONE:** Reconcile engine + screen. `helpers/reconcile_engine.py` (pure, 23 tests): STOCK (from stock_check_report; DO_NOT_SCHEDULE→blocking), COVERAGE (zero-scheduled due week 1→blocking; under-min→one summary; unknown-kg honesty note), CIP (overdue-now→blocking; due ≤48h unscheduled→warn; silent when a CIP is planned), CAPABILITY (scheduled-incapable→blocking; manprg-vs-table→warn, table is what needs fixing), FIT (overlaps/production-over-downtime→blocking; past-horizon→warn). `pages/reconcile.py` renders ranked findings with deep links; nav group added. **Browser-verified on live data — real catches:** 14 blocks scheduled on downed P11/P13, P12/P15/P22 past CIP interval, 280480-W0 (355.5 t) due week 1 with nothing scheduled, 241283@P22 missing from capability table. Suite 219 green. The engine doubles as the future planning agent's situational input (one engine, two consumers).
5. **P5/P6:** trial with Carsten
