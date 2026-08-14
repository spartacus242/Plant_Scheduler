# helpers/reconcile_engine.py — Reconcile: what needs attention BEFORE you plan.
#
# Charter §2.2, the daily loop: Connect → RECONCILE → Plan → Lock & Export →
# Track. This module is the Reconcile step's engine: it aggregates the checks
# a planner must see before touching the schedule — stock that won't cover a
# scheduled run, demand that isn't covered, cleaning coming due, capability
# conflicts, and physical impossibilities on the calendar itself.
#
# Design rules (same as data_health):
#   * pure functions per rule class — every rule takes loaded frames/objects
#     and returns findings; assess_plan() does the IO and never raises
#     (a broken input becomes a DATA finding, not a traceback);
#   * severity vocabulary is fixed: 2 = blocking (the plan is wrong or
#     unrunnable), 1 = needs attention, 0 = informational;
#   * every finding names the page that resolves it so the UI can deep-link.
#
# The future planning agent reads these findings as its situational input;
# the Reconcile page renders the same list for the human. One engine, two
# consumers — keep it free of Streamlit.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

# Categories (fixed vocabulary)
STOCK = "STOCK"
COVERAGE = "COVERAGE"
CIP = "CIP"
CAPABILITY = "CAPABILITY"
FIT = "FIT"
DATA = "DATA"

_CATEGORY_ORDER = {STOCK: 0, FIT: 1, CIP: 2, CAPABILITY: 3, COVERAGE: 4, DATA: 5}

BLOCKING = 2
WARN = 1
INFO = 0


@dataclass(frozen=True)
class Finding:
    key: str                 # stable id, e.g. "cip_overdue:P09"
    category: str            # STOCK | COVERAGE | CIP | CAPABILITY | FIT | DATA
    severity: int            # 2 blocking / 1 warn / 0 info
    title: str               # one-line human statement of the problem
    detail: str              # the numbers behind it
    action: str              # what the planner should do
    page: str | None = None  # Streamlit page path that resolves it
    context: dict[str, Any] = field(default_factory=dict)


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda f: (-f.severity, _CATEGORY_ORDER.get(f.category, 9), f.title),
    )


def summary(findings: Iterable[Finding]) -> dict[str, int]:
    out = {BLOCKING: 0, WARN: 0, INFO: 0}
    for f in findings:
        out[f.severity] = out.get(f.severity, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Rule: STOCK — components won't cover scheduled runs
# ---------------------------------------------------------------------------

# stock_check statuses that mean "this scheduled block cannot be made as-is".
_STOCK_BLOCKING = {"DO_NOT_SCHEDULE"}
_STOCK_WARN = {"SHORT", "UNK", "NO_BOM", "UNK_PARTIAL"}


def stock_findings(report: dict) -> list[Finding]:
    """Transform a stock_check_report() dict into findings (pure)."""
    out: list[Finding] = []
    if not report:
        return out
    if report.get("error"):
        out.append(Finding(
            key="stock_inputs", category=DATA, severity=WARN,
            title="Stock check has no usable VIF inputs",
            detail=str(report["error"]),
            action="Point Stock Check at a fresh VIF export folder",
            page="pages/stock_check.py",
        ))
        return out

    for b in report.get("schedule_view", []):
        status = str(b.get("status", "OK"))
        if status in _STOCK_BLOCKING:
            sev = BLOCKING
        elif status in _STOCK_WARN:
            sev = WARN
        else:
            continue
        constraining = [i for i in b.get("items", [])
                        if i.get("need") and i.get("status") not in ("OK", None)]
        worst = sorted(
            constraining,
            key=lambda i: i.get("ratio") if i.get("ratio") is not None else 9e9,
        )[:3]
        short_txt = "; ".join(
            f"{i.get('primary_item', i.get('item', '?'))} covers "
            f"{i.get('ratio'):.0%}" if i.get("ratio") is not None else
            f"{i.get('primary_item', i.get('item', '?'))} unknown"
            for i in worst
        ) or status
        out.append(Finding(
            key=f"stock:{b.get('block_id')}",
            category=STOCK, severity=sev,
            title=(f"Stock {'blocks' if sev == BLOCKING else 'is tight for'} "
                   f"{b.get('sku')} on {b.get('line_name')}"),
            detail=(f"Scheduled {b.get('qty_kg') or '?'} kg at h{b.get('start_h')}"
                    f" — {short_txt}"),
            action="Open Stock Check for the component detail; move or shrink the run",
            page="pages/stock_check.py",
            context={"block_id": b.get("block_id"), "sku": b.get("sku"),
                     "line_name": b.get("line_name"), "status": status},
        ))
    return out


# ---------------------------------------------------------------------------
# Rule: COVERAGE — demand not covered by the plan (M2)
# ---------------------------------------------------------------------------

def coverage_findings(
    calendar: pd.DataFrame,
    demand: pd.DataFrame,
    *,
    week1_end_h: float = 168.0,
) -> list[Finding]:
    """Demand orders vs kg actually scheduled on the calendar (pure).

    Zero-scheduled orders due in week 1 are BLOCKING (they will not be made
    unless the plan changes today); every other under-minimum order rolls into
    one summary finding — the Gantt adherence table already carries the
    per-order detail, Reconcile only needs to say how big the hole is.
    """
    out: list[Finding] = []
    if demand is None or demand.empty:
        return out

    prod = calendar[calendar["block_type"].isin(["production", "trial"])] \
        if not calendar.empty else calendar
    sched_kg: dict[str, float] = {}
    unknown_blocks = 0
    if prod is not None and not prod.empty:
        kg = pd.to_numeric(prod["qty_kg"], errors="coerce")
        unknown_blocks = int(kg.isna().sum())
        by_order = prod.assign(_kg=kg.fillna(0.0)).groupby(
            prod["order_id"].astype(str))["_kg"].sum()
        sched_kg = by_order.to_dict()

    zero_week1: list[dict] = []
    under: list[dict] = []
    total_missing = 0.0
    for _, r in demand.iterrows():
        oid = str(r["order_id"])
        target = float(r.get("qty_target", 0) or 0)
        lo = float(r.get("lower_pct", 0.9) or 0.9)
        qty_min = target * lo
        if qty_min <= 0:
            continue
        got = float(sched_kg.get(oid, 0.0))
        if got >= qty_min:
            continue
        missing = qty_min - got
        total_missing += missing
        due_start = float(r.get("due_start_hour", 0) or 0)
        entry = {"order_id": oid, "sku": str(r.get("sku", "")),
                 "qty_min": round(qty_min, 1), "scheduled": round(got, 1),
                 "missing": round(missing, 1)}
        if got <= 0 and due_start < week1_end_h:
            zero_week1.append(entry)
        else:
            under.append(entry)

    if zero_week1:
        worst = sorted(zero_week1, key=lambda e: -e["missing"])[:5]
        out.append(Finding(
            key="coverage_zero_week1", category=COVERAGE, severity=BLOCKING,
            title=(f"{len(zero_week1)} order(s) due in week 1 have NOTHING "
                   "scheduled"),
            detail=", ".join(f"{e['order_id']} ({e['missing']:,.0f} kg)"
                             for e in worst)
                   + ("…" if len(zero_week1) > 5 else ""),
            action="Plan them (solver or drag from holding) or park them with a reason",
            page="pages/calendar.py",
            context={"orders": zero_week1},
        ))
    if under:
        worst = sorted(under, key=lambda e: -e["missing"])[:5]
        out.append(Finding(
            key="coverage_under", category=COVERAGE, severity=WARN,
            title=(f"{len(under)} order(s) under their minimum "
                   f"({total_missing:,.0f} kg missing in total)"),
            detail=", ".join(f"{e['order_id']} short {e['missing']:,.0f} kg"
                             for e in worst)
                   + ("…" if len(under) > 5 else ""),
            action="See the SKU adherence table on the Plant Calendar",
            page="pages/calendar.py",
            context={"orders": under, "total_missing_kg": round(total_missing, 1)},
        ))
    if unknown_blocks:
        out.append(Finding(
            key="coverage_unknown_kg", category=COVERAGE, severity=INFO,
            title=f"{unknown_blocks} production block(s) carry unknown kg",
            detail="They count 0 toward coverage — the true coverage may be higher.",
            action="Re-import a solver schedule (writes real qty_kg) or type kg via the block editor",
            page="pages/calendar.py",
        ))
    return out


# ---------------------------------------------------------------------------
# Rule: CIP — cleaning overdue now, or coming due with nothing scheduled
# ---------------------------------------------------------------------------

def cip_findings(
    cip_by_line: dict[str, Any],
    calendar: pd.DataFrame,
    *,
    anchor,
    now,
    horizon_h: float,
    due_soon_h: float = 48.0,
) -> list[Finding]:
    """Cleaning discipline per line (pure).

    `cip_by_line` is helpers.cip_import.read_cip_info(...).by_line — each
    entry carries previous_cip, scheduled_cip and max_hours_between.
    """
    out: list[Finding] = []
    cal_cips: dict[str, list[float]] = {}
    if calendar is not None and not calendar.empty:
        for _, r in calendar[calendar["block_type"] == "cip"].iterrows():
            cal_cips.setdefault(str(r["line_name"]).upper(), []).append(
                float(r["start_h"]))

    for line, info in sorted(cip_by_line.items()):
        prev = getattr(info, "previous_cip", None)
        max_h = float(getattr(info, "max_hours_between", 0) or 0)
        if prev is None or max_h <= 0:
            continue
        prev_dt = prev.to_pydatetime() if hasattr(prev, "to_pydatetime") else prev
        hours_since = (now - prev_dt).total_seconds() / 3600.0
        if hours_since < 0:
            continue  # clock skew / future timestamp — data_health territory
        due_in_h = max_h - hours_since

        # A clean is planned when cip_info carries a ScheduledCIP before the
        # due moment, or the calendar draws a CIP block on the line before it.
        sched = getattr(info, "scheduled_cip", None)
        due_dt_h = (prev_dt - anchor).total_seconds() / 3600.0 + max_h
        planned = False
        if sched is not None:
            sched_dt = sched.to_pydatetime() if hasattr(sched, "to_pydatetime") else sched
            planned = (sched_dt - prev_dt).total_seconds() / 3600.0 <= max_h + 1e-9
        if not planned:
            planned = any(s <= due_dt_h for s in cal_cips.get(line.upper(), []))

        if due_in_h < 0:
            out.append(Finding(
                key=f"cip_overdue:{line}", category=CIP, severity=BLOCKING,
                title=f"{line} is {-due_in_h:,.0f}h PAST its CIP interval",
                detail=(f"Last CIP {prev_dt:%a %m-%d %H:%M}, max interval "
                        f"{max_h:.0f}h — running dirty now."),
                action="Schedule a CIP on this line immediately",
                page="pages/calendar.py",
                context={"line": line, "hours_since": round(hours_since, 1),
                         "max_h": max_h},
            ))
        elif due_in_h <= due_soon_h and not planned:
            out.append(Finding(
                key=f"cip_due:{line}", category=CIP, severity=WARN,
                title=f"{line} needs a CIP within {due_in_h:,.0f}h — none scheduled",
                detail=(f"Last CIP {prev_dt:%a %m-%d %H:%M}, interval {max_h:.0f}h; "
                        "no ScheduledCIP in cip_info and no CIP block on the calendar "
                        "before the deadline."),
                action="Add a CIP block before the interval runs out",
                page="pages/calendar.py",
                context={"line": line, "due_in_h": round(due_in_h, 1)},
            ))
    return out


# ---------------------------------------------------------------------------
# Rule: CAPABILITY — plan or plant runs a (line, sku) the table says can't run
# ---------------------------------------------------------------------------

def capability_findings(
    capabilities: pd.DataFrame,
    calendar: pd.DataFrame,
    manprg_conflicts: Any | None = None,
) -> list[Finding]:
    """(a) scheduled production the capability table forbids — BLOCKING;
    (b) manprg (plant ground truth) MOs missing from the table — WARN
    (never zero out what the plant ran; the TABLE is what needs fixing)."""
    out: list[Finding] = []

    capable: set[tuple[str, str]] = set()
    known_skus: set[str] = set()
    if capabilities is not None and not capabilities.empty:
        for _, r in capabilities.iterrows():
            sku = str(r["sku"]).strip()
            known_skus.add(sku)
            try:
                cap = int(r.get("capable", 0) or 0)
            except (TypeError, ValueError):
                cap = 0
            if cap == 1:
                capable.add((sku, str(r["line_name"]).strip().upper()))

    if calendar is not None and not calendar.empty and capable:
        prod = calendar[calendar["block_type"] == "production"]
        bad_pairs: dict[tuple[str, str], int] = {}
        for _, r in prod.iterrows():
            sku = str(r.get("sku", "")).strip()
            line = str(r.get("line_name", "")).strip().upper()
            if not sku or sku.upper() in ("CIP", "TRIALS"):
                continue
            # A/B sides of a double line inherit the group's capability rows.
            group = line[:-1] if line and line[-1] in ("A", "B") else line
            if (sku, line) not in capable and (sku, group) not in capable:
                bad_pairs[(sku, line)] = bad_pairs.get((sku, line), 0) + 1
        for (sku, line), n in sorted(bad_pairs.items()):
            out.append(Finding(
                key=f"cap_sched:{line}:{sku}", category=CAPABILITY,
                severity=BLOCKING,
                title=f"{line} is scheduled to run {sku} but is not capable",
                detail=(f"{n} block(s) on the calendar; capabilities_rates.csv "
                        "has no capable=1 row for this pair"
                        + ("" if sku in known_skus else
                           " — the SKU is missing from the table entirely")),
                action="Move the block(s) to a capable line, or fix the capability table",
                page="pages/calendar.py",
                context={"sku": sku, "line": line, "blocks": n},
            ))

    if manprg_conflicts is not None and getattr(manprg_conflicts, "count", 0):
        kinds = manprg_conflicts.by_kind()
        for kind, items in sorted(kinds.items()):
            sample = ", ".join(f"{c.sku}@{c.line_name}" for c in items[:4])
            out.append(Finding(
                key=f"cap_manprg:{kind}", category=CAPABILITY, severity=WARN,
                title=(f"{len(items)} running/queued MO(s) not in the "
                       f"capability table ({kind})"),
                detail=(f"{sample}{'…' if len(items) > 4 else ''} — manprg is "
                        "ground truth; the table is what needs fixing."),
                action="Add the missing (line, SKU) rows to capabilities_rates.csv",
                page="pages/data.py",
                context={"kind": kind,
                         "pairs": [{"sku": c.sku, "line": c.line_name,
                                    "mo": c.mo} for c in items]},
            ))
    return out


# ---------------------------------------------------------------------------
# Rule: FIT — the calendar is physically impossible where it stands
# ---------------------------------------------------------------------------

def fit_findings(
    calendar: pd.DataFrame,
    downtimes: pd.DataFrame | None = None,
    *,
    horizon_h: float | None = None,
) -> list[Finding]:
    """Overlaps, production over downed lines, and blocks past the horizon."""
    out: list[Finding] = []
    if calendar is None or calendar.empty:
        return out
    busy = calendar[calendar["block_type"].isin(
        ["production", "trial", "cip"])].copy()
    busy["start_h"] = pd.to_numeric(busy["start_h"], errors="coerce")
    busy["end_h"] = pd.to_numeric(busy["end_h"], errors="coerce")

    # Overlaps per line (same sweep as the Gantt's checkOverlaps)
    overlaps: list[str] = []
    for line, grp in busy.groupby(busy["line_name"].astype(str)):
        g = grp.sort_values("start_h")
        prev_end, prev_label = None, ""
        for _, r in g.iterrows():
            if prev_end is not None and float(r["start_h"]) < prev_end - 1e-9:
                overlaps.append(
                    f"{line}: {prev_label} runs into {r.get('sku') or r.get('label')}"
                    f" at h{float(r['start_h']):.0f}")
            if prev_end is None or float(r["end_h"]) > prev_end:
                prev_end = float(r["end_h"])
                prev_label = str(r.get("sku") or r.get("label") or "?")
    if overlaps:
        out.append(Finding(
            key="fit_overlaps", category=FIT, severity=BLOCKING,
            title=f"{len(overlaps)} overlapping block pair(s) on the calendar",
            detail="; ".join(overlaps[:4]) + ("…" if len(overlaps) > 4 else ""),
            action="Separate the overlapping blocks on the Plant Calendar",
            page="pages/calendar.py",
            context={"overlaps": overlaps},
        ))

    # Production scheduled over a line-down window
    if downtimes is not None and not downtimes.empty:
        hits: list[str] = []
        for _, d in downtimes.iterrows():
            line = str(d.get("line_name", "")).strip().upper()
            ds = float(d.get("start_hour", 0) or 0)
            de = float(d.get("end_hour", 0) or 0)
            prod = busy[(busy["line_name"].astype(str).str.upper() == line)
                        & (busy["block_type"] != "cip")]
            for _, r in prod.iterrows():
                if float(r["start_h"]) < de and float(r["end_h"]) > ds:
                    hits.append(
                        f"{line}: {r.get('sku') or r.get('label')} "
                        f"h{float(r['start_h']):.0f}-{float(r['end_h']):.0f} "
                        f"vs down {ds:.0f}-{de:.0f}")
        if hits:
            out.append(Finding(
                key="fit_downtime", category=FIT, severity=BLOCKING,
                title=f"{len(hits)} block(s) scheduled while the line is DOWN",
                detail="; ".join(hits[:4]) + ("…" if len(hits) > 4 else ""),
                action="Move the blocks off the downtime window",
                page="pages/calendar.py",
                context={"hits": hits},
            ))

    if horizon_h:
        past_end = busy[busy["end_h"] > float(horizon_h) + 1e-9]
        if len(past_end):
            out.append(Finding(
                key="fit_horizon", category=FIT, severity=WARN,
                title=f"{len(past_end)} block(s) run past the {horizon_h:.0f}h horizon",
                detail=", ".join(
                    f"{r.get('sku') or r.get('label')}@{r['line_name']}"
                    for _, r in past_end.head(4).iterrows())
                    + ("…" if len(past_end) > 4 else ""),
                action="Pull them inside the horizon or roll the calendar",
                page="pages/calendar.py",
            ))
    return out


# ---------------------------------------------------------------------------
# assess_plan — the IO shell. Never raises; broken inputs become findings.
# ---------------------------------------------------------------------------

def assess_plan(
    data_dir: Path | None = None,
    cfg: dict | None = None,
    *,
    stock_report: dict | None = None,
) -> list[Finding]:
    from helpers.config import datasources_config, load_toml
    from helpers.paths import data_dir as _dd

    dd = Path(data_dir) if data_dir else _dd()
    cfg = cfg or load_toml()
    ref = dd / "reference"
    findings: list[Finding] = []

    def guard(name: str, fn) -> None:
        try:
            findings.extend(fn())
        except Exception as exc:  # noqa: BLE001 — a broken rule must not kill Reconcile
            findings.append(Finding(
                key=f"rule_error:{name}", category=DATA, severity=WARN,
                title=f"Reconcile check '{name}' could not run",
                detail=str(exc),
                action="Check the input files on the Data page",
                page="pages/data.py",
            ))

    from helpers.calendar_io import load_calendar
    calendar = load_calendar(dd / "calendar_blocks.csv")

    from helpers import horizon as _hz
    hz = _hz.resolve(cfg)

    # STOCK — only when a report is supplied (the page passes its cached one;
    # building a VIF snapshot here would make Reconcile slow and share-bound).
    if stock_report is not None:
        guard("stock", lambda: stock_findings(stock_report))

    # COVERAGE
    def _coverage():
        dem_path = ref / "demand_plan.csv"
        if not dem_path.exists():
            return []
        return coverage_findings(
            calendar, pd.read_csv(dem_path, dtype={"sku": str}))
    guard("coverage", _coverage)

    # CIP
    def _cip():
        from helpers.cip_import import read_cip_info
        ds = datasources_config(cfg)
        cip_path = str(ds.get("cip_info_csv", "")).strip() or str(ref / "cip_info.csv")
        if not Path(cip_path).exists():
            return []
        return cip_findings(
            read_cip_info(cip_path).by_line, calendar,
            anchor=hz.anchor, now=hz.now, horizon_h=float(hz.hours))
    guard("cip", _cip)

    # CAPABILITY
    def _capability():
        from helpers.capability_check import (check_capabilities,
                                              load_capabilities,
                                              load_manprg_mos)
        caps_path = ref / "capabilities_rates.csv"
        if not caps_path.exists():
            return []
        caps = load_capabilities(caps_path)
        conflicts = None
        ds = datasources_config(cfg)
        mp_paths = [p.strip() for p in str(ds.get("manprg_files", "")).split(";")
                    if p.strip()] or [str(ref / "manprg.txt"),
                                      str(ref / "manprg2.txt")]
        existing = [p for p in mp_paths if Path(p).exists()]
        if existing:
            conflicts = check_capabilities(caps, load_manprg_mos(existing))
        return capability_findings(caps, calendar, conflicts)
    guard("capability", _capability)

    # FIT
    def _fit():
        dt_path = ref / "downtimes.csv"
        downtimes = pd.read_csv(dt_path) if dt_path.exists() else None
        return fit_findings(calendar, downtimes, horizon_h=float(hz.hours))
    guard("fit", _fit)

    return sort_findings(findings)
