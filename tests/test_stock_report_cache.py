# tests/test_stock_report_cache.py — the ONE persisted stock report every
# page shares (helpers/stock_report_cache.py). The ~35s compute is faked for
# the behavior tests; one integration test crunches the bundled dev_vif
# fixtures end-to-end into a tmp data dir.

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers import stock_report_cache as src  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEV_VIF = ROOT / "data" / "stockcheck" / "dev_vif"


@pytest.fixture()
def dd(tmp_path):
    """Self-contained data dir: fake VIF folder + the three report inputs,
    settings.json pointing the shared resolver at the fake folder."""
    d = tmp_path / "data"
    (d / "stockcheck").mkdir(parents=True)
    (d / "reference").mkdir()
    vif = d / "vif"
    vif.mkdir()
    for name in src._VIF_FILES:
        (vif / name).write_text("x\n", encoding="utf-8")
    (d / "calendar_blocks.csv").write_text(
        "block_id,block_type,line_id,line_name,start_h,end_h,label,"
        "order_id,sku,sku_description,qty_kg,locked,attrs\n",
        encoding="utf-8")
    (d / "reference" / "demand_plan.csv").write_text(
        "order_id,sku,qty_target,lower_pct,upper_pct,due_start_hour,"
        "due_end_hour\n", encoding="utf-8")
    (d / "reference" / "capabilities_rates.csv").write_text(
        "line_id,line_name,sku,capable,calc_rate_kgph\n", encoding="utf-8")
    (d / "stockcheck" / "settings.json").write_text(
        json.dumps({"vif_folder": str(vif), "toggles": {"M01|Ava": True}}),
        encoding="utf-8")
    return d


def _fake_report(calls: list):
    def fn(data_dir, vif_folder, toggles=None, week_index=None):
        calls.append(str(vif_folder))
        return {"schedule_view": [], "demand_view": [], "n": len(calls)}
    return fn


def _touch(p: Path, delta: float = 120.0) -> None:
    t = p.stat().st_mtime + delta
    os.utime(p, (t, t))


def test_signature_tracks_every_input(dd):
    vif = str(dd / "vif")
    tog = {"M01|Ava": True}
    base = src.current_signature(dd, vif, tog)
    assert src.current_signature(dd, vif, tog) == base          # stable
    assert src.current_signature(dd, vif, {"M01|Ava": False}) != base
    assert src.current_signature(dd, "elsewhere", tog) != base
    for f in (dd / "calendar_blocks.csv",
              dd / "reference" / "demand_plan.csv",
              dd / "reference" / "capabilities_rates.csv",
              dd / "vif" / "ediact 3.csv"):
        before = src.current_signature(dd, vif, tog)
        _touch(f)
        assert src.current_signature(dd, vif, tog) != before, f
    # a missing file signs 0.0 instead of raising
    (dd / "calendar_blocks.csv").unlink()
    assert 0.0 in src.current_signature(dd, vif, tog)


def test_first_call_computes_persists_then_serves_from_disk(dd, monkeypatch):
    calls: list[str] = []
    import stockcheck.api as api
    monkeypatch.setattr(api, "stock_check_report", _fake_report(calls))
    first = src.get_report(dd, auto=False)
    assert first.source == "computed" and not first.stale
    assert calls == [str(dd / "vif")]   # the shared resolver read settings
    assert (dd / "stockcheck" / src.CACHE_NAME).exists()
    again = src.get_report(dd, auto=False)
    assert again.source == "cache" and not again.stale
    assert again.report == first.report
    assert again.computed_at == first.computed_at
    assert len(calls) == 1              # served from disk, no recompute
    assert src.get_report(dd, auto=True).source == "cache"
    assert len(calls) == 1


def test_manual_mode_shows_stale_saved_data_until_refreshed(dd, monkeypatch):
    calls: list[str] = []
    import stockcheck.api as api
    monkeypatch.setattr(api, "stock_check_report", _fake_report(calls))
    src.refresh_report(dd)
    _touch(dd / "calendar_blocks.csv")          # the board moved
    c = src.get_report(dd, auto=False)          # Stock Check / Reconcile
    assert c.source == "cache" and c.stale      # fast + honest, no recompute
    assert len(calls) == 1
    c2 = src.refresh_report(dd)                 # the Refresh button
    assert c2.source == "computed" and not c2.stale
    assert len(calls) == 2
    assert not src.get_report(dd, auto=False).stale


def test_auto_mode_recomputes_when_inputs_moved(dd, monkeypatch):
    calls: list[str] = []
    import stockcheck.api as api
    monkeypatch.setattr(api, "stock_check_report", _fake_report(calls))
    src.refresh_report(dd)
    _touch(dd / "reference" / "demand_plan.csv")
    c = src.get_report(dd, auto=True)           # Home: auto refresh
    assert c.source == "computed"
    assert len(calls) == 2
    assert src.get_report(dd, auto=True).source == "cache"
    assert len(calls) == 2


def test_error_reports_are_returned_but_never_saved(dd, monkeypatch):
    calls: list[str] = []
    import stockcheck.api as api
    monkeypatch.setattr(api, "stock_check_report", _fake_report(calls))
    good = src.refresh_report(dd)

    def boom(*a, **k):
        raise OSError("share unreachable")

    monkeypatch.setattr(api, "stock_check_report", boom)
    err = src.refresh_report(dd)
    assert err.report["error"] == "share unreachable"
    assert err.source == "computed"
    assert src.load_cached(dd).report == good.report   # last-good intact
    # an engine-returned error dict is not saved either
    monkeypatch.setattr(api, "stock_check_report",
                        lambda *a, **k: {"error": "bad import"})
    assert src.refresh_report(dd).report["error"] == "bad import"
    assert src.load_cached(dd).report == good.report


def test_corrupt_or_alien_cache_reads_as_absent(dd, monkeypatch):
    calls: list[str] = []
    import stockcheck.api as api
    monkeypatch.setattr(api, "stock_check_report", _fake_report(calls))
    path = dd / "stockcheck" / src.CACHE_NAME
    path.write_text("{not json", encoding="utf-8")
    assert src.load_cached(dd) is None
    assert src.get_report(dd, auto=False).source == "computed"
    path.write_text(json.dumps({"report": "not-a-dict"}), encoding="utf-8")
    assert src.load_cached(dd) is None


def test_integration_real_dev_vif(dd):
    """End-to-end on the bundled VIF fixtures: compute, persist, reload —
    no monkeypatching. The board/demand files are header-only, so the views
    are empty but the whole import + assembly path runs for real."""
    (dd / "stockcheck" / "settings.json").write_text(
        json.dumps({"vif_folder": str(DEV_VIF)}), encoding="utf-8")
    c = src.get_report(dd, auto=False)
    assert c.source == "computed"
    assert not c.report.get("error"), c.report
    assert c.report["source_files"].get("ediact 3.csv")
    assert c.report["schedule_view"] == [] and c.report["demand_view"] == []
    c2 = src.get_report(dd, auto=False)
    assert c2.source == "cache" and not c2.stale
    assert c2.report["source_files"] == c.report["source_files"]
