# P4 — Daily Loop (plan, 2026-08-13)

**Branch:** `develop` (work slices land here; each slice is browser-verified + suite-green before commit)
**Charter ref:** [Charter v3 §2.2 the daily loop](2026-08-12-charter.md) · **Status board:** `PROGRESS.md`
**Goal of P4:** make the daily loop *walkable and trustworthy* so Carsten opens Flowstate every workday during the 3-week trial (M6). Trial is imminent (~2 weeks) → **usability + honesty over new features.**

---

## 1. The daily loop (charter) and where it stands today

**Connect → Reconcile → Plan → Lock & Export → Track → (weekly) Roll.**

P4 is **not a from-scratch rebuild.** ~70% of the loop is already wired. This plan *assembles, hardens, and fills three real holes* — it does not re-implement the Gantt or the scorecard.

| Loop step | What exists today | Gap to close in P4 |
|---|---|---|
| **Connect** (live data) | Bridge refreshes `data/reference/` daily; `data_health.assess()` flags stale/missing/semantic; staleness banners on Home + Calendar | ✅ Adequate. Only P1 stock-check live link still missing (tracked separately). |
| **Reconcile** (flag problems *before* planning) | Pieces scattered: `data_health` (freshness/semantic), `stock_check` page (component coverage), solver `diagnostics` (capability conflicts) | ❌ **No single Reconcile screen.** The "what's wrong before I plan" review isn't assembled. **Biggest gap.** |
| **Plan** (solver ↔ drag&drop) | `calendar.py` = bidirectional sandbox: DnD Gantt, live rescore, holding area, "rebuild from plant state", scenarios E/A–D via `generate.py` → save version | 🟡 Mostly done. One correctness bug: **save wipes `qty_kg`** (frontend TS). |
| **Lock & Export** (2wk locked, write back to VIF) | `compare.py` promote→official + Excel export; solver writes `mo_changes.csv` | ❌ **No week-lock**; ❌ **`mo_changes.csv` never surfaced** (VIF write-back generated but not reviewable/exportable). |
| **Track** (honest scorecard) | `scorecard.py` + history; P3 honesty done | ✅ Done. (Depends on the `qty_kg` fix to stay honest after edits.) |
| **Roll** (lock rolls forward 1 week) | "Roll calendar to today" button rebases blocks onto today's anchor | 🟡 Rebase exists; the "lock advances one week" semantics don't. |

---

## 2. Design principle — build P4's surfaces as shared primitives the agent reuses

The end-goal (P5+) is an **AI agent that steers the solver, proposes macro adjustments on a sandbox with reasoning, and hands a diff to a human to accept.** Every P4 piece below is built so the agent plugs into the *same* primitive — no rebuild:

- **Reconcile** → a pure `reconcile_engine.py` (like `data_health`) returning typed findings. The **human screen renders it; the agent reads it** as its situational input ("what changed / what's at risk today").
- **Plan** → the solver + versions are already the agent's action space (agent picks intent, drives Scenario E, saves a version). No change needed beyond the `qty_kg` honesty fix.
- **Lock & Export diff** → `compare.py`'s "official vs proposed" diff + `delta_narrative` is exactly where **agent proposals will surface with their reasoning.** Harden it into a real accept/reject surface now.
- **Write-back** → `mo_changes.csv` review/export is the human-accept → plant handoff; the agent produces the same artifact.

So: **Reconcile findings, the sandbox-version model, and the accept-diff surface are the three seams the agent will inherit.** Build them clean.

---

## 3. Concrete gaps → work slices (trial-critical first)

### Slice 1 — Fix the `qty_kg` save-honesty bug  *(blocks trust; small)*
**Problem (KB open item 4):** `calendar_to_gantt_payload` drops `qty_kg`; after any manual drag+save the Service score silently falls back to the "(estimated)" path even when the solver had real kg. The honest scorecard (P3) degrades the moment Carsten touches a block.
**Fix:** carry `qty_kg` through the Gantt payload round-trip (`calendar_to_gantt_payload` → `GanttBlock` TS → `gantt_payload_to_calendar`); rebuild the `dist/` bundle. Preserve kg on unmoved blocks; recompute `rate × run_h` only for blocks whose duration changed.
**Verify:** drag a block, save, confirm Service stays on the *real* kg path (no "(estimated)" flag) for untouched blocks; browser-verified; `test_scorecard` extended.

### Slice 2 — Reconcile screen  *(highest planner value; the missing screen)*
**New:** `helpers/reconcile_engine.py` (pure, tested) → `assess_plan(cal, live, cfg) -> list[Finding]`, aggregating what a planner must see *before* touching the schedule:
- **Stock won't cover** a scheduled run (from `stockcheck/` BOM explosion + component coverage).
- **Order won't fit** real available time / committed MO keeps < 85% (M1).
- **Cleaning due** — line past `MaxHoursBetweenCIP` with no CIP drawn.
- **Capability conflict** — a scheduled (line, sku) missing from `capabilities_rates.csv` (surfaced today only in solver `diagnostics`).
- **Demand not covered** — SKUs on no line / under qmin (M2), reusing the holding-area logic.

**New page** `pages/reconcile.py` — ranked findings (blocking → warn), each with a deep-link to the page that resolves it, mirroring Home's "what to do next" pattern. Reuses `data_health` styling/severity vocabulary.
**Verify:** unit tests per finding class; browser-verify against the pinned snapshot fixture; a known stock short + a capability gap both surface.

### Slice 3 — Lock & Export, made real  *(the plant handoff)*
1. **Week-lock model** — add a "locked-through" date (2 weeks from anchor) in `flowstate.toml`/session. Blocks inside the locked window render locked and are guarded from drag/replace in the sandbox; the solver already respects per-block `locked`. Visual lock line on the Gantt + a "Lock weeks 1–2" affordance.
2. **`mo_changes.csv` review + export** — surface the solver-generated split/trim/reorder deltas in `compare.py` (or the promote flow): a table of "what changes vs what the plant currently has committed," exportable for VIF write-back. This is the "changes go back to the plant system" charter promise.
**Verify:** locked blocks reject moves in-browser; `mo_changes` table matches solver output; Excel/CSV export round-trips.

### Slice 4 — Daily-loop framing + one-action weekly roll  *(polish; makes it a cockpit)*
- Re-group nav / Home to walk the loop (**Connect · Reconcile · Plan · Lock · Track**) instead of feature tabs, so "every morning" has an obvious path. Low-risk relabel + ordering; pages unchanged.
- **Weekly roll**: extend the existing "Roll to today" into one action that advances the 2-week lock window forward one week and re-anchors, per charter §2.2. Target ≤ 15 min weekly (M6).
**Verify:** browser walk of the full loop; roll advances lock window correctly; backups written.

---

## 4. Sequencing & why

1. **Slice 1 (qty_kg)** first — it's small and it protects the P3 honesty win the moment Carsten edits anything. Trust is the trial's currency.
2. **Slice 2 (Reconcile)** next — the single highest-value screen for a planner and the agent's future input. Ship it early to dogfood during the trial.
3. **Slice 3 (Lock/Export)** — needed before the plan is actually handed to the plant.
4. **Slice 4 (framing/roll)** — polish; can land mid-trial from Carsten's feedback.

Slices 1–3 are the trial-critical core. Slice 4 can slip without blocking daily use.

## 5. The agent seam (P5+, not built in P4 — recorded so P4 stays compatible)
When the agent arrives: it reads **Slice 2's `reconcile_engine` findings** as its situational input → decides intent → drives **Scenario E** (its action space, already built) → saves a **sandbox version** → its reasoning + the **Slice 3 diff/`mo_changes`** surface become the human accept/reject screen. No P4 surface gets rebuilt; the agent is a new *producer* of versions + a *narrator* on the existing compare/diff surface.

## 6. Working discipline (unchanged from the project agreement)
- Each slice on `develop`, browser-verified per `flowstate-app-qa`, suite green (`env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q`) before commit; CRLF preserved; no solver-behavior change without sign-off; probes never weakened.
- New engines are pure + unit-tested; pages stay thin renderers (repo convention).

## 7. Acceptance (P4 done when)
- [ ] Manual drag+save keeps the *honest* (real-kg) Service score for untouched blocks.
- [ ] Reconcile screen surfaces stock / fit / cleaning / capability / coverage problems before planning, ranked, with deep links — browser-verified.
- [ ] Weeks 1–2 lock is visible and guarded; `mo_changes.csv` is reviewable + exportable for VIF.
- [ ] The loop is walkable Connect→Reconcile→Plan→Lock→Track; weekly roll is one action ≤ 15 min.
- [ ] Full suite green; every page loads with no traceback.
