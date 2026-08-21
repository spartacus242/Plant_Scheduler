# helpers/overnight_ui.py — the "Overnight results" section on Generate.
#
# Lives here (not inline in pages/generate.py) because that page is edited
# constantly — the insertion surface there stays two lines. Renders nothing
# at all when no overnight generation exists: the section must not announce
# a feature the batch hasn't produced yet.

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from helpers import overnight_results as onr


def _leaderboard_frame(board: dict) -> pd.DataFrame:
    # Δ is same-generation by construction: every candidate in this board
    # against THIS board's own baseline (never another generation's)
    base = onr.board_baseline_composite(board)
    rows = []
    for c in sorted(board["candidates"],
                    key=lambda c: c["overnight_score"]["composite"],
                    reverse=True):
        sc = c["overnight_score"]
        gap = c.get("gap_pct_end")
        wall = c.get("wall_s")
        rows.append({
            "Arm": c["run_id"],
            "Published": onr.published_label(c),
            "Composite": round(float(sc["composite"]), 1),
            "Δ vs board": (round(float(sc["composite"]) - base, 2)
                           if base is not None else None),
            "Fill": sc.get("fill"),
            "Changeovers": sc.get("changeovers"),
            "Campaign": sc.get("campaign"),
            "On-time": sc.get("on_time"),
            "Budgets": onr.budgets_text(c),
            "Guards": onr.guards_text(c),
            "Gap %": round(float(gap), 1) if gap is not None else None,
            "Wall": f"{float(wall) / 60:.0f}m" if wall is not None else "—",
            "Version": c.get("version_slug") or "",
        })
    return pd.DataFrame(rows)


def render_overnight_results(dd: Path) -> None:
    """Leaderboard + noise floor + baseline delta; silent when absent."""
    ov = onr.summarize(dd)
    if ov.state == onr.NOT_SET:
        return
    st.divider()
    st.subheader("Overnight results")
    when = f"{ov.created:%a %Y-%m-%d %H:%M}" if ov.created else "unknown time"
    stale = (" — **stale**: no fresh batch has landed since"
             if ov.state == onr.STALE else "")
    st.caption(f"Generation `{ov.generation}` · {ov.n_runs} runs · finished "
               f"{when}{stale}")

    if ov.board_composite is not None and ov.delta_vs_board is not None:
        line = (f"Board baseline {ov.board_composite:.1f} → best overnight "
                f"**{ov.best_composite:.1f}** ({ov.delta_vs_board:+.1f}, "
                "same generation).")
        if (ov.published_best
                and ov.published_best.get("generation") != ov.generation):
            line += (" Published best came from generation "
                     f"`{ov.published_best['generation']}` — its delta is "
                     "against THAT generation's board.")
        st.caption(line)
    if ov.noise_spread is not None:
        runs_txt = (f"{ov.noise_runs} repeat runs of the champion"
                    if ov.noise_runs else "repeat champion runs")
        st.caption(
            f"Noise floor: {runs_txt} spread ±{ov.noise_spread:.1f} composite "
            "points — treat smaller deltas between arms as noise.")

    st.dataframe(_leaderboard_frame(ov.board),
                 use_container_width=True, hide_index=True)
    if onr.all_within_noise(ov.board, ov.noise_spread):
        st.caption(f"All candidates within the ±{ov.noise_spread:.2f} noise "
                   "floor of the board — treat the ranking as a tie.")

    for c in ov.published:
        slug = c.get("version_slug")
        if slug:
            st.caption(f"Published {onr.published_label(c)}: `{c['run_id']}` "
                       f"→ version `{slug}` — inspect it side-by-side in "
                       "Compare & Promote.")
    if any(c.get("version_slug") for c in ov.published):
        st.page_link("pages/compare.py",
                     label="Compare & Promote the published versions",
                     icon=":material/compare:")
