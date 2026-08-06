# code/pages/stock_check.py — Stock Check: component availability monitor.
#
# Two views:
#   Schedule view — will anything on the current board fail for components?
#   Demand view   — which demand SKUs can't reach >=90% of target?
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

from helpers.paths import data_dir  # noqa: E402
from helpers.timefmt import hour_to_stamp  # noqa: E402

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
    from stockcheck.api import stock_check_report
    from stockcheck.receiving_import import parse_receiving_schedule
    from stockcheck.vif_import import import_vif_folder
except Exception as exc:  # engine not merged yet -> render shell w/ demo
    ENGINE_OK = False
    ENGINE_ERR = str(exc)

STATUS_CHIP = {
    "OK": ":green[OK]",
    "TIGHT": ":orange[TIGHT]",
    "AT_RISK": ":red[AT RISK]",
    "DO_NOT_SCHEDULE": ":red[DO NOT SCHEDULE]",
    "NO_BOM": ":gray[NO BOM]",
    "UNK": ":gray[UNK]",
    "NOT_TRACKED": ":gray[NOT TRACKED]",
}

# ---------------------------------------------------------------- header
left, mid, right = st.columns([3, 2, 2])
with left:
    vif_folder = st.text_input(
        "VIF export folder",
        value=settings.get("vif_folder",
                           DEFAULT_VIF if Path(DEFAULT_VIF).exists()
                           else str(DEV_VIF)),
        help="Network share on Carsten's machine; dev fixtures locally.")
with mid:
    week_index = st.selectbox("Demand week", options=[None, 0, 1, 2],
                              format_func=lambda x: "All weeks" if x is None
                              else f"Week {x}")
with right:
    refresh = st.button("Refresh from VIF", type="primary",
                        help="Re-imports only if source files changed.")

if not ENGINE_OK:
    st.warning(f"Stock-check engine unavailable: {ENGINE_ERR}")
    st.stop()

# ------------------------------------------------------- availability toggles
with st.expander("Availability — which stock counts?", expanded=False):
    st.caption("Default: only **Ava** (available). `Loc` = QC/warehouse-hold — "
               "opt in per depot if you know it will release.")
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
@st.cache_data(ttl=300, show_spinner="Crunching BOMs…")
def _report(vif_folder: str, toggles_json: str, week_index):
    return stock_check_report(DATA, vif_folder,
                              toggles=json.loads(toggles_json),
                              week_index=week_index)


if refresh:
    st.cache_data.clear()

rep = _report(vif_folder, json.dumps(toggles, sort_keys=True), week_index)

if "error" in rep:
    st.error(f"Import failed: {rep['error']}")
    st.json(rep.get("import_errors", []))
    st.stop()

# source freshness strip
src = rep.get("source_files", {})
with st.container():
    chips = " · ".join(f"`{n}` {t}" for n, t in sorted(src.items()))
    st.caption(f"Sources: {chips}")
if rep.get("import_errors"):
    st.warning("Some files failed to import: " +
               "; ".join(rep["import_errors"]))

# receiving appointments
appts, appt_errors = parse_receiving_schedule(DEV_RECV) if DEV_RECV.exists() else ([], [])
po_appts = {a["po"]: a for a in appts if a["po"]}

# ---------------------------------------------------------------- metrics
sv = rep["schedule_view"]
dv = rep["demand_view"]
n_blocks_risk = sum(1 for b in sv if b["status"] in ("AT_RISK", "TIGHT"))
n_dns = sum(1 for d in dv if d["status"] == "DO_NOT_SCHEDULE")
n_dem_risk = sum(1 for d in dv if d["status"] in ("AT_RISK",))
m1, m2, m3, m4 = st.columns(4)
m1.metric("Schedule blocks at risk", n_blocks_risk, delta=f"of {len(sv)}")
m2.metric("Demand SKUs DO_NOT_SCHEDULE", n_dns, delta=f"of {len(dv)}")
m3.metric("Demand SKUs at risk", n_dem_risk)
m4.metric("Receiving appointments", len(appts))

# ---------------------------------------------------------------- tabs
tab_sched, tab_demand, tab_drill, tab_item, tab_recv, tab_dq = st.tabs(
    ["Schedule view", "Demand view", "SKU drill-down", "Item view",
     "Receiving", "Data quality"])


def _item_line(i: dict) -> str:
    po = ""
    for a in i.get("alternates", []):
        pass
    return (f"{STATUS_CHIP.get(i['status'], i['status'])} **{i['item']}** "
            f"{i['designation'][:28]} — need {i['need']} {i['unit']}, "
            f"available {i['available_total']}, ratio {i['ratio']}")


with tab_sched:
    st.subheader("Will the current board run?")
    risky = [b for b in sv if b["status"] in ("AT_RISK", "TIGHT", "NOT_TRACKED")]
    ok = len(sv) - len(risky)
    st.caption(f"{ok} of {len(sv)} production blocks fully covered.")
    for b in sorted(risky, key=lambda b: (b["start_h"])):
        with st.expander(
                f"{STATUS_CHIP.get(b['status'], b['status'])} **{b['sku']}** "
                f"{b['line_name']} · {hour_to_stamp(b['start_h'])} → "
                f"{hour_to_stamp(b['end_h'])} · {b['cases']:.0f} cases"):
            bad = [i for i in b["items"]
                   if i["status"] in ("AT_RISK", "TIGHT", "NOT_TRACKED")]
            bad.sort(key=lambda i: (i["ratio"] is None,
                                    i["ratio"] if i["ratio"] is not None else 0))
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
        tbl.append({
            "SKU": d["sku"], "Wk": d["week_index"],
            "Target kg": d["target_kg"],
            "Coverage": (f"{d['achievable_ratio']:.0%}"
                         if d["achievable_ratio"] is not None else "—"),
            "Status": STATUS_CHIP.get(d["status"], d["status"]),
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
                    st.dataframe(pd.DataFrame(i["alternates"]),
                                 hide_index=True)

with tab_item:
    st.subheader("Component → consuming SKUs")
    items = sorted(rep["item_reverse"].keys())
    item = st.selectbox("Component item", items)
    cons = rep["item_reverse"][item]
    st.dataframe(pd.DataFrame(cons), use_container_width=True,
                 hide_index=True)

with tab_recv:
    st.subheader("Inbound appointments (Shipping/Receiving schedule)")
    if appts:
        st.dataframe(pd.DataFrame(appts), use_container_width=True,
                     hide_index=True)
    else:
        st.info("No receiving file found at "
                f"`{DEV_RECV}`. Drop the weekly xlsm there.")
    if appt_errors:
        st.caption(f"{len(appt_errors)} rows skipped (decayed cells).")

with tab_dq:
    st.subheader("Data quality")
    st.markdown(f"**NO_BOM SKUs ({len(rep['no_bom_skus'])})** — no recipe in "
                "ediact 3, cannot be checked:")
    st.code(", ".join(rep["no_bom_skus"]) or "none")
    st.markdown(f"**UNK items ({len(rep['unk'])})** — requirement exists but "
                "magnitude unknown:")
    if rep["unk"]:
        st.dataframe(pd.DataFrame(rep["unk"]).head(100),
                     use_container_width=True, hide_index=True)
    else:
        st.caption("none")
