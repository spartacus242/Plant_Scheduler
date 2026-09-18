# The plant data drop — `fs_data` (VIF / Sage X3 exports + people-maintained files)

Since 2026-09-15 the plant delivers its data as one drop with two folders.
This page is the structure reference: every file in the drop, what it is,
how it is encoded, what Flowstate does with it, and which files are
deliberately ignored. Row counts are from the drop of 2026-09-15.

Code: `code/stockcheck/vif_import.py` (loaders, `VifSnapshot`, `lot_frames`,
`receipts_frame`, `missing_semi_recipes`), `code/stockcheck/coverage.py` (on-hand toggles),
`code/stockcheck/timeline.py` (receipt slips → landed POs),
`code/stockcheck/x3_po_export.py` (PO lines, see `docs/order_npa_export.md`),
`scripts/azap_demand_summary.py` (AZAP workbook → `demand_plan_summary.csv`).
Bridge: `scripts/fs-live-data.conf.json` (file list, `source_dirs_work`),
`scripts/fs-live-pull.py` / `install_flowstate.ps1 -FeedDir` (planner's PC),
`scripts/fs-live-push.py` (work PC → GitHub → dev laptop).

## Layout

```
fs_data\
├── fs_vif\      the ERP's own exports (VIF / Sage X3) — written by the ERP, read by everyone
└── fs_manual\   files people maintain — cip_info.csv, the AZAP workbook, the dock schedule
```

| Folder | Who writes it | What the sync does with it |
|---|---|---|
| `fs_vif` | the ERP, every evening (~19:10, landing by ~19:25) | read only — no lock, marker, rename or delete |
| `fs_manual` | planners (the AZAP workbook is replaced weekly, `cip_info.csv` by hand) | read, **plus one write**: the sync rebuilds `demand_plan_summary.csv` here from the newest AZAP workbook (see *AZAP demand recipe*) |

A planner's PC feeds both folders to the app with
`install_flowstate.ps1 -FeedDir "…\fs_data"` — the root, one value (folder
mode, `docs/live-data-bridge-setup.md` PART 6). Since 2026-09-17 a
configured folder that holds `fs_vif` and/or `fs_manual` stands for those
subfolders at every pass (`fs_vif` first); the root's own files are ignored
once a subfolder exists, and a subfolder created later is picked up without
a re-install. The explicit list
`-FeedDir "…\fs_data\fs_vif;…\fs_data\fs_manual"` still works and gives the
same result; a flat folder holding the files themselves is read as it is.
The work PC lists the same drop under `"source_dirs_work"` in
`fs-live-data.conf.json` (the root, or the two subfolders) and
`fs-live-push.py` pushes it to the private live-data repo for the dev laptop.
The dev laptop rebuilds the drop from that repo (2026-09-18, the *drop
mirror*): `fs-live-pull.py` drops the clone's files into its own local
`fs_data` by the conf's `drop_layout` (ERP exports → `fs_vif`, people-maintained
files → `fs_manual`; a file's time is when its content first appeared in the
repo, and it lands only when that is newer than the file there; nothing
deleted) and then runs folder mode from that root, so the dev PC and a
planner's PC read the plant data the same way (bridge doc PART 5b).
A file present in both folders is taken from wherever it is newest (equal
modified times: `fs_vif`). Only the names on the conf `files` list travel;
everything else in the folders is ignored — and a reachable folder holding
none of them is reported as `no plant files found` instead of a quiet
"nothing new".

## Conventions shared by the VIF exports (`fs_vif`)

- Delimiter `;`, encoding Windows-1252 (`cp1252`), CRLF line endings —
  except `jestkexp5.csv` (pipe `|`) and `order_npa.csv` (pipe, LF).
- Every `jestk*` and `ediact` report **ends with a footer row `Page1/1 `**
  that the loaders drop. An **empty report** is 134 bytes: blank lines,
  `Recall of the selection`, then `No data corresponds to your selection.
  Page1/1` — it parses to an empty frame silently and the snapshot notes
  "empty export" (`VifSnapshot.notes`; read it with
  `getattr(snap, "notes", [])`, older pickled snapshots lack the field).
- Dates are `MM/DD/YYYY`. The lot loaders read `BBD` month-first (since
  2026-09-16), so a column of only ambiguous dates (`09/02/2026`) is
  2 September; the per-column day-first/month-first auto-detect is used only
  when month-first leaves values unparsed (the dev fixture's day-first
  `16/04/2027`). `ediact` effective dates keep the auto-detect (day-first on
  the real export). Quantities use `.` decimals; `ediact.csv` puts a
  thousands comma in larger quantities (`1,600`); `jestkexp5.csv` carries
  four decimals (`2160.0000`).
- Item codes are strings everywhere (`735009-A`, `GSSPTP9221SCN`) — never
  numeric.
- Lot files share one normalized frame shape after import: `item,
  designation, scn, status, statut, bbd` (datetime64), `batch, qty` (float,
  blank → 0.0), `unit, depot, location, precaut, supplier_batch, source`
  (the file name); `jestksav.csv` adds `variety, supplier_no,
  supplier_name, apple_receipt`; `jestkexp5.csv` adds `family`.

## Required and optional exports (import errors vs notes)

`import_vif_folder` reports two kinds of remark. **Errors**
(`VifSnapshot.errors`, `"<file>: <why>"`) drive the Stock Check page's
*import error(s)* chip and warning; **notes** (`VifSnapshot.notes`) are
informational.

| Case | Where it goes | Text |
|---|---|---|
| A **required** export is missing (2026-09-16): a BOM (`ediact.csv` or `ediact 3.csv`), `jestkexp.csv` (or its alias `jestkexp4.csv`), `jestkexp2.csv`, `azapart.csv` — `REQUIRED_FILES` | error | `jestkexp2.csv: missing from <folder>` (the BOM is reported as `ediact.csv`) |
| An **optional** export is missing: `jestkamb.csv`, `jestkexq.csv`, `jestksav.csv`, `jestkexp5.csv`, `PKG-REC.csv`, `ediact 4.csv`, `rmpkitems.csv` | nothing | — the frame is simply absent |
| A present file fails to parse or cannot be read (locked by Excel, a OneDrive hiccup) | error (no mtime recorded, so the refresh gate retries it next call) — also when `ediact 3.csv` stood in for an unreadable `ediact.csv` (2026-09-16) | `<file>: <exception>`; `ediact.csv: <exception> (BOM taken from ediact 3.csv)` |
| An empty export, dropped duplicate lines, an ignored alias / generation / stale file, the BOM fallback past an **empty** `ediact.csv`, missing semi-finished recipes, a lot listed by two exports | note | see the sections below |

The pre-drop loader reported a missing file as `[Errno 2] …`; the first
drop loader had gone silent, so an export the ERP stopped writing hid behind
a report that looked normal. The dev fixture `data/stockcheck/dev_vif`
carries every required file and still imports with no errors and no notes.

## Schedule

| File | What it is | Format | Rows (2026-09-15) | Key columns | Flowstate use |
|---|---|---|---|---|---|
| `manprg.txt` | The plant schedule (manufacturing orders) for lines P09–P18 | `;` cp1252 CRLF; header row; no footer | 33 (+ header) | `Start date;Start time;Line;MO No.;Item;Designation;Pal;Type;Hours;Fct qty (Cas);Qty made (Cas);Fct qty [Kg];;Qty made [Kg];;Left (Cas)` | The plant state: committed blocks, fixed line-time in Scenario F, made kg for demand netting. The file's write time is the as-of stamp (`manprg.asof.json`). Unchanged by the drop |
| `manprg2.txt` | The same for lines P19–P22 | same | 19 (+ header) | same | same |

## Bill of materials

| File | What it is | Format | Rows (2026-09-15) | Key columns | Flowstate use |
|---|---|---|---|---|---|
| `ediact.csv` **(new)** | Every production activity and its inputs/outputs for every finished product (PF). It does **not** carry the semi-finished (HSM/RF) recipes: 24 of the 27 `ediact 4.csv` activities are absent, and 21 of those codes appear only as `Input` items with no activity (verified 2026-09-16; a 22nd recipe-less input, `RF750005`, never had a recipe in any export) — so it replaces `ediact 3.csv` but **not** `ediact 4.csv` | `;` cp1252 CRLF; **no header**; footer `Page1/1 ` | 24,534 data rows + footer; **8,443 exact duplicate lines** (multiplicity 2–4) | 10 columns: `PF;Activity;Effective date;Famille;Item type;Item;Designation;Qty;Unit;Freinte`. Families `FG/R/SU/POU`; item types `Output/Input/of cost`; 761 activities, 391 PFs | `frames["ediact 3.csv"]` — the BOM frame keeps its historical key whichever file it came from (`EDIACT_COLS` + `freinte`; `flow_type` is `""`). `BomGraph` groups by activity and keeps the latest effective-date row per (item type, item), so repeats never double-count |
| `ediact 3.csv` (old) | The pre-drop BOM export | `;` cp1252 CRLF; **header row** (`PF;Activity;Type de flux;Effective date;…`); footer | — (not in the drop; `data/stockcheck/dev_vif/` fixture) | 10 columns with the flow-type column at index 2 (always `Pull`) | Same frame key; the fallback when `ediact.csv` is missing, empty or unreadable (see *Which BOM file is used*). `find_bom_file(folder)` returns the first of `BOM_FILES = ("ediact.csv", "ediact 3.csv")` that **exists** (a presence check only) |
| `ediact 4.csv` **(still needed)** | The semi-finished (HSM/RF) recipes: `Activity` is the HSM/RF code, `Input` rows its components per batch, the `Output` row the batch basis (e.g. 1,000 kg of slurry). Old layout (no flow-type column) | `;` cp1252; header | — (**not in the drop**; `data/reference` on the planner PC keeps a 2026-08-19 copy, 27 activities) | as `ediact.csv` | The recipe **supplement**: `frames["ediact 4.csv"]` whenever the file exists, beside either BOM export; merged into the `BomGraph` so a semi-finished input explodes to its sub-components (user rule 2026-08-14: a house-made intermediate never gates a SKU on its own stock) |

Layout detection: a first cell `PF` means the old headed layout; a headerless
file whose third column looks like a date is the new layout.

**Which BOM file is used (2026-09-16).** The loader walks `BOM_FILES` in
order and takes the first file that loads **and has rows**. An empty
`ediact.csv` (the 134-byte "No data corresponds" report) or one that fails to
parse no longer hides a good `ediact 3.csv`: the BOM comes from
`ediact 3.csv`. An empty `ediact.csv` gets a note,
`ediact.csv: empty export - BOM taken from ediact 3.csv`, and its mtime is
recorded. An unreadable one stays an **error**,
`ediact.csv: <error> (BOM taken from ediact 3.csv)`, with no mtime: the
refresh gate (`api.refresh_vif_snapshot`) retries only
the files the errors name, so once Excel releases the lock the next report
re-imports and takes the fresher `ediact.csv` BOM. Before this was settled
(2026-09-16) the failure was a stamped note, and the older `ediact 3.csv`
BOM stayed in the saved snapshot until the next export changed
`ediact.csv`'s mtime. The other generation is noted `ignored (ediact.csv is
the BOM export)` only when `ediact.csv` itself supplied the frame. When no
BOM file has rows, the first readable one is kept (an empty frame, noted
`empty export`) and any file that failed to parse is an error.

**Missing semi-finished recipes (2026-09-16).** With `ediact.csv` alone the
BOM cannot explode through the semi-finished inputs: on the 2026-09-15 drop
schedule blocks OK went 150 → 121, AT_RISK 13 → 40 and demand
DO_NOT_SCHEDULE 57 → 107; adding `ediact 4.csv` restores the old report. The
import says so instead of degrading silently: `missing_semi_recipes(frames)`
lists the `Input` items of the BOM (and of `ediact 4.csv`) that carry a
quantity, have no `Activity` in either file and start with `HSM` or `RF`
(case-insensitive). A non-empty list becomes **one note** —
`semi-finished recipes missing: 17 - HSM750216, HSM764059, … +9 more
(ediact 4.csv not in the folder)` (the hint only when that file is absent) —
and `VifSnapshot.missing_recipes` holds the full list (`[]` on snapshots
pickled before the field existed). It is a note, not an error: the refresh
gate re-imports errored files on every call. Blank-quantity `Input` rows are
alternates `BomGraph` never explodes, so they are not counted — `RF750005`
(RF ORG BLACKCURRANT IQF) is such an alternate with no recipe in any export
and would otherwise pin a note nothing can clear. On the real drop:
`ediact.csv` alone → 17 codes; with the `data/reference` `ediact 4.csv` → none.

## Item masters

| File | What it is | Format | Rows (2026-09-15) | Key columns | Flowstate use |
|---|---|---|---|---|---|
| `azapart.csv` | Finished-product master with pack conversions | `;` cp1252 CRLF; **no header**; no footer; fixed-width padded cells | 540 | `Column1..Column8`: item (padded), designation, stock unit (`CAS`), weight unit, `1 CAS = 4.320 Kg`, container/pallet text, kg per case, AZAP product group | `frames["azapart.csv"]`; case ↔ kg conversion. Byte-identical to what `data/reference` already held |
| `rmpkitems.csv` | Raw-material and packaging item list (families PK1, PK3, RM1) | `,` cp1252 CRLF; **7 preamble rows** (2 blank, `Recall of the selection`, the selection line, ` , , , `, 2 blank) then header `Item,Designation`; no footer | 2,010 (+ header) | `Item,Designation` | `frames["rmpkitems.csv"]`; the tracked-item universe and designations. Unchanged |

## Stock lots (on-hand)

All lot files are `;` cp1252 CRLF with a header row and the `Page1/1 `
footer, except `jestkexp5.csv` (pipe, no footer). `lot_frames(snap)` returns
the present ones in the order rm, pkg, amb, qc, apples, semi.

| File | What it is | Layout | Rows (2026-09-15) | Depots / statuses | Flowstate use |
|---|---|---|---|---|---|
| `jestkexp.csv` | Raw-material lots | 13 cols: `Item;Designation;SCN;Status;Statut;BBD;BATCH;Qty (MU);Unit;Depot;Location;PreCaut;SupplierBatch` | 18,703 | `SB1` 17,575 · `SC1` 574 · `SF1` 279 · `M01` 202 · `M02` 73; `Ava/Loc/Out` | `frames["jestkexp.csv"]`, lot frame **rm**. Toggle group *Raw materials*: `Ava` on, `Loc`/`Out` off. Batch-dated lots near a PO's receipt date back the timeline's *landed* rule |
| `jestkexp2.csv` | Packaging lots | 12 cols, Batch/BBD before Status: `Item;Designation;SCN;Batch;BBD;Status;Statut;Qty (MU);Unit;Depot;Location;PreCaut` | 3,876 | `SFG` 3,773 · `M21` 103; `Ava/Loc/Out` | `frames["jestkexp2.csv"]`, lot frame **pkg**. Group *Packaging*: `Ava` on |
| `jestkamb.csv` **(new)** | Lots at `AMB` = Americold Burley, the off-site ambient store | 13 cols as `jestkexp.csv`; `Location` carries the receiving PO number on the sample | 1,864 | `AMB`; `Loc` 1,571 · `Ava` 293; 18 of the 23 items are BOM inputs | `frames["jestkamb.csv"]`, lot frame **amb**. Group *Off-site AMB*: `Ava` on, `Loc` off |
| `jestkexq.csv` **(new)** | Lots in the quality-control depots | 12 cols as `jestkexp2.csv` | 160 | `QCR` 126 · `QCP` 34; `Ava` 103 · `Loc` 40 · `Out` 17; 50 of the 54 items are BOM inputs | `frames["jestkexq.csv"]`, lot frame **qc**. Group *Quality control QCR-QCP*: **every status off** by default — stock under QC hold is not available until released |
| `jestksav.csv` **(new)** | Fresh-apple lots at the apple depots | 17 cols = the `jestkexp` 13 + `Variety;Supplier Number;Supplier Name;RAW APPLE RECEIVING #`; `BBD` blank | 2,261 | `SA1` 1,266 · `SA2` 995; all `Ava` | `frames["jestksav.csv"]`, lot frame **apples** (+ `variety, supplier_no, supplier_name, apple_receipt`). Group *Apples SA1-SA2*: `Ava` on |
| `jestksa.csv` | The same 2,261 rows **without** the last four columns | 13 cols | 2,261 | as above | Redundant — read only as a **fallback when `jestksav.csv` is missing** (then stamped under both names, see *Ignored files*); not on the bridge list |
| `jestkexp5.csv` **(new)** | Semi-finished / work-in-progress lots (`GSSPTP9221SCN`, `HSM764059`, `RF730060` … all BOM items) | **pipe-delimited**, header `Item\|SCN\|BATCH\|Quantity\|UOM\|Designation\|Batch status\|Family\|Depot\|Location\|BBD`, no footer; `Quantity` with 4 decimals | 38 (29 after the cross-file de-dup) | `M12` 20 · `QCR` 9 · `M01` 6 · `SC1` 3; `Batch status` `AVA` 19 · `OL` 19 | `frames["jestkexp5.csv"]`, lot frame **semi** (+ `family`). `AVA` → `Ava`, `OL` kept as is. Group *Semi-finished M12*: `Ava` on, `OL` off. Its 9 `QCR` rows are the **same physical lots** `jestkexq.csv` lists (status `Out` there, `OL` here): the loader keeps them in `jestkexq.csv` only and notes `jestkexp5.csv: dropped 9 lots already listed in jestkexq.csv` (see *De-duplication rules*) |

### On-hand availability — toggle defaults

`available_stock` counts a lot only when its `depot|status` key is a known,
switched-on toggle; **an unknown depot or status is excluded**, so a new
depot appearing in an export is invisible until it is added to a group. The
Stock Check page lists every depot under its group label.

| Group | Depots | `Ava` | `Loc` | `Out` | `OL` |
|---|---|---|---|---|---|
| Raw materials | `M01 SB1 SC1 SF1 M02` | **on** | off | off | – |
| Packaging | `SFG M21` | **on** | off | off | – |
| Off-site AMB | `AMB` | **on** | off | – | – |
| Apples SA1-SA2 | `SA1 SA2` | **on** | – | – | – |
| Semi-finished M12 | `M12` | **on** | – | – | off |
| Quality control QCR-QCP | `QCR QCP` | off | off | off | off |

## Receipts

| File | What it is | Format | Rows (2026-09-15) | Fields | Flowstate use |
|---|---|---|---|---|---|
| `PKG-REC.csv` **(new)** | Packaging **receipt slips** for a rolling window of about one week (`Receipt Date : 09/12/2026 -> 09/18/2026` on the sample) | `;` cp1252 CRLF. 2 blank lines, a 5-line preamble (`Recall of the selection` / `Prechrono :;Receipt slips;Management :;Actual` / `Receipt Date :;09/12/2026 -> 09/18/2026;Admin update in stock :;To update in stock` / `Item :;[Selection on pyramid];;` / ` ; ; ; `), 2 blank lines, then a header line `Receipt Date;Item;Description;Batch;Quantity;UOM;PO Number;Receipt Trans #;User` and the data rows; no footer. The loader keeps only rows whose first field is a date | 390 data rows (16 POs, 16 slips, receipt dates 09/12–09/15) | 9 fields: receipt date `MM/DD/YYYY`; item; designation; batch (`N62…`); qty; unit (`EA`/`Kg`); PO number (8 digits, `30043986`); receipt slip number (8 digits, `30119536`); user | `frames["PKG-REC.csv"]`; `receipts_frame(snap)` → `receipt_date` (datetime64), `item, designation, batch, qty` (float), `unit, po8, slip, user, source`. 338 of the 390 batches on the slips are already lots in `jestkexp2.csv` |

**Landed rule (timeline, 2026-09-15).** A PO line whose `(po8, item)` has
receipt slips dated on or before today totalling **at least the landed
fraction of its remaining qty** gets fate `landed`, with a reason naming the
slip number(s) and date. This check runs **before** the batch-dated lot
rule, and slip qty is consumed the way lots are consumed, so one slip never
lands two lines. Slips are stronger evidence than lots: a lot only proves
*something* of that item arrived, a slip names the PO.

## Purchase orders

| File | What it is | Format | Rows (2026-09-15) | Flowstate use |
|---|---|---|---|---|
| `order_npa.csv` | The ERP's PO-line export (Sage X3) | pipe, cp1252, **LF**, header, 219 columns, 13-digit fixed-point numbers with four implied decimals | 983 lines | The open-PO feed of the supply timeline — full reference in `docs/order_npa_export.md` |

## Ignored files (present in `fs_vif`, deliberately not used)

| File | Why | Rule |
|---|---|---|
| `jestkexp4.csv` | Byte-identical duplicate of `jestkexp.csv` (1,933,503 bytes both) | Ignored; **fallback alias only when `jestkexp.csv` is missing** |
| `jestksa.csv` | Subset of `jestksav.csv` (same 2,261 rows, four columns fewer) | Fallback only when `jestksav.csv` is missing |
| `jestkex2.csv` | A **stale 2025 packaging snapshot**: 13 cols (`jestkexp2` + a `RECDate` column that is always blank), batches `N51xxxxxx` (newest `N51990224`), 3,428 rows, **0** of the `PKG-REC` batches match | Ignored; the snapshot notes it |
| `jestkami.csv`, `jestkavg.csv`, `jestktgb.csv` | Empty reports (134 bytes: `No data corresponds to your selection. Page1/1`) | Parsed to an empty frame silently; note "empty export" |

When an alias stands in, its frame sits under the canonical key with the
alias name in `source`, the note reads `jestkexp.csv missing: loaded
jestkexp4.csv in its place`, and (since 2026-09-16) `source_files` carries the
alias file's mtime under **both** names — the report's per-frame export stamp
(`snapshot_stamp`, e.g. `rm`) reads the canonical key and was blank before.

## De-duplication rules

| Where | Rule | Why |
|---|---|---|
| `ediact.csv` | **Drop exact duplicate lines** (8,443 of 24,534; multiplicity 2–4) before building the BOM | The old `ediact 3.csv` never repeated a line; the new export does, and a repeated input line would double the component draw. `BomGraph` additionally keeps the latest effective-date row per (item type, item) within an activity |
| Lot files, across files (2026-09-16) | A row whose `(item, scn, batch, depot, location)` already appears in an **earlier** lot frame (order `jestkexp`, `jestkexp2`, `jestkamb`, `jestkexq`, `jestksav`, `jestkexp5`) is dropped from the later frame, with a note per frame: `jestkexp5.csv: dropped 9 lots already listed in jestkexq.csv`. Within-file repeats are left alone (none on the sample), and a row with neither `scn` nor `batch` never matches | On the 2026-09-15 drop the only overlap is the 9 `QCR` lots in both `jestkexq.csv` (`Out`) and `jestkexp5.csv` (`OL`); `jestkexp`, `jestkexp2`, `jestkamb` and `jestksav` share no key. **No stock number changed** (corrected 2026-09-16): `jestkexp5.csv` is never counted (`api._NEVER_COUNTED_FRAMES`) and `QCR`/`QCP` are off for every status by default, so available stock and tracked items are identical with or without the rule (checked on the real drop). The visible effect is **lot detail** on the Stock Check page, which listed each of those lots twice and now lists it once, as the `jestkexq.csv` `QCR` row; the `jestkexp5.csv` `OL` copy (same qty and BBD) is dropped on purpose. The rule also keeps a future overlap between the counted frames (`jestkexp`, `jestkexp2`, `jestkamb`, `jestkexq`, `jestksav`) from being summed twice |
| `jestkexp4.csv`, `jestksa.csv` | Aliases, never loaded beside their primary | see *Ignored files* |
| `PKG-REC.csv` | Slip qty is consumed per `(po8, item)`; a slip that landed one PO line is not available to another | one slip, one landing |

## People-maintained files (`fs_manual`)

| File | What it is | Format | Rows (2026-09-15) | Flowstate use |
|---|---|---|---|---|
| `cip_info.csv` | CIP state per line: last CIP, max hours between CIPs, next scheduled CIP | `,` utf-8 with BOM, CRLF; header `ID,LineEquipment,PreviousCIP,MaxHoursBetweenCIP,ScheduledCIP,Notes` | 14 | CIP projection and the *CIP (sched)* overlay on the calendar. Unchanged |
| `demand_plan_summary.csv` | The weekly demand plan, tons per product per ISO week | `,` **utf-8 with BOM, CRLF**; header `Week,Product,Tons` | 238 | **Written by the sync** from the AZAP workbook (recipe below); the bridge then derives `demand_plan.csv` from it exactly as before |
| `Shipping Receiving Schedule NPA - 2024.xlsm` | The dock's receiving appointment book | Excel macro workbook | — | Dock appointments joined to PO lines on the 8-digit PO suffix; SL3 lines need a transfer appointment to count (`docs/order_npa_export.md`) |
| `New Export AZAP MMDDYY.xlsx` (also `.xlsm`) | The weekly AZAP demand export (`New Export AZAP 091126.xlsx` on 2026-09-15, 14 MB); **replaced every week**, `091126` = the export date `MMDDYY` | Excel; sheet `pdp export AZAP n20` | — | Source of `demand_plan_summary.csv` |

## AZAP demand recipe

Pinned against the current `demand_plan_summary.csv`: all 238 rows reproduce
exactly.

1. Sheet `pdp export AZAP n20`, header in row 1, 33 columns; the ones used
   are `Factory` (`NPA` / `TVC` / blank), `Product` (int), `Tons` (float),
   `Start Date` (a Monday datetime) and `Week` (= the ISO week of
   `Start Date`).
2. Keep `Factory == "NPA"` only. **No** `To Export` filter.
3. Drop non-numeric products (only `Trial` exists).
4. Sum `Tons` per (ISO year, ISO week, Product).
5. Window = **7 ISO weeks starting the first Monday strictly after the
   export date**; the export date is parsed from the file name
   `New Export AZAP MMDDYY.xlsx|xlsm` (fallback: the file's mtime date).
6. Rows sorted by `Week` then `Product`; `Product` as the 6-digit code
   (zero-padded to 6 when shorter).
7. Output `demand_plan_summary.csv` beside the workbook: utf-8 with BOM,
   CRLF, header `Week,Product,Tons`, `Tons` with up to 3 decimals and
   trailing zeros stripped (`39.003`, `50`, `8.999`).

**Freshness rule.** The rebuild is skipped when `demand_plan_summary.csv`
exists and its mtime is **on or after** the workbook's mtime — a hand edit
made after the export is never overwritten; a newer workbook always wins.

**CLI.** `scripts/azap_demand_summary.py` does the same by hand. Without
`--folder` it uses the **first folder-mode source folder** — `source_dirs`
(or the single `source_dir`) in `scripts/fs-live-data.local.json` (written by
the installer) merged over the tracked `scripts/fs-live-data.conf.json` —
that holds an AZAP workbook. No folder is hard-coded: on a machine without a
folder-mode conf (the dev laptop, the work PC), or when no configured folder
holds a workbook, it exits 1 and you pass `--folder`:

```powershell
.venv\Scripts\python.exe scripts\azap_demand_summary.py                                        # planner PC: the configured folder that holds the workbook
.venv\Scripts\python.exe scripts\azap_demand_summary.py --folder "C:\…\fs_data\fs_manual"      # any other machine
.venv\Scripts\python.exe scripts\azap_demand_summary.py --folder "C:\…\fs_data\fs_manual" --dry-run   # print the rows, write nothing
```

`--help` lists the options (`--folder`, `--weeks`, `--out`, `--dry-run`,
`--force` to rebuild past the freshness rule or past the shrink guard — a
0-row build is never written). The sync runs the rebuild every pass before
copying `fs_manual`, so the app always sees the summary that matches the
newest workbook.
