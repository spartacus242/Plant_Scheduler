# code/stockcheck/coverage.py — availability toggles + coverage scoring.
#
# available(item) = sum of lot qty where (depot, status) toggle is on.
# Default: Ava=True everywhere except the quality-control depots; Loc/Out/OL
# =False (QC-held stock must be opted in).
#
# Depot groups (drop of 2026-09-15): the ERP now exports six lot files —
# raw materials (jestkexp), packaging (jestkexp2), the Americold Burley
# off-site ambient store (jestkamb, depot AMB), the quality-control depots
# (jestkexq, QCR/QCP), fresh apples (jestksav, SA1/SA2) and semi-finished /
# WIP lots (jestkexp5, M12, batch status "OL" besides AVA). Each group is a
# named constant so the toggle UI can label it and default_toggles can
# treat QC differently; RM_DEPOTS / PKG_DEPOTS are unchanged so the WW33
# goldens still count exactly the same rows.
#
# (2026-09-16) The report never hands the semi-finished export (jestkexp5)
# to available_stock: a house-made intermediate never gates a SKU on its own
# stock (user rule of 2026-08-14) — stockcheck.api._NEVER_COUNTED_FRAMES.
# The functions here stay frame-agnostic; the M12 toggles only matter for
# M12 rows some other export might carry.

from __future__ import annotations

import math

import pandas as pd

STATUSES = ("Ava", "Loc", "Out", "OL")
RM_DEPOTS = ("M01", "SB1", "SC1", "SF1", "M02")
PKG_DEPOTS = ("SFG", "M21")
AMB_DEPOTS = ("AMB",)            # off-site ambient store (Americold Burley)
APPLE_DEPOTS = ("SA1", "SA2")    # fresh-apple receiving depots
SEMI_DEPOTS = ("M12",)           # semi-finished / work-in-progress lots
QC_DEPOTS = ("QCR", "QCP")       # quality control: nothing counts until released

# (group label, depots) in UI order — the Stock Check toggle grid renders one
# labelled row per group; every depot default_toggles knows is in here.
DEPOT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Raw materials", RM_DEPOTS),
    ("Packaging", PKG_DEPOTS),
    ("Off-site AMB", AMB_DEPOTS),
    ("Apples SA1-SA2", APPLE_DEPOTS),
    ("Semi-finished M12", SEMI_DEPOTS),
    ("Quality control QCR-QCP", QC_DEPOTS),
)
ALL_DEPOTS: tuple[str, ...] = tuple(d for _, deps in DEPOT_GROUPS for d in deps)

# In-house produced ingredients: made on the preprocessing lines on demand,
# never carried in the VIF inventory exports (per user). They must never gate
# a SKU — always treated as fully available. Extensible if more appear.
IN_HOUSE_ITEMS = {"BT001", "BT002"}

OK_MARGIN = 1.10      # coverage >= 1.10 -> OK
TIGHT_FLOOR = 0.95    # 0.95..1.10 -> TIGHT; below -> AT_RISK
DNS_RATIO = 0.90      # demand view: achievable < 90% of target -> DO_NOT_SCHEDULE


def default_toggles() -> dict[str, bool]:
    """Keys 'depot|status' for every depot in DEPOT_GROUPS.

    Ava is on for the plant, off-site, apple and semi-finished depots; the
    quality-control depots QCR/QCP are OFF for every status (a lot sitting
    in QC is not released, whatever the ERP status says); Loc / Out / OL
    are off everywhere (decision of 2026-09-15)."""
    t = {}
    for dep in ALL_DEPOTS:
        for st in STATUSES:
            t[f"{dep}|{st}"] = (st == "Ava" and dep not in QC_DEPOTS)
    return t


def _effective_toggles(toggles: dict[str, bool] | None) -> dict[str, bool]:
    """The caller's toggles over the defaults: a settings.json saved before
    the 2026-09-15 drop only knows the seven old depots, and treating its
    missing AMB/SA1/SA2/M12 keys as "off" would silently zero the off-site
    and apple stock for every planner with saved settings. A depot|status
    key outside DEPOT_GROUPS is still excluded unless the caller sets it."""
    if not toggles:
        return default_toggles()
    return {**default_toggles(), **toggles}


def available_stock(rm: pd.DataFrame, pkg: pd.DataFrame,
                    toggles: dict[str, bool] | None = None,
                    extra=()) -> dict[str, float]:
    """Per-item available qty across the stock frames, honoring toggles.

    rm / pkg keep their positional slots (the WW33 callers); `extra` is any
    further lot frames (AMB, QC, apples, semi-finished — the drop of
    2026-09-15), summed the same way. A row whose depot|status key is not
    toggled on (unknown keys included) is excluded."""
    toggles = _effective_toggles(toggles)
    avail: dict[str, float] = {}
    for df in (rm, pkg, *(extra or ())):
        if df is None or df.empty:
            continue
        on = df[df.apply(lambda r: toggles.get(f"{r['depot']}|{r['status']}",
                                               False), axis=1)]
        for item, q in on.groupby("item")["qty"].sum().items():
            avail[item] = avail.get(item, 0.0) + float(q)
    return avail


def lot_detail(rm: pd.DataFrame, pkg: pd.DataFrame, item: str,
               extra=()) -> pd.DataFrame:
    """Lot-level rows for one item across every frame (rm, pkg, extra)."""
    parts = []
    for df in (rm, pkg, *(extra or ())):
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


def coverage_for_requirement(group, avail: dict[str, float],
                             tracked_items: set[str] | None = None) -> dict:
    """Coverage of one RequirementGroup: primary + alternates pooled.
    - In-house items (IN_HOUSE_ITEMS) are always fully available.
    - Items absent from every stock frame are NOT_TRACKED (unknown supply),
      which must not poison the SKU status — they render as their own chip.
    - A semi-finished primary whose recipe is missing (group.no_recipe,
      BomGraph.explode, 2026-09-16) is NOT_TRACKED whatever its alternates
      hold: an intermediate never gates a SKU (rule of 2026-08-14)."""
    in_house = (group.primary_item in IN_HOUSE_ITEMS
                or any(a["item"] in IN_HOUSE_ITEMS for a in group.alternates))
    pool = avail.get(group.primary_item, 0.0)
    alt_rows = []
    for a in group.alternates:
        aq = avail.get(a["item"], 0.0)
        pool += aq
        alt_rows.append({"item": a["item"], "designation": a["designation"],
                         "available": aq})
    any_tracked = True
    if tracked_items is not None:
        any_tracked = (group.primary_item in tracked_items
                       or any(a["item"] in tracked_items
                              for a in group.alternates))
    ratio = (pool / group.need_qty) if group.need_qty > 0 else math.inf
    if in_house:
        status = "OK"             # made in-house: never gates
        ratio = math.inf
        note = "in-house"
    elif getattr(group, "no_recipe", False):
        status = "NOT_TRACKED"
        ratio = math.inf
        note = "semi-finished, recipe missing (ediact 4.csv)"
    elif not any_tracked:
        status = "NOT_TRACKED"
        ratio = math.inf
        note = "not in VIF stock exports"
    else:
        status = item_status(ratio)
        note = ""
    return {
        "item": group.primary_item,
        "designation": group.designation,
        "need": group.need_qty,
        "unit": group.unit,
        "available_primary": avail.get(group.primary_item, 0.0),
        "alternates": alt_rows,
        "available_total": pool,
        "ratio": ratio,
        "status": status,
        "note": note,
    }


_WORST = {"NOT_TRACKED": -1, "AT_RISK": 0, "TIGHT": 1, "OK": 2}


def worst_status(statuses: list[str]) -> str:
    """NOT_TRACKED never dominates: a SKU is only AT_RISK/TIGHT on items we
    actually count. NOT_TRACKED surfaces separately as a data-quality chip."""
    tracked = [s for s in statuses if s != "NOT_TRACKED"]
    if not tracked:
        return "NOT_TRACKED" if statuses else "OK"
    return min(tracked, key=lambda s: _WORST.get(s, 2))
