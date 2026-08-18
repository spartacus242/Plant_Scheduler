# helpers/st_compat.py — workarounds for Streamlit rendering traps.
#
# st.dataframe's glide data grid NEVER mounts when its first render happens
# inside an initially-collapsed st.expander (verified live 2026-08-18,
# Streamlit 1.60: the opened expander shows an empty 400px box with zero
# canvas elements). Two ways out, used across the pages:
#
#   * st.table — a static table renders unconditionally; right for small
#     tables (the home.py "Data status" fix).
#   * deferred_dataframe() below — keeps the interactive grid for BIG tables:
#     the grid is only rendered after a toggle click, and that click happens
#     with the expander already open, so the grid's first render is visible
#     and it mounts normally.

from __future__ import annotations

from typing import Any

import streamlit as st


def deferred_dataframe(data: Any, *, key: str,
                       label: str = "Load table (interactive)",
                       **dataframe_kwargs: Any) -> bool:
    """st.dataframe that survives living inside a collapsed expander.

    Renders a toggle; the dataframe itself is only created once the user
    flips it — by then the surrounding expander is open, so the glide grid
    mounts visible instead of into a hidden (broken) canvas.

    Do NOT use inside an `if st.button(...)` block: the toggle's rerun makes
    the button read False and the whole section vanishes — use st.table there.
    """
    if st.toggle(label, key=key):
        st.dataframe(data, **dataframe_kwargs)
        return True
    return False
