# tests/test_default_due_week_policy.py -- the SHIPPED due-week default is
# "hard" (decided 2026-09-04 night under the plant's change policy "benchmark
# before/after, revert if worse").
#
# Evidence (scratchpad bench/results/policy_soft_v2/REPORT.md, 600 s, two
# seeds): the v2 soft policy delivered 2.22 / 2.06 kt on time vs 3.59 / 3.36
# Mt for hard weeks (-38 %), 463 / 602 t late, W1 on-time 52 / 54 % vs 86 /
# 81 %, changeover hours 80.8 / 85.2 vs 73.8 / 73.2, and the plant's own
# acceptance criterion (a >= 30 t order must not move a week without several
# major changeovers saved) failed for 6 of 8 discretionary large late orders.
# Root cause: pass-1 search stalls on the larger soft model; the lateness is
# search residue, not a priced trade. So "hard" ships and "soft" stays an
# opt-in (tests/test_soft_week_policy.py exercises it with the key set).
#
# Every entry point that resolves the policy must agree on the default:
#   data_loader.Params / parse_due_week_policy / params_from_config,
#   model_builder.due_week_policy_of (tolerant of older Params objects),
#   independent_validator.Cfg / parse_due_week_policy / load_cfg (restated on
#   purpose -- the validator shares no code with the model),
#   helpers/solver_rules.py (the planner-facing rulebook row),
#   and the shipped flowstate.toml itself.

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "code", ROOT / "code" / "solver"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from data_loader import Params, parse_due_week_policy  # noqa: E402
from model_builder import due_week_policy_of, effective_due_end, soft_due_weeks  # noqa: E402
import phase2_scheduler as p2  # noqa: E402
from solver import independent_validator as iv  # noqa: E402
from helpers.solver_rules import solver_rules  # noqa: E402


def test_default_due_week_policy_is_hard():
    """Params, the parser fallbacks, params_from_config with the key absent,
    due_week_policy_of, the validator's Cfg / parser / load_cfg and the
    rulebook row all resolve to "hard" when nothing is configured. "soft" is
    still accepted everywhere (the opt-in); junk still raises."""
    # solver
    assert Params.due_week_policy == "hard"
    assert Params().due_week_policy == "hard"
    assert parse_due_week_policy(None) == "hard"
    assert parse_due_week_policy("") == "hard"
    assert parse_due_week_policy("  ") == "hard"
    assert parse_due_week_policy("soft") == "soft"
    assert parse_due_week_policy(" HARD ") == "hard"
    with pytest.raises(ValueError):
        parse_due_week_policy("wall")
    with pytest.raises(ValueError):
        parse_due_week_policy(True)
    assert p2.params_from_config({}).due_week_policy == "hard"
    assert p2.params_from_config({"scheduler": {}}).due_week_policy == "hard"
    assert p2.params_from_config({"scheduler": {"due_week_policy": ""}}).due_week_policy == "hard"
    assert p2.params_from_config({"scheduler": {"due_week_policy": "soft"}}).due_week_policy == "soft"
    # model_builder: the tolerant reader and the window it drives
    assert due_week_policy_of(Params()) == "hard"

    class _Old:  # a Params-like object from before the field existed
        pass

    assert due_week_policy_of(_Old()) == "hard"
    assert due_week_policy_of(Params(due_week_policy="soft")) == "soft"
    o = dict(order_id="X-W1", sku="X", due_start=168, due_end=335)
    P = Params(horizon_h=504)
    assert not soft_due_weeks(P, o)
    assert effective_due_end(P, o) == 336          # due_end + 1: the wall
    Ps = Params(horizon_h=504, due_week_policy="soft")
    assert soft_due_weeks(Ps, o)
    assert effective_due_end(Ps, o) == 504         # opt-in: the horizon
    # independent validator (restated parse)
    assert iv.Cfg().due_week_policy == "hard"
    assert iv.parse_due_week_policy(None) == "hard"
    assert iv.parse_due_week_policy("") == "hard"
    assert iv.parse_due_week_policy("Soft") == "soft"
    with pytest.raises(ValueError):
        iv.parse_due_week_policy("wall")
    assert iv.load_cfg({"scheduler": {}}).due_week_policy == "hard"
    assert iv.load_cfg({"scheduler": {"due_week_policy": "soft"}}).due_week_policy == "soft"
    # rulebook row
    rows = {r["id"]: r for r in solver_rules({})}
    assert rows["due_week_policy"]["default"] == "hard"
    assert rows["due_week_policy"]["value"] == "hard (default)"
    assert "opt-in" in rows["due_week_policy"]["planner"].lower()
    assert "2026-09-04" in rows["due_week_policy"]["planner"]


def test_shipped_flowstate_toml_parses_to_hard():
    """The repo's flowstate.toml spells the shipped default out and every
    reader resolves it the same way; the opt-in prices stay in the file at
    their documented values so opting in is one line."""
    cfg = tomllib.loads((ROOT / "flowstate.toml").read_text(encoding="utf-8"))
    sched = cfg["scheduler"]
    assert sched["due_week_policy"] == "hard"
    assert p2.params_from_config(cfg).due_week_policy == "hard"
    assert iv.load_cfg(cfg).due_week_policy == "hard"
    assert {r["id"]: r for r in solver_rules(cfg)}["due_week_policy"]["value"] == "hard"
    # the opt-in prices and decision #1 are untouched
    assert sched["late_kg_week_weight"] == 200000
    assert sched["early_kg_week_weight"] == 50000
    assert sched["early_fill_hours"] == "unbounded"
    P = p2.params_from_config(cfg)
    assert (P.late_kg_week_weight, P.early_kg_week_weight) == (200_000, 50_000)
    assert P.early_fill_hours is None
    # the toml documents why soft is off and how to opt in
    text = (ROOT / "flowstate.toml").read_text(encoding="utf-8")
    assert 'due_week_policy = "hard"' in text
    assert "OPT-IN" in text and "-38 %" in text
    assert "400000" in text and "100000" in text
