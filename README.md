# Flowstate

**Operational truth → Digital twin → Optimizer**

The hard part is not generating a Gantt chart. It is building a model that can say:

> "This schedule costs us 47.2 hours of manufacturing opportunity."

Once that exists, optimization is a mathematically solvable problem. Planners will not accept "the computer says do this," but they will accept:

> "Current schedule scores 62. Proposed balanced schedule scores 84 - show me why."

## Phases

| Phase | Question | Status in this repo |
| --- | --- | --- |
| **0 — Schedule Scorecard** | How good is this week's schedule? | Primary home page |
| **1 — Digital Twin** | If I move X, what happens to the score? | Plant Calendar (DnD) |
| **2 — Optimizer** | Which scenarios beat the current schedule, and why? | Generate Scenarios |

CIP remains first-class. The solver is a **scenario generator**, not the home screen.

## Local setup (Windows)

```powershell
git clone https://github.com/spartacus242/Plant_Scheduler.git
cd Plant_Scheduler
git checkout main; git pull

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# One-time: silent Desktop shortcut (no console window) that starts/opens http://localhost:8501
powershell -ExecutionPolicy Bypass -File .\scripts\install_desktop_shortcut.ps1
```

Double-click **Flowstate** on your Desktop. The shortcut runs `scripts\open_flowstate.vbs`, which silently launches `scripts\open_flowstate.bat` — no console window. The launcher starts Streamlit from this clone if needed, then opens the browser.

When editing frontend Gantt source, rebuild:

```powershell
cd code\components\gantt\frontend
npm install
npm run build
cd ..\..\..\..
```

A prebuilt `dist/` is already committed, so rebuild is only required after TypeScript/React changes.

## Run (Linux / macOS / Cloud)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional: ./scripts/open_flowstate.sh
python3 -m streamlit run code/app.py --server.headless true
```

Open http://localhost:8501

## Push when done

```powershell
git checkout -b your-feature-branch
git add -A
git commit -m "Describe the change"
git push -u origin HEAD
```

Then open a PR into `develop` on GitHub.

## Look & feel

The app is a light workbench (see [`docs/ui-redesign-2026-09.md`](docs/ui-redesign-2026-09.md)). It opens on the **Plant Calendar**; the design tokens live in `.streamlit/config.toml`, `code/helpers/theme.py` and the Gantt's `src/utils/theme.ts` — change all three together.

## Data

- `data/calendar_blocks.csv` — unified plant calendar (production, CIP, maintenance, trial, contractor, line_down)
- `data/reference/` — changeovers, demand, CIP intervals, capabilities (scoring inputs)
- `data/scorecards/` — weekly score history
- `data/versions/` — named options with scorecard + pros/cons

## Legacy

The previous solver-first app lives in [`Flowstate-legacy/`](Flowstate-legacy/DEPRECATED.md) for reference and for lifting CP-SAT / Gantt code.
