# Flowstate Legacy (Deprecated)

This directory archives the original Flowstate application (solver-first Streamlit GUI + CP-SAT scheduler + Gantt sandbox).

**Do not use this as the active product.** The rebuilt Flowstate lives at the repository root.

## Why it was archived

The product thesis shifted from "optimizer first" to:

1. **Phase 0 — Schedule Scorecard** (operational truth model)
2. **Phase 1 — Digital Twin** (DnD what-if with live rescoring)
3. **Phase 2 — Optimizer** (scenario generator vs AZAP baseline)

## What to lift from here

| Asset | Use in new Flowstate |
| --- | --- |
| `code/components/gantt_sandbox/` | Phase 1 DnD calendar |
| `code/helpers/version_manager.py` | Version snapshots / compare |
| `code/phase2_scheduler.py`, `model_builder.py` | Phase 2 scenario generator |
| `data/Changeovers.csv`, CIP / demand CSVs | Scorecard reference inputs |
| Changeover typing in `gantt_viewer.py` | Scorecard changeover metrics |

## Running the legacy app (reference only)

```bash
cd Flowstate-legacy
python -m streamlit run code/app.py
```
