# Flowstate — Live Ops & Multi-Week Schedule (implementation plan)

**Branch:** `feature/live-ops` · **Status:** design, awaiting sign-off
**Builds on:** `feature/azap-raw-import` (rough-draft workflow, PDF schedule import, WW labels)

## What this turns Flowstate into

From a *weekly planning* tool into a **constantly-running ops tool**: current week (WW32) live on the calendar, next week (WW33) planned, MO completion filling in real time, CIP schedule drawn and enforced, all data sources configurable so Carsten points at his own files/SQL.

## Confirmed inputs (all verified against real files)

| Source | Format | Key facts |
|---|---|---|
| **WW32 / WW33 schedule PDF** | VIF print | Parsed by existing `schedule_pdf_import` (51 blocks WW33). WW32 same layout, week 08/02–08/08. |
| **manprg.txt + manprg2.txt** | `;`-delimited, 15-min refresh | Line-split (P09–P18 / P19–P22), **zero MO overlap — keep two-file merge**. `Qty made (Cas)` / `Fct qty (Cas)` = completion. Current MO per line = latest-start row with qty>0. |
| **cip_info.csv** | CSV | Per line: `PreviousCIP`, `MaxHoursBetweenCIP` (120/144), `ScheduledCIP`, Notes. Both draw scheduled CIPs AND drive CIP limits. |
| **SQL (manprg live)** | SQL Server `NPA-PMUTSQL3901.bel.com,62272` / NPA | Direct connection for auto-refresh; file import as fallback. CIP also via SQL: `[NPA].[dbo].[tblCIPSchedule]`. |

## Workstreams

### 1. Multi-week horizon
- `flowstate.toml` `horizon_hours` 336 → **504** (3 weeks).
- Anchor from AZAP import (already moves to file's earliest week).
- Import **both** WW32 + WW33 PDFs as *versions* (Version Compare), or WW32 as the live baseline + WW33 as next-week version.
- Verify Gantt renders 3 weeks and the week dividers/ISO labels scale.

### 2. Live MO completion (manprg)
- `code/helpers/manprg_import.py`: read both files, merge (union on MO), parse `;`, cp1252. Per line, current MO = latest `Start date+time` with `Qty made (Cas) > 0`. Completion % = made/fct.
- SQL connector `code/helpers/manprg_sql.py`: configurable DSN; runs the user's `ROW_NUMBER() ... rn=1` query for current-MO-per-line. File import = fallback when SQL unreachable.
- **Gantt:** production bar gets a left-to-right progress fill (width = completion %). **Above the chart:** a per-line "now running" strip (line, MO, SKU, %, cases left).
- Refresh: manual button + auto-refresh on calendar load when manprg mtime changed (15-min cadence respected, no daemon).

### 3. CIP from cip_info.csv
- `code/helpers/cip_import.py`: parse the CSV (UTF-8 BOM, NULLs).
- Draw `ScheduledCIP` as CIP blocks on the Gantt; `PreviousCIP` as a small marker.
- Drive CIP limits from `MaxHoursBetweenCIP` (replaces/augments `line_cip_hrs.csv`).
- Flag lines running past `MaxHoursBetweenCIP` without a CIP.

### 4. Downtime/maintenance/trial with real date+time
- `downtimes.csv` schema → `start_dt` / `end_dt` (ISO), replacing `start_hour`/`end_hour`.
- `downtime_ui.py` editor: `st.date_input` + `st.time_input` for start AND end (no more bare hours).
- `calendar_io` converts to hour offsets against the anchor for storage/solver.
- Migration: read legacy `start_hour`/`end_hour` if present, else `start_dt`/`end_dt`.

### 5. Configurable data connections (Settings)
- New Settings section / `flowstate.toml` `[datasources]`: VIF folder, AZAP CSV path, manprg file paths (2), schedule-PDF folder, cip_info path, SQL DSN + enabled flag.
- Every importer reads its path from config (Carsten points at his files, no code edits).
- Sensible defaults = the dev fixtures in `data/`.

## Architecture notes
- New helpers are pure Python, unit-tested; pages are thin renderers (repo convention).
- Gantt progress fill is a frontend change (`GanttBlock.tsx`): add `completion_pct` to the payload, render a filled sub-bar. Rebuild `dist/`.
- SQL via `pyodbc` (added to venv); import guarded so app still runs file-only if driver absent.
- All on `feature/live-ops`; nothing touches `main` until browser-verified and you say merge.

## Verification
- Golden tests: manprg merge (70+34 rows → per-line current MO, completion %s match probe), cip_info (14 lines, 120/144), PDF WW32 parses.
- Browser QA: calendar shows WW32 live + WW33 planned; progress fills present; now-running strip correct; CIP blocks drawn; downtime editor has date+time pickers; Settings page persists paths.
- Director re-verifies before merge.

## Open defaults I'll assume unless you say otherwise
- WW32 becomes the **live baseline** (`calendar_blocks.csv`), WW33 imported as a **version**.
- SQL refresh is **opt-in** (toggle in Settings, default off until Carsten's DSN is confirmed); file import works out of the box.
- 3-week horizon 504h.
