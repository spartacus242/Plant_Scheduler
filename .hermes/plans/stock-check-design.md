# Stock Check — Component Availability & At-Risk SKU Monitor

**Branch:** `feature/stock-check` · **Status:** design approved-in-parts, pre-implementation
**Source workflows replaced:** `Stock Check WWxx 1st Draft.xlsm` (manual, weekly) + inbound side of `Shipping Receiving Schedule NPA.xlsm`

---

## 1. Purpose

Flowstate gains a **daily-refreshed component availability monitor** that answers two questions for the planner:

1. **Schedule view** — "Is anything on the *current board* about to fail for lack of components?"
2. **Demand view** — "Which SKUs in the demand plan *cannot reach ≥90% of target* with available stock — i.e. should not be scheduled?"

Per-SKU drill-down shows exactly which purchased component constrains it, how much is on hand, how much is usable, what coverage that gives, and what inbound appointments exist.

This replaces the manual Excel stock check (hand-typed "Negative Qty" from a separate tool, hand-typed PO blocks, VLOOKUP risk formula) and eliminates its known blind spot: the Excel sums **all** stock statuses, so QC-held ("Loc") stock silently counts as available — the workbook notes are full of manual "36,960 kg in QC status" corrections.

## 2. Data sources

### 2.1 VIF CSV exports (primary, daily-refreshed)

Configurable folder, default `\\usnpa-appfs\DATA\vif-export\auto editions\` on Carsten's machine; local folder for dev. All `;`-delimited, codepage 1252, except `rmpkitems.csv` (comma, 7 junk preamble rows).

| File | Content | Key columns |
|---|---|---|
| `ediact 3.csv` | Master multi-level BOM (14.6k rows) | PF, Activity, flow-type, Effective date, Family (`FG`/`R`/`SU`/`POU`), Item type (`Output`/`Input`/`of cost`), Item, Designation, Quantity (ACT), Unit |
| `ediact 4.csv` | Red-fruit/HSM slurry BOM + Freinte (scrap) | same shape; FG-description fallback |
| `jestkexp.csv` | Raw-material inventory, lot-level (13.8k rows) | Item, SCN, Status (`Ava`/`Loc`/`Out`), BBD, Batch, Qty (MU) [Kg], Depot, Location |
| `jestkexp2.csv` | Packaging inventory, lot-level (4.4k rows) | Item, Batch, Status, Qty (MU) [EA / M2 / Kg], Depot, Location |
| `azapart.csv` | FG master data | sku, designation, CAS, kg-per-case (col 7), format string `48x90G` (col 8) |
| `rmpkitems.csv` | Item→designation catalog | Item, Designation — **stale**: 615 of 1,114 BOM components missing; designations must fall back to BOM/stock rows |

Verified facts that shape the design:
- All 211 Flowstate SKUs exist in azapart. 193 have BOMs; **18 have none** (280464–280481 etc.) → these get a `NO_BOM` state, never silently pass.
- Inventory statuses observed: `Ava` (11,590 RM rows), `Loc` (2,242 — QC/warehouse-held), `Out` (13). Depots: RM = M01/SB1/SC1/SF1/M02; PKG = SFG/M21.
- The warehouse↔plant transfer file is appointment-only (see 2.2).

### 2.2 Shipping/Receiving schedule (weekly appointment feed)

One tab per week; inbound rows marked `RECEIVING` in the Dest column. Fields: PO# (in Chrono column), category label (CAPS/GPI/AMCOR/CHEP/"SL3 transefer"), day, time. **No item numbers, no quantities.** Imported as *arrival appointments* only. Parsing must tolerate: `#REF!` cells, text dates, typo'd labels, mixed time formats ("9AM"/"11am"). Decay is expected — parser is best-effort with row-level error reporting, never fatal.

### 2.3 Future: VIF PO report

Stub slot in the data model now (`inbound_po_lines`: PO#, item, qty, due date, confirmed date). The `Comparaison` tab of the shipping file shows the VIF receipts-export column family (order date, order number, item, qty, receipt date, batch, arrival area) — use it as the column-mapping template when the real report lands.

## 3. BOM explosion engine

### 3.1 Structure (verified against ediact 3)

Four-level recipe tree per FG, e.g. `280351` (24×90G Kirkland):

```
FG activity 280351        OUT 2640 CAS      ← packaging inputs per 2640 CAS (1 CAS granularity)
 └ IN  TL760116  2640 SLV
     SU activity TL760116 OUT 1000 SLV      ← 1000 EA sleeve blanks (754751)
      └ IN  VP762164  24000 POU             ← 24 pouches/sleeve ✓ (format string)
          POU activity VP762164 OUT 1000 POU ← film 22.88 M2, cap 1000 EA
           ├ IN  SPTP764029  45 Kg          ┐ blend: ALL listed slurry activities
           └ IN  SPTP764066  45 Kg          ┘ are consumed simultaneously
               R activity SPTP764066 OUT 1000 Kg ← 730009 432 Kg, 750036 90 Kg, BT002 144.7 Kg …
```

Normalization is **per activity**: inputs scale as `input_qty / output_qty` of that activity; requirements chain down by multiplication. Output units: FG=CAS, SU=SLV, POU=POU, R=Kg.

### 3.2 Alternate components (blank Quantity (ACT))

Per user: a blank-qty Input row is an **acceptable replacement** for a same-activity sibling that has a quantity (e.g. drummed organic applesauce `730009-A` as alternate to in-house `BT002`). Rules:

- Blank-qty inputs attach to their activity as **alternates**; the required quantity of the *primary* is the planning quantity.
- Coverage counts available stock of primary **+ all alternates** toward the need (each contributing its own on-hand usable qty).
- Alternate groups display as one requirement line with expandable members.
- `xxxSCN` self-referencing rows (qty-less, item ≈ activity code) are VIF stock-keeping artifacts → dropped.

### 3.3 Cycle guard & UNK bucket

- Guard: activity stack per path; a repeated activity terminates the branch and logs `CYCLE`.
- If a quantity is genuinely unresolvable (blank primary with no qty-bearing sibling), the item lands in a per-SKU **UNK bucket**: "requirement exists, magnitude unknown" — shown as a data-quality flag, never treated as zero need.
- `of cost` rows are cost-accounting lines → excluded from material requirements.

### 3.4 Multi-BOM variants & effective dates

Same PF/activity can carry multiple effective-date rows (e.g. `01/01/2026` vs `26/06/2026`). Resolution: **latest effective date ≤ planning week start** wins per (PF, activity, item). PF variants (`280351` vs `280351-L` vs `280351-DS`): use the exact SKU's PF; suffix variants are distinct sellable formats already separate in the demand plan.

## 4. Requirements, coverage, risk scoring

### 4.1 Two demand signals

| Signal | Source | Question answered |
|---|---|---|
| **Schedule need** | `calendar_blocks.csv` production blocks (qty_kg → CAS via azapart kg/case) per week | Will *this board* fail? → block-level flag |
| **Demand need** | `demand_plan.csv` `qty_target` per SKU/week | Can this SKU reach ≥90% of target? → `DO_NOT_SCHEDULE` flag |

Units stay **per item** (CAS, EA, M2, Kg, L, FT — labeled, never force-converted).

### 4.2 Available stock

`available(item) = Σ qty over lots matching active depot×status toggles`.
UI exposes toggles for every observed combination (statuses Ava/Loc/Out × depots M01/SB1/SC1/SF1/M02/SFG/M21). **Default: `Ava` only, all depots** — replicates the safe interpretation; `Loc` opt-in per depot (e.g. SB1 Loc = QC hold, SL3-adjacent stock the planner knows will release).

**In-house ingredients never gate.** `BT001`/`BT002` (conventional / organic fresh apple puree) are made on the preprocessing lines on demand and never appear in the VIF inventory exports. They are always treated as fully available (`coverage.py: IN_HOUSE_ITEMS`), render with an "in-house" note, and never produce AT_RISK. The list is extensible if more in-house items surface.

Lot-level detail retained for drill-down (SCN, batch, BBD) — BBD expiry-vs-run-date is a future enhancement, noted not built.

### 4.3 Coverage & status

Per SKU (for each demand signal):

```
need(item)           = exploded requirement for the week
coverage_ratio(item) = available(item) / need(item)        (∞ if need = 0)
constraining_items   = items with coverage_ratio < 1 sorted ascending
sku_coverage         = min over items of coverage_ratio    (bottleneck)
```

Per item, coverage horizon: `available / burn_rate` where burn comes from scheduled blocks → expressed as **"runs out during block #N on line P09, Wed 14:00"** for the schedule view; as plain ratio for the demand view.

**Item/SKU status (per signal):**

| Status | Condition |
|---|---|
| `OK` | coverage ≥ 1.0 with margin ≥ 10% |
| `TIGHT` | 0.95 ≤ coverage < 1.10 boundary zone — planner judgment |
| `AT_RISK` | coverage < 0.95 (mirrors the Excel's 95% threshold) |
| `DO_NOT_SCHEDULE` | demand view only: sku_coverage < 0.90 of qty_target |
| `NO_BOM` | SKU has no ediact 3 recipe (18 SKUs) |
| `UNK` | unresolved quantities somewhere in its tree |

Appointment context (from 2.2): if an at-risk item's PO# appears in the receiving feed (matched by the planner's PO→item annotation, or later by the PO report), show `PO 30042199 expected Tue 8:00` inline. Appointments never change coverage math in v1 — contents unverified.

## 5. UI — new "Stock Check" page

1. **Header:** data-folder path (settings), last-refresh timestamp per source file, week selector.
2. **Availability toggles:** depot × status matrix (persisted per user).
3. **Schedule-view table:** one row per scheduled block at risk → SKU, line, block time, constraining item(s), need vs available, appointment hints.
4. **Demand-view table:** one row per demand SKU → coverage %, status chip, constraining item; sort puts `DO_NOT_SCHEDULE` on top.
5. **Item drill-down:** SKU → full exploded tree → per component: need, on-hand by status, lot table (SCN/batch/BBD/qty/depot), alternates group, appointment markers.
6. **Item-centric reverse view:** pick a component (e.g. `730009`) → every scheduled/demand SKU consuming it with each one's share — this is the Excel "Table" sheet's actual job, done properly.
7. **Refresh:** manual button + auto-refresh on page load if any source file mtime changed since last import (the "daily check" Carsten wants, without a daemon).

## 6. Architecture

```
code/
  stockcheck/
    vif_import.py      # 5 CSV parsers (encodings, delimiters, rmpkitems preamble)
    receiving_import.py# weekly-tab xlsm appointment parser (tolerant)
    bom.py             # activity graph, alternates, cycle guard, effective dates
    explode.py         # requirements per SKU per signal (schedule / demand)
    coverage.py        # availability toggles, coverage ratios, statuses
    models.py          # dataclasses + JSON persistence under data/stockcheck/
pages/
  stock_check.py       # Streamlit page (sections per §5)
data/
  stockcheck/          # imported snapshots + toggle settings + run cache
tests/
  test_stockcheck_*.py # golden-file tests from the WW33 workbook values
```

- Importers produce **versioned snapshots** (timestamped) so a bad VIF export never corrupts the last good state; the page reads the latest good snapshot.
- The engine is pure Python/pandas, independent of Streamlit → unit-testable; page is a thin renderer.
- Nothing touches the solver. This feature reads `calendar_blocks.csv` / `demand_plan.csv` but never writes them. `DO_NOT_SCHEDULE` is advisory in v1.
- Settings (folder path, toggles) persist in `data/stockcheck/settings.json`.

## 7. Verification plan

Golden-value tests from the WW33 workbook (values read out of the real file during analysis):

- `730009` on-hand Ava-only total must reconcile to Excel E4's RM SUMIF **minus** its Loc rows (Excel counted all statuses; we must reproduce both numbers to prove the toggle works).
- `280351` explosion: TL760116 = 2640 SLV per 2640 CAS; VP762164 = 24,000 POU per 1000 SLV; 730009 total per CAS = 2640/1000 × 24000/1000 × (45+45)/1000 × {454,432}/1000 blended path — assert exact rational arithmetic per path.
- Receiving parser: WW33 tab must yield exactly the 11 appointments observed (incl. the "SL3 transefer" typo row and PO 30042199).
- 18 no-BOM SKUs all report `NO_BOM`.
- App QA per `flowstate-app-qa` skill: browser-verify the page against the dev dataset before any merge to main. Director independently re-verifies.

## 8. Explicit non-goals (v1)

- No automatic rescheduling / solver coupling.
- No BBD expiry-risk scoring (lot data retained for it later).
- No PO line-item import until the real VIF PO report exists (stub only).
- No warehouse-format guessing beyond the appointment feed (their inbound format remains unknown by design).
- No historical analytics / missed-APT tracking (the shipping file's own broken consolidation stays in Excel).
