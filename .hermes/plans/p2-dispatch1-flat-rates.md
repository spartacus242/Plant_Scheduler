# P2 Dispatch 1 — Flat per-line rates ON (charter §6.1–6.3, M1/M3)

**Branch:** `feature/live-data-links`. **Owner:** Director. **Executor:** Claude Code (sonnet).
**Status:** spec — Claude executes, Director verifies before "done".

## Context (verified 2026-08-12/13, do not re-derive)

- User decision (charter §6.1): flat per-line rates replace per-SKU rates.
- The user's file is at `.hermes/desktop-attachments/line_rates.csv` — schema `line_id,Line,rate_kgph` (14 lines, P09 737 … P22 1398, sum 14,702 kg/h). **NO `Month` column.**
- `code/solver/data_loader.py:167-189` reads it when `use_sku_rates = false`, but REQUIRES a `Month` column (`lr["Month"] = pd.to_numeric(...)` at :178 → **KeyError crash** on the flat file). `Files.line_rates` = `<data_dir>/line_rates.csv` (:107).
- `code/helpers/scenario_runner.py:405-418` `_prepare_work_dir` copies ONLY 8 CSVs into the solver work dir — `line_rates.csv` is NOT in the mapping, so even with the loader fixed, real solves would never see it.
- `flowstate.toml [scheduler] use_sku_rates = true` (must flip to false).
- `code/helpers/data_health.py:361-381` `_rate_mode_semantics` currently WARNS (STALE) when `use_sku_rates = false` ("solver durations disagree with the calendar/scorecard") — its advice must flip to match the new intended mode.
- `code/helpers/scorecard_engine.py:569-600` `avg_rate_kgph` reads `calc_rate_kgph` (per-SKU) from `capabilities_rates.csv` (renaming `rate_kgph` if present) — used by `cap_cip_forfeited_kg`. Charter §6.3: scorecard kg lookups must use flat rates in flat mode.
- Suite: `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` → currently 160 passed, 8 deselected.
- ⚠️ Solver files are CRLF — preserve line endings, ASCII-only edits, do not reformat.
- ⚠️ `tests/test_changeover_cache.py` stages the WHOLE `data/reference/` dir into a temp workdir — the loader fix MUST land before `line_rates.csv` is placed in `data/reference/`, or that test goes red with KeyError.

## Scope (exactly this)

### A. Loader: make `Month` optional (data_loader.py)
1. In the line-rates branch (:167-189): if the CSV has a `Month` column → keep current behaviour (filter to `plan_month`). If NO `Month` column → all rows are flat rates for every month (build `line_rate_map` from every row).
2. Keep the existing `line_id`/`rate_kgph` coercion, the `(lid, sku)` override loop, and the `os.path.exists` guard unchanged.
3. Do NOT change `use_sku_rates = true` semantics — the per-SKU path stays as-is.

### B. Work-dir staging (scenario_runner.py)
4. Add `"line_rates.csv": "line_rates.csv"` to the mapping in `_prepare_work_dir` (:405-418) so solves see it.

### C. Config + file placement
5. `flowstate.toml`: `use_sku_rates = false` (keep everything else).
6. Copy `.hermes/desktop-attachments/line_rates.csv` → `data/reference/line_rates.csv` (git-tracked, part of this change).

### D. Health check flip (data_health.py)
7. `_rate_mode_semantics`: new semantics — `use_sku_rates = false` AND `data/reference/line_rates.csv` present → **OK** ("flat line rates active: solver and scorecard both use line_rates.csv"); `use_sku_rates = false` AND file missing → **STALE** (advice: place line_rates.csv in data/reference/); `use_sku_rates = true` → OK as today.

### E. Scorecard rates (scorecard_engine.py)
8. `avg_rate_kgph` (or its call site): when the flat-rate mode is active (use_sku_rates=false AND line_rates.csv exists), return the line's flat `rate_kgph` instead of the per-SKU mean. Keep the per-SKU mean as fallback. Do NOT touch scoring formulas, caps, or weights — only the rate source.

### F. Tests
9. `tests/test_changeover_cache.py`-style regression: a flat line_rates.csv WITHOUT Month loads via `Data.load()` (stub a small temp file, assert per-line rates override and no KeyError) — put it in the right existing solver test file (e.g. `tests/test_solver_contracts.py` or a new `tests/test_line_rates.py`, matching repo style with `sys.path.insert` for `code` + `code/solver`).
10. Update `tests/test_data_health.py` `test_rate_mode_false_warns` / `test_rate_mode_true_ok` to the new semantics (false + file present = OK; false + file missing = STALE).
11. If a scorecard test pins `avg_rate_kgph` output, update it; otherwise add a minimal one (flat file → avg_rate_kgph == flat rate).
12. Run the FULL suite — must stay green (expect ~160+ passed).

## Constraints
- Do NOT touch the solver model, objectives, relax ladder, or any other data_loader branch.
- Do NOT reformat CRLF solver files or convert line endings.
- Do NOT commit. Working tree only.
- Do NOT edit `flowstate.toml` by hand for testing — only the one flip in C5.
- Order matters: A → B → D → E code first, suite run, THEN C6 (place the file), suite run again. If the suite breaks only after C6, the loader fix is incomplete.

## Exit criteria (Director verifies all)
1. Full suite green (with C6 in place).
2. `Data.load()` accepts the flat file (no KeyError) — verified by the new test + a direct probe run.
3. Work-dir staging copies line_rates.csv (verified by reading the mapping or a staged dir).
4. `data_health` rate_mode row shows OK with flat mode + file present.
5. Scorecard `avg_rate_kgph` returns flat rates in flat mode.
6. Real solver smoke run (two-phase, default 60s budget) still produces a FEASIBLE schedule — Director runs it; report the staged work-dir path so Director can check solver_error.txt for `SOLVER level=0 status=`.
