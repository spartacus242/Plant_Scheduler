# Flowstate Command Center — Data Health + Process Flow + Scorecard Visualization (Design Doc)

**Status:** APPROVED (user 2026-08-10: "Do option B… rename legacy, move into main folder, ensure no legacy solver code references legacy data files"). Build in progress on `feature/command-center`.
**Date:** 2026-08-10
**Author:** Hermes Director (analysis of `feature/handoff-ww32`, commit `a6cff31`)
**Branch for build:** `feature/command-center` (off main)

> **Scope add-on (user-approved):** Workstream B — de-legacy the solver. Move the
> live CP-SAT solver out of `Flowstate-legacy/` into the main `code/` tree,
> flip `use_sku_rates = true`, and ensure no solver code reads legacy data
> files. The health engine then surfaces a *consistent* rate model instead of
> warning about one.

---

## 1. Goal

Make Flowstate feel like a **planner's cockpit** instead of a developer's toolbox:

1. A **graphical process-flow diagram** on Home showing the weekly pipeline
   (VIF/ERP data → Demand → Calendar → Score → Stock check → Optimize → Promote),
   with each stage colored by live health.
2. A **data-health engine** that answers, for every input and live feed: *present?
   readable? fresh? semantically complete?* — and produces a ranked
   **"what you need to do next"** list.
3. A **scorecard visualization** that reads correctly at a glance: category bars,
   contribution to composite, cap-saturation badges, and a hard distinction
   between **"no data"** and **"good"**.

Design principles: no new runtime dependencies beyond the current stack
(Streamlit, pandas, plotly already in `requirements.txt`), every health rule
testable without a browser, and **stale ≠ missing** — the two must never render
as the same color.

---

## 2. Current context (verified findings this session)

- 60/60 tests green; app verified in-browser on all pages (Home, Scorecard,
  Stock Check, Plant Calendar, Generate Scenarios).
- The scorecard for the current calendar reads **Changeovers 0, CIP 100,
  Maintenance 100, Trials 100, Campaigns 100** — live confirmation of the
  known defects (maintenance flat-100 with zero blocks; CIP 100 with zero CIPs;
  changeover saturation pins the score at 0; `excess_inventory_kg` still null).
- `data_catalog.status()` checks existence/rows/columns only — **no freshness,
  no semantic completeness** (e.g. manprg 3h old reads "OK").
- Home's workflow is a static 6-step list; Data status is a flat table with a
  single mtime column; no "what next" logic anywhere.
- Hidden inconsistency: solver runs on legacy `line_rates.csv` flat monthly
  rates while the UI/naive/scorecard use per-SKU `calc_rate_kgph`
  (`use_sku_rates = false` in `flowstate.toml`). Health engine must surface this.
- `AGENTS.md` claims "no automated test suite" — false; tests exist (60). Fix.

---

## 3. Proposed architecture

### 3.1 New module: `code/helpers/data_health.py` (pure, testable, no Streamlit)

Single entry point:

```python
@dataclass(frozen=True)
class HealthStatus:
    key: str                    # "demand_plan", "manprg", ...
    name: str                   # human label
    state: str                  # OK | STALE | MISSING | ERROR | NOT_APPLICABLE
    detail: str                 # one-line human explanation
    severity: int               # 0 ok, 1 warn, 2 blocking
    actions: list[str]          # e.g. "Re-import AZAP (week 33 -> 33)"
    cadence_h: float | None     # expected refresh interval
    age_h: float | None         # measured age
    source: str                 # "catalog" | "live_feed" | "semantic"

def assess(data_dir: Path, cfg: dict) -> list[HealthStatus]:
    ...
def next_actions(health: list[HealthStatus], limit: int = 3) -> list[str]:
    ...
```

**Rule classes:**

| Class | Checks | State mapping |
|---|---|---|
| Catalog files (10) | exists, readable, required columns, row count > 0 | MISSING / ERROR / OK |
| Live feeds (manprg, manprg2, cip_info) | exists, age vs cadence (manprg 15–30 min, cip_info 24 h) | STALE if age > cadence |
| Demand freshness | `demand_plan.csv` max week_index vs `horizon` ISO week; anchor match | STALE if mismatch |
| Calendar anchor | `planning_start_date` vs `horizon.anchor` (via `Horizon.stale`) | STALE ("Roll calendar to today") |
| Semantic completeness | zero CIP blocks in calendar vs lines; changeover setup_hours == 0 rows; trials present; version slots free | STALE/OK with action |
| Rate consistency | `use_sku_rates == false` + legacy `line_rates.csv` exists | WARN with explanation |
| Stock check | VIF snapshot mtime vs daily cadence; dev_vif vs network share | STALE if old |

**Cadence table** (defaults in code, overridable in `flowstate.toml [health]`):

| Source | Cadence | Stale threshold |
|---|---|---|
| manprg / manprg2 | 15 min | 30 min |
| cip_info | daily | 26 h |
| VIF exports (stock check) | daily | 26 h |
| demand_plan (AZAP) | weekly | next Monday after due week |
| calendar_blocks | manual | anchor != today |
| scorecards (history) | weekly | no scorecard this ISO week |

**Naming:** state vocabulary is fixed: `OK`, `STALE`, `MISSING`, `ERROR`,
`NOT_APPLICABLE`. Colors: green / amber / red / gray.

### 3.2 New module: `code/helpers/process_flow.py`

Builds the pipeline model — pure data, no rendering:

```python
STAGES = [
    ("vif",        "VIF / ERP data",      "Daily component + BOM exports"),
    ("demand",     "Demand plan (AZAP)",  "Corporate weekly demand"),
    ("calendar",   "Plant Calendar",      "The schedule of record (twin)"),
    ("score",      "Schedule Scorecard",  "Weekly score snapshot"),
    ("stock",      "Stock Check",         "Component coverage vs board"),
    ("optimize",   "Generate Scenarios",  "Solver alternatives"),
    ("promote",    "Promote & roll",      "Version Compare -> official"),
]
def stage_state(stage_id: str, health: list[HealthStatus]) -> str: ...
```

Each stage maps to one or more health keys; `stage_state` derives the worst
state among them (blocking wins).

### 3.3 Home page rewrite (`code/pages/home.py`)

1. **Pipeline diagram** rendered as an inline HTML/SVG string (no new deps —
   `st.markdown(unsafe_allow_html=True)`, dark-theme-aware, arrows between
   nodes, node color from `stage_state`, tooltip with the detail line).
   Each node is clickable (`st.page_link` under it or `on_click` navigation).
2. **Freshness banner** — `st.warning`/`st.error` line summarizing the worst
   states, shown only when something is not OK.
3. **"What you need to do next"** card — top 3 actions from `next_actions()`,
   each with a deep-link button to the page that resolves it.
4. **Data status table** — replace the flat table with the health-engine output:
   colored status chips, age column (e.g. "3 h", "4 days"), and an inline
   action hint. Keep the existing columns where useful.

### 3.4 Scorecard visualization (`code/helpers/scorecard_ui.py` + scorecard page)

1. **Category contribution bars** — one horizontal bar per category: width =
   `score`, colored by state; annotated `score × weight → contribution`.
   Use Streamlit-native `st.progress` + columns (or a small plotly bar) — no
   new dependency.
2. **Composite gauge** — simple 0–100 progress + delta vs baseline where known.
3. **Saturation badges** — when a metric is at/over its cap, show
   `⚠ recipe_changes 78 ≥ cap 40 → scores 0` inline instead of burying it in
   the reference expander.
4. **No-data ≠ good** — Maintenance with `maint_count == 0` renders as
   **"no maintenance data"** (gray / NOT_APPLICABLE styling), not a 100 bar.
   Same for Trials with zero blocks and Service without demand_plan.
5. Keep the raw-metric text blocks and the "How these metrics are calculated"
   reference — they are valuable; they just move below the visual layer.

### 3.5 Global freshness strip (`code/app.py`)

A slim `st.container` after the header on every page: `⚠ 2 stale, 1 missing —
[View]` linking to Home. Implemented via a small `render_health_strip(health)`
helper in `data_health.py`-adjacent UI module; cached per session (TTL ~5 min).

---

## 4. Files to change

| File | Change |
|---|---|
| `code/helpers/data_health.py` | **NEW** — health engine + next_actions |
| `code/helpers/process_flow.py` | **NEW** — pipeline model + stage_state |
| `code/pages/home.py` | Rewrite: pipeline diagram, banner, next-actions, health table |
| `code/helpers/scorecard_ui.py` | Add category bars, gauge, saturation badges, no-data styling |
| `code/pages/scorecard.py` | Wire new scorecard rendering |
| `code/helpers/scorecard_engine.py` | Expose `maint_count`/`trial_count`/`cip_count` already present; add helper for saturation notes reuse (minor) |
| `code/app.py` | Optional global freshness strip |
| `flowstate.toml` | Optional `[health]` cadence overrides (defaults suffice initially) |
| `AGENTS.md` | Fix "no test suite" claim; document tests + new modules |
| `data/reference/capabilities_rates.csv` | Drop `nominal_rate_kgph` column (separate commit) |
| `.gitignore` | Add `data/_backups/`, `data/versions/` policy note (separate cleanup commit) |

**Tests (new file `tests/test_data_health.py`, ~20 cases):**

- catalog: missing file → MISSING; corrupt CSV → ERROR; empty rows → ERROR.
- freshness: manprg 3h old → STALE with "refresh live feed" action; fresh → OK.
- demand week mismatch → STALE + "re-import AZAP" action.
- calendar anchor stale → STALE + "Roll calendar to today" action.
- semantic: zero CIP blocks → warn action; changeover setup_hours all zero → warn.
- rate mode: `use_sku_rates=false` → warning with explanation.
- `next_actions` ordering: blocking first, limit respected.
- `stage_state`: worst-state propagation.

Existing suite must stay green: run `PYTHONPATH= .venv/Scripts/python.exe -m pytest -q`.

---

## 5. Build sequence (feature branch `feature/command-center`)

1. **Task 1** — `data_health.py` skeleton: `HealthStatus` + `assess()` catalog
   rules + tests (TDD).
2. **Task 2** — live-feed freshness + cadence table + tests.
3. **Task 3** — semantic rules (demand week, calendar anchor, zero-CIP,
   changeover zeros, rate mode) + tests.
4. **Task 4** — `next_actions()` + `process_flow.py` + tests.
5. **Task 5** — Home rewrite (diagram, banner, next-actions card, health table);
   browser-verify per `flowstate-app-qa` skill (render, click each node link).
6. **Task 6** — scorecard visualization in `scorecard_ui.py`; browser-verify
   scorecard page (bars, gauge, saturation badges, no-data gray).
7. **Task 7** — optional global freshness strip in `app.py`.
8. **Task 8** — cleanup commit: AGENTS.md, `_backups` gitignore, drop
   `nominal_rate_kgph` column, move sample PDFs/xlsx decision (see open Q).
9. **Task 9** — full suite + full browser QA pass; report to user for approval;
   merge to main only after sign-off.

---

## 6. Risks / tradeoffs / open questions

1. **Solver rate inconsistency (§2)** — the health engine will *surface* it,
   but fixing it (flipping `use_sku_rates = true`) is a solver-behavior change
   and needs its own verification. **Open question:** fix in this branch or
   track separately?
2. **SVG pipeline vs plotly** — SVG inline is dependency-free and theme-
   controllable but hand-rolled; plotly is heavier and theming is harder. I
   propose SVG. OK?
3. **"No data ≠ good" rendering** — changes Maintenance/Trials/CIP category
   display from a number (100) to a gray state. The composite still uses the
   engine's numbers; this is a display-layer change only. Confirm that's
   desired (it will make the composite look "worse" for the seed/naive
   schedules, which is honest).
4. **Version cleanup** — delete `azap_baseline` + `scenario_d_balanced`
   (Aug-3 seed versions)? They are the only history; deleting frees 2 of 5
   slots. Recommend keep until first real week, then archive.
5. **`data/versions/naive_demand_plan/`** is untracked on disk — commit it as
   the strawman version or leave untracked?
6. **Freshness thresholds** — are the defaults (§3.1 table) right for Carsten's
   cadence (manprg 15 min, VIF daily, AZAP weekly)? Confirm before building.
7. **Scope of "next actions"** — should actions be limited to data-health
   issues, or also include workflow nudges (e.g. "score this week", "you have
   X/5 versions")? I propose both, data-health first.

---

## 7. Acceptance criteria

- [ ] `tests/test_data_health.py` green; full suite stays 60+ green.
- [ ] Home renders a clickable process-flow diagram with per-stage color.
- [ ] A stale manprg (mtime > 30 min) shows amber on the diagram AND a
      "refresh live feed" action — verified by browser with a real stale file.
- [ ] A missing catalog file shows red, not green.
- [ ] Scorecard page shows category bars + saturation badges; maintenance with
      zero blocks renders as "no data", not a 100 bar.
- [ ] Every page loads with no traceback (browser-verified, per QA skill).

---

## 8. Workstream B — de-legacy the solver (user-approved scope add-on)

### 8.1 Goal
`Flowstate-legacy/` stops being the home of any *live* code. The solver
moves into the main tree, renamed, with **zero references to legacy data
files**. `Flowstate-legacy/` becomes a pure archive (DEPRECATED.md + README
only) and is then deletable once the user confirms the solver works from the
new home.

### 8.2 Solver dependency map (verified this session)

The **live** solver files (imported by `phase2_scheduler.py`):
- `code/phase2_scheduler.py` → imports `data_loader`, `diagnostics`,
  `model_builder`, `validate_schedule`, `helpers.solver_progress`
- `code/model_builder.py` → imports `data_loader`
- `code/diagnostics.py` → imports `data_loader`
- `code/validate_schedule.py` → imports `data_loader`
- `code/helpers/solver_progress.py` → stdlib only (139 lines)

`sandbox_engine.py`, `version_manager.py`, `paths.py`, `safe_io.py` in the
legacy `helpers/` dir are **NOT** imported by the solver chain — they are
dead for the live path (the new app has its own).

### 8.3 Data-file reads inside the solver chain (`data_loader.Files`)

| File | Read | Used when |
|---|---|---|
| `capabilities_rates.csv` | always | capable flags + per-SKU rate (when `use_sku_rates`) |
| `changeovers.csv` (or `Changeovers.csv`) | always | setup hours + change flags |
| `initial_states.csv` | always | initial SKU / free hour / CIP carryover |
| `demand_plan.csv` | always | orders |
| `downtimes.csv` (or `Downtimes.csv`) | always | planned line-downs |
| `line_cip_hrs.csv` | always | hard CIP interval |
| `line_rates.csv` | **only when `use_sku_rates == false`** | flat monthly override |
| `sku_info.csv` | always | descriptions |
| `trials.csv` | always | protected trials |
| `line_sku_last_run.csv` | guarded (`os.path.exists`) | absent everywhere, harmless |

### 8.4 The move

New home: **`code/solver/`** (flat package, same files):
```
code/solver/__init__.py          (new, empty)
code/solver/phase2_scheduler.py  (renamed from Flowstate-legacy/code/phase2_scheduler.py)
code/solver/model_builder.py
code/solver/data_loader.py
code/solver/diagnostics.py
code/solver/validate_schedule.py
code/solver/solver_progress.py   (flattened from helpers/solver_progress.py)
```

Import adjustments (verified):
- `phase2_scheduler.py`: change `from helpers.solver_progress import (…`
  → `from solver_progress import (…` (it already does `sys.path.insert(0, BASE_DIR)`).
- `model_builder.py` / `diagnostics.py` / `validate_schedule.py`:
  `from data_loader import …` stays valid because `phase2_scheduler` adds
  `code/solver` to `sys.path` before importing them.
- `phase2_scheduler.py` reads `flowstate.toml [scheduler] use_sku_rates`
  directly (line 1251: `P.use_sku_rates = bool(_CFG_SCHED["use_sku_rates"])`),
  so flipping the toml value is sufficient — no code edit needed for the fix.

### 8.5 `use_sku_rates = true` (option B)

- Set `use_sku_rates = true` in `flowstate.toml`.
- Consequence: `data_loader` skips the `line_rates.csv` flat override
  (`if not self.P.use_sku_rates and os.path.exists(self.F.line_rates)`),
  so every (line, sku) rate comes from `capabilities_rates.csv`
  `calc_rate_kgph` — matching the UI/scorecard/naive baseline.
- `line_rates.csv` is then **dead data** → can be deleted from the repo
  after verification (it lives in `Flowstate-legacy/data/` only).

### 8.6 scenario_runner changes

`code/helpers/scenario_runner.py`:
- `_prepare_work_dir`: replace the "copy everything from legacy data dir"
  block with a copy from `data/reference/` ONLY. The reference dir already
  contains every solver input (`capabilities_rates.csv`, `changeovers.csv`,
  `downtimes.csv`, `demand_plan.csv`, `line_cip_hrs.csv`, `trials.csv`,
  `sku_info.csv`, `initial_states.csv`) — verified present this session.
  Normalize the legacy case names (`Changeovers.csv` → `changeovers.csv`,
  `Downtimes.csv` → `downtimes.csv`) unconditionally since we now control
  the source.
- `run_scenario`: point the subprocess at
  `code/solver/phase2_scheduler.py` with `cwd = code/solver`.
- `_overlay_current_state` stays exactly as-is (it patches the work-dir
  `initial_states.csv`).

### 8.7 Callers

- `code/pages/generate.py:44` — existence check →
  `(repo_root / "code" / "solver" / "phase2_scheduler.py")`.
- `code/helpers/paths.py` — add `solver_dir()`; `legacy_dir()` remains for
  the scorecard seed import (that one is a genuine legacy-data consumer for
  first-run import; keep until `azap_baseline` version is deleted, then the
  seed import path can be removed too).
- `code/pages/home.py` "About" text — update "Legacy solver-first app" wording.

### 8.8 Verification (per flowstate-app-qa skill — solver internals are NOT
browser-visible; verify with real CLI runs)

1. Full pytest suite green (84/84 on branch).
2. Real CLI run from the new home:
   `PYTHONPATH= .venv/Scripts/python.exe code/solver/phase2_scheduler.py --data-dir <workdir> --objective balanced --config flowstate.toml --two-phase`
   → FEASIBLE, `schedule_phase2.csv` + `feasibility_report.json` written.
3. Run `scripts/check_solver_current_state.py` (repeatable gate check) from
   the new home: **"lines starting BEFORE their gate: none"** — the
   availability floor is enforced at every relax level.
4. Browser: Generate Scenarios still runs a scenario A + D and renders a
   preview without error (verified 2026-08-10).
5. Confirm no `import Flowstate-legacy` / `legacy_dir` path remains in the
   solver chain: `grep -rn "legacy" code/solver/` → docstrings only.
6. **Rate-flip A/B (verified 2026-08-10):** same work dir, `use_sku_rates`
   false vs true, two-phase balanced, 60s:
   - false: 113 blocks, 36 orders short of qmin, 4,069,544 kg produced
   - true:  **134 blocks, 32 orders short of qmin, 4,255,487 kg produced**
   The flip does NOT regress feasibility — it improves throughput. The
   validation report's "1/4 checks / 106 issues" is the known relax-level-3
   state (UNDER by design + changeover gaps ignored under `ignore_co`),
   pre-documented in handoff-ww32; not a regression of this branch.

### 8.9 When is `Flowstate-legacy/` deletable?
After 8.8 passes and the user confirms the moved solver on a real run:
`git rm -r Flowstate-legacy/` (history preserves it), update AGENTS.md and
Home About text. `data/stockcheck/dev_vif` and `dev_receiving_schedule.xlsm`
are NOT part of legacy (they're new-app fixtures) — keep.
