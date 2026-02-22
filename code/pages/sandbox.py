# pages/sandbox.py — Interactive sandbox for manual schedule editing.
#
# Mounts the custom React Gantt sandbox component for full drag-and-drop
# editing.  Save/Reset/Export buttons remain in Streamlit below the component.
# All real-time KPIs and interactions are handled client-side in the React
# component for instant feedback.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.paths import data_dir
from helpers.sandbox_engine import (
    load_capabilities,
    load_changeovers,
    load_demand_targets,
    save_sandbox_to_files,
)
from gantt_viewer import compute_changeover_details
from components.gantt_sandbox import gantt_sandbox

st.header("Sandbox")
st.caption(
    "Drag blocks horizontally or between lines.  Resize by dragging edges.  "
    "Right-click for split/remove.  Changes update KPIs live.  "
    "Use Save below to write changes to files."
)

dd = data_dir()

# ── Load TOML config ────────────────────────────────────────────────────
try:
    import tomli

    with open(Path(BASE_DIR).parent / "flowstate.toml", "rb") as f:
        toml_cfg = tomli.load(f)
except (ImportError, OSError, ValueError):
    toml_cfg = {}

planning_anchor = toml_cfg.get("scheduler", {}).get("planning_start_date", "2026-02-15 00:00:00")
cip_duration_h = toml_cfg.get("cip", {}).get("duration_h", 6)
min_run_hours = toml_cfg.get("scheduler", {}).get("min_run_hours", 4)
horizon_hours = 336

# ── Load reference data ─────────────────────────────────────────────────
caps_tuples = load_capabilities(dd)  # {(line_name, sku): rate}
changeovers_tuples = load_changeovers(dd)  # {(from_sku, to_sku): hours}
demand = load_demand_targets(dd)  # [{order_id, sku, qty_min, qty_max}]

# Convert capabilities to nested dict for React: {line: {sku: rate}}
caps_nested: dict[str, dict[str, float]] = {}
for (ln, sku), rate in caps_tuples.items():
    caps_nested.setdefault(ln, {})[sku] = rate

# Convert changeovers to nested dict: {from_sku: {to_sku: hours}}
co_nested: dict[str, dict[str, float]] = {}
for (f_sku, t_sku), hrs in changeovers_tuples.items():
    co_nested.setdefault(f_sku, {})[t_sku] = float(hrs)


# ── Load schedule blocks from CSV ──────────────────────────────────────
def _load_schedule_blocks() -> list[dict]:
    path = dd / "schedule_phase2.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    blocks = []
    for i, r in df.iterrows():
        blocks.append({
            "id": f"sched_{i}",
            "line_id": int(r.get("line_id", 0)),
            "line_name": str(r.get("line_name", "")),
            "order_id": str(r.get("order_id", "")),
            "sku": str(r.get("sku", "")),
            "sku_description": str(r.get("sku_description", "") or ""),
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
    for i, r in df.iterrows():
        blocks.append({
            "id": f"cip_{i}",
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


# Build line info list
def _load_lines() -> list[dict]:
    caps_path = dd / "capabilities_rates.csv"
    if not caps_path.exists():
        return []
    df = pd.read_csv(caps_path).drop_duplicates("line_name").sort_values("line_id")
    return [{"line_id": int(r["line_id"]), "line_name": str(r["line_name"])} for _, r in df.iterrows()]


# ── Initialize / refresh session state ──────────────────────────────────
# Reload from CSV if: (a) first visit, or (b) CSV is newer than cached data
_sched_path = dd / "schedule_phase2.csv"
_csv_mtime = _sched_path.stat().st_mtime if _sched_path.exists() else 0
_cached_mtime = st.session_state.get("sb_csv_mtime", 0)

if "sb_schedule" not in st.session_state or _csv_mtime > _cached_mtime:
    st.session_state["sb_schedule"] = _load_schedule_blocks()
    st.session_state["sb_cips"] = _load_cip_blocks()
    st.session_state["sb_holding"] = []
    st.session_state["sb_csv_mtime"] = _csv_mtime

schedule = st.session_state["sb_schedule"]
cip_blocks = st.session_state["sb_cips"]
holding = st.session_state["sb_holding"]
lines = _load_lines()

if not schedule and not cip_blocks:
    st.info("No schedule loaded. Run the solver first, then return here.")
    st.stop()

# ── Changeover KPIs for current sandbox schedule ──────────────────────
_chg_csv = dd / "changeovers.csv"
if schedule and _chg_csv.exists():
    _co_rows = []
    _cip_rows = []
    for b in schedule:
        if b.get("block_type") == "cip":
            _cip_rows.append({
                "line_id": b.get("line_id", 0),
                "start_hour": b.get("start_hour", 0),
                "end_hour": b.get("end_hour", 0),
            })
            continue
        _co_rows.append({
            "line_id": b.get("line_id", 0),
            "Task": b.get("line_name", ""),
            "Resource": str(b.get("sku", "")),
            "Start": b.get("start_hour", 0),
            "end_hour": b.get("end_hour", 0),
        })
    if _co_rows:
        _co_df = pd.DataFrame(_co_rows)
        _cip_co_df = pd.DataFrame(_cip_rows) if _cip_rows else None
        _co_detail = compute_changeover_details(_co_df, changeovers_path=_chg_csv, cip_df=_cip_co_df)
        if not _co_detail.empty:
            _co_totals = _co_detail[["changeovers", "ttp", "ffs", "topload", "casepacker", "conv_to_org", "cinn_to_non"]].sum()
            _m0, _m1, _m2, _m3, _m4, _m5, _m6 = st.columns(7)
            _m0.metric("CHANGEOVERS", int(_co_totals["changeovers"]))
            _m1.metric("TTP", int(_co_totals["ttp"]))
            _m2.metric("FFS", int(_co_totals["ffs"]))
            _m3.metric("Topload", int(_co_totals["topload"]))
            _m4.metric("Casepacker", int(_co_totals["casepacker"]))
            _m5.metric("Conv→Org", int(_co_totals["conv_to_org"]))
            _m6.metric("Cinn→Non", int(_co_totals["cinn_to_non"]))
            with st.expander("Changeover breakdown by line"):
                st.dataframe(
                    _co_detail.rename(columns={
                        "line_name": "Line", "changeovers": "Total CO",
                        "ttp": "TTP", "ffs": "FFS", "topload": "Topload",
                        "casepacker": "Casepacker", "conv_to_org": "Conv→Org",
                        "cinn_to_non": "Cinn→Non",
                    }),
                    use_container_width=True, hide_index=True,
                )

# ── Mount React component ──────────────────────────────────────────────
# A reset counter in the key forces a full remount after "Reset from solver",
# ensuring the React component reinitializes with fresh data.
_reset_gen = st.session_state.get("sb_reset_gen", 0)
component_state = gantt_sandbox(
    schedule=schedule,
    cip_windows=cip_blocks,
    capabilities=caps_nested,
    changeovers=co_nested,
    demand_targets=demand,
    lines=lines,
    holding_area=holding,
    config={
        "planning_anchor": planning_anchor,
        "cip_duration_h": cip_duration_h,
        "min_run_hours": min_run_hours,
        "horizon_hours": horizon_hours,
    },
    height=800,
    key=f"gantt_sandbox_{_reset_gen}",
)

# ── Update state from React ────────────────────────────────────────────
# Skip component_state processing right after a reset so the cached/stale
# component return value doesn't overwrite freshly loaded CSV data.
if st.session_state.pop("sb_just_reset", False):
    pass  # consume the flag; ignore stale component_state this cycle
elif component_state is not None:
    if "schedule" in component_state:
        st.session_state["sb_schedule"] = component_state["schedule"]
        schedule = component_state["schedule"]
    if "cipWindows" in component_state:
        st.session_state["sb_cips"] = component_state["cipWindows"]
        cip_blocks = component_state["cipWindows"]
    if "holdingArea" in component_state:
        st.session_state["sb_holding"] = component_state["holdingArea"]
        holding = component_state["holdingArea"]
    if component_state.get("lastAction"):
        st.caption(f"_Last action: {component_state['lastAction']}_")

# ── Save / Reset / Export ──────────────────────────────────────────────
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
                save_sandbox_to_files(schedule, cip_blocks, caps_tuples, demand, dd)
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
        st.session_state["sb_csv_mtime"] = _csv_mtime
        st.session_state["sb_just_reset"] = True
        st.session_state["sb_reset_gen"] = st.session_state.get("sb_reset_gen", 0) + 1
        st.rerun()

with col_export:
    if st.button("Go to Export", use_container_width=True):
        st.switch_page("pages/export.py")

# ══════════════════════════════════════════════════════════════════════
# Schedule Versions
# ══════════════════════════════════════════════════════════════════════
from helpers.version_manager import (
    list_versions,
    save_version,
    load_version,
    rename_version,
    delete_version,
    delete_all_versions,
    promote_version,
    export_version_excel,
    MAX_VERSIONS,
)
from gantt_viewer import (
    load_schedule as gv_load_schedule,
    build_gantt_figure,
    compute_changeovers,
)

st.divider()
st.subheader("Schedule Versions")
st.caption(
    f"Save up to {MAX_VERSIONS} versions of the current sandbox schedule. "
    "Compare KPIs and choose which version to promote as the official schedule."
)

# ── Save as Version ──────────────────────────────────────────────────
saved_versions = list_versions(dd)
next_num = len(saved_versions) + 1
ver_col1, ver_col2 = st.columns([3, 1])
with ver_col1:
    ver_name = st.text_input(
        "Version name",
        value=f"Option {next_num}",
        key="sb_ver_name",
        label_visibility="collapsed",
        placeholder="Enter version name...",
    )
with ver_col2:
    save_ver_clicked = st.button(
        "Save as Version",
        type="primary",
        use_container_width=True,
        disabled=len(saved_versions) >= MAX_VERSIONS,
    )

if save_ver_clicked:
    # Compute KPIs for the current sandbox state
    ver_co_total = 0
    ver_makespan = 0
    sched_only = [b for b in schedule if b.get("block_type") != "cip"]
    if sched_only:
        ver_makespan = max(b.get("end_hour", 0) for b in sched_only) - min(b.get("start_hour", 0) for b in sched_only)
        prev_by_line: dict[str, str] = {}
        for b in sorted(sched_only, key=lambda x: (x.get("line_name", ""), x.get("start_hour", 0))):
            ln = b.get("line_name", "")
            sk = b.get("sku", "")
            if ln in prev_by_line and prev_by_line[ln] != sk:
                ver_co_total += 1
            prev_by_line[ln] = sk

    # Demand adherence
    produced_by_order: dict[str, float] = {}
    for b in sched_only:
        oid = b.get("order_id", "")
        sk = b.get("sku", "")
        ln = b.get("line_name", "")
        rate = caps_tuples.get((ln, sk), 0)
        produced_by_order[oid] = produced_by_order.get(oid, 0) + rate * b.get("run_hours", 0)
    orders_met = 0
    orders_total = len(demand)
    for d in demand:
        qty = produced_by_order.get(d["order_id"], 0)
        if qty >= d["qty_min"]:
            orders_met += 1
    adh_pct = round(100 * orders_met / orders_total, 1) if orders_total else 0

    kpis = {
        "changeovers": ver_co_total,
        "makespan_h": round(ver_makespan, 1),
        "orders_met": orders_met,
        "orders_total": orders_total,
        "adherence_pct": adh_pct,
    }
    try:
        slug = save_version(ver_name, schedule, cip_blocks, kpis, dd)
        st.success(f"Saved version **{ver_name}**.")
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))

if len(saved_versions) >= MAX_VERSIONS:
    st.warning(f"Maximum of {MAX_VERSIONS} versions reached. Delete a version to save a new one.")

# ── Display saved versions ───────────────────────────────────────────
for ver in list_versions(dd):
    slug = ver["slug"]
    meta_name = ver.get("name", slug)
    kpis = ver.get("kpis", {})
    ts = ver.get("timestamp", "")

    with st.expander(f"{meta_name}  —  {ts}", expanded=False):
        # KPI metrics row
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Changeovers", kpis.get("changeovers", "—"))
        k2.metric("Makespan (h)", kpis.get("makespan_h", "—"))
        k3.metric("Orders Met", f"{kpis.get('orders_met', '—')}/{kpis.get('orders_total', '—')}")
        k4.metric("Adherence", f"{kpis.get('adherence_pct', '—')}%")

        # Load version data for Gantt and table
        vdata = load_version(slug, dd)
        v_sched = vdata.get("schedule", [])

        if v_sched:
            # Build a DataFrame for Gantt rendering
            from datetime import datetime as _dt, timedelta as _td
            try:
                anchor_dt = _dt.strptime(planning_anchor, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                anchor_dt = _dt(2026, 2, 15)
            v_rows = []
            for b in v_sched:
                start_dt = anchor_dt + _td(hours=float(b.get("start_hour", 0)))
                end_dt = anchor_dt + _td(hours=float(b.get("end_hour", 0)))
                v_rows.append({
                    "line_id": b.get("line_id", 0),
                    "line_name": b.get("line_name", ""),
                    "order_id": b.get("order_id", ""),
                    "sku": b.get("sku", ""),
                    "sku_description": b.get("sku_description", ""),
                    "start_hour": b.get("start_hour", 0),
                    "end_hour": b.get("end_hour", 0),
                    "run_hours": b.get("run_hours", 0),
                    "Task": b.get("line_name", ""),
                    "Resource": str(b.get("sku", "")),
                    "Start": start_dt,
                    "Finish": end_dt,
                    "start_dt": start_dt.strftime("%Y-%m-%d %H:%M"),
                    "end_dt": end_dt.strftime("%Y-%m-%d %H:%M"),
                })
            v_df = pd.DataFrame(v_rows)

            # Changeover breakdown by type
            chg_csv = dd / "changeovers.csv"
            if chg_csv.exists():
                v_cip_blocks = vdata.get("cip_blocks", [])
                v_cip_df = pd.DataFrame(v_cip_blocks) if v_cip_blocks else None
                co_detail = compute_changeover_details(v_df, changeovers_path=chg_csv, cip_df=v_cip_df)
                if not co_detail.empty:
                    co_totals = co_detail[["ttp", "ffs", "topload", "casepacker", "conv_to_org", "cinn_to_non"]].sum()
                    m1, m2, m3, m4, m5, m6 = st.columns(6)
                    m1.metric("TTP", int(co_totals["ttp"]))
                    m2.metric("FFS", int(co_totals["ffs"]))
                    m3.metric("Topload", int(co_totals["topload"]))
                    m4.metric("Casepacker", int(co_totals["casepacker"]))
                    m5.metric("Conv→Org", int(co_totals["conv_to_org"]))
                    m6.metric("Cinn→Non", int(co_totals["cinn_to_non"]))
                    st.dataframe(
                        co_detail.rename(columns={
                            "line_name": "Line", "changeovers": "Total CO",
                            "ttp": "TTP", "ffs": "FFS", "topload": "Topload",
                            "casepacker": "Casepacker", "conv_to_org": "Conv→Org",
                            "cinn_to_non": "Cinn→Non",
                        }),
                        use_container_width=True, hide_index=True,
                    )

            # Gantt chart — include CIP blocks from saved version
            v_cip_gantt = None
            v_cip_list = vdata.get("cip_blocks", [])
            if v_cip_list:
                _cip_rows = []
                for cb in v_cip_list:
                    _cs = anchor_dt + _td(hours=float(cb.get("start_hour", 0)))
                    _ce = anchor_dt + _td(hours=float(cb.get("end_hour", 0)))
                    _cip_rows.append({
                        "line_id": cb.get("line_id", 0),
                        "Start": _cs,
                        "Finish": _ce,
                        "Task": cb.get("line_name", str(cb.get("line_id", ""))),
                        "Resource": "CIP",
                    })
                v_cip_gantt = pd.DataFrame(_cip_rows)
            v_fig = build_gantt_figure(v_df, cip_df=v_cip_gantt, title=meta_name)
            st.plotly_chart(v_fig, use_container_width=True, key=f"ver_gantt_{slug}")

            # Schedule table with sku_description, sorted by line then start time
            display_cols = ["line_name", "order_id", "sku", "sku_description", "start_dt", "end_dt", "run_hours"]
            avail = [c for c in display_cols if c in v_df.columns]
            v_display = v_df[avail].copy()
            v_cip_list = vdata.get("cip_blocks", [])
            if v_cip_list:
                cip_rows = []
                for cb in v_cip_list:
                    cs = anchor_dt + _td(hours=float(cb.get("start_hour", 0)))
                    ce = anchor_dt + _td(hours=float(cb.get("end_hour", 0)))
                    cip_rows.append({
                        "line_name": cb.get("line_name", ""),
                        "order_id": "CIP",
                        "sku": "CIP",
                        "sku_description": "",
                        "start_dt": cs.strftime("%Y-%m-%d %H:%M"),
                        "end_dt": ce.strftime("%Y-%m-%d %H:%M"),
                        "run_hours": round((ce - cs).total_seconds() / 3600, 1),
                    })
                cip_tbl = pd.DataFrame(cip_rows)
                cip_avail = [c for c in avail if c in cip_tbl.columns]
                v_display = pd.concat([v_display, cip_tbl[cip_avail]], ignore_index=True)
            sort_cols = [c for c in ["line_name", "start_dt"] if c in v_display.columns]
            v_display = v_display.sort_values(sort_cols) if sort_cols else v_display
            st.dataframe(v_display, use_container_width=True, hide_index=True)

        # Action buttons
        ba, bb, bc = st.columns(3)
        with ba:
            if st.button("Save as Official Schedule", key=f"promote_{slug}", type="primary", use_container_width=True):
                promote_version(slug, dd)
                st.session_state["schedule_source"] = "version"
                for k in ["sb_schedule", "sb_cips", "sb_holding"]:
                    st.session_state.pop(k, None)
                st.success(f"**{meta_name}** is now the official schedule.")
                st.rerun()
        with bb:
            if st.button("Delete this version", key=f"delete_{slug}", use_container_width=True):
                delete_version(slug, dd)
                st.rerun()
        with bc:
            try:
                excel_bytes = export_version_excel(slug, dd)
                st.download_button(
                    "Export to Excel",
                    data=excel_bytes,
                    file_name=f"{meta_name}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"export_{slug}",
                    use_container_width=True,
                )
            except (ImportError, OSError, ValueError) as exc:
                st.caption(f"Export unavailable: {exc}")

# ── Delete all versions ──────────────────────────────────────────────
if list_versions(dd):
    st.divider()
    if st.button("Delete all schedule versions", type="secondary"):
        delete_all_versions(dd)
        st.rerun()
