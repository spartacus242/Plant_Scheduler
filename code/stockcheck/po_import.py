# code/stockcheck/po_import.py — open purchase orders from the ERP
# "NPA Open POs" extract (xlsx from IT, or a csv with the same headers).
#
# Layout (verified on the 08-24 sample): header row 1, one line per PO item;
# 'Ordered item' arrives as int; five columns are literally named 'U' — the
# order unit is the first one after 'Qty ordered at the origin'; 'Receipt
# number' non-blank marks a received line; the sheet ends with a sentinel
# row ('?' / empty). Best-effort like receiving_import: errors are collected,
# never raised — a bad file yields empty lines.
#
# Every row is kept (received, unjoinable, garbage qty) so the Inbound tab
# can show it; timeline.gate_receipts decides each line's fate later.

from __future__ import annotations

import csv
import io
import re
import time
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

# normalized header -> PoLine key. Names are matched case/space-insensitively.
_REQUIRED = {"order number": "po", "ordered item": "item",
             "qty ordered at the origin": "qty", "receipt date": "receipt_date"}
_OPTIONAL = {"received item designation": "designation",
             "initial receipt date": "initial_receipt_date",
             "arrival area": "arrival_area",
             "supplier designation": "supplier",   # name; 'Supplier' is the id
             "supplier": "supplier_id",
             "receipt number": "receipt_number",
             "order date": "order_date"}
_UNIT_NAMES = ("u", "unit")
_AS_OF = ("as of", "report date")
_SENTINEL = {"", "?"}
_DATE_FMTS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%y",
              "%d/%m/%Y", "%d.%m.%Y")
_DATE_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}")
_EXCEL_EPOCH = date(1899, 12, 30)
_PO8 = re.compile(r"\d{8,}")
_BATCH = re.compile(r"N(\d)(\d{3})\d{4}")
_HEADER_SCAN = 10   # rows searched for the header (IT may add a title row)


@dataclass
class PoResult:
    lines: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_path: str = ""
    source_mtime: str = ""          # "YYYY-MM-DD HH:MM:SS" local (VIF precedent) or ""
    as_of: str | None = None        # ISO date when the file carries one
    max_receipt_date: str | None = None
    n_rows: int = 0


# ── small value helpers ────────────────────────────────────────────────────

def _text(v) -> str:
    """Cell → stripped string; integral floats lose the '.0' so numeric codes
    survive a csv/xlsx round trip ('754751.0' is never an item key)."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _item_code(v) -> str:
    s = _text(v)
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def _iso(v) -> str | None:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # a date cell without a date format reaches us as an Excel serial
        if 20000 <= v <= 80000:
            return (_EXCEL_EPOCH + timedelta(days=int(v))).isoformat()
        return None
    s = _text(v)
    if not s:
        return None
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _qty(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _text(v).replace(",", "").replace(" ", "")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _norm_hdr(v) -> str:
    return re.sub(r"\s+", " ", _text(v)).casefold()


# ── public helpers (contract §2) ───────────────────────────────────────────

def po8(order_number) -> str:
    """Last 8 digits of the first ≥8-digit run ('2 02 1ACDE 30043537' →
    '30043537'): the dock sheet books trucks by this short form, so it is the
    join key between PO lines and receiving appointments. '' when none."""
    m = _PO8.search(_text(order_number))
    return m.group(0)[-8:] if m else ""


def decode_batch_date(batch) -> date | None:
    """'N62420340' → 2026-08-30: N + year digit (2020+d) + day-of-year(3) +
    sequence(4). Packaging lots carry this code; aseptic/IQF lots ('S…',
    6-digit) do not and decode to None, so callers cannot verify them."""
    m = _BATCH.fullmatch(_text(batch).upper())
    if not m:
        return None
    year, doy = 2020 + int(m.group(1)), int(m.group(2))
    if doy < 1:
        return None
    d = date(year, 1, 1) + timedelta(days=doy - 1)
    return d if d.year == year else None


def resolve_item_key(code, bom_items) -> str | None:
    """PO codes are plain ('735009'); BOM keys may carry a variant suffix
    ('735009-A'). Exact wins; a lone suffixed variant is accepted; two or
    more variants are ambiguous → None (listed, never guessed)."""
    code = _text(code)
    if not code:
        return None
    if code in bom_items:
        return code
    prefix = code + "-"
    hits = [k for k in bom_items if k.startswith(prefix)]
    return hits[0] if len(hits) == 1 else None


# ── file readers ───────────────────────────────────────────────────────────

def _read_xlsx(path: Path) -> list[tuple]:
    import openpyxl
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # extension warnings on IT workbooks
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [tuple(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()   # read_only keeps the zip handle open otherwise


def _read_csv(path: Path) -> list[tuple]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    # '' → None so csv rows follow the same blank rules as xlsx cells
    return [tuple(c if c != "" else None for c in r)
            for r in csv.reader(io.StringIO(text), dialect)]


def _find_header(rows: list[tuple]) -> tuple[int | None, dict, list[str]]:
    """(header row index, normalized name -> column, normalized names).
    Picks the first row within the scan window that names any required
    column; the caller reports what is missing from it."""
    for i, row in enumerate(rows[:_HEADER_SCAN]):
        names = [_norm_hdr(c) for c in row]
        if not any(n in _REQUIRED for n in names):
            continue
        ix: dict[str, int] = {}
        for j, n in enumerate(names):
            if n and n not in _UNIT_NAMES and n not in ix:
                ix[n] = j      # first occurrence wins for duplicate headers
        return i, ix, names
    return None, {}, []


def _as_of(rows: list[tuple], hdr_i: int, ix: dict) -> str | None:
    """As-of stamp IT was asked to add: either a title cell above the header
    ('As of 2026-08-24' or the date in the next cell) or a column named
    'As of'/'Report date' (first data value)."""
    for row in rows[:hdr_i]:
        for j, c in enumerate(row):
            t = _norm_hdr(c)
            if not any(k in t for k in _AS_OF):
                continue
            m = _DATE_IN_TEXT.search(t)
            if m and _iso(m.group(0)):
                return _iso(m.group(0))
            for nxt in row[j + 1:]:
                if _text(nxt):
                    return _iso(nxt)
    for name, j in ix.items():
        if any(k in name for k in _AS_OF):
            for row in rows[hdr_i + 1:]:
                if j < len(row) and _text(row[j]):
                    return _iso(row[j])
    return None


# ── loader ─────────────────────────────────────────────────────────────────

def load_open_pos(path) -> PoResult:
    """Parse the open-PO extract. Never raises for a bad file: problems land
    in errors and lines stays empty."""
    res = PoResult(source_path="" if path is None else str(path))
    if path is None or not str(path):
        res.errors.append("no PO file configured")
        return res
    p = Path(path)
    if not p.is_file():
        res.errors.append(f"file not found: {p}")
        return res
    try:
        res.source_mtime = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
    except OSError:
        pass
    try:
        rows = (_read_csv(p) if p.suffix.lower() in (".csv", ".txt")
                else _read_xlsx(p))
    except Exception as e:  # noqa: BLE001 — importer never raises
        res.errors.append(f"cannot read {p.name}: {e}")
        return res

    hdr_i, ix, names = _find_header(rows)
    if hdr_i is None:
        blank = not any(any(_text(c) for c in r) for r in rows)
        res.errors.append("empty file" if blank else
                          "header row not found (need: Order number, Ordered "
                          "item, Qty ordered at the origin, Receipt date)")
        return res
    missing = [n for n in _REQUIRED if n not in ix]
    if missing:
        res.errors.append("missing columns: " + ", ".join(missing))
        return res
    missing_opt = [n for n in _OPTIONAL if n not in ix]
    if missing_opt:
        res.errors.append("missing optional columns: " + ", ".join(missing_opt))
    qty_ix = ix["qty ordered at the origin"]
    unit_ix = next((j for j in range(qty_ix + 1, len(names))
                    if names[j] in _UNIT_NAMES), None)
    if unit_ix is None:
        res.errors.append("no unit column ('U') after 'Qty ordered at the origin'")
    res.as_of = _as_of(rows, hdr_i, ix)

    def cell(cells, name):
        j = ix.get(name)
        return None if j is None else cells[j]

    ncol = len(names)
    for r, row in enumerate(rows[hdr_i + 1:], start=hdr_i + 2):
        cells = list(row) + [None] * max(0, ncol - len(row))
        item = _item_code(cell(cells, "ordered item"))
        if item in _SENTINEL:
            # the trailing '?' row is silent; a filled row without an item
            # is data lost and gets reported
            if any(_text(c) not in _SENTINEL for c in cells):
                res.errors.append(f"row {r}: missing Ordered item, skipped")
            continue
        qty_raw = cells[qty_ix]
        qty = _qty(qty_raw)
        if qty is None:
            res.errors.append(f"row {r}: bad qty {_text(qty_raw)!r}, kept as 0")
            qty = 0.0
        rd_raw = cell(cells, "receipt date")
        rd = _iso(rd_raw)
        if rd is None and _text(rd_raw):
            res.errors.append(f"row {r}: bad Receipt date {_text(rd_raw)!r}")
        ird_raw = cell(cells, "initial receipt date")
        ird = _iso(ird_raw)
        if ird is None and _text(ird_raw):
            res.errors.append(f"row {r}: bad Initial Receipt Date {_text(ird_raw)!r}")
        slip = ((date.fromisoformat(rd) - date.fromisoformat(ird)).days
                if rd and ird else None)
        po = _text(cell(cells, "order number"))
        res.lines.append({
            "po": po,
            "po8": po8(po),
            "item": item,
            "designation": _text(cell(cells, "received item designation")),
            "qty": qty,
            "unit": "" if unit_ix is None else _text(cells[unit_ix]),
            "receipt_date": rd,
            "initial_receipt_date": ird,
            "slip_days": slip,
            "arrival_area": _text(cell(cells, "arrival area")),
            "supplier": _text(cell(cells, "supplier designation")),
            "supplier_id": _text(cell(cells, "supplier")),
            "received": bool(_text(cell(cells, "receipt number"))),
            "order_date": _iso(cell(cells, "order date")),
            "row": r,
        })
    res.n_rows = len(res.lines)
    res.max_receipt_date = max(
        (ln["receipt_date"] for ln in res.lines if ln["receipt_date"]),
        default=None)
    return res
