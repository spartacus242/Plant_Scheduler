# pages/compare.py — Side-by-side version / scenario scorecard compare.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.calendar_io import load_calendar
from helpers.labels import display_name
from helpers.paths import data_dir, versions_dir
from helpers.scorecard_engine import (
    ScorecardResult,
    delta_narrative,
    score_calendar,
    scoring_inputs_signature,
)
from helpers.scorecard_ui import render_delta_strip, render_scorecard
from helpers.version_manager import (
    MAX_VERSIONS,
    calendar_in_board_frame,
    delete_all_versions,
    delete_version,
    export_version_excel,
    list_versions,
    load_version,
    promote_version,
    rename_version,
    update_notes,
)
from helpers.week_lock import LockViolation

st.header("Version Compare")
st.caption(
    "Compare named options and solver scenarios against the current schedule. "
    "Raw metrics drive 'show me why'; the composite is only a conversation starter."
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


dd = data_dir()

# ── Rerun speed (2026-08-19): scoring every version and rebuilding every
# Excel export on EVERY Streamlit rerun made this page take 10-20 s per
# widget click. Everything expensive is cached below, keyed by the files it
# actually reads: (slug, metadata.json mtime, calendar_blocks.csv mtime) for
# versions, the board file's mtime for the official calendar — plus the live
# scoring inputs' signature, because scores are computed against TODAY's
# feeds and must refresh when they move. NOTHING is keyed by slug alone.


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


_LIVE_SIG = scoring_inputs_signature(dd)
_OFFICIAL_PATH = dd / "calendar_blocks.csv"


def _vkey(slug: str) -> tuple[str, float, float]:
    vdir = versions_dir(dd) / slug
    return (slug, _mtime(vdir / "metadata.json"),
            _mtime(vdir / "calendar_blocks.csv"))


@st.cache_data(show_spinner=False)
def _board_cached(dd_str: str, cal_mtime: float, live_sig: tuple):
    """(calendar, ScorecardResult|None) for the on-disk official board."""
    cal = load_calendar(Path(dd_str) / "calendar_blocks.csv")
    if cal.empty:
        return cal, None
    return cal, score_calendar(cal, week_label="official", data_dir=Path(dd_str))


@st.cache_data(show_spinner=False)
def _version_cached(dd_str: str, slug: str, meta_mtime: float,
                    cal_mtime: float, live_sig: tuple, week_label: str):
    """(calendar, metadata, ScorecardResult) for one saved version.

    The calendar is returned in the version's OWN frame (the preview renders
    it with the version's anchor). Scoring happens in the BOARD frame (agent
    W handoff, writeback-2 tail): a version solved on a newer rolling anchor
    has its ISO weeks / CIP intervals misaligned by the frame gap when its
    raw hours are scored. An unstamped legacy version is scored raw.
    """
    data = load_version(slug, Path(dd_str))
    cal_for_score = data["calendar"]
    try:
        _board = load_calendar(Path(dd_str) / "calendar_blocks.csv")
        cal_for_score, _shift = calendar_in_board_frame(
            data["calendar"], slug, Path(dd_str), board=_board)
    except ValueError:
        pass  # no planning_anchor stamp: score the raw hours (legacy)
    res = score_calendar(cal_for_score, week_label=week_label,
                         data_dir=Path(dd_str))
    return data["calendar"], data["metadata"], res


@st.cache_data(show_spinner=False)
def _window_score_cached(dd_str: str, cache_key: tuple, week_label: str,
                         fill_gates: dict, live_sig: tuple,
                         _cal: pd.DataFrame, _fill_ledger: dict | None = None):
    # `_fill_ledger` (agent Q handoff 3 / C57): the staging ledger legs the
    # proposal was netted against, forwarded so the fill-window residual is
    # the SAME subtraction the solver saw. Unhashed (leading underscore): it
    # comes from metadata.json whose mtime is already in cache_key.
    return score_calendar(_cal, week_label=week_label, data_dir=Path(dd_str),
                          fill_gates=fill_gates,
                          ledger_inputs=(_fill_ledger or None))


def _fill_ledger_inputs(meta: dict | None) -> dict | None:
    """Rebuild score_calendar's `ledger_inputs` from a version's metadata.

    Fix V (agent Q handoff 3, C57 / netting-5): staging nets the demand with
    build_ledger(completed=..., history_demand=..., anchor=..., lookback_weeks=...);
    the Compare fill-window view re-nets with plan_fill.subtract_committed and,
    without those legs, counted kg already in the warehouse as still due.
    The legs are persisted with the version under metadata["fill_ledger"]
    (HANDOFF C: scenario_runner.save_scenario_version writes it, JSON-safe:
    "anchor" as "%Y-%m-%d %H:%M:%S", "history_demand" as {"<sku>|<iso_week>": kg},
    "completed" rows with start_dt/end_dt as strings, "lookback_weeks" int).
    Versions saved without it -> None (the view falls back to the plain
    subtraction, exactly as before). Never raises.
    """
    fl = (meta or {}).get("fill_ledger")
    if not isinstance(fl, dict) or not fl:
        return None
    out: dict = {}
    try:
        from datetime import datetime as _dt
        a = fl.get("anchor")
        if a:
            out["anchor"] = a if isinstance(a, _dt) else _dt.strptime(str(a), "%Y-%m-%d %H:%M:%S")
        hd = fl.get("history_demand")
        if isinstance(hd, dict):
            conv: dict = {}
            for k, v in hd.items():
                if isinstance(k, tuple):
                    conv[k] = float(v)
                else:
                    sku, _, wk = str(k).rpartition("|")
                    conv[(sku, int(wk))] = float(v)
            out["history_demand"] = conv
        comp = fl.get("completed")
        if isinstance(comp, list):
            rows = []
            for r in comp:
                r = dict(r)
                for c in ("start_dt", "end_dt"):
                    if r.get(c) not in (None, ""):
                        r[c] = pd.to_datetime(r[c])
                rows.append(r)
            out["completed"] = rows
        if fl.get("lookback_weeks") is not None:
            out["lookback_weeks"] = int(fl["lookback_weeks"])
    except (ValueError, TypeError, KeyError):
        return None
    return out or None


@st.cache_data(show_spinner=False)
def _weekly_cached(dd_str: str, cache_key: tuple, live_sig: tuple,
                   _cal: pd.DataFrame):
    from helpers.scorecard_engine import weekly_breakdown as _wb
    return _wb(_cal, data_dir=Path(dd_str))


@st.cache_data(show_spinner=False)
def _excel_cached(dd_str: str, slug: str, meta_mtime: float,
                  cal_mtime: float, live_sig: tuple) -> bytes:
    # live_sig carries the toml mtime. The workbook uses the version's OWN
    # planning anchor for its human start/end columns (agent W, 2026-09-03),
    # so keying by the toml mtime is merely harmless over-invalidation.
    return export_version_excel(slug, Path(dd_str))


def _version_validation(meta: dict | None) -> tuple[str, int | None]:
    """(one-line validation verdict, physical error count or None).

    Fix V-1 (C81): a saved version carries the solver's feasibility record
    (relax level, setup_times_enforced, independent-validator counts) in its
    metadata under ``feasibility`` once scenario_runner.save_scenario_version
    stores it (handoff). Older versions have none -> "not recorded".
    """
    feas = (meta or {}).get("feasibility") or {}
    if not feas:
        return ("validation: not recorded for this version (saved before the "
                "solver carried the independent validator's verdict)"), None
    val = feas.get("validation") or {}
    bits = [f"relax level {feas.get('relax_level', '?')} ({feas.get('relax_mode', '?')})"]
    if feas.get("setup_times_enforced") is False:
        bits.append("UNSAFE: changeover times NOT enforced")
    phys = val.get("physical_errors")
    if val.get("n_errors") is None:
        bits.append("independent validator: not run")
    else:
        bits.append(f"validator: {val.get('n_errors', 0)} error(s) "
                    f"({phys or 0} physical), {val.get('n_warnings', 0)} warning(s)")
    tp = feas.get("two_pass") or {}
    if tp.get("adopted"):
        bits.append(f"two-pass: {tp['adopted']} adopted")
    return "validation: " + " · ".join(bits), (None if phys is None else int(phys))


_all_versions = list_versions(dd)
# Orphans (folders without readable metadata) hold a slot but have nothing to
# compare — they only appear in the cleanup section below.
orphan_versions = [v for v in _all_versions if v.get("orphan")]
versions = [v for v in _all_versions if not v.get("orphan")]


def _render_orphans() -> None:
    if not orphan_versions:
        return
    st.subheader("Orphaned version folders")
    st.caption(
        f"Folders under `data/versions/` without readable metadata — "
        f"leftovers from crashes or hand copies. They count toward the "
        f"{MAX_VERSIONS}-version limit; delete them to free slots.")
    for _ov in orphan_versions:
        _oc1, _oc2 = st.columns([5, 1])
        _oc1.markdown(f"`{_ov['slug']}`")
        if _oc2.button("Delete", key=f"del_orphan_{_ov['slug']}"):
            delete_version(_ov["slug"], dd)
            st.rerun()


# Official baseline score for reference (cached by board mtime + live inputs)
official, baseline = _board_cached(str(dd), _mtime(_OFFICIAL_PATH), _LIVE_SIG)

OFFICIAL_KEY = "__official__"

if not versions and baseline is None:
    st.info("No versions yet. Save one from the Plant Calendar or Generate Scenarios.")
    _render_orphans()
    st.stop()

if baseline:
    st.subheader("Current schedule (official calendar)")
    render_scorecard(baseline, show_formulas=False, show_contribution=True)

if not versions:
    st.info("No named versions yet — official calendar is shown above. Save options from Plant Calendar or Generate Scenarios.")
    _render_orphans()
    st.stop()

# Side-by-side picker — default left = official AZAP when available
# display_name() remaps legacy on-disk names (e.g. the azap_baseline slug)
# without renaming anything under data/versions/.
names = {v["slug"]: display_name(v["slug"], v.get("name")) for v in versions}
names_meta = {v["slug"]: v for v in versions}

# ── Version colors: every version's data is tinted with ITS OWN color
# everywhere on this page; the official schedule stays default/white
# (user request 2026-08-14: make it unmistakable WHOSE numbers you see).
_PALETTE = [("blue", "#4da6ff"), ("orange", "#ffa421"), ("green", "#21c354"),
            ("violet", "#a463f2"), ("red", "#ff4b4b")]


def _vcolor(slug: str) -> tuple[str, str]:
    """(streamlit-markdown-color, hex) — stable per slug."""
    import hashlib as _hl
    i = int(_hl.md5(slug.encode()).hexdigest(), 16) % len(_PALETTE)
    return _PALETTE[i]


def _cname(slug: str) -> str:
    """Version name wrapped in its color for markdown surfaces."""
    if slug == OFFICIAL_KEY:
        return "Current schedule (official)"
    md, _ = _vcolor(slug)
    return f":{md}[{names.get(slug, slug)}]"


st.markdown(
    "Colors: **official = white** · "
    + " · ".join(f":{_vcolor(sl)[0]}[● {nm}]" for sl, nm in names.items()))
left_options = ([OFFICIAL_KEY] if baseline else []) + list(names.keys())

def _fmt_left(s: str) -> str:
    if s == OFFICIAL_KEY:
        return "Current schedule (official)"
    return names.get(s, s)

default_left = OFFICIAL_KEY if baseline else list(names.keys())[0]
c1, c2 = st.columns(2)
with c1:
    left_slug = st.selectbox(
        "Baseline (left)",
        left_options,
        index=left_options.index(default_left) if default_left in left_options else 0,
        format_func=_fmt_left,
        key="cmp_left",
    )
with c2:
    right_opts = [s for s in names if s != left_slug] or list(names.keys())
    # Prefer something other than the imported-schedule snapshot when left is official
    preferred = next((s for s in right_opts if s != "azap_baseline"), right_opts[0])
    right_slug = st.selectbox(
        "Proposed (right)",
        right_opts,
        index=right_opts.index(preferred) if preferred in right_opts else 0,
        format_func=lambda s: names[s],
        key="cmp_right",
    )

# Every side is scored FRESH against the SAME live data, in this pass.
# Stored scorecards are fossils of the data at save time (live feeds move
# every ~30 min), so mixing them with fresh numbers made the page disagree
# with itself and with the Plant Calendar (user report 2026-08-14).
def _blocks_signature(cal) -> str:
    import hashlib
    key = cal[["line_name", "block_type", "start_h", "end_h", "sku"]]         .sort_values(["line_name", "start_h"]).to_csv(index=False)
    return hashlib.md5(key.encode()).hexdigest()

_official_sig = _blocks_signature(official) if not official.empty else ""

if left_slug == OFFICIAL_KEY:
    left_cal = official
    left_label = "Current schedule (official)"
    left_res = baseline
    left_stored = None
    _left_key: tuple = (OFFICIAL_KEY, _mtime(_OFFICIAL_PATH))
else:
    left_label = names[left_slug]
    left_cal, _left_meta, left_res = _version_cached(
        str(dd), *_vkey(left_slug), _LIVE_SIG, left_label)
    left_stored = (_left_meta.get("scorecard") or {}).get("composite")
    _left_key = _vkey(left_slug)
left_sc = left_res.to_dict() if left_res else {}

right_label = names[right_slug]
_right_key = _vkey(right_slug)
right_cal, right_meta, right_res = _version_cached(
    str(dd), *_right_key, _LIVE_SIG, right_label)
right_stored = (right_meta.get("scorecard") or {}).get("composite")
right_sc = right_res.to_dict()

for _slug_b, _lbl, _cal_df, _stored, _res in (
        (left_slug, left_label, left_cal, left_stored, left_res),
        (right_slug, right_label, right_cal, right_stored, right_res)):
    _bits = []
    if _stored is not None and _res is not None:
        _fresh_comp = _res.to_dict().get("composite")
        if _fresh_comp is not None and abs(float(_stored) - float(_fresh_comp)) >= 0.5:
            _bits.append(f"scored {_fresh_comp} against TODAY's live data "
                         f"(was {_stored} when saved)")
    if not official.empty and _blocks_signature(_cal_df) == _official_sig:
        _bits.append("this plan IS the current official calendar")
    if _bits:
        st.info(f"**{_cname(_slug_b)}:** " + " · ".join(_bits))
    # Agent Q handoff 2 (adversarial-7 / scorecard-9): a result whose
    # geometry failed the sanity pass or whose categories degraded to
    # defaults is NOT rankable - its composite is shown for reading, never
    # compared as better/worse (the metrics table below blanks the verdict).
    if _res is not None and not getattr(_res, "rankable", True):
        _why = (list(getattr(_res, "sanity", None) or [])
                + list(getattr(_res, "degraded", None) or []))
        st.warning(f"**{_cname(_slug_b)}** — composite NOT rankable: "
                   + ("; ".join(str(w) for w in _why[:4]) or "composite is n/a")
                   + (" …" if len(_why) > 4 else ""))
    if _slug_b != OFFICIAL_KEY:
        _vline, _vphys = _version_validation(names_meta.get(_slug_b))
        (st.error if _vphys else st.caption)(f"**{_cname(_slug_b)}** — {_vline}")

# ── Fill-window verdict (Scenario F proposals) ────────────────────────────
# An F proposal shares its committed layer (manprg MOs, trials, projected
# CIPs) with the official board BY CONSTRUCTION — the solver only decided
# the fill region after each line's committed tail. Scoring whole calendars
# against each other charged the proposal for changeovers/CIP the fill
# necessarily costs while comparing unequal scopes (~2-week board vs 3-week
# plan). The honest headline is both sides windowed to the fill region with
# the SAME gates the solve was staged with (design doc
# scenario-f-fill-the-tail-2026-08-14: honesty rules). Full-horizon numbers
# remain below for the whole-board picture.
_fill_gates = (right_meta or {}).get("fill_gates")
if _fill_gates:
    _fill_ledger = _fill_ledger_inputs(right_meta)
    _lw = _window_score_cached(str(dd), _left_key,
                               f"{left_label} (fill window)", _fill_gates,
                               _LIVE_SIG, left_cal, _fill_ledger)
    _rw = _window_score_cached(str(dd), _right_key,
                               f"{right_label} (fill window)", _fill_gates,
                               _LIVE_SIG, right_cal, _fill_ledger)
    if _fill_ledger is None:
        st.caption("Residual demand netted WITHOUT the staging ledger legs "
                   "(completed MOs / history): this version carries no "
                   "fill_ledger record, so warehouse stock may still count as due.")
    st.subheader("Fill-window verdict — what the solver actually decided")
    st.caption(
        "Both plans are cut to the fill region (after each line's committed "
        "tail, same gates the solve was staged with) and judged against the "
        "residual demand left after committed production. The committed "
        "layer is identical on both sides and cancels out.")
    _c1, _c2, _c3 = st.columns(3)
    _lc = _lw.composite
    _rc = _rw.composite
    _c1.metric(f"{left_label} — fill window",
               "n/a" if _lc is None else f"{_lc:g}")
    _c2.metric(f"{right_label} — fill window",
               "n/a" if _rc is None else f"{_rc:g}")
    if _lc is not None and _rc is not None:
        _c3.metric("Δ composite (fill window)", f"{_rc - _lc:+.1f}")
    render_delta_strip(_lw, _rw,
                       title=f"Δ fill window: {_cname(right_slug)} vs "
                             f"{_cname(left_slug)}")
    with st.expander("Show me why (fill window)"):
        _wdeltas = delta_narrative(_lw, _rw)
        if _wdeltas:
            for _d in _wdeltas:
                st.write(f"- {_d}")
        else:
            st.caption("No material differences inside the fill window.")

# ── Visual preview: SEE the proposed plan before promoting it ─────────────
# (user request 2026-08-14: visual confirmation that a proposed schedule
# actually looks right before it overwrites the official one.)
with st.expander(f"📅 Preview {_cname(right_slug)} on a calendar (read-only)",
                 expanded=True):
    from components.gantt import gantt_calendar
    from helpers.calendar_io import calendar_to_gantt_payload, load_lines
    from helpers.config import load_toml as _lt
    from helpers.timefmt import planning_anchor as _pa

    from helpers.lines_model import expand_caps_with_groups
    from helpers.paths import reference_dir

    _cfg_prev = _lt()
    _anchor_prev = _pa(_cfg_prev)
    # Render the preview in the VERSION'S OWN frame: a version solved on a
    # newer rolling anchor than the un-rolled board would otherwise draw a
    # week early (frame-shift bug, 2026-08-17).
    if right_slug != OFFICIAL_KEY:
        try:
            from datetime import datetime as _dtf

            _va = str((names_meta.get(right_slug) or {}).get(
                "planning_anchor") or "").strip()
            if _va:
                _anchor_prev = _dtf.strptime(_va, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, AttributeError, NameError):
            pass
    _sched_prev, _win_prev = calendar_to_gantt_payload(right_cal)
    # read-only: every block locked, so drags/edits are rejected in place
    for _b in _sched_prev + _win_prev:
        _b["locked"] = True
    _lines_df = load_lines(dd / "lines.csv")
    _line_cols = [c for c in ("line_id", "line_name", "line_group", "side",
                              "is_double") if c in _lines_df.columns]
    # REAL capabilities + demand: the KPI bar (adherence / orders met /
    # changeovers) computes from these — empty inputs showed 0/0 nonsense
    # (user report 2026-08-14).
    _caps_prev: dict = {}
    _caps_path = reference_dir(dd) / "capabilities_rates.csv"
    if _caps_path.exists():
        from helpers.effective_rates import load_effective_capabilities
        _cdf = load_effective_capabilities(_caps_path, dd=dd)
        for _, _r in _cdf.iterrows():
            if int(_r.get("capable", 0) or 0) == 1:
                _caps_prev.setdefault(str(_r["line_name"]), {})[str(_r["sku"])] =                     float(_r.get("calc_rate_kgph") or 0)
    _caps_prev = expand_caps_with_groups(_caps_prev)
    # Agent Q handoff 1 (ui-2): ONE rule for the demand targets every
    # adherence surface consumes. The old inline loop used the demand file's
    # RAW hours; the preview is drawn in the version's frame (_anchor_prev),
    # so the targets are shifted into that frame like calendar.py does.
    _dem_prev = []
    try:
        from helpers.scorecard_engine import build_demand_targets as _bdt
        _dem_prev = _bdt(dd, anchor=_anchor_prev)
    except Exception as _dexc:  # noqa: BLE001 — the preview must still draw
        st.caption(f"Demand targets unavailable for the KPI bar: {_dexc}")
        _dem_prev = []
    st.caption("Preview only — blocks are locked; nothing here changes any "
               "saved plan. Promote below when it looks right.")
    # ui-3 (audit 2026-09-03): without a kpis payload and a changeovers map
    # the Gantt's KPI bar fell back to its client-side default classifier
    # (every pair = 1 recipe change, 0 format, 1.5 h) — on the frozen board it
    # read 85/85/0/127.5 h against the truth 85/85/85/141 h. Pass the SAME
    # server-computed numbers the Plant Calendar shows (gantt_kpis) and the
    # real per-pair setup map, exactly as pages/generate.py does.
    _kpis_prev = None
    _co_prev: dict = {}
    try:
        from helpers.scorecard_engine import gantt_kpis as _gk
        from solver.changeover_cache import load_changeover_setup_nested as _lcs
        _co_p = reference_dir(dd) / "changeovers.csv"
        if _co_p.exists():
            _co_prev = _lcs(_co_p)
        _kpis_prev = _gk(right_cal, _dem_prev, _caps_prev, data_dir=dd)
    except Exception as _kexc:  # noqa: BLE001
        st.warning(f"KPI bar: server changeover numbers unavailable ({_kexc}) — "
                   "the bar falls back to the client's default classification; "
                   "do NOT read its changeover figures.")
        _kpis_prev = None
    gantt_calendar(
        schedule=_sched_prev,
        cip_windows=_win_prev,
        capabilities=_caps_prev,
        changeovers=_co_prev,
        kpis=_kpis_prev,
        demand_targets=_dem_prev,
        lines=_lines_df[_line_cols].to_dict("records") if len(_lines_df) else [],
        holding_area=[],
        side_downtime={},
        config={
            "planning_anchor": f"{_anchor_prev:%Y-%m-%d %H:%M:%S}",
            "demand_base_iso_week": _demand_base_iso_week(),
            "cip_duration_h": int((_cfg_prev.get("cip", {}) or {}).get("duration_h", 6)),
            "min_run_hours": int((_cfg_prev.get("scheduler", {}) or {}).get("min_run_hours", 4)),
            "horizon_hours": int((_cfg_prev.get("scheduler", {}) or {}).get("horizon_hours", 504)),
        },
        height=560,
        key=f"preview_gantt_{right_slug}",
    )

# KPI comparison table with Δ
if _fill_gates:
    st.subheader("Full horizon (committed layer + fill)")
    st.caption(
        "Whole-board numbers — includes the committed layer both plans "
        "share, so deltas here mix the plant's own plan with the solver's "
        "fill decisions. The fill-window verdict above isolates the latter.")
rows = []
sections = [
    ("composite", None, "Composite", True),
    ("changeovers", "recipe_changes", "Recipe COs", False),
    ("changeovers", "format_changes", "Format COs", False),
    ("changeovers", "total_co_hours", "CO hours", False),
    ("cip", "cip_hours", "CIP hours", False),
    ("cip", "cip_forfeited_h", "CIP forfeited h", False),
    ("cip", "cip_forfeited_kg", "CIP forfeited kg", False),
    ("trials", "trial_hours", "Trial hours", False),
    ("trials", "trial_disruptions", "Trial disruptions", False),
    ("campaigns", "avg_run_h", "Avg run h", True),
    ("campaigns", "short_run_count", "Short runs", False),
    ("service", "orders_late", "Orders late", False),
    # Agent Q handoff 4: orders_at_risk is a WARNING count, no longer scored.
    ("service", "orders_at_risk", "Orders at risk (warning, not scored)", False),
]
# Agent Q handoff 2: a non-rankable composite (sanity / degraded) gets no
# better/worse verdict - the value is still shown for reading.
_rankable_both = (
    (left_res is None or getattr(left_res, "rankable", True))
    and (right_res is None or getattr(right_res, "rankable", True)))
for section, key, label, higher_better in sections:
    if key is None:
        lv, rv = left_sc.get("composite"), right_sc.get("composite")
        if not _rankable_both:
            label = "Composite (not rankable — see warning above)"
    else:
        lv = (left_sc.get(section) or {}).get(key)
        rv = (right_sc.get(section) or {}).get(key)
    delta = None
    verdict = ""
    if lv is not None and rv is not None:
        try:
            delta = float(rv) - float(lv)
            if abs(delta) < 1e-9:
                verdict = "same"
            else:
                improved = (delta > 0) if higher_better else (delta < 0)
                # Composite: higher score is better
                if key is None:
                    improved = delta > 0
                verdict = "better" if improved else "worse"
        except (TypeError, ValueError):
            delta = None
    if key is None and not _rankable_both:
        delta, verdict = None, "not rankable"
    rows.append({
        "Metric": label,
        left_label: lv,
        right_label: rv,
        "Δ": None if delta is None else round(delta, 2),
        "vs baseline": verdict,
    })

_kpi_df = pd.DataFrame(rows)
_styles = {}
if left_slug != OFFICIAL_KEY:
    _styles[left_label] = _vcolor(left_slug)[1]
_styles[right_label] = _vcolor(right_slug)[1]
_styled = _kpi_df.style.format(precision=2, na_rep="—")
for _col, _hex in _styles.items():
    if _col in _kpi_df.columns:
        _styled = _styled.set_properties(subset=[_col], color=_hex)
st.dataframe(_styled, use_container_width=True, hide_index=True)

if left_res is not None:
    render_delta_strip(left_res, right_res,
                       title=f"Δ {_cname(right_slug)} vs {_cname(left_slug)}")

# ── Weekly breakdown: argue the version PER WEEK ─────────────────────────
# "This version is better for W35 because topload drops 9" — every headline
# metric split by TRUE ISO week for both sides, plus the per-week delta.
st.subheader("Weekly breakdown")
try:
    _wk_left = (_weekly_cached(str(dd), _left_key, _LIVE_SIG, left_cal)
                if left_cal is not None else None)
    _wk_right = _weekly_cached(str(dd), _right_key, _LIVE_SIG, right_cal)
    _wk_cols = ["week", "fulfilled_pct", "scheduled_kg", "demand_kg",
                "orders_met", "orders", "topload", "ffs", "casepacker",
                "ttp", "weighted_co", "co_hours", "cip_hours", "avg_run_h",
                "short_runs"]

    if _wk_left is not None and len(_wk_left) and len(_wk_right):
        _dl = _wk_left.set_index("week")
        _dr = _wk_right.set_index("week")
        _weeks = [w for w in _dr.index if w in _dl.index]
        _rows = []
        for _w in _weeks:
            _row = {"week": _w}
            for _m, _lbl2, _better_low in (
                    ("fulfilled_pct", "fulfilled %", False),
                    ("orders_met", "orders met", False),
                    ("topload", "topload", True),
                    ("ffs", "FFS", True),
                    ("casepacker", "casepacker", True),
                    ("ttp", "TTP", True),
                    ("weighted_co", "weighted CO", True),
                    ("co_hours", "CO hours", True),
                    ("short_runs", "short runs", True)):
                _a = _dl.at[_w, _m] if _m in _dl.columns else None
                _b = _dr.at[_w, _m] if _m in _dr.columns else None
                if _a is None or _b is None or pd.isna(_a) or pd.isna(_b):
                    _row[_lbl2] = "—"
                    continue
                _d = round(float(_b) - float(_a), 1)
                _mark = ""
                if _d != 0:
                    _good = (_d < 0) if _better_low else (_d > 0)
                    _mark = " ✅" if _good else " ⚠️"
                _row[_lbl2] = f"{_a:g} → {_b:g} ({_d:+g}){_mark}"
            _rows.append(_row)
        st.caption(f"{_cname(left_slug)} → {_cname(right_slug)} per ISO week "
                   "(✅ = right side better on that metric)")
        st.dataframe(pd.DataFrame(_rows), use_container_width=True,
                     hide_index=True)

    with st.expander("Full weekly tables (both sides)"):
        # st.table: st.dataframe never mounts inside this initially-collapsed
        # expander (helpers/st_compat); one row per ISO week, so static is fine.
        if _wk_left is not None and len(_wk_left):
            st.markdown(f"**{_cname(left_slug)}**")
            st.table(_wk_left[_wk_cols].set_index("week"))
        if len(_wk_right):
            st.markdown(f"**{_cname(right_slug)}**")
            st.table(_wk_right[_wk_cols].set_index("week"))
except Exception as _wbe:  # noqa: BLE001
    st.caption(f"Weekly breakdown unavailable: {_wbe}")

# Delta narrative
deltas = delta_narrative(left_res, right_res) if left_res is not None else []
st.subheader("Show me why")
if deltas:
    for d in deltas:
        st.write(f"- {d}")
else:
    st.caption("No material differences.")

if baseline and left_slug != OFFICIAL_KEY:
    st.subheader("vs current schedule (official)")
    for _sl, label, res in ((left_slug, left_label, left_res),
                            (right_slug, right_label, right_res)):
        with st.expander(f"{_cname(_sl)} vs official"):
            for d in delta_narrative(baseline, res):
                st.write(f"- {d}")

# Per-version management
st.divider()
st.subheader("Manage versions")
for v in versions:
    slug = v["slug"]
    with st.expander(f"{_cname(slug)}  ·  composite at save={(v.get('scorecard') or {}).get('composite', '—')}  ·  {v.get('source', '')}"):
        st.caption(v.get("timestamp", ""))
        pros = st.text_area("Pros", value=v.get("pros", ""), key=f"pros_{slug}")
        cons = st.text_area("Cons", value=v.get("cons", ""), key=f"cons_{slug}")
        notes = st.text_area("Decision notes", value=v.get("notes", ""), key=f"notes_{slug}")
        if st.button("Save notes", key=f"save_notes_{slug}"):
            update_notes(slug, dd, pros=pros, cons=cons, notes=notes)
            st.success("Notes saved")

        sc = v.get("scorecard")
        if sc:
            render_scorecard(sc, show_formulas=False)

        new_name = st.text_input("Rename", value=display_name(slug, v.get("name")), key=f"rename_{slug}")
        # "Load into calendar" was removed 2026-08-19: it wrote the SAME
        # calendar_blocks.csv as Promote but WITHOUT the pre-promote backup —
        # a worse duplicate (user spotted the redundancy). Promote is the one
        # action that makes a version official; the side-by-side preview
        # above is the what-if view.
        _vline, _vphys = _version_validation(v)
        (st.error if _vphys else st.caption)(_vline)
        _promo_override = st.checkbox(
            "Override: promote despite the 2-week lock / physical validation "
            "errors", value=False, key=f"promo_override_{slug}",
            help="promote_version refuses a version that re-plans the locked "
                 "weeks (LockViolation); this page refuses one whose solver "
                 "run carries ERROR-severity physical validator violations. "
                 "Tick to do it anyway — the pre-promote backup is still taken.")
        a, c, d = st.columns(3)
        if a.button("Rename", key=f"do_rename_{slug}"):
            rename_version(slug, new_name, dd)
            st.rerun()
        if c.button("Promote to official", key=f"promo_{slug}"):
            if _vphys and not _promo_override:
                st.error(
                    f"Not promoted — this version's solver run carries "
                    f"{_vphys} ERROR-severity physical validation error(s) "
                    "(overlap / downtime / CIP interval / changeover gap / "
                    "gate). Tick the override to promote it anyway.")
            else:
                try:
                    res = promote_version(slug, dd, lock_override=_promo_override)
                except LockViolation as lv:
                    st.error("Not promoted — " + str(lv) + " Tick the override "
                             "above to promote over the 2-week lock.")
                except ValueError as e:
                    st.error(str(e))
                else:
                    if not res.get("promoted", True):
                        st.error("Not promoted — the version has no calendar file.")
                    else:
                        st.success(
                            f"Promoted — shift {res.get('shift_h', 0):+.0f} h, "
                            f"{res.get('matched', 0)} block(s) kept their board "
                            f"id/lock, {res.get('new', 0)} new; the previous board "
                            "was backed up first"
                            + (f" ({Path(res['backup']).name})" if res.get("backup") else "")
                            + ". Open the Plant Calendar to work with it.")
                        for _w in res.get("warnings") or []:
                            st.warning(_w)
                        # W handoff 3 / writeback-11: remember WHICH version is
                        # official so the write-back panel below talks about the
                        # promoted plan, not the newest work dir.
                        try:
                            import json as _pj
                            from datetime import datetime as _pdt
                            (dd / "promoted.json").write_text(_pj.dumps({
                                "slug": slug, "name": display_name(slug, v.get("name")),
                                "promoted_at": _pdt.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "planning_anchor": v.get("planning_anchor"),
                                "scenario_id": v.get("scenario_id"),
                                "source": v.get("source", ""),
                            }, indent=2), encoding="utf-8")
                        except OSError as _pe:
                            st.warning(f"Could not record the promoted version: {_pe}")
        if d.button("Delete", key=f"del_{slug}"):
            delete_version(slug, dd)
            st.rerun()
        xbytes = _excel_cached(str(dd), *_vkey(slug), _LIVE_SIG)
        st.download_button("Export Excel", data=xbytes, file_name=f"{slug}.xlsx", key=f"xl_{slug}")

_render_orphans()

if st.button("Delete all versions", type="secondary"):
    delete_all_versions(dd)
    st.rerun()

# ---------------------------------------------------------------------------
# Plant write-back (mo_changes) — the charter's "changes go back to VIF" step.
# The solver records every delta against committed manprg MOs (tonnage trims,
# splits, reorders) in mo_changes.csv per scenario run. Review here, export
# for VIF.
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Plant write-back — MO changes (VIF export)")
st.caption(
    "What a solver run changed against the plant's committed MOs (from "
    "manprg): tonnage trims, splits, reorders, DROPPED MOs. The record that "
    "matters is the PROMOTED plan's — that is what goes back to VIF."
)

_scen_root = dd / "_scenario_work"
_promoted: dict = {}
try:
    import json as _pj2
    _pp = dd / "promoted.json"
    if _pp.exists():
        _promoted = _pj2.loads(_pp.read_text(encoding="utf-8")) or {}
except (OSError, ValueError):
    _promoted = {}

if _promoted.get("slug"):
    st.markdown(
        f"**Promoted plan:** `{_promoted.get('name') or _promoted['slug']}` "
        f"(promoted {_promoted.get('promoted_at', '?')}, planning anchor "
        f"{_promoted.get('planning_anchor') or 'not stamped'})")
    try:
        _px = _excel_cached(str(dd), *_vkey(_promoted["slug"]), _LIVE_SIG)
        st.download_button(
            "Export the promoted plan (Excel) — this is what Scenario F writes back",
            data=_px, file_name=f"{_promoted['slug']}.xlsx", key="xl_promoted")
    except Exception as _pxe:  # noqa: BLE001
        st.caption(f"Excel export of the promoted plan unavailable: {_pxe}")
else:
    st.caption("No version has been promoted from this page yet — the list "
               "below shows every solver run's record, newest first.")

_mo_files = sorted(
    (p for p in _scen_root.glob("*/mo_changes.csv")),
    key=lambda p: p.stat().st_mtime, reverse=True,
) if _scen_root.exists() else []

# Tie the promoted version to its work dir when the metadata says which
# scenario produced it (scenario_id — stored by scenario_runner once the
# handoff lands); a Scenario F fill has no MO changes by construction.
_promoted_sid = str(_promoted.get("scenario_id") or "").strip()
_promoted_mo = None
if _promoted_sid:
    _cand = _scen_root / _promoted_sid / "mo_changes.csv"
    _promoted_mo = _cand if _cand.exists() else None
    if _promoted_sid == "F":
        st.info("The promoted plan is a Scenario F fill: the plant's committed "
                "MOs were not changed — nothing to type into VIF.")

if not _mo_files:
    st.info("No solver run has produced mo_changes.csv yet — run a scenario "
            "(Generate Scenarios), typically E (current state + demand).")
else:
    from datetime import datetime as _dt
    _labels = {
        str(p): (f"{p.parent.name} — "
                 f"{_dt.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}"
                 + (" — PROMOTED plan's run" if _promoted_mo and p == _promoted_mo else ""))
        for p in _mo_files
    }
    _options = [str(p) for p in _mo_files]
    _default_idx = _options.index(str(_promoted_mo)) if _promoted_mo else 0
    if _promoted.get("slug") and not _promoted_mo:
        st.caption("This record cannot be tied to the promoted version (its "
                   "metadata carries no scenario_id) — pick the run by hand.")
    _sel = st.selectbox(
        "Solver run", _options, index=_default_idx,
        format_func=lambda s: _labels.get(s, s), key="mo_changes_run")
    _mo = pd.read_csv(_sel, dtype={"mo": str, "sku": str})
    _anchor_col = str(_mo["planning_anchor"].dropna().iloc[0]) if (
        "planning_anchor" in _mo.columns and _mo["planning_anchor"].notna().any()) else ""
    st.caption(
        "Hours count from planning anchor "
        + (f"**{_anchor_col}**" if _anchor_col else
           "**(not stamped — older solver output; hours are anchor-less)**")
        + "; one row per produced piece of a split MO; a DROPPED MO has empty "
          "new_start_h / new_end_h.")
    if _mo.empty:
        st.caption("This run changed nothing against the committed MOs.")
    else:
        _changed = _mo[_mo["reason"] != "unmoved"]
        _dropped = _mo[_mo["reason"].astype(str).str.contains("dropped")]
        st.markdown(
            f"**{_changed['mo'].nunique()} of {_mo['mo'].nunique()} committed "
            f"MO(s) changed** — total tonnage delta "
            f"**{_mo.drop_duplicates('mo')['delta_kg'].sum():+,.0f} kg**")
        if len(_dropped):
            st.error(f"{_dropped['mo'].nunique()} committed MO(s) were DROPPED "
                     "by the solver (no block placed): "
                     + ", ".join(sorted(_dropped["mo"].astype(str).unique())[:12]))
        st.dataframe(_mo, use_container_width=True, hide_index=True)
        st.download_button(
            "Download mo_changes.csv (VIF write-back)",
            data=Path(_sel).read_bytes(),
            file_name=f"mo_changes_{Path(_sel).parent.name}.csv",
            mime="text/csv",
        )
