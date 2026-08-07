# pages/calendar.py — Phase 1 Digital Twin: DnD what-if with live rescoring.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from components.gantt import gantt_calendar
from helpers.calendar_io import (
    calendar_to_gantt_payload,
    ensure_lines_from_calendar,
    gantt_payload_to_calendar,
    load_calendar,
    load_lines,
    save_calendar,
)
from helpers.config import load_toml
from helpers.downtime_ui import downtime_map_for_calendar, render_side_downtime_editor
from helpers.lines_model import expand_caps_with_groups, is_double, side_of, sides_of
from helpers.paths import data_dir, reference_dir
from helpers.scorecard_engine import ScorecardResult, delta_narrative, score_calendar
from helpers.scorecard_ui import render_delta_strip, render_scorecard
from helpers.version_manager import list_versions, save_version

st.header("Plant Calendar")
st.caption(
    "Phase 1 — Digital Twin. Move blocks and verify the scorecard changes. "
    "Still no auto-scheduling — you're building trust in the model."
)

dd = data_dir()
cal_path = dd / "calendar_blocks.csv"
cfg = load_toml()
sched_cfg = cfg.get("scheduler", {})

# Show which ISO calendar week(s) the horizon covers so hour offsets read as
# real weeks (WW33 ...) rather than abstract W0/W1.
from helpers.timefmt import planning_anchor, week_index_to_iso
_anchor = planning_anchor(cfg)
_w0 = week_index_to_iso(0, _anchor)
_w1 = week_index_to_iso(1, _anchor)
_w2 = week_index_to_iso(2, _anchor)
st.caption(
    f"Planning weeks: **WW{_w0:02d}** → WW{_w1:02d} / WW{_w2:02d} "
    f"(planning anchor {_anchor:%a %Y-%m-%d})."
)
cip_cfg = cfg.get("cip", {})

cal = load_calendar(cal_path)
if cal.empty:
    st.warning("No calendar yet. Import a schedule on the **Schedule Scorecard** page.")
    st.stop()

ensure_lines_from_calendar(cal, dd / "lines.csv")
lines_df = load_lines(dd / "lines.csv")
lines = lines_df.copy()
if "active" in lines.columns:
    lines = lines[lines["active"] != False]
_line_cols = [c for c in ("line_id", "line_name", "line_group", "side", "is_double") if c in lines.columns]
lines = lines[_line_cols].to_dict("records")
# Annotate every row with its group / side so the Gantt can split double lines
# into an A-over-B row even when lines.csv predates the A/B migration.
for _l in lines:
    _name = str(_l.get("line_name", ""))
    _l["line_group"] = str(_l.get("line_group") or "") or (_name[:-1] if side_of(_name) else _name)
    _l["side"] = side_of(_name) or str(_l.get("side") or "")
    _l["is_double"] = bool(is_double(_name))

# Capabilities map line_name -> sku -> rate
caps: dict = {}
caps_path = reference_dir(dd) / "capabilities_rates.csv"
if caps_path.exists():
    cdf = pd.read_csv(caps_path)
    for _, r in cdf.iterrows():
        if int(r.get("capable", 0) or 0) != 1:
            continue
        ln = str(r["line_name"])
        sku = str(r["sku"])
        rate = float(r.get("calc_rate_kgph") or r.get("nominal_rate_kgph") or 0)
        caps.setdefault(ln, {})[sku] = rate
# A double line's caps must be readable both per side (halved) and per group
# (both sides running), whichever way capabilities_rates.csv is keyed.
caps = expand_caps_with_groups(caps)

# STEP 1 of the workflow: scheduled downtime per side, entered before production.
with st.expander(
    "STEP 1 - Set scheduled downtime per side first, then schedule production",
    expanded=False,
):
    render_side_downtime_editor(dd, key_prefix="cal_dt")

side_downtime = {k: [[s, e] for s, e in v] for k, v in downtime_map_for_calendar(dd, cal).items()}
_one_sided = [g for g in sorted({str(l["line_group"]) for l in lines if l["is_double"]})
              if any(side_downtime.get(s) for s in sides_of(g))]
if _one_sided:
    st.info(
        "One-sided (half-rate) downtime recorded on: " + ", ".join(_one_sided) +
        ". Blocks dragged across those hours are stretched automatically."
    )

changeovers: dict = {}
co_path = reference_dir(dd) / "changeovers.csv"
if co_path.exists():
    codf = pd.read_csv(co_path)
    for _, r in codf.iterrows():
        changeovers.setdefault(str(r["from_sku"]), {})[str(r["to_sku"])] = float(r.get("setup_hours") or 0)

demand_targets = []
dem_path = reference_dir(dd) / "demand_plan.csv"
if dem_path.exists():
    ddf = pd.read_csv(dem_path)
    for _, r in ddf.iterrows():
        target = float(r.get("qty_target", 0) or 0)
        lo = float(r.get("lower_pct", 0.9) or 0.9)
        hi = float(r.get("upper_pct", 1.1) or 1.1)
        demand_targets.append({
            "order_id": str(r["order_id"]),
            "sku": str(r["sku"]),
            "qty_min": target * lo,
            "qty_max": target * hi,
        })

schedule, windows = calendar_to_gantt_payload(cal)

# ---- Live ops overlay: MO completion (manprg) + CIP schedule (cip_info) ----
from helpers.cip_import import read_cip_info
from helpers.config import datasources_config
from helpers.manprg_import import read_manprg
from helpers.ops_sql import SqlConfig, available as sql_available, fetch_current_mo

_ds = datasources_config(cfg)
_manprg_paths = [p.strip() for p in str(_ds.get("manprg_files", "")).split(";")
                 if p.strip()] or [
                     str(dd / "reference" / "manprg.txt"),
                     str(dd / "reference" / "manprg2.txt")]
_cip_path = str(_ds.get("cip_info_csv", "")).strip() or str(dd / "reference" / "cip_info.csv")

# completion % per line (file first; SQL override when configured+reachable)
_completion: dict[str, float] = {}
_now_running: list[dict] = []
_mp = read_manprg(_manprg_paths)
for line, lp in _mp.current.items():
    _completion[line] = lp.completion_pct
    _now_running.append({
        "line": line, "mo": lp.mo, "item": lp.item,
        "pct": lp.completion_pct, "left": lp.left_cas,
    })
_sql_cfg = SqlConfig(dsn=str(_ds.get("sql_dsn", "")),
                     enabled=bool(_ds.get("sql_enabled", False)))
if sql_available(_sql_cfg):
    _rows = fetch_current_mo(_sql_cfg)
    if _rows:
        for r in _rows:
            ln = str(r.get("Line", "")).strip()
            pct = r.get("MO Completion %")
            if ln and pct is not None:
                _completion[ln] = round(float(pct) * 100.0, 1)

# attach completion to production blocks (matched by line -> current MO item)
for b in schedule:
    if b.get("block_type") == "sku":
        ln = b.get("line_name", "")
        if ln in _completion:
            b["completion_pct"] = _completion[ln]

# scheduled CIPs from cip_info as overlay windows (drawn, not editable)
_cip = read_cip_info(_cip_path)
from helpers.timefmt import planning_anchor as _pa
_anchor = _pa(cfg)
for line, ci in _cip.by_line.items():
    if ci.scheduled_cip is not None:
        start_h = (ci.scheduled_cip - _anchor).total_seconds() / 3600.0
        dur = float(cip_cfg.get("duration_h", 6))
        windows.append({
            "id": f"cipinfo_{line}", "line_id": int(line[1:]) - 9 if line[1:].isdigit() else 0,
            "line_name": line, "order_id": "", "sku": "", "sku_description": "",
            "start_hour": start_h, "end_hour": start_h + dur,
            "run_hours": dur, "is_trial": False, "block_type": "cip",
            "label": "CIP (sched)", "locked": True,
        })

# Freeze the on-disk current schedule as baseline once per session (or after reload / save)
if "cal_baseline_score" not in st.session_state or st.session_state.get("cal_baseline_path") != str(cal_path):
    baseline_res = score_calendar(cal, week_label="current schedule", data_dir=dd)
    st.session_state["cal_baseline_score"] = baseline_res.to_dict()
    st.session_state["cal_baseline_path"] = str(cal_path)

if "cal_reset_gen" not in st.session_state:
    st.session_state["cal_reset_gen"] = 0

c1, c2, c3 = st.columns(3)
with c1:
    if st.button("Reload from disk", use_container_width=True):
        st.session_state["cal_reset_gen"] += 1
        st.session_state.pop("cal_holding", None)
        st.session_state.pop("cal_baseline_score", None)
        st.rerun()
with c2:
    save_name = st.text_input("Save as version name", value="Option 1", label_visibility="collapsed")
with c3:
    pass

# ---- Now-running table: current MO per line from manprg ----
if _now_running:
    st.subheader("Now running (live from manprg)")
    _desig = {line: lp.designation for line, lp in _mp.current.items()}
    _nr = sorted(_now_running, key=lambda r: r["line"])
    st.dataframe(
        [{"Line": r["line"], "MO": r["mo"], "SKU": r["item"],
          "Designation": _desig.get(r["line"], ""),
          "Completion": f"{r['pct']:.1f}%",
          "Cases left": int(r["left"])} for r in _nr],
        use_container_width=True, hide_index=True)

state = gantt_calendar(
    schedule=schedule,
    cip_windows=windows,
    capabilities=caps,
    changeovers=changeovers,
    demand_targets=demand_targets,
    lines=lines,
    holding_area=st.session_state.get("cal_holding", []),
    side_downtime=side_downtime,
    config={
        "planning_anchor": sched_cfg.get("planning_start_date", "2026-02-15 00:00:00"),
        "cip_duration_h": int(cip_cfg.get("duration_h", 6)),
        "min_run_hours": int(sched_cfg.get("min_run_hours", 4)),
        "horizon_hours": int(sched_cfg.get("horizon_hours", 336)),
    },
    height=820,
    key=f"gantt_calendar_{st.session_state['cal_reset_gen']}",
)

working = cal
holding = st.session_state.get("cal_holding", [])
if state and state.get("schedule") is not None:
    working = gantt_payload_to_calendar(state.get("schedule") or [], state.get("cipWindows") or [])
    holding = state.get("holdingArea") or []
    st.session_state["cal_holding"] = holding
    if state.get("lastAction"):
        st.caption(f"Last action: {state['lastAction']}")

n_holding = len(holding)
if n_holding:
    st.warning(
        f"{n_holding} block(s) are in the holding area and will **not** be written on Save. "
        "Restore them to a line first, or clear holding after saving."
    )

st.divider()
st.subheader("Live scorecard (same engine as Phase 0)")
live = score_calendar(working, week_label="what-if", data_dir=dd)

baseline_dict = st.session_state.get("cal_baseline_score") or {}
if baseline_dict:
    baseline = ScorecardResult.from_dict(baseline_dict)
    render_delta_strip(baseline, live, title="Δ vs current schedule (on disk)")
    for line in delta_narrative(baseline, live)[:8]:
        st.caption(line)

render_scorecard(live, show_formulas=False)

b1, b2 = st.columns(2)
with b1:
    if st.button("Save calendar to disk", type="primary", use_container_width=True):
        if n_holding:
            st.warning(f"Saving without {n_holding} held block(s). Holding area cleared.")
        save_calendar(working, cal_path)
        st.session_state["cal_holding"] = []
        st.session_state.pop("cal_baseline_score", None)
        st.success("Saved calendar_blocks.csv")
with b2:
    if st.button("Save as named version", use_container_width=True):
        if n_holding:
            st.warning(f"Version will omit {n_holding} held block(s).")
        try:
            slug = save_version(
                save_name or "Option",
                working,
                live.to_dict(),
                dd,
                source="digital_twin",
            )
            st.session_state["cal_holding"] = []
            st.success(f"Saved version `{slug}` — see Version Compare")
        except ValueError as e:
            st.error(str(e))

n_ver = len(list_versions(dd))
st.caption(f"{n_ver} / 5 versions saved")
