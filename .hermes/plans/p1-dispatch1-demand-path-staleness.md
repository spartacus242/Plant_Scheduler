# P1 Dispatch 1 — Demand baseline path config + live staleness on the calendar

**Branch:** `develop` (one dedicated branch, renamed 2026-08-13) (off `feature/handoff`). **Owner:** Director. **Executor:** Claude Code.
**Status:** spec — Claude Code executes, Director verifies before "done".

## Context (verified, do not re-derive)

- Flowstate's demand baseline is `demand_plan_summary.csv` (the WEEKLY AZAP baseline — user decision 2026-08-12: **keep the modified CSV, NO new AZAP importer**). It is transformed into the canonical `data/reference/demand_plan.csv` by `code/helpers/demand_summary_import.py` via the upload flow in `code/pages/data.py` (line ~160-230).
- The user's decision D1 said the demand file path must be **configurable** (settings/toml) so Carsten can drop his file in a folder — today the ONLY entry path is the Data page uploader; there is no `[datasources]` key for it.
- Live staleness already exists for manprg (cadence 0.5h) and cip_info (26h) in `code/helpers/data_health.py` (DEFAULT_CADENCE_H + `cadence` dict, lines ~42-190), surfaced ONLY on the home page (`code/pages/home.py:31-40`). The Plant Calendar page (`code/pages/calendar.py`) shows live data but NO stale warnings.
- Tests run with: `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` (currently 150 passed, 8 deselected). No linter configured; `python3 -m py_compile` for syntax.

## Scope (exactly this, nothing else)

### A. Configurable demand baseline path
1. `code/helpers/config.py` — `datasources_config()`: add `demand_summary_csv: ""` default (sibling of `manprg_files`, `cip_info_csv`), read from `[datasources]` in `flowstate.toml`.
2. `code/pages/settings.py` — add a text input "Demand plan summary CSV (weekly AZAP baseline)" next to the other file inputs; persist it in the `[datasources]` save block (same pattern as `cip` / `manprg`).
3. `code/pages/data.py` — the demand import section: if `datasources_config()["demand_summary_csv"]` is set AND the file exists, offer it as the source (pre-filled/used path); the uploader remains the fallback. The downstream transform to `data/reference/demand_plan.csv` is unchanged — do NOT touch `demand_summary_import.py`'s parsing logic.
4. `code/helpers/data_health.py` — add a health row for the demand baseline: source = configured path, else `data/reference/demand_plan_summary.csv`; cadence **168 h** (weekly export) in DEFAULT_CADENCE_H, overridable via `[health] cadence_h` like the others.

### B. Stale warnings on the Plant Calendar page
5. `code/pages/calendar.py` — at the top of the page, compute health for the LIVE feeds (manprg, cip_info, demand baseline) using the existing `data_health` engine and render a compact banner: `st.error` if missing/unreadable, `st.warning` if stale (same semantics as `home.py:36-40`). Keep it a banner, not the full health grid. Do not add new caching layers; reuse what exists.

### C. Regression tests
6. Add/extend tests in `tests/` (match existing style): (a) `datasources_config` default contains `demand_summary_csv` and honors a toml value; (b) health engine returns a demand-baseline row with the 168h cadence (and the configured-path override if cheap). Run the FULL suite — it must stay 150+ green, 0 new failures.

## Constraints
- Do NOT build an AZAP importer, do NOT change `demand_summary_import.py` parsing, do NOT touch the solver, scorecard, or Gantt frontend.
- Do NOT commit. Leave changes staged-free (working tree only) for Director review.
- Do NOT edit `flowstate.toml` — settings writes are runtime-only.
- Keep the existing code style (module docstrings, `from __future__ import annotations`, no new deps).

## Exit criteria (Director verifies all)
1. `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` → all pass.
2. Settings page renders the new field and saves it to `flowstate.toml` `[datasources]` (browser-verified).
3. With the path set to an existing file, the Data page demand import uses it; uploader still works (browser-verified).
4. Calendar page shows the stale banner when the live files are old; home page unchanged (browser-verified).
5. `py_compile` clean on every touched file.
