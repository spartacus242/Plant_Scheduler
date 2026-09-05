# code/stockcheck/receiving_import.py — inbound appointment feed from the
# weekly Shipping/Receiving schedule xlsm.
#
# Layout (verified against the real file): one tab per week (W1..W52, some
# hidden/junk tabs). Days are 3 blocks of 9 columns starting at A/J/S
# (offsets 0/9/18). A date header row precedes each group of day columns.
# INBOUND rows have Dest == 'RECEIVING'. Fields: Chrono col holds the PO#,
# SKU/Type holds a category label (CAPS/GPI/AMCOR/CHEP/'SL3 transefer'),
# Plan APT holds a messy time ('9AM', '11am', '1PM').
#
# The file is decaying (#REF!, text dates, typos) — parsing is best-effort,
# row errors are collected, never fatal.

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from .po_import import po8

DAY_OFFSETS = (0, 9, 18)  # 0-indexed column of each day's 'Dest'
_RECEIVING = "RECEIVING"
_ERR_MARKERS = {"#REF!", "#VALUE!", "#N/A", "#NAME?"}


def _norm_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(v.strip(), fmt).date()
            except ValueError:
                continue
    return None


def _norm_time(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        # A date-only Plan APT cell arrives as midnight. '00:00' would read as
        # a real slot (ready 02:00 after the appt offset); blank sends the
        # line to the ERP ready-hour rule instead. Real times are kept.
        if v.hour == 0 and v.minute == 0:
            return ""
        return v.strftime("%H:%M")
    s = str(v).strip().upper().replace(".", "")
    m = re.match(r"^(\d{1,2})\s*(AM|PM)$", s)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(2) == "PM" else 0)
        return f"{h:02d}:00"
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return s


def parse_receiving_schedule(path: str | Path, week_tab: str | None = None,
                             ) -> tuple[list[dict], list[dict]]:
    """Return (appointments, errors). Each appointment: {week_tab, date,
    time, po, po8, category, carrier, row}."""
    import openpyxl
    import warnings
    warnings.filterwarnings("ignore")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    appts: list[dict] = []
    errors: list[dict] = []

    tabs = [week_tab] if week_tab else [
        s for s in wb.sheetnames if re.fullmatch(r"W\d{1,2}", s.strip())]
    for tab in tabs:
        if tab not in wb.sheetnames:
            errors.append({"tab": tab, "error": "tab not found"})
            continue
        ws = wb[tab]
        current_dates: dict[int, date | None] = {o: None for o in DAY_OFFSETS}
        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            cells = list(row) + [None] * (28 - len(row))
            # date header row? capture dates sitting at day offsets
            for off in DAY_OFFSETS:
                d = _norm_date(cells[off])
                if d is not None:
                    current_dates[off] = d
            for off in DAY_OFFSETS:
                dest = cells[off]
                if dest is None:
                    continue
                d_s = str(dest).strip()
                if d_s in _ERR_MARKERS:
                    continue
                if d_s.upper() != _RECEIVING:
                    continue
                po = cells[off + 1]           # Chrono column holds PO#
                cat = cells[off + 2]          # SKU/Type category
                carrier = cells[off + 4]      # Carrier/Sup
                apt = cells[off + 5]          # Plan APT time
                po_s = "" if po is None else str(po).strip()
                if po_s in _ERR_MARKERS:
                    po_s = ""
                if not po_s and not cat:
                    continue
                appts.append({
                    "week_tab": tab,
                    "date": (current_dates[off].isoformat()
                             if current_dates[off] else None),
                    "time": _norm_time(apt),
                    "po": po_s,
                    # join key to PO lines; a multi-PO cell keeps its first
                    "po8": po8(po_s),
                    "category": re.sub(r"\s+", " ", str(cat).strip()) if cat else "",
                    "carrier": str(carrier).strip() if carrier else "",
                    "row": r_idx,
                })
    return appts, errors
