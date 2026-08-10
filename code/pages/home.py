# pages/home.py -- Home: the Command Center — process flow + data health + next actions.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers import data_health as dh
from helpers.data_catalog import CATALOG
from helpers.paths import data_dir
from helpers.process_flow import STAGES, stage_detail, stage_state

st.header("Flowstate")
st.caption("Operational truth -> digital twin -> optimizer. Start at step 1 and work down.")

dd = data_dir()
cfg = __import__("helpers.config", fromlist=["load_toml"]).load_toml()

# Cache health for ~60s so the page doesn't stat the disk on every rerun.
@st.cache_data(ttl=60, show_spinner=False)
def _health(data_dir_str: str) -> list[dict]:
    return [vars(h) for h in dh.assess(Path(data_dir_str), cfg)]

health = [dh.HealthStatus(**h) for h in _health(str(dd))]

# ---------------------------------------------------------------------------
# Freshness banner
# ---------------------------------------------------------------------------
counts = dh.summary(health)
n_bad = counts[dh.MISSING] + counts[dh.ERROR]
n_stale = counts[dh.STALE]
if n_bad:
    st.error(f"**{n_bad} data source(s) missing or unreadable**, {n_stale} stale — see below.")
elif n_stale:
    st.warning(f"**{n_stale} data source(s) stale** — see the pipeline and actions below.")
else:
    st.success(f"All data sources are present and fresh ({counts[dh.OK]} OK).")

# ---------------------------------------------------------------------------
# Pipeline diagram (inline SVG, dark-theme aware)
# ---------------------------------------------------------------------------
COLOR = {dh.OK: "#2e7d32", dh.STALE: "#f9a825", dh.MISSING: "#c62828",
         dh.ERROR: "#c62828", dh.NOT_APPLICABLE: "#616161"}
STATE_ICON = {dh.OK: "●", dh.STALE: "▲", dh.MISSING: "✕", dh.ERROR: "✕",
              dh.NOT_APPLICABLE: "·"}


def _svg_pipeline() -> str:
    n = len(STAGES)
    node_w, node_h, gap = 150, 72, 28
    total_w = n * node_w + (n - 1) * gap + 40
    total_h = node_h + 70
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{total_h}" '
        f'viewBox="0 0 {total_w} {total_h}">',
        '<style>.fs-node{cursor:pointer;} .fs-title{font:600 13px sans-serif; fill:#ffffff;} '
        '.fs-sub{font:10px sans-serif; fill:#b0b7c3;} .fs-state{font:10px sans-serif;} '
        '.fs-arrow{stroke:#7a8290; stroke-width:1.5;}</style>',
    ]
    for i, stage in enumerate(STAGES):
        x = 20 + i * (node_w + gap)
        y = 30
        state = stage_state(stage.id, health)
        color = COLOR[state]
        icon = STATE_ICON[state]
        parts.append(
            f'<g class="fs-node">'
            f'<rect x="{x}" y="{y}" width="{node_w}" height="{node_h}" rx="10" '
            f'fill="#1e2233" stroke="{color}" stroke-width="2.5"/>'
            f'<circle cx="{x + 16}" cy="{y + 18}" r="7" fill="{color}"/>'
            f'<text x="{x + 16}" y="{y + 22}" text-anchor="middle" class="fs-state" '
            f'fill="#ffffff">{icon}</text>'
            f'<text x="{x + 28}" y="{y + 22}" class="fs-title">{stage.title}</text>'
            f'<text x="{x + 12}" y="{y + 44}" class="fs-sub">{stage.subtitle[:34]}</text>'
            f'<text x="{x + 12}" y="{y + 60}" class="fs-sub" fill="{color}">{state}</text>'
            f'</g>'
        )
        if i < n - 1:
            ax = x + node_w
            ay = y + node_h / 2
            bx = ax + gap
            parts.append(
                f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{ay}" class="fs-arrow"/>'
                f'<polygon points="{bx},{ay} {bx - 7},{ay - 4} {bx - 7},{ay + 4}" fill="#7a8290"/>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


st.markdown(_svg_pipeline(), unsafe_allow_html=True)
st.caption("Colors: green = OK, amber = stale, red = missing/error. Open a stage with the buttons below.")

# ---------------------------------------------------------------------------
# Stage detail chips + deep links
# ---------------------------------------------------------------------------
st.subheader("Stage health")
cols = st.columns(len(STAGES))
for col, stage in zip(cols, STAGES):
    state = stage_state(stage.id, health)
    with col:
        st.markdown(f"**{stage.title}**")
        st.caption(stage_detail(stage.id, health))
        st.markdown(f":{'green' if state == dh.OK else 'orange' if state == dh.STALE else 'red'}[{state}]")
        st.page_link(stage.page, label="Open", icon=":material/arrow_forward:")

# ---------------------------------------------------------------------------
# What you need to do next
# ---------------------------------------------------------------------------
actions = dh.next_actions(health, limit=5)
st.subheader("What you need to do next")
if actions:
    for i, a in enumerate(actions, 1):
        st.markdown(f"{i}. {a}")
else:
    st.success("Nothing blocking right now. Score the week to snapshot history.")

# ---------------------------------------------------------------------------
# Data status table
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Data status")
st.caption(f"Data folder: `{dd}`")

rows = []
for h in health:
    rows.append({
        "Status": h.state,
        "Source": h.name,
        "Detail": h.detail,
        "Age": dh._fmt_age(h.age_h) if h.age_h is not None else "—",
        "Cadence": f"{h.cadence_h:g} h" if h.cadence_h is not None else "—",
    })
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
st.page_link("pages/data.py", label="Upload / edit data files", icon=":material/upload_file:")

st.divider()
with st.expander("About Flowstate"):
    st.markdown(
        """
The optimizer is maybe 20-30% of the project. The hard part is the **operational truth model**
that scores a schedule the same way every week.

| Phase | Question |
| --- | --- |
| **Schedule Scorecard** | How good is this week's line schedule? |
| **Plant Calendar** | If I move this block, what happens to the score? |
| **Generate Scenarios** | Which solver alternatives beat the baseline -- and why? |

AZAP (`data/reference/demand_plan.csv`) is the customer / corporate **demand plan**: which SKUs,
how many kg, which week. It is not a schedule -- it never assigns lines, sequence or equipment.
The line schedule (`data/calendar_blocks.csv`) is the plant's own, built by the production planner.

CIP stays critical. One planner enters production, maintenance, trials, contractor work, and line-downs.

The optimizer lives in `code/solver/` (CP-SAT). The old solver-first app
(`Flowstate-legacy/`) was removed 2026-08-10 — history is in git.
"""
    )
