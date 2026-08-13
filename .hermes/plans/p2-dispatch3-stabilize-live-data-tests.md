# P2 Dispatch 3 — Stabilize live-data tests (suite gate)

**Branch:** `develop` (one dedicated branch, renamed 2026-08-13) · **Owner:** Director · **Executor:** Claude Code (sonnet — mechanical, well-specified; no Opus needed)
**Status:** spec — Claude executes, Director verifies before "done".

## Why

The GitHub file-drop bridge refreshes `data/reference/` daily (manprg 15-min cadence, cip_info, demand, capabilities). Five tests pin dated values from those LIVE files and now fail on the 2026-08-13 refresh:

1. `tests/test_live_imports.py::test_manprg_merge_counts` — asserts `rows == 58` from the Aug-10 export
2. `tests/test_live_imports.py::test_manprg_current_mo_per_line` — current-MO spot checks from Aug-10
3. `tests/test_live_imports.py::test_manprg_by_mo` — MO 29901 `left_cas == 2840` (now 63)
4. `tests/test_live_imports.py::test_cip_info` — P09/P14 scheduled-CIP timestamps moved
5. `tests/test_capability_check.py::test_real_manprg_reports_four_known_conflicts` — conflict set changed

Every refresh will break them again until they read a **pinned snapshot**, not the live dir.

## Context (verified 2026-08-13, do not re-derive)

- Test constants: `tests/test_live_imports.py:16-18` — `M1/M2/CIP = ROOT/"data"/"reference"/…` (manprg.txt, manprg2.txt, cip_info.csv).
- `tests/test_capability_check.py` reads the real manprg + `data/reference/capabilities_rates.csv` (check its imports before changing — snapshot every file it reads).
- Precedent for staging: `tests/test_changeover_cache.py::_stage_workdir` copies `data/reference/` into a `tmp_path` — the cache test may KEEP reading the live dir (it doesn't pin values); only the 5 listed tests must move.
- Bridge file list: `scripts/fs-live-data.conf.json` (13 files) — refresh cadence is daily or faster; do not fight it, just stop tests from reading live values.
- Suite invocation: `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` from repo root. Currently 5 failed / 159 passed / 8 deselected.
- ⚠️ Solver/helper `.py` files are CRLF — preserve line endings, ASCII-only edits, no reformatting.

## Scope (exactly this)

### A. Committed snapshot fixture
1. Create `data/test_fixtures/live_2026-08-13/` (commit it) containing byte-identical copies of the files the 5 tests read:
   - `manprg.txt`, `manprg2.txt`, `cip_info.csv` (from `data/reference/` today)
   - `capabilities_rates.csv` if `test_capability_check.py` reads it
   - any other file those tests import (check their imports)
2. Do NOT copy `demand_plan*`, `changeovers.csv`, or anything else — minimal snapshot.

### B. Point the tests at the snapshot
3. `tests/test_live_imports.py`: repoint `M1`, `M2`, `CIP` to the fixture dir. Add a one-line comment: `# Pinned 2026-08-13 snapshot — live data/reference is refreshed daily by the bridge`.
4. `tests/test_capability_check.py`: repoint its real-data paths to the snapshot.

### C. Re-pin expected values to the snapshot's actual values
5. Run the tests against the snapshot; update every pinned number/timestamp to what the 2026-08-13 snapshot actually contains (e.g. `left_cas` 2840 → 63; `rows` 58 → actual; CIP timestamps → snapshot's; conflict count/SKUs → snapshot's). Do NOT invent values — read them from the snapshot.
6. Keep the structural assertions (parsing works, shape, warnings == []) intact.

### D. Guard
7. Add `tests/test_no_live_data_reads.py` (or a marker) that greps `tests/` for hard-coded `data/reference` reads in test files and fails if a test file imports live paths — EXCEPT `test_changeover_cache.py` (staging copy, value-agnostic). Keep it simple: a `Path`-based scan of `tests/*.py` for `"data" / "reference"` / `data/reference` strings.

## Verification (Director runs these before "done")

1. `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` → **0 failed** (all 164 pass, 8 deselected).
2. Run it a SECOND time after touching `data/reference/manprg.txt` (append a harmless line, restore after) → still green. Proves decoupling from live data.
3. `grep -rn "data/reference" tests/ | grep -v test_changeover_cache` → only fixture-dir references remain.
4. No changes outside `tests/` and `data/test_fixtures/` unless a test's imports force it (say so in the commit message).

## Out of scope

- Changing the bridge, the importers, or the app.
- Touching the other 159 tests.
- The changeover-table data fix (separate dispatch, needs VIF data from the user).
