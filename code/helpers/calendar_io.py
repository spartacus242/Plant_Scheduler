# helpers/calendar_io.py - Unified calendar_blocks load/save + solver import.

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from helpers.safe_io import safe_write_csv

BLOCK_TYPES = (
    "production",
    "cip",
    "maintenance",
    "trial",
    "contractor",
    "line_down",
)

CALENDAR_COLUMNS = [
    "block_id",
    "block_type",
    "line_id",
    "line_name",
    "start_h",
    "end_h",
    "label",
    "order_id",
    "sku",
    "sku_description",
    "qty_kg",
    "locked",
    "attrs",
]


def empty_calendar() -> pd.DataFrame:
    return pd.DataFrame(columns=CALENDAR_COLUMNS)


def load_calendar(path: Path) -> pd.DataFrame:
    if not path.exists():
        return empty_calendar()
    # Read all columns as strings; numeric columns are coerced explicitly below.
    # Code columns (sku, order_id, label, block_id) get an integer dtype in a
    # CSV with empty cells, producing "280351.0" labels. dtype=str prevents that.
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in CALENDAR_COLUMNS:
        if col not in df.columns:
            df[col] = None
    if "locked" in df.columns:
        df["locked"] = df["locked"].str.lower().isin({"true", "1", "yes"})
    else:
        df["locked"] = False
    df["start_h"] = pd.to_numeric(df["start_h"], errors="coerce").fillna(0).astype(float)
    df["end_h"] = pd.to_numeric(df["end_h"], errors="coerce").fillna(0).astype(float)
    # qty_kg keeps NaN for "unknown kg" - do NOT fillna(0). A 0 here is
    # indistinguishable from "produced nothing", which made the scorecard's
    # Service excess-kg path read unknown production as "no excess" instead of
    # falling back to its estimate. Consumers treat NaN as missing; display
    # sites blank it out (never write 0 back into the data model).
    df["qty_kg"] = pd.to_numeric(df.get("qty_kg", np.nan), errors="coerce").astype(float)
    # Strip trailing ".0" from code columns that were written by a previous
    # pandas save with float dtype (e.g. "570468.0" in sku). dtype=str alone
    # doesn't fix this because the CSV literally stores the string "570468.0".
    for _col in ("sku", "order_id", "label"):
        if _col in df.columns:
            df[_col] = df[_col].str.replace(r"\.0$", "", regex=True)
    return df[CALENDAR_COLUMNS]


def save_calendar(df: pd.DataFrame, path: Path) -> None:
    out = df.copy()
    for col in CALENDAR_COLUMNS:
        if col not in out.columns:
            out[col] = None
    safe_write_csv(out[CALENDAR_COLUMNS], path)


def _new_id(prefix: str = "b") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _opt_kg(v: Any) -> float | None:
    """Optional produced kg: unknown stays None, never 0.0.

    Same rule as load_calendar's NaN: a 0 in qty_kg is indistinguishable from
    "produced nothing" and makes the scorecard read unknown production as "no
    excess". Blank, NaN, unparseable and the Gantt's 0 placeholder (see
    useScheduleState.addToHolding, which defaults qty_kg to 0) all collapse to
    None so the missing-kg path downstream can still fire.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(f) or f == 0.0:
        return None
    return f


def import_solver_schedule(
    schedule_path: Path,
    cip_path: Path | None = None,
    downtimes_path: Path | None = None,
    planning_anchor: str = "2026-02-15 00:00:00",
) -> pd.DataFrame:
    """Convert solver schedule_phase2 + cip_windows (+ downtimes) → calendar_blocks."""
    rows: list[dict[str, Any]] = []

    if schedule_path.exists():
        sched = pd.read_csv(schedule_path)
        for _, r in sched.iterrows():
            is_trial = bool(r.get("is_trial", False))
            btype = "trial" if is_trial else "production"
            start = float(r.get("start_hour", 0))
            end = float(r.get("end_hour", start))
            # Solver schedules carry the produced kg per row (qty_kg). Older
            # schedules have no such column - keep None then, exactly as before.
            qty = r.get("qty_kg")
            qty = None if qty is None or pd.isna(qty) else float(qty)
            # Trials are blocked production hours, never tonnage (user rule
            # 2026-08-14) - a trial's target_kgs sizes its window, nothing else.
            if is_trial:
                qty = None
            rows.append({
                "block_id": _new_id("prod" if not is_trial else "trial"),
                "block_type": btype,
                "line_id": int(r.get("line_id", 0)),
                "line_name": str(r.get("line_name", "")),
                "start_h": start,
                "end_h": end,
                "label": str(r.get("sku", "")),
                "order_id": str(r.get("order_id", "")),
                "sku": str(r.get("sku", "")),
                "sku_description": str(r.get("sku_description", "") or ""),
                "qty_kg": qty,
                "locked": False,
                "attrs": "",
            })

    if cip_path and cip_path.exists():
        cip = pd.read_csv(cip_path)
        for _, r in cip.iterrows():
            start = float(r.get("start_hour", 0))
            end = float(r.get("end_hour", start))
            rows.append({
                "block_id": _new_id("cip"),
                "block_type": "cip",
                "line_id": int(r.get("line_id", 0)),
                "line_name": str(r.get("line_name", "")),
                "start_h": start,
                "end_h": end,
                "label": "CIP",
                "order_id": "",
                "sku": "CIP",
                "sku_description": "",
                "qty_kg": None,
                "locked": False,
                "attrs": "",
            })

    if downtimes_path and downtimes_path.exists():
        dt = pd.read_csv(downtimes_path)
        for _, r in dt.iterrows():
            reason = str(r.get("reason", "Down") or "Down")
            btype = "line_down" if "down" in reason.lower() else "maintenance"
            start = float(r.get("start_hour", 0))
            end = float(r.get("end_hour", start))
            rows.append({
                "block_id": _new_id("down" if btype == "line_down" else "maint"),
                "block_type": btype,
                "line_id": int(r.get("line_id", 0)),
                "line_name": str(r.get("line_name", "")),
                "start_h": start,
                "end_h": end,
                "label": reason,
                "order_id": "",
                "sku": "",
                "sku_description": "",
                "qty_kg": None,
                "locked": False,
                "attrs": reason,
            })

    if not rows:
        return empty_calendar()
    return pd.DataFrame(rows)[CALENDAR_COLUMNS]


def calendar_to_gantt_payload(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Split calendar into schedule-like blocks and window blocks for the React Gantt.

    Production + trial → schedule; cip/maintenance/contractor/line_down → windows.
    """
    schedule: list[dict] = []
    windows: list[dict] = []
    for _, r in df.iterrows():
        btype = str(r["block_type"])
        start = float(r["start_h"])
        end = float(r["end_h"])

        def _txt(v) -> str:
            """Clean optional text: NaN/None/'nan' -> ''."""
            s = str(v or "").strip()
            return "" if s.lower() in ("nan", "none", "nat") else s

        def _code(v) -> str:
            """Item/MO codes: strip a trailing '.0' from float-typed CSV cells."""
            s = _txt(v)
            return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s

        block = {
            "id": str(r["block_id"]),
            "line_id": int(r["line_id"]) if pd.notna(r["line_id"]) else 0,
            "line_name": str(r["line_name"]),
            "order_id": _code(r.get("order_id")),
            "sku": _code(r.get("sku")) or _code(r.get("label")) or btype,
            "sku_description": _txt(r.get("sku_description")),
            "start_hour": start,
            "end_hour": end,
            "run_hours": round(max(0.0, end - start), 1),
            "is_trial": btype == "trial",
            "block_type": _to_gantt_type(btype),
            "label": _code(r.get("label")),
            "locked": bool(r.get("locked", False)),
            # Carried so a drag/drop round trip does not silently drop the
            # produced kg. Unknown kg travels as null, never 0.
            "qty_kg": _opt_kg(r.get("qty_kg")),
            # attrs round-trips so provenance flags (current_state:completed)
            # survive a save; `completed` drives the greyed render.
            "attrs": _txt(r.get("attrs")),
            "completed": "current_state:completed" in _txt(r.get("attrs")),
            # Planner-pinned: fixed for the solver (attrs token 'pinned',
            # toggled in the block popup). Exact-token check — never a
            # substring match, so future tokens like 'unpinned' can't lie.
            "pinned": "pinned" in _txt(r.get("attrs")).split(";"),
        }
        if btype in ("production", "trial"):
            schedule.append(block)
        else:
            windows.append(block)
    return schedule, windows


def _to_gantt_type(btype: str) -> str:
    mapping = {
        "production": "sku",
        "trial": "trial",
        "cip": "cip",
        "maintenance": "maintenance",
        "contractor": "contractor",
        "line_down": "line_down",
    }
    return mapping.get(btype, btype)


def gantt_payload_to_calendar(schedule: list[dict], windows: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for b in schedule:
        gtype = b.get("block_type", "sku")
        btype = "trial" if gtype == "trial" or b.get("is_trial") else "production"
        rows.append(_block_row(b, btype))
    for b in windows:
        gtype = str(b.get("block_type", "cip"))
        btype = {
            "cip": "cip",
            "maintenance": "maintenance",
            "contractor": "contractor",
            "line_down": "line_down",
        }.get(gtype, "cip")
        rows.append(_block_row(b, btype))
    if not rows:
        return empty_calendar()
    return pd.DataFrame(rows)[CALENDAR_COLUMNS]


def _block_row(b: dict, btype: str) -> dict:
    start = float(b.get("start_hour", 0))
    end = float(b.get("end_hour", start))
    # The pin toggle edits the payload BOOL; the CSV stores the attrs token.
    # Reconcile here so a toggle survives the save and an unpin removes it.
    tokens = [t for t in str(b.get("attrs") or "").split(";")
              if t and t != "pinned"]
    if b.get("pinned"):
        tokens.append("pinned")
    return {
        "block_id": str(b.get("id") or _new_id()),
        "block_type": btype,
        "line_id": int(b.get("line_id", 0)),
        "line_name": str(b.get("line_name", "")),
        "start_h": start,
        "end_h": end,
        "label": str(b.get("label") or b.get("sku") or btype),
        "order_id": str(b.get("order_id") or ""),
        "sku": str(b.get("sku") or ""),
        "sku_description": str(b.get("sku_description") or ""),
        "qty_kg": _opt_kg(b.get("qty_kg")),
        "locked": bool(b.get("locked", False)),
        "attrs": ";".join(tokens),
    }


# Changeover-flag bitmask bit order (bit i = column i) — mirrored in the
# Gantt frontend (utils/skuPicker.ts CO_FLAG_BITS); change both or neither.
CO_FLAG_COLUMNS = (
    "ttp_change",
    "ffs_change",
    "topload_change",
    "casepacker_change",
    "conv_to_org_change",
    "cinn_to_non",
)


def build_co_flags(changeovers_csv: Path, demand_skus: set[str]) -> dict[str, int]:
    """{"FROM|TO": bitmask} of changeover-type flags for SKU pairs where both
    sides appear in the demand plan — data for the blank-space SKU picker.

    Setup hours are NOT duplicated here: the Gantt already receives the full
    setup matrix (`changeovers`). Zero-mask pairs are omitted — the client
    reads a missing pair as "no machine touched".
    """
    if not changeovers_csv.exists() or not demand_skus:
        return {}
    df = pd.read_csv(changeovers_csv, dtype={"from_sku": str, "to_sku": str})
    df = df[df["from_sku"].isin(demand_skus) & df["to_sku"].isin(demand_skus)]
    for col in CO_FLAG_COLUMNS:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    out: dict[str, int] = {}
    for r in df.itertuples(index=False):
        mask = 0
        for i, col in enumerate(CO_FLAG_COLUMNS):
            if getattr(r, col):
                mask |= 1 << i
        if mask:
            out[f"{r.from_sku}|{r.to_sku}"] = mask
    return out


def build_line_capable_skus(
    caps_csv: Path, demand_skus: set[str]
) -> dict[str, list[dict]]:
    """{line_name: [{"sku", "rate"}]} of demand-plan SKUs each line can run
    (capable == 1) — candidate list for the blank-space SKU picker."""
    if not caps_csv.exists() or not demand_skus:
        return {}
    df = pd.read_csv(caps_csv, dtype={"sku": str})
    if "calc_rate_kgph" not in df.columns and "rate_kgph" in df.columns:
        df = df.rename(columns={"rate_kgph": "calc_rate_kgph"})
    df["capable"] = pd.to_numeric(df.get("capable", 0), errors="coerce").fillna(0)
    df["calc_rate_kgph"] = pd.to_numeric(
        df.get("calc_rate_kgph", 0), errors="coerce").fillna(0)
    out: dict[str, list[dict]] = {}
    for r in df.itertuples(index=False):
        sku = str(r.sku)
        if int(r.capable) != 1 or sku not in demand_skus:
            continue
        out.setdefault(str(r.line_name), []).append(
            {"sku": sku, "rate": float(r.calc_rate_kgph)})
    return out


def drop_display_overlays(df: pd.DataFrame) -> pd.DataFrame:
    """Remove display-only overlay blocks before a save.

    The Plant Calendar draws cip_info's ScheduledCIP as overlay windows
    (block_id 'cipinfo_<line>') so the planner SEES the plant's cleaning
    plan — but they are a live-feed visualization, not calendar data. A save
    that keeps them writes the overlay into calendar_blocks.csv, where the
    next page load skips re-overlaying (line already has a CIP) and the
    scorecard counts a CIP the planner never placed. Strip them at every
    save boundary.
    """
    if df is None or df.empty or "block_id" not in df.columns:
        return df
    return df[~df["block_id"].astype(str).str.startswith("cipinfo_")]


def load_lines(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame(columns=["line_id", "line_name", "active"])


def ensure_lines_from_calendar(calendar: pd.DataFrame, lines_path: Path) -> pd.DataFrame:
    existing = load_lines(lines_path)
    if not existing.empty:
        return existing
    if calendar.empty:
        return pd.DataFrame(columns=["line_id", "line_name", "active"])
    lines = (
        calendar[["line_id", "line_name"]]
        .drop_duplicates()
        .sort_values("line_id")
        .reset_index(drop=True)
    )
    lines["active"] = True
    safe_write_csv(lines, lines_path)
    return lines
