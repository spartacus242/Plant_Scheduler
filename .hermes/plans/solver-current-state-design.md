# Solver-Driven Current State — Design Doc (DRAFT)

Status: DRAFT — for review
Date: 2026-08-10
Branch: feature/command-center (this feature builds on it; separate commits)

## 1. Problem statement

Carsten uploads the live plant state (manprg/cip_info). Today that state is
drawn onto the calendar as-is, and the solver only sees it as *gates*
(`initial_states.available_from_hour`) — it never re-optimizes the MOs that
are already scheduled. Three concrete defects:

1. **CIP ↔ production overlap (measured: 21 overlaps on the 2026-08-10
   export).** `helpers/current_state.py` lays queued MOs sequentially (cursor
   advances over production only), then projects CIPs *independently* from
   `cip_info.csv`. The two sets overlap freely. The solver *does* enforce
   NoOverlap (model_builder.py:909, CIP intervals included) — but only for
   orders *it* places; the current-state blocks are never given to it.
2. **No Line/SKU capability check.** manprg says P12 runs 280612/280611/280614
   and P22 runs 251200; `capabilities_rates.csv` says those lines can't run
   them (4 conflicts on today's data). Nothing flags it. manprg is ground
   truth (the plant physically ran them) → the table is wrong, not the plant.
3. **No holding-area flow for demand the solver can't place.** Under-target
   orders just sit in the adherence table as UNDER with no path to manual
   placement, and no way to turn a demand row into a draggable block.

## 2. Goals (user decisions, 2026-08-10)

- **The solver owns the current-state MOs.** It may split, reorder, or
  adjust the tonnage of today's MOs per its optimization — Carsten should
  not be hand-fixing blocks at this stage.
- **MO tonnage is adjustable (B)** but every change must be **shown in a
  change table** (line, MO, before/after qty, before/after window) so the
  changes can be written back to VIF.
- **CIPs are flexible within their frequency window** (MaxHoursBetweenCIP,
  hard compliance deadline) — production may move them; the solver places
  them optimally inside that window; production blocks split around them
  (CIP never splits).
- **Capability check: flag + one-click fix (B).** On startup (Home + Data)
  show the conflicting SKU/line pairs; offer an "add to capabilities" button
  that appends the manprg-proven combos (rate from manprg kg/cases if
  available, else manual entry). No silent writes.
- **Holding area after a solve:** every demand-plan order that is
  *under-qmin* OR has *zero scheduled qty* is auto-placed into holding.
- **Click-a-SKU button** in the adherence/demand table creates a production
  block at the order's qmin (at that line's rate) and drops it into holding.

## 3. Architecture

### 3.1 Solver input: current-state MOs as orders (new: `current_mo.csv`)

Today `_prepare_work_dir` copies reference files; `_overlay_current_state`
writes `initial_states.csv`. Add a **new work-dir input file
`current_mo.csv`** produced by the overlay:

```
mo, line_name, sku, remaining_kg, due_start_h, due_end_h, locked_line, source
29901, P09, 570468, 15200, 3.8, ..., 1, manprg
```

- One row per *running* and *queued* manprg production MO (CIP/TRIALS rows
  are not MOs — CIP comes from the CIP model; trials stay `trials.csv`).
- `locked_line = 1`: the MO can only run on its manprg line (already in
  VIF, cannot shift).
- `remaining_kg`: for running MOs = fct qty − made qty (cases × kg/case);
  for queued = fct qty.
- **Tonnage adjustable**: the solver treats `remaining_kg` as `qty_max`
  with a floor of 0 under `relax_demand`, and is *rewarded* for meeting it
  (objective term), so it can trim/fill toward demand targets.
- The solver may split an MO into multiple blocks around CIPs (existing
  seg_a/seg_b machinery already supports splits; NoOverlap already includes
  CIP intervals — this is the *fix* that makes the current-state blocks go
  through the same machinery instead of being pre-placed).

### 3.2 Solver change table (`mo_changes.csv` in the work dir)

The solver writes, per MO:

```
mo, line_name, sku, orig_qty_kg, new_qty_kg, orig_start_h, new_start_h,
orig_end_h, new_end_h, split_count, reason
```

Compare planned (post-solve) MO blocks vs the manprg baseline (pre-solve)
from `current_mo.csv`. Rendered as a Streamlit table on the Generate +
Calendar pages; a "Copy for VIF" / CSV-download button is in scope.

### 3.3 CIP handling

- No new CIP model changes needed: `model_builder` already has hard
  MaxHoursBetweenCIP deadlines + optional CIP intervals inside NoOverlap.
- The fix is (a) feeding current-state MOs as orders (3.1) so the CIP
  NoOverlap applies to them, and (b) making `current_state.py` stop
  emitting pre-placed CIP blocks that overlap production **for display**
  (it currently does; the calendar should render CIP *windows* from the
  solver/overlay, not independent projections).

### 3.4 Line/SKU capability check (pure module: `helpers/capability_check.py`)

- Input: manprg parsed rows + `capabilities_rates.csv`.
- Output: list of `{sku, line, mo, kind}` where kind ∈
  {SKU_MISSING (no row for sku at all), LINE_NOT_CAPABLE (sku exists but
  capable=0 for this line)}.
- Rule (user): *if it's in manprg, it's capable* → the fix button appends
  `capable=1` rows (or flips the existing row) using a rate derived from
  manprg kg/hours when derivable, else `calc_rate_kgph=0` → Data page shows
  a rate input for those before save.
- Wired into Home Command Center health table (new status: "capability
  conflicts: 4") and Data page (button).

### 3.5 Holding area: auto-populate + click-to-holding

- After a solve, compute per demand order: scheduled qty vs qmin.
  Under-qmin or zero → holding block (`{order_id, sku, qty_kg, run_hours,
  line: null}`) appended to `st.session_state.cal_holding` (existing
  mechanism — Gantt already renders and drags holding items; holding is
  already excluded from Save).
- Click-a-SKU: in the frontend AdherenceTable add a small "+" button per
  row → emits `addToHolding(order_id, sku, qty_min)` → Streamlit callback
  creates the block at qmin/line-rate and pushes to holding. Frontend
  change is small (button + event); Streamlit side builds the block with the
  existing payload schema.

### 3.6 Demand table source

The adherence table already exists (AdherenceTable.tsx, rows computed in
GanttSandbox). "SKU Adherence (live)" is the same component. No new table;
just the + button and the holding wiring.

## 4. Files touched (estimate)

| Area | Files |
|---|---|
| Solver input | `code/solver/data_loader.py` (read current_mo.csv), `code/solver/model_builder.py` (locked-line + adjustable-qty order support; change-table output) |
| Work-dir build | `code/helpers/scenario_runner.py` (`_prepare_work_dir`/`_overlay_current_state` write current_mo.csv + mo_changes.csv) |
| Current-state display | `code/helpers/current_state.py` (stop overlapping CIP projection for display; keep line_free_h gates) |
| Capability check | `code/helpers/capability_check.py` (new), `code/pages/home.py`, `code/pages/data.py` (one-click fix + rate entry) |
| Holding | `code/pages/calendar.py` (auto-populate from solve, click-to-holding callback), `code/components/gantt/frontend/src/components/AdherenceTable.tsx` (+ button), `code/components/gantt/frontend/src/GanttSandbox.tsx` (event), rebuilt dist |
| Change table | `code/pages/generate.py` + `code/pages/calendar.py` (render mo_changes.csv + download) |
| Tests | `tests/test_current_mo_input.py`, `tests/test_capability_check.py`, `tests/test_holding_flow.py` (pure-logic parts); extend `tests/test_data_health.py` |

## 5. Behavior details

- The calendar page: "Reload from disk" now means "re-run the solver on the
  current state" (or a separate "Re-optimize current state" button — see
  open questions).
- Locked semantics: *running* MOs stay `locked=True` (they are physically
  in progress); *queued* MOs become solver orders (unlocked by the solver,
  re-placed). Carsten still cannot drag a running MO.
- mo_changes.csv `reason` values: `tonnage_trim`, `tonnage_fill`, `split`,
  `reordered`, `unmoved`.
- Capability fix button: dry-run preview first (checkbox list), then apply;
  writes `capabilities_rates.csv` via existing safe_io backup.

## 6. Open questions — RESOLVED 2026-08-10

1. **Re-optimize UX: FOLD into Generate as scenario E** ("Current state +
   demand"). Calendar stays read-only until Save.
2. **`mo_changes.csv` destination: work-dir per scenario**; on Save it is
   promoted to `data/mo_changes/latest.csv`. ✓ (my rec, accepted)
3. **VIF write-back: change table + CSV download for now.** A VIF import
   format exists but is not available yet — design the CSV so it can be
   repurposed later.
4. **Capability-fix default rate: average rate of ALL SKUs on that line**
   (from capabilities_rates.csv), with manual entry option.

## 6b. Click-a-SKU block sizing (RESOLVED 2026-08-10)

Block duration = **missing tonnage to finish the order** ÷ average line rate
for that SKU:

    missing_kg = qty_target − scheduled_qty       (0 if already met)
    block_hours = min(missing_kg / avg_rate_kgph, 24)

- `avg_rate_kgph` = mean of `calc_rate_kgph` across the SKU's capable lines.
- **Capped at 24h** — an order missing more than 24h of work still produces
  one 24h block; Carsten can add more via the + button again.
- Block qty = block_hours × avg_rate_kgph, `line: null`, dropped into holding.

## 7. Verification (per flowstate-app-qa skill)

1. pytest suite green (existing 77 + new tests).
2. Real solve on fresh fixtures: solver takes current_mo.csv, produces
   schedule + mo_changes.csv; **zero CIP↔production overlaps** in output
   (assert via validator/script).
3. Capability check reports exactly the 4 known conflicts; one-click fix
   updates the table; re-run shows 0.
4. Browser QA: calendar renders fresh current state with no overlapping CIP
   windows; adherence table "+" creates a holding block; under-qmin orders
   auto-land in holding after a solve; change table renders with correct
   before/after.

## 8. Out of scope (this pass)

- Auto-writing changes to VIF (needs VIF import format — see Q3).
- Changing MaxHoursBetweenCIP values in the model (they are hard compliance
  constants from cip_info).
