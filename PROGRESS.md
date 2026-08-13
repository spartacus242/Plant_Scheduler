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
| **P1** | Live data links (manprg/CIP/stock/AZAP) | 🟡 **ALMOST DONE** | `5117c8d` demand path configurable + staleness warnings; `845dcf6`/`49b904f` GitHub file-drop bridge (13 files, both PCs); demand live link = `demand_plan_summary.csv` (user decision 2026-08-13). **Missing:** stock check live link only |
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
3. **P3:** ✅ DONE (`c462769`, suite 196 green, browser-verified). Open: Plant Calendar save wipes qty_kg (TS frontend — fold into P4)
4. **P4:** rebuild the daily screens around the sandbox (biggest chunk — bidirectional DnD, Reconcile with stock flags, qty_kg-preserving save)
5. **P5/P6:** trial with Carsten
