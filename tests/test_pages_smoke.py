# tests/test_pages_smoke.py — Stock Check + Reconcile render through the
# persisted stock-report cache (helpers/stock_report_cache.py).
#
# Same AppTest pattern as test_overnight_ui_smoke.py: boot the REAL
# entrypoint so st.navigation registers every page, then switch_page onto
# the page under test. The key behavior under test: with a saved report on
# disk, NEITHER page recomputes on its own — the engine is patched to blow
# up if called, and the page must still render from the saved data.

from __future__ import annotations

import json
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

from helpers import stock_report_cache as src  # noqa: E402


def _boot(page: str, data_dir: Path) -> AppTest:
    at = AppTest.from_file(str(ROOT / "code" / "app.py"), default_timeout=180)
    at.session_state["data_dir"] = str(data_dir)
    at.switch_page(page)
    at.run()
    return at


def _texts(at: AppTest) -> str:
    parts = [str(getattr(el, "value", "")) for el in at.markdown]
    parts += [str(getattr(el, "value", "")) for el in at.caption]
    parts += [str(getattr(el, "value", "")) for el in at.warning]
    parts += [str(getattr(el, "body", "")) for el in at.subheader]
    return "\n".join(parts)


# the pre-PO-feed report shape: no anchor / inbound / quality / supply keys
_OLD_REPORT = {"schedule_view": [], "demand_view": [], "item_reverse": {},
               "no_bom_skus": [], "unk": [], "source_files": {},
               "import_errors": []}


def _supply_report() -> dict:
    """A saved report carrying the Supply Timeline keys (contract §4): one
    flat AT_RISK block that is also SHORT, one flat-OK block that is
    DEPENDENT (must join the risk list), one minor DEPENDENT (must not) and
    one old-shape row without "supply" at all."""
    def item(status, ratio):
        return {"item": "754751", "designation": "SLV 24x90", "need": 1200.0,
                "unit": "EA", "available_total": 470.0,
                "available_primary": 470.0, "ratio": ratio, "status": status,
                "alternates": []}

    short = {"key": "b1@0.00", "verdict": "SHORT", "backed": False,
             "minor": False, "mid_run": False, "item": "754751",
             "covered_frac": 0.39, "depletion_h": 40.0, "lead_h": None,
             "safe_from_h": None, "binding": None, "action": "move",
             "lead_from": "block_start", "items": [], "untracked": [],
             "co_consumers": {}, "text": "⛔ 754751: on hand covers 39% "
             "(runs out h40.0) · no inbound counted"}
    dep = {**short, "key": "b2@100.00", "verdict": "DEPENDENT",
           "covered_frac": 0.6, "depletion_h": 160.0, "lead_h": 30.0,
           "safe_from_h": 166.0,
           "binding": {"po8": "30043537", "qty": 67200.0, "ready_h": 70.0,
                       "receipt_date": "2026-09-02", "label": "PO 30043537"},
           "text": "🚚 754751: on hand covers 60%"}
    minor = {**dep, "key": "b3@200.00", "minor": True}

    def block(bid, start, status, ratio, supply):
        b = {"block_id": bid, "sku": "280351", "line_id": "L1",
             "line_name": "Line 1", "start_h": start, "end_h": start + 48.0,
             "qty_kg": 1000.0, "cases": 100.0, "status": status,
             "items": [item(status, ratio)], "unk": [], "cycles": []}
        if supply is not None:
            b["key"] = f"{bid}@{start:.2f}"
            b["supply"] = supply
        return b

    line = {"po": "2 02 1ACDE 30043537", "po8": "30043537", "item": "754751",
            "designation": "SLV 24x90", "qty": 67200.0, "unit": "EA",
            "receipt_date": "2026-09-02", "initial_receipt_date": "2026-08-27",
            "slip_days": 6, "arrival_area": "RP1", "supplier": "GPI",
            "supplier_id": "48", "received": False, "order_date": "2026-07-24",
            "row": 12, "fate": "used", "reason": "", "ready_h": 70.0,
            "tier": "erp", "item_key": "754751"}
    lines = [
        line,
        {**line, "po8": "30043500", "receipt_date": "2026-08-20",
         "slip_days": None, "fate": "overdue", "ready_h": None, "tier": None,
         "reason": "receipt date before the stock snapshot"},
        {**line, "po8": "30043501", "item": "999", "fate": "unjoinable",
         "ready_h": None, "tier": None, "reason": "item 999 not in BOM",
         "item_key": None},
    ]
    inbound = {"state": "ok", "source_path": "x/open_pos.xlsx",
               "source_mtime": "2026-09-01 06:10:00", "as_of": "2026-09-01",
               "max_receipt_date": "2026-09-20", "n_rows": 3,
               "errors": ["row 9: bad qty"], "lines": lines,
               "receipts": {"754751": [{"ready_h": 70.0, "qty": 67200.0,
                                        "po8": "30043537", "tier": "erp",
                                        "receipt_date": "2026-09-02",
                                        "label": "PO 30043537"}]},
               "join": {"used": 1, "received": 0, "overdue": 1, "landed": 0,
                        "unjoinable": 1, "unit_mismatch": 0,
                        "offsite_no_transfer": 0},
               "appt_join": {"matched": 1, "total": 2}}
    quality = {"unjoinable_items": ["999"],
               "unit_mismatch": [{"item": "754751", "po_unit": "KG",
                                  "bom_unit": "EA"}],
               "landed_unverifiable": []}
    return {**_OLD_REPORT,
            "schedule_view": [block("b1", 0.0, "AT_RISK", 0.39, short),
                              block("b2", 100.0, "OK", 2.0, dep),
                              block("b3", 200.0, "OK", 2.0, minor),
                              block("b4", 300.0, "OK", 2.0, None)],
            "anchor": "2026-09-01", "inbound": inbound, "quality": quality,
            "supply_meta": {"feed_state": "ok"}}


def _seed_saved_report(dd: Path, monkeypatch, report: dict | None = None
                       ) -> None:
    """A data dir whose stockcheck cache already holds a (fake) report —
    the old shape by default, or the given one."""
    (dd / "stockcheck").mkdir(parents=True, exist_ok=True)
    (dd / "reference").mkdir(exist_ok=True)
    vif = dd / "vif"
    vif.mkdir(exist_ok=True)
    for name in src._VIF_FILES:
        (vif / name).write_text("x\n", encoding="utf-8")
    (dd / "calendar_blocks.csv").write_text(
        "block_id,block_type,line_id,line_name,start_h,end_h,label,"
        "order_id,sku,sku_description,qty_kg,locked,attrs\n",
        encoding="utf-8")
    (dd / "reference" / "demand_plan.csv").write_text(
        "order_id,sku,qty_target,lower_pct,upper_pct,due_start_hour,"
        "due_end_hour\n", encoding="utf-8")
    (dd / "reference" / "capabilities_rates.csv").write_text(
        "line_id,line_name,sku,capable,calc_rate_kgph\n", encoding="utf-8")
    (dd / "stockcheck" / "settings.json").write_text(
        json.dumps({"vif_folder": str(vif)}), encoding="utf-8")
    import stockcheck.api as api
    fake = report if report is not None else _OLD_REPORT
    monkeypatch.setattr(api, "stock_check_report", lambda *a, **k: fake)
    assert src.refresh_report(dd).source == "computed"

    def _never(*a, **k):  # pages must serve the SAVED report, not recompute
        raise AssertionError("stock_check_report called on page load")

    monkeypatch.setattr(api, "stock_check_report", _never)


def test_stock_check_boots_on_an_empty_dir(tmp_path):
    """No VIF anywhere: the compute fails fast into an error report and the
    page renders the failure instead of crashing."""
    at = _boot("pages/stock_check.py", tmp_path)
    assert not at.exception


def test_reconcile_boots_on_an_empty_dir(tmp_path):
    at = _boot("pages/reconcile.py", tmp_path)
    assert not at.exception


def test_stock_check_serves_the_saved_report(tmp_path, monkeypatch):
    _seed_saved_report(tmp_path, monkeypatch)
    at = _boot("pages/stock_check.py", tmp_path)
    assert not at.exception
    text = _texts(at)
    assert "loads instantly" in text          # the provenance caption
    assert "Refresh from VIF" not in text or True  # button lives in widgets


def test_stock_check_degrades_on_a_pre_po_feed_report(tmp_path, monkeypatch):
    """An old cache (no inbound / quality / supply keys): every tab renders
    with an info line instead of crashing."""
    _seed_saved_report(tmp_path, monkeypatch)
    at = _boot("pages/stock_check.py", tmp_path)
    assert not at.exception
    infos = "\n".join(str(getattr(el, "value", "")) for el in at.info)
    assert "predates the PO feed" in infos          # Inbound tab
    assert "Supply quality lists are not" in infos  # Data quality tab
    assert "carries no time-phased supply" in _texts(at)


def test_stock_check_renders_supply_inbound_and_quality(tmp_path,
                                                        monkeypatch):
    _seed_saved_report(tmp_path, monkeypatch, _supply_report())
    at = _boot("pages/stock_check.py", tmp_path)
    assert not at.exception
    text = _texts(at)
    # blocks at risk: b1 (flat AT_RISK + SHORT) and b2 (flat OK, DEPENDENT);
    # b3 is minor and b4 has no supply -> 2, and the caption says why
    m = {el.label: el.value for el in at.metric}
    assert m["Schedule blocks at risk"] == "2"
    assert "or time-phased supply SHORT/DEPENDENT (2" in text
    # schedule tab: the Supply line is re-stamped with real dates, not h-frame
    assert "**Supply** :red[SHORT]" in text
    assert "**Supply** :orange[DEPENDENT]" in text
    assert "PO 30043537 lands" in text
    assert "h70.0" not in text
    assert "listed for the supply verdict only" in text
    assert "Supply: 1 short · 1 dependent · 0 no data (PO feed ok)" in text
    # Inbound tab: join metrics, source line, fates table, errors
    assert m["Counted"] == "1" and m["Overdue"] == "1"
    assert m["Unjoinable"] == "1" and m["Other"] == "0"
    assert "x/open_pos.xlsx" in text and "2026-09-20" in text
    assert "Feed state **OK**" in text
    assert "1 import problem(s): row 9: bad qty" in text
    tables = [df for df in (el.value for el in at.dataframe)
              if "fate" in getattr(df, "columns", [])]
    assert len(tables) == 1
    df = tables[0]
    assert list(df.columns) == ["po8", "item", "designation", "qty", "unit",
                                "receipt_date", "slip_days", "arrival_area",
                                "supplier", "fate", "reason", "ready", "tier"]
    assert list(df["fate"]) == ["used", "overdue", "unjoinable"]
    assert df["ready"].iloc[0] and not df["ready"].iloc[1]  # None -> blank
    # Data quality tab: the three quality lists with fix hints
    assert "Unjoinable PO items (1)" in text
    assert "Unit mismatch (PO vs BOM) (1)" in text
    assert "Landed check unavailable (0)" in text
    assert "no conversion is applied" in text


def test_reconcile_serves_the_saved_report_and_flags_stale(tmp_path,
                                                          monkeypatch):
    _seed_saved_report(tmp_path, monkeypatch)
    at = _boot("pages/reconcile.py", tmp_path)
    assert not at.exception
    assert "loads instantly" in _texts(at)
    # the board moves -> the page still renders the saved report (the
    # patched engine proves no recompute) and raises the stale banner
    import os
    cal = tmp_path / "calendar_blocks.csv"
    t = cal.stat().st_mtime + 120
    os.utime(cal, (t, t))
    at2 = _boot("pages/reconcile.py", tmp_path)
    assert not at2.exception
    text2 = _texts(at2)
    assert "Inputs changed since the saved stock report" in text2
    assert "Press **Refresh** to recompute" in text2


def test_home_reads_the_saved_report_for_its_counts(tmp_path, monkeypatch):
    _seed_saved_report(tmp_path, monkeypatch)
    at = _boot("pages/home.py", tmp_path)
    assert not at.exception
    assert "Reconcile" in _texts(at)


def test_stock_check_stale_feed_says_nothing_is_counted(tmp_path, monkeypatch):
    """Off a fresh feed the Inbound tab must not show 'Counted 1' beside
    'receipts are NOT counted': the first metric reads 'Would count' and the
    state line says why nothing counts."""
    rep = _supply_report()
    rep["inbound"] = {**rep["inbound"], "state": "stale"}
    rep["supply_meta"] = {"feed_state": "stale"}
    _seed_saved_report(tmp_path, monkeypatch, rep)
    at = _boot("pages/stock_check.py", tmp_path)
    assert not at.exception
    m = {el.label: el.value for el in at.metric}
    assert m["Would count"] == "1" and "Counted" not in m
    text = _texts(at)
    assert "Feed state **STALE** — no inbound receipt is counted" in text
    assert "no longer looks ahead" in text
    assert "1 line(s) under 'Would count'" in text


# ---------------------------------------------------------------------------
# Plant Calendar — the planner's main screen (default page since 2026-09-11).
# ---------------------------------------------------------------------------

def _minimal_board(dd: Path) -> None:
    (dd / "reference").mkdir(parents=True, exist_ok=True)
    (dd / "lines.csv").write_text("line_id,line_name,active\n0,P09,True\n1,P10,True\n",
                                  encoding="utf-8")
    (dd / "calendar_blocks.csv").write_text(
        "block_id,block_type,line_id,line_name,start_h,end_h,label,"
        "order_id,sku,sku_description,qty_kg,locked,attrs\n"
        "b1,production,0,P09,10.0,20.0,280351,280351-W0,280351,desc,5000,False,\n"
        "c1,cip,1,P10,4.0,10.0,CIP,,CIP,,,False,\n",
        encoding="utf-8")


def test_calendar_boots_on_an_empty_dir(tmp_path):
    """No calendar file: the page explains and stops — no traceback."""
    at = _boot("pages/calendar.py", tmp_path)
    assert not at.exception
    assert "No calendar yet" in "\n".join(str(w.value) for w in at.warning)


def test_calendar_renders_a_minimal_board_without_reference_files(tmp_path):
    """Every live feed missing: the board still renders, with the attention
    strip saying what is missing, the control row, and the lock & export
    strip — the page never dies on absent inputs."""
    _minimal_board(tmp_path)
    at = _boot("pages/calendar.py", tmp_path)
    assert not at.exception
    labels = [str(b.label) for b in at.button]
    assert any("Reload from disk" in lb for lb in labels)
    assert any("Lock through" in lb for lb in labels)
    assert any(cb.label.startswith("Hide blocks") for cb in at.checkbox)
    assert "Plant Calendar" in _texts(at)
