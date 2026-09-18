# tests/test_initial_states_synth.py — 2026-09-18.
#
# data/reference/initial_states.csv was a hand fixture (2026-08-07) copied
# byte-for-byte into every solver work dir; the overlays rewrote SOME fields
# under SOME conditions and the rest leaked into live solves (P10's phantom
# 2 h long-shutdown setup; 6-week-old CIP carryovers on P11/P13/P15; Scenario
# F's projected grid phased from the fixture instead of the board's rule).
# Now the work copy is SYNTHESIZED from the line set with every field reset.

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers import scenario_runner as sr  # noqa: E402

RESET = {"initial_sku": "CLEAN", "available_from_hour": 0, "long_shutdown_flag": 0,
         "long_shutdown_extra_setup_hours": 0, "carryover_run_hours_since_last_cip_at_t0": 0}


def _lines(dd: Path, names=("P09", "P10", "P12")):
    pd.DataFrame([{"line_id": i, "line_name": n, "active": True}
                  for i, n in enumerate(names)]).to_csv(dd / "lines.csv", index=False)


def _poisoned_reference(ref: Path, names=("P09", "P10", "P12")):
    ref.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"line_id": i, "line_name": n, "initial_sku": "999",
                   "available_from_hour": 40, "long_shutdown_flag": 1,
                   "long_shutdown_extra_setup_hours": 2,
                   "carryover_run_hours_since_last_cip_at_t0": 50,
                   "last_cip_end_datetime": "2026-08-01 00:00"}
                  for i, n in enumerate(names)]).to_csv(ref / "initial_states.csv", index=False)


def _caps(work: Path, names=("P09", "P10", "P12")):
    pd.DataFrame([{"line_id": i, "line_name": n, "sku": s, "capable": 1, "calc_rate_kgph": 500.0}
                  for i, n in enumerate(names) for s in ("111", "222")]
                 ).to_csv(work / "capabilities_rates.csv", index=False)


def _assert_reset(df: pd.DataFrame, names):
    assert list(df.columns) == list(sr.INIT_STATE_COLUMNS)
    assert list(df["line_name"]) == list(names)
    for col, val in RESET.items():
        assert (df[col] == val).all(), (col, df[col].tolist())
    assert df["last_cip_end_datetime"].isna().all()


def test_synthesized_from_lines_csv(tmp_path):
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    _lines(dd)
    _poisoned_reference(dd / "reference")          # present but must be ignored
    notes = sr._synthesize_initial_states(work, dd)
    out = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str})
    _assert_reset(out, ("P09", "P10", "P12"))
    assert out["comment"].str.contains("lines.csv").all()
    assert any("synthesized from lines.csv (3 lines)" in n for n in notes), notes
    assert any("reference fixture is not read" in n for n in notes), notes


def test_falls_back_to_the_reference_row_set_with_fields_reset(tmp_path):
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    _poisoned_reference(dd / "reference", ("P09", "P22"))
    notes = sr._synthesize_initial_states(work, dd)
    out = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str})
    _assert_reset(out, ("P09", "P22"))                # row SET kept, VALUES reset
    assert any("reference initial_states.csv row set (fields reset)" in n for n in notes), notes


def test_falls_back_to_the_staged_capabilities_line_set(tmp_path):
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    _caps(work, ("P13", "P15"))
    notes = sr._synthesize_initial_states(work, dd)
    out = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str})
    _assert_reset(out, ("P13", "P15"))
    assert any("capabilities_rates.csv line set" in n for n in notes), notes


def test_no_line_source_writes_nothing_and_says_so(tmp_path):
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    notes = sr._synthesize_initial_states(work, dd)
    assert not (work / "initial_states.csv").exists()
    assert any("NOT synthesized" in n for n in notes), notes


def test_capabilities_line_missing_from_lines_csv_is_reported(tmp_path):
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    _lines(dd, ("P09", "P10"))
    _caps(work, ("P09", "P10", "P21"))
    notes = sr._synthesize_initial_states(work, dd)
    out = pd.read_csv(work / "initial_states.csv")
    assert list(out["line_name"]) == ["P09", "P10"]
    assert any("model defaults apply" in n and "P21" in n for n in notes), notes


def test_prepare_work_dir_does_not_copy_the_fixture(tmp_path):
    """The staging entry point: a poisoned reference file next to lines.csv
    — the work copy is the synthesized one, byte for byte unrelated."""
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir()
    _lines(dd)
    _poisoned_reference(dd / "reference")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")               # other reference inputs absent
        sr._prepare_work_dir(dd, work)
    out = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str})
    _assert_reset(out, ("P09", "P10", "P12"))
    assert not (out["initial_sku"] == "999").any()


def test_malformed_rows_are_skipped_and_reported_without_truncating(tmp_path):
    """A non-numeric line_id or a blank name in the MIDDLE of lines.csv must
    not lose the lines after it (the first cut appended per row inside one
    try block: a bad third row wrote a two-line file and skipped every
    fallback)."""
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    pd.DataFrame([{"line_id": 0, "line_name": "P09", "active": True},
                  {"line_id": "P17B", "line_name": "P17B", "active": True},   # non-numeric id
                  {"line_id": 3, "line_name": None, "active": True},          # blank name
                  {"line_id": 4, "line_name": "P13", "active": True}]
                 ).to_csv(dd / "lines.csv", index=False)
    notes = sr._synthesize_initial_states(work, dd)
    out = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str})
    assert list(out["line_name"]) == ["P09", "P13"]
    _assert_reset(out, ("P09", "P13"))
    assert any("2 malformed row(s) skipped" in n and "P17B" in n for n in notes), notes
    assert any("synthesized from lines.csv (2 lines)" in n for n in notes), notes


def test_all_rows_malformed_falls_through_to_the_next_source(tmp_path):
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    pd.DataFrame([{"line_id": "x", "line_name": "P09", "active": True}]
                 ).to_csv(dd / "lines.csv", index=False)
    _caps(work, ("P09", "P10"))
    notes = sr._synthesize_initial_states(work, dd)
    out = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str})
    _assert_reset(out, ("P09", "P10"))
    assert any("capabilities_rates.csv line set" in n for n in notes), notes


def test_inactive_line_keeps_a_row_and_duplicate_id_keeps_the_first(tmp_path):
    """The solver wants a start-state row per capabilities line, active or
    not; a duplicated line_id is a data error — first row wins, reported."""
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    pd.DataFrame([{"line_id": 0, "line_name": "P09", "active": True},
                  {"line_id": 1, "line_name": "P10", "active": False},
                  {"line_id": 1, "line_name": "P10DUP", "active": True}]
                 ).to_csv(dd / "lines.csv", index=False)
    notes = sr._synthesize_initial_states(work, dd)
    out = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str})
    assert list(out["line_name"]) == ["P09", "P10"]
    assert any("duplicate line_id" in n and "P10DUP" in n for n in notes), notes


def test_overlays_resynthesize_over_an_existing_work_copy(tmp_path):
    """Both overlays call the synthesizer on every staging: a poisoned work
    copy (any older path, a hand edit) never survives — every field."""
    dd = tmp_path / "plant"; work = tmp_path / "work"
    dd.mkdir(); work.mkdir()
    _lines(dd)
    poison = pd.DataFrame([{"line_id": 0, "line_name": "P09", "initial_sku": "999",
                            "available_from_hour": 40, "long_shutdown_flag": 1,
                            "long_shutdown_extra_setup_hours": 2,
                            "carryover_run_hours_since_last_cip_at_t0": 50,
                            "last_cip_end_datetime": "2026-08-01 00:00"}])
    poison.to_csv(work / "initial_states.csv", index=False)
    notes = sr._synthesize_initial_states(work, dd)
    out = pd.read_csv(work / "initial_states.csv", dtype={"initial_sku": str})
    _assert_reset(out, ("P09", "P10", "P12"))           # the poisoned single row is gone
    assert any("synthesized from lines.csv (3 lines)" in n for n in notes), notes
