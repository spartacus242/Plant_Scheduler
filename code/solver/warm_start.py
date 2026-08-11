"""Warm-start (CP-SAT solution hinting) for the Flowstate scheduler.

Task list item 10. The model is re-solved against a world that changed only
slightly since the last run, so the previous schedule is a near-feasible
starting point. CP-SAT's `AddHint` lets the search begin from it instead of
from scratch.

Hint source
-----------
`prev_schedule.csv` in the solver work dir: a verbatim copy of the previous
run's `schedule_phase2.csv`, preserved by `scenario_runner._prepare_work_dir`
before it wipes the work dir. Columns used: `line_id`, `order_id`,
`start_hour`, `end_hour`, `run_hours`.

Design rules
------------
* **A hint is advice, not a constraint.** CP-SAT repairs an infeasible hint
  (`fix_variables_to_their_hinted_value` stays False), so a stale or partly
  wrong hint can never change the feasible set — only the search path. That is
  what makes this safe to enable by default.
* **Never hint outside a variable's domain.** The horizon can grow between
  runs (item 8: 336 -> 504) and orders come and go with each demand import.
  Every value is range-checked against the current horizon and every
  `order_id` is looked up in the CURRENT order list; anything that does not
  map is dropped and counted, never guessed.
* **Always return a note.** Pitfall 15: a silent best-effort no-op on the data
  path is how the current-state overlay shipped broken. This module reports
  what it did on every path, including the failure paths, and the caller logs
  it.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Tuple

PREV_SCHEDULE_NAME = "prev_schedule.csv"


def _read_prev_rows(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read the preserved previous schedule. Returns (rows, notes)."""
    notes: List[str] = []
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            for raw in csv.DictReader(fh):
                try:
                    rows.append({
                        "line_id": int(float(raw["line_id"])),
                        "order_id": str(raw["order_id"]).strip(),
                        "start_hour": int(float(raw["start_hour"])),
                        "end_hour": int(float(raw["end_hour"])),
                        "run_hours": int(float(raw["run_hours"])),
                    })
                except (KeyError, TypeError, ValueError):
                    # One malformed row must not cost the whole warm start.
                    continue
    except OSError as exc:
        notes.append(f"[warm-start] could not read {path.name}: {exc}")
    return rows, notes


def build_hint_plan(
    data: Any,
    horizon_h: int,
    prev_rows: List[Dict[str, Any]],
) -> Tuple[Dict[Tuple[int, int], Dict[str, int]], Dict[str, int]]:
    """Map the previous schedule onto the CURRENT (line, order) keys.

    Returns (plan, stats). `plan` is keyed by (line_id, order_index) and holds
    the hint values for that assignment. Pure function - no model, no I/O - so
    it is unit-testable without a solve.
    """
    order_index: Dict[str, int] = {}
    for idx, o in enumerate(data.orders):
        order_index[str(o["order_id"]).strip()] = idx

    lines = set(data.lines)

    stats = {
        "rows": len(prev_rows),
        "matched_rows": 0,
        "unknown_order": 0,
        "unknown_line": 0,
        "out_of_horizon": 0,
    }

    # Group the previous rows per assignment; a CIP split produces two rows
    # for the same (line, order), which map to seg_a and seg_b in start order.
    grouped: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for r in prev_rows:
        oid = r["order_id"]
        if oid not in order_index:
            stats["unknown_order"] += 1
            continue
        if r["line_id"] not in lines:
            stats["unknown_line"] += 1
            continue
        if not (0 <= r["start_hour"] <= horizon_h and 0 <= r["end_hour"] <= horizon_h):
            stats["out_of_horizon"] += 1
            continue
        if r["run_hours"] < 0 or r["start_hour"] + r["run_hours"] > horizon_h:
            stats["out_of_horizon"] += 1
            continue
        stats["matched_rows"] += 1
        grouped.setdefault((r["line_id"], order_index[oid]), []).append(r)

    plan: Dict[Tuple[int, int], Dict[str, int]] = {}
    for key, segs in grouped.items():
        segs.sort(key=lambda s: s["start_hour"])
        a = segs[0]
        entry = {
            "present": 1,
            "seg_a_start": a["start_hour"],
            "seg_a_run": a["run_hours"],
            "seg_a_end": a["start_hour"] + a["run_hours"],
            "seg_b_present": 0,
            "seg_b_start": 0,
            "seg_b_run": 0,
            "seg_b_end": 0,
        }
        if len(segs) > 1:
            b = segs[-1]
            entry["seg_b_present"] = 1
            entry["seg_b_start"] = b["start_hour"]
            entry["seg_b_run"] = b["run_hours"]
            entry["seg_b_end"] = b["start_hour"] + b["run_hours"]
        # Derived vars the model links to the segments. Hinting them costs
        # nothing and moves the hint closer to COMPLETE, which is what the
        # CP-SAT Primer says is required for a hint to actually pay off: an
        # incomplete hint that the solver cannot finish quickly can waste more
        # search time than it saves.
        entry["run_h"] = entry["seg_a_run"] + entry["seg_b_run"]
        entry["eff_end"] = (
            entry["seg_b_end"] if entry["seg_b_present"] else entry["seg_a_end"]
        )
        plan[key] = entry

    stats["assignments"] = len(plan)
    stats["split_assignments"] = sum(
        1 for e in plan.values() if e.get("seg_b_present")
    )
    return plan, stats


def apply_warm_start(
    model: Any,
    vars_dict: Dict[str, Any],
    data: Any,
    horizon_h: int,
    work_dir: Path,
) -> List[str]:
    """Attach a solution hint to `model`. Returns notes to log (never empty)."""
    path = Path(work_dir) / PREV_SCHEDULE_NAME
    if not path.exists():
        return [
            "[warm-start] no prev_schedule.csv in the work dir - "
            "cold start (expected on the first solve of a scenario)"
        ]

    prev_rows, notes = _read_prev_rows(path)
    if not prev_rows:
        notes.append("[warm-start] prev_schedule.csv held no usable rows - cold start")
        return notes

    plan, stats = build_hint_plan(data, horizon_h, prev_rows)
    if not plan:
        notes.append(
            "[warm-start] no previous row mapped onto a current (line, order) "
            f"pair - cold start (rows={stats['rows']}, "
            f"unknown_order={stats['unknown_order']}, "
            f"unknown_line={stats['unknown_line']}, "
            f"out_of_horizon={stats['out_of_horizon']})"
        )
        return notes

    present = vars_dict["present"]
    hinted_vars = 0
    hinted_present_true = 0

    # A COMPLETE hint is the one that pays off (CP-SAT Primer): every
    # (line, order) pair gets a full assignment, not just the ones the previous
    # schedule used. Pairs absent from the previous schedule are hinted as an
    # empty assignment - not present, zero-length segments - which is exactly
    # what the model implies for them and costs the solver nothing to verify.
    EMPTY = {
        "seg_a_start": 0, "seg_a_run": 0, "seg_a_end": 0,
        "seg_b_present": 0, "seg_b_start": 0, "seg_b_run": 0, "seg_b_end": 0,
        "run_h": 0, "eff_end": 0,
    }

    for key in present:
        entry = plan.get(key)
        val = 1 if entry else 0
        model.AddHint(present[key], val)
        hinted_vars += 1
        if val:
            hinted_present_true += 1

    for name in ("seg_a_start", "seg_a_run", "seg_a_end",
                 "seg_b_present", "seg_b_start", "seg_b_run", "seg_b_end",
                 "run_h", "eff_end"):
        var_map = vars_dict.get(name)
        if not var_map:
            continue
        for key in present:
            if key not in var_map:
                continue
            entry = plan.get(key) or EMPTY
            if name not in entry:
                continue
            model.AddHint(var_map[key], entry[name])
            hinted_vars += 1

    notes.append(
        "[warm-start] hinted {vars} vars from the previous schedule: "
        "{asg} assignments ({split} CIP-split), {rows}/{allrows} rows mapped "
        "(dropped: {uo} unknown order, {ul} unknown line, {oh} outside horizon "
        "{H}h)".format(
            vars=hinted_vars,
            asg=stats["assignments"],
            split=stats["split_assignments"],
            rows=stats["matched_rows"],
            allrows=stats["rows"],
            uo=stats["unknown_order"],
            ul=stats["unknown_line"],
            oh=stats["out_of_horizon"],
            H=horizon_h,
        )
    )
    notes.append(
        f"[warm-start] present=1 hinted for {hinted_present_true} of "
        f"{len(present)} (line, order) pairs"
    )
    return notes
