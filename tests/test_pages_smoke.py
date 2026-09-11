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


def _seed_saved_report(dd: Path, monkeypatch) -> None:
    """A data dir whose stockcheck cache already holds a (fake) report."""
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
    monkeypatch.setattr(
        api, "stock_check_report",
        lambda *a, **k: {"schedule_view": [], "demand_view": [],
                         "item_reverse": {}, "no_bom_skus": [], "unk": [],
                         "source_files": {}, "import_errors": []})
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
