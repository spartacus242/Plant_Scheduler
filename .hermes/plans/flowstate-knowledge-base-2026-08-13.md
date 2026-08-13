# Flowstate — Complete Knowledge Base

**Compiled:** 2026-08-13 (Hermes Director session) · **Repo:** `C:\Users\jbdil\Flowstate\Plant_Scheduler` · **Branch:** `develop` (working trunk; `main` frozen until go/no-go)
**Source of truth in-repo:** [Charter v3 — DECISIONS LOCKED](2026-08-12-charter.md) · `PROGRESS.md` (status board) · `README.md` · `AGENTS.md`

---

## 1. What Flowstate is

Flowstate is a **Streamlit-based manufacturing schedule decision-support tool** for a plant with **14 packaging lines** (P09–P22). It runs as ONE Streamlit service (`code/app.py`, port 8501); all state lives in flat CSV/JSON files under `data/` — no database, no external API.

**Product framing (README):** "Operational truth → Digital twin → Optimizer." The hard part is not generating a Gantt; it is a model that can say *"This schedule costs us 47.2 hours of manufacturing opportunity."* Planners won't accept "the computer says do this" — they accept "Current schedule scores 62; proposed schedule scores 84 — show me why."

**Core goal (user's words):** *"Produce the demand plan tonnage in the most efficient way possible. Minimal changeovers, maximum throughput."*

**Target user:** Carsten, the production planner. The tool runs alongside his current scheduling process for a few weeks, then either becomes primary or gets a written fix list.

---

## 2. The Charter (v3, decisions locked 2026-08-12)

### 2.1 Goal statement
> **Every day, Flowstate turns the plant's live data into ONE plan that makes all of the demand tonnage, on time, with the fewest changeovers and the highest throughput — 2 weeks locked and ready, week 3 flexible. Stock, cleaning, and capacity problems are flagged before you plan; you adjust with drag & drop; the tool replans around your blocks; changes go back to the plant system; the scorecard is honest.**

### 2.2 The daily loop
**Connect** (live data: manprg, CIP, stock, AZAP) → **Reconcile** (stock flags, orders that won't fit, cleaning due, capability conflicts) → **Plan** (solver proposes ↔ user drag & drop, both directions) → **Lock & Export** (2 weeks locked, changes written back to VIF) → **Track** (honest scorecard, no maintenance) → repeat. **Each week the lock rolls forward one week** (AZAP is a 3-week fixed horizon; the tool locks a 2-week schedule with a fluid week 3).

### 2.3 Measurables (M1–M6)
| # | What it means | Today | Target |
|---|---|---|---|
| **M1** | A plan that fits, runnable as-is | Fits in principle (demand < capacity) | 100% of daily plans fit real time; committed MOs keep ≥85% of their amount |
| **M2** | Demand covered | No coverage report | 100% of demand tonnage scheduled (or on a waiting list with a reason); sums add up ±0.1% |
| **M3** | Efficiency: fewest changeovers, max throughput | Half the changeover table is zero | Changeover count/hours reported every run; ≤ current manual plan's baseline |
| **M4** | Live data, not copies | Copied sample files | Live links; every number traceable; stale data warns. ✅ P3 done (`c462769`): no score on absent data — Service kg real or estimated+flagged; trials N/A when no input; CIP overdue-gate regression-tested |
| **M5** | Honest scores: no 100 without data; maintenance gone | 3 wrong scores + maintenance phantom | ✅ Maintenance removed (P2); other wrong scores fixed (P3); no 100 on missing data |
| **M6** | Daily use | No trial | Carsten opens it every workday for a 3-week trial; weekly roll ≤ 15 min |

**Done = M1–M5 green during a 3-week daily trial (M6) and Carsten says go.** Otherwise a written fix list — not more features.

### 2.4 Locked decisions (D1–D5, §10 of charter)
1. **Demand source:** `data/reference/demand_plan_summary.csv` is the LIVE link (bridge-refreshed daily into the repo). No external path needed.
2. **Time shape:** 2 weeks locked + 1 flexible, rolling weekly.
3. **Screens:** rebuild the daily screens around the sandbox — NOT tied to the current UI.
4. **Min run time:** 4 h is the rule (`flowstate.toml`), adjustable via config.
5. **Changeover table:** fixed from VIF/plant knowledge (done 2026-08-13 — see §6.3).

### 2.5 Phases and status
| Phase | What | Status |
|---|---|---|
| **P0** | Sign-off, tidy tree, charter committed | ✅ DONE (`6afd246`, `5297b29`) |
| **P1** | Live data links (manprg/CIP/stock/AZAP) | 🟡 ALMOST DONE — demand link done; **stock check live link missing** |
| **P2** | Runnable plan core: flat rates, maintenance out, constraint audit | 🟡 DONE (2 notes: P6/P8 probe coverage; TRIALS oddity) |
| **P3** | Honest scorecard | ✅ DONE (`c462769` — suite 196, browser-verified) |
| **P4** | Daily screens (Reconcile → bidirectional sandbox → lock/export → track) | ⬜ PENDING — biggest remaining chunk |
| **P5** | 3-week daily trial with Carsten | ⬜ PENDING |
| **P6** | Go / no-go | ⬜ PENDING |

---

## 3. Repository layout (current)

```
code/app.py                  Streamlit entry (8 nav groups, 10 pages)
code/pages/                  home (Command Center), data, scorecard, stock_check,
                             calendar, compare, generate, lines, settings
code/helpers/                config, horizon, calendar_io, scorecard_engine,
                             scorecard_ui, scenario_runner, current_state,
                             data_health, process_flow, importers (demand summary,
                             PDF, manual, manprg, cip), stock-check API
code/solver/                 phase2_scheduler.py (CLI), model_builder.py,
                             data_loader.py, diagnostics.py, validate_schedule.py,
                             solver_progress.py
code/components/gantt/       React/TS Gantt (prebuilt dist/ committed; rebuild
                             only after TS changes: npm run build in frontend/)
code/stockcheck/             VIF BOM explosion + component coverage engine
data/reference/              the input CSVs + live feeds (see §6)
data/seed/                   bundled seed fixtures (schedule_phase2.csv + cip_windows.csv)
data/scorecards/             weekly score JSON history (gitignored)
data/_scenario_work/         solver scratch work dirs (gitignored, rebuilt per run)
data/test_fixtures/          pinned snapshot for live-data tests
tests/                       21 test files — see §7
.hermes/plans/               charter, flow diagram, dispatch specs, design docs
```

### Plan/design docs in `.hermes/plans/`
`2026-08-12-charter.md` (source of truth) · `2026-08-12-flow-diagram.html` (daily-flow SVG) · `command-center-design.md` · `handoff-log.md` · `handoff-ww32.md` · `live-ops-plan.md` · `solver-current-state-design.md` · `stock-check-design.md` · dispatch specs: `p1-dispatch1-demand-path-staleness.md`, `p2-dispatch1-flat-rates.md`, `p2-dispatch2a-maintenance-out.md`, `p2-dispatch3-stabilize-live-data-tests.md`, `p2-dispatch4-constraint-probes.md`, `p2-dispatch5-minrun-current-mo.md`, `p3-dispatch1-scorecard-honesty.md`.

---

## 4. The solver (CP-SAT)

- **Engine:** Google OR-Tools CP-SAT, spawned as an on-demand subprocess by the Generate Scenarios page via `code/solver/phase2_scheduler.py` (same Python interpreter). Reads inputs from a scratch work dir under `data/_scenario_work/` rebuilt from `data/reference/` each run.
- **Work dirs:** scenarios A–D (two-phase: Week-0 168h then Week-1) and **E** (single-phase current-state, `use_current_mo=true`, 504h horizon, ladder skips levels 1–2 → jumps 0→3). Scenario E MUST be single-phase (two-phase can't place multi-week current MOs).
- **Model size (E/Etrim):** ~158k vars / 593k constraints at 504h single-phase; ~18k/32k trimmed. Single-phase full-horizon 504h is the HARDEST mode (can exceed 300s budget → UNKNOWN; 9.8 GB memory blowup observed on an orphaned run — kill orphan solves).
- **Objective (weighted):** changeover 120, makespan 6, late 200, CIP-defer 5, week-deviation 40. Charter direction: one objective, no knobs; "minimal changeovers" is the dominant term.
- **Config:** `flowstate.toml` — `min_run_hours = 4`, `use_sku_rates = false` (flat line rates), changeover attribute weights (ffs 75 — largest; topload 50, ttp 5, casepacker 20, conv_org 30, cinn 20, flavor 5, base 5).
- **Constraint inventory (audited, charter §5):** min-run floor · batch size qmin/qmax · capability gating · NoOverlap per line (prod + CIP) · CIP hard deadline (max_cip_hrs, duration 6h) · ≤2 lines per order · due dates (soft late penalty 200 + hard cap due+1) · demand total hard with cross-week preference.
- **Relax ladder:** level 0 (hard) → 1 (relax_demand, qty_min→0) → 2 (+soft due) → 3 (+ignore_co). Level 0 is **genuinely INFEASIBLE** on the current dataset (P11/P13 down 0–504h + demand at hard qmin) — pre-existing; the ladder escalates to 3 (FEASIBLE). `_RELAX_SKIP = {0: 3}` in current-MO mode (levels 1–2 never materialize for scenario E).

### 4.1 Current-MO (committed work) handling — dispatch 5 story
- Current MOs (from `current_mo.csv`, source manprg) are **locked to their line** (present=1 forced at every relax level; only tonnage adjustable; `qty_min == qty_max == remaining_kg`).
- **Min-run floor (user-approved 4h, committed `e778e6d`):** committed MOs fall through to the run-bound block and get `run_h ≥ min_run` — **only when remaining/rate ≥ 4h** (nearly-done MOs get no floor; a floor above remaining/rate is mathematically unsatisfiable with integer hours). None-safe rate lookup; per-segment floors apply to normal orders only (CIP splits can legitimately leave <4h on one side; the order-TOTAL floor is the stub-prevention contract).
- Capability gate is bypassed for current MOs (manprg is ground truth — never zero out what the plant ran); a manprg (line, sku) missing from capabilities is excluded from changeover/CIP eligibility lists and surfaced via `diagnostics.py`.

### 4.2 Known solver findings (open or resolved)
| Finding | Status |
|---|---|
| Min-run floor skipped for current MOs (1h stubs for committed work) | ✅ FIXED (dispatch 5, `e778e6d`) |
| Level-0 hard INFEASIBLE on current dataset (P11/P13 down + demand at qmin) | 🟡 Open — ladder handles it; candidate dispatch: producible-zeroing fix |
| TRIALS current MOs present-but-0-kg (no rate for pseudo-SKU → prod=0, no rows) | 🟡 Open (dispatch 4 finding 3) |
| P6 (changeover gaps) / P8 (due caps) probes unexercised — no relax ≤2 artifact exists for scenario E | 🟡 Open — run an A–D solve at level ≤2 to exercise |
| 9.8 GB memory blowup on long single-phase solves (orphan runs) | ✅ Operational note — kill orphan solves; direct invocation with `-u` + log file |

---

## 5. The scorecard (Phase 0)

- **Categories (5):** changeovers, cip, trials, campaigns, service → weighted composite (0.30 service). Maintenance REMOVED (P2 `f3bf013` — can still block time on the calendar; never scored).
- **CIP category:** scored on **overdue compliance alone** (cap 1 → any overdue = 0; a fully compliant schedule = 100). `cip_count/hours/forfeited_kg` reported for diagnostics only — "fewer CIPs = higher score" rewarded never cleaning. Raw inputs visible on the page.
- **Service:** orders_late, orders_at_risk, excess_inventory_kg (cap 50,000 kg).
- **P3 changes (`c462769`):**
  - **Solver writes real `qty_kg`** into `schedule_phase2.csv` (per-row `rate × run_hours`, the model's own decomposition, with a reconcile pass pinning order totals to produced).
  - **`import_solver_schedule`** (renamed from `import_legacy_schedule` — no legacy names!) carries `qty_kg` through to the calendar.
  - **`load_calendar` keeps NaN** for unknown kg (was `fillna(0)` → unknown read as "no excess" — the fake-zero defect). Display sites blank it.
  - **Estimate fallback:** when kg is absent/all-NaN/all-zero, Service estimates `run_hours × line avg rate` and sets `excess_inventory_kg_estimated=true` → page shows "(estimated)".
  - **Trials N/A:** when the calendar has no trial blocks AND `reference/trials.csv` is absent/empty → category is None (not silent 100).
  - New `tests/test_scorecard.py` (~21 tests).
- **Known scorecard history:** cip_forfeited 1120h saturating a 200 cap → CIP 2/100 (defect — superseded by the compliance-gate redesign); maintenance phantom 100 (fixed P2); Service ignoring kg (fixed P3). **Verified in browser:** solver-schedule calendar now scores Service 53 with real excess 1.8 kg (was a fake 0.0).

---

## 6. Data model, live sources & plant knowledge

### 6.1 The live-data bridge
A GitHub file-drop bridge (`scripts/fs-live-*.py` + `flowstate-live-data` repo, personal + work PCs) refreshes `data/reference/` daily: manprg (MO progress), cip_info, capabilities, changeovers, demand, downtimes, initial states, line_cip_hrs, sku_info, trials. The bridge-owned files are **gitignored** (never committed). `data/scorecards/` runs are also gitignored.

### 6.2 Files in `data/reference/`
`capabilities_rates.csv` (line×SKU capable + rate_kgph/calc_rate_kgph) · `changeovers.csv` (37,830 rows) · `cip_info.csv` (live CIP schedule; watch for garbage dates from bridge — `05:00.0`-style corruption observed 2026-08-13) · `demand_plan.csv` (101 orders, `<sku>-W<idx>`, qty_target + pct bounds, priority 3) · `demand_plan_summary.csv` (SOLE demand import for the app; self-anchors to Monday of earliest ISO week) · `downtimes.csv` (P11, P13 down 0–504h currently) · `initial_states.csv` (gates, carryover CIP hours) · `line_cip_hrs.csv` (max interval per line) · `line_rates.csv` (flat rates — now bridge-delivered) · `sku_info.csv` · `sku_plan_evidence.csv` · `trials.csv` (1 row currently: P12 trial 2026-08-15 16:00, 60,000 kg).

### 6.3 Plant process knowledge (user-provided 2026-08-13 — the changeover semantics)
- **Tetrapak (ttp)** = where applesauce is made/mixed; the sauce is cooked there and sent to the FFS machines.
- **FFS** = form-fill-seal machines (two types: **Volpak** and **Bossar**) that package sauce into pouches. **`ffs_change` = pouch-size changes (90 g / 110 g / 120 g) — changeovers there DO happen** (the column was dead/zero everywhere in the old file; the user's updated file makes it alive: 12,306 pairs).
- **Topload** = end-of-line: pouches packaged into sleeves.
- **Casepacker** = sleeves sometimes packed into cases.
- The updated `changeovers.csv` (user-provided): 37,830 rows; `ttp_change > 0` on 99.6% of rows; `topload_change` 32,700; `casepacker_change` 21,602; `ffs_change` 12,306; `setup_hours > 0` on only 40% — **zeros are REAL (user-confirmed)**; cost is attribute-weighted. All demand SKUs present (280698, 570560 now in the table).

### 6.4 Key verified numbers
- Flat line rates sum: **14,702 kg/h** → ~2,470 t/week max, ~2,300 t realistic → **demand fits**.
- Demand: W32 ≈ 1,954 t · W33 1,686.9 t · W34 1,840.3 t · W35 1,766.1 t.
- The old "7.1 kt demand vs capacity" scare was a misread (4-week total vs short window) — retracted.
- Snapshot fixture (`data/test_fixtures/live_2026-08-13/`): 58 rows, MO 29901 = 2,840 cases left, P09 scheduled 8/15, P14 none — provenance `git 6103535^:data/reference/`.
- Suite: **196 passed / 0 failed / 3 skipped / 8 deselected** (skips = level-gated probes P6/P8/P8b, loud by design).

---

## 7. Testing architecture & environment

- **Pytest from repo root:** `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` (PYTHONPATH scrub is REQUIRED — a leaked agent site-packages shadows the venv numpy and crashes pandas). 8 slow solver-contract tests deselected by default.
- **Test files (21):** capability_check, changeover_cache, constraint_probes (P1–P9 + checker tests), current_mo, current_state, current_state_overlay, data_health, holding_builder, horizon, line_rates, live_imports, **no_live_data_reads (guard — was inert, fixed 2026-08-13 to iterate lines not chars, comment/docstring-aware, self-exempt)**, schedule_pdf_import, scorecard (new, P3), settings_toml, solver_contracts, solver_fresh_solve, stockcheck_engine, stockcheck_receiving, warm_start.
- **Live-data tests pin a committed snapshot** (`data/test_fixtures/live_2026-08-13/`), never live `data/reference/`; the guard test enforces it (test_changeover_cache exempt — stages its own copy).
- **Verification discipline:** every claim verified with real runs; solver internals verified via CLI runs + `feasibility_report.json`; UI changes browser-verified; suite green before commit; probe tests never weakened (loud skips with reasons instead).

---

## 8. Execution model & workflow (how we work)

- **Cost-aware:** Director runs on a cheap model (deepseek-v4-flash via nous) for orchestration/analysis/verification. **ALL Flowstate coding via Claude Code CLI** (Claude Max, logged in as jbdille@gmail.com). **Default coding model: `fable`** (user: "Fable 5"; CLI alias `fable` — `fable-5` does NOT resolve; verified 2026-08-13). Simple/mechanical tasks → haiku/sonnet or Director directly. Dispatches: `--max-turns 150 --max-budget-usd 20` in background (`claude -p 'Execute the spec in …' --model fable …`).
- **Dispatch protocol:** Director writes a bounded spec (verified context → exact scope with file:line → hard rules → exit criteria); executor implements (no commits, no pushes, preserve CRLF in edited files, `env -u PYTHONPATH` pytest); Director verifies independently before accepting. Solver-behavior changes need user approval first; probes never weakened; found bugs are reported, not silently fixed.
- **Branching:** one dedicated branch `develop` (19 stale branches + ghost worktree removed 2026-08-13); `main` frozen until go/no-go; PRs target `develop`.
- **Working agreement:** source artifacts analyzed + clarifying questions BEFORE code; design doc for review; feature work on a branch until user approves; plain language in docs (no jargon like "gap policy").
- **App lifecycle (Windows):** launch/stop via the Hermes profile scripts `fs-launch.ps1 -Dir … -Port 8501`, `fs-stop.ps1 -Port 8501`, `fs-health.ps1` (never foreground health-polls or `Start-Process -RedirectStandardOutput` — they hang). PYTHONPATH scrubbed inside the launcher.
- **Environment:** Windows host; terminal runs git-bash/MSYS (POSIX syntax); venv Python 3.12 at `.venv\Scripts\python.exe`; search_files tool is broken in this env (MSYS path mangling) — use terminal grep; `wmic` removed on this build (use `Get-CimInstance`); MSYS `ps` hides native processes (use CIM).

---

## 9. Known open items (as of 2026-08-13)

1. **P1:** stock check live link missing (only remaining P1 item).
2. **P2 notes:** P6/P8 probes need an A–D solve at relax ≤2; TRIALS present-but-0-kg oddity.
3. **Level-0 infeasibility** on current dataset (P11/P13 down) — worth its own dispatch (producible-zeroing fix).
4. **Plant Calendar save wipes `qty_kg`** — `calendar_to_gantt_payload` drops it; proper fix needs a frontend TS change + bundle rebuild (fold into P4; the degraded path is honest — it takes the "(estimated)" route).
5. **Mixed real/missing kg** understates production slightly (has_qty if ANY row has kg; NaN rows count as 0 in that path) — known edge, flagged.
6. **Bridge data quality:** cip_info.csv garbage dates observed once (05:00.0/00:00.0) — user checks the work-side export if it recurs.
7. **P4:** the daily screens rebuild (biggest chunk — Reconcile with stock flags, bidirectional sandbox DnD, qty_kg-preserving save, one-action weekly roll).
8. **P5/P6:** 3-week Carsten trial → go/no-go.

---

## 10. Session history — how we got here (2026-08-12 → 2026-08-13)

1. User: "we've lost the thread" → charter v1 → v2 (plain language) → v3 (decisions locked), flow diagram built, verified.
2. Live-data pivot: demand corrected (14,702 kg/h capacity), flat rates adopted, maintenance out of scorecards, GitHub file-drop bridge adopted (P1).
3. Branch consolidation: 19 branches → single `develop`; live-data files gitignored; live-data tests pinned to a committed snapshot (dispatch 3) + guard test.
4. Constraint probes P1–P9 (dispatch 4) caught the current-MO min-run skip → user approved the 4h floor → dispatch 5 with three engineering guards discovered by A/B verification (nearly-done MOs, segment floors, None-safe rates); suite 175 green.
5. User decisions: demand live link = `demand_plan_summary.csv`; maintenance out of scorecards; updated changeovers.csv + plant process knowledge (ffs_change semantics).
6. P3 (dispatch 1): solver writes qty_kg, loader keeps NaN, Service estimates+flags, trials N/A, guard test fixed (was inert); suite 196 green; browser-verified end-to-end (Service 53, real excess 1.8 kg).
7. Naming cleanup: `import_legacy_schedule` → `import_solver_schedule` (no legacy-named live code).
8. Model switch: coding model = Fable 5 (alias `fable`).

**Recent commits on `develop`:** `f8ec7f8` docs(progress): P3 done · `c462769` fix(scorecard): P3 honesty · `2012a35` docs(progress): P2 done · `e778e6d` fix(solver): min-run floor · `d646fd7` test(solver): probes · `5ab972e` test(solver): snapshot pin + guard.

---

## 11. Stored memory (verbatim, current)

- `min_run=4 (user-approved)`
- User jbdil: real Windows Desktop is OneDrive-synced at `C:\Users\jbdil\OneDrive\Desktop` (no `C:\Users\jbdil\Desktop`). Flowstate app runs on port 8501 via repo venv `.venv\Scripts\python.exe` (Python 3.12).
- User expects the Director to catch these errors, not to click every button himself. 'Ultra mode' = max rigor, drive to completion. No 'legacy'-named live code (user rejected `import_legacy_schedule`).
- Launch via hermes-profile scripts `fs-launch.ps1`/`fs-health.ps1`/`fs-stop.ps1` (never foreground health-polls or `Start-Process -RedirectStandardOutput` — they hang).
- Demand source: `demand_plan_summary.csv` SOLE import (self-anchors Mon of earliest ISO week); `demand_plan.csv`: `<sku>-W<idx>` orders, qty_target + pct bounds, priority=3.
- holding under-qmin/zero-qty; click-a-SKU → holding.
- Cost-aware execution: Director on cheap model (deepseek-v4-flash) for orchestration/analysis/verification; ALL Flowstate coding via Claude Code CLI (Claude Max); default coding model **fable** (user: 'Fable 5'; CLI alias fable, fable-5 invalid); simple tasks → haiku/sonnet/Director.

## 12. User profile (verbatim, current)

- User owns GitHub **spartacus242** and actively develops the Flowstate repo (Streamlit manufacturing-schedule decision-support tool).
- Flowstate data is REAL plant data, but the seeded schedule has been modified and is not the true historical schedule.
- Target user is the production planner (Carsten); tool runs alongside his process for a few weeks before becoming primary.
- Horizon intent: AZAP is a 3-week fixed horizon; tool should lock a 2-week schedule with a fluid week 3, rolling weekly.
- Next tracks (user-chosen priority): (1) solver feasibility overhaul ✅; (2) open question — weekly AZAP baseline: current modified CSV vs fresh true-AZAP export (+ canonical format for an importer); (3) scorecard defects (all three now fixed).
- Feature work: analyze source artifacts + clarifying questions BEFORE code; design doc for review; build on a feature branch until approved.

---

## 13. Skills catalog (all skills + text descriptions)

### autonomous-ai-agents
- **claude-code** — Delegate coding to Claude Code CLI (features, PRs).
- **codex** — Delegate coding to OpenAI Codex CLI (features, PRs).
- **computer-use** — Drive the user's desktop in the background — clicking, ty...
- **hermes-agent** — Use, configure, theme, extend, and orchestrate Hermes Agent.
- **opencode** — Delegate coding to OpenCode CLI (features, PR review).

### creative
- **architecture-diagram** — Dark-themed SVG architecture/cloud/infra diagrams as HTML.
- **ascii-art** — ASCII art: pyfiglet, cowsay, boxes, image-to-ascii.
- **ascii-video** — ASCII video: convert video/audio to colored ASCII MP4/GIF.
- **baoyu-infographic** — Infographics: 21 layouts x 21 styles (信息图, 可视化).
- **claude-design** — Design one-off HTML artifacts (landing, deck, prototype).
- **comfyui** — Generate images, video, and audio via diffusion workflows.
- **design-md** — Author/validate/export Google's DESIGN.md token spec files.
- **excalidraw** — Hand-drawn Excalidraw JSON diagrams (arch, flow, seq).
- **humanizer** — Humanize text: strip AI-isms and add real voice.
- **manim-video** — Manim CE animations: 3Blue1Brown math/alg videos.
- **p5js** — p5.js sketches: gen art, shaders, interactive, 3D.
- **popular-web-designs** — 54 real design systems (Stripe, Linear, Vercel) as HTML/CSS.
- **pretext** — Build creative browser demos with DOM-free text layout.
- **sketch** — Throwaway HTML mockups: 2-3 design variants to compare.
- **songwriting-and-ai-music** — Songwriting craft and Suno AI music prompts.
- **touchdesigner-mcp** — Control TouchDesigner via twozero MCP.

### email
- **himalaya** — Himalaya CLI: IMAP/SMTP email from terminal.

### flowstate-carsten-run
- **flowstate-carsten-run** — Use when running Flowstate as Carsten (cron or manual): s...

### github
- **codebase-inspection** — Inspect codebases w/ pygount: LOC, languages, ratios.
- **github-auth** — GitHub auth setup: HTTPS tokens, SSH keys, gh CLI login.
- **github-code-review** — Review PRs: diffs, inline comments via gh or REST.
- **github-issues** — Create, triage, label, assign GitHub issues via gh or REST.
- **github-pr-workflow** — GitHub PR lifecycle: branch, commit, open, CI, merge.
- **github-repo-management** — Clone/create/fork repos; manage remotes, releases.

### media
- **gif-search** — Search/download GIFs from Tenor via curl + jq.
- **songsee** — Audio spectrograms/features (mel, chroma, MFCC) via CLI.
- **youtube-content** — YouTube transcripts to summaries, threads, blogs.

### mlops
- **huggingface-hub** — HuggingFace hf CLI: search/download/upload models, datasets.

### mlops/evaluation
- **weights-and-biases** — W&B: log ML experiments, sweepes, model registry, dashboards.

### mlops/inference
- **llama-cpp** — llama.cpp local GGUF inference + HF Hub model discovery.

### note-taking
- **obsidian** — Read, search, create, and edit notes in the Obsidian vault.

### productivity
- **airtable** — Airtable REST API via curl. Records CRUD, filters, upserts.
- **docx** — Create, read, edit Word .docx documents and templates.
- **excel-forensics** — Use when dissecting .xlsm data sources (Power Query, VBA).
- **excel-workbook-forensics** — Use when dissecting .xlsm internals: Power Query, VBA.
- **google-workspace** — Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python.
- **maps** — Geocode, POIs, routes, timezones via OpenStreetMap/OSRM.
- **nano-pdf** — Edit text in existing PDFs via natural-language prompts.
- **notion** — Notion API + ntn CLI: pages, databases, markdown, Workers.
- **ocr-and-documents** — Extract text from PDFs/scans (pymupdf, marker-pdf).
- **pdf** — Create, read, edit, fill, and secure PDF files.
- **powerpoint** — Create, read, edit .pptx decks, notes, templates.
- **teams-meeting-pipeline** — Teams meeting summaries, job replay, Graph subscriptions.
- **xlsx** — Create, read, edit Excel .xlsx spreadsheets and CSVs.

### research
- **arxiv** — Search arXiv papers by keyword, author, category, or ID.
- **blogwatcher** — Monitor blogs and RSS/Atom feeds via blogwatcher-cli tool.
- **grounded-citations** — Ground answers and documents in cited, verifiable sources.
- **llm-wiki** — Karpathy's LLM Wiki: build/query interlinked markdown KB.
- **polymarket** — Query Polymarket: markets, prices, orderbooks, history.

### smart-home
- **openhue** — Control Philips Hue lights, switches, scenes, rooms via OpenHue CLI.

### software-development
- **behavior-preserving-changes** — Docs/UI/refactor edits that must not change behavior.
- **cp-sat-scheduler-feasibility** — Make CP-SAT schedulers degrade gracefully, not INFEASIBLE.
- **dogfood** — Exploratory QA of web apps: find bugs, evidence, reports.
- **flowstate-app-qa** — Browser-verify the Flowstate app before claiming it works.
- **flowstate-cp-sat-solver** — Use when editing the Flowstate CP-SAT solver in code/solv...
- **flowstate-planning** — Use when planning Flowstate direction/charters/design docs.
- **flowstate-vif-erp-data** — VIF ERP exports (ediact BOM, jestkexp stock) in Flowstate.
- **flowstate-vif-imports** — Use for Flowstate work on VIF ERP exports or stock check.
- **hermes-agent-skill-authoring** — Author in-repo SKILL.md files: frontmatter and structure.
- **inspecting-hermes-desktop-dom** — Read the live Hermes desktop DOM/CSS over CDP.
- **node-inspect-debugger** — Debug Node.js via --inspect + Chrome DevTools Protocol CLI.
- **orchestrating-coding-subagents** — Use when delegating repo coding to background subagents.
- **plan** — Write a markdown plan to .hermes/plans/; no execution.
- **requesting-code-review** — Pre-commit review: security scan, quality gates, auto-fix.
- **simplify-code** — Parallel 4-agent cleanup of recent code changes.
- **spike** — Throwaway experiments to validate an idea before build.
- **streamlit-app-qa** — Browser-QA Streamlit apps: stale helpers, hidden errors.
- **systematic-debugging** — 4-phase root cause debugging: understand bugs before fixing.
- **test-driven-development** — TDD: enforce RED-GREEN-REFACTOR, tests before code.
- **verifying-web-ui-changes** — Prove a local web UI change works via DOM, not screenshots.
- **windows-shell-scripting** — Use for Windows PowerShell/.bat/.vbs/.lnk from git-bash.

---

## 14. Tools (all available + usage in Flowstate work)

### File & workspace
- **read_file** — read files with line numbers (used constantly: charter, solver, tests, docs).
- **write_file** — create/overwrite files (charter, dispatch specs, guard test, probe).
- **patch** — targeted find/replace edits + V4A multi-file patches (all code edits by Director).
- **search_files** — ripgrep-backed search (BROKEN in this env — MSYS path mangling; fallback: terminal grep).
- **focus_pane / open_preview** — desktop app panes; preview pane used for the flow diagram.
- **read_terminal** — read the in-app terminal pane (unused in Flowstate work so far).

### Terminal & processes
- **terminal** — shell (git-bash/MSYS on Windows): git, pytest, solver CLI runs, greps, process mgmt (used constantly; `env -u PYTHONPATH` pattern).
- **process** — poll/wait/kill/log background processes (dispatches, solver runs, app health).
- **close_terminal** — drop a background process's terminal tab.

### Browser (UI verification)
- **browser_navigate / browser_snapshot / browser_click / browser_type / browser_press / browser_scroll / browser_back** — drive the app at localhost:8501 (scorecard page verification, nav).
- **browser_vision** — screenshot + visual analysis (baseline evidence).
- **browser_console** — page console/JS errors + DOM evaluation.
- **browser_get_images** — list page images.

### Knowledge & memory
- **web_search / web_extract** — web lookups (model research, docs).
- **session_search** — past-session recall (used for the charter thread + this knowledge base).
- **memory** — persistent memory store (model policy, env facts, user prefs).
- **skills_list / skill_view / skill_manage** — the skill system (plan, flowstate-*, hermes-agent, claude-code, cp-sat skills used).

### Delegation & automation
- **delegate_task** — background subagents (isolated contexts).
- **cronjob** — scheduled jobs (the Carsten-run automation pattern exists via flowstate-carsten-run skill).
- **todo** — task tracking for multi-step work.
- **execute_code** — Python scripts calling Hermes tools programmatically (filtering/loops).

### Media & generation
- **image_generate** — FAL FLUX 2 image generation.
- **bfl_flux3_text_to_video / image_to_video / keyframes_to_video / video_continuation / get_result / prompting_guide** — FLUX 3 video generation suite.
- **vision_analyze** — load an image for analysis.
- **text_to_speech** — TTS (Edge/OpenAI).

### Interaction & deferred tools
- **clarify** — ask the user with choices.
- **tool_search / tool_describe / tool_call** — load deferred tools (project_create/list/switch: desktop Projects).
- **project_create / project_list / project_switch** — desktop named workspaces (deferred).

### External executor (the coding engine)
- **Claude Code CLI** (`claude`, Claude Max account jbdille@gmail.com) — ALL Flowstate coding; models: `fable` (default now), opus, sonnet, haiku; run in background with `--max-turns 150 --max-budget-usd 20 --allowedTools Read,Write,Edit,Bash`.
- **Hermes profile scripts** — `fs-launch.ps1`, `fs-stop.ps1`, `fs-health.ps1`, `fs-carsten-run.py` (app lifecycle + scheduled Carsten runs).

---

*End of knowledge base. Maintained by the Hermes Director; the charter + PROGRESS.md in-repo are the live sources of truth.*
