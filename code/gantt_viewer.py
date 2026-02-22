# gantt_viewer.py — Gantt chart for schedule_phase2.csv (and optional cip_windows.csv).
# Color by SKU; data labels on bars; click to highlight same SKU; CIP as grey blocks; changeovers table.

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Add script dir for imports if needed
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from inventory_checker import run_inventory_check, results_to_dataframe


PLANNING_ANCHOR = "2026-02-15 00:00:00"
# Week-0 ends at hour 167 (Sat 23:00); Week-1 starts at hour 168 (Sun 00:00)
WEEK1_START_HOUR = 168


def load_schedule(csv_path: Path) -> pd.DataFrame:
    """Load and normalize schedule_phase2.csv."""
    df = pd.read_csv(csv_path)
    df["Start"] = pd.to_datetime(df["start_dt"])
    df["Finish"] = pd.to_datetime(df["end_dt"])
    df["Task"] = df["line_name"].astype(str)
    # Trial rows get a distinct Resource label
    if "is_trial" in df.columns:
        df["is_trial"] = df["is_trial"].fillna(False).astype(bool)
    else:
        df["is_trial"] = False
    df["Resource"] = df.apply(
        lambda r: f"TRIAL: {r['sku']}" if r["is_trial"] else str(r["sku"]),
        axis=1,
    )
    if "run_hours" not in df.columns:
        df["run_hours"] = (df["Finish"] - df["Start"]).dt.total_seconds() / 3600
    if "sku_description" not in df.columns:
        df["sku_description"] = ""
    else:
        df["sku_description"] = df["sku_description"].fillna("").astype(str)
    df = df.sort_values(["line_id", "Start"])
    return df


def load_cip_windows(csv_path: Path) -> pd.DataFrame | None:
    """Load cip_windows.csv if present. Expected: line_id, start_hour, end_hour (and optional line_name)."""
    if not csv_path.exists():
        return None
    df = pd.read_csv(csv_path)
    if df.empty or "line_id" not in df.columns or "start_hour" not in df.columns:
        return None
    anchor = pd.Timestamp(PLANNING_ANCHOR)
    df["Start"] = anchor + pd.to_timedelta(df["start_hour"], unit="h")
    df["Finish"] = anchor + pd.to_timedelta(df["end_hour"], unit="h")
    df["Task"] = df.get("line_name", df["line_id"].astype(str)).astype(str)
    df["Resource"] = "CIP"
    return df


def compute_changeovers(schedule_df: pd.DataFrame) -> tuple[int, pd.DataFrame]:
    """Compute total changeovers and per-line changeover counts (SKU switches on same line)."""
    df = schedule_df.sort_values(["line_id", "Start"])
    per_line = []
    total = 0
    for line_id, grp in df.groupby("line_id", sort=True):
        skus = grp["Resource"].tolist()
        count = sum(1 for i in range(1, len(skus)) if skus[i] != skus[i - 1])
        total += count
        line_name = grp["Task"].iloc[0] if not grp.empty else str(line_id)
        per_line.append({"line_id": line_id, "line_name": line_name, "changeovers": count})
    return total, pd.DataFrame(per_line)


def compute_changeover_details(
    schedule_df: pd.DataFrame,
    changeovers_path: Path | None = None,
    cip_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Detailed changeover report broken down by type per line.

    When *cip_df* is provided (with columns ``line_id``, ``Start``/
    ``start_hour``, ``Finish``/``end_hour``), conv_to_org and cinn_to_non
    penalties are waived for changeovers where a CIP falls between the
    two production blocks (the CIP absorbs the cleaning cost).

    Returns a DataFrame with columns:
        line_name, changeovers, ttp, ffs, topload, casepacker,
        conv_to_org, cinn_to_non
    """
    # Load machine-change flags from changeovers.csv
    mc_map: dict[tuple[str, str], dict] = {}
    if changeovers_path and changeovers_path.exists():
        chg = pd.read_csv(changeovers_path)
        has_mc = all(c in chg.columns for c in ("ttp_change", "ffs_change", "topload_change", "casepacker_change"))
        has_extra = all(c in chg.columns for c in ("conv_to_org_change", "cinn_to_non"))
        def _flag(val, default=0):
            v = pd.to_numeric(val, errors="coerce")
            return default if pd.isna(v) else int(v)

        for _, r in chg.iterrows():
            pair = (str(r["from_sku"]), str(r["to_sku"]))
            entry: dict = {}
            if has_mc:
                entry["ttp"] = _flag(r.get("ttp_change", 0))
                entry["ffs"] = _flag(r.get("ffs_change", 0))
                entry["topload"] = _flag(r.get("topload_change", 0))
                entry["casepacker"] = _flag(r.get("casepacker_change", 0))
            else:
                entry.update({"ttp": 1, "ffs": 1, "topload": 1, "casepacker": 1})
            if has_extra:
                entry["conv_to_org"] = _flag(r.get("conv_to_org_change", 0))
                entry["cinn_to_non"] = _flag(r.get("cinn_to_non", 0))
            else:
                entry.update({"conv_to_org": 0, "cinn_to_non": 0})
            mc_map[pair] = entry

    # Use numeric hour columns for CIP-gap comparison when available;
    # this avoids type mismatches between int hours and Timestamps.
    _use_numeric = "start_hour" in schedule_df.columns and "end_hour" in schedule_df.columns
    s_col_sched = "start_hour" if _use_numeric else "Start"
    f_col_sched = "end_hour" if _use_numeric else ("Finish" if "Finish" in schedule_df.columns else None)

    # Build per-line CIP lookup: {line_id: [(start, end), ...]}
    cip_by_line: dict[int, list[tuple]] = {}
    if cip_df is not None and not cip_df.empty and "line_id" in cip_df.columns:
        s_col_cip = "start_hour" if "start_hour" in cip_df.columns else "Start"
        f_col_cip = "end_hour" if "end_hour" in cip_df.columns else "Finish"
        if s_col_cip in cip_df.columns and f_col_cip in cip_df.columns:
            for _, cr in cip_df.iterrows():
                cip_by_line.setdefault(int(cr["line_id"]), []).append(
                    (cr[s_col_cip], cr[f_col_cip])
                )

    default_mc = {"ttp": 1, "ffs": 1, "topload": 1, "casepacker": 1, "conv_to_org": 0, "cinn_to_non": 0}
    df = schedule_df.sort_values(["line_id", "Start"])
    rows = []
    for line_id, grp in df.groupby("line_id", sort=True):
        skus = grp["Resource"].tolist()
        starts = grp[s_col_sched].tolist()
        finishes = grp[f_col_sched].tolist() if f_col_sched else [None] * len(skus)
        line_cips = cip_by_line.get(int(line_id), [])
        line_name = grp["Task"].iloc[0] if not grp.empty else str(line_id)
        counts = {"changeovers": 0, "ttp": 0, "ffs": 0, "topload": 0, "casepacker": 0, "conv_to_org": 0, "cinn_to_non": 0}
        for i in range(1, len(skus)):
            if skus[i] != skus[i - 1]:
                counts["changeovers"] += 1
                mc = mc_map.get((str(skus[i - 1]), str(skus[i])), default_mc)
                for k in ("ttp", "ffs", "topload", "casepacker"):
                    counts[k] += mc.get(k, 0)
                # Check if a CIP between these blocks absorbs conv→org / cinn→non
                cip_between = False
                prev_end = finishes[i - 1]
                curr_start = starts[i]
                if prev_end is not None and line_cips:
                    cip_between = any(
                        cs >= prev_end and cf <= curr_start
                        for cs, cf in line_cips
                    )
                if not cip_between:
                    for k in ("conv_to_org", "cinn_to_non"):
                        counts[k] += mc.get(k, 0)
        rows.append({"line_name": line_name, **counts})
    return pd.DataFrame(rows)


def build_gantt_figure(
    schedule_df: pd.DataFrame,
    cip_df: pd.DataFrame | None,
    title: str = "Flowstate schedule",
    highlighted_sku: str | None = None,
) -> go.Figure:
    """Build Gantt: one row per line, color by SKU, labels on bars, CIP grey with 'CIP' text; optional highlight by SKU."""
    line_order = schedule_df.drop_duplicates("line_id").sort_values("line_id")["Task"].tolist()
    if not line_order:
        return go.Figure(layout_title_text="No schedule data")

    # Production frame: keep run_hours and datetimes for labels/hover
    prod_cols = ["Start", "Finish", "Task", "Resource", "line_id", "run_hours", "sku", "is_trial", "sku_description"]
    if "start_dt" in schedule_df.columns:
        prod_cols.append("start_dt")
    if "end_dt" in schedule_df.columns:
        prod_cols.append("end_dt")
    frames = [schedule_df[[c for c in prod_cols if c in schedule_df.columns]].copy()]
    if "sku_description" not in frames[0].columns:
        frames[0]["sku_description"] = ""
    frames[0]["start_dt"] = frames[0].get("start_dt", frames[0]["Start"].astype(str))
    frames[0]["end_dt"] = frames[0].get("end_dt", frames[0]["Finish"].astype(str))
    # Trial bars get a TRIAL prefix; all bars show SKU + truncated description + hours
    is_trial_col = frames[0].get("is_trial", pd.Series(False, index=frames[0].index))
    def _bar_label(r):
        desc = str(r.get("sku_description", "") or "")
        desc_part = f"\n{desc[:22]}" if desc else ""
        if is_trial_col.get(r.name, False):
            return f"TRIAL\n{r['sku']}{desc_part}\n{int(r['run_hours'])} h"
        return f"{r['Resource']}{desc_part}\n{int(r['run_hours'])} h"
    frames[0]["label"] = frames[0].apply(_bar_label, axis=1)

    if cip_df is not None and not cip_df.empty:
        cip_df = cip_df.copy()
        cip_df["line_id"] = cip_df.get("line_id", 0)
        cip_df["run_hours"] = (cip_df["Finish"] - cip_df["Start"]).dt.total_seconds() / 3600
        cip_df["start_dt"] = cip_df["Start"].dt.strftime("%Y-%m-%d %H:%M")
        cip_df["end_dt"] = cip_df["Finish"].dt.strftime("%Y-%m-%d %H:%M")
        cip_df["label"] = "CIP"
        cip_df["sku_description"] = ""
        frames.append(cip_df[["Start", "Finish", "Task", "Resource", "line_id", "run_hours", "start_dt", "end_dt", "label", "sku_description"]])
    combined = pd.concat(frames, ignore_index=True)

    combined["Task"] = pd.Categorical(combined["Task"], categories=line_order, ordered=True)
    combined = combined.sort_values(["line_id", "Start"])

    resources = [str(r) for r in combined["Resource"].unique().tolist()]
    skus = sorted([r for r in resources if r != "CIP" and not r.startswith("TRIAL:")])
    trial_resources = sorted([r for r in resources if r.startswith("TRIAL:")])
    color_discrete_map = dict(
        zip(skus, px.colors.qualitative.Plotly * (1 + len(skus) // len(px.colors.qualitative.Plotly))),
    )
    color_discrete_map["CIP"] = "#888888"
    for tr in trial_resources:
        color_discrete_map[tr] = "#D4A017"  # gold for trial blocks

    legend_order = skus + trial_resources + (["CIP"] if "CIP" in resources else [])
    fig = px.timeline(
        combined,
        x_start="Start",
        x_end="Finish",
        y="Task",
        color="Resource",
        color_discrete_map=color_discrete_map,
        category_orders={"Resource": legend_order},
        text="label",
        title=title,
    )
    fig.update_traces(textposition="inside", textfont=dict(size=14), insidetextanchor="middle")
    fig.update_layout(
        height=max(400, 28 * len(line_order)),
        xaxis_title="",
        yaxis_title="Line",
        legend_title="SKU",
        bargap=0.15,
        barmode="overlay",
        xaxis=dict(showgrid=True, type="date"),
        yaxis=dict(categoryorder="array", categoryarray=line_order),
        legend=dict(itemclick="toggleothers", itemdoubleclick="toggle"),
    )
    fig.update_yaxes(autorange="reversed")

    # Hover: SKU, description, start datetime, end datetime, run hours
    for trace in fig.data:
        if trace.name == "CIP":
            trace.hovertemplate = "CIP<br>Start: %{base|%Y-%m-%d %H:%M}<br>End: %{x|%Y-%m-%d %H:%M}<br>Duration: 6 h<extra></extra>"
        else:
            trace.hovertemplate = (
                "<b>%{customdata[0]}</b><br>"
                "%{customdata[4]}<br>"
                "Start: %{customdata[1]}<br>End: %{customdata[2]}<br>Run: %{customdata[3]} h<extra></extra>"
            )
        # Build customdata per point: [sku, start_dt, end_dt, run_hours, sku_description]
        mask = combined["Resource"] == trace.name
        sub = combined.loc[mask]
        desc_col = sub["sku_description"] if "sku_description" in sub.columns else pd.Series("", index=sub.index)
        trace.customdata = list(zip(
            sub["Resource"],
            sub.get("start_dt", sub["Start"].astype(str)),
            sub.get("end_dt", sub["Finish"].astype(str)),
            sub["run_hours"].round(1),
            desc_col.fillna(""),
        ))

    if highlighted_sku:
        for trace in fig.data:
            opacity = 0.25 if trace.name != highlighted_sku else 1.0
            fig.update_traces(opacity=opacity, selector=dict(name=trace.name))

    # 2-week view: vertical line at Week-0 / Week-1 boundary (use shape; add_vline breaks with datetime)
    week1_start_ts = pd.Timestamp(PLANNING_ANCHOR) + pd.Timedelta(hours=WEEK1_START_HOUR)
    fig.add_shape(
        type="line",
        x0=week1_start_ts,
        x1=week1_start_ts,
        y0=0,
        y1=1,
        xref="x",
        yref="paper",
        line=dict(dash="dash", color="gray"),
    )
    fig.add_annotation(
        x=week1_start_ts,
        y=1.02,
        yref="paper",
        text="Week 1",
        showarrow=False,
        font=dict(size=11),
    )

    return fig


def run_streamlit(data_dir: Path | None = None) -> None:
    """Run Streamlit UI: Gantt with filters, CIP, export, inventory validation."""
    try:
        import streamlit as st
    except ImportError:
        print("Install streamlit: pip install streamlit")
        raise

    data_dir = data_dir or BASE_DIR.parent / "data"
    st.set_page_config(page_title="Flowstate Gantt", layout="wide")
    st.title("Flowstate schedule — Gantt viewer")

    default_schedule = data_dir / "schedule_phase2.csv"
    default_cip = data_dir / "cip_windows.csv"

    # --- Sidebar: filters and settings ---
    with st.sidebar:
        st.header("Settings")
        schedule_path = st.text_input(
            "Schedule CSV path",
            value=str(default_schedule),
            help="Path to schedule_phase2.csv",
        )
        use_cip = st.checkbox("Include CIP windows (grey bars)", value=default_cip.exists())
        cip_path = st.text_input(
            "CIP windows CSV path",
            value=str(default_cip),
            disabled=not use_cip,
        ) if use_cip else None

    if not Path(schedule_path).exists():
        st.warning(f"Schedule file not found: {schedule_path}")
        st.info("Run the scheduler first with full data to generate schedule_phase2.csv")
        return

    schedule_df = load_schedule(Path(schedule_path))
    cip_df = load_cip_windows(Path(cip_path)) if use_cip and cip_path else None

    # --- Sidebar: filters (Phase 4.1) ---
    with st.sidebar:
        st.header("Filters")
        all_lines = sorted(schedule_df["Task"].unique().tolist())
        all_skus = sorted(schedule_df["Resource"].unique().tolist())
        week_options = ["All", "Week 0", "Week 1"]

        filter_lines = st.multiselect("Lines", options=all_lines, default=all_lines, key="filter_lines")
        filter_skus = st.multiselect("SKUs", options=all_skus, default=all_skus, key="filter_skus")
        filter_week = st.selectbox("Week", options=week_options, index=0, key="filter_week")

    # Apply filters
    filtered_df = schedule_df.copy()
    if filter_lines:
        filtered_df = filtered_df[filtered_df["Task"].isin(filter_lines)]
    if filter_skus:
        filtered_df = filtered_df[filtered_df["Resource"].isin(filter_skus)]
    if filter_week == "Week 0":
        filtered_df = filtered_df[filtered_df["start_hour"] <= WEEK1_START_HOUR]
    elif filter_week == "Week 1":
        filtered_df = filtered_df[filtered_df["start_hour"] >= WEEK1_START_HOUR]

    # Filter CIP to match selected lines
    filtered_cip = None
    if cip_df is not None and not cip_df.empty and filter_lines:
        filtered_cip = cip_df[cip_df["Task"].isin(filter_lines)]
        if filter_week == "Week 0":
            anchor = pd.Timestamp(PLANNING_ANCHOR)
            week1_ts = anchor + pd.Timedelta(hours=WEEK1_START_HOUR)
            filtered_cip = filtered_cip[filtered_cip["Start"] <= week1_ts]
        elif filter_week == "Week 1":
            anchor = pd.Timestamp(PLANNING_ANCHOR)
            week1_ts = anchor + pd.Timedelta(hours=WEEK1_START_HOUR)
            filtered_cip = filtered_cip[filtered_cip["Start"] >= week1_ts]

    if filtered_df.empty:
        st.info("No schedule data matches the current filters.")
        return

    # Click-to-highlight
    highlighted_sku = st.session_state.get("gantt_highlighted_sku")

    fig = build_gantt_figure(
        filtered_df,
        filtered_cip,
        title="Schedule by line (color = SKU). Click a bar to highlight that SKU on all lines.",
        highlighted_sku=highlighted_sku,
    )

    st.plotly_chart(
        fig,
        use_container_width=True,
        key="gantt_chart",
        on_select="rerun",
        selection_mode="points",
    )

    # Selection handling
    sel = st.session_state.get("gantt_chart")
    if sel and isinstance(sel, dict):
        inner = sel.get("selection", sel)
        pts = (inner.get("points", sel.get("points", [])) if isinstance(inner, dict) else []) or []
        if pts and isinstance(pts, list) and len(pts) > 0:
            p = pts[0] if isinstance(pts[0], dict) else None
            if p:
                curve_number = p.get("curveNumber", p.get("curve_number"))
                if curve_number is not None and curve_number < len(fig.data):
                    new_sku = fig.data[curve_number].name
                    if st.session_state.get("gantt_highlighted_sku") != new_sku:
                        st.session_state["gantt_highlighted_sku"] = new_sku
                        st.rerun()
        elif highlighted_sku:
            st.session_state.pop("gantt_highlighted_sku", None)
            st.rerun()
    if highlighted_sku:
        if st.button("Clear SKU highlight"):
            st.session_state.pop("gantt_highlighted_sku", None)
            st.rerun()

    # --- Export (Phase 4.2) ---
    col_export1, col_export2, _ = st.columns([1, 1, 3])
    with col_export1:
        # Export as PNG via plotly
        try:
            export_h = min(4000, max(600, 30 * len(all_lines)))
            png_bytes = fig.to_image(format="png", width=1800, height=export_h, scale=2)
            st.download_button(
                label="Download PNG",
                data=png_bytes,
                file_name="flowstate_gantt.png",
                mime="image/png",
            )
        except (ImportError, ValueError, OSError, RuntimeError) as exc:
            st.caption(f"PNG export unavailable: {exc}")
    with col_export2:
        try:
            export_h = min(4000, max(600, 30 * len(all_lines)))
            pdf_bytes = fig.to_image(format="pdf", width=1800, height=export_h, scale=2)
            st.download_button(
                label="Download PDF",
                data=pdf_bytes,
                file_name="flowstate_gantt.pdf",
                mime="application/pdf",
            )
        except (ImportError, ValueError, OSError, RuntimeError) as exc:
            st.caption(f"PDF export unavailable: {exc}")

    # Changeover section
    total_co, changeovers_df = compute_changeovers(filtered_df)
    st.caption("Changeovers")
    col_metric, col_table = st.columns([1, 4])
    with col_metric:
        st.metric("Total", total_co)
    with col_table:
        co_display = changeovers_df[["line_name", "changeovers"]].rename(columns={"line_name": "Line", "changeovers": "CO"})
        st.dataframe(co_display, use_container_width=True, height=min(120, 28 * min(5, len(co_display))))

    # Validation report (if exists)
    validation_path = Path(schedule_path).parent / "validation_report.txt"
    if validation_path.exists():
        with st.expander("Validation report"):
            st.code(validation_path.read_text(encoding="utf-8"), language="text")

    with st.expander("Schedule summary"):
        st.dataframe(
            filtered_df[["line_name", "order_id", "sku", "start_dt", "end_dt", "run_hours"]].rename(
                columns={"start_dt": "Start", "end_dt": "End"}
            ),
            use_container_width=True,
        )

    # Inventory validation section
    st.divider()
    st.subheader("Inventory validation")
    data_dir_path = Path(schedule_path).parent
    bom_path = data_dir_path / "bom_by_sku.csv"
    onhand_path = data_dir_path / "on_hand_inventory.csv"
    inbound_path = data_dir_path / "inbound_inventory.csv"
    has_inventory_data = bom_path.exists() and onhand_path.exists()

    if has_inventory_data:
        inv_results = run_inventory_check(data_dir_path)
        inv_df = results_to_dataframe(inv_results)
        if inv_df.empty:
            st.info("No orders with BOM-defined materials in schedule.")
        else:
            n_flagged = (inv_df["status"] == "FLAG").sum()
            n_planned = (inv_df["status"] == "PLAN").sum()
            col_plan, col_flag, _ = st.columns([1, 1, 2])
            with col_plan:
                st.metric("Plan (OK)", n_planned, help="Sufficient on-hand + inbound inventory")
            with col_flag:
                st.metric("Flag (review)", n_flagged, help="Insufficient inventory; adjust demand or accept lower fill %")
            status_filter = st.multiselect(
                "Filter by status",
                options=["PLAN", "FLAG"],
                default=["PLAN", "FLAG"],
                key="inv_status_filter",
            )
            display_df = inv_df[inv_df["status"].isin(status_filter)]
            display_cols = ["order_id", "sku", "produced", "start_hour", "status", "message"]
            if "shortfall_detail" in display_df.columns and display_df["shortfall_detail"].str.len().gt(0).any():
                display_cols.append("shortfall_detail")
            st.dataframe(
                display_df[[c for c in display_cols if c in display_df.columns]],
                use_container_width=True,
                height=min(350, 35 * len(display_df) + 40),
                column_config={
                    "status": st.column_config.TextColumn("Status", help="PLAN = OK, FLAG = needs review"),
                    "message": st.column_config.TextColumn("Message", width="large"),
                    "shortfall_detail": st.column_config.TextColumn("Shortfall", width="medium"),
                },
            )
            if n_flagged > 0:
                st.warning(
                    "**Flagged orders**: Insufficient on-hand inventory and no inbound arrives before consumption. "
                    "Adjust demand volume or accept a lower fill % for these orders."
                )
    else:
        st.info(
            "To enable inventory validation, add these files to your data directory:\n"
            "- **bom_by_sku.csv** — Bill of Materials (SKU → material, qty per unit)\n"
            "- **on_hand_inventory.csv** — On-hand quantities by material\n"
            "- **inbound_inventory.csv** — Inbound shipments with arrival time (optional)\n\n"
            "Copy templates from `data/templates/` and fill with your data. See `data/templates/INVENTORY_TEMPLATES_README.md`."
        )


if __name__ == "__main__":
    import os
    data_dir = Path(os.environ.get("FLOWSTATE_DATA_DIR", str(BASE_DIR.parent / "data")))
    run_streamlit(data_dir)