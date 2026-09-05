# Supply Timeline — implementation contracts (2026-09-01)

Companion to `2026-09-01-po-stock-gantt-plan.md`. Every implementer codes to THIS file. Decisions taken at their
recommended defaults (buffer to block start, 4 d for all items, warn-only, raw +72 h, overdue never counted,
Ava-only, synthetic fixtures, bridge delivery `open_pos.xlsx`). Slice 3 (solver policy) is NOT built.

Conventions: Python is the source of truth; the TypeScript port copies names and semantics 1:1. All payload keys
are **snake_case** on both sides (precedent: `kpis.co_pairs`). Hours are floats in the board's storage frame
(`helpers.timefmt.planning_anchor(cfg)` = `[scheduler].planning_start_date`, hour 0). Dates → hours ONLY in Python
via `helpers.timefmt.datetime_to_hour(dt, anchor)`. Item codes are strings. Nothing is ever refused by default.

---

## 1. Config — `helpers/config.py::stock_config(cfg=None) -> dict`

Reads `[stock]` from flowstate.toml with defaults (mirror `scorecard_config`):

| key | default | meaning |
|---|---|---|
| `min_days_after_delivery` | 4 | buffer L = 24 × this, hours |
| `lead_measured_from` | `"block_start"` | or `"depletion"` (lead measured to the on-hand depletion hour) |
| `receipt_ready_hour` | 16 | packaging receipt usable from HH:00 local on its receipt date |
| `raw_qc_offset_h` | 72 | added to ready hour for raw arrival areas (RB1, AMB, RC1) |
| `appt_ready_offset_h` | 2 | when a dock appointment time is known: ready = appointment time + this |
| `po_ignore_after_h` | 168 | feed older than this (by content rule below) is `stale` |
| `landed_match_frac` | 0.95 | landed rule threshold |
| `dependent_frac_floor` | 0.05 | dependent share below this → `minor: true` (grey/info) |
| `hard_block` | false | if true, client `reject()`s only SHORT with zero on-hand & zero inbound on a fresh feed |
| `use_board_qty_kg` | true | block cases from board `qty_kg` when present, else rate × hours |
| `raw_areas` | `["RB1","AMB","RC1"]` | arrival areas that get the QC offset |
| `offsite_areas` | `["SL3"]` | count only from a joined `SL3 TRANSFER` appointment, else excluded (`reason="offsite_no_transfer"`) |

Also `[datasources] po_report_path` (default `""`) via `datasources_config`. `[health] cadence_h` gains
`open_pos = 26`, `receiving = 168` (see data_health section).

`flowstate.toml` gets a commented `[stock]` section with these defaults written out.

---

## 2. PO import — `code/stockcheck/po_import.py`

```python
@dataclass
class PoResult:
    lines: list[dict]          # PoLine dicts, see below (ALL rows incl. received/unjoinable — fate decided later)
    errors: list[str]          # non-fatal row/file problems
    source_path: str
    source_mtime: str          # "YYYY-MM-DD HH:MM:SS" local (VIF precedent) or ""
    as_of: str | None          # ISO date if the file carries one (header cell "As of"/"Report date"), else None
    max_receipt_date: str | None   # ISO date of the latest Receipt date in the file
    n_rows: int

def load_open_pos(path) -> PoResult        # never raises for a bad file: errors + empty lines
def po8(order_number) -> str               # last 8 digits of the ERP Order number ('2 02 1ACDE 30043537' -> '30043537'); '' if none
def decode_batch_date(batch) -> date|None  # 'N62420340' -> 2026-08-30 : N + year digit (2020+d) + DOY(3) + seq(4); case-insensitive; None otherwise
def resolve_item_key(code, bom_items: set[str]) -> str|None   # exact -> plain; else the single suffixed match 'code-X' if exactly one; else None
```

PoLine dict (all keys always present):
`{"po": "2 02 1ACDE 30043537", "po8": "30043537", "item": "754751", "designation": "SLV 24x90 ...", "qty": 67200.0,
"unit": "EA", "receipt_date": "2026-09-02", "initial_receipt_date": "2026-08-27", "slip_days": 6,
"arrival_area": "RP1", "supplier": "GRAPHIC PACKAGING", "supplier_id": "48", "received": false,
"order_date": "2026-07-24", "row": 12}` — strings stripped; `received = Receipt number non-blank`; dates ISO or None.

Parsing rules (verified on the sample): header row 1; openpyxl `read_only=True, data_only=True`; the sheet may end
with a sentinel row (`Ordered item` None or `?`) → skipped; five columns are literally named `U` → take the FIRST
`U` after `Qty ordered at the origin` by position; `Ordered item` may arrive as int/float → `str(int(x))`; empty
file / missing headers → `errors=["..."]`, `lines=[]`. Also accept `.csv` with the same headers (utf-8-sig or cp1252).

`receiving_import.parse_receiving_schedule` appointments gain `"po8": po8(appt["po"])` (multi-PO cells: first 8-digit
run; none → "").

---

## 3. Timeline — `code/stockcheck/timeline.py` (pure, stdlib only, no pandas)

### 3.1 Inputs

```python
DEFAULT_RULES = {...}   # same keys/defaults as stock_config §1 (rule keys only)

Block = {"key": str, "block_id": str, "sku": str, "line_name": str, "start_h": float, "end_h": float,
         "cases": float, "locked": bool, "running": bool, "cases_left": float|None}
# key = f"{block_id}@{start_h:.2f}"  (split MOs share block_id)

SkuNeeds = {sku: {"kg_per_case": float, "items": [{"item": str, "per_case": float, "unit": str, "alts": [str]}]}}
# per_case = need per ONE case from BomGraph.explode(sku, 1.0); explosion is linear in cases.

Opening = {item: qty}            # toggle-honouring on-hand (coverage.available_stock), pooled per item
Tracked = set[str]               # items with any stock row; untracked items are listed, never gate
InHouse = set[str]               # coverage.IN_HOUSE_ITEMS — never gate
Receipts = {item: [{"ready_h": float, "qty": float, "po8": str, "tier": "erp"|"appt", "receipt_date": "YYYY-MM-DD",
                    "label": str}]}      # ALREADY gated (see §4); sorted by ready_h
SnapshotH = {item: float}        # hour of the stock export the item's on-hand came from (rm vs pkg frame)
```

### 3.2 Draw model

For block b and recipe item i with `need = cases_b × per_case_i` (if `per_case_i == 0` → no draw):
- `S = snapshot_h.get(i, 0.0)`.
- If `end_b <= S`: draw 0 (already consumed and reflected in stock).
- If `start_b < S` (running when the stock was counted):
  - if `cases_left` is a number: `remaining = cases_left × per_case_i`, spread uniformly over `[S, end_b]`;
  - else `remaining = need × (end_b − S)/(end_b − start_b)`, spread uniformly over `[S, end_b]`.
- Else: `need` spread uniformly over `[start_b, end_b]`.
- Draw window `W_b = [max(start_b, S), end_b]`. Zero-length windows draw nothing.

Per-item curve `C_i(t) = opening_i − Σ draws_i(≤t)` (piecewise linear, continuous). Receipts are steps:
`+qty` at `ready_h` (applied AT `ready_h`, so evaluate the minimum just BEFORE each step as well as at breakpoints).

### 3.3 Requirement groups and pooling

For a SKU, group g = primary item + its `alts`. Pool curves by SUM over members: `H_g(t) = Σ_{i∈g} C_i(t)`.
Draws of an alt item by other SKUs (where it is primary) are already inside `C_alt`. Skip groups whose primary is in
`InHouse`. Groups where NO member is in `Tracked` → listed under `untracked`, not evaluated.

Three curves for block b, group g, over `W_b`:
- **Hard** `H(t)` — no receipts.
- **Planned** `P(t)` — H + all receipts of members with `ready_h ≤ t`.
- **Reliable** `R_b(t)` — H + only receipts with `ready_h ≤ ref_b − L`, where `ref_b = start_b` when
  `lead_measured_from == "block_start"`, else `ref_b = d_H` (hard depletion hour, or start_b if none).

`minH/minP/minR` = minimum over `W_b` evaluated at: both window ends, every draw breakpoint inside, and just before
(`t⁻`) and at (`t`) every receipt step inside.

### 3.4 Verdict per (block, group)

```
if minH >= 0:                       verdict = "OK",  backed = False
elif feed_state != "ok":            verdict = "NO_DATA"           # cannot know receipts
elif minR >= 0:                     verdict = "OK",  backed = True
elif minP >= 0:                     verdict = "DEPENDENT"
else:
    d_P = first t in W_b with P(t) < 0
    verdict = "NO_DATA" if d_P > receipts_window_end_h else "SHORT"
```
Extra fields:
- `d_H` (hard depletion hour or None), `covered_frac = clamp((d_H − start_b)/(end_b − start_b), 0, 1)` (1.0 if no
  depletion; for a running block use `start_b` as the block's true start), `d_P`, `covered_frac_planned` likewise.
- `binding`: walk counted receipts (those with `ready_h < end_b`) latest-first, tentatively dropping each while
  `minP` stays ≥ 0; the first that cannot be dropped is binding → `{"po8","qty","ready_h","receipt_date","label"}`
  or None (SHORT/NO_DATA may still report the latest counted receipt as `binding` for text).
- `lead_h = ref_b − binding.ready_h` (may be negative = mid-run), `mid_run = binding.ready_h > start_b`,
  `safe_from_h = binding.ready_h + L` (None when no binding).
- `dependent_frac = clamp(−minH / need_bg, 0, 1)` where `need_bg` = the block's draw of g inside `W_b`;
  `minor = verdict == "DEPENDENT" and dependent_frac < dependent_frac_floor`.

### 3.5 Block verdict

Rank: `SHORT(4) > DEPENDENT(3) > NO_DATA(2) > OK backed(1) > OK(0)`. `minor` DEPENDENT ranks as 1 for the block chip.
Output for a block:

```python
Supply = {
  "key": str, "verdict": "OK"|"DEPENDENT"|"SHORT"|"NO_DATA", "backed": bool, "minor": bool, "mid_run": bool,
  "item": str|None,                # the worst group's primary item
  "covered_frac": float|None,      # of the worst group (hard)
  "depletion_h": float|None,       # d_H of the worst group (or d_P for SHORT)
  "lead_h": float|None, "safe_from_h": float|None,
  "binding": {...}|None,
  "action": "none"|"move"|"chase_po",   # chase_po when locked/running, move otherwise, none for OK
  "items": [ {"item","designation"?,"unit","need","opening_at_start","minH","minP","minR","verdict","backed",
              "covered_frac","depletion_h","binding","lead_h","safe_from_h","dependent_frac","minor","mid_run"} ],
  "untracked": [items], "co_consumers": {item: [block keys drawing the same item inside W_b]},
  "text": str                      # one-line planner sentence, see verdict_text
}
```

### 3.6 API

```python
def gate_receipts(lines, *, bom_items, unit_by_item, snapshot_date, today, anchor, rules, stock_lots=None,
                  appts=None) -> tuple[dict, list[dict], dict]
    # returns (receipts_by_item, line_fates, feed_info)
    # line_fates: one dict per input line: {**line, "fate": "used"|"received"|"unjoinable"|"unit_mismatch"|
    #   "bad_qty"|"bad_date"|"overdue"|"landed"|"landed_unverifiable"(used)|"offsite_no_transfer", "reason": str,
    #   "ready_h": float|None, "tier": "erp"|"appt"|None, "item_key": str|None}
    # order of checks: received → key resolves (resolve_item_key) → unit == unit_by_item[key] → qty > 0 → date parses
    #   → receipt_date >= snapshot_date (else overdue) → landed rule → offsite rule → ready_h
    # landed rule: stock_lots = {item: [(batch, qty)]}; lots with decode_batch_date(batch) >= receipt_date − 3 d
    #   summing to >= landed_match_frac × qty → "landed" (excluded). Item whose lots have NO decodable batch → used,
    #   fate "landed_unverifiable". No stock_lots → skip the rule.
    # ready_h: appts = {po8: [{"date","time"}]} — if an appointment within ±1 d of receipt_date exists:
    #   ready = appt datetime + appt_ready_offset_h, tier "appt"; else ready = receipt_date @ receipt_ready_hour,
    #   tier "erp"; raw areas add raw_qc_offset_h; offsite areas require an appt whose category contains "SL3".
    # feed_info: {"state": "ok"|"missing"|"stale"|"empty", "receipts_window_end_h": float, "n_used": int, ...}
    #   state "stale" when max_receipt_date < today (content rule) ; "missing" when lines is None.

def build_timelines(blocks, sku_needs, opening, receipts, snapshot_h, *, tracked, in_house) -> Timelines
def evaluate_block(block, timelines, rules, feed_state, receipts_window_end_h) -> Supply
def evaluate_board(blocks, timelines, rules, feed_state, receipts_window_end_h) -> dict[key, Supply]
def earliest_clear_start(sku, cases, duration_h, timelines, rules, feed_state, receipts_window_end_h,
                         from_h, *, line_name="") -> {"safe_from_h": float|None, "verdict_now": Supply,
                                                      "verdict_at_safe": Supply|None}
    # candidates = sorted({from_h} ∪ {r.ready_h + L for receipts of the sku's recipe items with ready_h >= from_h − L});
    # first candidate whose virtual block (added to a COPY of the timelines) evaluates OK (plain or backed) wins.
def verdict_text(supply, anchor=None) -> str
    # "🚚 754751: on hand covers 39% (runs out Fri 9/4 23:20) · PO 30043543 lands Wed 9/2 16:00 — 1.3 d before start
    #  (min 4 d) · safe from Sun 9/6 16:00"; stamps via timefmt.hour_to_stamp when anchor given, else "h95.3".
```
`Timelines` is a plain dict (JSON-able) so the TS port can consume the same structure:
`{"items": {item: {"opening": q, "draws": [{"key","a","e","qty"}], "receipts": [...], "snapshot_h": S}},
"groups": {sku: [{"primary": item, "members": [items], "per_case": float, "unit": str}]}, "blocks": {key: Block}}`.

Golden fixture: `data/test_fixtures/stock_risk/cases.json` written by `scripts/gen_stock_risk_fixture.py`
(`{"rules": {...}, "cases": [{"name", "inputs": {blocks, sku_needs, opening, tracked, in_house, receipts,
snapshot_h, feed_state, receipts_window_end_h}, "expected": {key: Supply}}]}`) — the TS parity test replays it.
Cases: the plan's worked 280351/754751 table (L=96 and L=0), PO moved past the crossing (two SHORT + one
DEPENDENT), window exhausted → NO_DATA, stacked receipts, two concurrent blocks on one item, running block clipped
at S with cases_left vs fallback, alternate pooling, feed missing, minor dependent share, block fully before S.

---

## 4. Report — `code/stockcheck/api.py::stock_check_report` additive keys

Flat statuses, `demand_view`, `item_reverse` UNCHANGED. New top-level keys:

- `"anchor"`: ISO of `planning_anchor(load_toml())` used for every hour below.
- `"supply_meta"`: `{"rules", "snapshot_h": {item: h}, "snapshot_stamp": {"rm": "...", "pkg": "..."},
  "receipts_window_end_h", "feed_state", "opening": {item: qty}, "tracked": [...], "in_house": [...],
  "units": {item: unit}, "designations": {item: str}}` (items = union of recipe items of board+demand SKUs).
- `"sku_needs"`: SkuNeeds for board+demand SKUs (memoised `explode(sku, 1.0)`; also reuse it to cut report cost).
- `"inbound"`: `{"state", "source_path", "source_mtime", "as_of", "max_receipt_date", "n_rows", "errors",
  "lines": [line_fates], "receipts": Receipts, "join": {"used","received","overdue","landed","unjoinable",
  "unit_mismatch","offsite_no_transfer"}, "appt_join": {"matched": n, "total": n}}`.
- `"quality"`: `{"unjoinable_items": [...], "unit_mismatch": [...], "landed_unverifiable": [...]}`.
- each `schedule_view[i]` gains `"key"` and `"supply"` (Supply dict).

Inputs resolution (`helpers/reconcile_engine.py`):
`open_po_path(data_dir) -> Path|None` = `[datasources].po_report_path` → `data/reference/open_pos.xlsx` →
`data/reference/open_pos.csv` → None. `stock_report_inputs` unchanged (still `(vif, toggles)`).
`stock_check_report(..., po_path=None)` resolves via `open_po_path` when None.

Snapshot hour per frame: `snap.source_files["jestkexp.csv"]` / `["jestkexp2.csv"]` ("%Y-%m-%d %H:%M:%S" local) →
`datetime_to_hour(..., anchor)`; an item takes the frame it has rows in (rm first).

Cache (`helpers/stock_report_cache.py`): `current_signature` adds mtimes of `open_pos.xlsx`, `open_pos.csv`, the
receiving xlsm, and `flowstate.toml`. `vif_import.VIF_FILES` is NOT extended (the PO file is not a VIF export);
`refresh_vif_snapshot` unchanged.

---

## 5. Gantt payload — `helpers/calendar_io.py::build_stock_payload(report, cfg, page_anchor) -> dict|None`

Returns `None` when the report lacks `supply_meta` (old cache) → the page passes `stock=None` and the client renders
nothing (`stock_enabled = !!args.stock`). Otherwise (all hours shifted by `report_anchor − page_anchor`):

```ts
export interface StockArgs {
  feed_state: "ok" | "missing" | "stale" | "empty";
  as_of: { stock_rm: string; stock_pkg: string; po: string };   // display stamps
  receipts_window_end_h: number;
  rules: { min_days_after_delivery: number; lead_measured_from: "block_start" | "depletion";
           dependent_frac_floor: number; hard_block: boolean };
  opening: Record<string, number>;
  tracked: string[];
  in_house: string[];
  units: Record<string, string>;
  designations: Record<string, string>;
  snapshot_h: Record<string, number>;
  receipts: Record<string, { ready_h: number; qty: number; po8: string; tier: "erp" | "appt";
                              receipt_date: string; label: string }[]>;
  sku_needs: Record<string, { kg_per_case: number;
                              items: { item: string; per_case: number; unit: string; alts: string[] }[] }>;
  cases_left: Record<string, number>;      // order_id -> cases_left for running MOs (from manprg), may be {}
}
```
`SandboxArgs.stock?: StockArgs | null; SandboxArgs.focusBlock?: string | null;`
`gantt_calendar(..., stock=None, focus_block=None)` → kwargs `stock`, `focusBlock`.

Client block → timeline Block: `key = `${id}|${start_hour}``, `cases = qty_kg / kg_per_case` (fallback: rate ×
hours via `capabilities[line][sku]` / kg_per_case), `running = start_hour < snapshot_h(item)` per item (handled
inside the draw model), `cases_left = args.stock.cases_left[order_id]`, `locked = locked || completed || pinned ||
start_hour < locked_through_h`.

`frontend/src/utils/stockRisk.ts` exports `buildTimelines`, `evaluateBlock`, `evaluateBoard`,
`earliestClearStart`, `verdictText`, `supplyRank` with the SAME semantics and field names (snake_case fields inside
the returned objects). Parity test `tests/test_stock_risk_parity.py` compiles it with the frontend's tsc (precedent
`tests/test_sku_picker_math.py`) and replays `data/test_fixtures/stock_risk/cases.json`.

---

## 6. Reconcile — `helpers/reconcile_engine.py`

`supply_findings(report, anchor) -> list[Finding]` (called from `assess_plan` next to `stock_findings`):
- per block with `supply.verdict == "SHORT"` → `category=STOCK`, `severity=BLOCKING` if `feed_state == "ok"` else
  WARN; `key = f"supply:{block_id}@{hour_to_iso(start_h, anchor)}"`; message = `supply.text`; `page="pages/calendar.py"`,
  `context={"block_id", "sku", "line", "start_h", "focus": block_id}`.
- `DEPENDENT` (not minor) → WARN, same key scheme.
- `NO_DATA` → never a finding.
- one `key="supply_feed"` DATA finding: WARN when `inbound.state != "ok"` (missing/stale/empty) or when
  `max_receipt_date < today + 7 d` ("PO window ends …"), INFO otherwise (never blocking).
Unknown verdict values are ignored (forward-compatible).

---

## 7. Pages

- `pages/stock_check.py`: tab **Inbound** (every `inbound.lines` row: po8, item, designation, qty+unit, receipt date,
  slip, area, supplier, fate, reason, ready stamp, tier; metrics from `inbound.join`; source stamp; errors) and a
  **Supply** column on the schedule tab (`supply.verdict` + `supply.text`), plus `quality` on Data quality.
- `pages/calendar.py`: `load_cached(dd)` (never recompute) → `build_stock_payload` → `gantt_calendar(stock=...,
  focus_block=st.query_params.get("focus"))`; caption "Supply: N short · M dependent · stock 9/1 14:46 · POs 9/1 06:10"
  computed server-side with `evaluate_board` on the pushed working board on each rerun (<50 ms) — same numbers the
  client shows; stale-cache badge when `cached.stale`.
- `pages/settings.py`: `po_report_path` text input + source status line (cip_info precedent).
- `pages/data.py`: no upload box (decision 13).

---

## 8. Ingestion registration

- `scripts/fs-live-data.conf.json` `files` += `"open_pos.xlsx"`; entries may be glob patterns (e.g.
  `"NPA Open POs*.xlsx -> open_pos.xlsx"` syntax: `"<glob> -> <dest name>"`); BOTH `fs-live-push.py` and
  `fs-live-pull.py` resolve a glob entry to the newest match and copy under the dest name. Plain names unchanged.
- `.gitignore` Live-data block += `data/reference/open_pos.xlsx`, `data/reference/open_pos.csv`.
- `helpers/data_health.py`: rules `open_pos` (cadence 26 h; MISSING when no file; STALE by mtime age; extra detail
  when `max receipt date < today + 7 d` if cheap — optional) and `receiving` (own row, 168 h). Default cadences in
  `DEFAULT_CADENCE_H`; tests touch the new files in `_min_catalog`.

---

## 9. Frontend surfaces (slice 2a/2b) — all gated on `args.stock`

- Split-piece enabler (separate commit): `useScheduleState.updateBlock/moveBlock/resizeBlock` patch by index/object
  identity, never by id alone.
- `GanttBlock`: chip right of the label: `🚚 1.3d` (orange, DEPENDENT), `⛔ 39%` (red, SHORT), `🚚` grey (backed or
  minor), `?` grey (NO_DATA); body hatched after `depletion_h` when SHORT; tick at `binding.ready_h` when DEPENDENT.
- `DragPreviewBadge`: extra orange row = `verdict_text` for the dragged block at the preview position (dragged SKU
  only; computed per frame); `DragPreview.supply` participates in `samePreview`.
- Commit paths (drop, resize, place from holding, picker place, insert-shift, typed edit, split, fill, CIP tools):
  after commit, if the moved/created block's verdict is DEPENDENT or SHORT → `setWarnMsg(text)`; `reject()` only when
  `rules.hard_block` and SHORT with zero opening & zero inbound & feed ok.
- `BlockPopover`: "Supply" section (mock in the plan §4), `[Acknowledge for this session]` hides the chip for that key.
- Week card / KPI bar: `supply ! N` (short) `· ? M` (dependent) pill + caption with the as-of stamps.
- `HoldingArea` card: `🚚 from <stamp>` / `⛔ no clear start` / `? no data` via `earliestClearStart` (candidate
  duration from the card's kg and mean rate); note "after due window" when `safe_from_h > due_end_hour`.
- `SkuPickerPopover` + `HoldingPlacePopover` rows: same pill + "safe from" stamp.
- `TimeAxis`: one 🚚 glyph per day that has ≥1 receipt, tooltip lists `label` lines.
- `focusBlock`: on mount, if set and a block with that id exists → set `highlightSku` to its sku and scroll it into view.
- Rebuild: `cd code/components/gantt/frontend && npm run build`; commit `dist/`; grep `stockRisk`/`supply` symbols in
  `dist/index.js` before declaring done.
