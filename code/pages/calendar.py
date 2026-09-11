# pages/calendar.py — Phase 1 Digital Twin: the planner's main screen.
#
# Layout (streamlined 2026-09-11 — the board is the page):
#   1. header + status chips (live data, lock, versions, hidden past, saved)
#   2. ONE attention strip: only what must be handled before planning
#      (live feeds missing/stale, weekly roll, MO drift), each with its button
#   3. one control row (version name · hide finished · reload)
#   4. the Gantt — its own toolbar carries Refresh / Save / Save as version;
#      Save pushes the live edits together with the request, so a save can
#      never miss what is on screen (the old separate Streamlit button only
#      saw the last "Refresh checks" push)
#   5. Lock & Export strip
#   6. tabs: live score · plant state & float links · downtime & view ·
#      now running — everything that used to sit ABOVE the board.

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from components.gantt import gantt_calendar
from helpers.calendar_io import (
    build_co_flags,
    build_line_capable_skus,
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
from helpers.config import load_toml, scorecard_config
from helpers.downtime_ui import downtime_map_for_calendar, render_side_downtime_editor
from helpers.lines_model import expand_caps_with_groups, is_double, side_of, sides_of
from helpers.paths import data_dir, reference_dir
from helpers.st_compat import deferred_dataframe
from helpers.theme import chip, note, page_header, render_chips, section_label
from solver.changeover_cache import load_changeover_setup_nested
from helpers.scorecard_engine import (
    ScorecardResult,
    delta_narrative,
    gantt_kpis,
    score_calendar,
    scoring_inputs_signature,
)
from helpers.scorecard_ui import render_delta_strip, render_scorecard
from helpers.version_manager import (
    MAX_VERSIONS,
    export_calendar_excel,
    list_versions,
    save_version,
)


def _demand_base_iso_week() -> int | None:
    """ISO week of the demand anchor (demand_plan.source.json) — order-id
    -W<k> suffixes label as W(base+k) in the Gantt."""
    import json as _j
    p = reference_dir() / "demand_plan.source.json"
    try:
        return int(_j.loads(p.read_text(encoding="utf-8"))["anchor_iso_week"])
    except Exception:
        return None


def _backup_calendar(path: Path, dd: Path) -> None:
    bdir = dd / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / f"calendar_blocks.{datetime.now():%Y%m%d-%H%M%S}.csv").write_bytes(
        path.read_bytes())


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
_weeks_txt = " / ".join(
    f"WW{week_index_to_iso(i, _horizon.anchor):02d}"
    for i in range(max(1, _horizon.hours // _hz.HOURS_PER_WEEK)))
_horizon_txt = (f"{_horizon.start:%a %Y-%m-%d} → {_horizon.end:%a %Y-%m-%d} · "
                f"{_weeks_txt} · "
                + ("rolling, starts today" if _horizon.mode == "today" else "fixed anchor"))

if "cal_reset_gen" not in st.session_state:
    st.session_state["cal_reset_gen"] = 0

# --- Live feed staleness (manprg, cip_info, demand baseline) --------------
from helpers import data_health as _dh
_live_health = [h for h in _dh.assess(dd, cfg)
                if h.key in ("manprg", "cip_info", "demand_summary")]
_live_bad = [h for h in _live_health if h.state in (_dh.MISSING, _dh.ERROR)]
_live_stale = [h for h in _live_health if h.state == _dh.STALE]

cip_cfg = cfg.get("cip", {})

# Corrupt rows (blank/unparsable hours) are DROPPED with a warning, never
# moved to hour 0 (fix quality-8, 2026-09-03) — saving would have made the
# relocation permanent.
_load_warnings: list[str] = []
cal = load_calendar(cal_path, warnings=_load_warnings)
for _lw in _load_warnings[:6]:
    st.warning(f"⚠️ {_lw}")
if cal.empty:
    page_header("Plant Calendar", subtitle=_horizon_txt)
    st.warning("No calendar yet. Import a schedule on the **Schedule Scorecard** page, "
               "or rebuild one from the plant state below.")
    st.stop()

# Attention items: (kind, message, button label, button key, action). They
# are rendered ONCE, compactly, right under the header — the board follows.
_attention: list[tuple[str, str, str | None, str | None, object]] = []

if _live_bad:
    _attention.append(("bad", "**Live data missing/unreadable:** "
                       + "; ".join(f"{h.name} — {h.detail}" for h in _live_bad),
                       None, None, None))
elif _live_stale:
    _attention.append(("warn", "**Live data stale:** "
                       + "; ".join(f"{h.name} — {h.detail}" for h in _live_stale),
                       None, None, None))

# --- Roll the stored schedule onto today's anchor -------------------------
# Stored hours are offsets from `planning_start_date`. When that date is no
# longer today, offer a one-click rebase: shift every block back by the delta
# and move the anchor, so wall-clock position is preserved and hour 0 = today.
def _roll_calendar_to_today() -> None:
    import re as _re
    from helpers.paths import toml_path as _toml_path
    _backup_calendar(cal_path, dd)
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
    st.session_state["cal_reset_gen"] += 1
    st.toast("Calendar rolled to today (lock advanced with it).", icon=":material/check_circle:")
    st.rerun()


if _horizon.mode == "today" and _horizon.stale:
    _days = _horizon.shift_h / 24.0
    _attention.append((
        "warn",
        f"Saved schedule is anchored to **{_horizon.config_anchor:%a %Y-%m-%d}**, "
        f"{_days:.1f} day(s) before today — blocks are shown against that anchor. "
        f"Roll the calendar to re-base every block onto today.",
        f"Roll calendar to today ({_horizon.anchor:%Y-%m-%d})", "cal_roll_today",
        _roll_calendar_to_today))

# --- Plant ground truth (manprg + cip_info) --------------------------------
# Handoff WW32 item 2. The initial state can be derived from what the plant
# is actually doing: manprg (running MO locked to its estimated end, queued
# MOs placed in order, completed MOs dropped) + cip_info (scheduled CIP
# drawn, further CIPs spaced at the line's MaxHoursBetweenCIP). Used by the
# MO-drift check here and by the "Rebuild" tool in the tabs below.
from datetime import timedelta as _td

from helpers.config import datasources_config as _ds_cfg
from helpers.current_state import build_current_state as _build_cs

_ds0 = _ds_cfg(cfg)
_mp_paths0 = [p.strip() for p in str(_ds0.get("manprg_files", "")).split(";")
              if p.strip()] or [str(dd / "reference" / "manprg.txt"),
                                str(dd / "reference" / "manprg2.txt")]
_cip_path0 = str(_ds0.get("cip_info_csv", "")).strip() or \
    str(dd / "reference" / "cip_info.csv")
_cs_err: str | None = None
try:
    _cs = _build_cs(_horizon, manprg_paths=_mp_paths0, cip_path=_cip_path0,
                    lines=load_lines(dd / "lines.csv"), cfg=cfg,
                    caps_path=dd / "reference" / "capabilities_rates.csv")
except Exception as _exc:  # noqa: BLE001
    _cs = None
    _cs_err = f"Could not read the live feeds: {_exc}"

# --- Floating blocks: MO drift chip + link management (2026-08-28) --------
# A block with an "after:<anchor>:<gap>" attrs token follows its anchor's
# end. Running-MO ends re-forecast from actual cases (manprg); the board
# NEVER moves silently (master-file invariant) — the attention row previews
# the drift and one click applies + saves.
from helpers.calendar_io import (apply_float_links, clear_float_link,
                                 float_link_of, set_float_link)

_cal_now = load_calendar(cal_path)
_float_n = int(sum(1 for _, _r in _cal_now.iterrows()
                   if float_link_of(_r.get("attrs"))))
if _cs is not None and not _cal_now.empty:
    # Fresh MO starts/ends from the live rebuild, matched by (line, MO).
    _fresh: dict = {}
    for _b in _cs.blocks.to_dict("records") if hasattr(_cs.blocks, "to_dict") \
            else _cs.blocks:
        _tok = str(_b.get("attrs") or "")
        if "current_state:running" in _tok or "current_state:queued" in _tok:
            _fresh[(str(_b.get("line_name", "")).upper(),
                    str(_b.get("order_id", "")))] = (
                float(_b["start_h"]), float(_b["end_h"]))
    _drift = _cal_now.copy()
    _n_drift = 0
    for _i, _r in _drift.iterrows():
        _tok = str(_r.get("attrs") or "")
        if not ("current_state:running" in _tok or "current_state:queued" in _tok):
            continue
        _key = (str(_r.get("line_name", "")).upper(), str(_r.get("order_id", "")))
        if _key not in _fresh:
            continue
        _ns, _ne = _fresh[_key]
        if abs(_ne - float(_r["end_h"])) > 0.05 or abs(_ns - float(_r["start_h"])) > 0.05:
            _drift.loc[_i, "start_h"] = _ns
            _drift.loc[_i, "end_h"] = _ne
            _n_drift += 1
    _synced, _float_notes = apply_float_links(_drift)
    if _n_drift or _float_notes:
        _msg = []
        if _n_drift:
            _msg.append(f"{_n_drift} MO block(s) drifted vs live manprg")
        _msg.extend(_float_notes[:4])

        def _apply_drift_sync(_synced=_synced) -> None:
            _backup_calendar(cal_path, dd)
            save_calendar(_synced, cal_path)
            st.session_state["cal_reset_gen"] += 1
            st.toast("Board synced to live MO ends; floating blocks followed.", icon=":material/link:")
            st.rerun()

        _attention.append(("warn", "⛓ " + " · ".join(_msg),
                           "Apply MO drift & float sync", "float_sync_apply",
                           _apply_drift_sync))

# --- Lock, versions, hidden past — the header chips -----------------------
_lock_dt = read_lock(dd)
_lock_h = locked_through_h(_lock_dt, _anchor)
_vers = list_versions(dd)
_n_orph = sum(1 for v in _vers if v.get("orphan"))
_hide_past = bool(st.session_state.get("cal_hide_past", True))
_past_rows = cal.iloc[0:0]
# wall clock as a BOARD-frame hour: drives the hide-past split and the
# 'completed' flag on the export (both frames must agree, 2026-09-03)
_now_h = (_horizon.now - _anchor).total_seconds() / 3600.0
if _hide_past:
    _mask_past = cal["end_h"].astype(float) <= _now_h
    _past_rows = cal[_mask_past].copy()
    cal = cal[~_mask_past].copy()

_chips = []
if _live_bad:
    _chips.append(chip("live data missing", "bad", icon="✕"))
elif _live_stale:
    _chips.append(chip("live data stale", "warn", icon="▲"))
else:
    _chips.append(chip("live data fresh", "ok", icon="●"))
if _lock_dt is not None:
    _chips.append(chip(f"locked through {_lock_dt:%a %m-%d}", "accent", icon="🔒"))
else:
    _chips.append(chip("no lock set", "neutral", icon="🔓"))
_chips.append(chip(f"{len(_vers)} / {MAX_VERSIONS} versions",
                   "warn" if _n_orph else "neutral",
                   sub=f"{_n_orph} orphaned" if _n_orph else ""))
if len(_past_rows):
    _chips.append(chip(f"{len(_past_rows)} finished hidden", "neutral"))
if st.session_state.get("cal_last_saved"):
    _chips.append(chip(f"saved {st.session_state['cal_last_saved']}", "ok", icon=":material/save:"))

page_header("Plant Calendar", subtitle=_horizon_txt, right=_chips)

# --- Attention strip ------------------------------------------------------
for _n in st.session_state.pop("cal_save_notes", None) or []:
    _attention.append(("warn", f"⚠️ {_n}", None, None, None))
for _kind, _text, _btn, _key, _fn in _attention:
    _c_msg, _c_btn = st.columns([5, 1.6]) if _btn else (st.container(), None)
    _box = st.error if _kind == "bad" else st.warning
    with _c_msg:
        _box(_text)
    if _btn and _c_btn is not None:
        with _c_btn:
            if st.button(_btn, key=_key, use_container_width=True, type="primary"):
                _fn()  # type: ignore[operator]

# --- Control row: version name · hide finished · reload -------------------
_cc1, _cc2, _cc4, _cc3 = st.columns([2.2, 2.2, 2.0, 1.2])
with _cc1:
    save_name = st.text_input(
        "Version name", value="Option 1", key="cal_save_name",
        help="Used by the board's **Save as version** button.",
        label_visibility="collapsed", placeholder="Version name (for Save as version)")
with _cc2:
    _hide_past_widget = st.checkbox(
        "Hide blocks that already finished", value=True, key="cal_hide_past",
        help="Completed blocks (end before now) stay on disk — they are "
             "merged back when you save.")
with _cc4:
    # Server-side 2-week lock (fix writeback-9): a save that moves, resizes,
    # adds or removes a block starting before the lock is refused unless the
    # planner ticks this — read by the board's Save handling below.
    if _lock_h is not None and _lock_h > 0:
        st.checkbox(
            "Override the 2-week lock on save", value=False,
            key="cal_lock_override",
            help="Blocks starting before the lock are committed to the plant. "
                 "Tick only when the plant has agreed to the change.")
    else:
        st.session_state.pop("cal_lock_override", None)
with _cc3:
    if st.button("↻ Reload from disk", use_container_width=True,
                 help="Drop unsaved board edits and reload calendar_blocks.csv."):
        st.session_state["cal_reset_gen"] += 1
        st.session_state.pop("cal_holding", None)
        st.session_state.pop("cal_baseline_score", None)
        st.rerun()
if _hide_past_widget != _hide_past:
    # First run of a toggled setting: the value above the widget was stale.
    st.rerun()

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
    from helpers.effective_rates import load_effective_capabilities
    cdf = load_effective_capabilities(caps_path, dd=dd)
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

# Downtime editor lives in the tabs below; only the map is computed here
# (it must see the file the editor wrote — the editor reruns after a save).
side_downtime = {k: [[s, e] for s, e in v] for k, v in downtime_map_for_calendar(dd, cal).items()}
_one_sided = [g for g in sorted({str(l["line_group"]) for l in lines if l["is_double"]})
              if any(side_downtime.get(s) for s in sides_of(g))]

changeovers: dict = {}
co_path = reference_dir(dd) / "changeovers.csv"
if co_path.exists():
    changeovers = load_changeover_setup_nested(co_path)

demand_targets = []
_dem_anchor0 = None
dem_path = reference_dir(dd) / "demand_plan.csv"
if dem_path.exists():
    # ONE rule for the demand targets (fix Q / ui-2, INTEGRATE applying the
    # Q->W handoff): helpers.scorecard_engine.build_demand_targets — qty_min /
    # qty_max from the columns or target x pct, due hours shifted from the
    # demand file's OWN frame into the board's (demand_plan.source.json
    # anchor vs the storage anchor `_anchor`) so the position-aware credit
    # pass overlaps them against block hours in one frame.
    from helpers.demand_coverage import demand_source_anchor as _dsa
    from helpers.scorecard_engine import build_demand_targets as _bdt
    _dem_anchor0 = _dsa(dd)
    demand_targets = _bdt(dd, anchor=_anchor)

# Downtimes are CONSTRAINTS, not schedule (user rule 2026-09-01): the
# export (calendar_blocks.csv) never carries them — the plant is simply not
# scheduled there. downtimes.csv (the downtime tab) is the single source;
# the board DRAWS its rows as locked windows on every load and strips them
# on every save (drop_display_overlays). Rows minted by older imports
# (down_/maint_) are purged the same way.
from helpers.calendar_io import DISPLAY_ONLY_PREFIXES as _DT_PREFIXES
from helpers.calendar_io import downtime_block_type as _dt_btype
from helpers.downtime_store import load_downtimes as _load_dt
cal = cal[~cal["block_id"].astype(str).str.startswith(_DT_PREFIXES)].copy()
schedule, windows = calendar_to_gantt_payload(cal)
_dt_note: str | None = None
try:
    _dt_rows = _load_dt(dd, anchor=_anchor)
except Exception as _exc:  # noqa: BLE001
    _dt_rows = None
    _dt_note = f"Downtimes not drawn: {_exc}"
if _dt_rows is not None and len(_dt_rows):
    for _i, _r in _dt_rows.iterrows():
        try:
            _s, _e = float(_r["start_hour"]), float(_r["end_hour"])
        except (TypeError, ValueError):
            continue
        if pd.isna(_s) or pd.isna(_e) or _e <= _s or _e <= 0:
            continue
        if _hide_past and _e <= _now_h:
            continue
        _reason = str(_r.get("reason", "") or "Down")
        windows.append({
            "id": f"dt_{_i}", "line_id": int(_r.get("line_id", 0) or 0),
            "line_name": str(_r.get("line_name", "")), "order_id": "",
            "sku": "", "sku_description": _reason,
            "start_hour": _s, "end_hour": _e, "run_hours": _e - _s,
            "is_trial": False, "block_type": _dt_btype(_r.get("type"), _reason),
            "label": _reason, "locked": True, "attrs": "ref_downtime",
        })

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

# completion % per MO (manprg by_mo) + per line for the now-running tab
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

# Remember the board file's mtime at the moment the Gantt was (re)mounted:
# the component holds its own copy of the board from then on, so a Save
# writes THAT state. If the file changed underneath (another tab, a promote,
# the bridge) the save still wins but the planner is told and a backup
# holds the clobbered board (fix writeback-13, 2026-09-03).
if st.session_state.get("cal_mount_gen") != st.session_state["cal_reset_gen"]:
    st.session_state["cal_mount_gen"] = st.session_state["cal_reset_gen"]
    st.session_state["cal_mount_mtime"] = (
        cal_path.stat().st_mtime if cal_path.exists() else None)

# ── Auto-populate holding from the latest scenario solve ─────────────────
# After a scenario (esp. E) produces a schedule, demand orders left under
# qmin / at zero qty belong in the holding area for manual placement. We
# read the newest scenario work-dir's produced_vs_bounds.csv once per session
# and merge those blocks into cal_holding (skipping ones already there).
# ── THE MASTER-FILE INVARIANT (user mandate 2026-08-21) ──────────────────
# The calendar shows calendar_blocks.csv; the metrics score
# calendar_blocks.csv; holding is demand minus the board minus made kg.
# Therefore the credit map handed to this page contains ONLY kg that NO
# visible board block represents: made kg from completed MOs the board
# hides (current_state drops them), minus any completed MO whose block
# still IS on the board. Committed MOs ARE board blocks after a rebuild —
# crediting them here too double-counts by construction (that mistake made
# a W35 card read 98% over a nearly empty calendar). The SOLVER's netting
# is the opposite deliberately: it keeps the FULL ledger (committed MOs are
# real future production it must not re-plan) — see
# build_ledger_from_data(made_only=...) for where the line is drawn.
_made_credit: dict = {}
try:
    from helpers.demand_coverage import build_ledger_from_data as _blfd
    _board_oids = {
        str(o) for o in cal.loc[
            cal["block_type"] == "production", "order_id"].dropna().astype(str)
        if o and o.lower() != "nan"}
    # order_id -> attrs of the board's production rows: a running MO's
    # pre-anchor `made_part` kg is credited only when its board block is a
    # post-CA-3 block (remaining kg, `made_kg=` token) — see
    # build_ledger_from_data.
    _board_attrs: dict = {}
    if "attrs" in cal.columns:
        for _o, _a in cal.loc[cal["block_type"] == "production",
                              ["order_id", "attrs"]].itertuples(index=False):
            if _o is not None and str(_o).lower() != "nan":
                _board_attrs[str(_o)] = (_board_attrs.get(str(_o), "") + ";"
                                         + str(_a or ""))
    _led0 = _blfd(dd, cfg, made_only=True, exclude_mos=_board_oids,
                  board_attrs=_board_attrs)
    if _led0 is not None:
        _made_credit = _led0.applied_by_order()
except Exception:  # noqa: BLE001 — numbers degrade to board-only
    _made_credit = {}

# Holding reflects the OFFICIAL BOARD, not the latest solve (user report
# 2026-08-18: "why only 1 item for W35?" — Monday's un-promoted proposal
# covered W35, so holding said 'done' while the board sat empty). Same
# master-file rule as the week cards: HOLDING = demand − board − made,
# computed by ONE compute_adherence pass over the board with the made-only
# credit map — the exact numbers the week cards and the adherence table
# show, so a card exists precisely when that shared number is under qmin.
# Committed-MO blocks credit through the waterfall (they are board blocks);
# _made_credit adds only completed-MO kg the board hides.
# Rebuilds whenever the board file changes (promote/save/roll).
_board_stamp = st.session_state.get(
    "cal_board_sig",
    cal_path.stat().st_mtime if cal_path.exists() else 0.0)
_holding_note: str | None = None
if st.session_state.get("cal_holding_stamp") != _board_stamp:
    _held: list[dict] = []
    try:
        from helpers.holding_builder import (
            average_rate_per_sku,
            build_holding,
            load_capabilities,
            load_demand,
        )
        _dem = load_demand(dd / "reference" / "demand_plan.csv")
        # last pushed what-if state (the Refresh button) wins over disk, so
        # tonnage edits move cards in/out of holding without a Save
        _wrk_recs = st.session_state.get("cal_working_records")
        _bsrc = (pd.DataFrame(_wrk_recs)
                 if _wrk_recs else cal)
        # Per-order covered kg via the SAME adherence pass the week cards
        # and the adherence table use: board blocks (matching order_ids
        # credit directly; committed-MO blocks waterfall onto their SKU's
        # orders earliest-due first, capped at qty_max) + made-only credit.
        from helpers.scorecard_engine import compute_adherence as _adh
        _board_kg: dict[str, float] = {
            str(r["order_id"]): float(r["scheduled_qty"])
            for r in _adh(_bsrc, demand_targets, caps,
                          covered_by_order=_made_credit)}
        _t = pd.to_numeric(_dem["qty_target"], errors="coerce").fillna(0)
        _lo = pd.to_numeric(_dem.get("lower_pct", 0.9),
                            errors="coerce").fillna(0.9)
        _hi = pd.to_numeric(_dem.get("upper_pct", 1.1),
                            errors="coerce").fillna(1.1)
        _prod = pd.DataFrame({
            "order_id": _dem["order_id"].astype(str),
            "sku": _dem["sku"].astype(str),
            "qty_min": _t * _lo,
            "qty_max": _t * _hi,
            "produced": [_board_kg.get(str(o), 0.0)
                         for o in _dem["order_id"]],
        })
        _rates = average_rate_per_sku(
            load_capabilities(dd / "reference" / "capabilities_rates.csv"))
        # produced already carries board + made per order (one shared
        # adherence pass above) — no second credit map, or made kg would
        # count twice. A week the board leaves empty MUST fill its holding
        # column: that is the planner's work queue.
        _blocks = build_holding(_dem, _prod, rates=_rates)
        _held = [b.to_payload() for b in _blocks]
    except Exception as _e:  # noqa: BLE001
        _holding_note = f"Holding auto-populate skipped: {_e}"
    st.session_state["cal_holding_from_solve"] = _held
    st.session_state["cal_holding_stamp"] = _board_stamp
    # The board changed: drop stale AUTO cards (hold_*) so demand covered
    # by a promote disappears instead of lingering — but keep blocks the
    # planner parked here by dragging them off the board (their intent,
    # not derived state).
    st.session_state["cal_holding"] = [
        b for b in st.session_state.get("cal_holding", [])
        if not str(b.get("id", "")).startswith("hold_")]

# Merge: fresh auto cards REPLACE same-id survivors (a stale component
# snapshot re-inserts pre-rebuild hold_* cards on the rerun after a
# rebuild — the old skip-if-present merge then kept the stale tonnage
# forever, review 2026-09-01). Planner-parked blocks (non-fresh ids) keep
# their place untouched.
_fresh_cards = st.session_state.get("cal_holding_from_solve", [])
_fresh_ids = {b.get("id") for b in _fresh_cards}
st.session_state["cal_holding"] = (
    [b for b in st.session_state.get("cal_holding", [])
     if b.get("id") not in _fresh_ids] + list(_fresh_cards))

# Past demand weeks never belong in holding (user rule 2026-08-17): a week
# that is over cannot be scheduled — its unmet tonnage is a MISS, tracked by
# Reconcile/netting, not a card to drag. Ages out stale cards that persisted
# in the session from earlier solves too. Cards without a -W suffix (blocks
# dragged off the board) are kept — they carry no week claim.
def _holding_is_current(entry: dict) -> bool:
    import re as _re
    _m = _re.search(r"-W(\d+)$", str(entry.get("order_id", "")))
    if not _m:
        return True
    _base = _demand_base_iso_week()
    if _base is None:
        return True
    from datetime import date as _date
    return _base + int(_m.group(1)) >= _date.today().isocalendar()[1]

_before = len(st.session_state.get("cal_holding", []))
st.session_state["cal_holding"] = [
    b for b in st.session_state.get("cal_holding", []) if _holding_is_current(b)]
# Legacy cards persisted with the Python-side type; the Gantt vocabulary is
# "sku" — anything else hides the pin button once the card lands on a line.
for _b in st.session_state["cal_holding"]:
    if _b.get("block_type") == "production":
        _b["block_type"] = "sku"
_aged_out = _before - len(st.session_state["cal_holding"])
_auto_held = len(st.session_state.get("cal_holding_from_solve", []))

# sku -> pack format (e.g. "6X12X90") for the holding-area card text, plus
# sku -> designation for the blank-space SKU picker rows (demand SKUs only —
# the picker offers nothing else, so the payload stays lean).
_fmt_map: dict[str, str] = {}
_desc_map: dict[str, str] = {}
_dem_skus = {str(t["sku"]) for t in demand_targets}
_sku_info_path = dd / "reference" / "sku_info.csv"
if _sku_info_path.exists():
    try:
        _si = pd.read_csv(_sku_info_path, dtype={"sku": str})
        if "format" in _si.columns:
            _fmt_map = {
                str(r["sku"]): str(r["format"]).strip()
                for _, r in _si.iterrows()
                if str(r.get("format", "")).strip()
                and str(r.get("format", "")).lower() != "nan"
            }
        if "designation" in _si.columns:
            _desc_map = {
                str(r["sku"]): str(r["designation"]).strip()
                for _, r in _si.iterrows()
                if str(r["sku"]) in _dem_skus
                and str(r.get("designation", "")).strip()
                and str(r.get("designation", "")).lower() != "nan"
            }
    except Exception:
        _fmt_map = {}
        _desc_map = {}

# Blank-space SKU picker payloads: demand-plan SKUs each line can run and the
# changeover-type bitmask per SKU pair (setup hours ride in `changeovers`).
# Pair universe includes board SKUs so committed-MO/trial neighbours get
# honest chips instead of reading as clean (review 2026-08-19).
_line_capable = build_line_capable_skus(caps_path, _dem_skus)
_board_skus = {
    s[:-2] if s.endswith(".0") else s  # float-formatted CSV skus ("280212.0")
    for s in cal.loc[
        cal["block_type"] == "production", "sku"].dropna().astype(str)
    if s and s.lower() != "nan"
}
_co_flags = build_co_flags(co_path, _dem_skus, _board_skus)

# Canonical KPI payload — same engine (scorecard_engine) as the scorecard
# rendered below, so the Gantt KPI bar and the scorecard cannot disagree.
# Scored from the last PUSHED board when one exists (2026-09-01): the disk
# board is stale the moment the planner edits, and the tiles must follow
# "Refresh checks", not Save.
_kpi_wrk = st.session_state.get("cal_working_records")
server_kpis = gantt_kpis(
    pd.DataFrame(_kpi_wrk) if _kpi_wrk else cal,
    demand_targets, caps, cfg=scorecard_config(cfg), data_dir=dd,
    covered_by_order=_made_credit,
)

# Supply timeline (contract §5/§7): the SAVED stock report only — this
# page never recomputes it (~35 s); Stock Check's Refresh does. An old
# cache without supply_meta yields None and the Gantt shows no chips.
from helpers.calendar_io import board_supply_summary, build_stock_payload
from helpers.stock_report_cache import load_cached as _load_stock_cached
_stock_cached = _load_stock_cached(dd)
_stock_payload = (build_stock_payload(_stock_cached.report, cfg, _anchor,
                                      cases_left=_left_by_mo)
                  if _stock_cached else None)

state = gantt_calendar(
    schedule=schedule,
    cip_windows=windows,
    capabilities=caps,
    changeovers=changeovers,
    demand_targets=demand_targets,
    lines=lines,
    holding_area=st.session_state.get("cal_holding", []),
    side_downtime=side_downtime,
    sku_formats=_fmt_map,
    kpis=server_kpis,
    line_capable_skus=_line_capable,
    co_flags=_co_flags,
    sku_descriptions=_desc_map,
    stock=_stock_payload,
    focus_block=st.query_params.get("focus"),
    config={
        "planning_anchor": f"{_anchor:%Y-%m-%d %H:%M:%S}",
        "cip_duration_h": int(cip_cfg.get("duration_h", 6)),
        # Per-line MaxHoursBetweenCIP (cip_info) so the board can re-forecast
        # later projected cleans when the planner inserts a CIP (2026-09-01).
        "cip_interval_h": {str(_l): float(_ci.max_hours_between or 0)
                           for _l, _ci in _cip.by_line.items()
                           if (_ci.max_hours_between or 0) > 0},
        "cip_interval_default_h": float(cip_cfg.get("interval_h", 120)),
        "min_run_hours": int(sched_cfg.get("min_run_hours", 4)),
        "horizon_hours": int(_horizon.hours),
        "locked_through_h": None if _lock_h is None else float(_lock_h),
        "demand_base_iso_week": _demand_base_iso_week(),
        # The demand file's own anchor (demand_plan.source.json) so the
        # frontend pins the demand week's ISO year outright instead of
        # inferring it from the base week (agent FE handoff W-4).
        "demand_anchor": (f"{_dem_anchor0:%Y-%m-%d %H:%M:%S}"
                          if _dem_anchor0 is not None else None),
    },
    height=820,
    key=f"gantt_calendar_{st.session_state['cal_reset_gen']}",
)

working = cal
holding = st.session_state.get("cal_holding", [])
if state and state.get("schedule") is not None:
    working = gantt_payload_to_calendar(state.get("schedule") or [], state.get("cipWindows") or [])
    holding = state.get("holdingArea") or []

# Supply caption: the pushed board re-graded server-side with the same
# engine + payload as the client chips (<50 ms), so both show one number.
if _stock_payload:
    _sup = board_supply_summary(
        working[working["block_type"] == "production"].to_dict("records"),
        _stock_payload, cfg, locked_through_h=_lock_h, caps=caps)
    _as_of = _stock_payload["as_of"]
    _sup_cap = (f"Supply: {_sup['short']} short · {_sup['dependent']} "
                f"delivery-dependent · {_sup['no_data']} no data · "
                f"stock {_as_of['stock_rm']} · POs {_as_of['po']}")
    if _stock_cached.stale:
        _sup_cap += " · report stale — press Refresh on Stock Check"
    st.caption(_sup_cap)
    if _sup["short"] > 0:
        st.warning(
            f"⛔ {_sup['short']} block(s) run out of a component before any "
            "counted PO lands — open the block for the PO to chase, or move "
            "it past its safe-from hour.")
else:
    st.caption("Supply check: no stock report yet — open Stock Check and "
               "press Refresh from VIF.")

# Fingerprint the pushed board state: order_id + kg + LINE + START of
# production rows. Positions are in the hash deliberately (2026-09-01):
# crediting is position-aware now, and a pure MOVE previously left the
# fingerprint unchanged — "Refresh checks" recomputed the same stale
# holding. When it changes, store it and rerun ONCE so holding re-derives.
try:
    _wp = working[working["block_type"] == "production"]
    _sig = hash(tuple(sorted(
        (str(r.get("order_id", "")),
         str(r.get("line_id", "")),
         round(float(r.get("start_h", 0) or 0), 1),
         round(float(pd.to_numeric(pd.Series([r.get("qty_kg")]),
                                   errors="coerce").iloc[0] or 0), 1))
        for _, r in _wp.iterrows())))
except Exception:  # noqa: BLE001
    _sig = None
if _sig is not None and st.session_state.get("cal_board_sig") != _sig:
    st.session_state["cal_board_sig"] = _sig
    st.session_state["cal_working_records"] = working.to_dict("records")
    # The component's holding snapshot is adopted ONLY on a real push —
    # on any other rerun it is the PREVIOUS push's state and would
    # resurrect pre-rebuild cards (review 2026-09-01).
    st.session_state["cal_holding"] = holding
    st.rerun()

n_holding = len(holding)

# ── Live score (same engine as Phase 0) ─────────────────────────────────
# Re-scoring an unchanged board wastes ~1-2s on every page visit / widget
# click. Fingerprint the board (positions matter — cal_board_sig above only
# covers order+qty, which is NOT enough for a score key) PLUS every off-board
# scoring input (demand plan, rates, CIP intervals, toml — review 2026-08-19:
# a feed sync must invalidate this cache exactly like the Compare/Generate
# caches, or the scorecard and the freshly-computed KPI bar disagree).
_score_cols = [c for c in (
    "order_id", "sku", "line_name", "block_type", "start_h", "end_h",
    "start_hour", "end_hour", "qty_kg", "attrs") if c in working.columns]
try:
    _live_sig = hash((
        int(pd.util.hash_pandas_object(
            working[_score_cols].astype(str), index=False).sum()),
        scoring_inputs_signature(dd),
    ))
except Exception:  # noqa: BLE001 — cache key failure just means recompute
    _live_sig = None
if (_live_sig is not None
        and st.session_state.get("cal_live_score_sig") == _live_sig
        and st.session_state.get("cal_live_score")):
    live = ScorecardResult.from_dict(st.session_state["cal_live_score"])
else:
    live = score_calendar(working, week_label="what-if", data_dir=dd)
    if _live_sig is not None:
        st.session_state["cal_live_score_sig"] = _live_sig
        st.session_state["cal_live_score"] = live.to_dict()
# A what-if that fails the geometry sanity check or lost a scoring input
# still shows its numbers, but the composite is NOT rankable (fix Q /
# adversarial-7, scorecard-1; INTEGRATE applying the Q->W handoff).
if not getattr(live, "rankable", True):
    _why = [n for n in (getattr(live, "notes", None) or [])
            if str(n).startswith(("SANITY", "SCORING INPUT MISSING"))]
    st.warning("What-if composite is not rankable — "
               + ("; ".join(str(n) for n in _why[:4]) if _why
                  else "sanity errors or missing scoring inputs (see notes)"))
baseline_dict = st.session_state.get("cal_baseline_score") or {}
baseline = ScorecardResult.from_dict(baseline_dict) if baseline_dict else None

# THE FULL BOARD for every write path (fix writeback-3 / writeback-12,
# 2026-09-03): hidden (already-finished) rows are merged back so hiding the
# past never deletes it — from disk, from a named version (whose promote
# would otherwise erase finished production) or from the Excel export.
# Display-only overlays (cip_info / downtimes) are stripped — they are a
# live-feed visualization, not calendar data.
_full_board = drop_display_overlays(
    working if _past_rows.empty
    else pd.concat([_past_rows, working], ignore_index=True))

# ── Save requests from the board (Save / Save as version / Ctrl+S) ───────
# The component pushes its LIVE board together with {kind, nonce}; the nonce
# is remembered so a rerun that re-reads the same component value never
# saves twice. Both kinds write `_full_board` (above). A disk save is also
# guarded server-side: the 2-week lock (fix writeback-9 — the Gantt's
# immovable flag alone did not stop drops INTO the window or a stale mount
# from re-planning it; the override lives in the control row) and the
# file's mtime at mount (fix writeback-13 — a board changed underneath is
# backed up and the planner told).
_req = state.get("saveRequest") if isinstance(state, dict) else None
if isinstance(_req, dict) and _req.get("nonce") \
        and st.session_state.get("cal_save_nonce_done") != _req.get("nonce"):
    st.session_state["cal_save_nonce_done"] = _req.get("nonce")
    _stamp = f"{datetime.now():%H:%M}"
    if _req.get("kind") == "version":
        try:
            # planning_anchor=_anchor: this board's hours are offsets from
            # the STORAGE anchor, not the rolling one — stamping the rolling
            # anchor shifted every block by the un-rolled gap on promote.
            slug = save_version(
                (st.session_state.get("cal_save_name") or "Option").strip() or "Option",
                _full_board,
                live.to_dict(),
                dd,
                source="digital_twin",
                planning_anchor=_anchor,
            )
            st.session_state["cal_holding"] = []
            st.session_state["cal_last_saved"] = f"{_stamp} (version {slug})"
            st.toast(f"Saved version `{slug}` — see Compare & Promote.", icon=":material/bookmark:")
            if n_holding:
                st.toast(f"Version saved without {n_holding} held block(s).", icon=":material/warning:")
            st.rerun()
        except (ValueError, OSError) as e:
            # Friendly message, not a stack trace — capacity errors name the
            # orphaned folders so the planner knows what to delete.
            st.error(str(e))
    else:
        from helpers.week_lock import LockViolation as _LockViolation
        try:
            _res = save_calendar(
                _full_board, cal_path,
                expected_mtime=st.session_state.get("cal_mount_mtime"),
                lock_h=_lock_h,
                lock_override=bool(st.session_state.get("cal_lock_override", False)))
        except _LockViolation as _lv:
            # Refused, nothing written: the message stays on screen (no rerun)
            # so the planner can tick the override in the control row and
            # press Save again.
            st.error("🔒 Not saved — " + str(_lv).replace("\n", "  \n"))
        else:
            st.session_state["cal_mount_mtime"] = (
                cal_path.stat().st_mtime if cal_path.exists() else None)
            st.session_state["cal_holding"] = []
            st.session_state.pop("cal_baseline_score", None)
            st.session_state["cal_last_saved"] = _stamp
            # Save-time notices (file changed underneath → backup taken, …)
            # must survive the rerun below: the attention strip shows them
            # once on the next run.
            _notes = list(_res.get("warnings") or []) if isinstance(_res, dict) else []
            _bk = _res.get("backup") if isinstance(_res, dict) else None
            if _bk:
                _notes.append(f"Previous board backed up to `{Path(_bk).name}`.")
            if _notes:
                st.session_state["cal_save_notes"] = _notes
            st.toast(f"Saved calendar_blocks.csv ({len(_full_board)} blocks) at {_stamp}.",
                     icon=":material/save:")
            if n_holding:
                st.toast(f"Saved without {n_holding} held block(s) — holding cleared.",
                         icon=":material/warning:")
            st.rerun()

# ── Under the board: holding note, one-sided downtime, lock & export ──────
_under = []
if n_holding:
    _under.append(chip(f"{n_holding} in holding — not written on Save", "warn", icon="▤"))
if _auto_held:
    _under.append(chip(f"{_auto_held} under-target demand order(s) auto-placed in holding", "info"))
if _aged_out:
    _under.append(chip(f"{_aged_out} past-week card(s) removed from holding", "neutral"))
if _one_sided:
    _under.append(chip("one-sided (half-rate) downtime: " + ", ".join(_one_sided), "info"))
if _holding_note:
    _under.append(chip(_holding_note, "warn"))
if _dt_note:
    _under.append(chip(_dt_note, "warn"))
if _under:
    render_chips(_under)

section_label("Lock & export")
_lc1, _lc2, _lc3, _lc4 = st.columns([1.6, 1, 1.6, 3])
with _lc1:
    # Two whole weeks from TODAY (the rolling anchor), not from the storage
    # anchor: on a stale board the latter locked less than two weeks
    # (audit writeback-15 side note, 2026-09-03).
    _default_lock = default_lock_through(_horizon.anchor)
    if st.button(f"🔒 Lock through {_default_lock:%a %m-%d}",
                 use_container_width=True,
                 help="Freeze weeks 1–2: every block starting before the boundary "
                      "is committed to the plant."):
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
    # The Export half of "Lock & Export": the schedule as shown on screen
    # (last pushed edits included), Calendar + Scorecard sheets. The VIF
    # write-back (mo_changes.csv) stays on Compare & Promote — it is per
    # solver run. Workbook bytes ride the live-score signature (board +
    # every scoring input incl. the toml anchor) — rebuilding a full
    # openpyxl workbook per rerun is exactly the waste the score cache
    # above exists to avoid (review 2026-08-19).
    if (_live_sig is None
            or st.session_state.get("cal_export_sig") != _live_sig
            or "cal_export_bytes" not in st.session_state):
        # Whole board incl. finished rows, flagged in a 'status' column
        # (writeback-12); rendered from the storage anchor the hours live in.
        st.session_state["cal_export_bytes"] = export_calendar_excel(
            _full_board, live.to_dict(), anchor=_anchor, now_h=_now_h)
        st.session_state["cal_export_sig"] = _live_sig
    st.download_button(
        "⬇ Export schedule (Excel)",
        data=st.session_state["cal_export_bytes"],
        file_name=f"flowstate_schedule_{datetime.now():%Y%m%d_%H%M}.xlsx",
        use_container_width=True,
    )
with _lc4:
    note("2 weeks locked and ready, week 3 flexible: locking freezes every block that "
         "starts before the boundary (no drag/resize/edit inside; the weekly roll "
         "advances it). Export = the whole board as last checked, including blocks that "
         "already finished (flagged `completed` in the status column), Calendar + "
         "Scorecard sheets; MO changes for VIF write-back live on Compare & Promote.")

# ── Tabs: everything that used to sit above the board ────────────────────
_tab_score, _tab_plant, _tab_dt, _tab_run = st.tabs([
    "📊 Live score",
    "🏭 Plant state & float links",
    "🌅 Downtime",
    f"▶ Now running ({len([r for r in _now_running if 0 < r['pct'] < 100])})",
])

with _tab_score:
    if baseline is not None:
        render_delta_strip(baseline, live, title="Δ vs current schedule (on disk)")
        for line in delta_narrative(baseline, live)[:8]:
            st.caption(line)
    render_scorecard(live, show_formulas=False)

with _tab_plant:
    section_label("Rebuild the calendar from the current plant state (manprg + cip_info)")
    if _cs_err:
        st.error(_cs_err)
    if _cs is not None:
        _c = _cs.counts
        # manprg CONTENT age (fix CA-1 / C03): the re-forecast measures the
        # running MOs' pace up to the export's observation time, not the
        # render clock — say when that was (INTEGRATE, agent CA handoff).
        _asof = getattr(_cs, "as_of", None)
        _asof_txt = ""
        if _asof is not None:
            try:
                _age_h = (_cs.now - _asof).total_seconds() / 3600.0
                _asof_txt = (f" manprg content observed {_asof:%a %m-%d %H:%M} "
                             f"({_age_h:.1f} h ago, "
                             f"{getattr(_cs, 'as_of_source', '') or 'stamp'}).")
            except Exception:  # noqa: BLE001 — caption only
                _asof_txt = ""
        st.caption(
            f"Ground truth: **{_c['running']}** running MO(s) (locked), "
            f"**{_c['queued']}** queued, **{_c['completed']}** completed "
            f"(not shown), **{_c['cip']}** CIP block(s) → "
            f"{_c['blocks']} blocks." + _asof_txt)
        for _w in _cs.warnings[:6]:
            st.caption(f"⚠️ {_w}")
        if len(_cs.blocks):
            _prev = _cs.blocks.copy()
            _prev["start"] = _prev["start_h"].map(
                lambda h: (_horizon.anchor + _td(hours=float(h))).strftime("%a %m-%d %H:%M"))
            _prev["end"] = _prev["end_h"].map(
                lambda h: (_horizon.anchor + _td(hours=float(h))).strftime("%a %m-%d %H:%M"))
            deferred_dataframe(
                _prev[["line_name", "block_type", "label", "order_id", "start",
                       "end", "locked", "attrs"]],
                key="cal_plant_state_preview", label="Load the block preview",
                use_container_width=True, hide_index=True, height=280)
        st.caption("Replacing backs up the current calendar to `data/_backups/` first.")
        if st.button("Replace calendar with current plant state",
                     key="cal_from_plant_state", type="primary",
                     disabled=not len(_cs.blocks)):
            _backup_calendar(cal_path, dd)
            save_calendar(_cs.blocks, cal_path)
            st.session_state.pop("cal_baseline_score", None)
            # Remount the Gantt or it keeps showing the PRE-replace board
            # (the component holds its own state under a stable key).
            st.session_state["cal_reset_gen"] += 1
            st.toast(f"Calendar rebuilt from plant state ({_c['blocks']} blocks).", icon=":material/factory:")
            st.rerun()

    st.divider()
    section_label(f"Float links ({_float_n} active)")
    st.caption(
        "Tie a block's START to another block's END: when a running MO's "
        "end re-forecasts from actual cases, its follower moves by the same "
        "amount (gap at link time is preserved; linking also pins the "
        "block). The attention row above the board previews drift — the "
        "board only moves when you apply.")
    _prod = _cal_now[(_cal_now["block_type"] == "production")
                     & ~_cal_now["attrs"].astype(str).str.contains(
                         "current_state:", regex=False)]
    if _prod.empty:
        st.caption("No linkable production blocks on the board.")
    else:
        def _blabel(r) -> str:
            return (f"{r['line_name']} · {r.get('label') or r.get('sku')} "
                    f"@ {float(r['start_h']):.1f}h")
        _by_id = {str(r["block_id"]): r for _, r in _cal_now.iterrows()}
        _opts = {_blabel(r): str(r["block_id"]) for _, r in _prod.iterrows()}
        _sel = st.selectbox("Block", list(_opts), key="float_pick")
        _bid = _opts[_sel]
        _cur = float_link_of(_by_id[_bid].get("attrs"))
        if _cur:
            _a = _by_id.get(_cur[0])
            st.caption(f"Currently follows: "
                       f"{_blabel(_a) if _a is not None else _cur[0]} "
                       f"(gap {_cur[1]:+.2f}h)")
            if st.button("Unlink", key="float_unlink"):
                save_calendar(clear_float_link(_cal_now, _bid), cal_path)
                st.rerun()
        else:
            _row = _by_id[_bid]
            _prev = _cal_now[
                (_cal_now["line_id"].astype(str) == str(_row["line_id"]))
                & (_cal_now["block_type"] == "production")
                & (_cal_now["end_h"].astype(float)
                   <= float(_row["start_h"]) + 1e-6)
                & (_cal_now["block_id"].astype(str) != _bid)]
            if _prev.empty:
                st.caption("No preceding production block on this line.")
            else:
                _fl_anchor = _prev.sort_values("end_h").iloc[-1]
                st.caption(f"Will follow: {_blabel(_fl_anchor)}")
                if st.button("Link to preceding block", key="float_link"):
                    try:
                        save_calendar(set_float_link(
                            _cal_now, _bid, str(_fl_anchor["block_id"])), cal_path)
                        st.rerun()
                    except ValueError as _exc:
                        st.error(str(_exc))

with _tab_dt:
    # Scheduled downtime per side — set before scheduling production; the
    # board stretches blocks over one-sided hours. This editor is the ONLY
    # writer of downtimes.csv.
    render_side_downtime_editor(dd, key_prefix="cal_dt")

with _tab_run:
    # Only MOs actually in progress (not completed, not future) belong here.
    _active = [r for r in _now_running if 0 < r["pct"] < 100]
    if _active:
        _desig = {line: lp.designation for line, lp in _mp.current.items()}
        _nr = sorted(_active, key=lambda r: r["line"])
        st.table(pd.DataFrame(
            [{"Line": r["line"], "MO": r["mo"], "SKU": r["item"],
              "Designation": _desig.get(r["line"], ""),
              "Completion": f"{r['pct']:.1f}%",
              "Cases left": int(r["left"])} for r in _nr]).set_index("Line"))
    else:
        st.caption("No MO is running right now (live from manprg).")
