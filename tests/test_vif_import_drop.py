# tests/test_vif_import_drop.py — the ERP drop of 2026-09-15 (fs_data/fs_vif):
# every VIF layout the importer must read, rebuilt inline under tmp_path with
# the real quirks — CRLF, cp1252, the 'Page1/1 ' footer row, the headerless
# ediact.csv with duplicated lines, the PKG-REC selection-recall preamble +
# header, the pipe-delimited jestkexp5, 134-byte empty reports, the
# jestkexp4 / jestksa aliases and the ignored jestkex2. Nothing reads
# data/reference (guard-compliant); the old-name fixture
# data/stockcheck/dev_vif must keep importing under the same frame keys.
# 2026-09-16 review: ediact.csv lacks the semi-finished recipes (note +
# missing_recipes), required exports missing are errors, an empty/unreadable
# ediact.csv falls back to ediact 3.csv (an unreadable one stays an error so
# the refresh gate retries it), alias-loaded frames are stamped under
# the canonical name, lot BBDs read month-first, and a lot listed by two
# exports is kept once.

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pandas as pd
import pytest
from pandas.api.types import is_datetime64_any_dtype, is_float_dtype

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from stockcheck.vif_import import (  # noqa: E402
    BOM_FILES, EDIACT4_COLS, EDIACT_COLS, JESTKEXP2_COLS, JESTKEXP_COLS,
    LOT_COLS, LOT_FILES, PKG_REC_COLS, REQUIRED_FILES, VIF_FILES, VifSnapshot,
    find_bom_file, import_vif_folder, load_ediact, load_ediact4, load_jestkexp,
    load_latest_snapshot, load_pkg_rec, lot_frames, missing_semi_recipes,
    receipts_frame, save_snapshot,
)

DEV_VIF = ROOT / "data" / "stockcheck" / "dev_vif"
FOOTER = "Page1/1 "
# The 134-byte report the VIF prints when a selection has no rows.
EMPTY_REPORT = ("\r\n\r\nRecall of the selection\r\n"
                "Item :;[Selection on pyramid];Depot :;AMI\r\n ; ; ; \r\n"
                "\r\n\r\n\r\nNo data corresponds to your selection. Page1/1 ")


def _w(folder: Path, name: str, lines: list[str], *, footer: str | None = FOOTER,
       newline: str = "\r\n") -> Path:
    """Write a VIF-style export: cp1252, CRLF, footer row without newline."""
    text = newline.join(lines) + newline + (footer or "")
    p = folder / name
    with open(p, "w", encoding="cp1252", newline="") as f:
        f.write(text)
    return p


# ---- inline layouts (copied from the real drop, trimmed) -------------------

EDIACT_NEW = [  # headerless, no flow type, Freinte last, lines repeated x2/x3
    "120430;120430;01/01/2026;FG;Output;120430;12X4X90 APL GGS;1,600;CAS;0.00",
    "120430;120430;01/01/2026;FG;Output;120430;12X4X90 APL GGS;1,600;CAS;0.00",
    "120430;120430;01/01/2026;FG;Input;735009-A;TRIM-SHEET DOUBLE WALL 40x48;10;EA;0.75",
    "120430;120430;01/01/2026;FG;Input;735009-A;TRIM-SHEET DOUBLE WALL 40x48;10;EA;0.75",
    "120430;120430;01/01/2026;FG;Input;735009-A;TRIM-SHEET DOUBLE WALL 40x48;10;EA;0.75",
    "120430;120430;01/01/2026;FG;Input;756150;WRAP 12x4x90;1,600;EA;0.75",
    # a real slurry activity consuming the HSM slurry: ediact.csv carries NO
    # HSM750216 activity (its recipe lives only in 'ediact 4.csv', 2026-09-16)
    "280605;SPTP764154;04/05/2026;R;Output;SPTP764154;R APPLE CINNAMON PROTEIN;1,000.000;Kg;0.00",
    "280605;SPTP764154;04/05/2026;R;Input;HSM750216;HSM CONV PROTEIN SLURRY;441.500;Kg;8.00",
    "280605;SPTP764154;04/05/2026;R;of cost;ORG APPLE BRIX;ORG APPLE BRIX EFFECT;;U;0.00",
    "TRIALS;VPATRIALS;22/07/2026;POU;Input;SPTPTRIALS;SPTPTRIALS;;Kg;0.00",
]
EDIACT_NEW_UNIQUE = 7
EDIACT_NEW_DUPS = 3

EDIACT_OLD = [  # 'ediact 3.csv' until 2026-09: header, flow type at index 2
    "PF;Activity;Type de flux de l'activité;Effective date;Famille d'activité;"
    "Item type;Item;Designation;Quantity (ACT);Column1",
    "120430;120430;Pull;01/01/2026;FG;Output;120430;12X4X90 APL GGS;1600;CAS",
    "120430;120430;Pull;01/01/2026;FG;Input;735009-A;TRIM-SHEET;10;EA",
]
EDIACT4_OLD = [  # 'ediact 4.csv': header names the no-flow-type layout
    "PF;Activity;Effective date;Famille d'activité;Item type;Item;Designation;"
    "Quantity (ACT);Column1;Freinte",
    ";HSM750216;01/01/2026;RF;of cost;APPLE BRIX;APPLE BRIX EFFECT;;U;0",
    ";HSM750216;01/01/2026;RF;Output;HSM750216;HSM CONV PROTEIN SLURRY;1000;Kg;0",
    ";HSM750216;01/01/2026;RF;Input;750145;L-MALIC ACID;19.3;Kg;8.1",
]
JESTKEXP = [  # raw materials, 13 cols
    "Item;Designation;SCN;Status;Statut;BBD;BATCH;Qty (MU);Unit;Depot;Location;"
    "PreCaut;SupplierBatch",
    "730001;CINNAMON POWDER;1003057166;Ava;AVA;04/16/2027;N61700468;22.68;Kg;M01;"
    "CINN;;5343835701",
    "730001;CINNAMON POWDER;1003057157;Loc;QC;12/01/2027;N61700468;1,022.5;Kg;SB1;"
    "E0101;;5343835701",
]
JESTKEXP2 = [  # packaging, 12 cols: Batch/BBD BEFORE Status
    "Item;Designation;SCN;Batch;BBD;Status;Statut;Qty (MU);Unit;Depot;Location;PreCaut",
    "735009-A;TRIM-SHEET DOUBLE WALL 40x48;1003009727;N61330487;;Ava;AVA;165.00;EA;"
    "SFG;FF00D;",
    "758019;HOT MELT HM093704;1003127763;N62300540;;Out;OL;889.00;Kg;M21;DK0502;",
]
JESTKAMB = [  # Americold Burley off-site, jestkexp layout, depot AMB
    JESTKEXP[0],
    "730002;APPLE JUICE CONCENTRATE;2099723920;Ava;AVA;07/05/2027;462151;1228.00;Kg;"
    "AMB;30038065;;20150140",
    "750199;APPLE JUICE NFC;2001817246;Loc;QC;04/18/2028;470867;880.00;Kg;AMB;"
    "30042719;;JU260418A-0-E1",
]
JESTKEXQ = [  # quality-control depots, jestkexp2 layout
    JESTKEXP2[0],
    "730006;PEACH PUREE SING STR;6002589211;S168137;02/04/2027;Ava;AVA;220.00;Kg;QCR;DES;",
    "GSSPTP9224SCN;R ORG ACIN SCN;1003096508;N62010617;07/23/2026;Out;OL;850.00;Kg;QCP;DES;",
    "730006;PEACH PUREE SING STR;6002613023;S168137;;Loc;QC;220.00;Kg;QCR;DES;",
]
JESTKSAV = [  # fresh apples, 17 cols = jestkexp 13 + provenance
    JESTKEXP[0] + ";Variety;Supplier Number;Supplier Name;RAW APPLE RECEIVING #",
    "730065;FRESH APPLE PRIMARY;1003153917;Ava;AVA;;N62520423;397.91;Kg;SA1;07;;;"
    "S - COSM;000075;EXCEL FRUIT BRO;33909",
    "730070;ORG FRESH APPLE PRIMARY;1003161299;Ava;AVA;;N62580379;404.23;Kg;SA2;14;;;"
    "S - HON;000075;EXCEL FRUIT BRO;34014",
]
JESTKSA = [  # the same lots without the 4 provenance columns
    JESTKEXP[0],
    "730065;FRESH APPLE PRIMARY;1003153917;Ava;AVA;;N62520423;397.91;Kg;SA1;07;;",
    "730070;ORG FRESH APPLE PRIMARY;1003161299;Ava;AVA;;N62580379;404.23;Kg;SA2;14;;",
]
JESTKEXP5 = [  # semi-finished WIP, PIPE-delimited, no footer in the real file
    "Item|SCN|BATCH|Quantity|UOM|Designation|Batch status|Family|Depot|Location|BBD",
    "GSSPTP9221SCN|1003140626|N62420133|2160.0000|Kg|R ORG APL SCN|OL|SFG|M01|CT1|09/02/2026",
    "HSM764059|1003157480|N62540452|500.0000|Kg|ORG SLURRY|AVA|SFG|M12|CT1|09/14/2026",
]
JESTKEX2 = [  # stale 2025 packaging snapshot: extra RECDate column, always blank
    JESTKEXP2[0] + ";RECDate",
    "735009-A;TRIM-SHEET DOUBLE WALL 40x48;1002390628;N42960191;;Ava;AVA;165.00;EA;"
    "SFG;AH0201;; ",
]
PKG_REC = [  # receipt slips: preamble (1-4 fields), header, 9-field rows, LF
    "", "", "Recall of the selection",
    "Prechrono :;Receipt slips;Management :;Actual",
    "Receipt Date :;09/12/2026 -> 09/18/2026;Admin update in stock :;To update in stock",
    "Item :;[Selection on pyramid];;", " ; ; ; ", "", "",
    "Receipt Date;Item;Description;Batch;Quantity;UOM;PO Number;Receipt Trans #;User",
    "09/12/2026;754751;SLV 24x90 KS OFV STBSPN-MANPEC;N62550171;2400;EA;30044052;30120501;DWOODSTR",
    "09/13/2026;756143;WRAP 2x20x90 OPT;N62580454;35,200;EA;2 02 1ACDE 30043986;30119536;OSANTOS",
    "09/15/2026;756143;WRAP 2x20x90 OPT;N62580453;9600;EA;30043986;30119536;OSANTOS",
]
AZAPART = [
    "030480         ;48X90 APL MEX GGS;CAS;Kg ;1 CAS = 4.320 Kg;1.000 CNT = 180 CAS;  4.320;",
    "120340         ;12x90 ASTB CAN GGS;CAS;Kg ;1 CAS = 6.480 Kg;1.000 CNT = 108 CAS;  6.480;12x90G",
]
RMPKITEMS = ["", "", "Recall of the selection",
             "Basic family :,PK1,PK3,RM1,Item :,000000000000000 -> ZZZZZZZZZZZZZZZ",
             " , , , ", "", "", "Item,Designation", "730001,CINNAMON POWDER",
             "754751,SLV 24x90 KS OFV"]


def _new_drop(folder: Path) -> Path:
    """The 2026-09-15 drop in miniature: every new name, both aliases, the
    ignored stale/empty reports and the unchanged azapart/rmpkitems."""
    folder.mkdir(parents=True, exist_ok=True)
    _w(folder, "ediact.csv", EDIACT_NEW)
    _w(folder, "jestkexp.csv", JESTKEXP)
    _w(folder, "jestkexp4.csv", JESTKEXP)          # byte-identical alias
    _w(folder, "jestkexp2.csv", JESTKEXP2)
    _w(folder, "jestkamb.csv", JESTKAMB)
    _w(folder, "jestkexq.csv", JESTKEXQ)
    _w(folder, "jestksav.csv", JESTKSAV)
    _w(folder, "jestksa.csv", JESTKSA)              # redundant alias
    _w(folder, "jestkexp5.csv", JESTKEXP5, footer=None)
    _w(folder, "jestkex2.csv", JESTKEX2)
    (folder / "jestkami.csv").write_bytes(EMPTY_REPORT.encode("cp1252"))
    (folder / "jestktgb.csv").write_bytes(EMPTY_REPORT.encode("cp1252"))
    _w(folder, "PKG-REC.csv", PKG_REC, footer=None, newline="\n")
    _w(folder, "azapart.csv", AZAPART, footer=None)
    _w(folder, "rmpkitems.csv", RMPKITEMS, footer=None)
    return folder


# ---- load_ediact ------------------------------------------------------------

def test_ediact_new_headerless_layout_dedupes_and_drops_footer(tmp_path):
    """ediact.csv (drop of 2026-09-15): no header, no flow-type column,
    Freinte last, exact duplicate lines x2-4 and a 'Page1/1 ' footer. The
    frame must come out in EDIACT_COLS order with flow_type '' and a
    'freinte' column, duplicates dropped and counted, footer gone."""
    df = load_ediact(_w(tmp_path, "ediact.csv", EDIACT_NEW))
    assert list(df.columns) == EDIACT_COLS + ["freinte"]
    assert len(df) == EDIACT_NEW_UNIQUE
    assert df.attrs["dropped_duplicates"] == EDIACT_NEW_DUPS
    assert (df["flow_type"] == "").all()
    assert not df["PF"].str.startswith("Page").any()
    out = df[(df["item"] == "120430") & (df["item_type"] == "Output")]
    assert len(out) == 1 and out["qty_act"].iloc[0] == 1600.0     # thousands comma
    assert df.loc[df["item"] == "SPTP764154", "qty_act"].iloc[0] == 1000.0
    assert df.loc[df["item"] == "HSM750216", "qty_act"].iloc[0] == 441.5
    assert "HSM750216" not in set(df["Activity"])      # no recipe in ediact.csv
    assert df.loc[df["item"] == "735009-A", "freinte"].iloc[0] == 0.75
    assert is_float_dtype(df["freinte"]) and is_float_dtype(df["qty_act"])
    assert df.loc[df["item_type"] == "of cost", "qty_act"].isna().all()
    assert is_datetime64_any_dtype(df["effective_date"])
    # 22/07/2026 only parses day-first -> the whole column stays day-first
    trial = df.loc[df["PF"] == "TRIALS", "effective_date"].iloc[0]
    assert (trial.day, trial.month) == (22, 7)
    assert df["effective_date"].notna().all()


def test_ediact_old_layout_with_header_and_flow_type_unchanged(tmp_path):
    """'ediact 3.csv' (pre-2026-09): header row with the flow-type column at
    index 2 and the 'Page1/1 ;;;;;;;;;' footer -> EDIACT_COLS exactly (no
    freinte), flow_type 'Pull', zero duplicates reported."""
    p = _w(tmp_path, "ediact 3.csv", EDIACT_OLD, footer="Page1/1 ;;;;;;;;;")
    df = load_ediact(p)
    assert list(df.columns) == EDIACT_COLS
    assert len(df) == 2 and (df["flow_type"] == "Pull").all()
    assert df.attrs["dropped_duplicates"] == 0
    assert df["qty_act"].tolist() == [1600.0, 10.0]


def test_ediact_old_layout_with_trailing_freinte_keeps_it(tmp_path):
    """The live 'ediact 3' variant carries an 11th 'Freinte' column after the
    unit: it is kept as 'freinte' (float), still with flow_type from index 2."""
    p = _w(tmp_path, "ediact 3.csv", [
        "PF;Activity;Type;Effective date;Famille;Item type;Item;Designation;"
        "Quantity (ACT);;Freinte",
        "120430;120430;Pull;01/01/2026;FG;Output;120430;X;1,600;CAS;0.00",
        "120430;120430;Pull;01/01/2026;FG;Input;735009-A;Y;10;EA;0.75"])
    df = load_ediact(p)
    assert list(df.columns) == EDIACT_COLS + ["freinte"]
    assert df["freinte"].tolist() == [0.0, 0.75]
    assert (df["flow_type"] == "Pull").all()


def test_load_ediact4_keeps_legacy_shape_without_flow_type(tmp_path):
    """load_ediact4 shares the parser but returns EDIACT4_COLS (+ freinte),
    never a flow_type column, so BomGraph's HSM branch is untouched."""
    df = load_ediact4(_w(tmp_path, "ediact 4.csv", EDIACT4_OLD,
                         footer="Page1/1 ;;;;;;;;;"))
    assert list(df.columns) == EDIACT4_COLS + ["freinte"]
    assert "flow_type" not in df.columns
    assert len(df) == 3 and set(df["Activity"]) == {"HSM750216"}
    assert df.loc[df["item_type"] == "Output", "qty_act"].iloc[0] == 1000.0
    assert df.loc[df["item"] == "750145", "freinte"].iloc[0] == pytest.approx(8.1)


def test_ediact_empty_report_gives_empty_typed_frame(tmp_path):
    """A 'No data corresponds to your selection' report -> empty frame with
    the full column set and typed qty/date columns, flagged empty_export."""
    p = tmp_path / "ediact.csv"
    p.write_bytes(EMPTY_REPORT.encode("cp1252"))
    df = load_ediact(p)
    assert df.empty and list(df.columns) == EDIACT_COLS + ["freinte"]
    assert df.attrs["dropped_duplicates"] == 0 and df.attrs["empty_export"]
    assert is_float_dtype(df["qty_act"]) and is_datetime64_any_dtype(df["effective_date"])


# ---- lot loaders ------------------------------------------------------------

def _check_lot_contract(df: pd.DataFrame, source: str, extras=()):
    assert list(df.columns) == LOT_COLS + list(extras)
    assert is_float_dtype(df["qty"]) and df["qty"].notna().all()
    assert is_datetime64_any_dtype(df["bbd"])
    assert (df["source"] == source).all()
    assert not df["item"].str.startswith("Page").any()
    assert (df["item"] != "").all()


def test_jestkexp_raw_materials_maps_by_header(tmp_path):
    """jestkexp.csv (13 cols): PreCaut/SupplierBatch land in precaut /
    supplier_batch, BBD auto-detects month-first, qty strips the thousands
    comma, the footer row is dropped."""
    df = load_jestkexp(_w(tmp_path, "jestkexp.csv", JESTKEXP))
    _check_lot_contract(df, "jestkexp.csv")
    assert len(df) == 2
    assert df["status"].tolist() == ["Ava", "Loc"]
    assert df["statut"].tolist() == ["AVA", "QC"]
    assert df["supplier_batch"].tolist() == ["5343835701"] * 2
    assert df["qty"].tolist() == [22.68, 1022.5]
    assert df["bbd"].iloc[0].month == 4 and df["bbd"].iloc[1].month == 12
    assert df["depot"].tolist() == ["M01", "SB1"]


def test_jestkexp2_packaging_batch_before_status(tmp_path):
    """jestkexp2.csv (12 cols) puts Batch/BBD before Status: mapping by
    header name must not confuse them (a positional read would put the
    batch number in 'status'); missing SupplierBatch -> ''."""
    df = load_jestkexp(_w(tmp_path, "jestkexp2.csv", JESTKEXP2), packaging=True)
    _check_lot_contract(df, "jestkexp2.csv")
    assert df["batch"].tolist() == ["N61330487", "N62300540"]
    assert df["status"].tolist() == ["Ava", "Out"]
    assert df["bbd"].isna().all()
    assert (df["supplier_batch"] == "").all()
    assert df["depot"].tolist() == ["SFG", "M21"]


def test_new_depot_files_share_the_lot_contract(tmp_path):
    """jestkamb (AMB off-site, jestkexp layout), jestkexq (QCR/QCP, jestkexp2
    layout) and jestksa (apples without provenance) all load into LOT_COLS
    with their own depots, statuses and source names."""
    amb = load_jestkexp(_w(tmp_path, "jestkamb.csv", JESTKAMB))
    _check_lot_contract(amb, "jestkamb.csv")
    assert set(amb["depot"]) == {"AMB"} and amb["status"].tolist() == ["Ava", "Loc"]
    assert amb["supplier_batch"].tolist() == ["20150140", "JU260418A-0-E1"]

    qc = load_jestkexp(_w(tmp_path, "jestkexq.csv", JESTKEXQ), packaging=True)
    _check_lot_contract(qc, "jestkexq.csv")
    assert set(qc["depot"]) == {"QCR", "QCP"}
    assert qc["status"].tolist() == ["Ava", "Out", "Loc"]
    assert qc["bbd"].iloc[0].month == 2 and pd.isna(qc["bbd"].iloc[2])

    sa = load_jestkexp(_w(tmp_path, "jestksa.csv", JESTKSA))
    _check_lot_contract(sa, "jestksa.csv")
    assert set(sa["depot"]) == {"SA1", "SA2"} and sa["bbd"].isna().all()


def test_jestksav_apples_keep_provenance_extras(tmp_path):
    """jestksav.csv (17 cols) adds variety / supplier_no / supplier_name /
    apple_receipt after LOT_COLS; the four map from the report's own
    header names ('Supplier Number', 'RAW APPLE RECEIVING #', ...)."""
    df = load_jestkexp(_w(tmp_path, "jestksav.csv", JESTKSAV))
    extras = ["variety", "supplier_no", "supplier_name", "apple_receipt"]
    _check_lot_contract(df, "jestksav.csv", extras)
    assert df["variety"].tolist() == ["S - COSM", "S - HON"]
    assert df["supplier_no"].tolist() == ["000075"] * 2
    assert df["supplier_name"].tolist() == ["EXCEL FRUIT BRO"] * 2
    assert df["apple_receipt"].tolist() == ["33909", "34014"]
    assert df["qty"].tolist() == [397.91, 404.23]


def test_jestkexp5_pipe_delimited_semi_finished(tmp_path):
    """jestkexp5.csv: '|'-delimited, header Item|SCN|BATCH|Quantity|UOM|
    Designation|Batch status|Family|Depot|Location|BBD, no footer. Quantity
    -> qty, UOM -> unit, 'Batch status' -> status with AVA folded to 'Ava'
    (OL kept), Family kept as the 'family' extra, statut/precaut ''."""
    df = load_jestkexp(_w(tmp_path, "jestkexp5.csv", JESTKEXP5, footer=None))
    _check_lot_contract(df, "jestkexp5.csv", ["family"])
    assert df["item"].tolist() == ["GSSPTP9221SCN", "HSM764059"]
    assert df["status"].tolist() == ["OL", "Ava"]
    assert df["unit"].tolist() == ["Kg", "Kg"]
    assert df["qty"].tolist() == [2160.0, 500.0]
    assert df["depot"].tolist() == ["M01", "M12"]
    assert df["family"].tolist() == ["SFG", "SFG"]
    assert (df["statut"] == "").all() and (df["precaut"] == "").all()
    assert df["bbd"].iloc[1].month == 9 and df["bbd"].iloc[1].day == 14


def test_lot_loader_unrecognised_header_falls_back_to_positions(tmp_path):
    """A header the map does not know (the dev 'Column1..' style) keeps the
    legacy behaviour: row 0 dropped, the first 11 columns named positionally
    by JESTKEXP_COLS (or JESTKEXP2_COLS when packaging=True)."""
    cols = ";".join(f"Column{i}" for i in range(1, 12))
    rm = load_jestkexp(_w(tmp_path, "rm.csv", [
        cols, "730001;CINNAMON;1;Ava;AVA;04/16/2027;N1;3.24;Kg;M01;CINN"]))
    _check_lot_contract(rm, "rm.csv")
    assert rm.loc[0, ["item", "status", "batch", "depot"]].tolist() == \
        ["730001", "Ava", "N1", "M01"]
    assert list(JESTKEXP_COLS) == LOT_COLS[:11]
    pk = load_jestkexp(_w(tmp_path, "pk.csv", [
        cols, "735009-A;TRIM;1;N61330487;;Ava;AVA;165;EA;SFG;FF00D"]),
        packaging=True)
    assert pk.loc[0, ["batch", "status", "depot"]].tolist() == \
        ["N61330487", "Ava", "SFG"]
    assert JESTKEXP2_COLS[3] == "batch" and JESTKEXP2_COLS[5] == "status"


def test_lot_empty_report_gives_empty_typed_frame(tmp_path):
    """An empty jestk* report -> zero rows, every LOT_COLS column, qty float
    and bbd datetime, attrs empty_export — no error."""
    p = tmp_path / "jestkexq.csv"
    p.write_bytes(EMPTY_REPORT.encode("cp1252"))
    df = load_jestkexp(p, packaging=True)
    assert df.empty and list(df.columns) == LOT_COLS
    assert is_float_dtype(df["qty"]) and is_datetime64_any_dtype(df["bbd"])
    assert df.attrs["empty_export"]


def test_lot_bbd_is_month_first_even_when_every_date_is_ambiguous(tmp_path):
    """Rule (2026-09-16): every VIF jestk export writes BBD month-first, so a
    column holding only ambiguous dates ('09/02/2026') reads 2 Sep 2026 —
    the per-column auto-detect used to tie and pick day-first (9 Feb). The
    auto-detect stays the fallback when month-first leaves values unparsed:
    the dev fixture's day-first '16/04/2027' still reads 16 Apr 2027."""
    header = JESTKEXP[0]
    amb = load_jestkexp(_w(tmp_path, "jestkexp.csv", [
        header,
        "730001;CINNAMON POWDER;1;Ava;AVA;09/02/2026;N1;1;Kg;M01;CINN;;",
        "730001;CINNAMON POWDER;2;Ava;AVA;03/04/2027;N2;1;Kg;M01;CINN;;",
        "730001;CINNAMON POWDER;3;Ava;AVA;;N3;1;Kg;M01;CINN;;"]))
    assert amb["bbd"].iloc[0] == pd.Timestamp("2026-09-02")
    assert amb["bbd"].iloc[1] == pd.Timestamp("2027-03-04")
    assert pd.isna(amb["bbd"].iloc[2])
    semi = load_jestkexp(_w(tmp_path, "jestkexp5.csv", [
        JESTKEXP5[0],
        "GSSPTP9221SCN|1003140626|N62420133|2160.0000|Kg|R ORG APL SCN|OL|SFG|M01|CT1|09/02/2026"],
        footer=None))
    assert semi["bbd"].iloc[0] == pd.Timestamp("2026-09-02")
    dev = load_jestkexp(_w(tmp_path, "dev.csv", [
        ";".join(f"Column{i}" for i in range(1, 12)),
        "730001;CINNAMON;1;Ava;AVA;16/04/2027;N1;3.24;Kg;M01;CINN",
        "730001;CINNAMON;2;Ava;AVA;05/06/2027;N2;3.24;Kg;M01;CINN"]))
    assert dev["bbd"].tolist() == [pd.Timestamp("2027-04-16"),
                                   pd.Timestamp("2027-06-05")]


# ---- PKG-REC receipt slips --------------------------------------------------

def test_pkg_rec_skips_preamble_and_header_rows(tmp_path):
    """PKG-REC.csv: only the lines that split into exactly 9 ';' fields are
    data, minus the header ('Receipt Date;Item;...') which also has 9.
    receipt_date is MM/DD/YYYY, qty strips the thousands comma, po8 is the
    last 8 digits of the PO field (plain or '2 02 1ACDE 30043986')."""
    df = load_pkg_rec(_w(tmp_path, "PKG-REC.csv", PKG_REC, footer=None,
                         newline="\n"))
    assert list(df.columns) == PKG_REC_COLS
    assert len(df) == 3
    assert is_datetime64_any_dtype(df["receipt_date"])
    assert df["receipt_date"].dt.month.tolist() == [9, 9, 9]
    assert df["receipt_date"].dt.day.tolist() == [12, 13, 15]
    assert df["qty"].tolist() == [2400.0, 35200.0, 9600.0]
    assert df["po8"].tolist() == ["30044052", "30043986", "30043986"]
    assert df["slip"].tolist() == ["30120501", "30119536", "30119536"]
    assert df["user"].tolist() == ["DWOODSTR", "OSANTOS", "OSANTOS"]
    assert df["batch"].tolist() == ["N62550171", "N62580454", "N62580453"]
    assert (df["source"] == "PKG-REC.csv").all()


def test_pkg_rec_with_no_slips_is_empty_not_an_error(tmp_path):
    """A receipt window with no slips prints only the preamble + header."""
    df = load_pkg_rec(_w(tmp_path, "PKG-REC.csv", PKG_REC[:10], footer=None,
                         newline="\n"))
    assert df.empty and list(df.columns) == PKG_REC_COLS
    assert df.attrs["empty_export"]


# ---- import_vif_folder ------------------------------------------------------

def test_import_new_drop_frame_keys_notes_and_helpers(tmp_path):
    """The 2026-09-15 drop imports under the CONTRACT keys: the BOM from
    ediact.csv sits at 'ediact 3.csv', no 'ediact 4.csv' key, the six lot
    files under their own names, receipts at 'PKG-REC.csv'; the aliases
    (jestkexp4, jestksa), the stale jestkex2 and the empty reports are not
    frames but notes; dropped duplicates are noted; no errors. Like the real
    drop it has no 'ediact 4.csv', so the HSM750216 the slurry consumes has
    no recipe: one note + missing_recipes (2026-09-16)."""
    snap = import_vif_folder(_new_drop(tmp_path))
    assert snap.errors == []
    assert snap.missing_recipes == ["HSM750216"]
    assert ("semi-finished recipes missing: 1 - HSM750216 "
            "(ediact 4.csv not in the folder)") in snap.notes
    assert set(snap.frames) == {"ediact 3.csv", *LOT_FILES, "PKG-REC.csv",
                                "azapart.csv", "rmpkitems.csv"}
    bom = snap.frames["ediact 3.csv"]
    assert len(bom) == EDIACT_NEW_UNIQUE and (bom["flow_type"] == "").all()
    assert [n for n, _ in lot_frames(snap)] == list(LOT_FILES)
    for name, df in lot_frames(snap):
        assert list(df.columns)[:len(LOT_COLS)] == LOT_COLS
        assert (df["source"] == name).all()
    assert len(receipts_frame(snap)) == 3
    assert len(snap.frames["azapart.csv"]) == 2
    assert snap.frames["rmpkitems.csv"]["item"].tolist() == ["730001", "754751"]
    assert f"ediact.csv: dropped {EDIACT_NEW_DUPS} exact duplicate rows" in snap.notes
    assert "jestkexp4.csv: ignored (jestkexp.csv present, same lots)" in snap.notes
    assert "jestksa.csv: ignored (jestksav.csv present, same lots)" in snap.notes
    assert any(n.startswith("jestkex2.csv: ignored") for n in snap.notes)
    assert "jestkami.csv: empty export" in snap.notes
    assert "jestktgb.csv: empty export" in snap.notes
    assert len(snap.notes) == 7
    # every present file — loaded, alias or ignored — is mtime-stamped under
    # its REAL name so the refresh gate (VIF_FILES) sees any re-export
    assert set(snap.source_files) == {
        "ediact.csv", "jestkexp.csv", "jestkexp4.csv", "jestkexp2.csv",
        "jestkamb.csv", "jestkexq.csv", "jestksav.csv", "jestksa.csv",
        "jestkexp5.csv", "jestkex2.csv", "jestkami.csv", "jestktgb.csv",
        "PKG-REC.csv", "azapart.csv", "rmpkitems.csv"}
    assert set(snap.source_files) <= set(VIF_FILES)


def test_import_uses_aliases_only_when_canonical_files_are_missing(tmp_path):
    """jestkexp4.csv stands in for jestkexp.csv and jestksa.csv for
    jestksav.csv — under the CANONICAL key, with the real file in 'source'
    and a note; jestksa brings no provenance extras. The alias satisfies the
    jestkexp.csv requirement; jestkexp2.csv / azapart.csv are still missing
    errors (2026-09-16)."""
    _w(tmp_path, "ediact.csv", EDIACT_NEW)
    _w(tmp_path, "jestkexp4.csv", JESTKEXP)
    _w(tmp_path, "jestksa.csv", JESTKSA)
    snap = import_vif_folder(tmp_path)
    assert snap.errors == [f"jestkexp2.csv: missing from {tmp_path}",
                           f"azapart.csv: missing from {tmp_path}"]
    assert set(snap.frames) == {"ediact 3.csv", "jestkexp.csv", "jestksav.csv"}
    assert (snap.frames["jestkexp.csv"]["source"] == "jestkexp4.csv").all()
    assert (snap.frames["jestksav.csv"]["source"] == "jestksa.csv").all()
    assert list(snap.frames["jestksav.csv"].columns) == LOT_COLS
    assert "jestkexp.csv missing: loaded jestkexp4.csv in its place" in snap.notes
    assert "jestksav.csv missing: loaded jestksa.csv in its place" in snap.notes
    assert [n for n, _ in lot_frames(snap)] == ["jestkexp.csv", "jestksav.csv"]
    # alias-loaded frames are stamped under the canonical name too (the
    # report's rm/apples export stamp reads that key), alias kept (2026-09-16)
    assert "jestkexp4.csv" in snap.source_files and "jestksa.csv" in snap.source_files
    assert snap.source_files["jestkexp.csv"] == snap.source_files["jestkexp4.csv"]
    assert snap.source_files["jestksav.csv"] == snap.source_files["jestksa.csv"]


def test_alias_loaded_lot_frame_carries_the_alias_mtime_under_its_canonical_key(tmp_path):
    """Rule (2026-09-16): when jestkexp4.csv stands in for jestkexp.csv (or
    jestksa.csv for jestksav.csv) source_files[<canonical>] is the ALIAS
    file's mtime — stock_check_report stamps the rm frame from
    source_files['jestkexp.csv'] and read '' before. When the canonical file
    exists its own mtime is used and the ignored alias keeps its own."""
    import os
    import time as _time
    _w(tmp_path, "ediact.csv", EDIACT_NEW)
    alias = _w(tmp_path, "jestkexp4.csv", JESTKEXP)
    t = _time.mktime((2026, 9, 15, 19, 10, 0, 0, 0, -1))
    os.utime(alias, (t, t))
    want = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(t))
    snap = import_vif_folder(tmp_path)
    assert snap.source_files["jestkexp.csv"] == want
    assert snap.source_files["jestkexp4.csv"] == want

    canon = _w(tmp_path, "jestkexp.csv", JESTKEXP)
    t2 = _time.mktime((2026, 9, 16, 19, 12, 0, 0, 0, -1))
    os.utime(canon, (t2, t2))
    snap2 = import_vif_folder(tmp_path)
    assert snap2.source_files["jestkexp.csv"] == _time.strftime(
        "%Y-%m-%d %H:%M:%S", _time.localtime(t2))
    assert snap2.source_files["jestkexp4.csv"] == want
    assert (snap2.frames["jestkexp.csv"]["source"] == "jestkexp.csv").all()


def test_import_old_names_keep_their_keys_and_bom_file_precedence(tmp_path):
    """A pre-2026-09 folder ('ediact 3.csv' + 'ediact 4.csv') imports under
    the same keys as before; when both BOM generations sit side by side
    ediact.csv wins and 'ediact 3.csv' is noted as ignored. ediact 4.csv
    supplies the HSM750216 recipe the new export lacks: no recipe note."""
    _w(tmp_path, "ediact 3.csv", EDIACT_OLD, footer="Page1/1 ;;;;;;;;;")
    _w(tmp_path, "ediact 4.csv", EDIACT4_OLD, footer="Page1/1 ;;;;;;;;;")
    _w(tmp_path, "jestkexp.csv", JESTKEXP)
    _w(tmp_path, "jestkexp2.csv", JESTKEXP2)
    _w(tmp_path, "azapart.csv", AZAPART, footer=None)
    assert find_bom_file(tmp_path) == tmp_path / "ediact 3.csv"
    snap = import_vif_folder(tmp_path)
    assert snap.errors == [] and snap.notes == []
    assert set(snap.frames) == {"ediact 3.csv", "ediact 4.csv", "jestkexp.csv",
                                "jestkexp2.csv", "azapart.csv"}
    assert (snap.frames["ediact 3.csv"]["flow_type"] == "Pull").all()
    assert "flow_type" not in snap.frames["ediact 4.csv"].columns
    assert receipts_frame(snap) is None

    _w(tmp_path, "ediact.csv", EDIACT_NEW)
    assert find_bom_file(tmp_path) == tmp_path / "ediact.csv"
    snap2 = import_vif_folder(tmp_path)
    assert len(snap2.frames["ediact 3.csv"]) == EDIACT_NEW_UNIQUE
    assert "ediact 3.csv: ignored (ediact.csv is the BOM export)" in snap2.notes
    assert {"ediact.csv", "ediact 3.csv"} <= set(snap2.source_files)
    assert snap2.errors == [] and snap2.missing_recipes == []
    assert not any(n.startswith("semi-finished recipes missing") for n in snap2.notes)


def test_import_missing_required_files_are_errors_optional_are_absent(tmp_path):
    """Rule (2026-09-16): a REQUIRED export missing is an error
    '<name>: missing from <folder>' (the pre-drop loader's '[Errno 2]' fed
    the Stock Check import-error chip; the drop loader had gone silent):
    a BOM (either generation), jestkexp.csv (or its alias), jestkexp2.csv,
    azapart.csv. Optional exports (jestkamb, jestkexq, jestksav, jestkexp5,
    PKG-REC, ediact 4, rmpkitems) missing stay silent — no error, no note."""
    assert REQUIRED_FILES == ("ediact.csv", "jestkexp.csv", "jestkexp2.csv",
                              "azapart.csv")
    _w(tmp_path, "ediact.csv", EDIACT_NEW)
    snap = import_vif_folder(tmp_path)
    assert snap.errors == [f"jestkexp.csv: missing from {tmp_path}",
                           f"jestkexp2.csv: missing from {tmp_path}",
                           f"azapart.csv: missing from {tmp_path}"]
    assert set(snap.frames) == {"ediact 3.csv"}
    assert lot_frames(snap) == [] and receipts_frame(snap) is None
    # the optional ones never surface, in errors or notes
    optional = ("jestkamb", "jestkexq", "jestksav", "jestkexp5", "PKG-REC",
                "rmpkitems")
    assert not any(o in e for o in optional for e in snap.errors + snap.notes)
    assert not any(n.startswith("ediact 4.csv") for n in snap.errors + snap.notes)
    # every required file present -> no error, optional still absent
    _w(tmp_path, "jestkexp.csv", JESTKEXP)
    _w(tmp_path, "jestkexp2.csv", JESTKEXP2)
    _w(tmp_path, "azapart.csv", AZAPART, footer=None)
    assert import_vif_folder(tmp_path).errors == []
    # an empty or absent folder: all four required names, BOM first
    assert find_bom_file(tmp_path / "nowhere") is None
    empty = import_vif_folder(tmp_path / "nowhere")
    assert empty.frames == {} and empty.notes == []
    assert [e.split(":", 1)[0] for e in empty.errors] == list(REQUIRED_FILES)
    assert all(e.endswith(f"missing from {tmp_path / 'nowhere'}") for e in empty.errors)


def test_import_unreadable_file_goes_to_errors_without_mtime(tmp_path):
    """Bytes cp1252 cannot decode -> the file lands in .errors, is absent
    from .frames and gets no mtime (so the next refresh retries it); the
    other files still load. A present-but-unreadable required file is its
    read error, never also a 'missing' error."""
    _w(tmp_path, "ediact.csv", EDIACT_NEW)
    _w(tmp_path, "jestkexp2.csv", JESTKEXP2)
    _w(tmp_path, "azapart.csv", AZAPART, footer=None)
    (tmp_path / "jestkexp.csv").write_bytes(b"Item;Designation\r\n\x81\x8d\x8f\x90\x9d;x\r\n")
    snap = import_vif_folder(tmp_path)
    assert len(snap.errors) == 1 and snap.errors[0].startswith("jestkexp.csv:")
    assert "missing from" not in snap.errors[0]
    assert "jestkexp.csv" not in snap.frames
    assert "jestkexp.csv" not in snap.source_files
    assert "ediact 3.csv" in snap.frames


def test_dev_vif_old_names_still_import_with_the_same_keys():
    """data/stockcheck/dev_vif (old names, pre-2026-09 layouts) must import
    exactly as before the drop: the six legacy keys, no errors, no notes,
    flow_type 'Pull', lot frames on the LOT contract, footer rows gone."""
    snap = import_vif_folder(DEV_VIF)
    assert snap.errors == [] and snap.notes == []
    assert snap.missing_recipes == []     # ediact 4 covers every qty-bearing HSM/RF
    assert set(snap.frames) == {"ediact 3.csv", "ediact 4.csv", "jestkexp.csv",
                                "jestkexp2.csv", "azapart.csv", "rmpkitems.csv"}
    bom = snap.frames["ediact 3.csv"]
    assert len(bom) > 14000 and (bom["flow_type"] == "Pull").all()
    assert list(bom.columns) == EDIACT_COLS
    assert not bom["PF"].str.startswith("Page").any()
    assert bom.attrs["dropped_duplicates"] == 0
    assert list(snap.frames["ediact 4.csv"].columns) == EDIACT4_COLS + ["freinte"]
    assert [n for n, _ in lot_frames(snap)] == ["jestkexp.csv", "jestkexp2.csv"]
    for name, df in lot_frames(snap):
        _check_lot_contract(df, name)
    assert len(snap.frames["jestkexp.csv"]) > 13000
    assert len(snap.frames["jestkexp2.csv"]) > 4000
    assert set(snap.frames["jestkexp.csv"]["depot"]) == {"M01", "M02", "SB1", "SC1", "SF1"}
    assert set(snap.frames["jestkexp2.csv"]["depot"]) == {"SFG", "M21"}
    assert set(snap.source_files) == set(snap.frames)


def _required(folder: Path) -> Path:
    """The required exports besides the BOM, so a test can pin errors == []."""
    folder.mkdir(parents=True, exist_ok=True)
    _w(folder, "jestkexp.csv", JESTKEXP)
    _w(folder, "jestkexp2.csv", JESTKEXP2)
    _w(folder, "azapart.csv", AZAPART, footer=None)
    return folder


# ---- semi-finished recipes (2026-09-16) -------------------------------------

def test_ediact_alone_notes_the_missing_semi_finished_recipes(tmp_path):
    """Rule (2026-09-16): ediact.csv does NOT carry the HSM/RF recipes (24
    of the 27 ediact 4 activities are absent on the real drop). Imported
    alone, the BOM consumes HSM750216 with no activity for it: ONE note
    starting 'semi-finished recipes missing:' with the count, the codes and
    the '(ediact 4.csv not in the folder)' hint, and snap.missing_recipes
    lists the codes — never an error (the refresh gate re-imports errored
    files every call)."""
    snap = import_vif_folder(_w(_required(tmp_path), "ediact.csv", EDIACT_NEW).parent)
    assert snap.errors == []
    assert snap.missing_recipes == ["HSM750216"]
    recipe = [n for n in snap.notes if n.startswith("semi-finished recipes missing:")]
    assert recipe == ["semi-finished recipes missing: 1 - HSM750216 "
                      "(ediact 4.csv not in the folder)"]


def test_ediact_with_ediact4_supplement_has_no_recipe_note(tmp_path):
    """Rule (2026-09-16): 'ediact 4.csv' is the recipe SUPPLEMENT, loaded
    beside ediact.csv whenever present; with it every qty-bearing HSM input
    has a recipe -> no note, missing_recipes == [], and the frame sits under
    'ediact 4.csv' for BomGraph."""
    _required(tmp_path)
    _w(tmp_path, "ediact.csv", EDIACT_NEW)
    _w(tmp_path, "ediact 4.csv", EDIACT4_OLD, footer="Page1/1 ;;;;;;;;;")
    snap = import_vif_folder(tmp_path)
    assert snap.errors == [] and snap.missing_recipes == []
    assert not any(n.startswith("semi-finished recipes missing") for n in snap.notes)
    assert set(snap.frames["ediact 4.csv"]["Activity"]) == {"HSM750216"}


def test_recipe_note_lists_eight_codes_then_a_count_and_no_hint_with_ediact4(tmp_path):
    """The note names at most the first 8 codes (sorted) and '+N more'; the
    '(ediact 4.csv not in the folder)' hint appears only when that file is
    absent — an ediact 4 present but lacking some recipes gets no hint."""
    _required(tmp_path)
    codes = [f"HSM9000{i:02d}" for i in range(10)]
    rows = ["280605;SPTP764154;04/05/2026;R;Output;SPTP764154;R SLURRY;1,000.000;Kg;0.00"]
    rows += [f"280605;SPTP764154;04/05/2026;R;Input;{c};HSM X;10.000;Kg;8.00" for c in codes]
    _w(tmp_path, "ediact.csv", rows)
    _w(tmp_path, "ediact 4.csv", EDIACT4_OLD, footer="Page1/1 ;;;;;;;;;")
    snap = import_vif_folder(tmp_path)
    assert snap.missing_recipes == codes
    [note] = [n for n in snap.notes if n.startswith("semi-finished recipes missing:")]
    assert note == ("semi-finished recipes missing: 10 - "
                    + ", ".join(codes[:8]) + " +2 more")


def test_missing_semi_recipes_counts_qty_bearing_semi_inputs_without_activity():
    """missing_semi_recipes(frames): Input items of frames['ediact 3.csv']
    and frames.get('ediact 4.csv') with a quantity, no Activity in either
    frame and an HSM/RF prefix (case-insensitive) — sorted, unique. A
    blank-qty Input is an ALTERNATE BomGraph never explodes (RF750005 on
    every real export has no recipe anywhere), so it is not counted; a
    purchased code (730001) is never semi-finished; an Activity in ediact 4
    covers an Input of ediact 3 and vice versa."""
    def bom(rows):
        return pd.DataFrame(rows, columns=["PF", "Activity", "item_type", "item",
                                           "qty_act"])
    e3 = bom([
        ("280605", "SPTP764154", "Output", "SPTP764154", 1000.0),
        ("280605", "SPTP764154", "Input", "HSM750216", 441.5),   # recipe in e4
        ("280605", "SPTP764154", "Input", "HSMBT001", 12.0),     # no recipe
        ("280605", "SPTP764154", "Input", "rf730060", 3.0),      # lower-case
        ("280605", "SPTP764154", "Input", "RF750005", float("nan")),  # alternate
        ("280605", "SPTP764154", "Input", "730001", 0.8),        # purchased
        ("280605", "SPTP764154", "Input", "HSMBT001", 5.0),      # repeat
        ("280605", "SPTP764154", "of cost", "HSMCOST", 1.0),     # not an Input
    ])
    e4 = bom([
        ("", "HSM750216", "Output", "HSM750216", 1000.0),
        ("", "HSM750216", "Input", "RF764053", 20.0),            # e4-only input
        ("", "HSM750216", "Input", "SPTP764154", 1.0),           # has activity in e3
    ])
    assert missing_semi_recipes({"ediact 3.csv": e3, "ediact 4.csv": e4}) == \
        ["HSMBT001", "RF764053", "rf730060"]
    assert missing_semi_recipes({"ediact 3.csv": e3}) == \
        ["HSM750216", "HSMBT001", "rf730060"]
    assert missing_semi_recipes({}) == []
    assert missing_semi_recipes({"ediact 3.csv": e3.iloc[0:0]}) == []


# ---- BOM fallback (2026-09-16) ----------------------------------------------

def test_empty_ediact_falls_back_to_a_populated_ediact3(tmp_path):
    """Rule (2026-09-16): the BOM comes from the first of BOM_FILES that
    loads WITH rows. The ERP's 134-byte 'No data corresponds' ediact.csv
    beside a good 'ediact 3.csv' used to win on existence and leave an empty
    BOM; now ediact 3 supplies the frame, a note says why, both files are
    stamped, and ediact 3 is NOT marked 'ignored'. ediact.csv empty alone
    stays an empty frame with its 'empty export' note, not an error."""
    _required(tmp_path)
    (tmp_path / "ediact.csv").write_bytes(EMPTY_REPORT.encode("cp1252"))
    _w(tmp_path, "ediact 3.csv", EDIACT_OLD, footer="Page1/1 ;;;;;;;;;")
    snap = import_vif_folder(tmp_path)
    assert snap.errors == []
    bom = snap.frames["ediact 3.csv"]
    assert len(bom) == 2 and (bom["flow_type"] == "Pull").all()
    assert "ediact.csv: empty export - BOM taken from ediact 3.csv" in snap.notes
    assert not any("ignored" in n for n in snap.notes if n.startswith("ediact"))
    assert not any(n.startswith("ediact.csv: empty export (no rows)") for n in snap.notes)
    assert {"ediact.csv", "ediact 3.csv"} <= set(snap.source_files)

    (tmp_path / "ediact 3.csv").unlink()
    alone = import_vif_folder(tmp_path)
    assert alone.errors == [] and alone.frames["ediact 3.csv"].empty
    assert "ediact.csv: empty export (no rows)" in alone.notes


def test_unreadable_ediact_falls_back_to_ediact3_and_stays_an_error(tmp_path):
    """Rule (2026-09-16, corrected after review): an ediact.csv that raises
    on load (bytes cp1252 cannot decode) beside a good 'ediact 3.csv' -> the
    BOM comes from ediact 3, but the read failure is still an ERROR,
    'ediact.csv: <err> (BOM taken from ediact 3.csv)', with no mtime and no
    'ediact.csv:' note. The refresh gate retries only the files .errors
    names; as a stamped note the file was never retried and the older BOM
    stuck until the next export. Without the fallback the error has no
    suffix and no BOM frame exists."""
    _required(tmp_path)
    (tmp_path / "ediact.csv").write_bytes(b"\x81\x8d\x8f;\x90\x9d;x\r\n" * 3)
    _w(tmp_path, "ediact 3.csv", EDIACT_OLD, footer="Page1/1 ;;;;;;;;;")
    snap = import_vif_folder(tmp_path)
    assert len(snap.frames["ediact 3.csv"]) == 2
    [err] = snap.errors
    assert err.startswith("ediact.csv: ") and "missing from" not in err
    assert err.endswith(" (BOM taken from ediact 3.csv)")
    assert not any(n.startswith("ediact.csv:") for n in snap.notes)
    assert "ediact.csv" not in snap.source_files
    assert "ediact 3.csv" in snap.source_files

    (tmp_path / "ediact 3.csv").unlink()
    alone = import_vif_folder(tmp_path)
    assert "ediact 3.csv" not in alone.frames
    assert len(alone.errors) == 1 and alone.errors[0].startswith("ediact.csv: ")
    assert "missing from" not in alone.errors[0]
    assert "BOM taken from" not in alone.errors[0]
    assert "ediact.csv" not in alone.source_files


def test_refresh_retries_a_locked_ediact_after_ediact3_stood_in(tmp_path, monkeypatch):
    """Rule (2026-09-16): an ediact.csv that fails to load while 'ediact
    3.csv' supplies the BOM must be retried by api.refresh_vif_snapshot.
    Excel locks ediact.csv on call 1 (PermissionError): the BOM comes from
    ediact 3, the error names ediact.csv and ediact.csv stays out of the
    signed gate_mtimes. The lock is released with the file UNCHANGED (same
    mtime): call 2 must re-import and take the BOM from ediact.csv (blank
    flow_type, deduped rows) instead of returning the saved ediact 3
    snapshot; call 3 is gated again."""
    import stockcheck.vif_import as vi
    from stockcheck import api

    vif = _required(tmp_path / "vif")
    _w(vif, "ediact.csv", EDIACT_NEW)
    _w(vif, "ediact 3.csv", EDIACT_OLD, footer="Page1/1 ;;;;;;;;;")
    real = vi.load_ediact
    locked = {"on": True}

    def excel_lock(p, *args, **kwargs):
        if locked["on"] and Path(p).name == "ediact.csv":
            raise PermissionError(13, "Permission denied", str(p))
        return real(p, *args, **kwargs)

    monkeypatch.setattr(vi, "load_ediact", excel_lock)
    data = tmp_path / "data"
    first = api.refresh_vif_snapshot(vif, data)
    [err] = first.errors
    assert err.startswith("ediact.csv: [Errno 13]")
    assert err.endswith(" (BOM taken from ediact 3.csv)")
    assert (first.frames["ediact 3.csv"]["flow_type"] == "Pull").all()
    assert "ediact.csv" not in first.gate_mtimes
    assert "ediact 3.csv" in first.gate_mtimes

    locked["on"] = False
    second = api.refresh_vif_snapshot(vif, data)
    assert second.errors == []
    bom = second.frames["ediact 3.csv"]
    assert len(bom) == EDIACT_NEW_UNIQUE and (bom["flow_type"] == "").all()
    assert "ediact.csv" in second.gate_mtimes
    assert "ediact 3.csv: ignored (ediact.csv is the BOM export)" in second.notes

    monkeypatch.setattr(api, "import_vif_folder",
                        lambda f: pytest.fail("a gated folder was re-imported"))
    third = api.refresh_vif_snapshot(vif, data)
    assert third.imported_at == second.imported_at
    assert len(third.frames["ediact 3.csv"]) == EDIACT_NEW_UNIQUE


# ---- cross-file lot duplicates (2026-09-16) ---------------------------------

def test_lot_listed_in_two_exports_is_kept_in_the_first_only(tmp_path, monkeypatch):
    """Rule (2026-09-16, rationale corrected after review): a row whose
    (item, scn, batch, depot, location) already appears in an EARLIER lot
    frame (LOT_FILES order) is dropped from the later one with a note naming
    both files; distinct lots and within-file repeats stay. The real drop
    lists the same 9 QCR lots in jestkexq.csv (Out) and jestkexp5.csv (OL).
    That never double-counted stock: jestkexp5 is never counted
    (api._NEVER_COUNTED_FRAMES) and QCR/QCP are off by default. What the
    rule changes is lot detail, which now lists each lot once, as the
    jestkexq row (the jestkexp5 'OL' copy is dropped on purpose). Pinned
    here: every lot key sits in ONE frame, lot detail as the page builds it
    shows the jestkexq rows only, the counted frames keep every row (a
    de-dup run the wrong way would empty jestkexq), and counted stock is
    the same with or without the de-dup even with the QCR toggles on."""
    import stockcheck.vif_import as vi
    from stockcheck import api
    from stockcheck import coverage as cov

    _required(tmp_path)
    _w(tmp_path, "ediact.csv", EDIACT_NEW)
    _w(tmp_path, "jestkexq.csv", [
        JESTKEXP2[0],
        "GSSPTP9221SCN;R ORG APL SCN;1003096509;N62010618;07/23/2026;Out;OL;850.00;Kg;QCR;DES;",
        "GSSPTP9221SCN;R ORG APL SCN;1003096510;N62010619;07/23/2026;Out;OL;850.00;Kg;QCR;DES;",
        "730006;PEACH PUREE SING STR;6002589211;S168137;02/04/2027;Ava;AVA;220.00;Kg;QCR;DES;",
    ])
    _w(tmp_path, "jestkexp5.csv", [
        JESTKEXP5[0],
        "GSSPTP9221SCN|1003096509|N62010618|850.0000|Kg|R ORG APL SCN|OL|SFG|QCR|DES|07/23/2026",
        "GSSPTP9221SCN|1003096510|N62010619|850.0000|Kg|R ORG APL SCN|OL|SFG|QCR|DES|07/23/2026",
        "HSM764059|1003157480|N62540452|500.0000|Kg|ORG SLURRY|AVA|SFG|M12|CT1|09/14/2026",
        "HSM764059|1003157480|N62540452|500.0000|Kg|ORG SLURRY|AVA|SFG|M12|CT1|09/14/2026",
    ], footer=None)
    snap = import_vif_folder(tmp_path)
    assert snap.errors == []
    qc = snap.frames["jestkexq.csv"]
    semi = snap.frames["jestkexp5.csv"]
    assert len(qc) == 3 and qc["status"].tolist()[:2] == ["Out", "Out"]
    assert semi["item"].tolist() == ["HSM764059", "HSM764059"]     # repeat kept
    assert list(semi.columns) == LOT_COLS + ["family"]
    assert "jestkexp5.csv: dropped 2 lots already listed in jestkexq.csv" in snap.notes

    owners: dict[tuple, list[str]] = {}
    for name, df in lot_frames(snap):
        for key in set(zip(*(df[c].astype(str) for c in vi.LOT_KEY))):
            owners.setdefault(key, []).append(name)
    assert all(len(names) == 1 for names in owners.values()), owners

    # lot detail, built the way pages/stock_check._lot_rows builds it
    lots = api._lot_frames(snap)
    rm, pkg = snap.frames["jestkexp.csv"], snap.frames["jestkexp2.csv"]
    extra = [df for n, df in lots if n not in ("jestkexp.csv", "jestkexp2.csv")]
    detail = cov.lot_detail(rm, pkg, "GSSPTP9221SCN", extra=extra)
    assert len(detail) == 2
    assert set(detail["source"]) == {"jestkexq.csv"} and set(detail["status"]) == {"Out"}

    def counted_stock(s):
        counted = api._counted_lot_frames(api._lot_frames(s))
        assert "jestkexp5.csv" not in [n for n, _ in counted]
        ex = [df for n, df in counted if n not in ("jestkexp.csv", "jestkexp2.csv")]
        return cov.available_stock(s.frames["jestkexp.csv"], s.frames["jestkexp2.csv"],
                                   {"QCR|Out": True, "QCR|OL": True}, extra=ex)

    with_dedup = counted_stock(snap)
    assert with_dedup["GSSPTP9221SCN"] == pytest.approx(850.0 * 2)
    monkeypatch.setattr(vi, "_drop_cross_file_lots", lambda frames, notes: None)
    raw = import_vif_folder(tmp_path)
    assert len(raw.frames["jestkexp5.csv"]) == 4          # the overlap is really there
    assert counted_stock(raw) == with_dedup


# ---- snapshot / contract constants ----------------------------------------

def test_snapshot_roundtrip_and_pre_drop_pickle_backfills_notes(tmp_path):
    """save/load keep .notes and .missing_recipes; a snapshot pickled before
    2026-09-15 (no 'notes') or before 2026-09-16 (no 'missing_recipes')
    unpickles with [] for the absent field instead of raising."""
    snap = import_vif_folder(_new_drop(tmp_path / "vif"))
    d = save_snapshot(snap, tmp_path / "snapshots")
    back = load_latest_snapshot(tmp_path / "snapshots")
    assert d.is_dir() and back is not None
    assert back.notes == snap.notes and set(back.frames) == set(snap.frames)
    assert back.missing_recipes == snap.missing_recipes == ["HSM750216"]
    assert back.frames["ediact 3.csv"].attrs["dropped_duplicates"] == EDIACT_NEW_DUPS
    old = VifSnapshot.__new__(VifSnapshot)
    old.__dict__.update({"frames": {}, "source_files": {}, "imported_at": "x",
                         "errors": []})          # pre-drop state, no notes
    revived = pickle.loads(pickle.dumps(old))
    assert revived.notes == [] and revived.missing_recipes == []
    mid = VifSnapshot.__new__(VifSnapshot)
    mid.__dict__.update({"frames": {}, "source_files": {}, "imported_at": "x",
                         "errors": [], "notes": ["n"]})   # 2026-09-15 state
    revived = pickle.loads(pickle.dumps(mid))
    assert revived.notes == ["n"] and revived.missing_recipes == []
    fresh = VifSnapshot(frames={}, source_files={}, imported_at="y")
    assert fresh.notes == [] and fresh.missing_recipes == []


def test_vif_files_lists_every_name_old_and_new():
    """VIF_FILES is the mtime-gating list: the two BOM generations, ediact 4,
    every lot file and alias, the receipts, azapart/rmpkitems and the
    ignored names, each once; the six legacy names are still in it."""
    assert BOM_FILES == ("ediact.csv", "ediact 3.csv")
    assert len(set(VIF_FILES)) == len(VIF_FILES)
    assert {"ediact 3.csv", "ediact 4.csv", "jestkexp.csv", "jestkexp2.csv",
            "azapart.csv", "rmpkitems.csv"} <= set(VIF_FILES)
    assert set(BOM_FILES) | set(LOT_FILES) | {"jestkexp4.csv", "jestksa.csv",
                                             "PKG-REC.csv", "jestkex2.csv",
                                             "jestkami.csv"} <= set(VIF_FILES)
