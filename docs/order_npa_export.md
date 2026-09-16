# `order_npa.csv` — the ERP's purchase-order line export

Since 2026-09-14 the ERP (Sage X3) exports the plant's purchase-order lines
itself as `order_npa.csv`. It replaces IT's hand-run **"NPA Open POs"**
workbook (`open_pos.xlsx`) as the open-PO feed for the supply timeline.
This page is the structure reference: what the file is, how it is
encoded, how the app reads it, and how to open it in Excel without losing
anything.

Code: `code/stockcheck/x3_po_export.py` (full-fidelity reader + PoLine
projection), `code/stockcheck/po_import.py` (`load_open_pos` detects the
layout), `scripts/convert_order_npa.py` (Excel-clean conversion).
Tests: `tests/test_x3_po_export.py`.

## Delivery

| Step | Where |
|---|---|
| ERP export lands on the work PC | `SRC_DIR` of `fs-live-push.py` |
| Pushed to the private live-data repo | `spartacus242/flowstate-live-data`, file `order_npa.csv` |
| Pulled and copied by the bridge | `scripts/fs-live-data.conf.json` lists `order_npa.csv` → `data/reference/order_npa.csv` |
| Read by the app | `helpers.reconcile_engine.open_po_path`: `[datasources] po_report_path` → `order_npa.csv` → `open_pos.xlsx` → `open_pos.csv` |

`data/reference/order_npa*` is git-ignored (supplier data; the bridge owns
it). The checkout converts line endings, so the delivered copy is CRLF
while the repo blob is LF — the reader accepts both.

## Physical format (verified on the 2026-09-11 export, 983 lines)

| Property | Value |
|---|---|
| Delimiter | `\|` (pipe). 219 fields on **every** line, header included |
| Quoting / escaping | **None.** A literal `"` (the inch mark in `LABEL 6" X 8.25"`) and commas (`SOLON, OH`) are plain text. This is why Excel mangles it: it assumes commas and treats the lone `"` as an opening quote |
| Encoding | Windows-1252 (`é è ê ô à « » ’` in the French header; the data rows of the sample were pure ASCII). `cp1252`, not `latin-1`: `’` (0x92) does not exist in latin-1 |
| Line endings | LF from the ERP; CRLF after the bridge's git checkout |
| Header | Line 1, French Sage X3 field labels. 15 labels repeat (e.g. *Raison sociale* ×2, *Prix brut* ×2, every amount ×3 for the three currencies) — the reader maps repeats by order of occurrence |
| Row identity | (`order_no`, `line_no`) is unique. `line_seq` (the second *Numéro de la ligne*) is the delivery sub-line |
| Dates | `DD/MM/YYYY` (1686 values with day > 12, none with month > 12). Three date columns: order date, expected receipt date, initially requested arrival date |
| Numbers | Every quantity, price and amount is a **13-digit zero-padded integer with four implied decimals**: `0000180000000` = 18000.0000 kg, `0000000011463` = 1.1463 USD/KG, `0000206334000` = 20633.40 USD. Pinned against the old workbook on three POs (qty, price, amount all agree). Amounts are rounded to cents by the ERP before scaling |
| Codes | Zero-padded strings and ERP keys: supplier `000048`, line `000001`, contract `30002191`. The reader keeps them verbatim |
| Times | `HH:MM` text (`00:00`, `07:00`, `13:00`) |
| Signs | `-1` / `1` integer columns that give the sign of the charge/discount amounts |
| Blanks | Empty field. 51 of the 219 columns were empty on the whole sample (intermediary, external codes, header criteria, line criteria 3–10) |

## Column groups

Index = 0-based position in the export. Keys are the English names the
reader uses (the `Columns` sheet of the converted workbook lists all 219
with their French labels).

| Index | Group | Keys (selection) |
|---|---|---|
| 0–5 | Company / site / order | `company` M2, `site` 02, `order_type` 1ACDE, `order_no` (8 digits, the PO number used everywhere) |
| 6–17 | Supplier | `supplier_order_ref`, `supplier_code`, `supplier_name`, city / country, family (RM raw, PK packaging, AP) |
| 18–27 | Intermediary / contact | empty on the sample |
| 28–36 | Order header | `order_date`, `buyer`, `planner`, `delivery_mode` (1 ocean, 3 ground), `delivery_terms` (DDP3, FOB3, EXW3 …) |
| 37–39 | Currencies | management USD, order USD, statistics EUR |
| 40–47 | Line + item | `line_no`, `item` (`754821`, `735009-A`), `item_name`, `item_family` (RM1, PK1, RM3, PK3, PK4, RD) |
| 48–54 | Contract / offer | `contract_type` 1AMAR, `contract_no`, `contract_line_no` |
| 55–58 | Price | `gross_price` (×2), `price_unit` (KG, KEA = per thousand, M2, EA, L, FT) |
| 59–85 | Amounts | gross amount ×3 currencies, then discounts / promotions / freight / other charges, on-invoice and off-invoice, each ×3 |
| 86–95 | **Quantities and dates** | `line_seq`, `line_status`, `qty_ordered`, `qty_remaining`, `order_unit`, `qty_ordered_stat` / `qty_remaining_stat` (in `stat_unit` KG — 0 for EA/M2 lines), `receipt_date`, `receipt_time` |
| 96–115 | **Receiving** | `receipt_location` (RP1, RB1, RA1, RC1, AMB, SL3), `warehouse` (SFG, SB1, SA1, SF1, SC1, AMB, SL3), `bin_location`, analytic section |
| 116–117 | Planned lot, internal-supplier flag | |
| 118–153 | Pay-to / ship-from / bill-from supplier | the supplier identity block repeated three times |
| 154–158 | Item short name, external codes | |
| 159–165 | Billing company, `requested_date`, payment terms | |
| 166–173 | Signs | |
| 174–175 | **Comments** | `line_comment_internal` (buyer notes: "8/18 ERV QTY FROM 118,000 TO 117,510 …"), `line_comment_external` |
| 176–178 | Contract amounts ×3 | |
| 179–198 | Header criteria 1–10 | empty on the sample |
| 199–218 | Line criteria 1–10 | `PRO` producer / `COO` country of origin on the raw-material lines (541 rows); 3–10 empty |

## Codes seen on the sample

**Line status (`line_status`)** — confirmed by IT on 2026-09-15 (see
*Answers from IT* below). The app keys on it since that day:

| Code | Rows | Remaining qty | ERP meaning | App |
|---|---|---|---|---|
| 20 | 889 | = ordered (882), partially received (7) | receivable — actively pending receipt | inbound while `qty_remaining` > 0 |
| 60 | 20 | 0 (19), full (1) | archived (closed) | `received` — closed, never counted |
| 70 | 74 | 0 on 29 lines; full on 45 lines mixed with status-20 lines inside the same PO | deleted | `cancelled` — its own fate, never inbound whatever remains |
| other | — | — | unknown | remaining-qty rule, `status_text` blank |

A fully archived order drops out of the export. An order keeps appearing
while some of its lines are deleted and the rest are not yet archived, and
a status change made after the previous evening's export only shows in the
next one.

**Receipt locations** (`receipt_location`, the old *Arrival area*): RP1
packaging 374, SL3 IDL Salt Lake City DC 306, RB1 ambient raw 144, AMB
Americold Burley 84, RA1 apple 66, RC1 cold 9. The timeline already treats
RB1/AMB/RC1 as raw (QC offset) and SL3 as off-site (counted only from a
joined transfer appointment) — `flowstate.toml [stock] raw_areas /
offsite_areas`.

**Units**: order unit KG 612, EA 240, M2 127, L 2, FT 1, KEA 1. Price unit
KEA (per thousand) on 191 EA lines. Statistical (KG) quantities are only
filled for KG/L lines. IT (2026-09-15): **KEA = thousand each** — the one
KEA line (PO 30044091) orders 1.5 KEA = 1,500 EA, priced per KEA, and the
amount reconciles. `to_po_lines` scales KEA lines to EA (`unit_original`
keeps `KEA`) so the BOM join counts them; purchasing / contracts still owe a
written confirmation of the convention.

## What the app takes from it

`x3_po_export.to_po_lines` projects every row onto the PoLine contract the
supply timeline already consumes (`po_import`, plan §2):

| PoLine key | Source | Note |
|---|---|---|
| `po` | `company site order_type order_no` | `M2 02 1ACDE 30043074` (old workbook: `2 02 1ACDE 30043074`) |
| `po8` | `order_no` | the join key to dock appointments — unchanged |
| `item`, `designation` | `item`, `item_name` | |
| `qty` | **`qty_remaining`** | what is still inbound; the received part of a partial delivery is already in stock; a KEA line ×1000 |
| `unit` | `order_unit` | compared case-insensitively to the BOM unit; `KEA` becomes `EA` (`unit_original` = `KEA`) |
| `receipt_date`, `initial_receipt_date` | `receipt_date`, `requested_date` | old *Receipt date* / *Initial Receipt Date* |
| `arrival_area` | `receipt_location` | same codes as the old *Arrival area* |
| `supplier`, `supplier_id` | `supplier_name`, `supplier_code` without zero padding (`48`) | `supplier_code` keeps the ERP key |
| `received` | `qty_remaining ≤ 0` **or status 60** (archived) | old rule: *Receipt number* present; the timeline's reason says "archived in the ERP" for status 60 |
| `cancelled` | status 70 (deleted) | never inbound whatever remains; fate `cancelled` on the Inbound tab |
| `status_text` | `line_status` | `receivable` / `archived` / `deleted`, blank for an unknown code (shown as *state* on the Inbound tab) |
| `order_date`, `row` | `order_date`, file line | |

Extras carried on every line (blank on legacy-workbook lines):
`qty_ordered`, `qty_remaining_kg`, `status`, `status_text`, `cancelled`,
`unit_original`, `line_no`, `line_seq`,
`supplier_code`, `supplier_ref`, `warehouse`, `receipt_time`, `price`,
`price_unit`, `amount`, `currency`, `buyer`, `planner`, `delivery_terms`,
`contract`, `producer`, `origin`, `comment`, `comment_external`. The
Inbound tab shows `ordered` and `status` beside the remaining qty.

Everything else stays reachable through `read_x3_po_export(path)`:
`rows` (all 219 decoded columns), `raw` (the untouched strings), `header`.

## Opening it in Excel

```bash
.venv/Scripts/python.exe scripts/convert_order_npa.py
```

writes `data/reference/order_npa.clean.xlsx` (sheet *PO lines*: typed
cells, real dates and numbers, codes kept as text; sheet *Columns*: key ↔
French label ↔ kind) and `order_npa.clean.csv` (UTF-8 BOM, comma, quoted).
`--summary` prints the structure/health report without writing;
`--french` puts the French labels on the csv.

## Header drift

The reader matches columns by normalized name (accents, case and
punctuation ignored — two ERP typos, `Nom du <Livré par »` and
`Nom du < Facturé par »`, already differ from their siblings), repeats by
occurrence. A renamed column at its old position is accepted by position
and noted; an unknown column is kept as `extra_<n>`; a dropped column is
listed in `missing_columns` and blank on every row; rows with the wrong
field count are kept and flagged. All of it lands in `errors`, which the
Inbound tab lists — nothing is dropped silently.

## Answers from IT (meeting 2026-09-15)

1. **Line status**: 20 = receivable (actively pending receipt), 60 =
   archived, 70 = deleted. Applied in `to_po_lines` the same day (table
   above). Example walked through: order 30044376, three lines 1–3 all
   status 20; repeated order numbers are the lines of one order.
2. **Cadence**: the export leaves the ERP at about **19:10** every day. The
   SharePoint Power BI upload refreshes every 15 min, so the file lands
   between 19:10 and about 19:25. No as-of stamp inside the file; the
   local copy's modified time (kept by OneDrive from SharePoint) is the
   observation time. The 26 h `open_pos` health cadence stands.
3. **Which lines**: everything not fully archived. A fully archived order
   disappears; an order with deleted lines stays visible until all its
   lines and the order are archived; a change made today may still show
   until tomorrow's export.
4. **KEA**: thousand each — 1.5 KEA = 1,500 units, price per KEA, the
   amount reconciles. Purchasing / contracts own the unit configuration
   and should confirm in writing. Small last-digit differences between
   amount columns are rounding, currency conversion or export formatting.
5. **Comments and criteria**: internal and external line comments are the
   two columns near the right of the extract (`line_comment_internal`,
   `line_comment_external`); line criteria carry `COO` (country of origin)
   and `PRO` (believed to be raw-material traceability — exact meaning to
   confirm if ever needed).
6. **Where the file lands**: the SharePoint library **VIF Extracts** of the
   NPA_ContinuousImprovement site (Shared Documents), same file name every
   day, no separate archive. Synced by OneDrive on the work PC at
   `C:\Users\B70048652\GROUPE BEL\NPA_ContinuousImprovement - Documents\VIF Extracts`
   — the bridge's `source_dir_work`, and the folder a planner's PC syncs
   for `install_flowstate.ps1 -FeedDir` (mark it *Always keep on this
   device*).
7. **MRP views** cannot be reproduced as extracts: only a few people build
   them and IT cannot export exactly what another user sees.
