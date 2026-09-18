# tests/test_data_files_catalog.py — the input-file registry behind the Data
# Files page (2026-09-17).
#
# Two promises: (1) every file the live sync delivers and every VIF export the
# stock check loads has a catalog row — the page cannot silently miss a new
# data source again (the 2026-09-15 drop had added a dozen files the page
# never showed); (2) helpers.data_health.file_statuses grades presence,
# freshness, fallbacks and configured paths the way the page shows them, on
# the same cadence table the Command Center uses.

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from helpers import data_catalog as dc
from helpers import data_health as dh

ROOT = Path(__file__).resolve().parent.parent
RECV_XLSM = "Shipping Receiving Schedule NPA - 2024.xlsm"


@pytest.fixture(autouse=True)
def _no_machine_conf(monkeypatch):
    """file_statuses without drop_folders reads THIS machine's live-data conf
    (its drop folders, 2026-09-18); tests must never see it."""
    monkeypatch.setenv("FS_LIVE_DATA_LOCAL_CONF", "")


def _touch(path: Path, age_h: float = 0.1, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    old = time.time() - age_h * 3600
    os.utime(path, (old, old))


def _cfg(**datasources) -> dict:
    return {"scheduler": {"planning_start_date": "2026-08-03 00:00:00",
                          "anchor_mode": "fixed", "horizon_weeks": 3,
                          "use_sku_rates": True},
            "datasources": dict(datasources)}


def _by_key(rows) -> dict:
    return {r.key: r for r in rows}


# ---------------------------------------------------------------------------
# the registry itself
# ---------------------------------------------------------------------------

def test_catalog_keys_unique_and_metadata_well_formed():
    keys = [s.key for s in dc.CATALOG]
    assert len(keys) == len(set(keys))
    groups = {g[0] for g in dc.GROUPS}
    for s in dc.CATALOG:
        assert s.group in groups, s.key
        assert s.kind in dc.KINDS, s.key
        assert s.folder in dc.FOLDERS, s.key
        assert s.managed_by in ("user", "bridge", "app", "derived"), s.key
        assert s.source, f"{s.key}: every row names who produces the file"
        if s.cadence_key is not None:
            assert s.cadence_key in dh.DEFAULT_CADENCE_H, s.key
        if s.config_key is not None:
            assert s.config_key in ("manprg_files", "cip_info_csv",
                                    "demand_summary_csv", "po_report_path"), s.key
        # uploads are CSV-only: a planner-maintained row must be a csv
        if s.managed_by == "user":
            assert s.kind == "csv", s.key
        # a row hidden from planners is never theirs to upload (2026-09-18)
        if not s.planner_visible:
            assert s.managed_by != "user", s.key
        # a drop-folder row is found by its glob, never under the data dir
        if s.folder == "drop":
            assert s.glob and s.config_key is None and not s.generic_health, s.key
    hidden = [s.key for s in dc.CATALOG if not s.planner_visible]
    assert hidden == ["initial_states"]
    assert dc.by_key("azap_workbook").folder == "drop"


def test_every_bridge_delivered_file_has_a_catalog_row():
    """The live sync's delivery list (scripts/fs-live-data.conf.json) is the
    contract of what lands in data/reference: each name — including the
    dated-glob rename targets — must be a catalog primary or fallback."""
    conf = json.loads((ROOT / "scripts" / "fs-live-data.conf.json")
                      .read_text(encoding="utf-8"))
    delivered = set()
    for entry in conf["files"]:
        delivered.add(entry.split("->")[-1].strip() if "->" in entry else entry.strip())
    missing = sorted(delivered - dc.covered_names())
    assert not missing, f"bridge files without a Data Files row: {missing}"


def test_every_vif_export_the_stock_check_loads_has_a_catalog_row():
    from stockcheck import vif_import as vi
    loaded = (set(vi.BOM_FILES) | {vi.HSM_FILE} | set(vi.LOT_FILES)
              | set(vi.LOT_FALLBACKS.values())
              | {vi.RECEIPTS_FILE, "azapart.csv", "rmpkitems.csv"})
    missing = sorted(loaded - dc.covered_names())
    assert not missing, f"VIF exports without a Data Files row: {missing}"
    # deliberately ignored exports stay OFF the page: they would only read
    # as missing/stale noise
    assert not (set(vi.IGNORED_FILES) & dc.covered_names())
    # required exports are never optional rows; the loader's optional ones are
    req = {dc.by_key("vif_bom"), dc.by_key("vif_rm_lots"),
           dc.by_key("vif_pkg_lots"), dc.by_key("vif_azapart")}
    assert all(not s.optional for s in req)
    assert dc.by_key("vif_amb_lots").optional and dc.by_key("vif_pkg_receipts").optional
    # every VIF row resolves against the folder the stock check reads
    assert all(s.folder == "vif" for s in dc.CATALOG if s.key.startswith("vif_"))


def test_feed_rows_with_dedicated_health_rules_are_not_generic():
    """The Command Center already has a row per feed (manprg, cip_info,
    demand baseline, VIF set, PO feed, receiving); the generic catalog loop
    must not add a second one."""
    for key in ("manprg", "manprg2", "cip_info", "demand_summary", "open_pos",
                "receiving", "line_rates", "azap_workbook"):
        assert dc.by_key(key).generic_health is False, key
    assert all(not s.generic_health for s in dc.CATALOG if s.folder == "vif")
    # the planner CSVs keep their Home rows
    for key in ("lines", "capabilities_rates", "changeovers", "line_cip_hrs",
                "sku_info", "calendar_blocks", "downtimes", "demand_plan"):
        assert dc.by_key(key).generic_health is True, key


def test_resolve_prefers_primary_then_fallbacks(tmp_path):
    ref = tmp_path / "reference"
    spec = dc.by_key("open_pos")
    # nothing there: the MISSING row names the expected primary
    assert spec.resolve(tmp_path, _cfg()) == ref / "order_npa.csv"
    _touch(ref / "open_pos.csv")
    assert spec.resolve(tmp_path, _cfg()).name == "open_pos.csv"
    _touch(ref / "open_pos.xlsx")
    assert spec.resolve(tmp_path, _cfg()).name == "open_pos.xlsx"
    _touch(ref / "order_npa.csv")
    assert spec.resolve(tmp_path, _cfg()).name == "order_npa.csv"
    # a configured override is authoritative even when absent
    override = tmp_path / "it_drop" / "NPA Open POs -9.17.xlsx"
    assert spec.resolve(tmp_path, _cfg(po_report_path=str(override))) == override


def test_manprg_files_override_indexes_the_two_paths(tmp_path):
    a, b = tmp_path / "a" / "manprg.txt", tmp_path / "b" / "manprg2.txt"
    cfg = _cfg(manprg_files=f"{a};{b}")
    assert dc.by_key("manprg").resolve(tmp_path, cfg) == a
    assert dc.by_key("manprg2").resolve(tmp_path, cfg) == b
    # a single configured path leaves the second file on its default
    cfg1 = _cfg(manprg_files=str(a))
    assert dc.by_key("manprg2").resolve(tmp_path, cfg1) == tmp_path / "reference" / "manprg2.txt"


# ---------------------------------------------------------------------------
# file_statuses — what the page shows
# ---------------------------------------------------------------------------

def test_file_statuses_one_row_per_entry_on_an_empty_dir(tmp_path):
    rows = dh.file_statuses(tmp_path, _cfg(), drop_folders=[])
    assert [r.key for r in rows] == [s.key for s in dc.CATALOG]
    by = _by_key(rows)
    # the hidden legacy start-state file is optional since 2026-09-18 (the
    # staging synthesizes its own): absent -> not applicable, never MISSING
    assert by["initial_states"].state == dh.NOT_APPLICABLE and by["initial_states"].severity == 0
    # the AZAP workbook: no drop folder here -> not applicable, never MISSING
    assert by["azap_workbook"].state == dh.NOT_APPLICABLE and by["azap_workbook"].severity == 0
    assert "GitHub mode" in by["azap_workbook"].detail
    assert by["azap_workbook"].filename == "New Export AZAP MMDDYY.xlsx"
    assert by["open_pos"].state == dh.MISSING and by["open_pos"].filename == "order_npa.csv"
    assert "also looked for open_pos.xlsx, open_pos.csv" in by["open_pos"].detail
    assert by["manprg"].state == dh.MISSING and by["manprg"].cadence_h == 0.5
    assert by["vif_bom"].state == dh.MISSING
    assert "dev fixture" in by["vif_bom"].detail        # resolver fell back to dev_vif
    assert by["vif_amb_lots"].state == dh.NOT_APPLICABLE
    assert by["vif_amb_lots"].severity == 0
    assert by["measured_rates"].state == dh.NOT_APPLICABLE
    assert by["receiving"].state == dh.MISSING and by["receiving"].cadence_h == 168.0
    assert by["lines"].state == dh.MISSING and by["lines"].cadence_h is None


def test_file_statuses_fresh_stale_and_static(tmp_path):
    ref = tmp_path / "reference"
    _touch(ref / "order_npa.csv", age_h=1.0, text="A|B\n1|2\n")
    _touch(ref / "cip_info.csv", age_h=40.0, text="ID,LineEquipment\n1,P09\n")
    _touch(ref / "capabilities_rates.csv", age_h=900.0,
           text="line_id,sku,capable\n0,1,1\n")
    _touch(ref / RECV_XLSM, age_h=30.0)
    by = _by_key(dh.file_statuses(tmp_path, _cfg()))
    assert by["open_pos"].state == dh.OK and by["open_pos"].rows == 2   # lines, text kind
    assert by["cip_info"].state == dh.STALE
    assert "older than its expected refresh (≤ 26 h)" in by["cip_info"].detail
    assert by["capabilities_rates"].state == dh.OK        # static: never stale
    assert by["capabilities_rates"].rows == 1
    assert by["receiving"].state == dh.OK and by["receiving"].rows is None  # workbook
    # [health] cadence override is honoured like everywhere else
    cfg = _cfg()
    cfg["health"] = {"cadence_h": {"cip_info": 48}}
    assert _by_key(dh.file_statuses(tmp_path, cfg))["cip_info"].state == dh.OK


def test_file_statuses_fallback_and_zero_rows(tmp_path):
    ref = tmp_path / "reference"
    _touch(ref / "open_pos.xlsx", age_h=1.0)
    _touch(tmp_path / "lines.csv", age_h=1.0, text="line_id,line_name\n")  # data/, not reference/
    by = _by_key(dh.file_statuses(tmp_path, _cfg()))
    assert by["open_pos"].state == dh.OK and by["open_pos"].filename == "open_pos.xlsx"
    assert "fallback in use — order_npa.csv not present" in by["open_pos"].detail
    assert by["lines"].state == dh.ERROR and "0 data rows" in by["lines"].detail


def test_file_statuses_configured_paths(tmp_path):
    custom = tmp_path / "carsten" / "cip_info.csv"
    _touch(custom, age_h=1.0, text="ID,LineEquipment\n1,P09\n")
    nowhere = tmp_path / "nowhere.xlsx"
    by = _by_key(dh.file_statuses(tmp_path, _cfg(cip_info_csv=str(custom),
                                                  po_report_path=str(nowhere))))
    assert by["cip_info"].state == dh.OK and by["cip_info"].path == custom
    assert "configured path (Settings)" in by["cip_info"].detail
    assert by["open_pos"].state == dh.MISSING and by["open_pos"].path == nowhere
    assert "also looked for" not in by["open_pos"].detail   # override is authoritative


def test_file_statuses_vif_rows_follow_the_stock_check_folder(tmp_path):
    vif = tmp_path / "share_vif"
    for name in ("ediact.csv", "jestkexp.csv", "jestkexp2.csv", "azapart.csv",
                 "jestksa.csv"):
        _touch(vif / name, age_h=2.0, text="a;b\n1;2\n")
    (tmp_path / "stockcheck").mkdir()
    (tmp_path / "stockcheck" / "settings.json").write_text(
        json.dumps({"vif_folder": str(vif)}), encoding="utf-8")
    by = _by_key(dh.file_statuses(tmp_path, _cfg()))
    assert by["vif_bom"].state == dh.OK and by["vif_bom"].path == vif / "ediact.csv"
    assert "dev fixture" not in by["vif_bom"].detail
    assert by["vif_apple_lots"].state == dh.OK
    assert by["vif_apple_lots"].filename == "jestksa.csv"      # alias in use
    assert "fallback in use" in by["vif_apple_lots"].detail
    assert by["vif_pkg_receipts"].state == dh.NOT_APPLICABLE  # optional, absent
    assert by["vif_hsm_recipes"].state == dh.MISSING           # required supplement
    assert by["vif_hsm_recipes"].cadence_h is None             # static: never stale
    assert by["vif_azapart"].state == dh.OK and by["vif_azapart"].cadence_h is None


def test_file_statuses_manprg_uses_the_content_stamp(tmp_path):
    """A manprg file re-written with identical content is still stale
    telemetry: the row's age is the stamp's as-of, not the file mtime."""
    from helpers.manprg_import import content_sha256
    ref = tmp_path / "reference"
    _touch(ref / "manprg.txt", age_h=0.1, text="h\n1\n")
    _touch(ref / "manprg2.txt", age_h=0.1, text="h\n")
    files = [ref / "manprg.txt", ref / "manprg2.txt"]
    stamp = {"as_of": (pd.Timestamp.now() - pd.Timedelta(hours=3)).isoformat(timespec="seconds"),
             "sha256": content_sha256(files),
             "files": ["manprg.txt", "manprg2.txt"],
             "stamped": pd.Timestamp.now().isoformat(timespec="seconds")}
    (ref / "manprg.asof.json").write_text(json.dumps(stamp), encoding="utf-8")
    by = _by_key(dh.file_statuses(tmp_path, _cfg()))
    for key in ("manprg", "manprg2"):
        assert by[key].state == dh.STALE, key                # 3 h > 0.5 h cadence
        assert by[key].age_h is not None and 2.9 < by[key].age_h < 3.2
        assert "as-of stamp" in by[key].detail
    # without a stamp the file mtime rules and the fresh files read OK
    (ref / "manprg.asof.json").unlink()
    by = _by_key(dh.file_statuses(tmp_path, _cfg()))
    assert by["manprg"].state == dh.OK and by["manprg"].rows == 2


# ---------------------------------------------------------------------------
# the Command Center keeps one row per feed
# ---------------------------------------------------------------------------

def test_home_assessment_has_no_duplicate_feed_rows_and_skips_absent_optional():
    from test_data_health import _cfg as hcfg, _empty_data_dir, _min_catalog
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dd = _empty_data_dir(Path(td))
        _min_catalog(dd)
        health = dh.assess(dd, hcfg())
        keys = [h.key for h in health]
        assert keys.count("manprg") == 1 and keys.count("cip_info") == 1
        assert keys.count("open_pos") == 1 and keys.count("receiving") == 1
        assert keys.count("vif_stock") == 1
        assert not any(k.startswith("vif_") and k != "vif_stock" for k in keys)
        # optional reference files absent from the fixture: no finding
        for k in ("sku_plan_evidence", "rate_rules", "measured_rates"):
            assert k not in keys
        bad = [h for h in health if h.state in (dh.MISSING, dh.ERROR)]
        assert bad == [], [(h.key, h.detail) for h in bad]
        # present optional files get an OK row
        (dd / "reference" / "sku_plan_evidence.csv").write_text(
            "sku,line_name,source,mo\n1,P09,x,1\n", encoding="utf-8")
        keys2 = [h.key for h in dh.assess(dd, hcfg())]
        assert "sku_plan_evidence" in keys2


# ---------------------------------------------------------------------------
# the demand chain: AZAP workbook -> summary -> demand plan (2026-09-18)
# ---------------------------------------------------------------------------

def test_file_statuses_azap_workbook_and_the_demand_chain(tmp_path):
    """The verdicts are judged the way the sync judges: the DROP folder's own
    summary csv against the newest workbook (data/reference only receives a
    copy), and the derived plan against data/reference's summary. Every
    stamp is relative to one clock so the test never ages out."""
    ref = tmp_path / "reference"
    drop = tmp_path / "fs_data"
    vif, manual = drop / "fs_vif", drop / "fs_manual"
    vif.mkdir(parents=True)
    manual.mkdir()
    # a configured drop without a workbook: MISSING, expected in fs_manual
    by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=[str(vif), str(manual)]))
    assert by["azap_workbook"].state == dh.MISSING
    assert str(manual) in by["azap_workbook"].detail and "fs_manual" in by["azap_workbook"].detail
    assert by["azap_workbook"].path == manual / "New Export AZAP MMDDYY.xlsx"
    # the workbook lands (2026-09-11 export by its name); the drop's summary is its build
    wb = manual / "New Export AZAP 091126.xlsx"
    _touch(wb, age_h=30.0)
    when = wb.stat().st_mtime
    text = "Week,Product,Tons\n38,120437,39.003\n"
    drop_csv = manual / "demand_plan_summary.csv"
    _touch(drop_csv, text=text)
    os.utime(drop_csv, (when, when))
    ref_csv = ref / "demand_plan_summary.csv"
    _touch(ref_csv, text=text)
    os.utime(ref_csv, (when, when))
    folders = [str(vif), str(manual)]
    by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=folders))
    wbrow = by["azap_workbook"]
    assert wbrow.state == dh.OK and wbrow.filename == "New Export AZAP 091126.xlsx"
    assert wbrow.cadence_h == 168.0 and wbrow.rows is None
    assert "export date 2026-09-11 (from its name)" in wbrow.detail
    assert "W38–W44" in wbrow.detail and "IS built from it" in wbrow.detail
    assert by["demand_summary"].state == dh.OK
    assert "built from New Export AZAP 091126.xlsx (export 2026-09-11)" in by["demand_summary"].detail
    # the drop's csv older than the workbook: the sync will rebuild it -> STALE
    os.utime(drop_csv, (when - 86400, when - 86400))
    by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=folders))
    assert by["demand_summary"].state == dh.STALE
    assert "NOT built from the newest workbook" in by["demand_summary"].detail
    assert "NOT built from it" in by["azap_workbook"].detail
    # a hand edit newer than every workbook is kept
    os.utime(drop_csv, (when + 5, when + 5))
    by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=folders))
    assert by["demand_summary"].state == dh.OK and "hand edit" in by["demand_summary"].detail
    # data/reference's copy differs from the drop's: the next pass copies it -> STALE
    os.utime(drop_csv, (when, when))
    ref_csv.write_text(text + "39,120438,1.0\n", encoding="utf-8")
    os.utime(ref_csv, (when, when))
    by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=folders))
    assert by["demand_summary"].state == dh.STALE
    assert "differs from the drop's summary" in by["demand_summary"].detail
    ref_csv.write_text(text, encoding="utf-8")
    os.utime(ref_csv, (when, when))
    # a Settings path is status only: no rebuild/keep claims, never STALE from them
    other = tmp_path / "elsewhere.csv"
    _touch(other, text=text)
    by = _by_key(dh.file_statuses(tmp_path, _cfg(demand_summary_csv=str(other)), drop_folders=folders))
    assert by["demand_summary"].state == dh.OK and "status only" in by["demand_summary"].detail
    # the derived plan names its provenance; stamps derive from the same clock
    _touch(ref / "demand_plan.csv", age_h=2.0, text="order_id,sku,qty_target\na,1,1\n")
    imported = datetime.fromtimestamp(when + 3600).replace(microsecond=0)
    (ref / "demand_plan.source.json").write_text(json.dumps({
        "source": "demand_plan_summary.csv", "imported": imported.isoformat(),
        "rows": 238, "weeks": [0, 1, 2], "skus": 82, "anchor": "2026-09-14 00:00:00",
        "anchor_iso_week": 38}), encoding="utf-8")
    by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=folders))
    assert by["demand_plan"].state == dh.OK
    assert (f"derived from demand_plan_summary.csv, imported {imported:%Y-%m-%d %H:%M}, "
            "anchor W38, 3 week(s), 238 orders") in by["demand_plan"].detail
    # the summary newer than the derivation -> STALE
    os.utime(ref_csv, (when + 7200, when + 7200))
    by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=folders))
    assert by["demand_plan"].state == dh.STALE and "NEWER than this derived plan" in by["demand_plan"].detail
    os.utime(ref_csv, (when, when))
    # an unreadable or wrong-shaped stamp: STALE with the reason, never a crash
    for bad in ("{not json", "[1, 2]", "5", json.dumps({"imported": imported.isoformat(), "weeks": 3, "rows": 1})):
        (ref / "demand_plan.source.json").write_text(bad, encoding="utf-8")
        by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=folders))
        assert by["demand_plan"].state in (dh.STALE, dh.OK), bad
        if bad.startswith(("{not", "[", "5")):
            assert by["demand_plan"].state == dh.STALE and "unreadable" in by["demand_plan"].detail, bad
    # a workbook in two folders: the newest by export date wins (as the sync copies)
    _touch(vif / "New Export AZAP 090426.xlsx", age_h=1.0)
    by = _by_key(dh.file_statuses(tmp_path, _cfg(), drop_folders=folders))
    assert by["azap_workbook"].filename == "New Export AZAP 091126.xlsx"


def test_initial_states_is_not_delivered_by_the_bridge_any_more():
    """A legacy file for direct CLI solves: the conf does not pretend the sync
    delivers it, the catalog row is hidden and OPTIONAL (2026-09-18: the
    staging synthesizes the work copy from lines.csv and never reads it)."""
    conf = json.loads((ROOT / "scripts" / "fs-live-data.conf.json").read_text(encoding="utf-8"))
    assert "initial_states.csv" not in conf["files"]
    assert all("initial_states.csv" not in names for names in conf["drop_layout"].values())
    spec = dc.by_key("initial_states")
    assert spec is not None and not spec.planner_visible
    assert not spec.generic_health          # the Command Center does not grade a file nothing reads
    assert spec.managed_by == "app" and spec.optional
    assert "not read" in spec.source.lower() or "legacy" in spec.source.lower()
