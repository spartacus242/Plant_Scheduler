# Plant Scheduler (Flowstate)

A 2-week factory scheduler using OR-Tools CP-SAT. Assigns SKU demand to production lines within due windows, honoring capability, rates (UPH), sequence-dependent changeovers, initial states, and Clean-In-Place (CIP) sanitation every 120 production hours.

## Features

- **2-week horizon** (336h): Week-0 (Mon–Sat) and Week-1 (Sun–Sat)
- **Two-phase mode** (`--two-phase`): Solve Week-0 first, then Week-1 with InitialStates from Week-0 — ensures Week-1 orders are scheduled even when qty_min=0
- **Rolling weekly process**: Autogenerate Week-1_InitialStates.csv from the schedule for use as next run's initial state
- **1-hour gap constraint**: Max 1h non-production (CIP/changeover only) between last Week-0 and first Week-1 run on each line
- **CIP blocks** placed in gaps between production (no overlap); CIP every 120 run-hours
- **Gantt viewer** (Streamlit): Color by SKU, data labels, click to highlight same SKU, changeovers table, 2-week view with Week boundary

## Quick start

```bash
# Create venv and install
python -m venv .venv
.venv\Scripts\activate
pip install ortools pandas streamlit plotly

# Run scheduler (single-phase)
python code/phase2_scheduler.py --data-dir data --min-run-hours 4 --time-limit 120

# Run two-phase (Week-0 then Week-1; shows Week-1 even when qty_min=0)
python code/phase2_scheduler.py --data-dir data --min-run-hours 4 --two-phase --time-limit 90

# Run Gantt viewer
streamlit run code/gantt_viewer.py
```

## Data inputs (`data/`)

| File | Description |
|------|-------------|
| Capabilities & Rates.csv | line_id, sku, capable, rate_uph |
| Changeovers.csv | from_sku, to_sku, setup_hours |
| InitialStates.csv | line_id, initial_sku, available_from_hour, carryover_run_hours_since_last_cip_at_t0, ... |
| DemandPlan.csv | order_id, sku, due_start_hour, due_end_hour, qty_target, lower_pct, upper_pct |
| Downtimes.csv (optional) | line_id, start_hour, end_hour |

## Outputs

- `schedule_phase2.csv` — per-line production timeline
- `produced_vs_bounds.csv` — produced vs qty_min/qty_max per order
- `cip_windows.csv` — CIP blocks (grey in Gantt)
- `Week-1_InitialStates.csv` — state at end of horizon (use with `--initial-states data/Week-1_InitialStates.csv` for next run)

## CLI options

| Option | Description |
|--------|-------------|
| `--data-dir PATH` | Data directory (default: script dir) |
| `--min-run-hours N` | Min run hours per (line, order) (default: 4) |
| `--two-phase` | Run Week-0 then Week-1 with Week-1 InitialStates from Week-0 |
| `--initial-states PATH` | Use custom InitialStates (e.g. Week-1_InitialStates.csv) |
| `--no-week1-in-week0` | Disallow Week-1 orders in Week-0 window |
| `--time-limit N` | Solver time limit (seconds) |
| `--diagnose` | Run diagnostics only |

## Project layout

```
Flowstate/
├── code/
│   ├── phase2_scheduler.py   # Main scheduler (CLI)
│   ├── model_builder.py      # CP-SAT model
│   ├── data_loader.py        # CSV loading, Params
│   ├── diagnostics.py        # Blockages, line load
│   └── gantt_viewer.py       # Streamlit Gantt chart
├── data/                     # Input CSVs + outputs
├── docs/
│   └── Factory_Scheduler_Summary.txt
└── README.md
```

## License

See repository for license details.
