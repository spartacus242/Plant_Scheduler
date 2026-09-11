# code/pages/stock_check.py — Stock Check: component availability monitor.
#
# Two questions, one report (helpers/stock_report_cache — shared with Home
# and Reconcile, ~35s to compute, milliseconds to load):
#   Board       — will anything on the current board fail for components?
#   Demand plan — which demand SKUs can't reach >=90% of target?
# plus the report turned round by COMPONENT (helpers/stock_reports): which
# purchased item is short, against what, blocking which runs — the list the
# planner chases with purchasing — and the same tables as an Excel workbook.
# Data: VIF CSV exports (ediact 3/4, jestkexp/2, azapart, rmpkitems) +
#       weekly Shipping/Receiving xlsm (appointment feed).

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers import stock_reports as sr  # noqa: E402
from helpers.config import load_toml  # noqa: E402
from helpers.paths import data_dir  # noqa: E402
from helpers.theme import (TOKENS, chip, kind_for, note, page_header,  # noqa: E402
                           render_chips, section_label, state_chip)
from helpers.timefmt import hour_to_stamp, planning_anchor  # noqa: E402

# Block hour offsets are anchored to the REAL planning anchor — omitting it
# fell back to timefmt's 2026-02-15 default and showed February dates.
_ANCHOR = planning_anchor(load_toml())

page_header(
    "Stock Check",
    subtitle="Component availability against the current board and the demand plan. "
             "Flags runs that can't be covered and SKUs that shouldn't be scheduled.",
)

DATA = Path(data_dir())
SC_DIR = DATA / "stockcheck"
SETTINGS = SC_DIR / "settings.json"
DEFAULT_VIF = r"\\usnpa-appfs\DATA\vif-export\auto editions"
DEV_VIF = SC_DIR / "dev_vif"
DEV_RECV = SC_DIR / "dev_receiving_schedule.xlsm"
# Live link (P1, landed 2026-08-14): the GitHub bridge drops the VIF exports
# into data/reference/ daily. Default source order: saved setting → network
# share (work PC) → bridge-refreshed reference/ → bundled dev fixtures.
REF_VIF = DATA / "reference"
REF_RECV = REF_VIF / "Shipping Receiving Schedule NPA - 2024.xlsm"


def _default_vif_folder() -> str:
    if Path(DEFAULT_VIF).exists():
        return DEFAULT_VIF
    if (REF_VIF / "ediact 3.csv").exists():
        return str(REF_VIF)
    return str(DEV_VIF)


def _receiving_path() -> Path:
    return REF_RECV if REF_RECV.exists() else DEV_RECV


# ---------------------------------------------------------------- settings
def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(s: dict) -> None:
    SC_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")


settings = _load_settings()

# ------------------------------------------------------- engine import
ENGINE_OK = True
ENGINE_ERR = ""
try:
    from stockcheck import coverage as cov
    from stockcheck import weeks as wk
    from stockcheck.api import stock_check_report  # noqa: F401 — engine presence check
    from stockcheck.receiving_import import parse_receiving_schedule
    from stockcheck.vif_import import import_vif_folder  # noqa: F401
except Exception as exc:  # engine not merged yet -> render shell w/ demo
    ENGINE_OK = False
    ENGINE_ERR = str(exc)

# Soft cell backgrounds for status columns in the tables (Styler).
_STATUS_BG = {
    "OK": TOKENS["ok_soft"], "TIGHT": TOKENS["warn_soft"],
    "AT RISK": TOKENS["bad_soft"], "DO NOT SCHEDULE": TOKENS["bad_soft"],
    "NO BOM": TOKENS["neutral_soft"], "UNK": TOKENS["neutral_soft"],
    "NOT TRACKED": TOKENS["neutral_soft"],
}
_STATUS_FG = {
    "OK": TOKENS["ok"], "TIGHT": TOKENS["warn"],
    "AT RISK": TOKENS["bad"], "DO NOT SCHEDULE": TOKENS["bad"],
    "NO BOM": TOKENS["neutral"], "UNK": TOKENS["neutral"],
    "NOT TRACKED": TOKENS["neutral"],
}


def _style_status(df: pd.DataFrame, col: str = "Status"):
    """Color the status column; falls back to the plain frame when the
    pandas Styler is unavailable for this frame."""
    if df.empty or col not in df.columns:
        return df
    try:
        return df.style.map(
            lambda v: (f"background-color: {_STATUS_BG.get(str(v), '')}; "
                       f"color: {_STATUS_FG.get(str(v), TOKENS['ink'])}; font-weight: 600"),
            subset=[col])
    except Exception:  # noqa: BLE001 — styling is cosmetic
        return df


def _public(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")}
                         for r in rows])


# ---------------------------------------------------------------- controls
_dem_anchor, _week_opts = (None, [])
if ENGINE_OK:
    # Weeks of the CURRENT demand plan (anchor from demand_plan.source.json),
    # floored at today's ISO week. Hardcoding [0, 1, 2] and labeling without
    # an anchor rendered timefmt's Feb default as WW07/WW08/WW09.
    _dem_anchor = wk.demand_anchor(DATA)
    _week_opts = wk.current_week_options(wk.demand_week_indices(DATA), _dem_anchor)


def _wk_label(x: int) -> str:
    return wk.week_index_label(x, _dem_anchor) if ENGINE_OK else f"W{x}"


c_week, c_refresh, c_spacer = st.columns([1.6, 1.4, 4])
with c_week:
    week_index = st.selectbox(
        "Demand week", options=[None] + _week_opts,
        format_func=lambda x: ("All weeks" if x is None else _wk_label(x)),
        label_visibility="collapsed")
with c_refresh:
    refresh = st.button("⟳ Refresh from VIF", type="primary", use_container_width=True,
                        help="Recompute the stock report (source files are "
                             "re-imported only if they changed). The page "
                             "otherwise renders the saved report instantly.")

with st.expander("Sources & availability — VIF folder, which stock counts", expanded=False):
    vif_folder = st.text_input(
        "VIF export folder",
        value=settings.get("vif_folder", _default_vif_folder()),
        help="Network share on Carsten's machine; data/reference/ when the "
             "GitHub bridge delivers the exports; dev fixtures as last resort.")
    # A folder edit must reach the shared resolver (stock_report_inputs) —
    # otherwise Home and Reconcile keep reading the old folder and the pages
    # disagree (walkthrough 2026-08-17).
    if vif_folder != settings.get("vif_folder", _default_vif_folder()):
        settings["vif_folder"] = vif_folder
        _save_settings(settings)
    if ENGINE_OK:
        st.caption("Which stock counts as available — default: only **Ava**. "
                   "`Loc` = QC/warehouse-hold — opt in per depot if you know it will "
                   "release. Changing a toggle saves it and marks the report stale — "
                   "press **Refresh from VIF** to apply.")
        toggles = settings.get("toggles", cov.default_toggles())
        depots = ["M01", "SB1", "SC1", "SF1", "M02", "SFG", "M21"]
        cols = st.columns(len(depots))
        new_toggles = dict(toggles)
        for ci, dep in enumerate(depots):
            with cols[ci]:
                st.markdown(f"**{dep}**")
                for stt in ("Ava", "Loc", "Out"):
                    key = f"{dep}|{stt}"
                    new_toggles[key] = st.checkbox(
                        stt, value=toggles.get(key, stt == "Ava"), key=f"t_{key}")
        if new_toggles != toggles:
            settings["toggles"] = new_toggles
            settings["vif_folder"] = vif_folder
            _save_settings(settings)
            st.rerun()
        toggles = new_toggles

if not ENGINE_OK:
    st.warning(f"Stock-check engine unavailable: {ENGINE_ERR}")
    st.stop()

# ---------------------------------------------------------------- report
# One persisted report shared with Home and Reconcile (~35s to compute,
# milliseconds to load): this page NEVER recomputes on its own — the saved
# report renders instantly and only the Refresh button (or a first-ever
# visit with nothing saved yet) crunches the BOMs. The demand-week filter
# is applied in-page, so one saved report serves every week choice.
from helpers.stock_report_cache import load_cached, refresh_report  # noqa: E402

cached = load_cached(DATA)
if refresh or cached is None:
    with st.spinner("Crunching BOMs…"):
        cached = refresh_report(DATA)
rep = cached.report

if "error" in rep:
    st.error(f"Import failed: {rep['error']}")
    st.json(rep.get("import_errors", []))
    st.stop()

_when = cached.computed_at.replace("T", " ")
_cost = f" in {cached.elapsed_s:.0f}s" if cached.elapsed_s else ""
src = rep.get("source_files", {}) or {}
_newest = max(src.values()) if src else ""

# receiving appointments (xlsm parse — cached on the file's mtime)
@st.cache_data(show_spinner=False)
def _receiving(path_str: str, mtime: float):
    return parse_receiving_schedule(Path(path_str))


_recv = _receiving_path()
appts, appt_errors = (_receiving(str(_recv), _recv.stat().st_mtime)
                      if _recv.exists() else ([], []))
po_appts = {a["po"]: a for a in appts if a["po"]}

# ---------------------------------------------------------------- status line
if cached.stale:
    st.warning(f"Board / demand / VIF inputs changed since this report "
               f"(computed {_when}{_cost}) — press **Refresh from VIF** to "
               "recompute. Showing the saved report.")
else:
    st.caption(f"Report computed {_when}{_cost} — saved; loads instantly "
               "until the inputs change.")
_status_chips = [
    chip("report stale" if cached.stale else "report current",
         "warn" if cached.stale else "ok", icon="▲" if cached.stale else "●"),
    chip(f"{len(src)} VIF files" + (f" · newest {_newest}" if _newest else ""), "neutral"),
    chip(f"{len(appts)} receiving appointments", "neutral" if appts else "warn"),
]
if rep.get("import_errors"):
    _status_chips.append(chip(f"{len(rep['import_errors'])} import error(s)", "bad"))
render_chips(_status_chips)
if rep.get("import_errors"):
    st.warning("Some files failed to import: " + "; ".join(rep["import_errors"]))

# ---------------------------------------------------------------- tables
_stamp = lambda h: hour_to_stamp(h, _ANCHOR)  # noqa: E731
sv = rep.get("schedule_view", []) or []
dv_all = rep.get("demand_view", []) or []
dv = [d for d in dv_all if week_index is None or d.get("week_index") == week_index]
board_all = sr.block_rows(rep, _stamp)
board_risk = [r for r in board_all if r["_status"] in sr.RISK_STATUSES]
demand_tbl = sr.demand_rows(rep, week_index, _wk_label)
shortages = sr.component_shortages(rep, week_index, stamp=_stamp, week_label=_wk_label)

n_blocks_risk = sum(1 for b in sv if b.get("status") in ("AT_RISK", "TIGHT"))
n_dns = sum(1 for d in dv if d.get("status") == "DO_NOT_SCHEDULE")
n_dem_risk = sum(1 for d in dv if d.get("status") in ("AT_RISK",))

# Excel workbook — the same tables the page shows. Cached per report +
# week filter so a rerun never rebuilds a workbook it already has.
_xl_key = (cached.computed_at, week_index, len(appts))
if st.session_state.get("sc_xl_key") != _xl_key:
    st.session_state["sc_xl_bytes"] = sr.excel_report(
        summary=[
            ("Report computed", _when),
            ("Demand week filter", "All weeks" if week_index is None else _wk_label(week_index)),
            ("Blocks at risk / tight", f"{n_blocks_risk} of {len(sv)}"),
            ("Demand SKUs do not schedule", f"{n_dns} of {len(dv)}"),
            ("Demand SKUs at risk", n_dem_risk),
            ("Components short", len(shortages)),
            ("Receiving appointments", len(appts)),
            ("VIF sources", "; ".join(f"{k} {v}" for k, v in sorted(src.items()))),
        ],
        board=board_all, demand=demand_tbl, shortages=shortages,
        appointments=appts, no_bom=list(rep.get("no_bom_skus", []) or []),
        unk=list(rep.get("unk", []) or []))
    st.session_state["sc_xl_key"] = _xl_key

m1, m2, m3, m4, m5 = st.columns([1, 1, 1, 1, 1.3])
m1.metric("Blocks at risk", f"{n_blocks_risk} / {len(sv)}",
          help="Production blocks on the board whose components are AT RISK or TIGHT, of all blocks.")
m2.metric("Do not schedule", f"{n_dns} / {len(dv)}",
          help="Demand SKUs (selected week) that cannot reach 90% of target with the stock on hand.")
m3.metric("Demand SKUs at risk", n_dem_risk)
m4.metric("Components short", len(shortages),
          help="Purchased items short somewhere — see Shortages by component.")
with m5:
    st.download_button(
        "⬇ Export stock report (Excel)",
        data=st.session_state["sc_xl_bytes"],
        file_name=f"stock_check_{cached.computed_at.replace(':', '').replace('-', '')[:15]}.xlsx",
        use_container_width=True,
        help="Summary · Board · Demand plan · Component shortages · Receiving · Data quality",
    )

# ---------------------------------------------------------------- tabs
tab_board, tab_demand, tab_short, tab_drill, tab_item, tab_recv, tab_dq = st.tabs(
    ["Board", "Demand plan", "Shortages by component", "SKU drill-down",
     "Component → SKUs", "Receiving", "Data quality"])


def _item_line(i: dict) -> str:
    return (f"{state_chip(i['status'])} <b>{i['item']}</b> "
            f"{str(i.get('designation', ''))[:28]} — need {sr.fmt_qty(i.get('need'))} "
            f"{i.get('unit', '')}, available {sr.fmt_qty(i.get('available_total'))}, "
            f"coverage {sr.fmt_pct(i.get('ratio'))}")


with tab_board:
    section_label("Will the current board run?")
    ok = len(sv) - len(board_risk)
    st.caption(f"{ok} of {len(sv)} production blocks fully covered. "
               "Worst first; the Constraints column names the items that fall short.")
    show_all = st.toggle("Show every block (not only the ones at risk)", key="sc_board_all")
    _rows = board_all if show_all else board_risk
    if _rows:
        st.dataframe(_style_status(_public(_rows)), use_container_width=True,
                     hide_index=True)
    else:
        st.success("Every production block on the board is fully covered.")
    if board_risk:
        with st.expander("Component detail per block at risk", expanded=False):
            for b in sorted((b for b in sv if b.get("status") in sr.RISK_STATUSES),
                            key=lambda b: (b["start_h"])):
                st.markdown(
                    f"{state_chip(b['status'])} <b>{b['sku']}</b> {b['line_name']} · "
                    f"{_stamp(b['start_h'])} → {_stamp(b['end_h'])} · "
                    f"{b['cases']:.0f} cases", unsafe_allow_html=True)
                bad = [i for i in b["items"]
                       if i["status"] in ("AT_RISK", "TIGHT", "NOT_TRACKED")]
                bad.sort(key=lambda i: (i["ratio"] is None,
                                        i["ratio"] if i["ratio"] is not None else 0))
                for i in bad[:8]:
                    st.markdown("&nbsp;&nbsp;&nbsp;" + _item_line(i), unsafe_allow_html=True)

with tab_demand:
    section_label("Should it be scheduled?")
    st.caption("Achievable coverage of each demand SKU's target with the stock on hand. "
               "DO NOT SCHEDULE = under 90% achievable; worst first.")
    if demand_tbl:
        st.dataframe(_style_status(_public(demand_tbl)), use_container_width=True,
                     hide_index=True)
    else:
        st.caption("No demand orders in the selected week.")

with tab_short:
    section_label("Shortages by component — the purchasing list")
    note("One row per purchased item that is short somewhere: what is on hand "
         "against what the board and the demand plan need, how many runs it puts "
         "at risk and which one starts first. Alternates are pooled into the "
         "availability already. Export the Excel above to send it on.")
    if shortages:
        st.dataframe(_style_status(_public(shortages)), use_container_width=True,
                     hide_index=True)
    else:
        st.success("No component is short for the board or the selected demand week.")
    if appts:
        _nxt = sorted((a for a in appts if a.get("date")), key=lambda a: (str(a.get("date")), str(a.get("time"))))[:8]
        if _nxt:
            st.caption("Next inbound appointments (the receiving feed carries PO and "
                       "category only, no item numbers): "
                       + " · ".join(f"{a.get('date')} {a.get('time')} {a.get('category') or ''}"
                                    f"{(' PO ' + a['po']) if a.get('po') else ''}".strip()
                                    for a in _nxt))

with tab_drill:
    section_label("SKU → component tree")
    sku_opts = sorted({b["sku"] for b in sv} | {d["sku"] for d in dv})
    sku = st.selectbox("SKU", sku_opts)
    blocks_for = [b for b in sv if b["sku"] == sku]
    dem_for = [d for d in dv if d["sku"] == sku]
    if dem_for:
        d = dem_for[0]
        st.markdown(f"**Demand:** {d['target_kg']} kg · status "
                    f"{state_chip(d['status'])}", unsafe_allow_html=True)
    if blocks_for:
        st.markdown(f"**On schedule:** {len(blocks_for)} block(s), "
                    f"{sum(b['cases'] for b in blocks_for):.0f} cases total")
        st.markdown("**Component coverage (schedule need):**")
        seen = {}
        for b in blocks_for:
            for i in b["items"]:
                k = i["item"]
                if k not in seen:
                    seen[k] = dict(i)
                else:
                    seen[k]["need"] = round((seen[k]["need"] or 0)
                                            + (i["need"] or 0), 3)
        for i in sorted(seen.values(),
                        key=lambda x: (x["ratio"] is None,
                                       x["ratio"] if x["ratio"] is not None else 0)):
            with st.expander(f"{sr.status_text(i['status'])} · {i['item']} "
                             f"{str(i.get('designation', ''))[:40]}"):
                st.write(f"Need **{i['need']} {i['unit']}** · available "
                         f"**{i['available_total']}** (primary {i['available_primary']})")
                if i.get("alternates"):
                    st.write("Alternates:")
                    # st.table: st.dataframe never mounts inside an initially-
                    # collapsed expander (helpers/st_compat).
                    st.table(pd.DataFrame(i["alternates"]))

with tab_item:
    section_label("Component → consuming SKUs")
    items = sorted(rep.get("item_reverse", {}).keys())
    item = st.selectbox("Component item", items)
    # an empty report (nothing scheduled/demanded) leaves the selectbox on
    # None — there is no row to look up
    cons = pd.DataFrame(rep["item_reverse"][item] if item else [])
    if "week_index" in cons.columns:
        # Sort on week_index (monotonic in real time), THEN label — sorting
        # on the ISO week number would put next January's W01 before W52.
        cons = cons.sort_values("week_index", kind="stable")
        cons.insert(int(cons.columns.get_loc("week_index")), "Wk",
                    [_wk_label(w) for w in cons["week_index"]])
        cons = cons.drop(columns=["week_index"])
    st.dataframe(cons, use_container_width=True, hide_index=True)

with tab_recv:
    section_label("Inbound appointments (Shipping/Receiving schedule)")
    if appts:
        st.dataframe(pd.DataFrame(appts), use_container_width=True,
                     hide_index=True)
    else:
        st.info("No receiving file found at "
                f"`{_receiving_path()}`. Drop the weekly xlsm there or push it via the bridge.")
    if appt_errors:
        st.caption(f"{len(appt_errors)} rows skipped (decayed cells).")

with tab_dq:
    section_label("Data quality")
    st.markdown(f"**NO_BOM SKUs ({len(rep['no_bom_skus'])})** in the current "
                "schedule/demand universe — no recipe in ediact 3, cannot be "
                "checked:")
    st.caption("Fix: these SKUs are on the board or in the demand plan but "
               "have no recipe in the BOM export (ediact 3.csv). Add the "
               "recipe to the VIF export, or take the SKU off the schedule / "
               "out of the demand plan.")
    st.code(", ".join(rep["no_bom_skus"]) or "none in the current plan")
    st.markdown("**All SKUs without a BOM** (full catalog from `sku_info.csv`):")
    st.caption("Fix: these SKUs are listed in sku_info.csv but have no recipe "
               "in the BOM exports (ediact 3.csv / ediact 4.csv). Either "
               "remove them from sku_info.csv or add their recipes to the "
               "BOM export.")

    @st.cache_data(show_spinner=False)
    def _no_bom_all(vif_folder: str, sources_key: str) -> list[str]:
        # keyed on the report's source mtimes: the full catalog scan reuses
        # the mtime-guarded snapshot instead of re-importing the VIF folder
        # on every rerun (that import alone cost seconds per visit)
        si = pd.read_csv(DATA / "reference" / "sku_info.csv",
                         dtype={"sku": str})
        from stockcheck.api import refresh_vif_snapshot
        from stockcheck.bom import BomGraph
        snap = refresh_vif_snapshot(vif_folder, DATA)
        bom_all = BomGraph(snap.frames["ediact 3.csv"])
        return sorted(s for s in si["sku"] if not bom_all.has_bom(s))

    try:
        no_bom_all = _no_bom_all(vif_folder, json.dumps(src, sort_keys=True))
    except Exception:
        no_bom_all = []
    st.code(", ".join(no_bom_all) or "none")
    st.markdown(f"**UNK items ({len(rep['unk'])})** — requirement exists but "
                "magnitude unknown:")
    st.caption("Fix: each row names a broken recipe line in ediact 3.csv / "
               "ediact 4.csv — either the activity's Output row is missing "
               "its quantity (the need can't be scaled), or a blank-qty "
               "alternate row has no quantity-bearing primary of the same "
               "unit in that activity. Correct that activity in the VIF "
               "BOM export.")
    if rep["unk"]:
        st.dataframe(pd.DataFrame(rep["unk"]).head(100),
                     use_container_width=True, hide_index=True)
    else:
        st.caption("none")
    st.markdown("**Source files** (name · export timestamp):")
    st.code("\n".join(f"{n}  {t}" for n, t in sorted(src.items())) or "none")
