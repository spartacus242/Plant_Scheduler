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
    drop_display_overlays,
    ensure_lines_from_calendar,
    gantt_payload_to_calendar,
    load_calendar,
    load_lines,
    save_calendar,
)
from helpers.week_lock import (
    default_lock_through,
    locked_through_h,
    read_lock,
    write_lock,
)
from helpers.config import load_toml
from helpers.downtime_ui import downtime_map_for_calendar, render_side_downtime_editor
from helpers.lines_model import expand_caps_with_groups, is_double, side_of, sides_of
from helpers.paths import data_dir, reference_dir
from solver.changeover_cache import load_changeover_setup_nested
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

# Rolling horizon (handoff WW32 #1): the calendar starts at TODAY. The stored
# anchor in flowstate.toml only defines what hour 0 of the saved CSV means; the
# view window is resolved against the wall clock.
from helpers import horizon as _hz
from helpers.timefmt import planning_anchor, week_index_to_iso
_horizon = _hz.resolve(cfg)
_anchor = planning_anchor(cfg)          # storage anchor (hour 0 of the CSV)
st.caption(_hz.caption(_horizon))

# --- Live feed staleness banner (manprg, cip_info, demand baseline) -------
from helpers import data_health as _dh
_live_health = [h for h in _dh.assess(dd, cfg)
                if h.key in ("manprg", "cip_info", "demand_summary")]
_live_bad = [h for h in _live_health if h.state in (_dh.MISSING, _dh.ERROR)]
_live_stale = [h for h in _live_health if h.state == _dh.STALE]
if _live_bad:
    st.error("**Live data missing/unreadable:** " +
              "; ".join(f"{h.name} — {h.detail}" for h in _live_bad))
elif _live_stale:
    st.warning("**Live data stale:** " +
               "; ".join(f"{h.name} — {h.detail}" for h in _live_stale))

cip_cfg = cfg.get("cip", {})

cal = load_calendar(cal_path)
if cal.empty:
    st.warning("No calendar yet. Import a schedule on the **Schedule Scorecard** page.")
    st.stop()

# --- Roll the stored schedule onto today's anchor -------------------------
# Stored hours are offsets from `planning_start_date`. When that date is no
# longer today, offer a one-click rebase: shift every block back by the delta
# and move the anchor, so wall-clock position is preserved and hour 0 = today.
if _horizon.mode == "today" and _horizon.stale:
    _days = _horizon.shift_h / 24.0
    st.warning(
        f"Saved schedule is anchored to **{_horizon.config_anchor:%a %Y-%m-%d}**, "
        f"{_days:.1f} day(s) before today. Blocks are shown against that anchor. "
        "Roll the calendar to re-base every block onto today (hour 0 = "
        f"{_horizon.anchor:%Y-%m-%d}).")
    if st.button(f"Roll calendar to today ({_horizon.anchor:%Y-%m-%d})",
                 key="cal_roll_today"):
        import re as _re
        from datetime import datetime as _dt

        from helpers.paths import toml_path as _toml_path
        _bdir = dd / "_backups"
        _bdir.mkdir(parents=True, exist_ok=True)
        (_bdir / f"calendar_blocks.{_dt.now():%Y%m%d-%H%M%S}.csv").write_bytes(
            cal_path.read_bytes())
        save_calendar(_hz.rebase_calendar(load_calendar(cal_path), _horizon.shift_h),
                      cal_path)
        _tp = _toml_path()
        _txt = _tp.read_text(encoding="utf-8")
        _tp.write_text(
            _re.sub(r'planning_start_date\s*=\s*"[^"]*"',
                    f'planning_start_date = "{_horizon.anchor:%Y-%m-%d %H:%M:%S}"',
                    _txt),
            encoding="utf-8")
        # Charter §2.2: "each week the lock rolls forward one week." The roll
        # is the ONE weekly action, so an existing lock advances with it —
        # 2 whole weeks from the new anchor. No lock set = none created.
        if read_lock(dd) is not None:
            write_lock(dd, default_lock_through(_horizon.anchor))
        st.session_state.pop("cal_baseline_score", None)
        st.session_state["cal_reset_gen"] = (
            st.session_state.get("cal_reset_gen", 0) + 1)
        st.success("Calendar rolled to today (lock advanced with it). Reloading…")
        st.rerun()

# --- Rebuild the calendar from PLANT GROUND TRUTH -------------------------
# Handoff WW32 item 2. Instead of carrying the old seeded fixture forward, the
# initial state can be derived from what the plant is actually doing:
# manprg (running MO locked to its estimated end, queued MOs placed in order,
# completed MOs dropped) + cip_info (scheduled CIP drawn, further CIPs spaced
# at the line's MaxHoursBetweenCIP). The result is a feasible, UNOPTIMISED
# starting point — item 3 then lets the solver fill demand after it.
with st.expander("🏭 Rebuild calendar from current plant state (manprg + cip_info)"):
    from datetime import timedelta as _td

    from helpers.config import datasources_config as _ds_cfg
    from helpers.current_state import build_current_state as _build_cs

    _ds0 = _ds_cfg(cfg)
    _mp_paths0 = [p.strip() for p in str(_ds0.get("manprg_files", "")).split(";")
                  if p.strip()] or [str(dd / "reference" / "manprg.txt"),
                                    str(dd / "reference" / "manprg2.txt")]
    _cip_path0 = str(_ds0.get("cip_info_csv", "")).strip() or \
        str(dd / "reference" / "cip_info.csv")
    try:
        _cs = _build_cs(_horizon, manprg_paths=_mp_paths0, cip_path=_cip_path0,
                        lines=load_lines(dd / "lines.csv"), cfg=cfg)
    except Exception as _exc:  # noqa: BLE001
        _cs = None
        st.error(f"Could not read the live feeds: {_exc}")
    if _cs is not None:
        _c = _cs.counts
        st.caption(
            f"Ground truth: **{_c['running']}** running MO(s) (locked), "
            f"**{_c['queued']}** queued, **{_c['completed']}** completed "
            f"(greyed, immovable), **{_c['cip']}** CIP block(s) → "
            f"{_c['blocks']} blocks.")
        for _w in _cs.warnings[:6]:
            st.caption(f"⚠️ {_w}")
        if len(_cs.blocks):
            _prev = _cs.blocks.copy()
            _prev["start"] = _prev["start_h"].map(
                lambda h: (_horizon.anchor + _td(hours=float(h))).strftime("%a %m-%d %H:%M"))
            _prev["end"] = _prev["end_h"].map(
                lambda h: (_horizon.anchor + _td(hours=float(h))).strftime("%a %m-%d %H:%M"))
            st.dataframe(
                _prev[["line_name", "block_type", "label", "order_id", "start",
                       "end", "locked", "attrs"]],
                use_container_width=True, hide_index=True, height=280)
        st.caption("Replacing backs up the current calendar to `data/_backups/` first.")
        if st.button("Replace calendar with current plant state",
                     key="cal_from_plant_state", type="primary",
                     disabled=not len(_cs.blocks)):
            from datetime import datetime as _dt2
            _bdir2 = dd / "_backups"
            _bdir2.mkdir(parents=True, exist_ok=True)
            (_bdir2 / f"calendar_blocks.{_dt2.now():%Y%m%d-%H%M%S}.csv").write_bytes(
                cal_path.read_bytes())
            save_calendar(_cs.blocks, cal_path)
            st.session_state.pop("cal_baseline_score", None)
            # Remount the Gantt or it keeps showing the PRE-replace board
            # (the component holds its own state under a stable key).
            st.session_state["cal_reset_gen"] = (
                st.session_state.get("cal_reset_gen", 0) + 1)
            st.success(f"Calendar rebuilt from plant state ({_c['blocks']} blocks). "
                       "Reloading…")
            st.rerun()

# --- Start of day: downtime + view options --------------------------------
# One compact strip instead of scattered controls — the Gantt is the page.
# Hidden past rows are held aside and merged back on save (never destroyed).
with st.expander("🌅 Start of day — downtime · view options", expanded=False):
    st.markdown("**Scheduled downtime per side** — set before scheduling "
                "production; the sandbox stretches blocks over one-sided hours.")
    render_side_downtime_editor(dd, key_prefix="cal_dt")
    st.markdown("**View**")
    _vc1, _vc2 = st.columns([3, 1])
    with _vc1:
        _hide_past = st.checkbox(
            "Hide blocks that already finished", value=True, key="cal_hide_past",
            help="Completed blocks (end before now) stay on disk — they are "
                 "merged back when you save.")
    with _vc2:
        if st.button("Reload from disk", use_container_width=True):
            st.session_state["cal_reset_gen"] = (
                st.session_state.get("cal_reset_gen", 0) + 1)
            st.session_state.pop("cal_holding", None)
            st.session_state.pop("cal_baseline_score", None)
            st.rerun()
_past_rows = cal.iloc[0:0]
if _hide_past:
    _now_h = (_horizon.now - _anchor).total_seconds() / 3600.0
    _mask_past = cal["end_h"].astype(float) <= _now_h
    _past_rows = cal[_mask_past].copy()
    cal = cal[~_mask_past].copy()
    if len(_past_rows):
        st.caption(f"{len(_past_rows)} finished block(s) hidden (before "
                   f"{_horizon.now:%a %Y-%m-%d %H:%M}).")

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
    if "calc_rate_kgph" not in cdf.columns and "rate_kgph" in cdf.columns:
        cdf = cdf.rename(columns={"rate_kgph": "calc_rate_kgph"})
    for _, r in cdf.iterrows():
        if int(r.get("capable", 0) or 0) != 1:
            continue
        ln = str(r["line_name"])
        sku = str(r["sku"])
        rate = float(r.get("calc_rate_kgph") or 0)
        caps.setdefault(ln, {})[sku] = rate
# A double line's caps must be readable both per side (halved) and per group
# (both sides running), whichever way capabilities_rates.csv is keyed.
caps = expand_caps_with_groups(caps)

# Downtime editor lives in the Start-of-day strip above; only the map is
# computed here (it must see the file the editor just wrote).
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
    changeovers = load_changeover_setup_nested(co_path)

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
            "due_start_hour": float(r.get("due_start_hour", 0) or 0),
            "due_end_hour": float(r.get("due_end_hour", 0) or 0),
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

# completion % per MO (manprg by_mo) + per line for the now-running strip
_completion_by_mo: dict[str, float] = {}
_left_by_mo: dict[str, float] = {}
_now_running: list[dict] = []
_mp = read_manprg(_manprg_paths)
for mo, lp in _mp.by_mo.items():
    _completion_by_mo[mo] = lp.completion_pct
    _left_by_mo[mo] = lp.left_cas
for line, lp in _mp.current.items():
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
                # SQL gives current MO per line; map onto that line's current MO
                cur = _mp.current.get(ln)
                if cur is not None:
                    _completion_by_mo[cur.mo] = round(float(pct) * 100.0, 1)

# attach completion to production blocks by MATCHING MO (order_id) — sequential
# MOs on a line each carry their own %; a finished block is full, a future one 0.
for b in schedule:
    if b.get("block_type") == "sku":
        mo = b.get("order_id", "")
        if mo in _completion_by_mo:
            b["completion_pct"] = _completion_by_mo[mo]
            b["cases_left"] = _left_by_mo.get(mo)

# scheduled CIPs from cip_info as overlay windows (drawn, not editable).
# Skip lines that already have a CIP block in the calendar (e.g. a PDF import
# or current-state rebuild that carries cip_info-derived CIPs) — otherwise the
# same scheduled CIP renders twice and the Gantt counts CIP-on-CIP overlaps.
_cip = read_cip_info(_cip_path)
from helpers.timefmt import planning_anchor as _pa
_anchor = _pa(cfg)
_cal_cip_lines = {str(b.get("line_name")) for b in windows
                  if b.get("block_type") == "cip"}
for line, ci in _cip.by_line.items():
    if ci.scheduled_cip is None:
        continue
    if line in _cal_cip_lines:
        continue
    start_h = (ci.scheduled_cip - _anchor).total_seconds() / 3600.0
    dur = float(cip_cfg.get("duration_h", 6))
    lid = -1
    try:
        lid = int(str(line)[1:]) - 9
    except (ValueError, IndexError):
        lid = 0
    windows.append({
        "id": f"cipinfo_{line}", "line_id": lid,
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

# ---- Now-running table: current MO per line from manprg ----
# Only MOs actually in progress (not completed, not future) belong here —
# collapsed so the Gantt stays the first thing on the page.
_active = [r for r in _now_running if 0 < r["pct"] < 100]
if _active:
    with st.expander(
            f"▶ Now running (live from manprg) — {len(_active)} line(s)",
            expanded=False):
        _desig = {line: lp.designation for line, lp in _mp.current.items()}
        _nr = sorted(_active, key=lambda r: r["line"])
        st.dataframe(
            [{"Line": r["line"], "MO": r["mo"], "SKU": r["item"],
              "Designation": _desig.get(r["line"], ""),
              "Completion": f"{r['pct']:.1f}%",
              "Cases left": int(r["left"])} for r in _nr],
            use_container_width=True, hide_index=True)

# ── Auto-populate holding from the latest scenario solve ─────────────────
# After a scenario (esp. E) produces a schedule, demand orders left under
# qmin / at zero qty belong in the holding area for manual placement. We
# read the newest scenario work-dir's produced_vs_bounds.csv once per session
# and merge those blocks into cal_holding (skipping ones already there).
if "cal_holding_from_solve" not in st.session_state:
    _held: list[dict] = []
    _scen_root = dd / "_scenario_work"
    if _scen_root.exists():
        _cands = sorted(
            [p for p in _scen_root.iterdir()
             if (p / "produced_vs_bounds.csv").exists()],
            key=lambda p: (p / "produced_vs_bounds.csv").stat().st_mtime,
            reverse=True,
        )
        if _cands:
            try:
                from helpers.holding_builder import (
                    average_rate_per_sku,
                    build_holding,
                    load_capabilities,
                    load_demand,
                    load_produced,
                )
                _latest = _cands[0]
                _dem = load_demand(dd / "reference" / "demand_plan.csv")
                _prod = load_produced(_latest / "produced_vs_bounds.csv")
                _rates = average_rate_per_sku(
                    load_capabilities(dd / "reference" / "capabilities_rates.csv"))
                _blocks = build_holding(_dem, _prod, rates=_rates)
                _held = [b.to_payload() for b in _blocks]
            except Exception as _e:  # noqa: BLE001
                st.caption(f"Holding auto-populate skipped: {_e}")
    st.session_state["cal_holding_from_solve"] = _held

_existing_ids = {b.get("id") for b in st.session_state.get("cal_holding", [])}
for _hb in st.session_state.get("cal_holding_from_solve", []):
    if _hb.get("id") not in _existing_ids:
        st.session_state.setdefault("cal_holding", []).append(_hb)
        _existing_ids.add(_hb.get("id"))
_auto_held = len(st.session_state.get("cal_holding_from_solve", []))
if _auto_held:
    st.caption(
        f"💡 {_auto_held} under-target demand order(s) auto-placed in holding "
        "(from the latest scenario solve). Drag them onto a line or ignore.")

# ── 2-week lock window (charter: 2 weeks locked, week 3 fluid) ────────────
_lock_dt = read_lock(dd)
_lock_h = locked_through_h(_lock_dt, _anchor)
if _lock_dt is not None:
    st.info(f"🔒 Weeks locked through **{_lock_dt:%a %Y-%m-%d %H:%M}** — "
            "blocks starting before then are committed to the plant "
            "(no drag/resize/edit).")

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
        "planning_anchor": f"{_anchor:%Y-%m-%d %H:%M:%S}",
        "cip_duration_h": int(cip_cfg.get("duration_h", 6)),
        "min_run_hours": int(sched_cfg.get("min_run_hours", 4)),
        "horizon_hours": int(_horizon.hours),
        "locked_through_h": None if _lock_h is None else float(_lock_h),
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
        # Merge hidden (already-finished) rows back so hiding the past never
        # deletes it from disk. Display-only cip_info overlays are stripped —
        # they are a live-feed visualization, not calendar data.
        _out = working if _past_rows.empty else pd.concat(
            [_past_rows, working], ignore_index=True)
        save_calendar(drop_display_overlays(_out), cal_path)
        st.session_state["cal_holding"] = []
        st.session_state.pop("cal_baseline_score", None)
        st.success("Saved calendar_blocks.csv")
with b2:
    save_name = st.text_input("Version name", value="Option 1",
                              key="cal_save_name")
    if st.button("Save as named version", use_container_width=True):
        if n_holding:
            st.warning(f"Version will omit {n_holding} held block(s).")
        try:
            slug = save_version(
                save_name or "Option",
                drop_display_overlays(working),
                live.to_dict(),
                dd,
                source="digital_twin",
            )
            st.session_state["cal_holding"] = []
            st.success(f"Saved version `{slug}` — see Version Compare")
        except ValueError as e:
            st.error(str(e))

# ── Lock & Export ──────────────────────────────────────────────────────────
st.divider()
st.subheader("Lock & Export")
st.caption(
    "Charter: **2 weeks locked and ready, week 3 flexible.** Locking freezes "
    "every block that starts before the boundary — the Gantt refuses "
    "drag/resize/edit inside the window. The weekly roll advances it."
)
_lc1, _lc2, _lc3 = st.columns(3)
with _lc1:
    _default_lock = default_lock_through(_anchor)
    if st.button(f"🔒 Lock weeks 1–2 (through {_default_lock:%a %m-%d})",
                 use_container_width=True):
        write_lock(dd, _default_lock)
        st.session_state["cal_reset_gen"] += 1  # remount so the Gantt sees it
        st.rerun()
with _lc2:
    if st.button("Unlock", use_container_width=True,
                 disabled=_lock_dt is None):
        write_lock(dd, None)
        st.session_state["cal_reset_gen"] += 1
        st.rerun()
with _lc3:
    st.caption(
        f"Currently: **{'locked through ' + f'{_lock_dt:%a %Y-%m-%d %H:%M}' if _lock_dt else 'no lock set'}**")

n_ver = len(list_versions(dd))
st.caption(f"{n_ver} / 5 versions saved")
