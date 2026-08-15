# helpers/versions_ui.py -- Write actions shared by pages that replace the
# schedule of record: back up the on-disk calendar and snapshot it as the
# "current schedule" version so Compare / Generate stay in sync.
#
# (Extracted from the retired manual-import widget; the Scorecard page's
# seed/upload import flows are the remaining callers.)

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from helpers.scorecard_engine import score_calendar
from helpers.version_manager import upsert_version

# Kept for backward compatibility with versions already on disk. The directory
# data/versions/azap_baseline/ is never renamed -- only its display name is.
CURRENT_SCHEDULE_SLUG = "azap_baseline"
CURRENT_SCHEDULE_NAME = "Current schedule (imported)"


def backup_file(path: Path, data_dir: Path) -> str:
    """Copy path into data/_backups/<stem>.<timestamp><suffix>. Returns the name."""
    if not path.exists():
        return ""
    bdir = Path(data_dir) / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest.name


def snapshot_current_schedule(cal, data_dir: Path) -> None:
    """Keep Compare / Generate in sync with the schedule now on disk."""
    result = score_calendar(cal, week_label="current schedule", data_dir=data_dir)
    try:
        upsert_version(
            CURRENT_SCHEDULE_SLUG,
            CURRENT_SCHEDULE_NAME,
            cal,
            result.to_dict(),
            data_dir,
            source="schedule_import",
            notes="calendar_blocks.csv snapshot taken when the schedule was imported.",
        )
    except ValueError as exc:
        st.warning(f"Could not save the current-schedule version: {exc}")
