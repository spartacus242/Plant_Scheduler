# Flowstate Scheduler

Two-week production scheduler for pouch manufacturing lines, built with
[OR-Tools CP-SAT](https://developers.google.com/optimization) and a
[Streamlit](https://streamlit.io) GUI.

## Quick Start

### Prerequisites

- **Python 3.11+** &mdash; [python.org/downloads](https://www.python.org/downloads/)
- **Git** &mdash; [git-scm.com](https://git-scm.com/)

### Clone & Run

```bash
# 1. Clone the repository
git clone https://github.com/spartacus242/Plant_Scheduler.git
cd Plant_Scheduler

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
#    Windows (PowerShell):
.venv\Scripts\Activate.ps1
#    Windows (CMD):
.venv\Scripts\activate.bat
#    macOS / Linux:
source .venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Launch the app
streamlit run code/app.py
```

The app opens in your browser at **http://localhost:8501**.

## What You'll See

| Section | Pages | Description |
|---------|-------|-------------|
| **Setup** | Settings, Demand Plan, Inventory Check | Configure the planning horizon, edit demand orders, review raw material coverage |
| **Configuration** | Capabilities & Rates, Changeovers, Trials, Downtimes, Initial States | Line-level parameters: which SKUs run where, changeover times, scheduled downtimes, current line states |
| **Solve** | Run Solver, Schedule Viewer | Run the OR-Tools optimizer and view the resulting schedule as a Gantt chart |
| **Adjust & Export** | Sandbox, Export | Drag-and-drop schedule adjustments, export to Excel/CSV |

## Running the Solver (CLI)

You can also run the solver directly from the command line:

```bash
# Full solve (changeovers + CIP modeling)
python code/phase2_scheduler.py --data-dir data --phase full --time-limit 120

# Quick feasibility check (no changeovers)
python code/phase2_scheduler.py --data-dir data --phase sanity1 --time-limit 30

# Two-phase solve (Week-0 then Week-1)
python code/phase2_scheduler.py --data-dir data --phase full --two-phase --time-limit 300
```

Output files are written to the `data/` directory:
- `schedule_phase2.csv` &mdash; the production schedule
- `produced_vs_bounds.csv` &mdash; actual vs. target quantities
- `cip_windows.csv` &mdash; CIP (clean-in-place) intervals

## Project Structure

```
Plant_Scheduler/
  code/
    app.py                  # Streamlit entry point
    phase2_scheduler.py     # CLI solver orchestration
    model_builder.py        # CP-SAT model construction
    data_loader.py          # Data loading and parameter management
    validate_schedule.py    # Post-solve validation
    diagnostics.py          # Feasibility diagnostics
    pages/                  # Streamlit GUI pages
    components/             # Custom Streamlit components (Gantt sandbox)
  data/
    DemandPlan.csv          # Demand orders
    Capabilities & Rates.csv
    Changeovers.csv         # SKU-to-SKU changeover matrix
    InitialStates.csv       # Current line states
    Downtimes.csv           # Scheduled maintenance windows
    ...
  flowstate.toml            # Solver configuration
  requirements.txt
```

## Recent Changes

### Gantt & UI Improvements
- **Sandbox block text truncation** — Block labels are now truncated in
  JavaScript to fit within the colored rect.  Eliminates SVG text overflow
  that caused highlight borders to stretch past the block area.
- **Legend click = highlight** — Single-clicking a SKU in the Plotly legend
  now isolates it (dims all others) instead of hiding the trace.  Double-click
  toggles individual traces.
- **CIP blocks on Option Gantt charts** — Saved versions now display CIP
  intervals on their Gantt charts (previously passed `cip_df=None`).
- **CIP rows in schedule tables** — CIP blocks appear inline in the schedule
  summary tables on both the Schedule Viewer and the Option expander pages.

### Changeover KPIs
- **Bug fix: `0 or 1` truthiness** — `compute_changeover_details` was using
  `int(val or 1)`, which converted `0` (a valid "no change" flag) to `1`.
  Every changeover was counted as TTP, FFS, Topload, *and* Casepacker.
  Replaced with a `_flag()` helper that distinguishes `0` from `NaN`.
- **Changeover metrics above sandbox** — A new KPI row above the React
  Gantt component shows total changeovers and per-type counts (TTP, FFS,
  Topload, Casepacker, Conv→Org, Cinn→Non) with an expandable per-line table.

### CIP Absorption of Conv→Org / Cinn→Non Penalties
- **Solver model** (`model_builder.py`) — When a CIP interval falls between
  two adjacent orders on a line, the conv_to_org and cinn_to_non weighted
  penalties are waived via a bonus term in the objective.  The solver
  naturally places CIPs to take advantage of this when beneficial.
- **KPI counting** (`gantt_viewer.py`) — `compute_changeover_details` now
  accepts an optional `cip_df` and skips conv_to_org / cinn_to_non for
  changeovers where a CIP sits between the two production blocks.
- **All callers updated** — Sandbox (main + version expanders) and
  Schedule Viewer pass CIP data for accurate KPI display.

## Configuration

Edit `flowstate.toml` to tune solver behavior:

```toml
[scheduler]
time_limit = 300          # Solver time limit (seconds)
min_run_hours = 8         # Minimum production run per line assignment

[cip]
interval_h = 120          # CIP required every 120 production hours
duration_h = 6            # CIP duration

[objective]
makespan_weight = 6       # Minimize total schedule length
changeover_weight = 120   # Penalize changeovers
idle_weight = 50          # Penalize idle gaps

[changeover]
topload_weight = 50       # Topload format changes (heaviest penalty)
ttp_weight = 10           # TTP station changes
ffs_weight = 10           # Form-fill-seal changes
casepacker_weight = 10    # Casepacker changes
base_changeover_weight = 5  # Flat cost per any transition
```
