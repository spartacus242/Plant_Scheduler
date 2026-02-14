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


PLANNING_ANCHOR = "2026-02-15 00:00:00"
# Week-0 ends at hour 167 (Sat 23:00); Week-1 starts at hour 168 (Sun 00:00)
WEEK1_START_HOUR = 168


def load_schedule(csv_path: Path) -> pd.DataFrame:
    """Load and normalize schedule_phase2.csv."""
    df = pd.read_csv(csv_path)
    df["Start"] = pd.to_datetime(df["start_dt"])
    df["Finish"] = pd.to_datetime(df["end_dt"])
    df["Task"] = df["line_name"].astype(str)
    df["Resource"] = df["sku"].astype(str)
    if "run_hours" not in df.columns:
        df["run_hours"] = (df["Finish"] - df["Start"]).dt.total_seconds() / 3600
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
    prod_cols = ["Start", "Finish", "Task", "Resource", "line_id", "run_hours"]
    if "start_dt" in schedule_df.columns:
        prod_cols.append("start_dt")
    if "end_dt" in schedule_df.columns:
        prod_cols.append("end_dt")
    frames = [schedule_df[[c for c in prod_cols if c in schedule_df.columns]].copy()]
    frames[0]["start_dt"] = frames[0].get("start_dt", frames[0]["Start"].astype(str))
    frames[0]["end_dt"] = frames[0].get("end_dt", frames[0]["Finish"].astype(str))
    frames[0]["label"] = frames[0]["Resource"] + "\n" + frames[0]["run_hours"].astype(int).astype(str) + " h"

    if cip_df is not None and not cip_df.empty:
        cip_df = cip_df.copy()
        cip_df["line_id"] = cip_df.get("line_id", 0)
        cip_df["run_hours"] = (cip_df["Finish"] - cip_df["Start"]).dt.total_seconds() / 3600
        cip_df["start_dt"] = cip_df["Start"].dt.strftime("%Y-%m-%d %H:%M")
        cip_df["end_dt"] = cip_df["Finish"].dt.strftime("%Y-%m-%d %H:%M")
        cip_df["label"] = "CIP"
        frames.append(cip_df[["Start", "Finish", "Task", "Resource", "line_id", "run_hours", "start_dt", "end_dt", "label"]])
    combined = pd.concat(frames, ignore_index=True)

    combined["Task"] = pd.Categorical(combined["Task"], categories=line_order, ordered=True)
    combined = combined.sort_values(["line_id", "Start"])

    skus = [r for r in combined["Resource"].unique().tolist() if r != "CIP"]
    color_discrete_map = dict(
        zip(skus, px.colors.qualitative.Plotly * (1 + len(skus) // len(px.colors.qualitative.Plotly))),
    )
    color_discrete_map["CIP"] = "#888888"

    fig = px.timeline(
        combined,
        x_start="Start",
        x_end="Finish",
        y="Task",
        color="Resource",
        color_discrete_map=color_discrete_map,
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
    )
    fig.update_yaxes(autorange="reversed")

    # Hover: SKU, start datetime, end datetime, run hours
    for trace in fig.data:
        if trace.name == "CIP":
            trace.hovertemplate = "CIP<br>Start: %{base|%Y-%m-%d %H:%M}<br>End: %{x|%Y-%m-%d %H:%M}<br>Duration: 6 h<extra></extra>"
        else:
            trace.hovertemplate = (
                "<b>%{customdata[0]}</b><br>"
                "Start: %{customdata[1]}<br>End: %{customdata[2]}<br>Run: %{customdata[3]} h<extra></extra>"
            )
        # Build customdata per point: [sku, start_dt, end_dt, run_hours]
        mask = combined["Resource"] == trace.name
        sub = combined.loc[mask]
        trace.customdata = list(zip(
            sub["Resource"],
            sub.get("start_dt", sub["Start"].astype(str)),
            sub.get("end_dt", sub["Finish"].astype(str)),
            sub["run_hours"].round(1),
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
    """Run Streamlit UI: pick schedule file, optional CIP, show Gantt; click bar to highlight SKU; changeovers table."""
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

    schedule_path = st.text_input(
        "Schedule CSV path",
        value=str(default_schedule),
        help="Path to schedule_phase2.csv",
    )
    use_cip = st.checkbox("Include CIP windows (grey bars)", value=default_cip.exists())
    cip_path = st.text_input(
        "CIP windows CSV path (optional)",
        value=str(default_cip),
        help="Path to cip_windows.csv (columns: line_id, start_hour, end_hour)",
        disabled=not use_cip,
    ) if use_cip else None

    if not Path(schedule_path).exists():
        st.warning(f"Schedule file not found: {schedule_path}")
        st.info("Run the scheduler first with full data to generate schedule_phase2.csv")
        return

    schedule_df = load_schedule(Path(schedule_path))
    cip_df = load_cip_windows(Path(cip_path)) if use_cip and cip_path else None

    # Click-to-highlight: read selection from plotly chart (stored in session_state[key])
    highlighted_sku = st.session_state.get("gantt_highlighted_sku")

    fig = build_gantt_figure(
        schedule_df,
        cip_df,
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

    # Selection in session_state["gantt_chart"]: apply highlight on point select, clear on click-off (deselect)
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
            # User clicked off chart (deselected) — clear highlight
            st.session_state.pop("gantt_highlighted_sku", None)
            st.rerun()
    if highlighted_sku:
        if st.button("Clear SKU highlight"):
            st.session_state.pop("gantt_highlighted_sku", None)
            st.rerun()

    # Changeover section below the schedule — compact table
    total_co, changeovers_df = compute_changeovers(schedule_df)
    st.caption("Changeovers")
    col_metric, col_table = st.columns([1, 4])
    with col_metric:
        st.metric("Total", total_co)
    with col_table:
        co_display = changeovers_df[["line_name", "changeovers"]].rename(columns={"line_name": "Line", "changeovers": "CO"})
        st.dataframe(co_display, use_container_width=True, height=min(120, 28 * min(5, len(co_display))))

    with st.expander("Schedule summary"):
        st.dataframe(
            schedule_df[["line_name", "order_id", "sku", "start_dt", "end_dt", "run_hours"]].rename(
                columns={"start_dt": "Start", "end_dt": "End"}
            ),
            use_container_width=True,
        )


if __name__ == "__main__":
    import os
    data_dir = Path(os.environ.get("FLOWSTATE_DATA_DIR", str(BASE_DIR.parent / "data")))
    run_streamlit(data_dir)