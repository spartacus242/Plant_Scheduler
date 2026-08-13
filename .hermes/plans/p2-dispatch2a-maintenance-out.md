# P2 Dispatch 2a — Maintenance REMOVED from the scorecard (charter M5)

**Branch:** `develop` (one dedicated branch, renamed 2026-08-13). **Owner:** Director. **Executor:** Claude Code (sonnet).
**Status:** spec — Claude executes, Director verifies before "done".

## Context (verified 2026-08-13, do not re-derive)

- Charter decision (v3, §1, §8, M5): maintenance is REMOVED from the scorecard
  entirely. Maintenance stays blockable on the calendar (solver/downtime path),
  but the scorecard must not score it, weight it, or display it. Known defect it
  fixes: "maintenance silently scores 100 with zero blocks" (phantom 100).
- Files: `code/helpers/scorecard_engine.py`, `flowstate.toml`,
  `code/pages/scorecard.py` (check for maintenance rendering; grep found none
  but verify).
- All maintenance surface in scorecard_engine.py (verified locations):
  - :42 `CATEGORY_WEIGHT_KEYS["maintenance"] = "weight_maintenance"`
  - :46 `CATEGORY_ORDER = [..., "maintenance", ...]`
  - :69-92 category description/detail entries for maintenance (incl. the
    "flat 100 when maint_count == 0" note at :92)
  - :281-320 `maint_aligned` + `maint_conflicts` metric definitions
  - :440-441 label branch for `maint_aligned`
  - :868-899 `score_maintenance()` — DELETE
  - :1077, :1246 `"maintenance": float(cfg["weight_maintenance"])` in the
    scorecard result / weights dicts
  - :1135 `"maintenance": score_maintenance(calendar, cfg),` in `score_calendar` raw
  - `ScorecardResult` dataclass (:516-520 area) has a `maintenance` field
  - `composite_score` (:1071) renormalizes over the categories present — verify
    it handles a removed category gracefully (it should, since categories can be
    None already — see the note at :100-101 about renormalization)
- `flowstate.toml:72` `weight_maintenance = 0.10` — remove the key.
- `scorecard_config()` / `[scorecard]` parsing must tolerate the missing key
  (defaults dict — check `code/helpers/config.py` scorecard_config defaults for
  `weight_maintenance` and remove it there too if present).
- Saved scorecard JSONs (data/scorecards/*.json) may contain a maintenance
  entry — old files must still LOAD (backward compat), even if the field is no
  longer scored. Do NOT migrate or rewrite old files.
- Suite: `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` → 164 passed,
  8 deselected (flat-rates dispatch landed). Keep green.

## Scope (exactly this)

1. Remove the maintenance category from the scorecard COMPLETELY:
   - CATEGORY_ORDER, CATEGORY_WEIGHT_KEYS, category descriptions
   - `score_maintenance()` function + `maint_aligned`/`maint_conflicts` metric
     definitions and their label branches
   - the `raw` dict entry in `score_calendar`
   - `ScorecardResult.maintenance` field: remove it IF nothing else reads it
     (grep first); if removing breaks old-JSON loading (ScorecardResult.from_dict
     style path), keep the field with `default=None` and NEVER populate it —
     prefer the minimal change that keeps old JSONs loading.
   - the two `"maintenance": float(cfg["weight_maintenance"])` weight dict entries
2. `flowstate.toml`: delete `weight_maintenance = 0.10` (line ~72).
3. `code/helpers/config.py`: remove `weight_maintenance` from scorecard defaults
   if present; ensure `scorecard_config()` returns a dict without it.
4. Composite: verify the composite renormalizes across the remaining 5
   categories (it already renormalizes when a category is None — confirm and
   add a note in the function docstring if the behaviour is implicit).
5. Tests: find scorecard tests (tests/test_scorecard*.py or similar — list
   tests/ first) and update any asserting the 6-category composite / maintenance
   category / weight_maintenance config. Add ONE regression test: a calendar
   containing maintenance blocks scores WITHOUT a maintenance category and the
   composite ignores them (maintenance blocks don't drag any category down).
6. Run the FULL suite and report counts.

## Constraints
- Maintenance on the CALENDAR (blocking/downtime/solver) is untouched — only the
  scorecard stops scoring it.
- Old saved scorecard JSONs must still load.
- Do NOT touch other scorecard categories or formulas (scorecard defects other
  than maintenance are out of scope — later dispatch).
- Do NOT commit. Working tree only.

## Exit criteria (Director verifies)
1. Full suite green.
2. No `maintenance`/`maint` reference remains in scorecard_engine.py except the
   backward-compat field if required (grep).
3. `weight_maintenance` absent from flowstate.toml and config defaults.
4. Browser: Schedule Scorecard page renders 5 categories, no maintenance row,
   composite sane; scoring a week with maintenance blocks shows no maintenance
   category (Director drives the page).
