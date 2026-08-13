# Flowstate — Build Progress

**Updated:** 2026-08-13 · **Branch:** `develop` (the one dedicated branch — all work happens here; `main` stays frozen until go/no-go)
**Source of truth:** [Charter v3 — DECISIONS LOCKED](.hermes/plans/2026-08-12-charter.md) · **Suite:** ✅ **173 passed / 0 failed / 4 skipped / 1 xfailed** (xfail = tracked min-run defect, fix in flight — dispatch 5)

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
| **P2** | Runnable plan core: flat rates, maintenance out, constraint audit | 🟡 **ALMOST DONE** | `6ecb0bc` flat rates ON; `f3bf013` maintenance out; `d646fd7` constraint probes P1–P9 (caught min-run skip for current MOs — fix dispatching now); changeover data fixed by user (ffs_change alive, all SKUs present). **Remaining:** dispatch 5 (4h floor) verification; TRIALS oddity review |
| **P3** | Honest scorecard | ⬜ PENDING | Maintenance defect already fixed in P2. **Remaining:** CIP cap artifact (2/100), Service ignores kg |
| **P4** | Daily screens: Reconcile → bidirectional sandbox → lock/export → track | ⬜ PENDING | Sandbox survives; screens not rebuilt yet |
| **P5** | 3-week daily trial with Carsten | ⬜ PENDING | |
| **P6** | Go / no-go | ⬜ PENDING | |

## Gate — the suite right now

✅ **165 passed / 0 failed / 8 deselected** (2026-08-13) — the 5 live-data test failures are fixed: tests now read the pinned snapshot `data/test_fixtures/live_2026-08-13/` (committed baseline), and `tests/test_no_live_data_reads.py` guards against future live-path reads.

## Next up (in order)

1. ~~Stabilize the live-data tests~~ ✅ DONE — suite green (165 passed)
2. **P2 finish:** dispatch 5 in flight (4h floor on current MOs — user-approved) → verify probes P1c/P6/P8 on a fresh solve; then P2 done → **P3** (CIP cap + Service kg scorecard fixes)
3. **P3:** fix the two remaining wrong scores (CIP cap, Service kg)
4. **P4:** rebuild the daily screens around the sandbox (biggest chunk — bidirectional DnD, Reconcile with stock flags)
5. **P5/P6:** trial with Carsten
