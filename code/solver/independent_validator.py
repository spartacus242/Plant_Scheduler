# independent_validator.py -- A SECOND, independent implementation of the
# Flowstate business rules, written to catch mistakes in the first.
#
# Deliberately imports NONE of model_builder / validate_schedule /
# scenario_runner / scorecard_engine / data_loader / changeover_cache. It
# re-reads the raw CSV inputs with pandas + stdlib only and applies the
# charter rules more strictly than the solver does:
#
#   * NO +12h tolerance on CIP spacing (validate_schedule.py allows one).
#   * Changeover setup uses the EXACT float setup_hours from changeovers.csv
#     (the solver rounds half-up to whole hours; the rounding is reported in
#     the violation detail so a reader can tell the two apart).
#   * Changeover pairs are direction-aware: (from_sku, to_sku) only.
#   * Duplicate (from_sku, to_sku) rows are resolved EXPLICITLY (strictest
#     value wins: max setup_hours, max cip_req_after) and reported.
#   * Half-open intervals [start, end) everywhere.
#
# Two entry points:
#   validate_work_dir(data_dir, ...)  -- a solver work dir (hours dialect)
#   validate_calendar(calendar_df, reference_dir, anchor, cfg)
#                                     -- a calendar_blocks-shaped board with
#                                        data/reference-shaped inputs
#                                        (downtimes.csv in wall-clock
#                                        datetimes, cip_info.csv per line)
#
# CLI:  python code/solver/independent_validator.py --data-dir <dir>
#           [--calendar <csv> --reference <dir> --anchor 'YYYY-MM-DD HH:MM:SS']
#       exits 1 when any ERROR violation is found (--strict: WARN too).

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

ERROR = "ERROR"
WARN = "WARN"

# Stand-down sentinel: line_cip_hrs.csv writes 100000 when solver CIPs are
# switched off (Scenario F). Any value >= this disables the interval check.
CIP_STANDDOWN_MIN = 10000
# calendar_blocks.csv keeps start_h/end_h to 3 decimals.
CALENDAR_HOURS_PRECISION = 1e-3

PRODUCTION_KINDS = ("production", "trial")
DOWNTIME_KINDS = ("maintenance", "contractor", "line_down")
ALL_CHECKS = [
    "OVERLAP", "IN_DOWNTIME", "LINE_NOT_CAPABLE", "QTY_RATE_MISMATCH",
    "RUN_HOURS_MISMATCH", "MIN_RUN", "MAX_LINES_PER_ORDER", "BEFORE_GATE",
    "INITIAL_SETUP", "TRIAL_MOVED", "CIP_INTERVAL", "CIP_DURATION",
    "CIP_REQ_MISSING", "CHANGEOVER_GAP", "HORIZON", "DUE_WINDOW",
    "EARLY_BEFORE_COMMITTED", "DEMAND_BOUNDS", "DEMAND_SUM", "UNKNOWN_SKU", "UNKNOWN_LINE",
    "UNKNOWN_ORDER", "DUPLICATE_ROWS", "NEGATIVE_OR_ZERO", "MATERIAL",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class Violation:
    code: str
    severity: str            # ERROR | WARN
    line: Optional[str]      # line_name (or "L<id>")
    order: Optional[str]
    hours: Optional[float]   # the magnitude that matters for this check
    detail: str

    def as_line(self) -> str:
        h = "" if self.hours is None else f" [{self.hours:g}h]"
        return (f"{self.severity:5s} {self.code:20s} line={self.line or '-':5s} "
                f"order={self.order or '-':14s}{h} {self.detail}")


@dataclass
class ValidationReport:
    ok: bool = True
    violations: List[Violation] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    checks_run: List[str] = field(default_factory=list)

    # -- helpers -----------------------------------------------------------
    def add(self, code: str, severity: str, line, order, hours, detail: str) -> None:
        self.violations.append(Violation(
            code, severity,
            None if line is None else str(line),
            None if order is None else str(order),
            None if hours is None else float(round(float(hours), 4)),
            detail))
        if severity == ERROR:
            self.ok = False

    def by_code(self, code: str) -> List[Violation]:
        return [v for v in self.violations if v.code == code]

    def errors(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == ERROR]

    def warnings(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == WARN]

    def counts(self) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = defaultdict(lambda: {"ERROR": 0, "WARN": 0})
        for v in self.violations:
            out[v.code][v.severity] += 1
        return dict(out)

    def summary(self, max_lines: int = 400) -> str:
        lines = ["=" * 72, "Flowstate INDEPENDENT schedule validation", "=" * 72]
        lines.append(f"ok={self.ok}  errors={len(self.errors())}  warnings={len(self.warnings())}")
        lines.append("")
        lines.append("--- checks ---")
        lines.extend(f"  {c}" for c in self.checks_run)
        lines.append("")
        lines.append("--- stats ---")
        for k in sorted(self.stats):
            lines.append(f"  {k}: {self.stats[k]}")
        lines.append("")
        lines.append("--- violations by code ---")
        for code, c in sorted(self.counts().items()):
            lines.append(f"  {code:20s} ERROR={c['ERROR']:4d} WARN={c['WARN']:4d}")
        lines.append("")
        lines.append("--- violations ---")
        vs = sorted(self.violations, key=lambda v: (v.severity != ERROR, v.code, v.line or "", v.order or ""))
        for v in vs[:max_lines]:
            lines.append("  " + v.as_line())
        if len(vs) > max_lines:
            lines.append(f"  ... {len(vs) - max_lines} more")
        lines.append("=" * 72)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "n_errors": len(self.errors()),
            "n_warnings": len(self.warnings()),
            "counts": self.counts(),
            "violations": [asdict(v) for v in self.violations],
            "stats": self.stats,
            "checks_run": list(self.checks_run),
        }


# ---------------------------------------------------------------------------
# Block model (both dialects normalise to this)
# ---------------------------------------------------------------------------
@dataclass
class Block:
    idx: int
    kind: str                # production | trial | cip | maintenance | contractor | line_down
    line_id: Optional[int]
    line_name: str
    order_id: Optional[str]
    sku: Optional[str]
    start: float
    end: float
    run_hours: Optional[float]
    qty_kg: Optional[float]
    committed: bool = False  # committed / current_state row (calendar mode)
    is_trial: bool = False
    src: str = ""            # where the row came from (file / block_id)

    @property
    def dur(self) -> float:
        return self.end - self.start

    @property
    def is_prod(self) -> bool:
        return self.kind in PRODUCTION_KINDS


@dataclass
class Window:
    line_id: Optional[int]
    line_name: str
    start: float
    end: float
    reason: str
    is_cip: bool = False     # downtime row whose reason is ONLY cip tokens
    mixed_cip: bool = False  # merged row mentioning CIP among other things


@dataclass
class Order:
    order_id: str
    sku: str
    due_start: float
    due_end: float
    qty_min: float
    qty_max: float
    qty_target: float


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Cfg:
    horizon_h: float = 336.0
    min_run_hours: float = 4.0
    max_lines_per_order: int = 3
    use_sku_rates: bool = False
    soft_demand: bool = False
    cip_interval_h: float = 120.0
    cip_duration_h: float = 6.0
    planning_start_date: Optional[datetime] = None
    long_shutdown_default_h: float = 4.0
    # Early-fill rule (DUE_WINDOW early-start branch). allow_week1_in_week0
    # off -> every start before due_start is an ERROR. On (the default and
    # Scenario F's setting) the allowance is [scheduler] early_fill_hours:
    #   None (absent / "unbounded" / "none") -> plant decision 2026-09-04:
    #        early fill may go as far back as free capacity allows, so an
    #        early start is NEVER an ERROR here — it is reported as an
    #        informational WARN "early fill (policy: unbounded)"; what still
    #        makes it invalid is starting before the line's gate or over a
    #        committed block (BEFORE_GATE / OVERLAP / IN_DOWNTIME) or before a
    #        committed current-state MO on the same line
    #        (EARLY_BEFORE_COMMITTED, E-style work dirs).
    #   h hours -> a start up to h before due_start is a WARN (inside the
    #        allowance), beyond it an ERROR. 48 = the pre-decision value.
    # week0_fill_start_h / week1_start_h are LEGACY (unused by the check
    # since 2026-09-03; kept so older callers constructing Cfg still work).
    allow_week1_in_week0: bool = True
    early_fill_hours: Optional[float] = None
    # Due-week policy (DUE_WINDOW late-finish branch). "hard" (the default
    # when the key is absent, matching the solver's Params default since
    # 2026-09-04 night): a demand block finishing past due_end + 1 -> ERROR.
    # "soft" (opt-in, plant decision 2026-09-04 #2): the late finish is a
    # PRICED TRADE-OFF the solver chose (kg x weeks late against the
    # changeovers a consolidated campaign saves) -> WARN with the late kg and
    # kg-weeks in the detail. The horizon end stays an ERROR either way
    # (HORIZON check).
    due_week_policy: str = "hard"
    week0_fill_start_h: float = 120.0
    week1_start_h: float = 168.0
    # Validator-only tolerances (explicit, not hidden)
    qty_rel_tol: float = 0.005    # 0.5 % of expected kg
    qty_abs_tol_kg: float = 1.0
    demand_sum_tol_kg: float = 0.5


# LEGACY documented value: until the plant decision of 2026-09-04 only the
# SECOND demand week could start early, by these 48 h (the same number as
# model_builder.EARLY_FILL_HOURS = 168 - 120, restated here so the validator
# shares no code with the model it checks). The live allowance is
# Cfg.early_fill_hours (None = unbounded); set [scheduler] early_fill_hours =
# 48 to get a bounded allowance back — for every demand order.
EARLY_FILL_H = 48.0


def parse_early_fill_hours(value: Any) -> Optional[float]:
    """[scheduler] early_fill_hours -> Cfg.early_fill_hours (restated on
    purpose, not imported from data_loader): None / "unbounded" / "none" /
    "" -> None (unbounded); a number or numeric string -> hours >= 0;
    anything else raises ValueError."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"early_fill_hours: expected hours or 'unbounded', got {value!r}")
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "unbounded", "none", "unlimited", "inf"):
            return None
        try:
            value = float(s)
        except ValueError:
            raise ValueError(
                f"early_fill_hours: expected hours or 'unbounded', got {value!r}") from None
    try:
        h = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"early_fill_hours: expected hours or 'unbounded', got {value!r}") from None
    if math.isnan(h) or math.isinf(h) or h < 0:
        raise ValueError(f"early_fill_hours must be >= 0 hours, got {value!r}")
    return h


def parse_due_week_policy(value: Any) -> str:
    """[scheduler] due_week_policy -> Cfg.due_week_policy (restated on
    purpose, not imported from data_loader): None / "" -> "hard" (the
    shipped default since 2026-09-04 night; "soft" is the opt-in of plant
    decision 2026-09-04 #2); "soft" / "hard" as given; anything else raises
    ValueError."""
    if value is None:
        return "hard"
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"due_week_policy: expected 'soft' or 'hard', got {value!r}")
    s = value.strip().lower()
    if s == "":
        return "hard"
    if s not in ("soft", "hard"):
        raise ValueError(f"due_week_policy: expected 'soft' or 'hard', got {value!r}")
    return s


WEEK_STEP_H = 168.0  # one demand week; kg-weeks late are graded in whole steps


def second_demand_week_start(orders: Dict[str, "Order"]) -> Optional[float]:
    """Start hour of the SECOND distinct demand week (None with one week).

    Week boundaries are the sorted distinct due_start hours of the demand
    orders — the plant's own frame, whatever weekday the horizon is anchored
    on (Wednesday-anchored Scenario F: [0, 120, 288, 456] -> 120). LEGACY
    since the plant decision of 2026-09-04: the DUE_WINDOW check no longer
    privileges the second week; kept for external callers and tests.
    """
    starts = sorted({float(o.due_start) for o in orders.values()})
    return starts[1] if len(starts) > 1 else None


def _parse_dt(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    t = str(s).strip()
    if not t or t.lower() == "nan":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    try:
        return pd.to_datetime(t).to_pydatetime()
    except Exception:
        return None


def load_cfg(config: Any, data_dir: Optional[Path] = None) -> Cfg:
    """config: None | Path/str to a toml | dict (already parsed toml) | Cfg."""
    if isinstance(config, Cfg):
        return config
    raw: Dict[str, Any] = {}
    if config is None and data_dir is not None:
        p = Path(data_dir) / "flowstate.toml"
        if p.exists():
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
    elif isinstance(config, (str, Path)):
        raw = tomllib.loads(Path(config).read_text(encoding="utf-8"))
    elif isinstance(config, dict):
        raw = config
    c = Cfg()
    sched = raw.get("scheduler", {}) or {}
    hz = sched.get("horizon_hours")
    if hz is None and sched.get("horizon_weeks") is not None:
        hz = float(sched["horizon_weeks"]) * 168.0
    if hz:
        c.horizon_h = float(hz)
    if sched.get("min_run_hours") is not None:
        c.min_run_hours = float(sched["min_run_hours"])
    if sched.get("max_lines_per_order") is not None:
        c.max_lines_per_order = int(sched["max_lines_per_order"])
    if sched.get("use_sku_rates") is not None:
        c.use_sku_rates = bool(sched["use_sku_rates"])
    if sched.get("soft_demand") is not None:
        c.soft_demand = bool(sched["soft_demand"])
    if sched.get("allow_week1_in_week0") is not None:
        c.allow_week1_in_week0 = bool(sched["allow_week1_in_week0"])
    if "early_fill_hours" in sched:
        c.early_fill_hours = parse_early_fill_hours(sched["early_fill_hours"])
    c.due_week_policy = parse_due_week_policy(sched.get("due_week_policy"))
    psd = sched.get("planning_start_date") or raw.get("planning_start_date")
    c.planning_start_date = _parse_dt(psd)
    cip = raw.get("cip", {}) or {}
    if cip.get("interval_h") is not None:
        c.cip_interval_h = float(cip["interval_h"])
    if cip.get("duration_h") is not None:
        c.cip_duration_h = float(cip["duration_h"])
    val = raw.get("validator", {}) or {}
    for k in ("qty_rel_tol", "qty_abs_tol_kg", "demand_sum_tol_kg"):
        if val.get(k) is not None:
            setattr(c, k, float(val[k]))
    return c


# ---------------------------------------------------------------------------
# Raw input loading (pandas only, no project imports)
# ---------------------------------------------------------------------------
def _read_csv(path: Path) -> Optional[pd.DataFrame]:
    if not Path(path).exists():
        return None
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [str(c).lstrip("﻿").strip() for c in df.columns]
    return df


def _num(v, default=None):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return default
        f = float(v)
        if math.isnan(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        if v.is_integer():
            return str(int(v))
    return str(v).strip()


def _sku(v) -> str:
    """SKU normalisation: '280351.0' -> '280351'; strings kept verbatim."""
    t = _s(v)
    if t.endswith(".0") and t[:-2].isdigit():
        t = t[:-2]
    return t


@dataclass
class Inputs:
    cfg: Cfg
    lines: Dict[int, str] = field(default_factory=dict)             # id -> name
    line_by_name: Dict[str, int] = field(default_factory=dict)
    skus: set = field(default_factory=set)
    capable: Dict[Tuple[int, str], int] = field(default_factory=dict)
    sku_rate: Dict[Tuple[int, str], float] = field(default_factory=dict)   # capabilities calc_rate_kgph
    flat_rate: Dict[int, float] = field(default_factory=dict)               # line_rates.csv
    setup: Dict[Tuple[str, str], float] = field(default_factory=dict)       # exact floats
    cip_req: Dict[Tuple[str, str], int] = field(default_factory=dict)
    co_dup_pairs: int = 0
    co_dup_conflicts: List[Tuple[str, str, str]] = field(default_factory=list)
    init: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    downtimes: List[Window] = field(default_factory=list)
    orders: Dict[str, Order] = field(default_factory=dict)
    max_cip_hrs: Dict[int, float] = field(default_factory=dict)
    cip_ref_hour: Dict[int, float] = field(default_factory=dict)  # last clean END before t0, in hours (<= 0)
    cip_ref_src: Dict[int, str] = field(default_factory=dict)
    pvb: Optional[pd.DataFrame] = None
    trials: Dict[str, Tuple[int, float, float]] = field(default_factory=dict)  # order_id -> (line, start, end)
    # current_mo.csv (E-style work dirs): mo -> {line_id, line_name, sku}.
    # The solver writes those MOs as schedule rows with order_id "<mo>|CUR"
    # (or "<mo>#n|CUR"); EARLY_BEFORE_COMMITTED treats them as committed.
    current_mos: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def rate_cfg(self, line_id: int, sku: str) -> Optional[float]:
        """The rate the config implies for qty = rate x hours."""
        if self.cfg.use_sku_rates:
            return self.sku_rate.get((line_id, sku))
        if line_id in self.flat_rate:
            return self.flat_rate[line_id]
        return self.sku_rate.get((line_id, sku))

    def rate_other(self, line_id: int, sku: str) -> Optional[float]:
        if self.cfg.use_sku_rates:
            return self.flat_rate.get(line_id)
        return self.sku_rate.get((line_id, sku))


def _load_capabilities(inp: Inputs, path: Path) -> None:
    cap = _read_csv(path)
    if cap is None:
        inp.notes.append("capabilities_rates.csv missing")
        return
    rate_col = next((c for c in ("calc_rate_kgph", "rate_kgph", "rate_uph") if c in cap.columns), None)
    for _, r in cap.iterrows():
        lid = _num(r.get("line_id"))
        if lid is None:
            continue
        lid = int(lid)
        name = _s(r.get("line_name")) or f"L{lid}"
        sku = _sku(r.get("sku"))
        inp.lines[lid] = name
        inp.line_by_name[name.upper()] = lid
        inp.skus.add(sku)
        inp.capable[(lid, sku)] = int(_num(r.get("capable"), 0) or 0)
        inp.sku_rate[(lid, sku)] = float(_num(r.get(rate_col), 0.0) or 0.0) if rate_col else 0.0


def _load_sku_info(inp: Inputs, path: Path) -> None:
    si = _read_csv(path)
    if si is None or "sku" not in si.columns:
        return
    for v in si["sku"]:
        inp.skus.add(_sku(v))


def _load_line_rates(inp: Inputs, path: Path) -> None:
    lr = _read_csv(path)
    if lr is None:
        return
    if "Month" in lr.columns and inp.cfg.planning_start_date is not None:
        m = inp.cfg.planning_start_date.month
        lr = lr[pd.to_numeric(lr["Month"], errors="coerce") == m]
    for _, r in lr.iterrows():
        lid = _num(r.get("line_id"))
        rate = _num(r.get("rate_kgph"))
        if lid is None or rate is None:
            continue
        inp.flat_rate[int(lid)] = float(rate)


def _load_changeovers(inp: Inputs, path: Path) -> None:
    chg = _read_csv(path)
    if chg is None:
        inp.notes.append("changeovers.csv missing -> setup 0 everywhere")
        return
    dup_pairs = 0
    seen: Dict[Tuple[str, str], Tuple[float, int]] = {}
    for _, r in chg.iterrows():
        pair = (_sku(r.get("from_sku")), _sku(r.get("to_sku")))
        su = float(_num(r.get("setup_hours"), 0.0) or 0.0)
        cr = int(_num(r.get("cip_req_after"), 0) or 0) if "cip_req_after" in chg.columns else 0
        if pair in seen:
            psu, pcr = seen[pair]
            if psu == su and pcr == cr:
                dup_pairs += 1
            else:
                inp.co_dup_conflicts.append(
                    (pair[0], pair[1], f"setup {psu:g} vs {su:g}, cip_req {pcr} vs {cr} -> strictest kept"))
                dup_pairs += 1
            seen[pair] = (max(psu, su), max(pcr, cr))
        else:
            seen[pair] = (su, cr)
    inp.co_dup_pairs = dup_pairs
    for pair, (su, cr) in seen.items():
        inp.setup[pair] = su
        inp.cip_req[pair] = cr


def _load_initial_states(inp: Inputs, path: Path) -> None:
    init = _read_csv(path)
    if init is None:
        return
    for _, r in init.iterrows():
        lid = _num(r.get("line_id"))
        if lid is None:
            continue
        lid = int(lid)
        name = _s(r.get("line_name"))
        if name and lid not in inp.lines:
            inp.lines[lid] = name
            inp.line_by_name[name.upper()] = lid
        inp.init[lid] = dict(
            initial_sku=_sku(r.get("initial_sku")) or "CLEAN",
            available_from=float(_num(r.get("available_from_hour"), 0.0) or 0.0),
            long_shutdown_flag=int(_num(r.get("long_shutdown_flag"), 0) or 0),
            long_shutdown_extra=float(_num(r.get("long_shutdown_extra_setup_hours"),
                                           inp.cfg.long_shutdown_default_h)),
            carryover=float(_num(r.get("carryover_run_hours_since_last_cip_at_t0"), 0.0) or 0.0),
            last_cip_end=_parse_dt(r.get("last_cip_end_datetime")),
        )


_CIP_TOKENS = ("cip",)


def _classify_reason(reason: str) -> Tuple[bool, bool]:
    """(is_pure_cip, mentions_cip). A merged staging row like
    'Committed PRODUCTION 30142 + Committed CIP' mentions CIP but is NOT a clean
    window: its end is the end of the last committed thing, not of the clean."""
    low = reason.lower()
    if "cip" not in low:
        return False, False
    parts = [p.strip() for p in low.split("+") if p.strip()]
    pure = all(("cip" in p and "production" not in p and "trial" not in p and "down" not in p)
               for p in parts)
    return pure, True


def _load_downtimes_hours(inp: Inputs, path: Path) -> None:
    dt = _read_csv(path)
    if dt is None:
        return
    for _, r in dt.iterrows():
        lid = _num(r.get("line_id"))
        s, e = _num(r.get("start_hour")), _num(r.get("end_hour"))
        if s is None or e is None:
            continue
        reason = _s(r.get("reason"))
        pure, mixed = _classify_reason(reason)
        lid_i = int(lid) if lid is not None else None
        name = _s(r.get("line_name")) or (inp.lines.get(lid_i, f"L{lid_i}") if lid_i is not None else "?")
        inp.downtimes.append(Window(lid_i, name, float(s), float(e), reason,
                                    is_cip=pure, mixed_cip=(mixed and not pure)))


def _load_downtimes_datetime(inp: Inputs, path: Path, anchor: datetime) -> None:
    dt = _read_csv(path)
    if dt is None:
        return
    for _, r in dt.iterrows():
        sd, ed = _parse_dt(r.get("start_datetime")), _parse_dt(r.get("end_datetime"))
        if sd is None or ed is None:
            continue
        s = (sd - anchor).total_seconds() / 3600.0
        e = (ed - anchor).total_seconds() / 3600.0
        name = _s(r.get("line_name")).upper()
        lid = inp.line_by_name.get(name)
        if lid is None:
            lid_n = _num(r.get("line_id"))
            lid = int(lid_n) if lid_n is not None else None
        reason = _s(r.get("reason"))
        pure, mixed = _classify_reason(reason)
        inp.downtimes.append(Window(lid, name or (inp.lines.get(lid, "?") if lid is not None else "?"),
                                    s, e, reason, is_cip=pure, mixed_cip=(mixed and not pure)))


def _load_demand(inp: Inputs, path: Path) -> None:
    dem = _read_csv(path)
    if dem is None:
        inp.notes.append("demand_plan.csv missing")
        return
    seen: Dict[str, int] = {}
    for _, r in dem.iterrows():
        sku = _sku(r.get("sku"))
        oid = _s(r.get("order_id"))
        if not oid:
            wk = _num(r.get("week_index"))
            if wk is None:
                wk = 0 if (_num(r.get("due_start_hour"), 0) or 0) <= 167 else 1
            oid = f"W{int(wk)}-{sku}"
        if oid in seen:
            seen[oid] += 1
            oid = f"{oid}_{seen[oid]}"
        else:
            seen[oid] = 0
        tgt = float(_num(r.get("qty_target"), 0.0) or 0.0)
        lo, up = _num(r.get("lower_pct")), _num(r.get("upper_pct"))
        qmin_c, qmax_c = _num(r.get("qty_min")), _num(r.get("qty_max"))
        if lo is not None and up is not None:
            qmin = float(math.floor(tgt * lo))
            qmax = float(math.ceil(tgt * up))
        elif qmin_c is not None and qmax_c is not None:
            qmin, qmax = float(qmin_c), float(qmax_c)
        else:
            qmin, qmax = 0.0, float("inf")
        inp.orders[oid] = Order(
            order_id=oid, sku=sku,
            due_start=float(_num(r.get("due_start_hour"), 0.0) or 0.0),
            due_end=float(_num(r.get("due_end_hour"), inp.cfg.horizon_h - 1)),
            qty_min=qmin, qty_max=qmax, qty_target=tgt)


def _load_line_cip_hrs(inp: Inputs, path: Path) -> None:
    lc = _read_csv(path)
    if lc is None:
        return
    for _, r in lc.iterrows():
        lid = _num(r.get("line_id"))
        mx = _num(r.get("max_cip_hrs"))
        if lid is None or mx is None:
            continue
        inp.max_cip_hrs[int(lid)] = float(mx)


def _load_cip_info(inp: Inputs, path: Path, anchor: datetime) -> None:
    ci = _read_csv(path)
    if ci is None:
        return
    for _, r in ci.iterrows():
        name = _s(r.get("LineEquipment")).upper()
        lid = inp.line_by_name.get(name)
        if lid is None:
            continue
        mx = _num(r.get("MaxHoursBetweenCIP"))
        if mx is not None:
            inp.max_cip_hrs[lid] = float(mx)
        prev = _parse_dt(r.get("PreviousCIP"))
        if prev is not None:
            inp.cip_ref_hour[lid] = (prev - anchor).total_seconds() / 3600.0
            inp.cip_ref_src[lid] = "cip_info.PreviousCIP"


def _load_current_mo(inp: Inputs, path: Path) -> None:
    """current_mo.csv (mo, line_name, sku, remaining_kg, ...): the committed
    current-state MOs of an E-style work dir. Rows with nothing remaining are
    not orders (data_loader skips them) and are ignored here too."""
    df = _read_csv(path)
    if df is None:
        return
    for _, r in df.iterrows():
        mo = _s(r.get("mo"))
        if not mo:
            continue
        remaining = _num(r.get("remaining_kg"), 0.0) or 0.0
        if remaining <= 0:
            continue
        name = _s(r.get("line_name")).upper()
        inp.current_mos[mo] = {
            "line_id": inp.line_by_name.get(name),
            "line_name": name,
            "sku": _sku(r.get("sku")),
        }


def _load_trials(inp: Inputs, path: Path) -> None:
    """Optional trials.csv (order_id|mo, line_id|line_name, start_hour, end_hour)
    gives pinned windows for TRIAL_MOVED. Absent -> check is skipped."""
    tr = _read_csv(path)
    if tr is None:
        return
    for _, r in tr.iterrows():
        oid = _s(r.get("order_id")) or _s(r.get("mo"))
        lid = _num(r.get("line_id"))
        if lid is None:
            lid = inp.line_by_name.get(_s(r.get("line_name")).upper())
        s, e = _num(r.get("start_hour")), _num(r.get("end_hour"))
        if oid and lid is not None and s is not None and e is not None:
            inp.trials[oid] = (int(lid), float(s), float(e))


def load_inputs_work_dir(data_dir: Path, cfg: Cfg) -> Inputs:
    d = Path(data_dir)
    inp = Inputs(cfg=cfg)
    _load_capabilities(inp, d / "capabilities_rates.csv")
    _load_sku_info(inp, d / "sku_info.csv")
    _load_line_rates(inp, d / "line_rates.csv")
    _load_changeovers(inp, d / "changeovers.csv")
    _load_initial_states(inp, d / "initial_states.csv")
    _load_downtimes_hours(inp, d / "downtimes.csv")
    _load_demand(inp, d / "demand_plan.csv")
    _load_line_cip_hrs(inp, d / "line_cip_hrs.csv")
    _load_trials(inp, d / "trials.csv")
    _load_current_mo(inp, d / "current_mo.csv")
    inp.pvb = _read_csv(d / "produced_vs_bounds.csv")
    # CIP reference (last clean END, hours relative to t0). Literal reading of
    # carryover_run_hours_since_last_cip_at_t0: at t0 the clock already shows
    # `carry` hours, so the last clean ended at -carry. last_cip_end_datetime,
    # when given, beats it.
    for lid, st in inp.init.items():
        if st.get("last_cip_end") is not None and cfg.planning_start_date is not None:
            inp.cip_ref_hour[lid] = (st["last_cip_end"] - cfg.planning_start_date).total_seconds() / 3600.0
            inp.cip_ref_src[lid] = "initial_states.last_cip_end_datetime"
        else:
            inp.cip_ref_hour[lid] = -float(st.get("carryover", 0.0))
            inp.cip_ref_src[lid] = "initial_states.carryover (t0 - carry)"
    return inp


def load_inputs_reference(reference_dir: Path, cfg: Cfg, anchor: datetime) -> Inputs:
    d = Path(reference_dir)
    inp = Inputs(cfg=cfg)
    _load_capabilities(inp, d / "capabilities_rates.csv")
    _load_sku_info(inp, d / "sku_info.csv")
    _load_line_rates(inp, d / "line_rates.csv")
    _load_changeovers(inp, d / "changeovers.csv")
    _load_initial_states(inp, d / "initial_states.csv")
    _load_downtimes_datetime(inp, d / "downtimes.csv", anchor)
    _load_demand(inp, d / "demand_plan.csv")
    _load_line_cip_hrs(inp, d / "line_cip_hrs.csv")
    for lid, st in inp.init.items():
        inp.cip_ref_hour[lid] = -float(st.get("carryover", 0.0))
        inp.cip_ref_src[lid] = "initial_states.carryover (t0 - carry)"
    _load_cip_info(inp, d / "cip_info.csv", anchor)   # overrides max + reference per line
    inp.pvb = None
    return inp


# ---------------------------------------------------------------------------
# Block loading
# ---------------------------------------------------------------------------
def blocks_from_schedule(sched: pd.DataFrame, inp: Inputs) -> List[Block]:
    out: List[Block] = []
    for i, r in sched.reset_index(drop=True).iterrows():
        lid = _num(r.get("line_id"))
        lid_i = int(lid) if lid is not None else None
        name = _s(r.get("line_name")) or (inp.lines.get(lid_i, f"L{lid_i}") if lid_i is not None else "?")
        is_trial = str(r.get("is_trial", "")).strip().lower() in ("true", "1", "yes")
        out.append(Block(
            idx=int(i), kind="trial" if is_trial else "production",
            line_id=lid_i, line_name=name,
            order_id=_s(r.get("order_id")) or None, sku=_sku(r.get("sku")) or None,
            start=float(_num(r.get("start_hour"), float("nan"))),
            end=float(_num(r.get("end_hour"), float("nan"))),
            run_hours=_num(r.get("run_hours")), qty_kg=_num(r.get("qty_kg")),
            committed=False, is_trial=is_trial, src=f"schedule_phase2.csv row {i}"))
    return out


def blocks_from_cips(cips: pd.DataFrame, inp: Inputs, committed: bool = False) -> List[Block]:
    out: List[Block] = []
    for i, r in cips.reset_index(drop=True).iterrows():
        lid = _num(r.get("line_id"))
        lid_i = int(lid) if lid is not None else None
        name = _s(r.get("line_name")) or (inp.lines.get(lid_i, f"L{lid_i}") if lid_i is not None else "?")
        out.append(Block(idx=int(i), kind="cip", line_id=lid_i, line_name=name, order_id=None, sku=None,
                         start=float(_num(r.get("start_hour"), float("nan"))),
                         end=float(_num(r.get("end_hour"), float("nan"))),
                         run_hours=None, qty_kg=None, committed=committed, src=f"cip_windows.csv row {i}"))
    return out


def blocks_from_calendar(cal: pd.DataFrame, inp: Inputs) -> List[Block]:
    out: List[Block] = []
    for i, r in cal.reset_index(drop=True).iterrows():
        kind = _s(r.get("block_type")).lower() or "production"
        attrs = _s(r.get("attrs"))
        locked = str(r.get("locked", "")).strip().lower() in ("true", "1", "yes")
        committed = ("current_state:" in attrs) or locked
        lid = _num(r.get("line_id"))
        name = _s(r.get("line_name"))
        if lid is None and name:
            lid = inp.line_by_name.get(name.upper())
        lid_i = int(lid) if lid is not None else None
        if not name and lid_i is not None:
            name = inp.lines.get(lid_i, f"L{lid_i}")
        s, e = _num(r.get("start_h")), _num(r.get("end_h"))
        sku = _sku(r.get("sku")) or None
        if kind == "cip":
            sku = None
        out.append(Block(
            idx=int(i), kind=kind, line_id=lid_i, line_name=name or "?",
            order_id=_s(r.get("order_id")) or None, sku=sku,
            start=float(s) if s is not None else float("nan"),
            end=float(e) if e is not None else float("nan"),
            run_hours=(float(e) - float(s)) if (s is not None and e is not None) else None,
            qty_kg=_num(r.get("qty_kg")), committed=committed, is_trial=(kind == "trial"),
            src=f"calendar {_s(r.get('block_id')) or i}"))
    return out


# ---------------------------------------------------------------------------
# Interval helpers (half-open [s, e))
# ---------------------------------------------------------------------------
def overlap_h(a_s: float, a_e: float, b_s: float, b_e: float) -> float:
    return max(0.0, min(a_e, b_e) - max(a_s, b_s))


def _fmt(x: Optional[float]) -> str:
    return "nan" if x is None else f"{x:g}"


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
class _Ctx:
    def __init__(self, inp: Inputs, blocks: List[Block], rep: ValidationReport,
                 tol: float, calendar_mode: bool,
                 material: Optional[Callable[[Block], Any]] = None):
        self.inp = inp
        self.cfg = inp.cfg
        self.blocks = blocks
        self.rep = rep
        self.tol = tol
        self.calendar_mode = calendar_mode
        self.material = material
        # Clean windows per line: (start, end, source, committed)
        self.cleans: Dict[Optional[int], List[Tuple[float, float, str, bool]]] = defaultdict(list)
        for b in blocks:
            if b.kind == "cip" and not (math.isnan(b.start) or math.isnan(b.end)):
                self.cleans[b.line_id].append((b.start, b.end, b.src, b.committed))
        n_dt_cip = 0
        for w in inp.downtimes:
            if w.is_cip:
                self.cleans[w.line_id].append((w.start, w.end, f"downtime '{w.reason}'", True))
                n_dt_cip += 1
        for k in self.cleans:
            self.cleans[k].sort()
        rep.stats["cleans_from_downtime_rows"] = n_dt_cip
        rep.stats["downtime_rows_mixed_with_cip"] = sum(1 for w in inp.downtimes if w.mixed_cip)
        self.prod = [b for b in blocks if b.is_prod and not (math.isnan(b.start) or math.isnan(b.end))]
        self.by_line: Dict[Optional[int], List[Block]] = defaultdict(list)
        for b in self.prod:
            self.by_line[b.line_id].append(b)
        for k in self.by_line:
            self.by_line[k].sort(key=lambda b: (b.start, b.end))

    def ran(self, name: str, note: str = "") -> None:
        self.rep.checks_run.append(name if not note else f"{name} ({note})")

    # ---- structural -------------------------------------------------------
    def check_structure(self) -> None:
        rep, inp = self.rep, self.inp
        seen: Dict[Tuple, int] = {}
        for b in self.blocks:
            if math.isnan(b.start) or math.isnan(b.end):
                rep.add("NEGATIVE_OR_ZERO", ERROR, b.line_name, b.order_id, None,
                        f"{b.kind} block has NaN start/end ({b.src})")
                continue
            if b.kind in PRODUCTION_KINDS or b.kind == "cip":
                key = (b.kind, b.line_id, b.order_id, b.sku, round(b.start, 6), round(b.end, 6))
                if key in seen:
                    rep.add("DUPLICATE_ROWS", ERROR, b.line_name, b.order_id, b.dur,
                            f"{b.kind} row duplicates row {seen[key]} ({b.src}) at [{b.start:g},{b.end:g})")
                else:
                    seen[key] = b.idx
            if b.end - b.start <= self.tol:
                rep.add("NEGATIVE_OR_ZERO", ERROR, b.line_name, b.order_id, b.dur,
                        f"{b.kind} block has end<=start [{b.start:g},{b.end:g}) ({b.src})")
            if b.is_prod:
                if b.run_hours is not None and b.run_hours <= self.tol:
                    rep.add("NEGATIVE_OR_ZERO", ERROR, b.line_name, b.order_id, b.run_hours,
                            f"run_hours={b.run_hours:g} <= 0 ({b.src})")
                if b.qty_kg is not None and b.qty_kg <= 0:
                    sev = WARN if b.committed else ERROR
                    rep.add("NEGATIVE_OR_ZERO", sev, b.line_name, b.order_id, b.dur,
                            f"qty_kg={b.qty_kg:g} <= 0 ({b.src})")
                if b.run_hours is not None and abs((b.end - b.start) - b.run_hours) > self.tol:
                    rep.add("RUN_HOURS_MISMATCH", ERROR, b.line_name, b.order_id,
                            (b.end - b.start) - b.run_hours,
                            f"end-start={b.end - b.start:g} but run_hours={b.run_hours:g} ({b.src})")
            # unknown line / sku
            if b.kind in PRODUCTION_KINDS or b.kind == "cip":
                if b.line_id is None or b.line_id not in inp.lines:
                    rep.add("UNKNOWN_LINE", ERROR, b.line_name, b.order_id, None,
                            f"line_id={b.line_id} not in capabilities_rates.csv ({b.src})")
            if b.is_prod:
                if not b.sku:
                    rep.add("UNKNOWN_SKU", ERROR, b.line_name, b.order_id, None, f"missing sku ({b.src})")
                elif inp.skus and b.sku not in inp.skus:
                    rep.add("UNKNOWN_SKU", ERROR, b.line_name, b.order_id, None,
                            f"sku {b.sku} not in capabilities_rates.csv/sku_info.csv ({b.src})")
                if inp.orders and not b.committed and (b.order_id not in inp.orders):
                    rep.add("UNKNOWN_ORDER", WARN, b.line_name, b.order_id, None,
                            f"order_id not in demand_plan.csv ({b.src}); due/demand checks skipped for it")
                elif b.order_id in inp.orders and b.sku and inp.orders[b.order_id].sku != b.sku:
                    rep.add("UNKNOWN_SKU", ERROR, b.line_name, b.order_id, None,
                            f"row sku {b.sku} != demand_plan sku {inp.orders[b.order_id].sku}")
        for n in ("DUPLICATE_ROWS", "NEGATIVE_OR_ZERO", "RUN_HOURS_MISMATCH", "UNKNOWN_LINE",
                  "UNKNOWN_SKU", "UNKNOWN_ORDER"):
            self.ran(n)

    # ---- overlap / downtime -----------------------------------------------
    def check_overlap(self) -> None:
        rep = self.rep
        occ = [b for b in self.blocks
               if (b.is_prod or b.kind == "cip") and not (math.isnan(b.start) or math.isnan(b.end))]
        by_line: Dict[Optional[int], List[Block]] = defaultdict(list)
        for b in occ:
            by_line[b.line_id].append(b)
        for lid, bl in by_line.items():
            bl.sort(key=lambda b: (b.start, b.end))
            for i in range(len(bl)):
                for j in range(i + 1, len(bl)):
                    a, c = bl[i], bl[j]
                    if c.start >= a.end - self.tol:
                        if c.start >= a.end:
                            break
                        continue
                    ov = overlap_h(a.start, a.end, c.start, c.end)
                    if ov > self.tol:
                        # a committed calendar CIP over a committed production is a
                        # board artefact, still an ERROR: two things cannot share a line.
                        rep.add("OVERLAP", ERROR, a.line_name,
                                a.order_id or c.order_id, ov,
                                f"{a.kind} {a.order_id or ''}[{a.start:g},{a.end:g}) overlaps "
                                f"{c.kind} {c.order_id or ''}[{c.start:g},{c.end:g}) by {ov:g}h")
        self.ran("OVERLAP")

    def check_in_downtime(self) -> None:
        rep, inp = self.rep, self.inp
        windows: List[Window] = list(inp.downtimes)
        # calendar-mode downtime-like blocks (the board often carries the same
        # window that downtimes.csv has: dedupe on (line, start, end) at 1e-3 h)
        have = {(w.line_id, round(w.start, 3), round(w.end, 3)) for w in windows}
        for b in self.blocks:
            if b.kind in DOWNTIME_KINDS and not (math.isnan(b.start) or math.isnan(b.end)):
                key = (b.line_id, round(b.start, 3), round(b.end, 3))
                if key in have:
                    continue
                have.add(key)
                windows.append(Window(b.line_id, b.line_name, b.start, b.end, f"{b.kind} block", False, False))
        n = 0
        for b in self.blocks:
            if not (b.is_prod or b.kind == "cip") or math.isnan(b.start) or math.isnan(b.end):
                continue
            for w in windows:
                if w.line_id != b.line_id:
                    continue
                ov = overlap_h(b.start, b.end, w.start, w.end)
                if ov <= self.tol:
                    continue
                if b.kind == "cip" and (w.is_cip or w.mixed_cip):
                    continue  # the same clean, seen from two files
                if b.committed and w.mixed_cip and "production" in w.reason.lower():
                    continue  # committed block IS the staged downtime
                sev = ERROR if b.is_prod else WARN
                if b.committed:
                    sev = WARN
                rep.add("IN_DOWNTIME", sev, b.line_name, b.order_id, ov,
                        f"{b.kind} [{b.start:g},{b.end:g}) overlaps downtime "
                        f"[{w.start:g},{w.end:g}) '{w.reason}' by {ov:g}h")
                n += 1
        self.ran("IN_DOWNTIME", f"{len(windows)} windows")

    # ---- capability / rate / qty ------------------------------------------
    def check_capability_qty(self) -> None:
        rep, inp, cfg = self.rep, self.inp, self.cfg
        for b in self.prod:
            if b.line_id is None or not b.sku:
                continue
            cap = inp.capable.get((b.line_id, b.sku))
            r_cfg = inp.rate_cfg(b.line_id, b.sku)
            r_oth = inp.rate_other(b.line_id, b.sku)
            if (b.line_id, b.sku) in inp.capable and (cap != 1 or (r_cfg or 0) <= 0):
                sev = WARN if (b.committed or b.is_trial) else ERROR
                rep.add("LINE_NOT_CAPABLE", sev, b.line_name, b.order_id, b.dur,
                        f"sku {b.sku} on {b.line_name}: capable={cap} rate_cfg={_fmt(r_cfg)}"
                        + (" (committed/current_state row, reported only)" if b.committed else "")
                        + (" (trial)" if b.is_trial else ""))
            elif (b.line_id, b.sku) not in inp.capable and inp.capable:
                sev = WARN if (b.committed or b.is_trial) else ERROR
                rep.add("LINE_NOT_CAPABLE", sev, b.line_name, b.order_id, b.dur,
                        f"no capabilities_rates.csv row for ({b.line_name}, {b.sku})")
            if b.qty_kg is None or b.run_hours is None or r_cfg is None or r_cfg <= 0:
                continue
            exp_cfg = r_cfg * b.run_hours
            d_cfg = b.qty_kg - exp_cfg
            d_oth = (b.qty_kg - r_oth * b.run_hours) if (r_oth is not None and r_oth > 0) else None
            tol = max(cfg.qty_abs_tol_kg, cfg.qty_rel_tol * exp_cfg)
            if abs(d_cfg) > tol:
                which = "flat line_rates.csv" if not cfg.use_sku_rates else "capabilities calc_rate_kgph"
                other = "capabilities calc_rate_kgph" if not cfg.use_sku_rates else "flat line_rates.csv"
                # over-claim (kg the hours cannot make) is physical -> ERROR;
                # under-claim is bookkeeping -> WARN; committed rows WARN.
                sev = ERROR if (d_cfg > 0 and not b.committed) else WARN
                rep.add("QTY_RATE_MISMATCH", sev, b.line_name, b.order_id, b.run_hours,
                        f"qty_kg={b.qty_kg:g} vs {which} {r_cfg:g}x{b.run_hours:g}h={exp_cfg:g} "
                        f"(delta {d_cfg:+.1f} kg); vs {other} "
                        + (f"{r_oth:g}x{b.run_hours:g}h (delta {d_oth:+.1f} kg)" if d_oth is not None else "n/a")
                        + (" [committed]" if b.committed else ""))
        self.ran("LINE_NOT_CAPABLE")
        self.ran("QTY_RATE_MISMATCH",
                 "rate = " + ("capabilities calc_rate_kgph" if cfg.use_sku_rates else "flat line_rates.csv")
                 + f", tol max({cfg.qty_abs_tol_kg} kg, {cfg.qty_rel_tol:.3%})")

    # ---- min run / max lines ----------------------------------------------
    def check_runs(self) -> None:
        rep, inp, cfg = self.rep, self.inp, self.cfg
        per_lo: Dict[Tuple[Optional[int], Optional[str]], float] = defaultdict(float)
        lines_per_order: Dict[str, set] = defaultdict(set)
        for b in self.prod:
            if b.run_hours is not None and b.run_hours < cfg.min_run_hours - self.tol:
                sev = WARN if (b.committed or b.is_trial) else ERROR
                rep.add("MIN_RUN", sev, b.line_name, b.order_id, b.run_hours,
                        f"block run {b.run_hours:g}h < min_run_hours {cfg.min_run_hours:g} ({b.src})"
                        + (" [committed]" if b.committed else ""))
            per_lo[(b.line_id, b.order_id)] += (b.run_hours if b.run_hours is not None else b.dur)
            if b.order_id and not b.committed:
                lines_per_order[b.order_id].add(b.line_id)
        # per (line, order) total: only when the blocks are individually >= min
        # (otherwise already reported) -- so it catches two 3h blocks summing 6? no:
        # the total rule is a floor on the SUM, report when sum < min.
        for (lid, oid), tot in per_lo.items():
            if tot < cfg.min_run_hours - self.tol:
                blocks = [b for b in self.prod if b.line_id == lid and b.order_id == oid]
                if all(b.run_hours is not None and b.run_hours < cfg.min_run_hours - self.tol for b in blocks) \
                        and len(blocks) == 1:
                    continue  # identical to the per-block finding
                rep.add("MIN_RUN", ERROR, inp.lines.get(lid, str(lid)), oid, tot,
                        f"(line, order) total {tot:g}h < min_run_hours {cfg.min_run_hours:g}")
        for oid, ls in lines_per_order.items():
            if len(ls) > cfg.max_lines_per_order:
                rep.add("MAX_LINES_PER_ORDER", ERROR, ",".join(inp.lines.get(l, str(l)) for l in sorted(ls, key=str)),
                        oid, None, f"order on {len(ls)} lines > max_lines_per_order {cfg.max_lines_per_order}")
        self.ran("MIN_RUN", f"min_run_hours={cfg.min_run_hours:g}")
        self.ran("MAX_LINES_PER_ORDER", f"max={cfg.max_lines_per_order}")

    # ---- gate / initial setup / horizon / trials ---------------------------
    def check_gate_horizon(self) -> None:
        rep, inp, cfg = self.rep, self.inp, self.cfg
        H = cfg.horizon_h
        for b in self.blocks:
            if not (b.is_prod or b.kind == "cip") or math.isnan(b.start) or math.isnan(b.end):
                continue
            if b.start < -self.tol or b.end > H + self.tol:
                sev = WARN if b.committed else ERROR
                rep.add("HORIZON", sev, b.line_name, b.order_id,
                        max(0.0, -b.start) + max(0.0, b.end - H),
                        f"{b.kind} [{b.start:g},{b.end:g}) outside [0,{H:g}]" + (" [committed]" if b.committed else ""))
            if b.is_prod and not b.committed and b.line_id in inp.init:
                gate = inp.init[b.line_id]["available_from"]
                if b.start < gate - self.tol:
                    sev = WARN if b.is_trial else ERROR
                    rep.add("BEFORE_GATE", sev, b.line_name, b.order_id, gate - b.start,
                            f"starts {b.start:g} < available_from_hour {gate:g}" + (" (trial, pinned)" if b.is_trial else ""))
        # First block on each line: setup from initial_sku (+ long shutdown extra)
        for lid, bl in self.by_line.items():
            st = inp.init.get(lid)
            if not st or not bl:
                continue
            first = next((b for b in bl if not b.committed), None)
            if first is None:
                continue
            init_sku = st["initial_sku"]
            base = 0.0
            if init_sku and init_sku.upper() != "CLEAN" and first.sku and init_sku != first.sku:
                base = inp.setup.get((init_sku, first.sku), 0.0)
            extra = st["long_shutdown_extra"] if st["long_shutdown_flag"] == 1 else 0.0
            need = st["available_from"] + base + extra
            if base + extra > 0 and first.start < need - self.tol:
                rep.add("INITIAL_SETUP", ERROR, first.line_name, first.order_id, need - first.start,
                        f"first block starts {first.start:g} < gate {st['available_from']:g} + "
                        f"setup({init_sku}->{first.sku})={base:g} + long_shutdown_extra={extra:g}")
        self.ran("HORIZON", f"H={H:g}")
        self.ran("BEFORE_GATE")
        self.ran("INITIAL_SETUP")
        # trials
        trial_blocks = [b for b in self.prod if b.is_trial]
        if not trial_blocks:
            self.ran("TRIAL_MOVED", "skipped: no is_trial rows")
        elif not inp.trials:
            self.ran("TRIAL_MOVED", "skipped: no pinned window source (trials.csv)")
            for b in trial_blocks:
                rep.add("TRIAL_MOVED", WARN, b.line_name, b.order_id, None,
                        "is_trial row but no pinned window derivable (no trials.csv)")
        else:
            for b in trial_blocks:
                pin = inp.trials.get(b.order_id or "")
                if pin is None:
                    rep.add("TRIAL_MOVED", WARN, b.line_name, b.order_id, None, "trial not in trials.csv")
                    continue
                pl, ps, pe = pin
                if pl != b.line_id or abs(ps - b.start) > self.tol or abs(pe - b.end) > self.tol:
                    rep.add("TRIAL_MOVED", ERROR, b.line_name, b.order_id,
                            abs(ps - b.start) + abs(pe - b.end),
                            f"trial pinned to {inp.lines.get(pl, pl)} [{ps:g},{pe:g}) but scheduled "
                            f"{b.line_name} [{b.start:g},{b.end:g})")
            self.ran("TRIAL_MOVED")

    # ---- CIP --------------------------------------------------------------
    def check_cip(self) -> None:
        rep, inp, cfg = self.rep, self.inp, self.cfg
        disabled: List[str] = []
        used_fallback: List[str] = []
        # duration
        for lid, cl in self.cleans.items():
            for (s, e, src, committed) in cl:
                if abs((e - s) - cfg.cip_duration_h) > self.tol:
                    rep.add("CIP_DURATION", WARN if committed else ERROR, inp.lines.get(lid, str(lid)), None,
                            (e - s) - cfg.cip_duration_h,
                            f"clean [{s:g},{e:g}) lasts {e - s:g}h != duration_h {cfg.cip_duration_h:g} ({src})")
        self.ran("CIP_DURATION", f"duration_h={cfg.cip_duration_h:g}")
        # interval
        for lid, bl in self.by_line.items():
            if not bl:
                continue
            name = inp.lines.get(lid, str(lid))
            mx = inp.max_cip_hrs.get(lid)
            if mx is None:
                mx = cfg.cip_interval_h
                used_fallback.append(name)
            if mx >= CIP_STANDDOWN_MIN:
                disabled.append(f"{name}(max_cip_hrs={mx:g})")
                continue
            ref = inp.cip_ref_hour.get(lid, 0.0)
            ref_src = inp.cip_ref_src.get(lid, "t0 (no carryover info)")
            cleans = self.cleans.get(lid, [])
            # consecutive cleans (start of next vs end of previous), only spans with production
            prev_end, prev_src = ref, ref_src
            spans: List[Tuple[float, float, str]] = []
            for (s, e, src, _c) in cleans:
                spans.append((prev_end, s, prev_src))
                prev_end, prev_src = e, src
            last_end = max(b.end for b in bl)
            spans.append((prev_end, last_end, prev_src + " -> last production end"))
            for (a, z, src) in spans:
                prod_in = [b for b in bl if overlap_h(b.start, b.end, a, z) > self.tol]
                if not prod_in:
                    continue
                worst = max(min(b.end, z) for b in prod_in)
                elapsed = worst - a
                if elapsed > mx + self.tol:
                    rep.add("CIP_INTERVAL", ERROR, name, None, elapsed - mx,
                            f"{elapsed:g}h wall-clock since last clean end at {a:g} ({src}) "
                            f"until production end {worst:g} > max_cip_hrs {mx:g} (no tolerance)")
        note = "no +12h tolerance"
        if disabled:
            note += "; DISABLED (stand-down >= 10000) on: " + ", ".join(disabled)
            rep.stats["cip_interval_disabled_lines"] = disabled
        if used_fallback:
            note += f"; [cip] interval_h={cfg.cip_interval_h:g} fallback on: " + ", ".join(used_fallback)
        self.ran("CIP_INTERVAL", note)
        # cip_req_after between adjacent production SKUs
        n_pairs = 0
        for lid, bl in self.by_line.items():
            name = inp.lines.get(lid, str(lid))
            cleans = self.cleans.get(lid, [])
            for a, b in zip(bl, bl[1:]):
                if not a.sku or not b.sku or a.sku == b.sku:
                    continue
                if inp.cip_req.get((a.sku, b.sku), 0) != 1:
                    continue
                n_pairs += 1
                inside = [(s, e) for (s, e, _s, _c) in cleans if s >= a.end - self.tol and e <= b.start + self.tol]
                if not inside:
                    sev = WARN if (a.committed and b.committed) else ERROR
                    rep.add("CIP_REQ_MISSING", sev, name, b.order_id, b.start - a.end,
                            f"{a.sku}->{b.sku} requires a CIP (cip_req_after=1) but no clean lies fully in "
                            f"gap [{a.end:g},{b.start:g}) ({b.start - a.end:g}h)")
        self.ran("CIP_REQ_MISSING", f"{n_pairs} flagged adjacencies")

    # ---- changeovers ------------------------------------------------------
    def check_changeovers(self) -> None:
        rep, inp, cfg = self.rep, self.inp, self.cfg
        n = 0
        for lid, bl in self.by_line.items():
            name = inp.lines.get(lid, str(lid))
            cleans = self.cleans.get(lid, [])
            for a, b in zip(bl, bl[1:]):
                if not a.sku or not b.sku or a.sku == b.sku:
                    continue
                n += 1
                pair = (a.sku, b.sku)
                if pair not in inp.setup:
                    if inp.setup:
                        rep.add("CHANGEOVER_GAP", WARN, name, b.order_id, None,
                                f"no changeovers.csv row for {a.sku}->{b.sku} (direction-aware); setup assumed 0")
                    continue
                setup = inp.setup[pair]
                gap = b.start - a.end
                cip_in_gap = sum(overlap_h(s, e, a.end, b.start) for (s, e, _s, _c) in cleans)
                solver_rounded = math.floor(setup + 0.5)
                if gap < setup - self.tol:
                    sev = WARN if (a.committed and b.committed) else ERROR
                    rep.add("CHANGEOVER_GAP", sev, name, b.order_id, setup - gap,
                            f"{a.sku}->{b.sku} needs setup {setup:g}h (exact; solver rounds half-up to "
                            f"{solver_rounded}) but gap is {gap:g}h [{a.end:g},{b.start:g}); shortfall "
                            f"{setup - gap:g}h; CIP hours in gap {cip_in_gap:g}"
                            + ("" if cip_in_gap > 0 else " (gap not covered by any CIP)"))
                elif setup > 0 and gap - cip_in_gap < setup - self.tol:
                    rep.add("CHANGEOVER_GAP", WARN, name, b.order_id, setup - (gap - cip_in_gap),
                            f"{a.sku}->{b.sku} setup {setup:g}h fits gap {gap:g}h only by overlapping a CIP "
                            f"({cip_in_gap:g}h of CIP in gap; free time {gap - cip_in_gap:g}h)")
        self.ran("CHANGEOVER_GAP", f"{n} different-SKU adjacencies; exact setup_hours, direction-aware")

    # ---- due window / demand ----------------------------------------------
    def check_demand(self) -> None:
        rep, inp, cfg = self.rep, self.inp, self.cfg
        if not inp.orders:
            self.ran("DUE_WINDOW", "skipped: no demand_plan.csv")
            self.ran("DEMAND_BOUNDS", "skipped")
            self.ran("DEMAND_SUM", "skipped")
            return
        # In calendar mode the reference demand_plan.csv is the RAW plan (its
        # own anchor / no netting) while the board was solved on a staged copy,
        # so due/demand findings there are advisory (WARN), never ERROR.
        due_sev = WARN if self.calendar_mode else ERROR
        # Early-start rule = the plant's early-fill policy (2026-09-04, see
        # Cfg.early_fill_hours). Until then the check mirrored fix SB-1
        # (only the SECOND demand week, 48 h). Now: allow_week1_in_week0 off
        # -> ERROR; early_fill_hours None (unbounded) -> informational WARN
        # only, the hard floors being the gate / committed blocks / committed
        # MOs (their own checks); early_fill_hours = h -> WARN inside the
        # allowance, ERROR beyond it. The END wall (due_end + 1) stays hard.
        efh = cfg.early_fill_hours
        rep.stats["early_fill_hours"] = "unbounded" if efh is None else efh
        # Late-finish rule = the due-week policy (see Cfg.due_week_policy):
        # "hard" (shipped default) -> ERROR (the wall); "soft" (opt-in, plant
        # decision 2026-09-04 #2) -> a priced trade-off, WARN with the late
        # kg / kg-weeks.
        soft_weeks = str(cfg.due_week_policy).lower() == "soft"
        rep.stats["due_week_policy"] = "soft" if soft_weeks else "hard"
        late_kg_total = 0.0
        late_kg_weeks_total = 0.0
        late_orders: set = set()
        for b in self.prod:
            if b.committed or not b.order_id or b.order_id not in inp.orders:
                continue
            o = inp.orders[b.order_id]
            early = o.due_start - b.start
            late = b.end - (o.due_end + 1.0)
            if early > self.tol:
                if not cfg.allow_week1_in_week0:
                    sev, note = due_sev, " [allow_week1_in_week0 = false: no early fill]"
                elif efh is None:
                    sev = WARN
                    note = (" [early fill (policy: unbounded), plant decision 2026-09-04: legal "
                            "unless before the line's gate or a committed block/MO — see "
                            "BEFORE_GATE / OVERLAP / IN_DOWNTIME / EARLY_BEFORE_COMMITTED]")
                elif early <= efh + self.tol:
                    sev = WARN
                    note = (f" [inside the early-fill allowance early_fill_hours = {efh:g}h: "
                            f"may start from {max(0.0, o.due_start - efh):g}h]")
                else:
                    sev = due_sev
                    note = (f" [beyond the early-fill allowance early_fill_hours = {efh:g}h: "
                            f"legal start >= {max(0.0, o.due_start - efh):g}h]")
                rep.add("DUE_WINDOW", sev, b.line_name, b.order_id, early,
                        f"starts {b.start:g} < due_start_hour {o.due_start:g} (early by {early:g}h; "
                        f"window [{o.due_start:g},{o.due_end + 1:g}))" + note)
            if late > self.tol:
                # kg that landed past due_end + 1 (the block's kg spread
                # evenly over its hours) and the kg x whole weeks late: hours
                # [de+1, de+1+168) are one week late, the next 168 two, ...
                dur = max(0.0, b.end - b.start)
                late_h = min(late, dur)
                kg_late = (float(b.qty_kg) * late_h / dur) if (b.qty_kg is not None and dur > 0) else 0.0
                kg_weeks = 0.0
                if b.qty_kg is not None and dur > 0:
                    t0 = o.due_end + 1.0
                    k = 1
                    while t0 + WEEK_STEP_H * (k - 1) < b.end:
                        kg_weeks += float(b.qty_kg) * max(0.0, b.end - max(b.start, t0 + WEEK_STEP_H * (k - 1))) / dur
                        k += 1
                late_kg_total += kg_late
                late_kg_weeks_total += kg_weeks
                late_orders.add(b.order_id)
                if soft_weeks:
                    sev_late = WARN
                    note = (f" [soft due weeks (plant decision 2026-09-04 #2): priced trade-off, "
                            f"~{kg_late:,.0f} kg late = {kg_weeks:,.0f} kg-weeks]")
                else:
                    sev_late = due_sev
                    note = f" [due_week_policy = hard; ~{kg_late:,.0f} kg late]"
                rep.add("DUE_WINDOW", sev_late, b.line_name, b.order_id, late,
                        f"ends {b.end:g} > due_end_hour+1 = {o.due_end + 1:g} (late by {late:g}h)" + note)
        rep.stats["late_kg"] = round(late_kg_total, 1)
        rep.stats["late_kg_weeks"] = round(late_kg_weeks_total, 1)
        rep.stats["late_orders"] = len(late_orders)
        policy = ("no early fill (allow_week1_in_week0 = false)" if not cfg.allow_week1_in_week0
                  else "early fill unbounded (plant decision 2026-09-04)" if efh is None
                  else f"early_fill_hours = {efh:g}h")
        end_rule = ("soft due weeks: late finish inside the horizon is a priced trade-off (WARN)"
                    if soft_weeks else "hard end due_end+1")
        self.ran("DUE_WINDOW", (f"{end_rule}; {policy}" if not self.calendar_mode
                 else "advisory in calendar mode: reference demand_plan.csv anchor may differ from the board"))
        # produced per order from schedule
        made: Dict[str, float] = defaultdict(float)
        for b in self.prod:
            if b.order_id and b.qty_kg is not None and not b.committed:
                made[b.order_id] += b.qty_kg
        pvb_map: Dict[str, Dict[str, float]] = {}
        if inp.pvb is not None:
            for _, r in inp.pvb.iterrows():
                pvb_map[_s(r.get("order_id"))] = dict(
                    produced=float(_num(r.get("produced"), 0.0) or 0.0),
                    qty_min=_num(r.get("qty_min")), qty_max=_num(r.get("qty_max")))
        under_sev = WARN if (cfg.soft_demand or self.calendar_mode) else ERROR
        over_sev = WARN if self.calendar_mode else ERROR
        for oid, o in inp.orders.items():
            sched_kg = made.get(oid, 0.0)
            pv = pvb_map.get(oid)
            pv_kg = pv["produced"] if pv else None
            # bounds definition cross-check
            if pv and pv.get("qty_min") is not None and pv.get("qty_max") is not None:
                if abs(pv["qty_min"] - o.qty_min) > 0.5 or (math.isfinite(o.qty_max) and abs(pv["qty_max"] - o.qty_max) > 0.5):
                    rep.add("DEMAND_BOUNDS", WARN, None, oid, None,
                            f"bounds definition differs: pvb [{pv['qty_min']:g},{pv['qty_max']:g}] vs recomputed "
                            f"floor/ceil(target x pct) [{o.qty_min:g},{o.qty_max:g}]")
            # Both sources are judged; when they agree (within the DEMAND_SUM
            # tolerance) one finding names both, otherwise one per source.
            if pv_kg is not None and abs(pv_kg - sched_kg) <= cfg.demand_sum_tol_kg:
                sources: List[Tuple[str, float]] = [("schedule = produced_vs_bounds", sched_kg)]
            else:
                sources = [("schedule", sched_kg)] + ([("produced_vs_bounds", pv_kg)] if pv_kg is not None else [])
            for src, kg in sources:
                if kg < o.qty_min - 0.5:
                    rep.add("DEMAND_BOUNDS", under_sev, None, oid, None,
                            f"{src}: produced {kg:g} kg < qty_min {o.qty_min:g} (short {o.qty_min - kg:g})"
                            + (" [soft_demand=true]" if cfg.soft_demand else ""))
                if kg > o.qty_max + 0.5:
                    rep.add("DEMAND_BOUNDS", over_sev, None, oid, None,
                            f"{src}: produced {kg:g} kg > qty_max {o.qty_max:g} (excess {kg - o.qty_max:g})")
            if pv_kg is not None and abs(pv_kg - sched_kg) > cfg.demand_sum_tol_kg:
                rep.add("DEMAND_SUM", ERROR, None, oid, None,
                        f"sum(schedule qty_kg)={sched_kg:g} vs produced_vs_bounds produced={pv_kg:g} "
                        f"(delta {sched_kg - pv_kg:+.1f} kg > {cfg.demand_sum_tol_kg} kg)")
        for oid in made:
            if oid not in inp.orders and pvb_map and oid not in pvb_map:
                rep.add("DEMAND_SUM", WARN, None, oid, None,
                        f"order has {made[oid]:g} kg scheduled but is in neither demand_plan nor produced_vs_bounds")
        if inp.pvb is not None:
            missing = [oid for oid in inp.orders if oid not in pvb_map]
            if missing:
                rep.add("DEMAND_SUM", WARN, None, None, None,
                        f"{len(missing)} demand orders missing from produced_vs_bounds.csv: {missing[:8]}")
        self.ran("DEMAND_BOUNDS", "soft_demand=" + str(cfg.soft_demand) + " (under qty_min -> " + under_sev + ")")
        self.ran("DEMAND_SUM", "tol 0.5 kg" if inp.pvb is not None else "skipped: no produced_vs_bounds.csv")

    # ---- material hook ----------------------------------------------------
    # ---- committed current-state MOs (E-style work dirs) -------------------
    def _is_committed_mo(self, b: Block) -> bool:
        """A schedule row that IS a committed current-state MO: the solver
        writes them with order_id '<mo>|CUR' (or '<mo>#n|CUR', SA-12); a bare
        mo number from current_mo.csv counts too."""
        if not b.is_prod or b.is_trial or not b.order_id:
            return False
        oid = b.order_id
        return oid.endswith("|CUR") or oid in self.inp.current_mos

    def check_early_before_committed(self) -> None:
        """Plant decision 2026-09-04: early fill may go as far back as free
        capacity allows but NEVER before an already scheduled MO. In an
        E-style work dir the scheduled MOs are the orders of current_mo.csv,
        so a demand block that starts before the end of a committed MO block
        on the same line is an ERROR (model_builder's committed-MO floor). In
        Scenario F / calendar mode the scheduled MOs are the availability
        gate and committed downtime windows — BEFORE_GATE / OVERLAP /
        IN_DOWNTIME already cover them, so this check is skipped there."""
        rep, inp = self.rep, self.inp
        if self.calendar_mode:
            self.ran("EARLY_BEFORE_COMMITTED",
                     "skipped: calendar mode (committed work = gate + downtime windows)")
            return
        if not inp.current_mos:
            self.ran("EARLY_BEFORE_COMMITTED", "skipped: no current_mo.csv")
            return
        n = 0
        for lid, bl in self.by_line.items():
            committed = [b for b in bl if self._is_committed_mo(b)]
            if not committed:
                continue
            for b in bl:
                if self._is_committed_mo(b) or b.is_trial or b.committed or not b.order_id:
                    continue
                if inp.orders and b.order_id not in inp.orders:
                    continue
                for c in committed:
                    if b.start < c.end - self.tol:
                        n += 1
                        rep.add("EARLY_BEFORE_COMMITTED", ERROR, b.line_name, b.order_id,
                                c.end - b.start,
                                f"demand block [{b.start:g},{b.end:g}) starts before committed MO "
                                f"{c.order_id} [{c.start:g},{c.end:g}) on the same line "
                                "(plant decision 2026-09-04: nothing before an already scheduled MO)")
        self.ran("EARLY_BEFORE_COMMITTED",
                 f"{len(inp.current_mos)} committed MO(s) in current_mo.csv; {n} violation(s)")

    def check_material(self) -> None:
        if self.material is None:
            self.ran("MATERIAL", "skipped: no hook")
            return
        n = 0
        for b in self.prod:
            res = self.material(b)
            if not res:
                continue
            items = res if isinstance(res, (list, tuple)) else [res]
            for it in items:
                n += 1
                if isinstance(it, Violation):
                    self.rep.violations.append(it)
                    if it.severity == ERROR:
                        self.rep.ok = False
                elif isinstance(it, dict):
                    self.rep.add("MATERIAL", it.get("severity", WARN), b.line_name, b.order_id,
                                 it.get("hours"), str(it.get("detail", "")))
                else:
                    self.rep.add("MATERIAL", WARN, b.line_name, b.order_id, None, str(it))
        self.ran("MATERIAL", f"hook returned {n} finding(s)")

    # ---- stats ------------------------------------------------------------
    def fill_stats(self) -> None:
        rep, inp = self.rep, self.inp
        rep.stats["blocks_total"] = len(self.blocks)
        kinds: Dict[str, int] = defaultdict(int)
        for b in self.blocks:
            kinds[b.kind] += 1
        rep.stats["blocks_by_kind"] = dict(kinds)
        rep.stats["production_hours"] = round(sum(b.dur for b in self.prod), 3)
        rep.stats["production_kg"] = round(sum(b.qty_kg or 0.0 for b in self.prod), 1)
        rep.stats["committed_blocks"] = sum(1 for b in self.blocks if b.committed)
        rep.stats["lines_with_production"] = len([l for l, bl in self.by_line.items() if bl])
        rep.stats["orders_in_demand"] = len(inp.orders)
        rep.stats["orders_scheduled"] = len({b.order_id for b in self.prod if b.order_id})
        rep.stats["cleans_total"] = sum(len(v) for v in self.cleans.values())
        rep.stats["changeover_pairs_loaded"] = len(inp.setup)
        rep.stats["changeover_duplicate_rows"] = inp.co_dup_pairs
        rep.stats["changeover_duplicate_conflicts"] = len(inp.co_dup_conflicts)
        rep.stats["cip_req_pairs_loaded"] = sum(1 for v in inp.cip_req.values() if v == 1)
        rep.stats["rate_dialect"] = ("capabilities calc_rate_kgph" if self.cfg.use_sku_rates
                                     else "flat line_rates.csv")
        rep.stats["horizon_h"] = self.cfg.horizon_h
        rep.stats["mode"] = "calendar" if self.calendar_mode else "work_dir"
        if inp.notes:
            rep.stats["input_notes"] = list(inp.notes)
        for (f, t, why) in inp.co_dup_conflicts[:50]:
            rep.add("CHANGEOVER_GAP", WARN, None, None, None,
                    f"changeovers.csv duplicate pair {f}->{t} disagrees: {why}")


def _run_all(inp: Inputs, blocks: List[Block], tol: float, calendar_mode: bool,
             material: Optional[Callable[[Block], Any]]) -> ValidationReport:
    rep = ValidationReport()
    ctx = _Ctx(inp, blocks, rep, tol, calendar_mode, material)
    ctx.check_structure()
    ctx.check_overlap()
    ctx.check_in_downtime()
    ctx.check_capability_qty()
    ctx.check_runs()
    ctx.check_gate_horizon()
    ctx.check_cip()
    ctx.check_changeovers()
    ctx.check_demand()
    ctx.check_early_before_committed()
    ctx.check_material()
    ctx.fill_stats()
    rep.ok = not any(v.severity == ERROR for v in rep.violations)
    return rep


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def validate_work_dir(data_dir, *, schedule=None, cips=None, config=None,
                      tolerance_h: float = 1e-6,
                      material: Optional[Callable[[Block], Any]] = None) -> ValidationReport:
    """Validate a solver work dir (hours dialect).

    schedule: DataFrame | path | None (-> data_dir/schedule_phase2.csv)
    cips:     DataFrame | path | None (-> data_dir/cip_windows.csv if present;
              downtime rows whose reason is only 'CIP' also count as cleans)
    config:   toml path | dict | Cfg | None (-> data_dir/flowstate.toml)
    material: optional callable(block) -> Violation | dict | str | list | None
    """
    d = Path(data_dir)
    cfg = load_cfg(config, d)
    inp = load_inputs_work_dir(d, cfg)
    if schedule is None:
        sched = _read_csv(d / "schedule_phase2.csv")
        if sched is None:
            rep = ValidationReport(ok=False)
            rep.add("UNKNOWN_LINE", ERROR, None, None, None, f"schedule_phase2.csv missing in {d}")
            return rep
    elif isinstance(schedule, (str, Path)):
        sched = _read_csv(Path(schedule))
    else:
        sched = schedule
    if cips is None:
        cdf = _read_csv(d / "cip_windows.csv")
    elif isinstance(cips, (str, Path)):
        cdf = _read_csv(Path(cips))
    else:
        cdf = cips
    blocks = blocks_from_schedule(sched, inp)
    if cdf is not None and len(cdf):
        blocks.extend(blocks_from_cips(cdf, inp))
    rep = _run_all(inp, blocks, tolerance_h, False, material)
    rep.stats["data_dir"] = str(d)
    rep.stats["cip_windows_csv"] = bool(cdf is not None and len(cdf))
    return rep


def validate_calendar(calendar_df, reference_dir, anchor: datetime, cfg=None, *,
                      tolerance_h: float = 1e-6,
                      material: Optional[Callable[[Block], Any]] = None) -> ValidationReport:
    """Validate a calendar_blocks-shaped frame (block_type production/cip/trial/
    maintenance/contractor/line_down; start_h/end_h floats from `anchor`) against
    data/reference-shaped inputs: downtimes.csv in WALL-CLOCK datetimes, cip_info.csv
    per-line PreviousCIP/MaxHoursBetweenCIP."""
    if isinstance(calendar_df, (str, Path)):
        calendar_df = _read_csv(Path(calendar_df))
    if isinstance(anchor, str):
        anchor = _parse_dt(anchor)
    c = load_cfg(cfg, Path(reference_dir))
    if c.planning_start_date is None:
        c.planning_start_date = anchor
    inp = load_inputs_reference(Path(reference_dir), c, anchor)
    blocks = blocks_from_calendar(calendar_df, inp)
    # The board stores start_h/end_h rounded to 3 decimals while cip_info /
    # downtimes are minute-precise datetimes: never let the tolerance drop
    # below that storage precision, or 120.0003 h reads as a CIP breach.
    tol = max(tolerance_h, CALENDAR_HOURS_PRECISION)
    rep = _run_all(inp, blocks, tol, True, material)
    rep.stats["tolerance_h"] = tol
    rep.stats["reference_dir"] = str(reference_dir)
    rep.stats["anchor"] = anchor.strftime("%Y-%m-%d %H:%M:%S")
    return rep


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Independent Flowstate schedule validator")
    ap.add_argument("--data-dir", type=Path, help="solver work dir (schedule_phase2.csv etc.)")
    ap.add_argument("--calendar", type=Path, help="calendar_blocks.csv to validate instead")
    ap.add_argument("--reference", type=Path, help="data/reference-shaped dir for --calendar")
    ap.add_argument("--anchor", type=str, help="'YYYY-MM-DD HH:MM:SS' hour-0 of the calendar")
    ap.add_argument("--config", type=Path, help="flowstate.toml (default: <dir>/flowstate.toml)")
    ap.add_argument("--json", type=Path, help="also write the report as JSON here")
    ap.add_argument("--strict", action="store_true", help="exit nonzero on WARN too")
    ap.add_argument("--max-lines", type=int, default=400)
    a = ap.parse_args(argv)
    if a.calendar is not None:
        if a.reference is None or a.anchor is None:
            ap.error("--calendar needs --reference and --anchor")
        rep = validate_calendar(a.calendar, a.reference, _parse_dt(a.anchor), a.config)
    elif a.data_dir is not None:
        rep = validate_work_dir(a.data_dir, config=a.config)
    else:
        ap.error("give --data-dir or --calendar")
        return 2
    print(rep.summary(a.max_lines))
    if a.json:
        a.json.write_text(json.dumps(rep.to_dict(), indent=2, default=str), encoding="utf-8")
    if rep.errors():
        return 1
    if a.strict and rep.warnings():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
