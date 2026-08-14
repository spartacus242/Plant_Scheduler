# tests/test_vif_live_formats.py — live VIF export format quirks (P1 live
# link, 2026-08-14). The real plant export differs from the dev fixtures in
# three ways that each silently broke the stock check:
#   1. thousands commas in quantities ('1,600') -> to_numeric NaN -> 8,403 of
#      15,267 BOM quantities lost -> every explosion UNK;
#   2. mixed date conventions in ONE export set (ediact effective_date
#      day-first, jestkexp BBD month-first) -> half the BBDs NaT;
#   3. azapart.csv has NO header row -> reading with header=0 ate the first
#      SKU (030480) as the header.
# All fixtures are inline; nothing reads data/reference (guard-compliant).

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from stockcheck.vif_import import (  # noqa: E402
    load_azapart,
    load_ediact,
    load_jestkexp,
)


def test_ediact_qty_with_thousands_comma_parses(tmp_path):
    p = tmp_path / "ediact 3.csv"
    p.write_text(
        "PF;Activity;Type;Effective date;Famille;Item type;Item;Designation;"
        "Quantity (ACT);;Freinte\n"
        "120430;120430;Pull;01/01/2026;FG;Output;120430;X;1,600;CAS;0.00\n"
        "120430;120430;Pull;01/01/2026;FG;Input;735009-A;Y;10;EA;0.75\n",
        encoding="cp1252")
    df = load_ediact(p)
    assert df.loc[df["item"] == "120430", "qty_act"].iloc[0] == 1600.0
    assert df["qty_act"].notna().all()


def test_ediact_effective_date_stays_dayfirst(tmp_path):
    # 22/07/2026 only parses day-first; the detector must keep the whole
    # column day-first (the live ediact measured 1,293 day-first proofs).
    p = tmp_path / "ediact 3.csv"
    p.write_text(
        "PF;Activity;Type;Effective date;Famille;Item type;Item;Designation;"
        "Quantity (ACT);;Freinte\n"
        "A;A;Pull;22/07/2026;FG;Output;A;X;5;CAS;0\n"
        "A;A;Pull;03/08/2026;FG;Input;B;Y;1;EA;0\n",
        encoding="cp1252")
    df = load_ediact(p)
    assert df["effective_date"].iloc[0].month == 7
    assert df["effective_date"].iloc[1].month == 8  # 3 Aug, not 8 Mar


def test_jestkexp_us_bbd_autodetects_monthfirst(tmp_path):
    # 04/16/2027 only parses month-first (live jestkexp: 10,259 proofs).
    p = tmp_path / "jestkexp.csv"
    p.write_text(
        "Item;Designation;SCN;Status;Statut;BBD;BATCH;Qty (MU);Unit;Depot;"
        "Location;PreCaut;SupplierBatch\n"
        "730001;CINNAMON;1;Ava;AVA;04/16/2027;N1;3.24;Kg;M01;CINN;;S1\n"
        "730001;CINNAMON;2;Ava;AVA;12/01/2027;N2;1,022.5;Kg;M01;CINN;;S2\n",
        encoding="cp1252")
    df = load_jestkexp(p)
    assert df["bbd"].iloc[0].month == 4 and df["bbd"].iloc[0].day == 16
    # month-first applies to the whole column: 12/01 = Dec 1, not Jan 12
    assert df["bbd"].iloc[1].month == 12
    # thousands comma in stock qty parses too
    assert df["qty"].iloc[1] == 1022.5


def test_azapart_without_header_keeps_first_sku(tmp_path):
    p = tmp_path / "azapart.csv"
    p.write_text(
        "030480         ;48X90 APL MEX GGS;CAS;Kg ;1 CAS = 4.320 Kg;"
        "1.000 CNT = 180 CAS;  4.320;\n"
        "120340         ;12x90 ASTB CAN GGS;CAS;Kg ;1 CAS = 6.480 Kg;"
        "1.000 CNT = 108 CAS;  6.480;12x90G\n",
        encoding="cp1252")
    df = load_azapart(p)
    assert set(df["sku"]) == {"030480", "120340"}  # first SKU not eaten
    assert df.loc[df["sku"] == "030480", "kg_per_case"].iloc[0] == 4.32


def test_azapart_dev_fixture_column1_header_still_dropped(tmp_path):
    p = tmp_path / "azapart.csv"
    p.write_text(
        "Column1;Column2;Column3;Column4;Column5;Column6;Column7;Column8\n"
        "30480;48X90;CAS;Kg ;1 CAS = 4.320 Kg;1 CNT;4.32;\n",
        encoding="cp1252")
    df = load_azapart(p)
    assert list(df["sku"]) == ["30480"]  # junk header row filtered, data kept
