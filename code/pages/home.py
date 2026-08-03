# pages/home.py — Product thesis.

from __future__ import annotations

import streamlit as st

st.header("Flowstate")
st.markdown(
    """
### Operational truth → Digital twin → Optimizer

The optimizer is maybe 20–30% of the project. The hard part is the **operational truth model**
that scores a schedule the same way every week.

| Phase | Question |
| --- | --- |
| **Schedule Scorecard** | How good is this week's (AZAP) schedule? |
| **Plant Calendar** | If I move this block, what happens to the score? |
| **Generate Scenarios** | Which solver alternatives beat the baseline — and why? |

CIP stays critical. One planner enters production, maintenance, trials, contractor work, and line-downs.

Legacy solver-first app: `Flowstate-legacy/` (deprecated, kept for reference).
"""
)
