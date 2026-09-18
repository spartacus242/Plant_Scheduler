# tests/test_current_state_overlay.py — handoff WW32 item 3.
#
# The solver's work dir must start from the REAL plant state. A previous
# version of _overlay_current_state imported load_lines from the wrong module
# and swallowed the ImportError, so the overlay was a silent no-op and the
# solver kept planning from the seeded fixture. These tests fail loudly if
# that ever regresses.

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers import scenario_runner as sr  # noqa: E402

REF = ROOT / "data" / "reference"
LIVE = (REF / "manprg.txt").exists() and (REF / "cip_info.csv").exists()


def test_overlay_imports_resolve():
    """Every module the overlay reaches for must actually exist."""
    from helpers.calendar_io import load_lines  # noqa: F401
    from helpers.cip_import import read_cip_info  # noqa: F401
    from helpers.config import datasources_config, load_toml  # noqa: F401
    from helpers.current_state import build_current_state  # noqa: F401
    from helpers.horizon import resolve  # noqa: F401


def test_overlay_synthesizes_the_start_state_and_reports(tmp_path):
    """A bare work dir gets its initial_states.csv SYNTHESIZED from lines.csv
    (2026-09-18: the reference fixture is never copied — its stale P10
    long-shutdown flag / 6-week-old carryovers used to leak into live
    solves), and the overlay always reports what it did or why it stopped."""
    notes = sr._overlay_current_state(tmp_path, ROOT / "data")
    assert notes, "overlay must always report what it did or why it didn't"
    assert any("synthesized from lines.csv" in n for n in notes), notes
    if LIVE:
        assert any("overlaid" in n for n in notes), notes
    else:
        assert any("skipped" in n for n in notes), notes
    out = pd.read_csv(tmp_path / "initial_states.csv")
    lines = pd.read_csv(ROOT / "data" / "lines.csv")
    assert len(out) == len(lines), "one start-state row per lines.csv line"
    assert set(out["line_name"]) == {str(x).strip().upper() for x in lines["line_name"]}
    assert (out["long_shutdown_flag"] == 0).all()
    assert (out["long_shutdown_extra_setup_hours"] == 0).all()
    assert out["last_cip_end_datetime"].isna().all()


@pytest.mark.skipif(not LIVE, reason="live manprg/cip_info exports not present")
def test_no_fixture_value_survives_the_overlay(tmp_path):
    """Poison every mechanical field of a pre-existing work copy; the
    overlay must overwrite all of them for EVERY line (the old rule zeroed
    the long-shutdown fields only for idle lines, so a running line kept
    the fixture's flag and paid a phantom 2 h setup)."""
    lines = pd.read_csv(ROOT / "data" / "lines.csv")
    poison = pd.DataFrame({
        "line_id": lines["line_id"], "line_name": lines["line_name"],
        "initial_sku": "POISON", "available_from_hour": 4141,
        "long_shutdown_flag": 1, "long_shutdown_extra_setup_hours": 2,
        "carryover_run_hours_since_last_cip_at_t0": 7777,     # no clamp can produce it
        "last_cip_end_datetime": "2026-01-01 00:00",
    })
    (tmp_path / "initial_states.csv").write_text(poison.to_csv(index=False), encoding="utf-8")
    notes = sr._overlay_current_state(tmp_path, ROOT / "data")
    assert any("overlaid" in n for n in notes), notes
    out = pd.read_csv(tmp_path / "initial_states.csv", dtype={"initial_sku": str})
    assert not (out["initial_sku"] == "POISON").any()
    assert not (out["available_from_hour"] == 4141).any()
    assert (out["long_shutdown_flag"] == 0).all()
    assert (out["long_shutdown_extra_setup_hours"] == 0).all()
    assert out["last_cip_end_datetime"].isna().all()
    # every line's carryover was written by the overlay: from cip_info's
    # PreviousCIP, set from the board's first performable clean, or 0 —
    # always below the line's max CIP interval (negative = cleaned after
    # hour 0 / the first performable clean lies beyond one interval)
    from helpers.cip_import import read_cip_info
    info = read_cip_info(REF / "cip_info.csv").by_line
    for _, row in out.iterrows():
        ln = str(row["line_name"]).strip().upper()
        i = info.get(ln) or info.get(ln.lower())
        limit = int((i.max_hours_between if i else 0) or 120)
        carry = float(row["carryover_run_hours_since_last_cip_at_t0"])
        assert carry < limit and carry != 7777, (ln, carry, limit)


@pytest.mark.skipif(not LIVE, reason="live manprg/cip_info exports not present")
def test_overlay_actually_gates_lines_from_the_running_mo(tmp_path):
    # the overlay synthesizes the work copy itself (2026-09-18) — the
    # reference fixture is legacy and may be absent
    init = pd.read_csv(ROOT / "data" / "lines.csv")
    notes = sr._overlay_current_state(tmp_path, ROOT / "data")
    out = pd.read_csv(tmp_path / "initial_states.csv")

    # Staging notes (time-frame + wall-clock downtime derivation) may precede
    # the overlay note — assert on presence, not position.
    assert notes and any("overlaid" in n for n in notes), notes
    gated = out[out["available_from_hour"].astype(float) > 0]
    assert len(gated) > 0, "no line was gated — the overlay did nothing"
    assert (out["initial_sku"] != "CLEAN").any(), \
        "no running SKU carried over — changeovers would be mis-costed"
    assert len(out) == len(init), "overlay must not add or drop lines"


@pytest.mark.skipif(not LIVE, reason="live manprg/cip_info exports not present")
def test_cip_carryover_never_exceeds_the_lines_max_interval(tmp_path):
    """A carryover >= MaxHoursBetweenCIP means 'CIP already overdue at hour 0',
    which the CIP constraints cannot satisfy — it made two-phase solves come
    back INFEASIBLE at every relax level. It must be clamped below the limit."""
    from helpers.cip_import import read_cip_info

    sr._overlay_current_state(tmp_path, ROOT / "data")     # synthesizes the work copy itself
    out = pd.read_csv(tmp_path / "initial_states.csv")

    limits = {k.upper(): v.max_hours_between
              for k, v in read_cip_info(REF / "cip_info.csv").by_line.items()}
    checked = 0
    for _, row in out.iterrows():
        ln = str(row["line_name"]).strip().upper()
        limit = limits.get(ln)
        if not limit:
            continue
        carry = float(row["carryover_run_hours_since_last_cip_at_t0"])
        assert carry < limit, f"{ln}: carryover {carry} >= max interval {limit}"
        checked += 1
    assert checked > 0, "no line had a CIP limit to check"


def test_overlay_reports_when_nothing_can_name_the_lines(tmp_path):
    """No lines.csv, no reference file, no capabilities: nothing is written
    and the notes say why, on both the synth and the overlay level."""
    dd = tmp_path / "plant"
    work = tmp_path / "work"
    dd.mkdir()
    work.mkdir()
    notes = sr._overlay_current_state(work, dd)
    joined = "\n".join(notes)
    assert "NOT synthesized" in joined, notes
    assert any("skipped" in n for n in notes), notes
    assert not (work / "initial_states.csv").exists()


def test_split_mo_qty_prorated_across_cip_pieces():
    """A committed MO split around a CIP must pro-rate its tonnage across
    the pieces — copying the full MO qty into every fragment double-counted
    1,535t on the 2026-08-14 board (one SKU read 1764% adherence)."""
    from helpers.current_state import _clip_prod_around_cips

    mo = {"block_id": "b1", "order_id": "30050", "start_h": 0.0,
          "end_h": 100.0, "qty_kg": 50000.0, "attrs": ""}
    cip = {"block_id": "c1", "label": "CIP", "start_h": 40.0, "end_h": 46.0}
    warnings: list[str] = []
    pieces, kept = _clip_prod_around_cips(
        [dict(mo)], [dict(cip)], warnings=warnings, line="P09")
    assert len(pieces) == 2 and len(kept) == 1
    a, b = sorted(pieces, key=lambda p: p["start_h"])
    # Updated 2026-09-03 (fix CA-4 / audit C09): the clean no longer eats
    # 6h of the MO — the tail is pushed to [46, 106], so the pieces are
    # 40h + 60h of the FULL 100h window: 20,000 + 30,000 kg.
    assert (a["start_h"], a["end_h"]) == (0.0, 40.0)
    assert (b["start_h"], b["end_h"]) == (46.0, 106.0)
    assert abs(a["qty_kg"] - 50000.0 * 40 / 100) < 1.0
    assert abs(b["qty_kg"] - 50000.0 * 60 / 100) < 1.0
    # total is exactly the MO quantity — nothing invented, nothing lost
    assert round(a["qty_kg"] + b["qty_kg"], 2) == 50000.0


def test_unsplit_mo_qty_untouched():
    from helpers.current_state import _clip_prod_around_cips

    mo = {"block_id": "b1", "order_id": "30051", "start_h": 0.0,
          "end_h": 30.0, "qty_kg": 21000.0, "attrs": ""}
    cip = {"block_id": "c1", "label": "CIP", "start_h": 50.0, "end_h": 56.0}
    pieces, kept = _clip_prod_around_cips(
        [dict(mo)], [dict(cip)], warnings=[], line="P09")
    assert len(pieces) == 1
    assert pieces[0]["qty_kg"] == 21000.0
