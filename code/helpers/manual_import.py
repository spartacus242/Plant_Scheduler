# helpers/manual_import.py -- Import the production planner's own line schedule.
#
# AZAP is the customer / corporate DEMAND PLAN (SKU + kg + week). It never says
# which line runs what, or when. The plant's production planner builds the real
# line schedule himself, usually in Excel. This module brings that file in and
# converts it to the unified calendar_blocks shape.
#
# Two layouts are accepted:
#   (a) native  -- the calendar_blocks columns themselves (start_h / end_h hours)
#   (b) planner -- line + sku + start datetime + end datetime, converted to hour
#                  offsets against [scheduler] planning_start_date via timefmt.
#
# Nothing here writes to disk: parse_manual_schedule returns a result object and
# the caller decides what to do with it. A file with ANY row error is rejected
# whole, so a partial / corrupt calendar_blocks.csv can never be produced.

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from helpers.calendar_io import BLOCK_TYPES, CALENDAR_COLUMNS, empty_calendar
from helpers.timefmt import datetime_to_hour

CSV_ENCODING = "utf-8-sig"

MAX_ERRORS_SHOWN = 50

# Friendlier planner-style header names -> canonical calendar_blocks names.
# Keys are normalized (lowercase, non-alphanumerics collapsed to "_").
_ALIASES: dict[str, str] = {
    # line
    "line": "line_name",
    "line_name": "line_name",
    "linename": "line_name",
    "machine": "line_name",
    "asset": "line_name",
    "resource": "line_name",
    "line_id": "line_id",
    "lineid": "line_id",
    # sku / product
    "sku": "sku",
    "item": "sku",
    "product": "sku",
    "material": "sku",
    "sku_description": "sku_description",
    "description": "sku_description",
    "product_description": "sku_description",
    # order
    "order": "order_id",
    "order_id": "order_id",
    "orderid": "order_id",
    "order_no": "order_id",
    "order_number": "order_id",
    # quantity
    "qty_kg": "qty_kg",
    "qty": "qty_kg",
    "quantity": "qty_kg",
    "kg": "qty_kg",
    "kgs": "qty_kg",
    "quantity_kg": "qty_kg",
    # raw hour offsets
    "start_h": "start_h",
    "starth": "start_h",
    "start_hour": "start_h",
    "end_h": "end_h",
    "endh": "end_h",
    "end_hour": "end_h",
    # datetimes
    "start": "start_dt",
    "start_dt": "start_dt",
    "start_time": "start_dt",
    "start_date": "start_dt",
    "start_datetime": "start_dt",
    "starts": "start_dt",
    "from": "start_dt",
    "end": "end_dt",
    "end_dt": "end_dt",
    "end_time": "end_dt",
    "end_date": "end_dt",
    "end_datetime": "end_dt",
    "finish": "end_dt",
    "finish_datetime": "end_dt",
    "to": "end_dt",
    # misc passthrough
    "block_id": "block_id",
    "block_type": "block_type",
    "type": "block_type",
    "activity": "block_type",
    "label": "label",
    "locked": "locked",
    "attrs": "attrs",
    "notes": "attrs",
}

# Free-text block_type synonyms a planner might type.
_TYPE_SYNONYMS = {
    "production": "production",
    "prod": "production",
    "run": "production",
    "make": "production",
    "sku": "production",
    "cip": "cip",
    "clean": "cip",
    "cleaning": "cip",
    "wash": "cip",
    "maintenance": "maintenance",
    "maint": "maintenance",
    "pm": "maintenance",
    "trial": "trial",
    "trials": "trial",
    "contractor": "contractor",
    "line_down": "line_down",
    "linedown": "line_down",
    "down": "line_down",
    "downtime": "line_down",
    "idle": "line_down",
}

_TRUE_WORDS = {"1", "true", "yes", "y", "t", "locked"}


@dataclass
class ImportResult:
    """Outcome of parsing a planner's manual schedule file."""

    calendar: pd.DataFrame = field(default_factory=empty_calendar)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    layout: str = "unknown"
    rows_in: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.calendar.empty


def _norm(name: Any) -> str:
    text = str(name or "").strip().lstrip("\ufeff").lower()
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() else "_")
    return "_".join(p for p in "".join(out).split("_") if p)


def read_tabular(raw: bytes, filename: str = "") -> pd.DataFrame:
    """Read CSV (utf-8-sig, BOM-tolerant) or XLSX bytes into a string frame."""
    name = str(filename or "").lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(io.BytesIO(raw), dtype=str).fillna("")
    try:
        return pd.read_csv(
            io.BytesIO(raw), encoding=CSV_ENCODING, dtype=str, keep_default_na=False
        )
    except UnicodeDecodeError:
        return pd.read_csv(
            io.BytesIO(raw), encoding="latin-1", dtype=str, keep_default_na=False
        )


def _rename_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Map incoming headers to canonical names. Unknown columns are dropped."""
    mapping: dict[Any, str] = {}
    dropped: list[str] = []
    seen: set[str] = set()
    for col in df.columns:
        canon = _ALIASES.get(_norm(col))
        if canon and canon not in seen:
            mapping[col] = canon
            seen.add(canon)
        else:
            dropped.append(str(col))
    out = df.rename(columns=mapping)
    keep = [c for c in out.columns if c in set(mapping.values())]
    return out[keep], dropped


def _cell(row: Any, key: str) -> str:
    if key not in row:
        return ""
    val = row[key]
    if val is None:
        return ""
    text = str(val).strip()
    return "" if text.lower() in ("nan", "nat", "none") else text


def _num(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.replace(",", "").replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _new_id(prefix: str = "man") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def load_line_index(lines_path: Path) -> tuple[dict[str, tuple[int, str]], dict[int, str]]:
    """Return (by normalized line_name -> (id, canonical name), by id -> name)."""
    by_name: dict[str, tuple[int, str]] = {}
    by_id: dict[int, str] = {}
    try:
        if not Path(lines_path).exists():
            return by_name, by_id
        df = pd.read_csv(lines_path, encoding=CSV_ENCODING, dtype=str, keep_default_na=False)
    except Exception:
        return by_name, by_id
    for _, r in df.iterrows():
        name = str(r.get("line_name", "")).strip()
        raw_id = str(r.get("line_id", "")).strip()
        try:
            lid = int(float(raw_id))
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        by_name[_norm(name)] = (lid, name)
        by_id[lid] = name
    return by_name, by_id


def parse_manual_schedule(
    raw: bytes,
    filename: str,
    lines_path: Path,
    planning_anchor: Any = None,
    *,
    horizon_hours: int | None = None,
) -> ImportResult:
    """Parse the planner's manual line schedule into the calendar_blocks shape.

    Never raises: every problem comes back in ImportResult.errors so the UI can
    show a per-row list and refuse the file.
    """
    res = ImportResult()

    try:
        df = read_tabular(raw, filename)
    except Exception as exc:
        res.errors.append(f"Could not read the file: {type(exc).__name__}: {exc}")
        return res

    if df is None or df.empty:
        res.errors.append("The file has no data rows.")
        return res

    res.rows_in = int(len(df))
    df, dropped = _rename_columns(df)
    if dropped:
        res.warnings.append("Ignored unrecognised column(s): " + ", ".join(dropped[:12]))

    cols = set(df.columns)
    has_hours = {"start_h", "end_h"} <= cols
    has_dts = {"start_dt", "end_dt"} <= cols
    if has_hours:
        res.layout = "native (start_h / end_h)"
    elif has_dts:
        res.layout = "planner (start / end datetime)"
    else:
        res.errors.append(
            "Missing required time columns. Provide either 'start_h' + 'end_h' "
            "(hour offsets) or 'start' + 'end' (dates/times)."
        )
    if "line_name" not in cols and "line_id" not in cols:
        res.errors.append("Missing required column: 'line' (or 'line_name' / 'line_id').")
    if res.errors:
        return res

    by_name, by_id = load_line_index(lines_path)
    if not by_name:
        res.warnings.append(
            f"Could not read known lines from {Path(lines_path).name}; line names were not validated."
        )

    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for idx, r in df.iterrows():
        rn = int(idx) + 2  # +1 for 0-index, +1 for the header row

        # -- line ------------------------------------------------------
        line_name = _cell(r, "line_name")
        line_id: int | None = None
        raw_lid = _cell(r, "line_id")
        if raw_lid:
            try:
                line_id = int(float(raw_lid))
            except ValueError:
                errors.append(f"Row {rn}: line_id '{raw_lid}' is not a number.")

        if line_name and by_name:
            hit = by_name.get(_norm(line_name))
            if hit is None:
                errors.append(
                    f"Row {rn}: line '{line_name}' is not in lines.csv "
                    f"(known: {', '.join(sorted(by_id.values())[:8])}...)."
                )
            else:
                line_id, line_name = hit
        elif not line_name and line_id is not None:
            line_name = by_id.get(line_id, "")
            if by_id and not line_name:
                errors.append(f"Row {rn}: line_id {line_id} is not in lines.csv.")
        elif not line_name and line_id is None:
            errors.append(f"Row {rn}: no line given.")

        # -- times -----------------------------------------------------
        start_h: float | None = None
        end_h: float | None = None
        if has_hours:
            s_txt, e_txt = _cell(r, "start_h"), _cell(r, "end_h")
            start_h, end_h = _num(s_txt), _num(e_txt)
            if start_h is None:
                errors.append(f"Row {rn}: start_h '{s_txt}' is not a number.")
            if end_h is None:
                errors.append(f"Row {rn}: end_h '{e_txt}' is not a number.")
        else:
            s_txt, e_txt = _cell(r, "start_dt"), _cell(r, "end_dt")
            start_h = datetime_to_hour(s_txt, planning_anchor)
            end_h = datetime_to_hour(e_txt, planning_anchor)
            if start_h is None:
                errors.append(f"Row {rn}: could not read start date/time '{s_txt}'.")
            if end_h is None:
                errors.append(f"Row {rn}: could not read end date/time '{e_txt}'.")

        if start_h is not None and end_h is not None and end_h <= start_h:
            errors.append(f"Row {rn}: end ({end_h:g}) must be after start ({start_h:g}).")
        if start_h is not None and start_h < 0:
            errors.append(
                f"Row {rn}: start is before the planning anchor (hour {start_h:g}). "
                "Check [scheduler] planning_start_date in flowstate.toml."
            )
        if horizon_hours and end_h is not None and end_h > float(horizon_hours):
            res.warnings.append(
                f"Row {rn}: ends at hour {end_h:g}, past the {horizon_hours}h horizon."
            )

        # -- type / labels ---------------------------------------------
        btype_raw = _cell(r, "block_type")
        btype = _TYPE_SYNONYMS.get(_norm(btype_raw), "") if btype_raw else ""
        if btype_raw and not btype:
            errors.append(
                f"Row {rn}: block_type '{btype_raw}' is not one of {', '.join(BLOCK_TYPES)}."
            )
        if not btype:
            btype = "production"

        sku = _cell(r, "sku")
        if btype == "production" and not sku:
            errors.append(f"Row {rn}: production block has no SKU.")

        qty_txt = _cell(r, "qty_kg")
        qty = _num(qty_txt)
        if qty_txt and qty is None:
            errors.append(f"Row {rn}: qty '{qty_txt}' is not a number.")

        if len(errors) > MAX_ERRORS_SHOWN:
            errors.append("... more errors suppressed; fix the ones above first.")
            break

        rows.append({
            "block_id": _cell(r, "block_id") or _new_id(btype[:4]),
            "block_type": btype,
            "line_id": int(line_id) if line_id is not None else 0,
            "line_name": line_name,
            "start_h": float(start_h) if start_h is not None else 0.0,
            "end_h": float(end_h) if end_h is not None else 0.0,
            "label": _cell(r, "label") or sku or btype,
            "order_id": _cell(r, "order_id"),
            "sku": sku,
            "sku_description": _cell(r, "sku_description"),
            "qty_kg": qty,
            "locked": _cell(r, "locked").lower() in _TRUE_WORDS,
            "attrs": _cell(r, "attrs"),
        })

    if errors:
        res.errors = errors
        return res

    if not rows:
        res.errors.append("No usable rows found in the file.")
        return res

    cal = pd.DataFrame(rows)
    for col in CALENDAR_COLUMNS:
        if col not in cal.columns:
            cal[col] = None
    res.calendar = cal[CALENDAR_COLUMNS]

    dupes = int(res.calendar["block_id"].duplicated().sum())
    if dupes:
        res.warnings.append(f"{dupes} duplicate block_id value(s) were kept as-is.")
    res.warnings.extend(_overlap_warnings(res.calendar))
    return res


def _overlap_warnings(cal: pd.DataFrame) -> list[str]:
    """Non-fatal notice when two blocks share a line and overlap in time."""
    out: list[str] = []
    for line, grp in cal.groupby("line_name"):
        g = grp.sort_values("start_h")
        prev_end: float | None = None
        prev_label = ""
        clashes = 0
        for _, r in g.iterrows():
            s, e = float(r["start_h"]), float(r["end_h"])
            if prev_end is not None and s < prev_end - 1e-9:
                clashes += 1
                if clashes == 1:
                    out.append(
                        f"Line {line}: '{r['label']}' overlaps '{prev_label}' "
                        f"(starts {s:g}, previous ends {prev_end:g})."
                    )
            if prev_end is None or e > prev_end:
                prev_end = e
                prev_label = str(r["label"])
        if clashes > 1:
            out.append(f"Line {line}: {clashes} overlapping block(s) in total.")
    return out


def template_csv() -> str:
    """A tiny example of the planner-style layout, for a download button."""
    return (
        "line,sku,start,end,qty_kg,block_type\r\n"
        "P09,120430,2026-02-15 06:00,2026-02-15 22:00,18000,production\r\n"
        "P09,,2026-02-15 22:00,2026-02-16 04:00,,cip\r\n"
        "P10,120431,2026-02-16 00:00,2026-02-16 18:00,16000,production\r\n"
    )
