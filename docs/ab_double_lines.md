# A/B Double Lines (Bossar P17-P22)

## Domain truth

- **P09-P16 ("Volpak")** are single lines. Unchanged by this model.
- **P17-P22 ("Bossar")** are **double** lines: each is physically two parallel
  sides, `A` and `B` (P17A/P17B ... P22A/P22B).
- The two sides share common upstream and downstream equipment, so **both sides
  must always run the same SKU at the same time**.
- Either side can be down independently. With exactly **one side down the line
  runs at exactly half rate**; with both sides down it produces nothing.
  A double line running one-sided is effectively a single Volpak-speed line.
- Confirmed in the source data: P17 `nominal_rate_kgph=2246`, `calc_rate_kgph=1080`;
  P09 `nominal=1123`, `calc=540`. Exactly 2x. So **per-side rate = line rate / 2**.

## Data model decision

Per-side rows are **genuine rows**, not a derived view.

`data/lines.csv` gains three columns while keeping `line_id,line_name,active`
first, so every existing reader keeps working:

| column       | single line | double line          |
|--------------|-------------|----------------------|
| `line_name`  | `P09`       | `P17A` / `P17B`      |
| `line_group` | `P09`       | `P17`                |
| `side`       | *(empty)*   | `A` / `B`            |
| `is_double`  | `False`     | `True`               |

### line_id allocation

| range   | meaning                                                        |
|---------|----------------------------------------------------------------|
| `0..7`  | P09..P16, single lines, unchanged                               |
| `8..13` | **reserved** for the groups P17..P22 - never emitted as a row after migration, but kept mapped so historical `line_id` references still resolve to the right group |
| `100+`  | the per-side rows: P17A=100, P17B=101, P18A=102, ... P22B=111    |

Formula: `side_line_id(group, side) = 100 + 2*index(group) + index(side)`.

## Migration

```
python scripts/migrate_ab_lines.py --dry-run       # show what would change
python scripts/migrate_ab_lines.py                 # apply
python scripts/migrate_ab_lines.py --data <DIR>    # migrate another data dir
```

Rewritten files (each backed up to `data/_backups/<stem>.<timestamp>.csv`):

- `lines.csv` - P17..P22 expanded into A/B rows + the new columns.
- `reference/capabilities_rates.csv` - each double-line row duplicated into A and
  B rows with `nominal_rate_kgph` and `calc_rate_kgph` **halved**.
- `reference/line_cip_hrs.csv`, `reference/initial_states.csv`,
  `reference/downtimes.csv` - duplicated per side.

Not rewritten: `calendar_blocks.csv` and `reference/trials.csv`. A block whose
`line_name` is the group (e.g. `P17`) means "both sides", which stays valid.

The migration is **idempotent** (a second run is a no-op) and reversible from
the backups.

## Helper module

All A/B logic lives in `code/helpers/lines_model.py`. No other module should
re-derive it.

- `is_double(line)`, `group_of(line)`, `side_of(line)`, `sides_of(group)`
- `per_side_rate(line_rate, group)` / `whole_line_rate(side_rate, group)`
- `effective_rate(group, hour, downtime, full_rate)` - full rate when both sides
  up, half when exactly one is down, 0 when both are down
- `hours_for_qty(group, qty_kg, start_hour, downtime, full_rate)` - integrates
  capacity hour by hour and returns the hours required
- `qty_over_window(...)`, `downtime_from_rows(...)`, `expand_caps_with_groups(...)`

The same integration is mirrored in TypeScript in
`code/components/gantt/frontend/src/utils/abLines.ts` so the live drag preview
agrees with the backend.

## Workflow

**Step 1: set scheduled downtime per side.** Mark `P17A` (or `P17B`) down for a
time range on the Data Files page or the Plant Calendar.

**Step 2: schedule production.** When a block is dragged onto that line the tool
already knows which hours are one-sided and stretches the block accordingly.
