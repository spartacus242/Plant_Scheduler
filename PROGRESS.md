# Flowstate — Build Progress

**Updated:** 2026-08-13 · **Branch:** `develop` (the one dedicated branch — all work happens here; `main` stays frozen until go/no-go)
**Source of truth:** [Charter v3 — DECISIONS LOCKED](.hermes/plans/2026-08-12-charter.md) · **Suite:** 159 passed / **5 failed** (see Gate below)

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
| **P1** | Live data links (manprg/CIP/stock/AZAP) | 🟡 **PARTIAL** | `5117c8d` demand path configurable + staleness warnings; `845dcf6`/`49b904f` GitHub file-drop bridge (13 files, both PCs). **Missing:** stock check live link; final AZAP folder location (TBD with Carsten) |
| **P2** | Runnable plan core: flat rates, maintenance out, constraint audit | 🟡 **PARTIAL** | `6ecb0bc` flat per-line rates ON (incl. loader `Month`-optional fix + `test_line_rates.py`); `f3bf013` maintenance removed from scorecard. **Missing:** constraint probe-verification pass (charter §5); changeover data fix (VIF/plant — 44% zero rows, dead `ffs_change`, 2 missing SKUs) |
| **P3** | Honest scorecard | ⬜ PENDING | Maintenance defect already fixed in P2. **Remaining:** CIP cap artifact (2/100), Service ignores kg |
| **P4** | Daily screens: Reconcile → bidirectional sandbox → lock/export → track | ⬜ PENDING | Sandbox survives; screens not rebuilt yet |
| **P5** | 3-week daily trial with Carsten | ⬜ PENDING | |
| **P6** | Go / no-go | ⬜ PENDING | |

## Gate — the suite right now

**5 failed / 159 passed** — all 5 are the same class: tests pin values from the *live* files in `data/reference/` (manprg, cip_info, capabilities), and the bridge refreshes those files daily, so the dated expectations go stale:

- `test_live_imports.py::test_manprg_merge_counts` · `test_manprg_current_mo_per_line` · `test_manprg_by_mo` (MO 29901: 63 cases left vs the pinned 2,840 — production progressed)
- `test_live_imports.py::test_cip_info` (CIP timestamps moved)
- `test_capability_check.py::test_real_manprg_reports_four_known_conflicts` (conflicts changed with the new export)

**Fix queued:** snapshot the reference files into a committed fixture dir and point the tests at it (dispatch doc ready: `.hermes/plans/p2-dispatch3-stabilize-live-data-tests.md`). Not a product defect — a test-architecture gap that appeared the moment live data started flowing.

## Next up (in order)

1. **Stabilize the live-data tests** (dispatch ready — small, sonnet-class work)
2. **P2 remainder:** constraint probe-verification pass (charter §5 rows 1–9) · changeover data fix (**needs VIF export / plant knowledge from you** — 2 missing SKUs: 280698, 570560)
3. **P3:** fix the two remaining wrong scores (CIP cap, Service kg)
4. **P4:** rebuild the daily screens around the sandbox (biggest chunk — bidirectional DnD, Reconcile with stock flags)
5. **P5/P6:** trial with Carsten
