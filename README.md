# Flowstate

**Operational truth → Digital twin → Optimizer**

The hard part is not generating a Gantt chart. It is building a model that can say:

> "This schedule costs us 47.2 hours of manufacturing opportunity."

Once that exists, optimization is a mathematically solvable problem. Planners will not accept "the computer says do this," but they will accept:

> "Current (AZAP) schedule scores 62. Proposed balanced schedule scores 84 — show me why."

## Phases

| Phase | Question | Status in this repo |
| --- | --- | --- |
| **0 — Schedule Scorecard** | How good is this week's schedule? | Primary home page |
| **1 — Digital Twin** | If I move X, what happens to the score? | Plant Calendar (DnD) |
| **2 — Optimizer** | Which scenarios beat AZAP, and why? | Generate Scenarios |

CIP remains first-class. The solver is a **scenario generator**, not the home screen.

## Run

```bash
pip install -r requirements.txt
cd code/components/gantt/frontend && npm install && npm run build && cd -
python -m streamlit run code/app.py --server.headless true
```

## Data

- `data/calendar_blocks.csv` — unified plant calendar (production, CIP, maintenance, trial, contractor, line_down)
- `data/reference/` — changeovers, demand, CIP intervals, capabilities (scoring inputs)
- `data/scorecards/` — weekly score history
- `data/versions/` — named options with scorecard + pros/cons

## Legacy

The previous solver-first app lives in [`Flowstate-legacy/`](Flowstate-legacy/DEPRECATED.md) for reference and for lifting CP-SAT / Gantt code.
