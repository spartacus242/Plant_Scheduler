# pages/sandbox.py — Interactive sandbox for manual schedule editing.
#
# Uses Plotly Gantt for visualization with Streamlit form-based interactions
# for move, resize, split, and drag-to/from holding area.  KPIs and the SKU
# adherence table update live.  Architecture is ready for a future custom
# React component upgrade (all logic is in helpers/sandbox_engine.py).

from __future__ import annotations
import json
import math
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from helpers.paths import data_dir
from helpers.sandbox_engine import (
    load_capabilities,
    load_changeovers,
    load_demand_targets,
    is_capable,
    get_rate,
    recalc_duration,
    compute_adherence,
    overall_adherence,
    count_changeovers,
    check_overlaps,
    split_block,
    save_sandbox_to_files,
)

st.header("Sandbox")
st.caption("Interactively adjust the schedule.  Changes do not affect files until you Save.")

dd = data_dir()

# ── Load reference data ─────────────────────────────────────────────────
caps = load_capabilities(dd)
changeovers = load_changeovers(dd)
demand = load_demand_targets(dd)
line_names_set = sorted({ln for ln, _ in caps.keys()})

# ── Initialize sandbox state from solver output ─────────────────────────
def _load_schedule_blocks() -> list[dict]:
    path = dd / "schedule_phase2.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    blocks = []
    for _, r in df.iterrows():
        blocks.append({
            "line_id": int(r.get("line_id", 0)),
            "line_name": str(r.get("line_name", "")),
            "order_id": str(r.get("order_id", "")),
            "sku": str(r.get("sku", "")),
            "start_hour": float(r.get("start_hour", 0)),
            "end_hour": float(r.get("end_hour", 0)),
            "run_hours": float(r.get("run_hours", 0)),
            "is_trial": bool(r.get("is_trial", False)),
            "block_type": "trial" if bool(r.get("is_trial", False)) else "sku",
        })
    return blocks


def _load_cip_blocks() -> list[dict]:
    path = dd / "cip_windows.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    blocks = []
    for _, r in df.iterrows():
        blocks.append({
            "line_id": int(r.get("line_id", 0)),
            "line_name": str(r.get("line_name", "")),
            "order_id": "CIP",
            "sku": "CIP",
            "start_hour": float(r.get("start_hour", 0)),
            "end_hour": float(r.get("end_hour", 0)),
            "run_hours": float(r.get("end_hour", 0)) - float(r.get("start_hour", 0)),
            "is_trial": False,
            "block_type": "cip",
        })
    return blocks


if "sb_schedule" not in st.session_state:
    st.session_state["sb_schedule"] = _load_schedule_blocks()
    st.session_state["sb_cips"] = _load_cip_blocks()
    st.session_state["sb_holding"] = []

schedule: list[dict] = st.session_state["sb_schedule"]
cip_blocks: list[dict] = st.session_state["sb_cips"]
holding: list[dict] = st.session_state["sb_holding"]

if not schedule and not cip_blocks:
    st.info("No schedule loaded. Run the solver or navigate from Schedule Viewer.")
    st.stop()

# ── KPI bar ─────────────────────────────────────────────────────────────
adherence_rows = compute_adherence(schedule, demand, caps)
pct_overall = overall_adherence(adherence_rows)
total_co, per_line_co = count_changeovers(schedule)
overlaps = check_overlaps(schedule + cip_blocks)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Demand Adherence", f"{pct_overall}%")
k2.metric("Orders MET", f"{sum(1 for r in adherence_rows if r['status']=='MET')}/{len(adherence_rows)}")
k3.metric("Total Changeovers", total_co)
k4.metric("Overlaps", len(overlaps), delta="OK" if not overlaps else "Fix!", delta_color="off" if not overlaps else "inverse")

# ── Gantt visualization ─────────────────────────────────────────────────
def _build_sandbox_gantt() -> go.Figure:
    anchor = pd.Timestamp("2026-02-15 00:00:00")
    rows = []
    for blk in schedule + cip_blocks:
        s = anchor + pd.Timedelta(hours=float(blk["start_hour"]))
        e = anchor + pd.Timedelta(hours=float(blk["end_hour"]))
        bt = blk.get("block_type", "sku")
        label = "CIP" if bt == "cip" else f"{blk['sku']}\n{int(blk['run_hours'])}h"
        rows.append({
            "Start": s, "Finish": e,
            "Line": blk["line_name"],
            "SKU": blk["sku"] if bt != "cip" else "CIP",
            "label": label,
            "type": bt,
            "order_id": blk.get("order_id", ""),
        })
    if not rows:
        return go.Figure()
    df = pd.DataFrame(rows)
    line_order = sorted(df["Line"].unique().tolist())
    color_map = {}
    sku_list = [s for s in df["SKU"].unique() if s != "CIP"]
    palette = px.colors.qualitative.Plotly
    for i, s in enumerate(sku_list):
        color_map[s] = palette[i % len(palette)]
    color_map["CIP"] = "#888888"

    fig = px.timeline(df, x_start="Start", x_end="Finish", y="Line",
                      color="SKU", color_discrete_map=color_map, text="label")
    fig.update_traces(textposition="inside", textfont_size=12, insidetextanchor="middle")
    fig.update_layout(
        height=max(350, 30 * len(line_order)),
        yaxis=dict(categoryorder="array", categoryarray=line_order),
        bargap=0.15, barmode="overlay", showlegend=True,
    )
    fig.update_yaxes(autorange="reversed")
    # Week boundary
    w1 = anchor + pd.Timedelta(hours=168)
    fig.add_shape(type="line", x0=w1, x1=w1, y0=0, y1=1, xref="x", yref="paper",
                  line=dict(dash="dash", color="gray"))
    return fig

st.plotly_chart(_build_sandbox_gantt(), use_container_width=True, key="sb_gantt")

# ── Action panel ────────────────────────────────────────────────────────
st.divider()
action_tabs = st.tabs(["Move Block", "Resize Block", "Split Block", "Add CIP", "Holding Area"])

# ── Move Block ──────────────────────────────────────────────────────────
with action_tabs[0]:
    st.caption("Move a production block to a different line (rate recalculates).")
    block_labels = [f"{b['line_name']} | {b['order_id']} | {b['sku']} (h{int(b['start_hour'])}–{int(b['end_hour'])})"
                    for b in schedule]
    if block_labels:
        sel_idx = st.selectbox("Select block", range(len(block_labels)), format_func=lambda i: block_labels[i], key="mv_sel")
        target_line = st.selectbox("Target line", line_names_set, key="mv_line")
        if st.button("Move", key="mv_go"):
            blk = schedule[sel_idx]
            sku = blk["sku"]
            if not is_capable(target_line, sku, caps):
                st.error(f"Line `{target_line}` cannot produce SKU `{sku}`.")
            else:
                new_dur = recalc_duration(blk, target_line, caps)
                if new_dur is None:
                    st.error("Cannot compute duration for target line.")
                else:
                    # Find line_id for target
                    lid = next((lid for (ln, _), lid in [((ln, s), blk["line_id"]) for ln, s in caps.keys()] if ln == target_line), blk["line_id"])
                    for _, r in pd.read_csv(dd / "Capabilities & Rates.csv").iterrows():
                        if str(r["line_name"]) == target_line:
                            lid = int(r["line_id"])
                            break
                    blk["line_name"] = target_line
                    blk["line_id"] = lid
                    blk["end_hour"] = blk["start_hour"] + new_dur
                    blk["run_hours"] = new_dur
                    st.session_state["sb_schedule"] = schedule
                    st.success(f"Moved to `{target_line}`. Duration: {new_dur}h")
                    st.rerun()

# ── Resize Block ────────────────────────────────────────────────────────
with action_tabs[1]:
    st.caption("Resize a block (CIP, trial, or SKU). SKU resizes update demand adherence.")
    all_blocks = schedule + cip_blocks
    all_labels = [f"{'CIP' if b.get('block_type')=='cip' else b['sku']} on {b['line_name']} (h{int(b['start_hour'])}–{int(b['end_hour'])})"
                  for b in all_blocks]
    if all_labels:
        rs_idx = st.selectbox("Select block", range(len(all_labels)), format_func=lambda i: all_labels[i], key="rs_sel")
        blk = all_blocks[rs_idx]
        new_hrs = st.number_input("New run hours", min_value=1, value=int(blk["run_hours"]), step=1, key="rs_hrs")
        if st.button("Resize", key="rs_go"):
            blk["end_hour"] = blk["start_hour"] + new_hrs
            blk["run_hours"] = new_hrs
            if rs_idx < len(schedule):
                st.session_state["sb_schedule"] = schedule
            else:
                st.session_state["sb_cips"] = cip_blocks
            st.success(f"Resized to {new_hrs}h")
            st.rerun()

# ── Split Block ─────────────────────────────────────────────────────────
with action_tabs[2]:
    st.caption("Split a SKU block into two segments. seg_b can then be moved to another line.")
    sku_blocks = [b for b in schedule if b.get("block_type") != "cip"]
    sku_labels = [f"{b['order_id']} | {b['sku']} on {b['line_name']} (h{int(b['start_hour'])}–{int(b['end_hour'])})"
                  for b in sku_blocks]
    if sku_labels:
        sp_idx = st.selectbox("Select block to split", range(len(sku_labels)), format_func=lambda i: sku_labels[i], key="sp_sel")
        blk = sku_blocks[sp_idx]
        mid = int(blk["start_hour"] + blk["run_hours"] / 2)
        split_at = st.number_input("Split at hour", min_value=int(blk["start_hour"]) + 4,
                                   max_value=int(blk["end_hour"]) - 4, value=mid, step=1, key="sp_hr")
        if st.button("Split", key="sp_go"):
            result = split_block(blk, split_at)
            if result is None:
                st.error("Each segment must be at least 4 hours.")
            else:
                seg_a, seg_b = result
                orig_idx = schedule.index(blk)
                schedule[orig_idx] = seg_a
                schedule.insert(orig_idx + 1, seg_b)
                st.session_state["sb_schedule"] = schedule
                st.success(f"Split into seg_a (h{int(seg_a['start_hour'])}–{int(seg_a['end_hour'])}) "
                           f"and seg_b (h{int(seg_b['start_hour'])}–{int(seg_b['end_hour'])}). "
                           f"Use Move Block to relocate seg_b.")
                st.rerun()

# ── Add CIP ─────────────────────────────────────────────────────────────
with action_tabs[3]:
    st.caption("Drop a CIP block onto a line.")
    cip_line = st.selectbox("Line", line_names_set, key="cip_ln")
    cip_start = st.number_input("Start hour", min_value=0, max_value=336, value=162, step=1, key="cip_sh")
    cip_dur = st.number_input("Duration (hours)", min_value=1, max_value=24, value=6, step=1, key="cip_dur")
    if st.button("Add CIP", key="cip_go"):
        lid = 0
        for _, r in pd.read_csv(dd / "Capabilities & Rates.csv").drop_duplicates("line_name").iterrows():
            if str(r["line_name"]) == cip_line:
                lid = int(r["line_id"])
                break
        cip_blocks.append({
            "line_id": lid, "line_name": cip_line,
            "order_id": "CIP", "sku": "CIP",
            "start_hour": cip_start, "end_hour": cip_start + cip_dur,
            "run_hours": cip_dur, "is_trial": False, "block_type": "cip",
        })
        st.session_state["sb_cips"] = cip_blocks
        st.success(f"Added CIP on `{cip_line}` at h{cip_start}–{cip_start + cip_dur}")
        st.rerun()

# ── Holding Area ────────────────────────────────────────────────────────
with action_tabs[4]:
    st.caption("Remove blocks from the schedule or restore them.")
    col_rem, col_res = st.columns(2)
    with col_rem:
        st.markdown("**Remove from schedule**")
        rem_labels = [f"{b['order_id']} | {b['sku']} on {b['line_name']} (h{int(b['start_hour'])}–{int(b['end_hour'])})"
                      for b in schedule]
        if rem_labels:
            rem_idx = st.selectbox("Select block", range(len(rem_labels)), format_func=lambda i: rem_labels[i], key="rem_sel")
            if st.button("Remove to holding", key="rem_go"):
                blk = schedule.pop(rem_idx)
                holding.append(blk)
                st.session_state["sb_schedule"] = schedule
                st.session_state["sb_holding"] = holding
                st.rerun()
    with col_res:
        st.markdown("**Restore from holding**")
        if holding:
            hold_labels = [f"{b['order_id']} | {b['sku']} (was {b['line_name']} h{int(b['start_hour'])}–{int(b['end_hour'])})"
                           for b in holding]
            res_idx = st.selectbox("Select block", range(len(hold_labels)), format_func=lambda i: hold_labels[i], key="res_sel")
            res_line = st.selectbox("Restore to line", line_names_set, key="res_ln")
            res_start = st.number_input("Start hour", min_value=0, max_value=336, value=int(holding[res_idx]["start_hour"]), key="res_sh")
            if st.button("Restore", key="res_go"):
                blk = holding.pop(res_idx)
                sku = blk["sku"]
                if not is_capable(res_line, sku, caps):
                    st.error(f"Line `{res_line}` cannot produce `{sku}`.")
                    holding.insert(res_idx, blk)
                else:
                    new_dur = recalc_duration(blk, res_line, caps)
                    if new_dur is None:
                        new_dur = blk["run_hours"]
                    for _, r in pd.read_csv(dd / "Capabilities & Rates.csv").drop_duplicates("line_name").iterrows():
                        if str(r["line_name"]) == res_line:
                            blk["line_id"] = int(r["line_id"])
                            break
                    blk["line_name"] = res_line
                    blk["start_hour"] = res_start
                    blk["end_hour"] = res_start + new_dur
                    blk["run_hours"] = new_dur
                    schedule.append(blk)
                    st.session_state["sb_schedule"] = schedule
                    st.session_state["sb_holding"] = holding
                    st.rerun()
        else:
            st.info("Holding area is empty.")

# ── SKU Adherence Table ─────────────────────────────────────────────────
st.divider()
st.subheader("SKU Adherence")
if adherence_rows:
    adh_df = pd.DataFrame(adherence_rows)
    # Color the status column
    def _status_color(val: str) -> str:
        if val == "MET":
            return "background-color: #d4edda"
        elif val == "UNDER":
            return "background-color: #f8d7da"
        elif val == "OVER":
            return "background-color: #fff3cd"
        return ""

    styled = adh_df.style.map(_status_color, subset=["status"])
    st.dataframe(
        styled,
        use_container_width=True,
        height=min(500, 35 * len(adh_df) + 50),
        column_config={
            "pct_adherence": st.column_config.ProgressColumn("% Adherence", min_value=0, max_value=100, format="%.1f%%"),
        },
    )
else:
    st.info("No demand data loaded.")

# ── Overlap warnings ────────────────────────────────────────────────────
if overlaps:
    st.divider()
    st.error("**Overlaps detected:**")
    for o in overlaps:
        st.write(f"- {o}")

# ── Save / Reset ────────────────────────────────────────────────────────
st.divider()
col_save, col_reset, col_export = st.columns(3)

with col_save:
    if st.button("Save to Schedule", type="primary", use_container_width=True):
        st.session_state["sb_confirm_save"] = True

    if st.session_state.get("sb_confirm_save"):
        st.warning(
            "This will overwrite `schedule_phase2.csv` and `cip_windows.csv` "
            "with your sandbox changes. The solver solution will be replaced. "
            "You will need to re-run the solver to restore the optimized schedule."
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Confirm save", type="primary"):
                save_sandbox_to_files(schedule, cip_blocks, caps, demand, dd)
                st.session_state["schedule_source"] = "sandbox"
                st.session_state.pop("sb_confirm_save", None)
                st.success("Schedule saved to files.")
        with c2:
            if st.button("Cancel"):
                st.session_state.pop("sb_confirm_save", None)
                st.rerun()

with col_reset:
    if st.button("Reset from solver", use_container_width=True):
        st.session_state["sb_schedule"] = _load_schedule_blocks()
        st.session_state["sb_cips"] = _load_cip_blocks()
        st.session_state["sb_holding"] = []
        st.rerun()

with col_export:
    if st.button("Go to Export", use_container_width=True):
        st.switch_page("pages/export.py")
