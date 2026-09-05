# Open-PO receipts + time-phased stock check on the Plant Calendar — DRAFT plan (2026-09-01)

**Status:** BUILT 2026-09-01 (slices 0, 1, 2a, 2b) with every decision at its recommended default; slice 3 NOT built (separate sign-off). Implementation contracts: `2026-09-01-po-stock-contracts.md`. Verified headless in the browser; suite 830 green. Known follow-ups: rounding of sub-day leads now reads in hours; the 8/24 sample feed is content-stale until IT widens the window; blanket POs and partial receipts still need IT columns (plan §3).
**Origin:** user request 2026-09-01 (sample `NPA Open POs -8.24.xlsx` from IT; "flag a block as a shipping constraint when
stock covers only part of the run and a delivery lands shortly before; minimum ~4 days after a delivery before scheduling").
**Method:** 6 codebase readers → 4 independent designs → 3 judges → synthesis → critic → revision (16 agents).
Winning angle: "Candid Supply Timeline" (robustness/honesty-first), with the MVP phase order grafted in.

**Empirical findings that shaped it (2026-09-01, live data):**
- Engine is FLAT: `coverage_for_requirement` compares every block / demand week against the full on-hand pool; no netting
  across blocks or weeks, no dates (`available_total` identical on all blocks for 130/133 items).
- Time-phased sweep of TODAY's board (on-hand only, linear draw, no receipts): 7 of 40 blocks run a component dry mid-run
  or before start; 4 of them read OK in the flat report (280324 P21 750131@31%, 280324 P18, 280591 P22 752706@84%,
  280564 P18 754748@62%). Netting alone changes verdicts on day one.
- PO file joins cleanly: 54/57 items are plain BOM keys with identical units (730060/730061 via ediact 4 RF recipes;
  899999 is GPI tooling). Receiving-schedule `po` = last 8 digits of the ERP Order number; the dock sheet covers
  packaging carriers only, booked ~8–11 days ahead.
- `Receipt date` is dock-accurate (±1 d) for packaging suppliers; imported fruit lines had already re-promised +9..+37 d.
  Physical slip (batch-date decode) ≤ +4 d for 91% of landed lines — the 4-day buffer covers the observed tail.
- The 08-24 extract is 5 days wide and all dates precede the 09-01 board; 43/79 lines had already landed by the 09-01
  stock export (naive on-hand + PO double-counts). IT needs to widen the window to ≥ 5 weeks and add an as-of stamp.
- Split MOs share one `block_id` in calendar_blocks.csv (8 ids × 2–3 pieces) — verdicts must key on id + start.

---

# Stock-aware placement on the Plant Calendar — final plan

## 1. What exists today

- **Engine (`code/stockcheck/`)**: `api.stock_check_report` builds one flat `avail = {item: qty}` (`coverage.available_stock`, Ava-only by default) and compares **every board block and every demand week against that full pile** (`coverage_for_requirement` only does `avail.get()`, never subtracts). Block kg = `hours × calc_rate_kgph` (`explode.block_qty_kg`), not the board's `qty_kg` (which is populated on 40/40 production rows — the header comment saying it is NULL is stale). Running MOs are counted in full even though `jestkexp` already reflects consumption up to its export moment. Verified on the live cache: 752420 film shows 64,158 M2 on all three 280351 blocks that jointly need 51,445; 752706 opens at 21,000 EA and the two 280591 P22 pieces (11,648 + 11,125) both read OK. Measured cost ~7 s warm (~90% per-row BOM explosion; explosion is exactly linear in cases; 79 distinct SKUs).
- **Receiving parser** (`stockcheck/receiving_import.py`): dock appointments `{date,time,po,category}` — PO# only, no item/qty; display-only on Stock Check; `po_appts` is built and never joined.
- **Report cache** (`helpers/stock_report_cache.py`): one persisted `data/stockcheck/report.cache.json` keyed by 6 VIF mtimes + board + demand + rates + toggles. Home auto-recomputes; Stock Check/Reconcile only on Refresh; pages must never compute on load (`tests/test_pages_smoke.py`); overnight batch always recomputes.
- **Gantt payload precedents**: Python precomputes lookup tables (`build_co_flags` → `coFlags`, `gantt_kpis.co_pairs`) passed through `gantt_calendar(...)` → `SandboxArgs`; read-only mounts omit them and the client gates on presence (`pickerEnabled`). Args are re-read every rerun without remount; block objects are copied into client state once. Warn = `setWarnMsg` (orange banner), reject = `reject()`; drag preview `valid=false` refuses the drop.

**The two gaps**: (1) no time-phasing — nothing nets consumption across blocks or weeks, so "stock covers 40% of this run" cannot be expressed; (2) no inbound — no data structure anywhere knows a PO, a quantity or a receipt date.

## 2. The risk model ("Candid Supply Timeline")

**Frame.** Board hours from the storage anchor (`config.planning_anchor`, today 09-01 00:00). Dates → hours only in Python (`timefmt.datetime_to_hour`). Demand-week buckets (slice 3) use `stockcheck.weeks.demand_anchor` (2026-08-31 today); block hours use the board anchor; **never mix** (time-frame invariant). Stock snapshot hour is **per stock frame**: `S_rm` from `snap.source_files['jestkexp.csv']`, `S_pkg` from `snap.source_files['jestkexp2.csv']` (identical today, not guaranteed), each converted with the local anchor (today 14:46 → **14.8 h**, not UTC). Caveat: these mtimes are laptop pull/copy times (`copy2` preserves the clone checkout; pull every 30 min, push every 5), so S is late by up to ~35 min — clipping error = rate × 35 min per running MO (Section 8). Both hours ship to the client as `snapshotRmH`/`snapshotPkgH`; the client never parses dates.

**Per-item timeline.** For each item i: `opening_i` (toggle-honouring on-hand); draws from every production block b: `need_{b,i} = cases_b × per_case_i` (unit recipe from one memoised `explode(sku, 1.0)`; `cases_b = qty_kg_b / kg_per_case`, board `qty_kg` first, `rate × hours` fallback), consumed **uniformly over `[max(start_b, S), end_b]`** on one shared curve across all lines (no ordering rule). **Running MOs**: remaining draw = `cases_left` (calendar.py L502-507, from manprg) when present, spread uniformly over `[S, end of the MO's last piece]` (split pieces share one MO); fallback = uniform-clipped from board `qty_kg`. Receipts are step events at `ready_h`. Alternates: every item has its own timeline; a SKU's balance for a requirement group = primary timeline + its alternates' timelines (same pooling as today, now time-phased).

Curves: **Hard** H(t) = opening − draws; **Planned** P(t) = H + all counted receipts landed by t; **Reliable** R_b(t) = H + receipts with `ready ≤ start_b − L` (L = 96 h).

**Verdicts per (block, item)** (skip in-house BT001/BT002 and untracked items):

| Verdict | Rule | Severity |
|---|---|---|
| **OK** | min H over the run ≥ 0 (on-hand alone lasts); or min R_b ≥ 0 → "OK · backed" (needs a truck, but every needed one lands ≥ 4 d before start — grey info) | none |
| **DELIVERY-DEPENDENT** | min R_b < 0 but min P ≥ 0: the run completes only thanks to a receipt landing < L before start or mid-run. Sub-text "mid-run" when the binding receipt lands after start | warning (orange) |
| **SHORT** | min P < 0: even with every counted receipt the line stops at depletion hour d (covered% = (d − start)/duration) | strong warning; BLOCKING in Reconcile like today's AT_RISK |
| **NO DATA** (modifier) | shortfall falls beyond the PO file's last receipt date, feed missing, or feed older than 7 d → grey "?" instead of SHORT | never alarms, never comforts |

Binding receipt = walk counted receipts latest-first, drop while P still ≥ 0; the first that cannot be dropped is binding; `lead = start − ready_binding`; `safe_from = ready_binding + L`. **Partial dependency**: the buffer applies only to receipts the timeline actually needs; on-hand covering the whole run makes tomorrow's truck irrelevant; dependent share < 5% is info-only. Stacked deliveries: each needed receipt has its own lead; the minimum is reported. Block verdict = worst item; every block spanning a shared crossing is flagged (locked/running blocks get the action "chase PO", movable ones "move to safe_from"). **Default is warn-only**: no drop, resize, place or edit is refused; `[stock] hard_block=true` rejects only SHORT with zero on-hand and zero inbound on a fresh feed whose window covers the run.

**Worked example — 280351, real board, hypothetical sleeves stock.** Item 754751 (1 EA/case, 2.16 kg/case). Suppose on-hand = 36,000 EA (real: 436,800) and one open PO 30043543 = 67,200 EA, Receipt date Wed 9/2 → ready 40 h (16:00).

| Block (board order) | Window | Cases | Hard balance at start | Hard covers | Receipt counted | Lead to start | Verdict (L = 4 d) | Verdict (L = 0) |
|---|---|---|---|---|---|---|---|---|
| P10 running MO `cs_87c620df4a` | 0.0–119.7 h | 29,283 (draw 25,667 after clipping at 14.8 h) | 36,000 | runs dry at 95.3 h (Fri 23:20) | PO 40 h (after start) | n/a (locked) | DELIVERY-DEPENDENT · mid-run · chase PO | DELIVERY-DEPENDENT · mid-run |
| P19 queued MO `cs_44c4bde023` | 71.85–131.5 h (Thu 23:51 →) | 41,435 | 22,046 | **39%** → 95.3 h | PO 40 h | **1.3 d** | **DELIVERY-DEPENDENT** · safe from Sun 9/6 16:00 | OK · backed |
| P10 tail `cs_87c620df4a;split` | 125.7–188.1 h | 15,258 | 0 (exhausted by the two above) | 0% | PO 40 h | 3.6 d | DELIVERY-DEPENDENT | OK · backed |

Receipts landing after a block's start can never make that block OK-backed at any L; they can only keep it out of SHORT. Planned end-of-board balance = 36,000 − 25,667 − 41,435 − 15,258 + 67,200 = **+20,840**, so nothing is SHORT. Move the PO to Sat 9/5 16:00 (112 h > 95.3 h) and both the running P10 MO and P19 become **SHORT** (P19 covered 39%); the P10 tail stays DELIVERY-DEPENDENT with a 13.7 h lead. Move P19 to start ≥ 136 h and it reads OK · backed. Remove the PO line (window ends 08-29, as in the real sample) and P19 reads "runs out at 39% — **no inbound data** past 08-29".

## 3. Data flow

**Ingestion** — `code/stockcheck/po_import.py::load_open_pos(path) -> PoResult` (openpyxl read_only/data_only, best-effort, errors collected; precedent `receiving_import`). Columns used: `Order number` (→ `po8` = last 8 digits), `Ordered item` (int → `str`, sentinel `?` row dropped first; suffix fallback `735009`→`735009-A` flagged), `Received item designation`, `Qty ordered at the origin`, the first `U` (five columns share that header), `Initial Receipt Date`, `Receipt date` (**authoritative**; slip shown as "re-promised +N d"), `Arrival area`, `Supplier`, `Receipt number` (non-blank = received → dropped). Join key `item` → BOM/stock key (54/57 plain, 730060/730061 via ediact 4, 899999 unjoinable → listed). Optional join to a dock appointment on `po8` ±1 d gives time-of-day (tier A); it confirms, never gates — except SL3 lines, which count only from a joined `SL3 TRANSFER` appointment (decision 5b). The xlsm is decaying and packaging-only.

**Inclusion gate per line**: key resolves, unit == BOM unit, qty > 0, date parses, unreceipted, `receipt_date ≥ date(S)` (older = overdue, listed never counted — avoids the 08-24-vs-09-01 double count: 43/79 sample lines were already in stock), feed usable (below). **Landed rule** (early arrival, the false-comfort direction — 3/10 sampled lines were physically in jestkexp before their ERP date with `Receipt number` still blank): a line is suppressed (listed as *landed*) when jestkexp/jestkexp2 lots of that item with decoded batch date ≥ `receipt_date − 3 d` sum to ≥ 95% of the PO qty (batch code `N`+year+DOY+seq; decodable for all packaging lots); items with S-/6-digit batches (aseptic purees, IQF) are shown as *landed: unverifiable* and still counted. **Ready hour** = receipt date 16:00 (packaging areas RP1) / +72 h QC offset for raw areas (RB1/AMB/RC1, decision 5); SL3 per decision 5b, else NO DATA. The report stores **ISO dates**; hours are computed per rerun in the calendar page with its `_anchor` (rolling-anchor safe).

**Where it lives**: `data/reference/open_pos.xlsx` via the bridge (`scripts/fs-live-data.conf.json files[]`, `.gitignore` Live-data block). Ask IT for a fixed filename (`open_pos.xlsx`). Until then, add a glob entry to `fs-live-data.conf.json` and matching glob support in **both** `scripts/fs-live-push.py` (work PC) and `scripts/fs-live-pull.py`, copying the newest match to `data/reference/open_pos.xlsx` — neither script has glob/rename logic today, so a dated IT filename is silently never pushed. `[datasources] po_report_path` override on Settings. **Freshness**: the inclusion gate is **content-based** — feed usable iff `max receipt date in file ≥ today` (plus an as-of cell if IT provides one), because the bridge skips byte-identical re-exports and mtime = pull time; the mtime-based 168 h rule and the `data_health` rows `open_pos` (26 h) and `receiving` (own row, 168 h — one line, fixes the Receiving tab's blind spot) are health-only. Semantic rule "PO window ends < today + 7 d" also health-only. Report signature includes the PO file, the receiving xlsm and `flowstate.toml`.

**Ask IT for**: fixed filename, an **explicit as-of timestamp** in the extract (header cell or column — mtime cannot supply it; the double-count and NO DATA rules depend on it), daily run **in the same job as the jestkexp export**, receipt window ≥ 5 weeks, columns `Qty received`/`Qty remaining`, item codes as plain integers, standing blanket POs (CAPS 30042838/30042798) included, arrival-area legend (RB1/RP1/AMB/SL3/RC1), and confirmation of when VIF backflushes consumption (at declaration or MO close).

## 4. Where Carsten sees it

All client surfaces are computed live by `utils/stockRisk.ts` from the shipped tables; server verdicts are only for Reconcile/Home/Stock Check on the **saved** board and are re-run in Python on "Refresh checks" (<50 ms, no BOM) so both converge. `stockEnabled = !!args.stock`; every stock surface renders nothing when the arg is absent (read-only mounts in `compare.py`/`generate.py`).

| Surface | Shows | Computes |
|---|---|---|
| Block face chip (`GanttBlock`) | `🚚 1.3d` orange · `⛔ 39%` red · `🚚` grey backed · `?` grey no-data; block body solid to the depletion hour, hatched after; tick where the binding truck lands | client, every edit |
| Drag badge (`DragPreviewBadge`) | orange row `🚚 754751: on hand covers 39% (runs out Fri 9/4 23:20) · PO 30043543 lands Wed 9/2 16:00 — 1.3 d before start (min 4 d) · safe from Sun 9/6 16:00`; ghost never turns red unless hard_block | client, per frame (dragged SKU only) |
| Post-commit banner | same sentence via `setWarnMsg` on all 10 commit paths incl. shifted blocks; `reject()` only under hard_block | client |
| Popover section | see mock below | client |
| Holding card | `🚚 from Sun 9/6 16:00` / `⛔ no clear start` / `? no data`; "after due window" when safe_from > `due_end_hour`; note "judged alone" | client (candidate starts = now ∪ {ready + L}) |
| SKU picker / holding-place rows | Chips-style pill + "Safe from" date per row; never disabled unless hard_block | client |
| Time-axis header | one 🚚 glyph per day with a tooltip (PO#, item, qty, tier); solid/hollow, "overdue N" pill and the no-data band are later ideas | client from payload |
| Week card | `supply ! N` / `supply ? N` + caption "stock 9/1 14:46 · POs 9/1 06:10" | client |
| Reconcile / Home | `supply:<block_id>@<ISO start>` (stable across anchor rolls, unlike `start_h`) WARN (dependent) / BLOCKING (short, fresh feed); one DATA finding `supply_feed`; **new plumbing**: `calendar.py` reads `st.query_params["focus"]` and passes a `focusBlock` arg (`types.ts` SandboxArgs, `gantt/__init__.py` kwarg); `GanttSandbox` seeds `highlightSku` from it on mount. The link opens the **saved-board** verdict; the popover shows the live one | server on Refresh/Home |
| Stock Check | new Inbound tab (every line with tier, used/excluded/landed reason), Supply column on schedule tab | server |

```
Supply · stock as of Tue 9/1 14:46 · POs as of Tue 9/1 06:10 (window to Fri 10/2)
754751 SLEEVE 1X24X90        need 41,435 EA
   on hand at start 22,046 → covers 39%, runs out Fri 9/4 23:20
   PO 30043543  +67,200 EA  Wed 9/2 16:00 (ERP date, GPI)   lead 1.3 d  < 4 d
   ▶ DELIVERY-DEPENDENT · safe from Sun 9/6 16:00
752420 FILM     need 22,748 M2 · covers 100%                         OK
730021 APPLE CONC  need 9,613 kg · covers 100%                        OK
… 18 more OK · untracked (never gate): 758001 ink, 758006 pallets, 758029 film
Also drawing 754751 in this window: 280351 P10 (running MO 30235, locked)
[Acknowledge for this session]
```

## 5. Architecture

**New**: `code/stockcheck/po_import.py`; `code/stockcheck/timeline.py` (pure, stdlib: `gate_receipts`, `build_timelines`, `evaluate_block`, `evaluate_board`, `earliest_clear_start`, `verdict_text`); `code/helpers/calendar_io.py::build_stock_payload(report, cfg, anchor)`; `frontend/src/utils/stockRisk.ts` (line-for-line port); `tests/test_po_import.py`, `test_timeline.py`, `test_stock_risk_parity.py`.

**Changed**: `vif_import.py` (`VIF_FILES += open_pos.xlsx`, `import_vif_folder` loaders dict += openpyxl loader — not the `_read_semicolon` path; `resolve_item_key`); `api.py` (`refresh_vif_snapshot` mtime guard picks the file up via `VIF_FILES` only; memoised unit recipes — also cuts the report from ~7 s to ~3 s; additive keys `inbound`, `sku_needs`, `quality`, `schedule_view[i].supply`, `demand_view[i].projected`; flat status untouched); `stock_report_cache.py` (`_VIF_FILES` is the **third** list that must name the file; signature + pass `anchor`/`stock_cfg`) — the PO file name must be stable for all three guards; `receiving_import.py` (+`po8`); `coverage.py` (export `tracked_items`); `reconcile_engine.py` (`open_po_path` resolver, supply findings); `data_health.py`; `config.py::stock_config`; `pages/calendar.py` (`load_cached` → payload → `gantt_calendar(stock=...)`; `focus` query param; Refresh re-evaluate); `components/gantt/__init__.py` (`stock=`, `focus_block=`); `pages/stock_check.py`, `settings.py`; `generate.py`/`agent_propose.py` routed through `stock_report_inputs`; `scripts/fs-live-push.py` + `scripts/fs-live-pull.py` (glob entry support). **Separate enabler commit before slice 2a — pre-existing split-piece bug**: `updateBlock`, `moveBlock` and `resizeBlock` (`useScheduleState.ts` L141-204) all patch every block sharing an id; fix all three by index/object identity (verdicts key on `id|start_hour`).

**Payload** `SandboxArgs.stock?: StockArgs` = `{asOf, poState, snapshotRmH, snapshotPkgH, receiptsHorizonEndH, rules, opening, tracked, units, receipts:{item:[{h,qty,po8,tier,label}]}, skuNeeds:{sku:{kgPerCase, items:[{item, perKg, alts}]}}, quality}` for the 79 demand+board SKUs (~100 KB against ~7 MB — noise; trimming `kpis.co_pairs` is a separate commit outside this plan). `DragPreview.supply` added to `samePreview`. Verdicts keyed by object/`id|start_hour`, never by id alone.

**Config** `flowstate.toml [stock]`: `min_days_after_delivery=4`, `lead_measured_from="block_start"` (alt `"depletion"`), `receipt_ready_hour=16`, `raw_qc_offset_h=72`, `appt_ready_offset_h=2`, `po_ignore_after_h=168`, `landed_match_frac=0.95`, `dependent_frac_floor=0.05`, `hard_block=false`, `use_board_qty_kg=true`; `[health] cadence_h open_pos=26, receiving=168`; `[datasources] po_report_path`. Nothing in `settings.json`, nothing in `attrs`.

**Speed**: report never runs from the calendar page; client full-board re-evaluation is <1 ms (~830 block-item pairs); drag frames evaluate one SKU.

## 6. Phased plan

| Slice | Deliverable | Carsten gets | Effort |
|---|---|---|---|
| **0 — See the feed** | `po_import` + tests, `po8`, bridge glob (both scripts)/gitignore/health/settings registration, Inbound tab with join stats, exclusion and landed reasons, `stock_config`; backflush check against a running MO's lots with `cases_left` as the cross-check | Proof IT's file parses and joins; concrete asks for IT | 1 d |
| **1 — Rule in the report and Reconcile** | `timeline.py` + goldens, memoised recipes, additive report keys, signature, supply findings + `supply_feed`, Stock Check Supply column | Any saved board inside the 4-day window is flagged on Home/Reconcile before the Gantt knows anything | 2 d |
| **2a — Placement moments** | split-piece fix (separate commit), payload builder + kwarg, `stockRisk.ts` + parity, drag row, commit banners, block chip/hatch, popover, week pill, Refresh re-evaluate, dist rebuilt | The 39%/truck/1.3 d story while dragging and after dropping | 2 d |
| **2b — Before placement** | holding badges, picker/holding-menu pills, one axis truck per day + tooltip, Reconcile→block focus (`focusBlock` plumbing) | Earliest safe date before choosing where a card goes | 1 d |
| **3 — Policy — outside this request, needs sign-off** | `demand_view.projected` drives per-order DNS trim; earliest-start constraint per order for `plan_fill`/solver/overnight | Solver stops planning inside the window and stops over-trimming later weeks | 1–2 d + discussion |

Later ideas (not planned): durable acknowledgements (`data/stockcheck/acks.json`); BBD expiry events; solid/hollow axis trucks, overdue pill, no-data band; `kpis.co_pairs` trim.

## 7. Decisions for the user

1. Buffer measured to **block start** (recommended, your sentence) or to the depletion moment? Both numbers always shown.
2. `min_days_after_delivery` = **4** for all items, or longer for imported fruit (RB1/AMB), whose Receipt date slipped 9–37 d in the sample?
3. Apply the rule to packaging and raw alike (**recommended: yes**, per-area readiness handles the difference).
4. Overdue open lines (date past, not receipted): **never counted, listed** (recommended) vs count as arriving today.
5. Readiness: packaging usable **16:00 on the receipt date**, raw **+72 h** for QC (recommended defaults) — confirm plant QC release times.
   5b. SL3 lines: count only from a joined `SL3 TRANSFER` dock appointment (**recommended**, else NO DATA), or treat like RP1 on the ERP date?
6. Which stock statuses count: keep **Ava only** (recommended); QC/QIN/TRL stay opt-in via toggles.
7. Hard-block: **no** (recommended); if ever, only zero-supply on a fresh feed.
8. Reconcile severity: DELIVERY-DEPENDENT WARN, SHORT BLOCKING on a fresh feed, NO DATA never (recommended).
9. Show grey "backed" trucks on delivery-covered blocks? **Yes, informational** (recommended); revisit after a week.
10. Receipts lift the solver's DNS trim per order and add an earliest-start constraint (slice 3)? **Recommended after slices 0–2 have run a week**, same sign-off class as the 2026-08-14 trim policy.
11. Untracked consumables (ink, CHEP pallets, stretch film): **keep never-gating**, or ask IT to export their stock?
12. Commit a trimmed real PO sample as a fixture (supplier names in git) or **synthetic only** (recommended)?
13. Bridge-only delivery under a fixed name (**recommended**) vs an upload box on the Data page?

## 8. Risks and honesty rules

- **Day-one surprise is real**: netting flags 752706 on the second 280591 P22 piece today although both read OK. The popover names the co-consuming block; the overnight brief lists verdict changes on the first run (keyed on `block_id@ISO start`, so anchor rolls do not churn them).
- **False comfort (the dangerous direction)**: a future-dated or same-day PO line that has already physically landed is counted on-hand **and** as a receipt. The landed rule catches packaging lots (N-format batches); aseptic purees/IQF (S-/6-digit batches) are only marked *unverifiable*. Mitigation beyond that needs IT's as-of timestamp and `Qty remaining`.
- The flag does **not** know: consumption profile within a run (assumed uniform); whether VIF backflushes at close (slice 0 checks); a truck on the yard un-receipted (conservative direction); partial/split receipts (received lines are dropped until IT adds `Qty remaining`); recurring blanket POs; competition between holding cards ("judged alone" on the card); the sample's window ends 08-29, so today most shortfalls read **NO DATA**, not SHORT.
- Snapshot hour S is a laptop pull time, late by up to ~35 min; running-MO clipping is off by rate × 35 min — small, captioned.
- Two kg conventions coexist until slice 3 (flat status on `rate × hours`, supply on board `qty_kg`, up to ±40% apart) — the popover says "based on this block's kg".
- A stale/expired feed can neither rescue nor alarm; a stale report is captioned, never hidden. Unknown SKU/rate/kg-per-case → grey "?", never green. Server (saved board) and client (live edits) verdicts can differ for the same block until save — the popover is the live one.
- Two ports of one rule; parity test + grep of new symbols in `dist/index.js` before commit (bundle drift is recurring).

## 9. Test strategy

- **Slice 0**: `test_po_import.py` synthetic xlsx (multi-line PO, sentinel row, five `U` headers, suffixed key, received vs open, 899999, garbage row); early-arrival landed-suppression test with synthetic N-format batches (≥95% match → landed; S-batch item → unverifiable); `test_data_health` touches the new files + stale/window cases; content-based freshness gate (unchanged re-export stays fresh; old max-date reads stale); `test_stock_report_cache` signature; bridge glob picks newest match. Browser: Inbound tab shows lines with reasons on a dev fixture.
- **Slice 1**: `test_timeline.py` goldens — the table above (39%/1.3 d → DEPENDENT; end-of-board **+20,840**; PO Sat → running P10 MO and P19 SHORT, P19 covered 39%, tail DEPENDENT with 13.7 h lead — every block spanning the crossing flagged; receipt landing mid-run of a running MO at L = 0 → DEPENDENT · mid-run, not OK; start ≥ 136 h → OK backed; window exhausted → NO DATA; stacked receipts; concurrent lines on one curve; running MO clipped at S with `cases_left` vs fallback; per-frame S_rm/S_pkg; alternate pooling; overdue excluded; expired feed); `test_reconcile` supply findings with ISO-start keys stable across a `rebase_calendar` roll; existing engine goldens unchanged. Browser: Reconcile lists the finding with the sentence.
- **Slice 2a/2b**: `test_stock_risk_parity.py` (shared JSON fixture under `data/test_fixtures/stock_risk/`, tsc→node like `test_sku_picker_math`); split-piece test for `updateBlock`/`moveBlock`/`resizeBlock`; read-only mounts render no stock surfaces when `args.stock` is absent. Browser (headed, per memory note): a **synthetic future-dated PO fixture** for a walkthrough with Carsten — drag the P19 block, see the orange row, drop, read the banner, click, see the popover, move to Sun 9/6 16:00 and watch it turn grey; picker row shows "Safe from"; `?focus=<block>` highlights on mount.
- **Slice 3** (outside this request): `test_agent_policy` per-order trim; solver earliest-start constraint test; demand-anchor vs board-anchor bucket test.