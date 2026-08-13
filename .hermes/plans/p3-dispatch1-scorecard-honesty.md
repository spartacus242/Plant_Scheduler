# P3 Dispatch 1 — Scorecard honesty: Service kg + Trials availability (charter M4)

**Branch:** `develop` · **Owner:** Director · **Executor:** Claude Code (opus) · **Status:** spec
**Verified facts (Director recon 2026-08-13, do not re-derive):**

- `code/helpers/calendar_io.py:108` — `import_solver_schedule` (renamed from
  `import_legacy_schedule` 2026-08-13; it is the LIVE solver-schedule import path) writes `"qty_kg": None` for every
  production row. Solver schedules (`schedule_phase2.csv`) have NO qty column today, so any
  schedule imported into the app calendar loses production kg.
- `code/helpers/scorecard_engine.py:836-840` — `score_service`: `produced` is only populated when
  the calendar has a `qty_kg` column with non-null values; otherwise `produced={}` and
  `excess_inventory_kg=None` (line 869) → the Service score silently averages only late/at-risk
  (lines 938-945). That is the "Service ignores kg produced" defect (charter M4).
- `code/helpers/scorecard_engine.py:778` `score_trials(calendar, co_map)` reads ONLY the calendar's
  trial blocks. When `data/reference/trials.csv` is absent/empty and the calendar has no trial
  blocks, trial_hours=0 → category 100.0 silently (observed in `data/scorecards/latest.json`:
  trials 100.0 with zero trial data). Silent-100 on absent data — M4 violation.
- CIP category (lines 907-920) is ALREADY the documented overdue-compliance gate (cap 1; any
  overdue → 0; raw metrics reported only). DO NOT change its scoring. Add regression tests only.
- UI (`code/pages/scorecard.py`) already renders no-data categories gray; it needs a small
  "(estimated)" note for estimated excess kg (Service section).
- `score_calendar(calendar, *, week_label, data_dir, cfg)` (line 1002) has `data_dir`; the engine
  can reach `data_dir/reference/trials.csv` for availability.
- `flowstate.toml` caps: `cap_excess_kg = 50000.0`; config defaults in `code/helpers/config.py`.

## Scope (7 changes + tests)

0. **Loader must NOT fake kg (NEW FINDING, Director browser repro 15:43)** —
   `code/helpers/calendar_io.py:59`: `load_calendar` coerces `qty_kg` via
   `pd.to_numeric(...).fillna(0).astype(float)` — missing kg becomes 0.0 before the
   engine sees it, so `score_service`'s null path never fires and unknown kg is scored
   as "no excess". Change line 59 to preserve NaN:
   `df["qty_kg"] = pd.to_numeric(df.get("qty_kg", np.nan), errors="coerce").astype(float)`
   (import numpy if needed). Then AUDIT every other `qty_kg` consumer in the app
   (plant calendar page, compare, sandbox, calendar preview, stock check — grep
   `qty_kg` across `code/`) and make display sites NaN-safe (render blank/0 at
   display, never in the data model). The scorecard engine keeps its :837 null path.

1. **Solver writes real kg** — `code/solver/phase2_scheduler.py` schedule writer (~lines 900-923,
   single-phase ~1267 / ~1328): add a `qty_kg` column to `schedule_phase2.csv` rows, one value per
   row = the produced kg for that order in the solution (prefer the solver's produced value;
   fallback run_hours × line rate only if the produced map is unreachable at write time). Output
   format change only — NO constraint/objective changes. `use_current_mo`/ladder logic untouched.
2. **Importer carries kg** — `code/helpers/calendar_io.py:97-111`: map `qty_kg` from the schedule
   row when present (`r.get("qty_kg")`, keep `None` when absent/NaN, exactly as today).
3. **Service estimates kg when absent** — `code/helpers/scorecard_engine.py` `score_service`:
   when the calendar lacks usable `qty_kg` (column absent, all-NaN, or all-zero), estimate
   per-order produced kg from `run_hours × line avg rate` (reuse the existing avg-rate logic used
   for forfeited kg; the rates table lives under `data_dir/reference/capabilities_rates.csv`),
   and return `"excess_inventory_kg_estimated": True` alongside the value. When real kg exists →
   flag False. Keep `excess_inventory_kg=None` only when there is no production data at all. The
   Service score then always includes the kg part (or None when truly no data → category becomes
   N/A). Pass `data_dir` into `score_service` (thread it from `score_calendar`; default None → no
   estimation possible → keep current behavior).
4. **Trials availability** — `score_trials`: return `"available": True/False` — False when the
   calendar has no trial blocks AND `data_dir/reference/trials.csv` is absent or empty. In
   `category_scores`, when `available` is False → trials category = None (JSON null, composite
   excludes it, page shows gray/no data). When trials.csv exists OR calendar has trial blocks →
   score exactly as today.

5. **Tests** — new `tests/test_scorecard.py` (small synthetic calendars, call
   `score_calendar`/`score_service`/`score_trials`/`category_scores` directly):
   - service uses real `qty_kg` when present (excess > 0 case + flag False)
   - service estimates when `qty_kg` absent (flag True, excess ≈ Σ run_h × rate within 1%)
   - trials: absent input + no blocks → category None; present input → score computed
   - cip: 0 overdue → 100; 1 overdue → 0 (regression for the compliance gate)
   - `import_legacy_schedule` passes `qty_kg` through when the schedule has it
   - solver schedule writer: unit-level if the rows builder is separable; otherwise skip (Director
     verifies the written CSV on a real Etrim solve — do not run a solve in tests).

6. **UI** — `code/pages/scorecard.py` Service section: show "(estimated)" next to Excess
   inventory (kg) when `excess_inventory_kg_estimated` is true in the scored result.

## Hard rules

- Branch `develop`. Do NOT commit, do NOT push. Do NOT touch `model_builder.py`,
  `data_loader.py`, or the ladder logic. No constraint/objective behavior changes.
- Preserve CRLF in files you edit (phase2_scheduler.py, calendar_io.py, scorecard_engine.py,
  scorecard.py are CRLF in the worktree) — ASCII-only edits.
- `env -u PYTHONPATH .venv/Scripts/python.exe` for pytest. Full suite must stay green
  (175 passed baseline; new tests add to it).
- If a change turns out to need a solver-behavior change, STOP and report — do not improvise.

## Exit criteria (report must state each)

1. Diff summary per file (paste the changed hunks).
2. `qty_kg` present in a written schedule_phase2.csv (say how you verified — a real solve is
   allowed, Etrim work dir exists at data/_scenario_work/Etrim; a 300 s single-phase run is fine).
3. New tests all pass; full suite green (counts).
4. trials category is None (not 100) in a scored JSON with no trials input; Service shows
   estimated excess kg on a calendar without qty_kg (tiny synthetic case in tests).
5. `load_calendar` keeps qty_kg NaN (no fillna(0)); list every other qty_kg consumer you
   audited and how each stays NaN-safe.
6. Remaining open items, if any.
