# tests/test_downtime_store.py — downtimes are STATIC wall-clock data.
#
# User rule 2026-08-21: reference downtimes are edited in ONE screen and
# nothing else may adjust them. The file stores absolute datetimes; anchor-
# relative hours are derived at load time, so "Roll calendar to today"
# (which moves the anchor) can no longer drag outages around the calendar.

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers.downtime_store import (  # noqa: E402
    LOAD_COLUMNS,
    SOLVER_COLUMNS,
    STORE_COLUMNS,
    load_downtimes,
    load_downtimes_file,
    save_downtimes,
    stage_solver_downtimes,
)

A0 = datetime(2026, 8, 19, 0, 0)   # the anchor P11's window was entered under
A1 = datetime(2026, 8, 21, 0, 0)   # the anchor after "Roll calendar to today"

V2_CSV = (
    "line_id,line_name,start_datetime,end_datetime,reason\n"
    "2,P11,2026-08-19 00:00,2026-08-24 00:00,Down\n"
    "4,P13,2026-08-21 00:00,2026-09-11 00:00,Down\n"
)

OLD_CSV = (
    "line_id,line_name,start_hour,end_hour,reason\n"
    "2,P11,0.0,120.0,Down\n"
    "4,P13,0.0,504.0,Down\n"
)


def _dd(tmp_path: Path, text: str) -> Path:
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    (dd / "reference" / "downtimes.csv").write_text(text, encoding="utf-8")
    return dd


# ---------------------------------------------------------------------------
# load-time derivation: wall-clock -> board hours, for ANY anchor
# ---------------------------------------------------------------------------

def test_loader_derives_board_hours_for_any_anchor(tmp_path):
    dd = _dd(tmp_path, V2_CSV)
    df = load_downtimes(dd, anchor=A0)
    assert list(df.columns) == LOAD_COLUMNS
    p11 = df[df["line_name"] == "P11"].iloc[0]
    assert p11["start_hour"] == 0.0
    assert p11["end_hour"] == 120.0          # 08/19 -> 08/24 = 5 days

    df = load_downtimes(dd, anchor=A1)
    p11 = df[df["line_name"] == "P11"].iloc[0]
    assert p11["start_hour"] == -48.0        # started before the new anchor
    assert p11["end_hour"] == 72.0           # 08/21 -> 08/24 = 3 days
    p13 = df[df["line_name"] == "P13"].iloc[0]
    assert p13["start_hour"] == 0.0
    assert p13["end_hour"] == 504.0


def test_roll_simulation_preserves_wall_clock_and_file_bytes(tmp_path):
    """Anchor +48h (a two-day roll): the FILE must not change at all, and the
    derived board hours must shift by exactly -48h — same wall-clock spot."""
    dd = _dd(tmp_path, V2_CSV)
    path = dd / "reference" / "downtimes.csv"
    before = path.read_bytes()

    h_a0 = load_downtimes(dd, anchor=A0)[["start_hour", "end_hour"]]
    h_a1 = load_downtimes(dd, anchor=A1)[["start_hour", "end_hour"]]

    assert path.read_bytes() == before        # roll never touches the file
    assert ((h_a1 - h_a0) == -48.0).all().all()

    # And the wall-clock render is anchor-independent by construction.
    d0 = load_downtimes(dd, anchor=A0)
    d1 = load_downtimes(dd, anchor=A1)
    assert list(d0["end_datetime"]) == list(d1["end_datetime"])


# ---------------------------------------------------------------------------
# migration: hour-format file converts ONCE, loudly, with the P11 correction
# ---------------------------------------------------------------------------

def test_migration_converts_hours_once_and_is_idempotent(tmp_path):
    old = (
        "line_id,line_name,start_hour,end_hour,reason\n"
        "3,P12,24.0,72.0,Maint\n"
    )
    dd = _dd(tmp_path, old)
    path = dd / "reference" / "downtimes.csv"

    df = load_downtimes_file(path, anchor=A1, storage_anchor=A1)
    row = df.iloc[0]
    assert row["start_datetime"] == "2026-08-22 00:00"
    assert row["end_datetime"] == "2026-08-24 00:00"
    assert row["start_hour"] == 24.0 and row["end_hour"] == 72.0

    text = path.read_text(encoding="utf-8")
    assert "start_datetime" in text and "start_hour" not in text
    backups = list((dd / "_backups").glob("downtimes.*.csv"))
    assert len(backups) == 1                  # backed up before the rewrite
    assert backups[0].read_text(encoding="utf-8") == old

    # Second load: no rewrite, byte-identical file.
    after_first = path.read_bytes()
    df2 = load_downtimes_file(path, anchor=A1, storage_anchor=A1)
    assert path.read_bytes() == after_first
    assert df2.equals(df)
    assert len(list((dd / "_backups").glob("downtimes.*.csv"))) == 1


def test_migration_applies_the_p11_correction(tmp_path):
    """The live P11 row (0-120h) was ENTERED to end 08/24 00:00 under the
    08-19 anchor; the roll to 08-21 made it render 08/26. The user stated the
    true end, so migration under the rolled anchor must land on 08/24 —
    not preserve the roll artifact. P13 keeps its plain conversion."""
    dd = _dd(tmp_path, OLD_CSV)
    path = dd / "reference" / "downtimes.csv"

    df = load_downtimes_file(path, anchor=A1, storage_anchor=A1)
    p11 = df[df["line_name"] == "P11"].iloc[0]
    assert p11["end_datetime"] == "2026-08-24 00:00"   # NOT 08-26
    p13 = df[df["line_name"] == "P13"].iloc[0]
    assert p13["end_datetime"] == "2026-09-11 00:00"   # 08-21 + 504h


def test_migrate_false_never_writes(tmp_path):
    dd = _dd(tmp_path, OLD_CSV)
    path = dd / "reference" / "downtimes.csv"
    before = path.read_bytes()
    df = load_downtimes_file(path, anchor=A1, storage_anchor=A1, migrate=False)
    assert path.read_bytes() == before
    assert df[df["line_name"] == "P11"].iloc[0]["end_datetime"] == "2026-08-24 00:00"


# ---------------------------------------------------------------------------
# solver staging: the work-dir copy speaks hours in the staging frame
# ---------------------------------------------------------------------------

def test_stage_solver_downtimes_writes_the_hour_frame(tmp_path):
    dd = _dd(tmp_path, V2_CSV)
    dst = tmp_path / "work" / "downtimes.csv"
    n = stage_solver_downtimes(dd / "reference" / "downtimes.csv", dst, A1)
    assert n == 2
    staged = pd.read_csv(dst)
    assert list(staged.columns) == SOLVER_COLUMNS
    p11 = staged[staged["line_name"] == "P11"].iloc[0]
    assert p11["start_hour"] == -48.0 and p11["end_hour"] == 72.0


def test_stage_drops_unparseable_rows(tmp_path):
    bad = (
        "line_id,line_name,start_datetime,end_datetime,reason\n"
        "2,P11,2026-08-19 00:00,2026-08-24 00:00,Down\n"
        "9,P99,not-a-date,also-bad,Down\n"
    )
    dd = _dd(tmp_path, bad)
    dst = tmp_path / "work" / "downtimes.csv"
    n = stage_solver_downtimes(dd / "reference" / "downtimes.csv", dst, A0)
    assert n == 1
    staged = pd.read_csv(dst)
    assert staged["line_name"].tolist() == ["P11"]


# ---------------------------------------------------------------------------
# the Gantt overlay: same wall-clock windows whatever the anchor says
# ---------------------------------------------------------------------------

def test_gantt_downtime_map_is_wall_clock_invariant(tmp_path, monkeypatch):
    """downtime_map_for_calendar feeds the Plant Calendar Gantt overlay in the
    toml-anchor frame. Simulate a roll (toml anchor 08-19 -> 08-21): interval
    hours must shift by exactly -48 so anchor+hours — the drawn wall-clock
    window — is identical before and after."""
    from datetime import timedelta

    import helpers.config as config
    from helpers.downtime_ui import downtime_map_for_calendar

    dd = _dd(tmp_path, V2_CSV)
    maps = {}
    for anchor in (A0, A1):
        cfg = {"scheduler": {
            "planning_start_date": anchor.strftime("%Y-%m-%d %H:%M:%S")}}
        monkeypatch.setattr(config, "load_toml", lambda path=None, cfg=cfg: cfg)
        maps[anchor] = downtime_map_for_calendar(dd)

    for line in ("P11", "P13"):
        walls = {
            anchor: [(anchor + timedelta(hours=s), anchor + timedelta(hours=e))
                     for s, e in m[line]]
            for anchor, m in maps.items()
        }
        assert walls[A0] == walls[A1], line


# ---------------------------------------------------------------------------
# save: only STORE_COLUMNS hit the disk — hours never do
# ---------------------------------------------------------------------------

def test_save_strips_derived_hours(tmp_path):
    dd = _dd(tmp_path, V2_CSV)
    df = load_downtimes(dd, anchor=A1)     # has derived hour columns
    save_downtimes(dd, df)
    text = (dd / "reference" / "downtimes.csv").read_text(encoding="utf-8")
    header = text.splitlines()[0]
    assert header == ",".join(STORE_COLUMNS)
    assert "start_hour" not in text
