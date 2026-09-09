# tests/test_effective_rates.py
#
# Measured-rate overlay (helpers.effective_rates, 2026-09-08). Fixtures are
# built in tmp_path — never the live data/reference dir.
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from helpers.effective_rates import (apply_measured_rates, infer_data_dir,
                                     load_effective_capabilities,
                                     rate_source_summary)

ROOT = Path(__file__).resolve().parents[1]

RULES = """
[effective_rates]
enabled = {enabled}
table = "rates_by_line_sku.csv"
primary_rate_column = "recent_rate_kgph"
primary_runs_column = "recent_n_runs"
primary_min_runs = 3
fallback_rate_column = "rate_kgph"
fallback_runs_column = "n_runs"
fallback_min_runs = 3
side_divisor = 2.0
"""


def _fixture(tmp_path: Path, *, enabled: str = "true", with_hist: bool = True) -> Path:
    dd = tmp_path / "data"
    ref = dd / "reference"
    ref.mkdir(parents=True)
    pd.DataFrame([
        {"line_id": 0, "sku": "111", "line_name": "P09", "capable": 1, "calc_rate_kgph": 700.0},
        {"line_id": 1, "sku": "111", "line_name": "P10", "capable": 0, "calc_rate_kgph": 650.0},
        {"line_id": 2, "sku": "333", "line_name": "P11", "capable": 1, "calc_rate_kgph": 600.0},
        {"line_id": 8, "sku": "222", "line_name": "P17", "capable": 1, "calc_rate_kgph": 1400.0},
        {"line_id": 100, "sku": "222", "line_name": "P17A", "capable": 1, "calc_rate_kgph": 700.0},
    ]).to_csv(ref / "capabilities_rates.csv", index=False)
    if with_hist:
        hist = ref / "historical"
        hist.mkdir()
        (hist / "historical_run_log.rules.toml").write_text(RULES.format(enabled=enabled), encoding="utf-8")
        pd.DataFrame([
            # enough recent evidence -> recent rate wins
            {"line": "P09", "sku": 111, "n_runs": 10, "rate_kgph": 740.0, "recent_n_runs": 5, "recent_rate_kgph": 750.0},
            # too few recent runs, enough all-time -> all-time rate
            {"line": "P17", "sku": 222, "n_runs": 4, "rate_kgph": 1350.0, "recent_n_runs": 1, "recent_rate_kgph": 1300.0},
            # too few runs either way -> plant's calc rate stays
            {"line": "P10", "sku": 111, "n_runs": 2, "rate_kgph": 900.0, "recent_n_runs": 2, "recent_rate_kgph": 900.0},
        ]).to_csv(hist / "rates_by_line_sku.csv", index=False)
    return dd


def _by(df: pd.DataFrame, line: str, sku: str) -> pd.Series:
    return df[(df["line_name"] == line) & (df["sku"] == sku)].iloc[0]


def test_overlay_policy_primary_fallback_and_model(tmp_path):
    dd = _fixture(tmp_path)
    df = load_effective_capabilities(dd / "reference" / "capabilities_rates.csv")
    r = _by(df, "P09", "111")
    assert r["calc_rate_kgph"] == 750.0 and r["rate_source"] == "measured_recent" and r["rate_evidence_runs"] == 5
    assert r["calc_rate_kgph_model"] == 700.0
    r = _by(df, "P17", "222")
    assert r["calc_rate_kgph"] == 1350.0 and r["rate_source"] == "measured_alltime" and r["rate_evidence_runs"] == 4
    r = _by(df, "P10", "111")
    assert r["calc_rate_kgph"] == 650.0 and r["rate_source"] == "model" and r["rate_evidence_runs"] == 0
    r = _by(df, "P11", "333")
    assert r["calc_rate_kgph"] == 600.0 and r["rate_source"] == "model"


def test_side_rows_get_half_the_group_rate(tmp_path):
    dd = _fixture(tmp_path)
    df = load_effective_capabilities(dd / "reference" / "capabilities_rates.csv")
    assert _by(df, "P17A", "222")["calc_rate_kgph"] == 675.0


def test_capable_flags_are_never_changed(tmp_path):
    dd = _fixture(tmp_path)
    raw = pd.read_csv(dd / "reference" / "capabilities_rates.csv", dtype={"sku": str})
    df = load_effective_capabilities(dd / "reference" / "capabilities_rates.csv")
    assert df["capable"].tolist() == raw["capable"].tolist()
    assert len(df) == len(raw)


def test_disabled_policy_keeps_model_rates(tmp_path):
    dd = _fixture(tmp_path, enabled="false")
    df = load_effective_capabilities(dd / "reference" / "capabilities_rates.csv")
    assert set(df["rate_source"]) == {"model"}
    assert df["calc_rate_kgph"].tolist() == df["calc_rate_kgph_model"].tolist()


def test_missing_historical_dir_keeps_model_rates(tmp_path):
    dd = _fixture(tmp_path, with_hist=False)
    df = load_effective_capabilities(dd / "reference" / "capabilities_rates.csv")
    assert set(df["rate_source"]) == {"model"}


def test_work_dir_copy_is_not_re_overlaid(tmp_path):
    dd = _fixture(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    staged = load_effective_capabilities(dd / "reference" / "capabilities_rates.csv")
    staged.to_csv(work / "capabilities_rates.csv", index=False)
    assert infer_data_dir(work / "capabilities_rates.csv") is None
    again = load_effective_capabilities(work / "capabilities_rates.csv")
    # a second pass over an already-effective table must be the identity
    assert again["calc_rate_kgph"].tolist() == staged["calc_rate_kgph"].tolist()


def test_rate_source_summary_counts_capable_rows_only(tmp_path):
    dd = _fixture(tmp_path)
    df = load_effective_capabilities(dd / "reference" / "capabilities_rates.csv")
    s = rate_source_summary(df)
    assert s == {"measured_recent": 1, "measured_alltime": 2, "model": 1}


def test_apply_without_data_dir_is_model_only():
    df = pd.DataFrame([{"line_id": 0, "sku": "1", "line_name": "P09", "capable": 1, "calc_rate_kgph": 5.0}])
    out = apply_measured_rates(df, None)
    assert out["rate_source"].tolist() == ["model"] and out["calc_rate_kgph"].tolist() == [5.0]


# --- guard: no consumer bypasses the overlay -------------------------------
# The legacy inline normalisation (`rename rate_kgph -> calc_rate_kgph`) was
# the fingerprint of a direct capabilities read. Only the canonical loaders
# may carry it now; a new direct read anywhere else re-introduces a consumer
# that prices hours differently from the solver.
_RENAME = re.compile(r'rename\(columns=\{"rate_kgph":\s*"calc_rate_kgph"\}\)')
_ALLOWED = {"helpers/capability_check.py"}


def test_no_inline_capabilities_normalisation_outside_canonical_loader():
    hits = []
    for p in (ROOT / "code").rglob("*.py"):
        rel = p.relative_to(ROOT / "code").as_posix()
        if rel in _ALLOWED:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _RENAME.search(line):
                hits.append(f"{rel}:{i}")
    assert not hits, "direct capabilities reads bypass helpers.effective_rates:\n" + "\n".join(hits)
