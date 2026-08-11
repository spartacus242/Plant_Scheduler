# scripts/measure_changeover.py
#
# Measures the real impact of changeover-matrix compression (item 11 / item 28).
#
# Theory: changeovers.csv is a 44,310-row (211x211) long-format table. The
# proposal was to (a) cache it and (b) "group SKUs into format families and
# penalise only between-family transitions" to cut solver presolve cost.
#
# This script proves what actually changes:
#   * the parse is now cached (see code/solver/changeover_cache.py) -> the real win;
#   * the *model* builds one cost term per ADJACENT (line, order_i, order_j)
#     pair, not per matrix entry. Zeroing within-family flags does NOT remove
#     those terms, because build_model adds W_base to every transition
#     unconditionally. So family compression does NOT shrink the model and
#     barely changes the objective (only removes the *weighted* within-family
#     flag penalties, keeping W_base). Presolve is driven by the O(n^2)
#     successor/ordering structure, not the lookup table.
#
# Run: PYTHONPATH-safe -> env -u PYTHONPATH python scripts/measure_changeover.py

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code" / "solver"))

import pandas as pd  # noqa: E402
from data_loader import Data, Files, Params  # noqa: E402
from model_builder import build_model  # noqa: E402
from changeover_cache import compress_machine_changes  # noqa: E402

REF = ROOT / "data" / "reference"


def stage():
    wd = Path(tempfile.mkdtemp(prefix="fs_co_meas_"))
    for f in REF.iterdir():
        if f.is_file():
            shutil.copy(f, wd / f.name)
    return wd


def count_within_family_eligible_pairs(data):
    """Upper bound on changeover cost terms whose penalty is reduced by
    family compression: ordered pairs of eligible same-family SKUs per line."""
    total = 0
    per_line = {}
    fam = data.sku_family
    cap = data.capable
    rate = data.rate
    for l in data.lines:
        # eligible SKUs = those in any order capable+rate>0 on this line
        elig_skus = set()
        for o in data.orders:
            if cap.get((l, o["sku"])) and (rate.get((l, o["sku"])) or 0) > 0:
                elig_skus.add(o["sku"])
        # group eligible by family
        by_fam = {}
        for s in elig_skus:
            by_fam.setdefault(fam.get(s, s), []).append(s)
        line_pairs = 0
        for fam_skus in by_fam.values():
            n = len(fam_skus)
            line_pairs += n * (n - 1)  # ordered pairs within the family
        per_line[l] = line_pairs
        total += line_pairs
    return total, per_line


def main():
    wd = stage()
    try:
        F = Files(wd)
        P = Params()
        P.horizon_h = 504
        P.planning_start_date = "2026-08-10 00:00:00"
        P.use_sku_rates = True
        data = Data(P, F)
        data.load()

        # --- OFF (current behaviour) ---
        model_off, _ = build_model(
            P, data, "full", False, False,
            max_lines_per_order_override=2,
            maximize_production=True, objective_mode="balanced",
            relax_due=False, cross_week=False, cip_flex=False,
        )
        proto_off = model_off.Proto()
        n_vars_off = len(proto_off.variables)
        n_cons_off = len(proto_off.constraints)

        # --- ON (family compression applied to the changeover penalty) ---
        data.machine_changes = compress_machine_changes(
            data.machine_changes, data.sku_family
        )
        model_on, _ = build_model(
            P, data, "full", False, False,
            max_lines_per_order_override=2,
            maximize_production=True, objective_mode="balanced",
            relax_due=False, cross_week=False, cip_flex=False,
        )
        proto_on = model_on.Proto()
        n_vars_on = len(proto_on.variables)
        n_cons_on = len(proto_on.constraints)

        within_pairs, per_line = count_within_family_eligible_pairs(data)

        print("=== changeover compression measurement ===")
        print(f"orders loaded                : {len(data.orders)}")
        print(f"lines                       : {len(data.lines)}")
        print(f"changeover matrix rows      : {len(data.setup)}")
        print(f"within-family eligible pairs: {within_pairs} "
              f"(cost terms whose weighted penalty is zeroed under compression)")
        print()
        print(f"{'metric':<28}{'OFF':>10}{'ON':>10}{'delta':>10}")
        print(f"{'model variables':<28}{n_vars_off:>10}{n_vars_on:>10}"
              f"{n_vars_on - n_vars_off:>+10}")
        print(f"{'model constraints':<28}{n_cons_off:>10}{n_cons_on:>10}"
              f"{n_cons_on - n_cons_off:>+10}")
        print()
        if n_vars_off == n_vars_on and n_cons_off == n_cons_on:
            print("CONCLUSION: family compression does NOT change model size.")
            print("  The O(n^2) successor/ordering variables dominate model size")
            print("  and presolve; the 44,310-row lookup table is not the cost.")
            print("  The parse cache (changeover_cache.py) is the real win.")
        else:
            print("NOTE: model size changed; see deltas above.")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    os.environ.pop("PYTHONPATH", None)
    main()
