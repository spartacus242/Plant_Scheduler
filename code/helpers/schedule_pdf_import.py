# code/helpers/schedule_pdf_import.py — Parse the plant's weekly production
# schedule PDF (the VIF "Recall of the selection" print) into a Flowstate
# calendar.
#
# Verified layout (WW33 2026): token stream per block, in order:
#   Start date  MM/DD            (e.g. 08/10)
#   Start time  HH:MM            (e.g. 00:01)
#   Line        LMH-Pxx          (carried from prior row when absent)
#   MO No.      5-digit          (e.g. 29901)
#   Item        SKU | CIP | TRIALS | MISCON
#   Designation free text, 1-3 lines, may end with a dangling hyphen fragment
#   Pal Type    3-digit          (e.g. 640)  + optional brand text (GGS/KIRKLAND)
#   Hours       decimal          (e.g. 20.06)   <- block duration
#   Fct qty (Cas)                (e.g. 3,800)
#   Qty made (Cas)               (often blank)
#   Fct qty [Kg]                 (e.g. 15,048.000) <- production kg
#   Qty made [Kg]  + 'Kg' markers ...
#
# We key on (date, time, MO, item, hours, fct-kg) — robust to the free-text
# designation wrapping. Lines persist downward until a new LMH-Pxx appears.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

RE_LINE = re.compile(r"^L[MR]H-P\d{2}$")
RE_DATE = re.compile(r"^\d{2}/\d{2}$")
RE_TIME = re.compile(r"^\d{2}:\d{2}$")
RE_MO = re.compile(r"^\d{5}$")
RE_NUM = re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^\d+\.\d+$")
RE_PAL = re.compile(r"^\d{3}$")
RE_SPECIAL = {"CIP", "TRIALS", "MISCON"}


@dataclass
class PdfBlock:
    line: str
    date: str           # MM/DD
    time: str           # HH:MM
    mo: str
    item: str
    designation: str
    hours: float
    qty_kg: float
    block_type: str     # production | cip | trial | line_down


@dataclass
class PdfParseResult:
    blocks: list[PdfBlock] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    year: int = 0


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_schedule_pdf(path: str, year: int | None = None) -> PdfParseResult:
    import pymupdf
    doc = pymupdf.open(path)
    tokens: list[str] = []
    for page in doc:
        tokens.extend(t.strip() for t in page.get_text().split("\n"))
    tokens = [t for t in tokens if t]
    res = PdfParseResult()

    # year from the selection header  [08/09/2026;08/15/2026]
    m = re.search(r"\[\d{2}/\d{2}/(\d{4});", "\n".join(tokens[:40]))
    res.year = year or (int(m.group(1)) if m else datetime.now().year)

    cur_line = ""
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if RE_LINE.match(tok):
            cur_line = tok[-3:]           # LMH-P09 -> P09
            i += 1
            continue
        # a block starts with date + time
        if RE_DATE.match(tok) and i + 1 < n and RE_TIME.match(tokens[i + 1]):
            date_s, time_s = tok, tokens[i + 1]
            j = i + 2
            # optional explicit line token
            if j < n and RE_LINE.match(tokens[j]):
                cur_line = tokens[j][-3:]
                j += 1
            # MO number
            if j >= n or not RE_MO.match(tokens[j]):
                i += 2
                continue
            mo = tokens[j]
            j += 1
            # item
            if j >= n:
                break
            item = tokens[j]
            j += 1
            # designation: free text until we hit Pal(3-digit) ... Hours(decimal)
            # scan forward for the pattern [pal?] [hours] [qtyCas] ... [qtyKg]
            desig_parts: list[str] = []
            hours = None
            qty_kg = None
            k = j
            # designation runs until a 3-digit pal or a decimal hours token
            while k < n and not (RE_PAL.match(tokens[k]) or
                                 (RE_NUM.match(tokens[k]) and "." in tokens[k])):
                if tokens[k] in ("GGS", "Kg", "CAN") or RE_LINE.match(tokens[k]):
                    break
                desig_parts.append(tokens[k])
                k += 1
                if len(desig_parts) > 4:
                    break
            # skip pal(3-digit) and optional brand text
            if k < n and RE_PAL.match(tokens[k]):
                k += 1
                while k < n and not RE_NUM.match(tokens[k]):
                    k += 1
            # hours (decimal)
            if k < n and RE_NUM.match(tokens[k]):
                hours = _num(tokens[k])
                k += 1
            # quantities: first numeric with 3+ digits (or decimal) after hours
            # is Fct qty (Cas); the decimal with 3 places is Fct qty [Kg]
            qtys: list[float] = []
            while k < n and len(qtys) < 4:
                t = tokens[k]
                if RE_DATE.match(t) or RE_LINE.match(t) or t in RE_SPECIAL:
                    break
                if RE_NUM.match(t):
                    qtys.append(_num(t))
                k += 1
                if t == "Kg":
                    continue
            # heuristics: the largest qty with a decimal is kg; else 2nd number
            if len(qtys) >= 2:
                qty_kg = max(qtys[1], qtys[0]) if qtys[1] >= qtys[0] else qtys[1]
                # Fct qty [Kg] is the bigger decimal (kg > cases always)
                qty_kg = max(qtys[0], qtys[1])
            elif qtys:
                qty_kg = qtys[0]

            if hours is not None:
                btype = ("cip" if item == "CIP"
                         else "trial" if item in ("TRIALS", "MISCON")
                         else "production")
                desig = " ".join(desig_parts).strip()
                desig = re.sub(r"\s+-\s*$", "", desig)  # dangling hyphen wrap
                res.blocks.append(PdfBlock(
                    line=cur_line, date=date_s, time=time_s, mo=mo, item=item,
                    designation=desig, hours=hours,
                    qty_kg=qty_kg or 0.0, block_type=btype))
                i = k
                continue
            i += 2
            continue
        i += 1

    if not res.blocks:
        res.warnings.append("No schedule blocks recognized in the PDF.")
    return res


def to_calendar_rows(res: PdfParseResult, anchor_monday: datetime,
                     ) -> list[dict]:
    """Convert parsed blocks to calendar_blocks.csv rows (hour offsets from
    the anchor). anchor_monday = Monday 00:00 of the schedule week."""
    import hashlib
    rows = []
    for b in res.blocks:
        try:
            dt = datetime.strptime(f"{res.year}-{b.date} {b.time}",
                                   "%Y-%m/%d %H:%M")
        except ValueError:
            res.warnings.append(f"bad date/time on {b.item} {b.date} {b.time}")
            continue
        start_h = (dt - anchor_monday).total_seconds() / 3600.0
        end_h = start_h + b.hours
        bid = "pdf_" + hashlib.md5(
            f"{b.line}{b.mo}{b.item}{b.date}{b.time}".encode()).hexdigest()[:10]
        # line_id via lines.csv lookup; fall back to P09=0 convention
        lid = -1
        if b.line:
            try:
                lid = int(str(b.line)[1:]) - 9
            except (ValueError, IndexError):
                lid = -1
        rows.append({
            "block_id": bid,
            "block_type": b.block_type,
            "line_id": lid,
            "line_name": b.line,
            "start_h": round(start_h, 3),
            "end_h": round(end_h, 3),
            "label": b.item if b.block_type == "production" else b.block_type.upper(),
            "order_id": b.mo,
            "sku": b.item if b.block_type == "production" else "",
            "sku_description": b.designation,
            "qty_kg": round(b.qty_kg, 3),
            "locked": False,
            "attrs": "pdf_import",
        })
    return rows
