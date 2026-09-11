# Flowstate UI streamline — proposal and what shipped (2026-09-11)

**Goal.** The Plant Calendar is Carsten's main screen. It has to be the first
thing he sees, the board has to be the page, and saving what he sees has to
be one click that can never miss an edit. Second priority: the Stock Check
and the reports that come out of it. Third: the whole app reads as one light,
low-glare workbench instead of a dark developer dashboard.

This document is both the proposal and the record of what was built on the
`claude/sharp-clarke-ipkftl` branch (based on `develop`). Everything in
§2–§5 is shipped; §6 lists what is proposed but deliberately not built yet.

> **Design source.** The request referred to a Claude Design schema/scheme.
> That file was not reachable from this session (no design artifact is
> published under the account; the repo carries none), so the palette and
> type below were derived from the one Claude-authored design language the
> project already has — the light IBM-Plex/teal system of the *Flowstate
> Forensic Audit* artifact — and reduced to a plant-floor system font stack.
> Every token lives in exactly three mirrored places (§1), so swapping in
> the Claude Design values is a 10-minute change with no page edits.

---

## 1. Design system

One palette, three mirrors that must stay in step:

| Where | What it drives |
|---|---|
| `.streamlit/config.toml` `[theme]` | Streamlit's own widgets, sidebar, dataframes, alerts |
| `code/helpers/theme.py` `TOKENS` + `inject_css()` | page chrome, metric tiles, chips, section labels, expanders, buttons |
| `code/components/gantt/frontend/src/utils/theme.ts` `T` / `CHART` | the React Gantt: rows, header band, gridlines, tiles, popovers, buttons |

### Tokens

| Role | Hex | Used for |
|---|---|---|
| page background | `#F3F5F7` | app background, board legend row |
| surface | `#FFFFFF` | cards, tables, sidebar, Gantt rows |
| surface-2 | `#E8ECF0` | table headers, day bands, code |
| rule | `#D3D9DF` | every border |
| ink / ink-2 / ink-3 | `#1E2530` / `#4E5A67` / `#7A8592` | text, secondary text, labels |
| accent / accent-2 / accent-soft | `#0F6E6E` / `#0B5757` / `#DCEFEE` | primary buttons, selected nav, lock chip |
| ok / warn / bad / info / neutral | `#2E7D4F` / `#B9640F` / `#B3322E` / `#2F6DB5` / `#6B7A8C` | status only — never decorative |

Status colors always ship with an icon or a word (● ▲ ✕, "AT RISK"); no
meaning is carried by color alone.

**Type.** System UI stack (`Segoe UI` on the plant PCs, `system-ui`
elsewhere). No web-font download, so the app renders identically on a
machine without internet access. Headings 1.6 / 1.18 / 1.0 rem, weight 700.

**Production block palette** (`colors.ts`): seven hues validated on a white
surface with the data-viz validator — adjacent-pair CVD ΔE ≥ 9.1, normal
vision ΔE ≥ 19.6. Red is absent on purpose: it belongs to downtime and
status. Block identity is always carried by the label text; color only groups
the same SKU at a glance.

| slot | hex | slot | hex |
|---|---|---|---|
| blue | `#2A78D6` | magenta | `#E87BA4` |
| orange | `#EB6834` | green | `#008300` |
| aqua | `#1BAF7A` | violet | `#4A3AA7` |
| yellow | `#EDA100` | | |

Window blocks: scheduled CIP slate `#6B7A8C`, projected CIP light `#B9C2CB`
with a dashed outline, trial gold `#C9962A`; downtime / maintenance /
contractor windows are **hatched** so a constraint never reads as a run.

---

## 2. Plant Calendar — the planner's main screen

### Before

The board sat ~800 px down the page, under: a developer caption ("Phase 1 —
Digital Twin…"), the horizon line, two banners, a roll button, three
expanders (rebuild, float links, start-of-day), a hidden-blocks caption, the
now-running expander and two holding captions. Save lived a full screen
below the board and only saw the last "Refresh checks" push — edits made
since were silently not saved.

### After

1. **Header row** — title, horizon (`Fri 2026-09-11 → Fri 2026-10-02 · WW37 /
   WW38 / WW39`), and status chips: live data fresh/stale/missing, lock
   state, versions used, finished blocks hidden, last save time.
2. **Attention strip** — only what must be handled before planning, each
   with its own button on the same row: live feeds missing or stale, the
   weekly *Roll calendar to today*.
3. **Control row** — version name · hide finished blocks · reload from disk.
4. **The board**, with its own toolbar: `✓ checked` / `● unsaved edits`
   chip, overlap chip, **⟳ Refresh checks**, **Save as version**, **💾 Save**
   (also `Ctrl+S`). Week cards, zoom, legend, holding area (now height-capped
   with its own scroll) and the live demand-adherence table follow.
5. **Lock & export strip** — lock through / unlock / Excel export.
6. **Tabs** for everything that used to sit above the board: *Live score*
   (Δ vs the saved board + the full scorecard), *Plant state*
   (rebuild from manprg + cip_info, link management), *Downtime* (the only
   editor of downtimes.csv), *Now running*.

### The Save fix (the one behavioural change)

Save now goes **through the board**: the component pushes its live edits
together with a `saveRequest {kind, nonce}`; Python writes exactly the
board it received (disk, or a named version using the name in the control
row), records the nonce so a rerun never saves twice, shows a toast and
updates the "saved HH:MM" chip. Hidden finished rows are merged back and
display-only overlays (cip_info CIPs, downtimes) are stripped exactly as
before. Verified end to end in a browser: both buttons write, the chip
updates, the toast shows.

### Board polish

- Legend row: production · committed MO · pinned · CIP · projected CIP ·
  trial · down/maintenance · locked weeks, plus the mouse/keyboard grammar
  (drag, edge-resize, click, right-click, Shift+right-click, gap right-click,
  Ctrl+Z) — planners asked what "MO" and the dashed cleans were.
- Light rows (`#FFFFFF` / `#F7F9FB`), quieter gridlines, slate week lines,
  hatched constraint windows, dashed projected cleans, teal drop targets,
  amber highlight ring.
- KPI tiles, week cards, holding cards, adherence table, context menu,
  block popup, SKU picker and placement popover all restyled with the same
  tokens (no logic touched; the parity-tested math in `utils/` is untouched).

---

## 3. Stock Check and its reports

### Before
Folder input and toggles at the top, a caption per source file, four
metrics with misleading up-arrows, and a list of expanders per block at
risk. No component-level view, no export.

### After
- Header + one control row (demand week · **⟳ Refresh from VIF**); the VIF
  folder and availability toggles moved into a *Sources & availability*
  expander (setup, not daily work).
- Status chips: report current/stale, VIF files + newest export, receiving
  appointments, import errors.
- Tiles: blocks at risk `n / total`, do-not-schedule `n / total`, demand
  SKUs at risk, **components short**, and **⬇ Export stock report (Excel)**.
- Tabs: **Board** (table, worst first, `Constraints` column names the short
  items; toggle to show every block; per-block detail below), **Demand
  plan** (table), **Shortages by component** (new), SKU drill-down,
  Component → SKUs, Receiving, Data quality.

### Shortages by component — the purchasing list (new)
`helpers/stock_reports.py` turns the report round by purchased item: on
hand vs board need vs demand need (week-filtered), board and demand
coverage, blocks at risk, the first run at risk (line + wall-clock start),
consuming SKUs, alternates, in-house/untracked notes. Only items short
somewhere are listed, worst first. Pure functions, 5 unit tests.

### Excel export (new)
Six sheets — Summary · Board · Demand plan · Component shortages · Receiving
· Data quality — the same tables the page shows, frozen header rows,
auto-sized columns. Cached per report + week filter.

Reconcile, which consumes the same persisted report, keeps its counts and
findings; it only gained the header, severity chips and the theme.

---

## 4. Command Center, Reconcile, Scorecard

- The app now **opens on the Plant Calendar** (`default=True` in
  `code/app.py`); the Command Center stays first in the sidebar and its loop
  status is echoed by the calendar's header chips (live data, lock).
- Command Center: each loop step is a card with a status chip; "What you
  need to do next" and the data-status table are unchanged in content.
- Reconcile: header, severity section labels with counts; the stale banner
  text and the *Refresh* button are unchanged (tests pin the wording).
- Scorecard: header + a one-line AZAP note instead of the info box; the
  composite gauge and category bars pick up the accent color.

---

## 5. Verification

- `python3 -m pytest -q` — full suite green, including the node-gated Gantt
  parity tests and 7 new tests (5 stock-report, 2 calendar smoke).
- `npm run build` in `code/components/gantt/frontend` — type-checks and
  rebuilds `dist/` (committed).
- Browser: every page screenshotted before/after at 1600 px; the board's
  Save and Save-as-version buttons exercised end to end.

---

## 6. Proposed next, not built

| Proposal | Why it is worth it | Why not now |
|---|---|---|
| Sticky board toolbar (Refresh/Save pinned while scrolling the holding area) | keeps Save one click away on a long board | needs a scroll-aware iframe/host handshake; the board is already above the fold |
| Holding area as a wrapping grid with a per-week filter | 30+ cards per week still make a tall panel | the "one column per ISO week" layout was a planner request; ask Carsten first |
| Draw downtime by dragging on the board (no form) | the downtime form is the last form on the page | needs a new drag mode in the Gantt; downtimes are the single source of truth — worth a separate review |
| Stock shortages on the board (block outline by stock status) | the two screens tell one story | a stock badge per block needs the report keyed by block id; small follow-up |
| Compare & Promote / Generate restyle | they inherit the theme but keep their old layout | not on the daily path for Carsten |
| Swap in the Claude Design tokens | the request named it | file not reachable here — three files to update (§1) |

### How to change the look
Edit the three mirrors in §1 (config.toml → theme.py → theme.ts), run
`npm run build` in the frontend folder, restart Streamlit. Nothing else
references a color.

---

## 7. Follow-ups from the 2026-09-11 trial (merged with the audit fix round)

- **Holding card "×"** — hovering or clicking a holding card shows a small ×
  in its corner; it removes the card. Because holding is *derived* (demand −
  board − made) on every board edit and on every "Refresh checks", a plain
  delete would pop straight back, so the order is remembered as *dismissed*
  (`holdingDismissed` in the component state, `cal_holding_dismissed` in the
  page session): no card of any kind is derived for it until the adherence
  table's "+" brings it back. Dismissals live as long as the holding itself
  (until *Reload from disk*); they are never written to disk. Ctrl+Z undoes
  a removal.
- **Float links removed** — the "Plant state & float links" tab is now
  "Plant state"; the link/unlink controls, the "float sync" half of the MO
  drift button and the `after:<id>:<gap>` helpers are gone (never used on the
  live board; the planner could not tell what the option did).
- **MO drift removed** — the attention-row chip "n MO block(s) drifted vs
  live manprg" and its "Apply MO drift" button are gone too (planner
  request, same day): moving MO blocks to their live times while their
  neighbours stayed put created overlaps. The board now changes only when
  the planner edits it or rebuilds it from the plant state (Plant state
  tab).
