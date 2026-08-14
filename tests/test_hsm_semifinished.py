# tests/test_hsm_semifinished.py — HSM = semi-finished goods (user rule
# 2026-08-14): an HSM ingredient never counts against a SKU by its own stock;
# its ediact-4 recipe explodes through to the sub-components, and only THEY
# can flag the SKU short.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from stockcheck.bom import BomGraph  # noqa: E402
from stockcheck.vif_import import load_ediact4  # noqa: E402


def _e3():
    """FG 120430 (100 CAS batch) needs 50 kg of HSM764059 + 10 EA of 735009."""
    cols = ["PF", "Activity", "flow_type", "effective_date", "family",
            "item_type", "item", "designation", "qty_act", "unit"]
    rows = [
        ["120430", "120430", "Pull", None, "FG", "Output", "120430", "FG X", 100.0, "CAS"],
        ["120430", "120430", "Pull", None, "FG", "Input", "HSM764059", "ORG SLURRY", 50.0, "Kg"],
        ["120430", "120430", "Pull", None, "FG", "Input", "735009", "TRIM SHEET", 10.0, "EA"],
    ]
    return pd.DataFrame(rows, columns=cols)


def _e4():
    """HSM764059: per 1000 kg batch — 300 kg fiber + 700 kg puree."""
    cols = ["PF", "Activity", "effective_date", "family",
            "item_type", "item", "designation", "qty_act", "unit"]
    rows = [
        ["", "HSM764059", None, "RF", "Output", "HSM764059", "ORG SLURRY", 1000.0, "Kg"],
        ["", "HSM764059", None, "RF", "Input", "750035", "ACACIA FIBER", 300.0, "Kg"],
        ["", "HSM764059", None, "RF", "Input", "730009", "APPLE PUREE", 700.0, "Kg"],
        ["", "HSM764059", None, "RF", "of cost", "APPLE BRIX", "EFFECT", None, "U"],
    ]
    return pd.DataFrame(rows, columns=cols)


def test_hsm_explodes_through_to_subcomponents():
    bom = BomGraph(_e3(), _e4())
    res = bom.explode("120430", 200)  # 2x the 100-CAS batch -> 100 kg HSM
    items = {g.primary_item: g for g in res.requirements}
    # the HSM itself must NOT be a requirement
    assert "HSM764059" not in items
    # its sub-components are, scaled through the 1000 kg recipe basis
    assert items["750035"].need_qty == 30.0   # 100 kg x 300/1000
    assert items["730009"].need_qty == 70.0   # 100 kg x 700/1000
    # unrelated packaging requirement untouched
    assert items["735009"].need_qty == 20.0
    assert res.status == "OK"


def test_hsm_without_recipe_stays_a_leaf_not_an_error():
    bom = BomGraph(_e3(), None)  # no ediact 4 at all
    res = bom.explode("120430", 100)
    items = {g.primary_item for g in res.requirements}
    # without a recipe the HSM stays visible as its own (untracked) line —
    # coverage renders NOT_TRACKED chips without poisoning the SKU status
    assert "HSM764059" in items
    assert res.status == "OK"


def test_ediact3_activity_wins_on_code_collision():
    e4 = _e4()
    e4.loc[len(e4)] = ["", "120430", None, "RF", "Input", "999999", "GHOST", 1.0, "Kg"]
    bom = BomGraph(_e3(), e4)
    res = bom.explode("120430", 100)
    items = {g.primary_item for g in res.requirements}
    assert "999999" not in items  # ediact 3's 120430 activity kept


def test_load_ediact4_parses_live_layout(tmp_path):
    p = tmp_path / "ediact 4.csv"
    p.write_text(
        "PF;Activity;Effective date;Famille;Item type;Item;Designation;"
        "Quantity (ACT);;Freinte\n"
        ";HSM750216;01/01/2026;RF;Input;750145;L-MALIC ACID;19.300;Kg;8.10\n"
        ";HSM750216;01/01/2026;RF;Output;HSM750216;SLURRY;1,000.000;Kg;0.00\n",
        encoding="cp1252")
    df = load_ediact4(p)
    assert list(df["Activity"].unique()) == ["HSM750216"]
    out = df[df["item_type"] == "Output"].iloc[0]
    assert out["qty_act"] == 1000.0  # thousands comma parsed
    assert df[df["item_type"] == "Input"].iloc[0]["qty_act"] == 19.3
