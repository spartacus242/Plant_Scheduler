"""Downtimes are constraints, not schedule (user rule 2026-09-01):
downtimes.csv is the single source, the board draws them as locked windows
and never stores them; planner CIPs on the board are committed windows for
the solver and replace the plant-projected cleans on their line."""
import pandas as pd

from helpers.calendar_io import (CALENDAR_COLUMNS, DISPLAY_ONLY_PREFIXES,
                                 downtime_block_type, drop_display_overlays,
                                 import_solver_schedule)
from helpers.downtime_store import (STORE_COLUMNS, load_downtimes_file,
                                    save_downtimes_file)
from helpers.plan_fill import planner_cip_blocks


def test_downtime_block_type_prefers_explicit_type_then_reason():
    assert downtime_block_type("Maintenance", "Down") == "maintenance"
    assert downtime_block_type("Contractor", "anything") == "contractor"
    assert downtime_block_type("Downtime", "PM") == "line_down"
    assert downtime_block_type("", "Line down for PM") == "line_down"
    assert downtime_block_type("", "Using P14 labor") == "maintenance"
    assert downtime_block_type(None, "contractor visit") == "contractor"


def test_type_column_round_trips_and_old_files_still_load(tmp_path):
    p = tmp_path / "downtimes.csv"
    df = pd.DataFrame([{
        "line_id": "5", "line_name": "P14",
        "start_datetime": "2026-09-02 00:00", "end_datetime": "2026-09-02 12:00",
        "reason": "PM", "type": "Maintenance",
    }])
    save_downtimes_file(p, df)
    back = load_downtimes_file(p, anchor="2026-09-01 00:00:00", migrate=False)
    assert list(back.columns[: len(STORE_COLUMNS)]) == STORE_COLUMNS
    assert back.iloc[0]["type"] == "Maintenance"
    assert float(back.iloc[0]["start_hour"]) == 24.0
    # legacy file without the column: loads with an empty type
    p.write_text("line_id,line_name,start_datetime,end_datetime,reason\n"
                 "5,P14,2026-09-02 00:00,2026-09-02 12:00,Down\n", encoding="utf-8")
    old = load_downtimes_file(p, anchor="2026-09-01 00:00:00", migrate=False)
    assert old.iloc[0]["type"] == ""


def test_import_uses_type_column_for_window_kind(tmp_path):
    dt = tmp_path / "downtimes.csv"
    dt.write_text(
        "line_id,line_name,start_datetime,end_datetime,reason,type\n"
        "5,P14,2026-09-02 00:00,2026-09-02 12:00,Down,Contractor\n",
        encoding="utf-8")
    cal = import_solver_schedule(tmp_path / "none.csv", None, dt,
                                 planning_anchor="2026-09-01 00:00:00")
    assert list(cal["block_type"]) == ["contractor"]


def test_drop_display_overlays_strips_every_derived_window():
    rows = []
    for bid, btype in [("cipinfo_P10", "cip"), ("dt_3", "line_down"),
                       ("down_abc", "line_down"), ("maint_xyz", "maintenance"),
                       ("blk_7", "cip"), ("prod_1", "production")]:
        rows.append({"block_id": bid, "block_type": btype, "line_id": 1,
                     "line_name": "P10", "start_h": 0, "end_h": 5, "label": "",
                     "order_id": "", "sku": "", "sku_description": "",
                     "qty_kg": None, "locked": False, "attrs": ""})
    df = pd.DataFrame(rows, columns=CALENDAR_COLUMNS)
    kept = drop_display_overlays(df)
    assert set(kept["block_id"]) == {"blk_7", "prod_1"}
    assert set(DISPLAY_ONLY_PREFIXES) == {"cipinfo_", "dt_", "down_", "maint_"}


def test_planner_cip_blocks_excludes_plant_and_overlay_cips():
    rows = []
    for bid, attrs in [("blk_1", "planner:cip"), ("blk_2", "planner:cip_projected"),
                       ("cs_abc", "current_state:cip_projected"),
                       ("cs_def", "current_state:cip_scheduled"),
                       ("cipinfo_P10", ""), ("dt_1", "ref_downtime")]:
        rows.append({"block_id": bid, "block_type": "cip", "line_id": 1,
                     "line_name": "P10", "start_h": 0, "end_h": 6, "label": "CIP",
                     "order_id": "", "sku": "", "sku_description": "",
                     "qty_kg": None, "locked": False, "attrs": attrs})
    rows.append({**rows[0], "block_id": "prod_9", "block_type": "production"})
    df = pd.DataFrame(rows, columns=CALENDAR_COLUMNS)
    got = planner_cip_blocks(df)
    assert set(got["block_id"]) == {"blk_1", "blk_2"}
