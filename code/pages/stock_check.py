# code/pages/stock_check.py — Stock Check: component availability monitor.
#
# Two views:
#   Schedule view — will anything on the current board fail for components?
#                   (flat on-hand status + the time-phased Supply verdict)
#   Demand view   — which demand SKUs can't reach >=90% of target?
# Plus the Inbound tab: every open-PO line and the fate the timeline engine
# gave it (contract .hermes/plans/2026-09-01-po-stock-contracts.md §7).
# Data: VIF CSV exports (ediact 3/4, jestkexp/2, azapart, rmpkitems) +
#       weekly Shipping/Receiving xlsm (appointment feed) + open-PO report.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.config import load_toml  # noqa: E402
from helpers.paths import data_dir  # noqa: E402
from helpers.timefmt import hour_to_stamp, planning_anchor  # noqa: E402

# Block hour offsets are anchored to the REAL planning anchor — omitting it
# fell back to timefmt's 2026-02-15 default and showed February dates.
_ANCHOR = planning_anchor(load_toml())

st.header("Stock Check")
st.caption(
    "Daily component availability against the current schedule and the demand "
    "plan. Flags blocks that can't run and SKUs that shouldn't be scheduled."
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
    from stockcheck.api import stock_check_report
    from stockcheck.receiving_import import parse_receiving_schedule
    from stockcheck.vif_import import import_vif_folder
except Exception as exc:  # engine not merged yet -> render shell w/ demo
    ENGINE_OK = False
    ENGINE_ERR = str(exc)
# The time-phased engine only re-stamps the supply sentence with real dates;
# without it the report's own text (h-frame stamps) still renders.
try:
    from stockcheck import timeline as tl
except Exception:  # noqa: BLE001
    tl = None

STATUS_CHIP = {
    "OK": ":green[OK]",
    "TIGHT": ":orange[TIGHT]",
    "AT_RISK": ":red[AT RISK]",
    "DO_NOT_SCHEDULE": ":red[DO NOT SCHEDULE]",
    "NO_BOM": ":gray[NO BOM]",
    "UNK": ":gray[UNK]",
    "NOT_TRACKED": ":gray[NOT TRACKED]",
}
# plain labels for dataframe cells (st.dataframe does not render markdown)
STATUS_TEXT = {k: v.split("[")[1].rstrip("]") for k, v in STATUS_CHIP.items()}

# Time-phased supply verdicts (contract §3.5) ride on each schedule_view row
# as "supply". A saved report from before the PO feed has none: every helper
# below returns its quiet default so the page renders exactly as it used to.
SUPPLY_CHIP = {
    "SHORT": ":red[SHORT]",
    "DEPENDENT": ":orange[DEPENDENT]",
    "NO_DATA": ":gray[NO DATA]",
    "OK": ":green[OK]",
}


def _supply(b: dict) -> dict | None:
    s = b.get("supply")
    return s if isinstance(s, dict) and s.get("verdict") else None


def _supply_flagged(b: dict) -> bool:
    # SHORT, or DEPENDENT above the minor floor — the verdicts that pull a
    # block into the risk list even when the flat on-hand check says OK
    s = _supply(b)
    if s is None:
        return False
    v = s.get("verdict")
    return v == "SHORT" or (v == "DEPENDENT" and not s.get("minor"))


def _supply_chip(s: dict) -> str:
    v = s.get("verdict")
    if v == "OK" and s.get("backed"):
        return ":gray[OK · backed by PO]"
    if v == "DEPENDENT" and s.get("minor"):
        return ":gray[DEPENDENT · minor]"
    return SUPPLY_CHIP.get(v, f":gray[{v}]")


def _supply_text(s: dict) -> str:
    # verdict_text re-stamps the hours with the real anchor; the report's own
    # text carries 'h95.3' stamps and is only the fallback
    if tl is not None:
        try:
            return tl.verdict_text(s, _ANCHOR)
        except Exception:  # noqa: BLE001 — one odd row must not kill the tab
            pass
    return str(s.get("text") or "")


# Inbound table order: counted lines first, then the fates a planner can act
# on, then housekeeping; unknown fates sort after, alphabetically.
_FATE_ORDER = ["used", "landed_unverifiable", "overdue", "offsite_no_transfer",
               "unjoinable", "unit_mismatch", "bad_qty", "bad_date", "landed",
               "received"]
# Per state: WHY. The consequence ("nothing is counted, gaps read NO DATA")
# is said once, in the state line next to the 'Would count' metric.
_FEED_STATE_HELP = {
    "ok": "feed current — receipts dated on/after the stock snapshot are "
          "counted.",
    "missing": "no open-PO report was found. Drop the ERP open-PO export at "
               "data/reference/open_pos.xlsx (the live bridge delivers it) "
               "or point [datasources] po_report_path at it on Settings.",
    "stale": "the latest receipt date in the file is already in the past, so "
             "the extract no longer looks ahead; a fresh export is needed.",
    "empty": "the report parsed but holds no PO lines.",
}
# (report key, title, one-line fix hint) for the Data quality tab
_QUALITY_HINTS = [
    ("unjoinable_items", "Unjoinable PO items",
     "Fix: the PO's item code matches no BOM item — neither exactly nor as "
     "a single '-X' suffixed variant — so its receipts are not counted. Check "
     "the code on the PO line against the BOM/item exports (ediact 3.csv, "
     "rmpkitems.csv); if the plant uses a suffixed code, fix the item master, "
     "not the PO."),
    ("unit_mismatch", "Unit mismatch (PO vs BOM)",
     "Fix: the PO orders the item in a different unit than the recipe "
     "consumes (e.g. KG vs EA) and no conversion is applied, so the receipt "
     "is dropped. Correct the unit on the PO line or the item's BOM unit."),
    ("landed_unverifiable", "Landed check unavailable",
     "Info: these receipts ARE counted. No stock lot of the item carries a "
     "decodable batch (N + year + day-of-year + seq), so the engine cannot "
     "tell whether the truck already landed in the on-hand pile — verify "
     "against the stock report; if it did land, expect a double count until "
     "the PO closes."),
]

# ---------------------------------------------------------------- header
left, mid, right = st.columns([3, 2, 2])
with left:
    vif_folder = st.text_input(
        "VIF export folder",
        value=settings.get("vif_folder", _default_vif_folder()),
        help="Network share on Carsten's machine; data/reference/ when the "
             "GitHub bridge delivers the exports; dev fixtures as last resort.")
with mid:
    # Weeks of the CURRENT demand plan (anchor from demand_plan.source.json),
    # floored at today's ISO week. Hardcoding [0, 1, 2] and labeling without
    # an anchor rendered timefmt's Feb default as WW07/WW08/WW09.
    if ENGINE_OK:
        _dem_anchor = wk.demand_anchor(DATA)
        _week_opts = wk.current_week_options(
            wk.demand_week_indices(DATA), _dem_anchor)
    else:
        _dem_anchor, _week_opts = None, []
    week_index = st.selectbox(
        "Demand week", options=[None] + _week_opts,
        format_func=lambda x: ("All weeks" if x is None
                               else wk.week_index_label(x, _dem_anchor)))
with right:
    refresh = st.button("Refresh from VIF", type="primary",
                        help="Recompute the stock report (source files are "
                             "re-imported only if they changed). The page "
                             "otherwise renders the saved report instantly.")

# A folder edit must reach the shared resolver (stock_report_inputs) —
# otherwise Home and Reconcile keep reading the old folder and the pages
# disagree (walkthrough 2026-08-17).
if vif_folder != settings.get("vif_folder", _default_vif_folder()):
    settings["vif_folder"] = vif_folder
    _save_settings(settings)

if not ENGINE_OK:
    st.warning(f"Stock-check engine unavailable: {ENGINE_ERR}")
    st.stop()

# ------------------------------------------------------- availability toggles
with st.expander("Availability — which stock counts?", expanded=False):
    st.caption("Default: only **Ava** (available). `Loc` = QC/warehouse-hold — "
               "opt in per depot if you know it will release. Changing a "
               "toggle saves it and marks the report stale — press "
               "**Refresh from VIF** to apply.")
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

# ---------------------------------------------------------------- report
# One persisted report shared with Home and Reconcile (~35s to compute,
# milliseconds to load): this page NEVER recomputes on its own — the saved
# report renders instantly and only the Refresh button (or a first-ever
# visit with nothing saved yet) crunches the BOMs. The demand-week filter
# is applied in-page, so one saved report serves every week choice.
from helpers.stock_report_cache import load_cached, refresh_report

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
if cached.stale:
    st.warning(f"Board / demand / VIF inputs changed since this report "
               f"(computed {_when}{_cost}) — press **Refresh from VIF** to "
               "recompute. Showing the saved report.")
else:
    st.caption(f"Report computed {_when}{_cost} — saved; loads instantly "
               "until the inputs change.")

# source freshness strip
src = rep.get("source_files", {})
with st.container():
    chips = " · ".join(f"`{n}` {t}" for n, t in sorted(src.items()))
    st.caption(f"Sources: {chips}")
if rep.get("import_errors"):
    st.warning("Some files failed to import: " +
               "; ".join(rep["import_errors"]))

# receiving appointments (xlsm parse — cached on the file's mtime)
@st.cache_data(show_spinner=False)
def _receiving(path_str: str, mtime: float):
    return parse_receiving_schedule(Path(path_str))


_recv = _receiving_path()
appts, appt_errors = (_receiving(str(_recv), _recv.stat().st_mtime)
                      if _recv.exists() else ([], []))
po_appts = {a["po"]: a for a in appts if a["po"]}

# ---------------------------------------------------------------- metrics
sv = rep["schedule_view"]
dv = rep["demand_view"]
if week_index is not None:
    dv = [d for d in dv if d.get("week_index") == week_index]
# a block counts once whether the flat check, the supply verdict or both
# flag it
has_supply = any(_supply(b) is not None for b in sv)
n_flat_risk = sum(1 for b in sv if b["status"] in ("AT_RISK", "TIGHT"))
n_supply_risk = sum(1 for b in sv if _supply_flagged(b))
n_blocks_risk = sum(1 for b in sv
                    if b["status"] in ("AT_RISK", "TIGHT") or _supply_flagged(b))
n_dns = sum(1 for d in dv if d["status"] == "DO_NOT_SCHEDULE")
n_dem_risk = sum(1 for d in dv if d["status"] in ("AT_RISK",))
m1, m2, m3, m4 = st.columns(4)
m1.metric("Schedule blocks at risk", n_blocks_risk, delta=f"of {len(sv)}")
m2.metric("Demand SKUs DO_NOT_SCHEDULE", n_dns, delta=f"of {len(dv)}")
m3.metric("Demand SKUs at risk", n_dem_risk)
m4.metric("Receiving appointments", len(appts))
if has_supply:
    st.caption(f"Blocks at risk = flat status AT RISK/TIGHT ({n_flat_risk}) "
               f"or time-phased supply SHORT/DEPENDENT ({n_supply_risk}, "
               "minor shares excluded); a block flagged by both counts once.")
else:
    st.caption("Blocks at risk = flat status AT RISK/TIGHT. This saved report "
               "predates the PO feed, so it carries no time-phased supply "
               "verdicts — press **Refresh from VIF** to add them.")
_feed_state = ((rep.get("inbound") or {}).get("state")
               or (rep.get("supply_meta") or {}).get("feed_state") or "")

# ---------------------------------------------------------------- tabs
(tab_sched, tab_demand, tab_drill, tab_item, tab_recv, tab_inb,
 tab_dq) = st.tabs(
    ["Schedule view", "Demand view", "SKU drill-down", "Item view",
     "Receiving", "Inbound", "Data quality"])


def _item_line(i: dict) -> str:
    po = ""
    for a in i.get("alternates", []):
        pass
    return (f"{STATUS_CHIP.get(i['status'], i['status'])} **{i['item']}** "
            f"{i['designation'][:28]} — need {i['need']} {i['unit']}, "
            f"available {i['available_total']}, ratio {i['ratio']}")


with tab_sched:
    st.subheader("Will the current board run?")
    # flat AT_RISK/TIGHT/NOT_TRACKED, plus blocks the time-phased verdict
    # flags (SHORT, non-minor DEPENDENT) even when on-hand looks fine today
    risky = [b for b in sv
             if b["status"] in ("AT_RISK", "TIGHT", "NOT_TRACKED")
             or _supply_flagged(b)]
    ok = len(sv) - len(risky)
    cap = f"{ok} of {len(sv)} production blocks fully covered."
    if has_supply:
        _verdicts = [(_supply(b) or {}).get("verdict") for b in sv]
        n_short = sum(1 for v in _verdicts if v == "SHORT")
        n_dep = sum(1 for b in sv if _supply_flagged(b)
                    and (_supply(b) or {}).get("verdict") == "DEPENDENT")
        n_nodata = sum(1 for v in _verdicts if v == "NO_DATA")
        cap += (f" Supply: {n_short} short · {n_dep} dependent · "
                f"{n_nodata} no data"
                + (f" (PO feed {_feed_state})." if _feed_state else "."))
    st.caption(cap)
    for b in sorted(risky, key=lambda b: (b["start_h"])):
        s = _supply(b)
        label = STATUS_CHIP.get(b['status'], b['status'])
        if s is not None:
            label += f" · supply {_supply_chip(s)}"
        with st.expander(
                f"{label} **{b['sku']}** "
                f"{b['line_name']} · {hour_to_stamp(b['start_h'], _ANCHOR)} → "
                f"{hour_to_stamp(b['end_h'], _ANCHOR)} · {b['cases']:.0f} cases"):
            if s is not None:
                st.markdown(f"**Supply** {_supply_chip(s)} — {_supply_text(s)}")
            bad = [i for i in b["items"]
                   if i["status"] in ("AT_RISK", "TIGHT", "NOT_TRACKED")]
            bad.sort(key=lambda i: (i["ratio"] is None,
                                    i["ratio"] if i["ratio"] is not None else 0))
            if not bad and s is not None:
                st.caption("Flat on-hand check OK — listed for the supply "
                           "verdict only.")
            for i in bad[:8]:
                st.markdown(_item_line(i))

with tab_demand:
    st.subheader("Should it be scheduled?")
    order = {"DO_NOT_SCHEDULE": 0, "AT_RISK": 1, "TIGHT": 2,
             "NOT_TRACKED": 3, "NO_BOM": 4, "UNK": 5, "OK": 6}
    rows = sorted(dv, key=lambda d: (order.get(d["status"], 9),
                                     d["achievable_ratio"] or 999))
    tbl = []
    for d in rows:
        c = d["constraining"][0] if d["constraining"] else {}
        ratio = d["achievable_ratio"]
        cov_s = ("—" if ratio is None
                 else (f"{ratio:.1%}" if ratio >= 0.005 else f"{ratio:.2%}"))
        tbl.append({
            "SKU": d["sku"],
            "Wk": wk.week_index_label(d["week_index"], _dem_anchor),
            "Target kg": d["target_kg"],
            "Coverage": cov_s,
            "Status": STATUS_TEXT.get(d["status"], d["status"]),
            "Top constraint": (f"{c.get('item', '')} "
                               f"({c.get('available_total', '')}/"
                               f"{c.get('need', '')} {c.get('unit', '')})"
                               if c else ""),
        })
    st.dataframe(pd.DataFrame(tbl), use_container_width=True, hide_index=True)

with tab_drill:
    st.subheader("SKU → component tree")
    sku_opts = sorted({b["sku"] for b in sv} | {d["sku"] for d in dv})
    sku = st.selectbox("SKU", sku_opts)
    blocks_for = [b for b in sv if b["sku"] == sku]
    dem_for = [d for d in dv if d["sku"] == sku]
    if dem_for:
        d = dem_for[0]
        st.markdown(f"**Demand:** {d['target_kg']} kg · status "
                    f"{STATUS_CHIP.get(d['status'], d['status'])}")
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
            with st.expander(f"{STATUS_CHIP.get(i['status'], i['status'])} "
                             f"**{i['item']}** {i['designation'][:40]}"):
                st.write(f"Need **{i['need']} {i['unit']}** · available "
                         f"**{i['available_total']}** (primary {i['available_primary']})")
                if i.get("alternates"):
                    st.write("Alternates:")
                    # st.table: st.dataframe never mounts inside an initially-
                    # collapsed expander (helpers/st_compat).
                    st.table(pd.DataFrame(i["alternates"]))

with tab_item:
    st.subheader("Component → consuming SKUs")
    items = sorted(rep["item_reverse"].keys())
    item = st.selectbox("Component item", items)
    # an empty report (nothing scheduled/demanded) leaves the selectbox on
    # None — there is no row to look up
    cons = pd.DataFrame(rep["item_reverse"][item] if item else [])
    if "week_index" in cons.columns:
        # Sort on week_index (monotonic in real time), THEN label — sorting
        # on the ISO week number would put next January's W01 before W52.
        cons = cons.sort_values("week_index", kind="stable")
        cons.insert(int(cons.columns.get_loc("week_index")), "Wk",
                    [wk.week_index_label(w, _dem_anchor)
                     for w in cons["week_index"]])
        cons = cons.drop(columns=["week_index"])
    st.dataframe(cons, use_container_width=True, hide_index=True)

with tab_recv:
    st.subheader("Inbound appointments (Shipping/Receiving schedule)")
    if appts:
        st.dataframe(pd.DataFrame(appts), use_container_width=True,
                     hide_index=True)
    else:
        st.info("No receiving file found at "
                f"`{_receiving_path()}`. Drop the weekly xlsm there or push it via the bridge.")
    if appt_errors:
        st.caption(f"{len(appt_errors)} rows skipped (decayed cells).")

with tab_inb:
    st.subheader("Inbound purchase orders (open-PO report)")
    inb = rep.get("inbound")
    if not isinstance(inb, dict):
        st.info("This saved report predates the PO feed — press **Refresh "
                "from VIF** to recompute with inbound receipts.")
    else:
        lines = [ln for ln in (inb.get("lines") or []) if isinstance(ln, dict)]
        counts = {k: int(v) for k, v in (inb.get("join") or {}).items()
                  if isinstance(v, (int, float))}
        if not counts and lines:  # older shape: no join summary, count fates
            for ln in lines:
                f = str(ln.get("fate") or "")
                counts[f] = counts.get(f, 0) + 1
        # landed_unverifiable lines ARE counted (contract §3.6)
        n_used = counts.get("used", 0) + counts.get("landed_unverifiable", 0)
        _named = {"used", "landed_unverifiable", "overdue", "landed",
                  "received", "unjoinable"}
        n_other = sum(v for k, v in counts.items() if k not in _named)
        state = str(inb.get("state") or "unknown")
        # Off a fresh feed the gate still grades lines but the engine counts
        # none: 'Counted' beside "NOT counted" read as a contradiction
        counting = state == "ok"
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Counted" if counting else "Would count", n_used,
                  help=("PO lines that became receipts on the timeline"
                        if counting else
                        f"PO lines that pass the gate — NOT counted while the "
                        f"feed is {state}; they count once a current export "
                        "lands"))
        c2.metric("Overdue", counts.get("overdue", 0),
                  help="receipt date before the stock snapshot — never counted")
        c3.metric("Landed", counts.get("landed", 0),
                  help="a stock lot with a matching batch date already holds "
                       "it")
        c4.metric("Received", counts.get("received", 0),
                  help="ERP receipt number present")
        c5.metric("Unjoinable", counts.get("unjoinable", 0),
                  help="item code not in the BOM universe")
        c6.metric("Other", n_other,
                  help="unit mismatch, off-site without a transfer "
                       "appointment, bad qty / date")
        n_rows = inb.get("n_rows")
        st.markdown(
            f"**Source:** `{inb.get('source_path') or '—'}` · file "
            f"{inb.get('source_mtime') or '—'} · as of "
            f"{inb.get('as_of') or '—'} · latest receipt date "
            f"{inb.get('max_receipt_date') or '—'} · "
            f"{n_rows if n_rows is not None else len(lines)} rows")
        why = _FEED_STATE_HELP.get(state, "unrecognised feed state.")
        if counting:
            st.caption(f"Feed state **OK** — {why}")
        else:
            st.warning(
                f"Feed state **{state.upper()}** — no inbound receipt is "
                f"counted and every on-hand gap reads NO DATA: {why}"
                + (f" The {n_used} line(s) under 'Would count' pass the gate "
                   "and will count once the feed is current." if n_used else ""))
        aj = inb.get("appt_join")
        if isinstance(aj, dict):
            st.caption(f"Dock appointments joined to a PO: "
                       f"{aj.get('matched', 0)} of {aj.get('total', 0)}.")
        if lines:
            _rank = {f: i for i, f in enumerate(_FATE_ORDER)}

            def _line_key(ln: dict):
                f = str(ln.get("fate") or "")
                return (_rank.get(f, len(_rank)), f,
                        str(ln.get("receipt_date") or "9999-12-31"))

            rows = []
            for ln in sorted(lines, key=_line_key):
                rh = ln.get("ready_h")
                rows.append({
                    "po8": ln.get("po8") or "",
                    "item": ln.get("item") or "",
                    "designation": ln.get("designation") or "",
                    "qty": ln.get("qty"),
                    "unit": ln.get("unit") or "",
                    "receipt_date": ln.get("receipt_date") or "",
                    "slip_days": ln.get("slip_days"),
                    "arrival_area": ln.get("arrival_area") or "",
                    "supplier": ln.get("supplier") or "",
                    "fate": ln.get("fate") or "",
                    "reason": ln.get("reason") or "",
                    "ready": (hour_to_stamp(rh, _ANCHOR)
                              if isinstance(rh, (int, float)) else ""),
                    "tier": ln.get("tier") or "",
                })
            df_inb = pd.DataFrame(rows)
            # a None slip turns the column float ("6.0"/"NaN"): keep it whole
            df_inb["slip_days"] = pd.to_numeric(
                df_inb["slip_days"], errors="coerce").astype("Int64")
            st.dataframe(df_inb, use_container_width=True, hide_index=True)
        else:
            st.caption("No PO lines in this report.")
        errs = [str(e) for e in (inb.get("errors") or [])]
        if errs:
            st.caption(f"{len(errs)} import problem(s): "
                       + "; ".join(errs[:5]) + (" …" if len(errs) > 5 else ""))

with tab_dq:
    st.subheader("Data quality")
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

    # ---- PO join quality (report "quality", contract §4)
    st.markdown("---")
    st.markdown("**Inbound PO join quality**")
    q = rep.get("quality")
    if not isinstance(q, dict):
        st.info("Supply quality lists are not in this saved report — press "
                "**Refresh from VIF** to recompute with the PO feed.")
    else:
        for qkey, title, hint in _QUALITY_HINTS:
            vals = q.get(qkey) or []
            vals = list(vals) if isinstance(vals, (list, tuple)) else [vals]
            st.markdown(f"**{title} ({len(vals)})**")
            st.caption(hint)
            if not vals:
                st.code("none")
            elif all(isinstance(v, str) for v in vals):
                st.code(", ".join(vals))
            else:
                # st.table: st.dataframe never mounts inside an initially-
                # collapsed expander (helpers/st_compat)
                with st.expander(f"{len(vals)} rows", expanded=False):
                    st.table(pd.DataFrame(
                        [v if isinstance(v, dict) else {"value": str(v)}
                         for v in vals]))
