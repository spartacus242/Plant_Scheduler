# helpers/scenario_runner.py - Phase 2: generate solver scenarios A-D vs the current schedule.
#
# Wraps the CP-SAT solver (code/solver/) with different objective modes, imports
# results into calendar_blocks shape, and scores with the same scorecard engine.

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from helpers.calendar_io import import_legacy_schedule
from helpers.scorecard_engine import score_calendar
from helpers.version_manager import list_versions, save_version

# ---------------------------------------------------------------------------
# Solver knob documentation
#
# Every knob below is a real input to the CP-SAT objective built in
# code/solver/model_builder.py (the objective branches live around
# lines 1005-1127).  Knobs sourced from flowstate.toml are declared with a
# "config" key of the form "<section>.<key>"; their live value is resolved by
# scenario_knobs() against the loaded toml.  Knobs with a literal "value" are
# hard-coded multipliers inside the objective branch and are NOT tunable.
# ---------------------------------------------------------------------------

# Weighted changeover cost is assembled from the per-machine weights below; it
# feeds every objective mode, so it is shown for all scenarios.
_CO_WEIGHT_KNOBS = [
    {
        "param": "changeover.topload_weight",
        "config": "changeover.topload_weight",
        "effect": "Cost of a topload (carton/format) change on a line. High = campaign formats together.",
    },
    {
        "param": "changeover.ffs_weight",
        "config": "changeover.ffs_weight",
        "effect": "Cost of a form-fill-seal film change. High = avoid film swaps.",
    },
    {
        "param": "changeover.ttp_weight",
        "config": "changeover.ttp_weight",
        "effect": "Cost of a tray/thermoform (TTP) change.",
    },
    {
        "param": "changeover.casepacker_weight",
        "config": "changeover.casepacker_weight",
        "effect": "Cost of a case-packer pattern change.",
    },
    {
        "param": "changeover.base_changeover_weight",
        "config": "changeover.base_changeover_weight",
        "effect": "Flat cost charged for any SKU switch, on top of the per-machine costs.",
    },
    {
        "param": "changeover.conv_org_weight",
        "config": "changeover.conv_org_weight",
        "effect": "Extra cost for a conventional <-> organic switch (allergen/clean-down risk).",
    },
    {
        "param": "changeover.cinn_weight",
        "config": "changeover.cinn_weight",
        "effect": "Extra cost for switching in/out of cinnamon (carry-over risk).",
    },
    {
        "param": "changeover.flavor_weight",
        "config": "changeover.flavor_weight",
        "effect": "Extra cost for a plain flavour-to-flavour switch.",
    },
]

# Terms present in every objective branch.
_COMMON_KNOBS = [
    {
        "param": "objective.idle_weight",
        "config": "objective.idle_weight",
        "effect": "Penalty per idle hour on a line. Raise to pack runs tighter; 0 disables idle tracking.",
    },
    {
        "param": "objective.cip_defer_weight",
        "config": "objective.cip_defer_weight",
        "effect": "Bonus (subtracted) per hour a CIP is deferred into an existing gap. Raise to absorb CIP into downtime.",
    },
    {
        "param": "objective.late_weight",
        "config": "objective.late_weight",
        "effect": "Penalty per hour an order finishes past its due date (active from relax level 2).",
    },
    {
        "param": "objective.week_deviation_weight",
        "config": "objective.week_deviation_weight",
        "effect": "Cross-week mode only: penalty per hour an order runs outside AZAP's requested week. Lower = more willing to move a SKU between week 1 and week 2 to build a longer run.",
    },
    {
        "param": "objective.cip_flex_weight",
        "config": "objective.cip_flex_weight",
        "effect": "CIP flexibility mode only: percent the cip_defer reward is scaled to, so a CIP can be pulled EARLIER to absorb a changeover. The line's max allowable CIP interval stays HARD.",
    },
]

_FORMULA_MIN_CO = (
    "minimize  100 * weighted_changeover_cost"
    " + makespan"
    " + idle_h * idle_weight"
    " - cip_deferred_h * cip_defer_weight"
    " + late_h * late_weight"
    "\n(no per-machine weights available -> 10000 * changeover_count replaces the first term)"
)

_FORMULA_SPREAD = (
    "minimize  1000 * max_line_run_h"
    " + weighted_changeover_cost"
    " + makespan"
    " + idle_h * idle_weight"
    " - cip_deferred_h * cip_defer_weight"
    " + late_h * late_weight"
    "\n(no per-machine weights available -> 10 * changeover_count replaces the changeover term)"
)

_FORMULA_BALANCED = (
    "minimize  makespan * makespan_weight"
    " + weighted_changeover_cost * changeover_weight"
    " + idle_h * idle_weight"
    " - cip_deferred_h * cip_defer_weight"
    " + late_h * late_weight"
)

_KNOBS_MIN_CO = [
    {
        "param": "weighted changeover multiplier",
        "value": 100,
        "effect": "Hard-coded in min-changeovers mode: changeover cost dominates everything else.",
    },
    {
        "param": "makespan coefficient",
        "value": 1,
        "effect": "Fixed at 1 in this mode (makespan_weight is IGNORED) - schedule length is only a tiebreaker.",
    },
    {
        "param": "objective.changeover_weight",
        "value": "not used",
        "effect": "Ignored in min-changeovers mode; the fixed 100x multiplier is used instead.",
    },
] + _COMMON_KNOBS + _CO_WEIGHT_KNOBS

_KNOBS_SPREAD = [
    {
        "param": "max_line_run multiplier",
        "value": 1000,
        "effect": "Hard-coded: minimizes the busiest line's total run hours, i.e. levels load across lines.",
    },
    {
        "param": "weighted changeover multiplier",
        "value": 1,
        "effect": "Changeovers are only a light tiebreaker here, so lines stay loaded.",
    },
    {
        "param": "objective.makespan_weight / changeover_weight",
        "value": "not used",
        "effect": "Both are IGNORED in spread-load mode; coefficients are fixed by the branch.",
    },
] + _COMMON_KNOBS + _CO_WEIGHT_KNOBS

_KNOBS_BALANCED = [
    {
        "param": "objective.makespan_weight",
        "config": "objective.makespan_weight",
        "effect": "Weight on total schedule length. Raise to finish the week earlier.",
    },
    {
        "param": "objective.changeover_weight",
        "config": "objective.changeover_weight",
        "effect": "Multiplier on the weighted changeover cost. Raise to trade makespan for fewer switches.",
    },
] + _COMMON_KNOBS + _CO_WEIGHT_KNOBS

# Flat override keys the UI/caller may supply, mapped to their toml section.
OVERRIDE_SECTIONS: dict[str, str] = {
    "makespan_weight": "objective",
    "changeover_weight": "objective",
    "cip_defer_weight": "objective",
    "idle_weight": "objective",
    "late_weight": "objective",
    "week_deviation_weight": "objective",
    "cip_flex_weight": "objective",
    "base_changeover_weight": "changeover",
    "topload_weight": "changeover",
    "ttp_weight": "changeover",
    "ffs_weight": "changeover",
    "casepacker_weight": "changeover",
    "conv_org_weight": "changeover",
    "cinn_weight": "changeover",
    "flavor_weight": "changeover",
}

OBJECTIVE_MODES = ("balanced", "min-changeovers", "spread-load")

# Fallbacks matching Params in code/solver/data_loader.py, used when a
# key is absent from flowstate.toml so the knob table never shows a blank.
SOLVER_DEFAULTS: dict[str, int] = {
    "objective.makespan_weight": 1,
    "objective.changeover_weight": 100,
    "objective.cip_defer_weight": 10,
    "objective.idle_weight": 0,
    "objective.late_weight": 200,
    "objective.week_deviation_weight": 40,
    "objective.cip_flex_weight": 20,
    "changeover.topload_weight": 50,
    "changeover.ttp_weight": 10,
    "changeover.ffs_weight": 10,
    "changeover.casepacker_weight": 10,
    "changeover.base_changeover_weight": 5,
    "changeover.conv_org_weight": 30,
    "changeover.cinn_weight": 20,
    "changeover.flavor_weight": 5,
}

CUSTOM_SCENARIO_ID = "X"

# Scenario definitions — mapped to legacy --objective modes (+ notes).
SCENARIOS = [
    {
        "id": "A",
        "name": "Scenario A — Minimum changeovers",
        "objective": "min-changeovers",
        "two_phase": True,
        "intent": "Minimize SKU switches above all else.",
        "objective_formula": _FORMULA_MIN_CO,
        "knobs": _KNOBS_MIN_CO,
    },
    {
        "id": "B",
        "name": "Scenario B — Maximum throughput",
        "objective": "spread-load",
        "two_phase": True,
        "intent": "Spread load / keep lines productive (proxy for throughput).",
        "objective_formula": _FORMULA_SPREAD,
        "knobs": _KNOBS_SPREAD,
    },
    {
        "id": "C",
        "name": "Scenario C — Maintenance friendly",
        "objective": "balanced",
        "two_phase": True,
        "intent": (
            "Balanced solve; planner should align maintenance with CIP in the twin. "
            "v0 uses balanced weights — tune CIP defer / idle in legacy config for stronger maint bias."
        ),
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
    {
        "id": "D",
        "name": "Scenario D — Balanced",
        "objective": "balanced",
        "two_phase": True,
        "intent": "Minimize makespan + weighted changeovers (recommended default).",
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
    {
        "id": CUSTOM_SCENARIO_ID,
        "name": "Custom scenario",
        "objective": "balanced",
        "two_phase": True,
        "intent": "Planner-defined objective mode and weight overrides.",
        "custom": True,
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
    {
        "id": "E",
        "name": "Scenario E — Current state + demand",
        "objective": "balanced",
        # Single-phase (full 336h in one model). The two-phase driver splits
        # week 0 (168h) from week 1, which structurally cannot place current
        # MOs whose remaining work spans the whole horizon. Single-phase
        # keeps them on their locked line and lets mo_changes.csv record the
        # split/trim/reorder for VIF write-back.
        "two_phase": False,
        "intent": (
            "Re-optimize the current plant state: manprg running/queued MOs "
            "are locked to their line (committed work), tonnage adjustable; "
            "new demand meshes into the tail. mo_changes.csv records every "
            "change for VIF write-back."
        ),
        "objective_formula": _FORMULA_BALANCED,
        "knobs": _KNOBS_BALANCED,
    },
]

# Formula / knob table per objective mode — used to re-describe a custom
# scenario when the planner picks a different mode.
MODE_DOCS: dict[str, dict[str, Any]] = {
    "min-changeovers": {"objective_formula": _FORMULA_MIN_CO, "knobs": _KNOBS_MIN_CO},
    "spread-load": {"objective_formula": _FORMULA_SPREAD, "knobs": _KNOBS_SPREAD},
    "balanced": {"objective_formula": _FORMULA_BALANCED, "knobs": _KNOBS_BALANCED},
}


def make_custom_scenario(
    name: str,
    objective: str,
    overrides: dict[str, Any] | None = None,
    *,
    two_phase: bool = True,
    cross_week: bool = False,
    cip_flex: bool = False,
) -> dict[str, Any]:
    """Build a runnable CUSTOM scenario dict from planner input."""
    mode = objective if objective in OBJECTIVE_MODES else "balanced"
    docs = MODE_DOCS[mode]
    return {
        "id": CUSTOM_SCENARIO_ID,
        "name": (name or "Custom scenario").strip() or "Custom scenario",
        "objective": mode,
        "two_phase": two_phase,
        "cross_week": bool(cross_week),
        "cip_flex": bool(cip_flex),
        "custom": True,
        "intent": f"Custom solve ({mode}) with planner weight overrides.",
        "objective_formula": docs["objective_formula"],
        "knobs": docs["knobs"],
        "overrides": dict(overrides or {}),
    }


def config_value(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Look up '<section>.<key>' in a loaded flowstate.toml dict."""
    section, _, key = dotted.partition(".")
    return (cfg.get(section) or {}).get(key, default)


def scenario_knobs(
    scenario: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Resolve a scenario's knob table into concrete (param, value, effect) rows."""
    cfg = cfg or {}
    overrides = overrides or {}
    rows: list[dict[str, Any]] = []
    for knob in scenario.get("knobs", []):
        dotted = knob.get("config")
        if dotted:
            key = dotted.partition(".")[2]
            if key in overrides and overrides[key] is not None:
                value = overrides[key]
            else:
                section = dotted.partition(".")[0]
                if key in (cfg.get(section) or {}):
                    value = cfg[section][key]
                else:
                    fallback = SOLVER_DEFAULTS.get(dotted)
                    value = (
                        f"{fallback} (solver default)" if fallback is not None else "(solver default)"
                    )
        else:
            value = knob.get("value", "")
        rows.append(
            {
                "Parameter": knob["param"],
                # Stringified: the column mixes ints and notes like "not used",
                # and a mixed-type column fails Arrow serialization in Streamlit.
                "Value": str(value),
                "What it does": knob["effect"],
            }
        )
    return rows


def _prepare_work_dir(data_dir: Path, work: Path) -> None:
    """Copy the solver's data inputs into a work directory.

    De-legacy (2026-08-10): the solver reads ONLY from its work dir, and the
    work dir is sourced exclusively from data/reference/ — no more copies out
    of Flowstate-legacy/data. All solver inputs are already in reference/.
    """
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    ref = data_dir / "reference"

    mapping = {
        "changeovers.csv": "changeovers.csv",
        "downtimes.csv": "downtimes.csv",
        "demand_plan.csv": "demand_plan.csv",
        "capabilities_rates.csv": "capabilities_rates.csv",
        "line_cip_hrs.csv": "line_cip_hrs.csv",
        "trials.csv": "trials.csv",
        "sku_info.csv": "sku_info.csv",
        "initial_states.csv": "initial_states.csv",
    }
    missing: list[str] = []
    for src_name, dst_name in mapping.items():
        src = ref / src_name
        if src.exists():
            shutil.copy2(src, work / dst_name)
        else:
            missing.append(src_name)

    root_toml = data_dir.parent / "flowstate.toml"
    if root_toml.exists():
        shutil.copy2(root_toml, work / "flowstate.toml")

    if missing:
        # The solver's data_loader raises on missing files; the caller turns
        # that into a visible "solver failed" — but be loud about it here too.
        import warnings

        warnings.warn(
            f"Solver work dir missing reference inputs: {', '.join(missing)}"
        )


def _audit_work_downtimes(work: Path) -> list[str]:
    """Report downtime windows that went stale when the horizon grew.

    Reads the work-dir downtimes.csv and flowstate.toml (both already staged)
    so the audit sees exactly what the solver will see. Returns note strings
    that the caller prepends to the run log -- always a list, never silent.
    """
    from helpers.downtime_horizon import audit_downtime_horizon

    import pandas as pd

    dt_path = work / "downtimes.csv"
    if not dt_path.exists():
        return []
    horizon = 0.0
    toml_path = work / "flowstate.toml"
    if toml_path.exists():
        try:
            import tomllib

            cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            sch = cfg.get("scheduler", {}) or {}
            horizon = float(sch.get("horizon_hours")
                            or (float(sch.get("horizon_weeks", 0) or 0) * 168.0))
        except Exception:  # noqa: BLE001
            horizon = 0.0
    if horizon <= 0:
        return []
    rows = pd.read_csv(dt_path, encoding="utf-8-sig", dtype=str,
                       keep_default_na=False).to_dict("records")
    return audit_downtime_horizon(rows, horizon)


def _overlay_current_state(work: Path, data_dir: Path) -> list[str]:
    """Patch the work-dir initial_states.csv with the real plant state.

    Handoff WW32 item 3: the solver must start from ground truth, not from the
    seeded fixture. From `helpers.current_state` we take, per line:
      * `available_from_hour` <- line_free_h (end of the locked running MO and
        everything already queued behind it) — the solver may not place work
        before it;
      * `initial_sku`         <- the SKU actually running (so the changeover
        matrix charges the real changeover, not a CLEAN start);
      * `carryover_run_hours_since_last_cip_at_t0` <- hours since the line's
        last CIP, so the first CIP is due at the right time.

    Returns a list of human-readable notes. Best-effort: if the live feeds are
    missing the initial_states file is left alone, but the reason is REPORTED
    rather than silently swallowed (a bare `except ImportError` here previously
    made the whole overlay a no-op because of a wrong import path).
    """
    notes: list[str] = []
    import pandas as _pd

    from helpers.calendar_io import load_lines as _ll  # NOT lines_model
    from helpers.config import datasources_config as _ds
    from helpers.config import load_toml as _lt
    from helpers.current_state import build_current_state as _bcs
    from helpers.horizon import resolve as _hr
    from helpers.paths import data_dir as _dd

    dd = data_dir if Path(data_dir).name == "data" else _dd()
    dd = Path(dd)
    cfg = _lt()
    hz = _hr(cfg)
    ds = _ds(cfg)
    mp_paths = [p.strip() for p in str(ds.get("manprg_files", "")).split(";")
                if p.strip()] or [
                    str(dd / "reference" / "manprg.txt"),
                    str(dd / "reference" / "manprg2.txt")]
    cip_path = str(ds.get("cip_info_csv", "")).strip() or str(
        dd / "reference" / "cip_info.csv")
    try:
        cs = _bcs(hz, manprg_paths=mp_paths, cip_path=cip_path,
                  lines=_ll(dd / "lines.csv"), cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        return [f"current-state overlay skipped: {exc}"]

    init_path = Path(work) / "initial_states.csv"
    if not init_path.exists():
        return ["current-state overlay skipped: no initial_states.csv in work dir"]
    init = _pd.read_csv(init_path)
    if init.empty:
        return ["current-state overlay skipped: initial_states.csv is empty"]

    free_map = {ln.upper(): max(0, int(h)) for ln, h in cs.line_free_h.items()}
    # When current_mo.csv is produced, the queued MOs become solver orders and
    # the availability gate must be only the RUNNING MO's end — otherwise the
    # gate double-counts the queued work and blocks the very MOs the solver
    # should place. Use line_running_free_h (falls back to line_free_h).
    running_free_map = {
        ln.upper(): max(0, int(h)) for ln, h in cs.line_running_free_h.items()
    }
    _use_cmo = str(
        cfg.get("scheduler", {}).get("use_current_mo", "")).strip().lower()
    if _use_cmo in ("1", "true", "yes"):
        use_running_gate = True
    elif _use_cmo in ("0", "false", "no"):
        use_running_gate = False
    else:
        use_running_gate = bool(running_free_map)  # default: running-only gate
    sku_map = {r["line"].upper(): r["item"] for r in cs.running}
    # hours since the last CIP -> the first CIP is due MaxHoursBetweenCIP later
    from helpers.cip_import import read_cip_info as _rci
    carry_map: dict[str, int] = {}
    for line, info in _rci(cip_path).by_line.items():
        if info.previous_cip is not None:
            hrs = (hz.anchor - info.previous_cip.to_pydatetime()).total_seconds() / 3600.0
            if hrs > 0:
                # A carryover >= the line's max CIP interval says "a CIP was
                # already overdue before hour 0", which the CIP constraints
                # cannot satisfy and turns the whole solve INFEASIBLE. Clamp
                # just below the limit so the solver schedules the CIP
                # immediately instead of failing. phase2_scheduler does the
                # same (min(carryover, 119)) when it writes week-1 states.
                limit = int(info.max_hours_between or 120)
                carry_map[line.upper()] = int(min(hrs, max(0, limit - 1)))

    changed = 0
    gate_map = running_free_map if use_running_gate else free_map
    for idx, row in init.iterrows():
        ln = str(row.get("line_name", "")).strip().upper()
        if ln in gate_map and gate_map[ln] > 0:
            init.at[idx, "available_from_hour"] = gate_map[ln]
            changed += 1
        if ln in sku_map and sku_map[ln]:
            init.at[idx, "initial_sku"] = sku_map[ln]
        if ln in carry_map and "carryover_run_hours_since_last_cip_at_t0" in init.columns:
            init.at[idx, "carryover_run_hours_since_last_cip_at_t0"] = carry_map[ln]

    init.to_csv(init_path, index=False)
    gate_note = "running-MO end" if use_running_gate else "running+queued end"
    notes.append(
        f"current state overlaid: {changed} line(s) gated by {gate_note}, "
        f"{len(sku_map)} initial SKU(s), {len(carry_map)} CIP carryover(s)")

    # ── current_mo.csv: running + queued MOs as solver input ─────────────
    # Each row is an MO locked to its manprg line; the solver may split /
    # reorder / trim it, and mo_changes.csv records the delta for VIF.
    # Only written when use_current_mo is enabled (scenario E).
    if not use_running_gate:
        return notes
    cmo_rows = []
    for r in cs.running:
        left_cas = float(r.get("left_cas", 0) or 0)
        fct_cas = float(r.get("fct_cas", 0) or 0)
        qty_kg = float(r.get("qty_kg", 0) or 0)
        remaining = round(qty_kg * (left_cas / fct_cas), 3) \
            if fct_cas > 0 else qty_kg
        cmo_rows.append({
            "mo": r["mo"], "line_name": str(r["line"]).upper(),
            "sku": r["item"], "remaining_kg": remaining,
            "due_start_h": 0, "due_end_h": 335,
            "locked_line": 1, "source": "manprg",
        })
    for r in cs.queued:
        remaining = round(float(r.get("qty_kg", 0) or 0), 3)
        start_h = max(0, int(_hours_h(r.get("placed_start"), hz.anchor)))
        end_h = max(start_h + 1, int(_hours_h(r.get("placed_end"), hz.anchor)))
        cmo_rows.append({
            "mo": r["mo"], "line_name": str(r["line"]).upper(),
            "sku": r["item"], "remaining_kg": remaining,
            "due_start_h": start_h, "due_end_h": end_h,
            "locked_line": 1, "source": "manprg",
        })
    if cmo_rows:
        _pd.DataFrame(cmo_rows).to_csv(Path(work) / "current_mo.csv", index=False)
        notes.append(
            f"current_mo.csv: {len(cmo_rows)} MO(s) locked to their manprg line")
    return notes


def _hours_h(when, anchor) -> float:
    """Hours from the horizon anchor to `when` (datetime/pandas Timestamp)."""
    import pandas as _pd
    ts = _pd.Timestamp(when).to_pydatetime()
    return (ts - anchor).total_seconds() / 3600.0


def normalize_overrides(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only known weight keys with a real value; coerce to int.

    Accepts flat keys ('makespan_weight') or dotted ('objective.makespan_weight').
    """
    clean: dict[str, Any] = {}
    for raw_key, value in (overrides or {}).items():
        key = raw_key.partition(".")[2] if "." in raw_key else raw_key
        if key not in OVERRIDE_SECTIONS or value is None:
            continue
        try:
            clean[key] = int(value)
        except (TypeError, ValueError):
            continue
    return clean


def _patch_work_toml(
    toml: Path,
    time_limit: int | None,
    overrides: dict[str, Any] | None = None,
) -> None:
    """Write time limit + weight overrides into the work-dir flowstate.toml.

    With no overrides this keeps the historical in-place regex patch so the
    copied config (comments and all) is byte-identical apart from time_limit.
    With overrides it round-trips the file through tomllib / tomli_w.
    """
    overrides = normalize_overrides(overrides)
    if not overrides:
        if time_limit is None:
            return
        try:
            import re

            text = toml.read_text(encoding="utf-8")
            text = re.sub(r"time_limit\s*=\s*\d+", f"time_limit = {int(time_limit)}", text)
            toml.write_text(text, encoding="utf-8")
        except Exception:
            pass
        return

    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - py<3.11
            import tomli as tomllib  # type: ignore
        import tomli_w

        with open(toml, "rb") as fh:
            cfg = tomllib.load(fh)
        if time_limit is not None:
            cfg.setdefault("scheduler", {})["time_limit"] = int(time_limit)
        for key, value in overrides.items():
            section = OVERRIDE_SECTIONS[key]
            cfg.setdefault(section, {})[key] = value
            # [objective] carries duplicate co_* keys that phase2_scheduler
            # applies before [changeover]; keep them in step so the override wins
            # regardless of which section the solver reads last.
            if section == "changeover" and key in ("conv_org_weight", "cinn_weight", "flavor_weight"):
                cfg.setdefault("objective", {})[f"co_{key}"] = value
        with open(toml, "wb") as fh:
            tomli_w.dump(cfg, fh)
    except Exception:
        # Never let config rewriting kill a solve; fall back to time-limit only.
        if time_limit is not None:
            try:
                import re

                text = toml.read_text(encoding="utf-8")
                text = re.sub(r"time_limit\s*=\s*\d+", f"time_limit = {int(time_limit)}", text)
                toml.write_text(text, encoding="utf-8")
            except Exception:
                pass


def _read_feasibility(work: Path) -> dict[str, Any] | None:
    """Read feasibility_report.json from a solver work dir (written every run)."""
    import json
    path = work / "feasibility_report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_diag_blockages(work: Path) -> str:
    """Human-readable blockages summary from the solver work dir (if any)."""
    path = work / "diag_blockages.txt"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def run_scenario(
    scenario: dict[str, Any],
    data_dir: Path,
    *,
    time_limit: int | None = None,
    python_exe: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one scenario. Returns {ok, calendar, scorecard, log, returncode,
    feasibility, relax_level}.

    ``overrides`` is a flat weight mapping (see OVERRIDE_SECTIONS) written into
    the work-dir flowstate.toml before the solver runs. Scenarios may also carry
    their own 'overrides' key (custom scenarios); the argument wins.
    """
    work = (Path(data_dir) / "_scenario_work" / scenario["id"]).resolve()
    _prepare_work_dir(Path(data_dir).resolve(), work)

    # Inject the current plant state: per-line free-from hour, running SKU and
    # CIP carryover so the solver cannot schedule over a locked running MO.
    # Best-effort, but ALWAYS reported — a silent no-op here means the solver
    # quietly plans from the stale fixture.
    try:
        _cs_notes = _overlay_current_state(work, data_dir)
    except Exception as _exc:  # noqa: BLE001
        _cs_notes = [f"current-state overlay FAILED: {_exc}"]

    # Downtime windows are stored as hour offsets, so they go stale whenever the
    # horizon grows: a "0-336h" row written under a 2-week horizon silently frees
    # the line for hours 336-504 of a 3-week plan. Detect and report; never rewrite.
    try:
        _cs_notes.extend(_audit_work_downtimes(work))
    except Exception as _exc:  # noqa: BLE001
        _cs_notes.append(f"downtime horizon audit FAILED: {_exc}")

    scheduler = (Path(data_dir).resolve().parent / "code" / "solver" / "phase2_scheduler.py").resolve()
    toml = work / "flowstate.toml"
    cmd = [
        python_exe or sys.executable,
        str(scheduler),
        "--data-dir", str(work),
        "--objective", scenario["objective"],
        "--config", str(toml.resolve()),
    ]
    if scenario.get("two_phase"):
        cmd.append("--two-phase")
    # Cross-week mode makes the solver ignore --two-phase and solve the whole
    # 336h horizon at once (see phase2_scheduler). Both default to off, so a
    # scenario that does not set them behaves exactly as before.
    if scenario.get("cross_week"):
        cmd.append("--cross-week")
    if scenario.get("cip_flex"):
        cmd.append("--cip-flex")
    # legacy reads time_limit + all weights from the toml; patch the work copy.
    eff_overrides = overrides if overrides is not None else scenario.get("overrides")
    _patch_work_toml(toml, time_limit, eff_overrides)

    proc = subprocess.run(
        cmd,
        cwd=str((Path(data_dir).resolve().parent / "code" / "solver").resolve()),
        capture_output=True,
        text=True,
        timeout=max(120, (time_limit or 60) * 4 + 60),
    )
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    err_file = work / "solver_error.txt"
    if err_file.exists():
        log += "\n" + err_file.read_text(encoding="utf-8")
    if _cs_notes:
        log = "[current state] " + "; ".join(_cs_notes) + "\n" + log

    sched = work / "schedule_phase2.csv"
    cip = work / "cip_windows.csv"
    feas = _read_feasibility(work)
    relax_level = feas.get("relax_level") if feas else None
    diag_blockages = _read_diag_blockages(work)
    if proc.returncode != 0 or not sched.exists():
        return {
            "ok": False,
            "returncode": proc.returncode,
            "log": log,
            "calendar": None,
            "scorecard": None,
            "feasibility": feas,
            "relax_level": relax_level,
            "diag_blockages": diag_blockages,
        }

    calendar = import_legacy_schedule(
        sched,
        cip if cip.exists() else None,
        work / "downtimes.csv" if (work / "downtimes.csv").exists() else None,
    )
    score = score_calendar(calendar, week_label=scenario["name"], data_dir=data_dir)
    return {
        "ok": True,
        "returncode": proc.returncode,
        "log": log,
        "calendar": calendar,
        "scorecard": score,
        "feasibility": feas,
        "relax_level": relax_level,
        "diag_blockages": diag_blockages,
    }


def save_scenario_version(
    scenario: dict[str, Any],
    result: dict[str, Any],
    data_dir: Path,
) -> str:
    """Persist scenario as a named version (may delete oldest if at capacity — caller should manage slots)."""
    if not result.get("ok") or result.get("calendar") is None:
        raise ValueError("Scenario did not produce a calendar")
    # If full, delete a previous scenario occupying the same slot
    existing = list_versions(data_dir)
    is_custom = bool(scenario.get("custom"))
    source = (
        f"solver-custom:{scenario['objective']}" if is_custom else f"solver:{scenario['objective']}"
    )
    slug_hint = f"scenario_{scenario['id'].lower()}"
    for v in existing:
        if is_custom:
            match = str(v.get("source", "")).startswith("solver-custom:")
        else:
            match = v.get("slug", "").startswith(slug_hint) or v.get("name", "").startswith(
                f"Scenario {scenario['id']}"
            )
        if match:
            from helpers.version_manager import delete_version
            delete_version(v["slug"], data_dir)
            break
    # Still at max? raise
    if len(list_versions(data_dir)) >= 5:
        raise ValueError("Version slots full (5). Delete a version before generating scenarios.")

    sc = result["scorecard"]
    return save_version(
        scenario["name"],
        result["calendar"],
        sc.to_dict() if hasattr(sc, "to_dict") else sc,
        data_dir,
        notes=scenario.get("intent", ""),
        source=source,
        pros="",
        cons="",
    )
