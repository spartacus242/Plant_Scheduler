# helpers/calendar_io.py — Unified calendar_blocks load/save + AZAP/legacy import.

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

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
    df = pd.read_csv(path)
    for col in CALENDAR_COLUMNS:
        if col not in df.columns:
            df[col] = None
    if "locked" in df.columns:
        df["locked"] = df["locked"].fillna(False).astype(bool)
    else:
        df["locked"] = False
    df["start_h"] = pd.to_numeric(df["start_h"], errors="coerce").fillna(0).astype(float)
    df["end_h"] = pd.to_numeric(df["end_h"], errors="coerce").fillna(0).astype(float)
    return df[CALENDAR_COLUMNS]


def save_calendar(df: pd.DataFrame, path: Path) -> None:
    out = df.copy()
    for col in CALENDAR_COLUMNS:
        if col not in out.columns:
            out[col] = None
    safe_write_csv(out[CALENDAR_COLUMNS], path)


def _new_id(prefix: str = "b") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def import_legacy_schedule(
    schedule_path: Path,
    cip_path: Path | None = None,
    downtimes_path: Path | None = None,
    planning_anchor: str = "2026-02-15 00:00:00",
) -> pd.DataFrame:
    """Convert legacy schedule_phase2 + cip_windows (+ downtimes) → calendar_blocks."""
    rows: list[dict[str, Any]] = []

    if schedule_path.exists():
        sched = pd.read_csv(schedule_path)
        for _, r in sched.iterrows():
            is_trial = bool(r.get("is_trial", False))
            btype = "trial" if is_trial else "production"
            start = float(r.get("start_hour", 0))
            end = float(r.get("end_hour", start))
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
                "qty_kg": None,
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
        block = {
            "id": str(r["block_id"]),
            "line_id": int(r["line_id"]) if pd.notna(r["line_id"]) else 0,
            "line_name": str(r["line_name"]),
            "order_id": str(r.get("order_id") or ""),
            "sku": str(r.get("sku") or r.get("label") or btype),
            "sku_description": str(r.get("sku_description") or ""),
            "start_hour": start,
            "end_hour": end,
            "run_hours": max(0.0, end - start),
            "is_trial": btype == "trial",
            "block_type": _to_gantt_type(btype),
            "label": str(r.get("label") or ""),
            "locked": bool(r.get("locked", False)),
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
        "qty_kg": b.get("qty_kg"),
        "locked": bool(b.get("locked", False)),
        "attrs": str(b.get("attrs") or ""),
    }


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
