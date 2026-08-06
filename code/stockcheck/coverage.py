# code/stockcheck/coverage.py — availability toggles + coverage scoring.
#
# available(item) = sum of lot qty where (depot, status) toggle is on.
# Default: Ava=True everywhere; Loc/Out=False (QC-held stock must be opted in).

from __future__ import annotations

import math

import pandas as pd

STATUSES = ("Ava", "Loc", "Out")
RM_DEPOTS = ("M01", "SB1", "SC1", "SF1", "M02")
PKG_DEPOTS = ("SFG", "M21")

OK_MARGIN = 1.10      # coverage >= 1.10 -> OK
TIGHT_FLOOR = 0.95    # 0.95..1.10 -> TIGHT; below -> AT_RISK
DNS_RATIO = 0.90      # demand view: achievable < 90% of target -> DO_NOT_SCHEDULE


def default_toggles() -> dict[str, bool]:
    """Keys 'depot|status'; default Ava-only."""
    t = {}
    for dep in RM_DEPOTS + PKG_DEPOTS:
        for st in STATUSES:
            t[f"{dep}|{st}"] = (st == "Ava")
    return t


def available_stock(rm: pd.DataFrame, pkg: pd.DataFrame,
                    toggles: dict[str, bool] | None = None
                    ) -> dict[str, float]:
    """Per-item available qty across both stock frames, honoring toggles."""
    toggles = toggles or default_toggles()
    avail: dict[str, float] = {}
    for df in (rm, pkg):
        if df is None or df.empty:
            continue
        on = df[df.apply(lambda r: toggles.get(f"{r['depot']}|{r['status']}",
                                               False), axis=1)]
        for item, q in on.groupby("item")["qty"].sum().items():
            avail[item] = avail.get(item, 0.0) + float(q)
    return avail


def lot_detail(rm: pd.DataFrame, pkg: pd.DataFrame, item: str
               ) -> pd.DataFrame:
    """Lot-level rows for one item across both frames."""
    parts = []
    for df in (rm, pkg):
        if df is not None and not df.empty:
            sub = df[df["item"] == item]
            if not sub.empty:
                parts.append(sub)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def item_status(ratio: float) -> str:
    if math.isinf(ratio):
        return "OK"
    if ratio >= OK_MARGIN:
        return "OK"
    if ratio >= TIGHT_FLOOR:
        return "TIGHT"
    return "AT_RISK"


def coverage_for_requirement(group, avail: dict[str, float]) -> dict:
    """Coverage of one RequirementGroup: primary + alternates pooled."""
    pool = avail.get(group.primary_item, 0.0)
    alt_rows = []
    for a in group.alternates:
        aq = avail.get(a["item"], 0.0)
        pool += aq
        alt_rows.append({"item": a["item"], "designation": a["designation"],
                         "available": aq})
    ratio = (pool / group.need_qty) if group.need_qty > 0 else math.inf
    return {
        "item": group.primary_item,
        "designation": group.designation,
        "need": group.need_qty,
        "unit": group.unit,
        "available_primary": avail.get(group.primary_item, 0.0),
        "alternates": alt_rows,
        "available_total": pool,
        "ratio": ratio,
        "status": item_status(ratio),
    }


_WORST = {"AT_RISK": 0, "TIGHT": 1, "OK": 2}


def worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "OK"
    return min(statuses, key=lambda s: _WORST.get(s, 2))
