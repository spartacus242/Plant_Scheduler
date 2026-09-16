# tests/test_x3_po_export.py — the ERP's Sage X3 PO-line export
# (order_npa.csv) on a SYNTHETIC file that mirrors the real layout: the 219
# French headers verbatim (they are the ERP's column names, not plant data),
# pipe delimiter, cp1252, 13-digit fixed-point numbers, DD/MM/YYYY dates, a
# literal inch mark and commas inside text. Supplier and item names are
# invented; the live file is never read (supplier data stays out of git).

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.reconcile_engine import PO_FEED_NAMES, open_po_path  # noqa: E402
from stockcheck import x3_po_export as x3  # noqa: E402
from stockcheck.po_import import PoResult, load_open_pos  # noqa: E402

HEADER = [fr for _, fr, _ in x3.COLUMNS]     # the ERP header, verbatim
CONTRACT_KEYS = {"po", "po8", "item", "designation", "qty", "unit",
                 "receipt_date", "initial_receipt_date", "slip_days",
                 "arrival_area", "supplier", "supplier_id", "received",
                 "order_date", "row"}


def _row(**over) -> list[str]:
    """One data line in layout order: the constant company/site block plus
    a plausible packaging line; blanks elsewhere (as the ERP leaves them)."""
    vals = {k: "" for k in x3.KEYS}
    vals.update(
        company="M2", company_name="MATERNE NORTH AMERICA", site="02",
        site_name="NAMPA PLANT", order_type="1ACDE", order_no="30049001",
        supplier_code="000048", partner_type="FRS", supplier_name="ACME PACKAGING",
        supplier_city="SOLON, OH", supplier_country="US", order_date="27/08/2026",
        currency_mgmt="USD", currency_order="USD", currency_stat="EUR",
        line_no="000001", line_direction="1", item="754751",
        item_name="SLV 24X90 TEST", item_family="PK1",
        gross_price="0000004116800", gross_price_2="0000004116800", price_unit="KEA",
        special_price_flag="0", gross_amount_inv="0000013832450",
        line_seq="000001", line_status="20",
        qty_ordered="0000336000000", qty_remaining="0000336000000", order_unit="EA",
        qty_ordered_stat="0000000000000", qty_remaining_stat="0000000000000",
        stat_unit="KG", receipt_date="14/10/2026", receipt_time="00:00",
        receipt_location="RP1", receipt_location_name="RECEIPT PACKAGING 1",
        warehouse="SFG", requested_date="13/10/2026", invoice_discount_sign="1",
        invoice_freight_sign="-1", line_crit_code_1="PRO",
        line_crit_value_1="ORCHARD CO", line_crit_code_2="COO",
        line_crit_value_2="CHILE")
    vals.update(over)
    return [vals[k] for k in x3.KEYS]


ROWS = [
    _row(),
    # raw material in KG, partially received; comma and inch mark in text
    _row(order_no="30049002", supplier_code="000274", supplier_name="FRUIT CO, INC",
         item="730010", item_name='LABEL 6" X 8.25" PUREE', item_family="RM1",
         gross_price="0000000011463", gross_price_2="0000000011463", price_unit="KG",
         gross_amount_inv="0000206334000", qty_ordered="0000180000000",
         qty_remaining="0000040000000", order_unit="KG",
         qty_ordered_stat="0000180000000", qty_remaining_stat="0000040000000",
         receipt_date="29/09/2026", requested_date="04/08/2026",
         receipt_location="RB1", warehouse="SB1",
         line_comment_internal="8/18 REV QTY FROM 20,000 TO 18,000"),
    # fully received (remaining 0), ERP status 60
    _row(order_no="30049003", line_status="60", qty_remaining="0000000000000",
         receipt_date="10/09/2026", requested_date="10/09/2026"),
    # second line of the first PO: a bad date and a bad number
    _row(line_no="000002", item="754752", receipt_date="soon",
         qty_remaining="12x", requested_date=""),
]
BAD_ROW = 5     # file line of the garbage line (header is line 1)


def _write(path: Path, rows=ROWS, header=HEADER, newline="\n",
           encoding="cp1252") -> Path:
    text = newline.join(["|".join(header)] + ["|".join(r) for r in rows]) + newline
    path.write_bytes(text.encode(encoding))
    return path


# ── layout table ───────────────────────────────────────────────────────────

def test_layout_table_is_the_erp_header():
    assert len(x3.COLUMNS) == 219
    assert len(set(x3.KEYS)) == 219
    assert x3.KEYS[5] == "order_no" and x3.KEYS[42] == "item"
    assert x3.KEYS[88] == "qty_ordered" and x3.KEYS[89] == "qty_remaining"
    assert x3.KEYS[94] == "receipt_date" and x3.KEYS[163] == "requested_date"
    assert x3.KEYS[100] == "receipt_location" and x3.KEYS[87] == "line_status"
    assert x3.KEYS[218] == "line_crit_value_10"
    # the 15 repeated French names map to distinct keys by occurrence
    assert x3.FRENCH["company_name"] == x3.FRENCH["site_name"] == "Raison sociale"
    assert x3.FRENCH["line_no"] == x3.FRENCH["line_seq"]
    assert x3.KIND["qty_remaining"] == x3.F and x3.KIND["line_status"] == x3.I


@pytest.mark.parametrize("a, b", [
    ("Nom du <Livré par »", "Nom du « Livré par »"),        # ERP typo variants
    ("Nom du < Facturé par »", "Nom du « Facturé par »"),
    ("Libellé de l’article", "Libelle de l'article"),
    ("  Numéro   de commande ", "NUMERO DE COMMANDE"),
])
def test_norm_header_ignores_accents_case_and_punctuation(a, b):
    assert x3.norm_header(a) == x3.norm_header(b)


# ── reader ─────────────────────────────────────────────────────────────────

def test_reads_every_column_decoded_and_raw(tmp_path):
    ex = x3.read_x3_po_export(_write(tmp_path / "order_npa.csv"))
    assert ex.header_ok and not ex.unknown_columns and not ex.missing_columns
    assert ex.keys == list(x3.KEYS) and ex.header == HEADER
    assert ex.n_rows == len(ex.rows) == len(ex.raw) == 4
    assert ex.source_path.endswith("order_npa.csv") and ex.source_mtime
    r0 = ex.rows[0]
    assert set(r0) == set(x3.KEYS)
    assert r0["order_no"] == "30049001" and r0["line_no"] == "000001"
    assert r0["supplier_code"] == "000048"            # ERP key keeps its zeros
    assert r0["qty_ordered"] == 33600.0 and r0["qty_remaining"] == 33600.0
    assert r0["gross_price"] == 411.68 and r0["price_unit"] == "KEA"
    assert r0["gross_amount_inv"] == 1383.245
    assert r0["receipt_date"] == "2026-10-14" and r0["requested_date"] == "2026-10-13"
    assert r0["order_date"] == "2026-08-27"
    assert r0["line_status"] == 20 and r0["invoice_freight_sign"] == -1
    assert r0["intermediary_code"] == "" and r0["planned_lot"] == ""
    assert r0["qty_ordered_stat"] == 0.0            # blank-free fixed zero
    assert r0["header_crit_code_1"] == "" and r0["line_crit_value_2"] == "CHILE"
    # text survives verbatim: comma, inch marks
    r1 = ex.rows[1]
    assert r1["supplier_city"] == "SOLON, OH" and r1["supplier_name"] == "FRUIT CO, INC"
    assert r1["item_name"] == 'LABEL 6" X 8.25" PUREE'
    assert r1["qty_remaining_stat"] == 4000.0 and r1["gross_price"] == 1.1463
    # raw strings untouched, in file order
    assert ex.raw[0] == ROWS[0] and ex.raw[1][43] == 'LABEL 6" X 8.25" PUREE'
    # the garbage line is kept, values as strings, problems reported
    r3 = ex.rows[3]
    assert r3["receipt_date"] == "soon" and r3["qty_remaining"] == "12x"
    assert r3["requested_date"] is None
    assert [e for e in ex.errors if e.startswith(f"row {BAD_ROW}:")] == [
        f"row {BAD_ROW}: qty_remaining '12x' is not a number",
        f"row {BAD_ROW}: receipt_date 'soon' is not a date",
    ]
    assert all(e.startswith(f"row {BAD_ROW}:") for e in ex.errors)


def test_crlf_and_lf_files_parse_identically(tmp_path):
    lf = x3.read_x3_po_export(_write(tmp_path / "lf.csv", newline="\n"))
    crlf = x3.read_x3_po_export(_write(tmp_path / "crlf.csv", newline="\r\n"))
    assert crlf.rows == lf.rows and crlf.raw == lf.raw and crlf.header == lf.header
    assert crlf.errors == lf.errors
    # no trailing newline at all: last row still read
    p = tmp_path / "eof.csv"
    p.write_bytes(p.read_bytes() if p.exists() else
                  ("|".join(HEADER) + "\n" + "|".join(ROWS[0])).encode("cp1252"))
    assert x3.read_x3_po_export(p).n_rows == 1


def test_cp1252_header_and_data_decode(tmp_path):
    rows = [_row(item_name="PURÉE « BIO » CÔTÉ", supplier_city="MONTRÉAL, QC")]
    ex = x3.read_x3_po_export(_write(tmp_path / "acc.csv", rows))
    assert ex.header_ok
    assert ex.rows[0]["item_name"] == "PURÉE « BIO » CÔTÉ"
    assert ex.rows[0]["supplier_city"] == "MONTRÉAL, QC"
    assert ex.header[43] == "Libellé de l’article"


def test_literal_quote_never_swallows_the_file(tmp_path):
    """csv.excel would treat the inch mark as an opening quote and merge the
    rest of the file into one field; this reader must not."""
    rows = [_row(item_name='CASE 12" X 6"'), _row(order_no="30049009")]
    ex = x3.read_x3_po_export(_write(tmp_path / "q.csv", rows))
    assert ex.n_rows == 2 and not ex.errors
    assert ex.rows[0]["item_name"] == 'CASE 12" X 6"'
    assert ex.rows[1]["order_no"] == "30049009"


def test_header_drift_renamed_extra_missing_reordered(tmp_path):
    # renamed at the same position -> accepted by position, noted
    hdr = list(HEADER)
    hdr[61] = "Montant brut en monnaie de statistiques"      # ERP fixes its typo
    ex = x3.read_x3_po_export(_write(tmp_path / "ren.csv", header=hdr))
    assert not ex.header_ok and ex.keys[61] == "gross_amount_stat"
    assert not ex.unknown_columns and not ex.missing_columns
    assert any("column 62" in e and "by position" in e for e in ex.errors)
    assert ex.rows[0]["gross_amount_stat"] == 0.0 or ex.rows[0]["gross_amount_stat"] is None
    # an extra unknown column appended: kept under extra_<i>, listed
    hdr = HEADER + ["Nouvelle colonne"]
    rows = [r + ["X"] for r in ROWS]
    ex = x3.read_x3_po_export(_write(tmp_path / "extra.csv", rows, hdr))
    assert ex.unknown_columns == ["Nouvelle colonne"] and not ex.header_ok
    assert ex.keys[-1] == "extra_219" and ex.rows[0]["extra_219"] == "X"
    assert not ex.missing_columns
    assert any("not in the" in e and "Nouvelle colonne" in e for e in ex.errors)
    # a layout column dropped by the ERP: listed, rows carry a blank for it
    hdr = HEADER[:174] + HEADER[175:]
    rows = [r[:174] + r[175:] for r in ROWS]
    ex = x3.read_x3_po_export(_write(tmp_path / "miss.csv", rows, hdr))
    assert ex.missing_columns == ["line_comment_internal"]
    assert ex.rows[1]["line_comment_internal"] == ""
    assert ex.rows[1]["line_comment_external"] == ""      # the next column still right
    assert ex.rows[1]["qty_remaining"] == 4000.0
    # reordered columns: still mapped by name
    order = list(range(len(HEADER)))
    order[5], order[42] = order[42], order[5]
    hdr = [HEADER[i] for i in order]
    rows = [[r[i] for i in order] for r in ROWS]
    ex = x3.read_x3_po_export(_write(tmp_path / "reord.csv", rows, hdr))
    assert ex.rows[0]["order_no"] == "30049001" and ex.rows[0]["item"] == "754751"
    assert ex.keys[5] == "item" and ex.keys[42] == "order_no"


def test_wrong_field_count_row_is_kept_and_flagged(tmp_path):
    short = ROWS[0][:90]                         # truncated before receipt_date (94)
    long_ = ROWS[1] + ["tail"]                  # a stray '|' inside a comment
    ex = x3.read_x3_po_export(_write(tmp_path / "w.csv", [short, long_, ROWS[2]]))
    assert ex.n_rows == 3
    assert ex.rows[0]["order_no"] == "30049001" and ex.rows[0]["receipt_date"] is None
    assert ex.rows[0]["qty_remaining"] == 33600.0          # index 89 survived
    assert ex.rows[1]["extra_overflow"] == ["tail"] and ex.rows[1]["order_no"] == "30049002"
    assert ex.raw[1] == long_ and ex.raw[0] == short       # raw is untouched
    msgs = [e for e in ex.errors if "fields, expected 219" in e]
    assert len(msgs) == 2 and msgs[0].startswith("row 2: 90 fields")
    assert msgs[1].startswith("row 3: 220 fields")


def test_empty_headerless_missing_and_none_never_raise(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_bytes(b"")
    assert x3.read_x3_po_export(p).errors == ["empty file"]
    p2 = tmp_path / "junk.csv"
    p2.write_bytes(b"a|b|c\n1|2|3\n")
    ex = x3.read_x3_po_export(p2)
    assert ex.rows == [] and ex.errors and "header row not found" in ex.errors[0]
    assert x3.read_x3_po_export(tmp_path / "nope.csv").errors[0].startswith("file not found")
    assert x3.read_x3_po_export(None).errors == ["no PO file configured"]
    assert x3.read_x3_po_export("").errors == ["no PO file configured"]


def test_looks_like_x3_export(tmp_path):
    assert x3.looks_like_x3_export(_write(tmp_path / "order_npa.csv"))
    assert x3.looks_like_x3_export(_write(tmp_path / "crlf.csv", newline="\r\n"))
    legacy = tmp_path / "open_pos.csv"
    legacy.write_text("Order date,Order number,Ordered item,Qty ordered at the "
                      "origin,U,Receipt date\n", encoding="utf-8")
    assert not x3.looks_like_x3_export(legacy)
    assert not x3.looks_like_x3_export(tmp_path / "missing.csv")
    pipes = tmp_path / "pipes.csv"
    pipes.write_text("|".join(f"c{i}" for i in range(80)) + "\n", encoding="utf-8")
    assert not x3.looks_like_x3_export(pipes)


# ── PoLine projection ──────────────────────────────────────────────────────

def test_to_po_lines_contract_and_extras(tmp_path):
    ex = x3.read_x3_po_export(_write(tmp_path / "order_npa.csv"))
    lines = x3.to_po_lines(ex)
    assert len(lines) == 4 and all(CONTRACT_KEYS <= set(ln) for ln in lines)
    a, b, c, d = lines
    assert {k: a[k] for k in CONTRACT_KEYS} == {
        "po": "M2 02 1ACDE 30049001", "po8": "30049001", "item": "754751",
        "designation": "SLV 24X90 TEST", "qty": 33600.0, "unit": "EA",
        "receipt_date": "2026-10-14", "initial_receipt_date": "2026-10-13",
        "slip_days": 1, "arrival_area": "RP1", "supplier": "ACME PACKAGING",
        "supplier_id": "48", "received": False, "order_date": "2026-08-27",
        "row": 2,
    }
    assert isinstance(a["qty"], float)
    # the export's own facts ride along
    assert a["qty_ordered"] == 33600.0 and a["status"] == 20
    assert a["qty_remaining_kg"] is None                # EA line: no kg figure
    assert a["price"] == 411.68 and a["price_unit"] == "KEA"
    assert a["amount"] == 1383.245 and a["currency"] == "USD"
    assert a["producer"] == "ORCHARD CO" and a["origin"] == "CHILE"
    assert a["warehouse"] == "SFG" and a["supplier_code"] == "000048"
    assert a["line_no"] == "000001" and a["line_seq"] == "000001"
    # partially received: qty is what is STILL inbound, ordered kept
    assert b["qty"] == 4000.0 and b["qty_ordered"] == 18000.0
    assert b["qty_remaining_kg"] == 4000.0 and b["unit"] == "KG"
    assert b["received"] is False and b["supplier_id"] == "274"
    assert b["slip_days"] == 56 and b["arrival_area"] == "RB1"
    assert b["comment"] == "8/18 REV QTY FROM 20,000 TO 18,000"
    assert b["designation"] == 'LABEL 6" X 8.25" PUREE' and b["row"] == 3
    # nothing left to receive -> received, whatever the status code says
    assert c["received"] is True and c["qty"] == 0.0 and c["status"] == 60
    assert c["row"] == 4
    # garbage: qty 0, dates None, slip None — the reader reported the row
    assert d["qty"] == 0.0 and d["receipt_date"] is None
    assert d["initial_receipt_date"] is None and d["slip_days"] is None
    assert d["received"] is False and d["item"] == "754752" and d["row"] == BAD_ROW


def test_to_po_lines_supplier_id_all_zeros_and_blank_company():
    ex = x3.X3PoExport(keys=list(x3.KEYS), rows=[
        {**{k: "" for k in x3.KEYS}, "order_no": "30049010", "supplier_code": "000",
         "qty_remaining": None},
    ])
    ln = x3.to_po_lines(ex)[0]
    assert ln["po"] == "30049010" and ln["po8"] == "30049010"
    assert ln["supplier_id"] == "000"          # never '' from a real code
    assert ln["qty"] == 0.0 and ln["received"] is False   # unknown ≠ received


# ── load_open_pos integration ──────────────────────────────────────────────

def test_load_open_pos_detects_the_x3_layout(tmp_path):
    p = _write(tmp_path / "order_npa.csv", newline="\r\n")   # bridge copies CRLF
    res = load_open_pos(p)
    assert isinstance(res, PoResult) and res.layout == "x3"
    assert res.n_rows == len(res.lines) == 4
    assert res.max_receipt_date == "2026-10-14" and res.as_of is None
    assert res.source_path == str(p) and res.source_mtime
    assert res.lines[0]["po8"] == "30049001" and res.lines[2]["received"] is True
    assert any(e.startswith(f"row {BAD_ROW}:") for e in res.errors)
    # a .txt copy is detected too; an .xlsx never goes through this path
    assert load_open_pos(_write(tmp_path / "order_npa.txt")).layout == "x3"


def test_load_open_pos_legacy_csv_still_takes_the_legacy_path(tmp_path):
    p = tmp_path / "open_pos.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Order number", "Ordered item", "Qty ordered at the origin",
                    "U", "Receipt date"])
        w.writerow(["2 02 1ACDE 30043537", "754751", "67200", "EA ", "2026-09-02"])
    res = load_open_pos(p)
    assert res.layout == "npa_open_pos" and res.n_rows == 1
    assert res.lines[0]["po8"] == "30043537" and res.lines[0]["qty"] == 67200.0
    assert "status" not in res.lines[0]


# ── resolver ───────────────────────────────────────────────────────────────

def test_open_po_path_prefers_the_erp_export(tmp_path):
    dd = tmp_path / "data"
    ref = dd / "reference"
    ref.mkdir(parents=True)
    assert PO_FEED_NAMES == ("order_npa.csv", "open_pos.xlsx", "open_pos.csv")
    (ref / "open_pos.xlsx").write_bytes(b"x")
    (ref / "open_pos.csv").write_text("a\n", encoding="utf-8")
    assert open_po_path(dd, {}) == ref / "open_pos.xlsx"
    (ref / "order_npa.csv").write_bytes(b"y")
    assert open_po_path(dd, {}) == ref / "order_npa.csv"
    # a folder by that name is not a feed
    (ref / "order_npa.csv").unlink()
    (ref / "order_npa.csv").mkdir()
    assert open_po_path(dd, {}) == ref / "open_pos.xlsx"
    # the configured override still wins
    custom = tmp_path / "it" / "order_npa.csv"
    custom.parent.mkdir()
    custom.write_bytes(b"z")
    assert open_po_path(dd, {"datasources": {"po_report_path": str(custom)}}) == custom


# ── clean re-exports ───────────────────────────────────────────────────────

def test_write_clean_csv_and_xlsx(tmp_path):
    ex = x3.read_x3_po_export(_write(tmp_path / "order_npa.csv"))
    out = x3.write_clean_csv(ex, tmp_path / "clean.csv")
    with open(out, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == list(x3.KEYS) and len(rows) == 5
    r1 = dict(zip(rows[0], rows[2]))
    assert r1["supplier_name"] == "FRUIT CO, INC"       # comma survives quoting
    assert r1["item_name"] == 'LABEL 6" X 8.25" PUREE'
    assert r1["qty_remaining"] == "4000.0" and r1["receipt_date"] == "2026-09-29"
    assert r1["supplier_code"] == "000274"
    fr = x3.write_clean_csv(ex, tmp_path / "clean_fr.csv", french_names=True)
    with open(fr, encoding="utf-8-sig", newline="") as f:
        assert next(csv.reader(f)) == HEADER
    xp = x3.write_clean_xlsx(ex, tmp_path / "clean.xlsx")
    wb = openpyxl.load_workbook(xp, read_only=True)
    ws = wb["PO lines"]
    got = [list(r) for r in ws.iter_rows(values_only=True)]
    assert got[0] == list(x3.KEYS) and len(got) == 5
    r2 = dict(zip(got[0], got[2]))
    assert r2["receipt_date"] == datetime(2026, 9, 29)   # a real date cell
    assert r2["qty_remaining"] == 4000.0 and r2["supplier_code"] == "000274"
    cols = [list(r) for r in wb["Columns"].iter_rows(values_only=True)]
    assert cols[0] == ["#", "key", "French header (as exported)", "kind"]
    assert len(cols) == 220 and cols[6][1:3] == ["order_no", "Numéro de commande"]
    wb.close()


# ── ERP line status + KEA (IT meeting 2026-09-15) ───────────────────────────

def test_status_codes_and_kea_follow_the_erp_meaning(tmp_path):
    """IT (2026-09-15): 20 = receivable, 60 = archived (closed), 70 = deleted;
    order unit KEA = thousand each. A deleted line must never count as
    inbound however much 'remains' on it, an archived line is closed, and a
    1.5 KEA line is 1,500 EA for the BOM join. An unknown code keeps the
    remaining-qty rule."""
    rows = [
        _row(order_no="30049101", line_status="70"),                       # deleted, full qty
        _row(order_no="30049102", line_status="60"),                       # archived, full qty
        _row(order_no="30049103", line_status="20"),                       # receivable
        _row(order_no="30049104", line_status="20", order_unit="KEA",
             qty_ordered="0000000015000", qty_remaining="0000000015000",
             price_unit="EA"),
        _row(order_no="30049105", line_status="35"),                       # unknown code
    ]
    ex = x3.read_x3_po_export(_write(tmp_path / "order_npa.csv", rows=rows))
    deleted, archived, open_, kea, unknown = x3.to_po_lines(ex)
    assert deleted["cancelled"] is True and deleted["status_text"] == "deleted"
    assert deleted["received"] is False and deleted["qty"] == 33600.0   # facts kept, never inbound
    assert archived["received"] is True and archived["cancelled"] is False
    assert archived["status_text"] == "archived" and archived["qty"] == 33600.0
    assert open_["received"] is False and open_["cancelled"] is False
    assert open_["status_text"] == "receivable" and open_["status"] == 20
    assert kea["qty"] == 1500.0 and kea["qty_ordered"] == 1500.0
    assert kea["unit"] == "EA" and kea["unit_original"] == "KEA"
    assert open_["unit_original"] == ""                    # EA line priced per KEA: untouched
    assert unknown["status_text"] == "" and unknown["received"] is False
    assert unknown["cancelled"] is False and unknown["status"] == 35
