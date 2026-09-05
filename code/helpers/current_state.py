# helpers/current_state.py — "known-good current state" (handoff WW32, item 2).
#
# Builds the plant's ACTUAL state as calendar blocks from ground truth instead
# of the old seeded fixture:
#
#   manprg.txt / manprg2.txt  -> what each line is running and what is queued
#   cip_info.csv              -> last / next CIP and the per-line max interval
#
# Classification per manprg row (see flowstate-vif-imports skill):
#   * Item == "CIP"                     -> a CIP block, NEVER production output.
#                                          A manprg CIP row is the plant's own
#                                          committed clean (start + Hours) and
#                                          wins over cip_info's ScheduledCIP.
#   * Qty made >= Fct qty (or Left<=0)  -> COMPLETED, dropped (don't render).
#   * Qty made > 0, not complete        -> RUNNING. Locked; the solver may not
#                                          move it. Its end is RE-FORECAST from
#                                          the ACTUAL observed rate (cases made
#                                          / hours from start to the manprg
#                                          OBSERVATION time `as_of`) when
#                                          telemetry is usable ([scheduler]
#                                          reforecast_running_mo_ends, default
#                                          on); guard rails fall back to
#                                          manprg's pro-rata estimate, loudly.
#                                          The block carries the REMAINING kg;
#                                          the made kg is an actuals row in
#                                          `completed` (kind "made_part").
#   * Qty made empty/0                  -> QUEUED. Placed sequentially by start
#                                          date after the line's running MO,
#                                          unlocked (the solver may reshuffle).
#   * Item == "TRIALS"                  -> a TRIAL block (blocked line time,
#                                          never tonnage), placed like a queued
#                                          MO and kept through the CIP merge.
#
# `Qty made` EMPTY means "not started" — it is not a zero-production fact.
# A row whose start is in the FUTURE but shows Qty made > 0 is data noise:
# it is queued at its start (never running) and warned about.
#
# CIP synthesis: manprg CIP rows are drawn as committed cleans at their own
# duration. cip_info gives PreviousCIP, ScheduledCIP and MaxHoursBetweenCIP
# (120 or 144 per line — authoritative, it overrides [cip] interval_h). We
# space further CIPs at MaxHoursBetweenCIP from the LAST KNOWN clean until the
# end of the horizon, so weeks 2-3 are not silently CIP-free. A line already
# past its interval gets a catch-up clean at `now` and a loud warning; a line
# with no CIP history at all is phased from a STABLE reference (never today's
# midnight, which moved the grid 24 h per day) and warned about.
#
# CIP/production merge: CIP windows are fixed; a production MO that spans a
# CIP is split and the remainder is PUSHED after the clean (the MO keeps its
# hours), and everything queued behind it shifts by the same amount.
#
# Output is a plain calendar_blocks frame (CALENDAR_COLUMNS) in ANCHOR HOURS,
# so it can be handed straight to the Gantt, saved, or used as the solver's
# initial state. Everything is pure: pass `now` (and `as_of`) to test without
# a clock.

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from helpers.calendar_io import CALENDAR_COLUMNS
from helpers.cip_import import CipInfoResult, read_cip_info
from helpers.horizon import Horizon
from helpers.manprg_import import ManprgResult, read_manprg

DEFAULT_CIP_HOURS = 6.0

# Status vocabulary attached to every emitted block (attrs + `state` column).
RUNNING = "running"
QUEUED = "queued"
CIP_SCHEDULED = "cip_scheduled"
CIP_PROJECTED = "cip_projected"
# Actuals row kind in CurrentState.completed for the ALREADY-MADE part of a
# running MO (fix CA-3 / audit C04): start = true start, end = as_of.
MADE_PART = "made_part"
TRIAL_ITEM = "TRIALS"


@dataclass
class CurrentState:
    """Ground-truth calendar plus the accounting of how it was derived."""

    blocks: pd.DataFrame
    running: list[dict] = field(default_factory=list)
    queued: list[dict] = field(default_factory=list)
    # kind "completed" (made >= Fct, or superseded) AND kind "made_part" (the
    # made share of a running MO, keyed to when it was produced). Consumers
    # that net actuals read `made_kg`, `start_dt`, `hours`, `item`, `mo`.
    completed: list[dict] = field(default_factory=list)
    cips: list[dict] = field(default_factory=list)
    line_free_h: dict[str, float] = field(default_factory=dict)
    # End of the RUNNING MO only (queued MOs are solver orders when
    # current_mo.csv is produced; the gate must not double-count them).
    line_running_free_h: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # The two clocks this state was built with (audit C01/C03): `now` is the
    # render clock, `as_of` the manprg content observation time.
    now: datetime | None = None
    as_of: datetime | None = None
    as_of_source: str = ""

    @property
    def counts(self) -> dict[str, int]:
        return {
            "running": len(self.running),
            "queued": len(self.queued),
            "completed": sum(1 for r in self.completed
                             if r.get("kind") != MADE_PART),
            "made_part": sum(1 for r in self.completed
                             if r.get("kind") == MADE_PART),
            "cip": len(self.cips),
            "blocks": int(len(self.blocks)),
        }


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _bid(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return "cs_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(f) else f


def _is_blank(v) -> bool:
    """manprg leaves 'Qty made' EMPTY for a not-started MO — that is not 0."""
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    return str(v).strip() == ""


def _code(v) -> str:
    """'280351.0' -> '280351'. Item/MO codes are strings, never floats."""
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _hours(dt: datetime, anchor: datetime) -> float:
    return (pd.Timestamp(dt) - pd.Timestamp(anchor)).total_seconds() / 3600.0


def line_id_for(line: str, lines: pd.DataFrame | None) -> int:
    """Resolve a line_id from lines.csv; -1 when the table does not know it.

    The "digits minus 9" (P09=0) convention is used ONLY when no lines table
    is available. With a table, an unknown name (LMH-P99 -> 90 before fix
    CA-10 / audit adversarial-11) is NOT a line: return -1 so the caller can
    skip it with a warning instead of inventing a line and killing the solve.
    """
    name = str(line or "").strip().upper()
    if lines is not None and len(lines) and "line_name" in lines.columns:
        hit = lines[lines["line_name"].astype(str).str.strip().str.upper() == name]
        if len(hit):
            try:
                return int(hit.iloc[0]["line_id"])
            except (TypeError, ValueError):
                pass
        else:
            return -1
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) - 9 if digits else -1


def cip_hours(cfg: dict | None) -> float:
    cip = (cfg or {}).get("cip") or {}
    return _num(cip.get("duration_h"), DEFAULT_CIP_HOURS) or DEFAULT_CIP_HOURS


def cip_interval_for(line: str, cips: CipInfoResult, cfg: dict | None) -> float:
    """Per-line MaxHoursBetweenCIP wins over the global [cip] interval_h."""
    info = cips.by_line.get(str(line).strip().upper())
    if info is not None and info.max_hours_between > 0:
        return float(info.max_hours_between)
    return _num(((cfg or {}).get("cip") or {}).get("interval_h"), 120.0) or 120.0


# --------------------------------------------------------------------------
# manprg -> classified MO rows
# --------------------------------------------------------------------------

def classify_rows(frame: pd.DataFrame | None, now: datetime | None = None,
                  warnings: list[str] | None = None) -> list[dict]:
    """Tag every manprg row running / queued / completed / cip.

    `now` (optional, fix CA-7 / adversarial-5): a row that shows Qty made > 0
    but STARTS IN THE FUTURE cannot be running — it is queued at its start and
    reported in `warnings`. Without `now` the legacy rule (made > 0 => running)
    applies. Duplicate MO numbers across lines are reported too (fix CA-10 /
    adversarial-10); every row carries a line-qualified `mo_uid`.
    """
    out: list[dict] = []
    if frame is None or not len(frame):
        return out
    now_ts = pd.Timestamp(now) if now is not None else None
    for _, r in frame.iterrows():
        start_dt = r.get("start_dt")
        if start_dt is None or pd.isna(start_dt):
            continue
        item = _code(r.get("item"))
        made_raw = r.get("made_cas")
        made = _num(made_raw)
        fct = _num(r.get("fct_cas"))
        left = _num(r.get("left_cas"), default=fct)
        hours = _num(r.get("hours"))
        fct_kg = _num(r.get("fct_kg"))
        made_kg = _num(r.get("made_kg"))
        started = (not _is_blank(made_raw)) and made > 0
        complete = fct > 0 and (made >= fct or left <= 0)
        line = str(r.get("line", "")).strip().upper()
        mo = _code(r.get("mo"))
        future_started = False
        if item.upper() == "CIP":
            kind = "cip"
        elif complete:
            kind = "completed"
        elif started:
            if now_ts is not None and pd.Timestamp(start_dt) > now_ts:
                # data noise: cases posted against an MO that has not begun
                kind = QUEUED
                future_started = True
                if warnings is not None:
                    warnings.append(
                        f"{line}: MO {mo} shows {made:g} cases made but starts "
                        f"{pd.Timestamp(start_dt):%Y-%m-%d %H:%M}, in the "
                        "future — treated as QUEUED at its start (data noise; "
                        "the counter is ignored)")
            else:
                kind = RUNNING
        else:
            kind = QUEUED
        pct = round(made / fct * 100.0, 1) if fct > 0 else 0.0
        # the made kg column is blank on some exports while cases are posted;
        # derive it from the MO's own kg-per-case so actuals are never lost
        if made_kg <= 0 and made > 0 and fct > 0 and fct_kg > 0:
            made_kg = round(fct_kg * made / fct, 3)
        remaining_kg = (round(fct_kg * max(0.0, left) / fct, 3)
                        if fct > 0 else fct_kg)
        out.append({
            "line": line,
            "mo": mo,
            # line-qualified id: the same MO number can appear on two lines
            "mo_uid": f"{mo}@{line}",
            "item": item,
            "designation": str(r.get("designation", "")).strip(),
            "start_dt": pd.Timestamp(start_dt),
            "hours": hours,
            "fct_cas": fct,
            "made_cas": made,
            "left_cas": left,
            "qty_kg": fct_kg,           # the MO's FULL Fct kg (row semantics)
            # actuals — the coverage ledger nets ALREADY-MADE kg out of the
            # demand plan; a completed MO's truth is what it made, not Fct
            "made_kg": made_kg,
            "remaining_kg": remaining_kg,
            "completion_pct": pct,
            "kind": kind,
            "started": started,
            "future_started": future_started,
        })
    if warnings is not None:
        seen: dict[str, set[str]] = {}
        for row in out:
            seen.setdefault(row["mo"], set()).add(row["line"])
        for mo, lines in sorted(seen.items()):
            if len(lines) > 1 and mo:
                warnings.append(
                    f"MO {mo} appears on {len(lines)} lines "
                    f"({', '.join(sorted(lines))}) — kept as distinct rows "
                    f"({', '.join(f'{mo}@{ln}' for ln in sorted(lines))}); "
                    "check the manprg export")
    return out


def _estimated_end(row: dict, now: datetime) -> datetime:
    """Where a running MO actually finishes: pro-rata remaining work from now.

    A running MO whose nominal window already elapsed does NOT end in the past
    — that is the bug that makes the calendar look empty. Remaining hours are
    scaled by the share of cases still to make, and the end is clamped to now.
    """
    hours = row["hours"] if row["hours"] > 0 else 0.0
    nominal_end = row["start_dt"] + timedelta(hours=hours)
    frac_left = 1.0
    if row["fct_cas"] > 0:
        frac_left = max(0.0, 1.0 - (row["made_cas"] / row["fct_cas"]))
    remaining = hours * frac_left
    projected = pd.Timestamp(now) + timedelta(hours=remaining)
    end = max(pd.Timestamp(nominal_end), projected)
    if end <= pd.Timestamp(now):
        end = pd.Timestamp(now) + timedelta(hours=max(remaining, 0.25))
    return end.to_pydatetime()


# --------------------------------------------------------------------------
# running-MO end re-forecast from the ACTUAL observed rate (user-approved
# 2026-08-19): manprg's pro-rata estimate trusts the PLANNED rate, so a line
# running 20% slow "finishes" hours before it really will and the committed
# window lies to netting / fill gates / staging. cases_made / elapsed is the
# honest rate; every guard rail falls back to the manprg estimate, loudly.
#
# Audit C01 (2026-09-03): the counter is what manprg showed at its OBSERVATION
# time `as_of`, so elapsed = as_of - start (never render-clock - start: a
# 21 h old export read "73 % slow" and gated P21 for the whole horizon). The
# line is assumed to keep producing at the observed rate after as_of, so
# end = as_of + left / rate — identical whenever the page is rendered.
# --------------------------------------------------------------------------

REFORECAST_MIN_ELAPSED_H = 1.0    # under an hour the rate is mostly startup
REFORECAST_MIN_PCT = 2.0          # <2% done: the counter barely moved
REFORECAST_RATE_BAND = (0.25, 2.0)  # sane multiple of the catalog rate
REFORECAST_NOTE_MIN_DELTA_MIN = 30.0  # agree within 30 min -> no noise
# Beyond this content age the extrapolation is a guess: fall back to the
# manprg estimate and say how old the content is (C01 d).
REFORECAST_MAX_STALE_H = 8.0
# When the extrapolated end is already behind `now` (the MO should be done,
# manprg just has not said so) keep a minimal tail so the block stays visible.
REFORECAST_MIN_TAIL_H = 0.25
# Print the date in the tooltip when the two ends differ by more than a day.
REFORECAST_NOTE_DATE_DELTA_H = 24.0


def reforecast_enabled(cfg: dict | None) -> bool:
    """[scheduler] reforecast_running_mo_ends — default ON. False = legacy."""
    raw = ((cfg or {}).get("scheduler") or {}).get(
        "reforecast_running_mo_ends", True)
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _catalog_rate_cph(row: dict, caps: pd.DataFrame | None) -> float | None:
    """Catalog rate for the MO's line×SKU, in CASES/h (comparable to manprg).

    capabilities_rates.csv speaks kg/h; the MO's own Fct kg / Fct cases gives
    the kg-per-case to convert. None when the rate cannot be resolved — the
    sanity band is then unverifiable and the caller must fall back.
    """
    if caps is None or not len(caps):
        return None
    if row["fct_cas"] <= 0 or row["qty_kg"] <= 0:
        return None
    kg_per_cas = row["qty_kg"] / row["fct_cas"]
    hit = caps[(caps["line_name"].astype(str) == row["line"])
               & (caps["sku"].astype(str) == str(row["item"]))]
    if not len(hit):
        return None
    kgph = pd.to_numeric(pd.Series([hit.iloc[0].get("calc_rate_kgph")]),
                         errors="coerce").iloc[0]
    if pd.isna(kgph) or kgph <= 0:
        return None
    return float(kgph) / kg_per_cas


@dataclass
class Reforecast:
    """Result of the running-MO re-forecast (see `_reforecast`)."""

    end: datetime | None = None   # None = unusable (see `why`)
    why: str = ""                 # reason when unusable; caution otherwise
    actual_cph: float = 0.0       # observed cases/h (made / elapsed)
    used_cph: float = 0.0         # rate after the band clamp
    elapsed_h: float = 0.0        # start -> as_of
    age_h: float = 0.0            # as_of -> now (content age)
    clamped: bool = False         # actual rate was under the band floor
    stale: bool = False           # age_h > REFORECAST_MAX_STALE_H


def _reforecast(row: dict, now: datetime, catalog_cph: float | None,
                as_of: datetime | None = None) -> Reforecast:
    """Re-forecast a running MO's end from the rate observed up to `as_of`.

    elapsed = as_of - start; actual = made / elapsed; the line keeps producing
    at that rate after the observation, so end = as_of + left / rate (clamped
    to a minimal tail after `now`). `as_of` defaults to `now` (legacy: the
    export is assumed fresh). A rate under the band floor is CLAMPED to the
    floor (fix CA-2 / audit C02 — the old cliff swapped a 13-day commitment
    for a 3-day one between pace 0.26 and 0.24); a rate over the ceiling is
    a counter jump and stays unusable. `stale` is set when the content is
    older than REFORECAST_MAX_STALE_H; the caller decides to fall back.
    """
    rf = Reforecast()
    started = row.get("start_dt")
    if started is None or pd.isna(started):
        rf.why = "no usable start timestamp"
        return rf
    started = pd.Timestamp(started)
    now_ts = pd.Timestamp(now)
    obs = pd.Timestamp(as_of) if as_of is not None else now_ts
    if obs > now_ts:
        obs = now_ts   # a stamp ahead of the clock is skew; never negative age
    rf.age_h = (now_ts - obs).total_seconds() / 3600.0
    rf.stale = rf.age_h > REFORECAST_MAX_STALE_H
    if started >= obs:
        rf.why = (f"start {started:%Y-%m-%d %H:%M} is not before the manprg "
                  f"observation time {obs:%Y-%m-%d %H:%M}")
        return rf
    rf.elapsed_h = (obs - started).total_seconds() / 3600.0
    if rf.elapsed_h < REFORECAST_MIN_ELAPSED_H:
        rf.why = (f"only {rf.elapsed_h:.1f}h elapsed at observation "
                  f"(<{REFORECAST_MIN_ELAPSED_H:g}h)")
        return rf
    if row["completion_pct"] < REFORECAST_MIN_PCT:
        rf.why = (f"only {row['completion_pct']:.1f}% complete "
                  f"(<{REFORECAST_MIN_PCT:g}%)")
        return rf
    if row["made_cas"] <= 0 or row["left_cas"] < 0:
        rf.why = "no usable case counts"
        return rf
    rf.actual_cph = row["made_cas"] / rf.elapsed_h
    if catalog_cph is None:
        rf.why = "no catalog rate to sanity-check the actual rate"
        return rf
    lo, hi = REFORECAST_RATE_BAND
    if rf.actual_cph > hi * catalog_cph:
        rf.why = (f"actual rate {rf.actual_cph:.1f} cas/h outside "
                  f"{lo:g}x-{hi:g}x of catalog {catalog_cph:.1f} cas/h")
        return rf
    rf.used_cph = rf.actual_cph
    if rf.actual_cph < lo * catalog_cph:
        rf.used_cph = lo * catalog_cph
        rf.clamped = True
        rf.why = (f"rate clamped: actual {rf.actual_cph:.1f} cas/h is under "
                  f"{lo:g}x of catalog {catalog_cph:.1f} cas/h — using "
                  f"{rf.used_cph:.1f} cas/h")
    # production continues at the observed rate since the observation:
    # end = now + max(0, left - rate*(now-as_of)) / rate == as_of + left/rate
    end = obs + timedelta(hours=row["left_cas"] / rf.used_cph)
    floor = now_ts + timedelta(hours=REFORECAST_MIN_TAIL_H)
    if end < floor:
        end = floor
        rf.why = (rf.why + "; " if rf.why else "") + (
            "extrapolated end is already behind now — the MO should be "
            "complete; awaiting the next manprg pull")
    rf.end = end.to_pydatetime()
    return rf


def _reforecast_end(row: dict, now: datetime,
                    catalog_cph: float | None,
                    as_of: datetime | None = None,
                    ) -> tuple[datetime | None, str]:
    """(re-forecast end, note) from the actual rate, or (None, why-not).

    Backward-compatible wrapper around `_reforecast`: a stale observation
    (content older than REFORECAST_MAX_STALE_H) is reported as unusable so
    the caller keeps manprg's estimate. When an end IS returned the note is
    empty or a caution (rate clamped / MO should already be complete).
    """
    rf = _reforecast(row, now, catalog_cph, as_of)
    if rf.end is None:
        return None, rf.why
    if rf.stale:
        return None, (f"manprg content is {rf.age_h:.1f}h old "
                      f"(>{REFORECAST_MAX_STALE_H:g}h) — extrapolating the "
                      "observed rate that far would be a guess")
    return rf.end, rf.why


def _reforecast_note(row: dict, now: datetime, end: datetime,
                     manprg_end: datetime, rf: Reforecast | None = None) -> str:
    """Honest tooltip line when re-forecast and manprg disagree by >30 min.

    "ends Thu 03:40 re-forecast from actual rate (manprg said Thu 01:10 — at
    42% of planned rate (2.4x longer))". Empty when they agree — no noise.
    Audit C07: the old "(1-pace)% slow" read like a time stretch; the number a
    planner acts on is the time factor 1/pace, so print both. The date is
    included when the two ends differ by more than a day.
    """
    delta_min = (pd.Timestamp(end)
                 - pd.Timestamp(manprg_end)).total_seconds() / 60.0
    if abs(delta_min) <= REFORECAST_NOTE_MIN_DELTA_MIN:
        return ""
    pace_txt = ""
    plan_cph = row["fct_cas"] / row["hours"] if row["hours"] > 0 else 0.0
    if rf is not None and rf.used_cph > 0:
        used = rf.used_cph
    else:
        elapsed_h = (pd.Timestamp(now)
                     - pd.Timestamp(row["start_dt"])).total_seconds() / 3600.0
        used = row["made_cas"] / elapsed_h if elapsed_h > 0 else 0.0
    if used > 0 and plan_cph > 0:
        pace = used / plan_cph
        if pace < 1.0:
            pace_txt = (f" — at {round(pace * 100)}% of planned rate "
                        f"({1.0 / pace:.1f}x longer)")
        elif pace > 1.0:
            pace_txt = (f" — at {round(pace * 100)}% of planned rate "
                        f"({pace:.1f}x faster)")
        if rf is not None and rf.clamped:
            pace_txt += " [rate clamped]"
    fmt = ("%a %m-%d %H:%M" if abs(delta_min) > REFORECAST_NOTE_DATE_DELTA_H * 60
           else "%a %H:%M")
    return (f"ends {pd.Timestamp(end):{fmt}} re-forecast from actual rate "
            f"(manprg said {pd.Timestamp(manprg_end):{fmt}}{pace_txt})")


# --------------------------------------------------------------------------
# CIP projection
# --------------------------------------------------------------------------

def _iso_monday(dt: datetime) -> datetime:
    d = pd.Timestamp(dt).normalize()
    return (d - timedelta(days=d.weekday())).to_pydatetime()


def project_cips(line: str, info, hz: Horizon, dur: float,
                 interval: float, *,
                 committed: Iterable[datetime] | None = None,
                 now: datetime | None = None,
                 carry_h: float | None = None,
                 carry_anchor: datetime | None = None,
                 fallback_phase: datetime | None = None,
                 warnings: list[str] | None = None,
                 ) -> list[tuple[datetime, str]]:
    """Scheduled CIP + CIPs spaced at MaxHoursBetweenCIP across the horizon.

    `ScheduledCIP` is not maintained after the CIP happens, so it can sit in the
    past or even before `PreviousCIP` (seen on P15: scheduled 08-05 23:00 vs
    previous 08-06 17:03). A stale one is ignored rather than drawn behind the
    anchor — and it must not seed the projection either, or every later CIP
    inherits the wrong phase.

    `committed` (fix CA-5 / audit C10): the plant's own CIP rows in manprg.
    They are drawn by the caller (at their own duration); here they phase the
    grid and REPLACE cip_info's ScheduledCIP (a disagreement >1 h is warned).

    Phase when nothing is known (fix CA-8 / audits C12, C91, adversarial-6):
    `carry_h` hours since the last clean at `carry_anchor` (initial_states
    carryover), else `fallback_phase` (a stable committed production start),
    else the anchor's ISO Monday — never today's midnight, which slid the grid
    24 h per run day. A line already past its interval at `now` with no clean
    committed gets a catch-up clean AT NOW plus a warning.
    """
    out: list[tuple[datetime, str]] = []
    ref_now = pd.Timestamp(now if now is not None else hz.now).to_pydatetime()
    committed_ts = sorted(pd.Timestamp(c).to_pydatetime()
                          for c in (committed or []) if c is not None
                          and not pd.isna(pd.Timestamp(c)))
    known: list[datetime] = list(committed_ts)
    prev: datetime | None = None
    if info is not None:
        if info.previous_cip is not None:
            prev = pd.Timestamp(info.previous_cip).to_pydatetime()
            known.append(prev)
        if info.scheduled_cip is not None:
            sched = pd.Timestamp(info.scheduled_cip).to_pydatetime()
            stale = sched < hz.anchor or (prev is not None and sched < prev)
            if not stale:
                if committed_ts:
                    # the plant's manprg CIP row is the committed clean; the
                    # cip_info date is informational — say so when they differ
                    near = min(abs((sched - c).total_seconds()) for c in committed_ts)
                    if near > 3600 and warnings is not None:
                        warnings.append(
                            f"{line}: cip_info ScheduledCIP "
                            f"{sched:%Y-%m-%d %H:%M} disagrees with the manprg "
                            "CIP row(s) at "
                            + ", ".join(f"{c:%Y-%m-%d %H:%M}" for c in committed_ts)
                            + " — manprg drawn")
                else:
                    out.append((sched, CIP_SCHEDULED))
                    known.append(sched)
    if interval <= 0:
        return out
    last: datetime | None = max(known) if known else None
    if last is None:
        if carry_h is not None and carry_h >= 0:
            base = pd.Timestamp(carry_anchor or hz.anchor).to_pydatetime()
            last = base - timedelta(hours=float(carry_h))
            source = f"initial_states carryover {carry_h:g} h before {base:%Y-%m-%d %H:%M}"
        elif fallback_phase is not None:
            last = pd.Timestamp(fallback_phase).to_pydatetime()
            source = f"first committed production start {last:%Y-%m-%d %H:%M}"
        else:
            last = _iso_monday(hz.anchor)
            source = f"the anchor week's Monday {last:%Y-%m-%d}"
        if warnings is not None:
            warnings.append(
                f"{line}: no PreviousCIP/ScheduledCIP in cip_info and no CIP "
                f"row in manprg — projected CIP grid phased from {source}; "
                "add PreviousCIP to cip_info")
        known.append(last)
    # Overdue check (adversarial-6): the last clean that HAPPENED vs now.
    past = [k for k in known if k <= ref_now]
    future = [k for k in known if k > ref_now]
    if past:
        last_past = max(past)
        since_h = (ref_now - last_past).total_seconds() / 3600.0
        if not future and since_h > interval:
            first = max(ref_now, hz.anchor)
            if warnings is not None:
                warnings.append(
                    f"{line}: CIP overdue by {since_h - interval:.0f} h (last "
                    f"clean {last_past:%Y-%m-%d %H:%M}, max {interval:g} h "
                    "between cleans) — catch-up clean drawn at now")
            if first < hz.end:
                out.append((first, CIP_PROJECTED))
            last = first
        elif future:
            nxt_known = min(future)
            gap_h = (nxt_known - last_past).total_seconds() / 3600.0
            if gap_h > interval and warnings is not None:
                warnings.append(
                    f"{line}: next committed clean {nxt_known:%Y-%m-%d %H:%M} "
                    f"is {gap_h - interval:.0f} h past the {interval:g} h "
                    f"interval since the last clean {last_past:%Y-%m-%d %H:%M}")
    nxt = last + timedelta(hours=interval)
    guard = 0
    taken = [s for s, _ in out] + committed_ts
    while nxt < hz.end and guard < 64:
        guard += 1
        if all(abs((nxt - s).total_seconds()) > 3600 for s in taken):
            out.append((nxt, CIP_PROJECTED))
            taken.append(nxt)
        nxt = nxt + timedelta(hours=interval)
    return out


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

def build_current_state(
    hz: Horizon,
    *,
    manprg_paths: Iterable[str | Path] | None = None,
    cip_path: str | Path | None = None,
    manprg: ManprgResult | None = None,
    cips: CipInfoResult | None = None,
    lines: pd.DataFrame | None = None,
    cfg: dict | None = None,
    now: datetime | None = None,
    caps: pd.DataFrame | None = None,
    caps_path: str | Path | None = None,
    as_of: datetime | None = None,
    initial_states: pd.DataFrame | None = None,
) -> CurrentState:
    """Ground-truth calendar for the horizon, in anchor hours.

    Either pass already-parsed `manprg` / `cips` results (tests, reuse from the
    page) or the file paths to read them from. `caps` / `caps_path`
    (capabilities_rates.csv) sanity-check the running-MO re-forecast rate;
    without them every re-forecast falls back to the manprg estimate.

    `now` is the render clock (default hz.now); `as_of` is the manprg CONTENT
    observation time (default: the ManprgResult's stamp/mtime, else `now`).
    Board, netting and staging should pass the SAME pair so they agree (audit
    C03). `initial_states` (reference/initial_states.csv) supplies the CIP
    carryover used to phase the grid of a line without any CIP history.
    """
    n = pd.Timestamp(now or hz.now).to_pydatetime()
    if manprg is None:
        manprg = read_manprg(list(manprg_paths or []))
    if cips is None:
        cips = read_cip_info(cip_path) if cip_path else CipInfoResult()

    state = CurrentState(blocks=pd.DataFrame(columns=CALENDAR_COLUMNS))
    state.warnings.extend(manprg.warnings)
    state.warnings.extend(cips.warnings)

    # one observation clock for every running MO (C01/C03)
    obs_src = "arg"
    obs = as_of
    if obs is None:
        obs = getattr(manprg, "as_of", None)
        obs_src = getattr(manprg, "as_of_source", "") or ("manprg" if obs is not None else "")
    if obs is None or pd.isna(pd.Timestamp(obs)):
        obs, obs_src = n, "now"
    obs = pd.Timestamp(obs).to_pydatetime()
    if obs > n:
        state.warnings.append(
            f"manprg as-of {obs:%Y-%m-%d %H:%M} is ahead of now "
            f"{n:%Y-%m-%d %H:%M} (clock skew?) — using now as the observation "
            "time")
        obs = n
    state.now, state.as_of, state.as_of_source = n, obs, obs_src

    if caps is None and caps_path and Path(caps_path).exists():
        from helpers.capability_check import load_capabilities
        try:
            caps = load_capabilities(caps_path)
        except Exception as exc:  # noqa: BLE001 — degrade to fallback, loudly
            state.warnings.append(
                f"capabilities_rates unreadable ({exc}) — running-MO ends "
                "keep the manprg estimate")
            caps = None
    do_reforecast = reforecast_enabled(cfg)

    rows = classify_rows(manprg.frame, now=n, warnings=state.warnings)
    dur = cip_hours(cfg)
    blocks: list[dict] = []

    by_line: dict[str, list[dict]] = {}
    for r in rows:
        by_line.setdefault(r["line"], []).append(r)

    # unknown line names (adversarial-11): report and skip, never invent
    has_table = lines is not None and len(lines) > 0 and "line_name" in lines.columns
    unknown_lines: set[str] = set()
    if has_table:
        # Dropped rows are the most severe data problem this builder reports,
        # so these go FIRST in state.warnings (consumers such as the staging
        # overlay forward only the first few). The wording matches the note
        # scenario_runner used to emit for the same case (fix adversarial-11
        # now stops the rows here, before they can reach it).
        unk_warn: list[str] = []
        for line in sorted(by_line):
            if line_id_for(line, lines) < 0:
                unknown_lines.add(line)
                unk_warn.append(
                    f"unknown manprg line(s) skipped (not in lines.csv): {line} "
                    f"— {len(by_line[line])} manprg row(s) dropped")
        for line in sorted(cips.by_line):
            ln = str(line).strip().upper()
            if ln not in by_line and line_id_for(ln, lines) < 0:
                unknown_lines.add(ln)
                unk_warn.append(
                    f"unknown cip_info line skipped (not in lines.csv): {ln}")
        state.warnings[0:0] = unk_warn

    for line in sorted(by_line):
        if line in unknown_lines:
            continue
        lid = line_id_for(line, lines)
        line_rows = sorted(by_line[line], key=lambda r: r["start_dt"])
        cursor = pd.Timestamp(n)

        # 1. the running MO — locked, runs to its re-forecast (or estimated) end
        running = [r for r in line_rows if r["kind"] == RUNNING]
        if len(running) > 1:
            # only the latest start can really be running; the rest are stale
            running.sort(key=lambda r: r["start_dt"])
            for stale in running[:-1]:
                state.warnings.append(
                    f"{line}: MO {stale['mo']} started but superseded by "
                    f"{running[-1]['mo']} — treated as completed "
                    f"({stale['left_cas']:g} cases / "
                    f"{stale['remaining_kg']:,.0f} kg remaining)")
                stale["kind"] = "completed"
                stale["superseded_by"] = running[-1]["mo"]
            running = running[-1:]
        for r in running:
            manprg_end = _estimated_end(r, n)
            end, rf_note = manprg_end, ""
            rf = None
            if do_reforecast:
                rf = _reforecast(r, n, _catalog_rate_cph(r, caps), obs)
                if rf.end is None:
                    state.warnings.append(
                        f"{line}: MO {r['mo']} kept at manprg's estimated "
                        f"end — re-forecast unavailable ({rf.why})")
                    rf = None
                elif rf.stale:
                    state.warnings.append(
                        f"{line}: MO {r['mo']} kept at manprg's estimated end "
                        f"— manprg content is {rf.age_h:.1f}h old "
                        f"(>{REFORECAST_MAX_STALE_H:g}h; observed "
                        f"{obs:%Y-%m-%d %H:%M}); the observed rate cannot be "
                        "extrapolated that far")
                    rf = None
                else:
                    end = rf.end
                    if rf.why:
                        state.warnings.append(f"{line}: MO {r['mo']} {rf.why}")
                    rf_note = _reforecast_note(r, n, end, manprg_end, rf)
            # A long-running MO can have started days before the anchor. Keep
            # the block INSIDE the window (a start_h of -191 renders nowhere on
            # a rolling Gantt) but record the true start so nothing is lost.
            true_start = pd.Timestamp(r["start_dt"])
            shown_start = max(true_start, pd.Timestamp(hz.anchor))
            blk = _block(r, line, lid, shown_start, end, RUNNING,
                         locked=True, anchor=hz.anchor)
            # The block is the REMAINING work (C04): its kg is what is still
            # to be made; the made share goes to `completed` as actuals.
            if not _is_trial_row(r):
                blk["qty_kg"] = r["remaining_kg"]
            blk["attrs"] += (f";started={true_start:%Y-%m-%dT%H:%M}"
                             f";fct_kg={r['qty_kg']:.0f};made_kg={r['made_kg']:.0f}")
            if shown_start > true_start:
                blk["attrs"] += ";clamped_to_anchor"
            if rf is not None and rf.clamped:
                blk["attrs"] += ";rate_clamped"
            if rf_note:
                # attrs carries the honest delta; sku_description is the field
                # the Gantt tooltip already renders, so the line rides along
                # with no frontend change.
                blk["attrs"] += f";reforecast={rf_note}"
                blk["sku_description"] = (
                    f"{blk['sku_description']} · {rf_note}"
                    if blk["sku_description"] else rf_note)
            blocks.append(blk)
            state.running.append({**r, "est_end": end,
                                  "manprg_end": manprg_end,
                                  "shown_start": shown_start.to_pydatetime(),
                                  "actual_cph": rf.actual_cph if rf else None,
                                  "used_cph": rf.used_cph if rf else None})
            if r["made_kg"] > 0 and not _is_trial_row(r):
                # actuals: what was made between the true start and the
                # observation, keyed by the ledger to the week it was produced
                made_h = max(0.0, _hours(obs, true_start))
                state.completed.append({
                    **r, "kind": MADE_PART, "start_dt": true_start,
                    "end_dt": pd.Timestamp(obs), "hours": made_h,
                    "made_kg": r["made_kg"], "remaining_kg": r["remaining_kg"],
                    "of_running_mo": r["mo"],
                })
            cursor = max(cursor, pd.Timestamp(end))
            # Running-only free hour (queued MOs are solver orders now)
            state.line_running_free_h[line] = round(
                _hours(max(pd.Timestamp(end), pd.Timestamp(n)), hz.anchor), 3)
        if not running:
            state.line_running_free_h[line] = round(
                _hours(max(pd.Timestamp(n), pd.Timestamp(hz.anchor)),
                       hz.anchor), 3)

        # 2. queued MOs (and TRIALS reservations) — sequential, no overlap,
        #    keep manprg's intended order
        for r in line_rows:
            if r["kind"] != QUEUED:
                continue
            start = max(pd.Timestamp(r["start_dt"]), cursor)
            hours = r["hours"] if r["hours"] > 0 else 1.0
            end = start + timedelta(hours=hours)
            blk = _block(r, line, lid, start, end, QUEUED,
                         locked=False, anchor=hz.anchor)
            if r.get("future_started"):
                blk["attrs"] += ";future_start_noise"
            blocks.append(blk)
            state.queued.append({**r, "placed_start": start, "placed_end": end})
            cursor = end

        # Completed MOs (made >= fct, or superseded per the latest-start rule
        # — the same rn=1 logic as the planner's SQL) are counted but NOT
        # drawn (user decision 2026-08-14, revised same day: keep the board
        # forward-looking). A superseded MO keeps its remaining cases/kg on
        # the row (`left_cas`, `remaining_kg`) so nothing vanishes silently.
        state.completed.extend([r for r in line_rows if r["kind"] == "completed"])
        state.line_free_h[line] = round(_hours(cursor, hz.anchor), 3)

    # 3. CIP — the plant's manprg CIP rows (committed, own duration) win;
    #    cip_info supplies PreviousCIP / interval / a fallback ScheduledCIP,
    #    and the grid is projected from the LAST KNOWN clean.
    carry_by_line = _carryover_by_line(initial_states)
    cip_lines = (set(by_line) | {str(k).strip().upper() for k in cips.by_line}) - unknown_lines
    for line in sorted(cip_lines):
        lid = line_id_for(line, lines)
        info = cips.by_line.get(line)
        interval = cip_interval_for(line, cips, cfg)
        if info is not None and info.scheduled_cip is not None:
            sched = pd.Timestamp(info.scheduled_cip).to_pydatetime()
            if sched < hz.anchor or (info.previous_cip is not None
                                     and sched < pd.Timestamp(info.previous_cip)):
                state.warnings.append(
                    f"{line}: ScheduledCIP {sched:%Y-%m-%d %H:%M} is stale "
                    "(before the anchor or before PreviousCIP) — ignored")
        note = (info.notes if info and info.notes
                and info.notes.upper() != "NULL" else "")
        # 3a. manprg CIP pseudo-MOs: committed cleans with their own Hours
        committed_starts: list[datetime] = []
        for r in by_line.get(line, []):
            if r["kind"] != "cip":
                continue
            when = pd.Timestamp(r["start_dt"]).to_pydatetime()
            committed_starts.append(when)
            row_dur = r["hours"] if r["hours"] > 0 else dur
            end = when + timedelta(hours=row_dur)
            if when >= hz.end or end <= hz.anchor:
                continue   # history, or beyond the window
            end = min(end, hz.end)
            blocks.append({
                "block_id": _bid("cip", line, when),
                "block_type": "cip",
                "line_id": lid,
                "line_name": line,
                "start_h": round(_hours(when, hz.anchor), 3),
                "end_h": round(_hours(end, hz.anchor), 3),
                "label": "CIP",
                "order_id": "",
                "sku": "",
                "sku_description": note,
                "qty_kg": 0.0,
                "locked": True,
                "attrs": f"current_state:{CIP_SCHEDULED};source=manprg;mo={r['mo']}",
            })
            state.cips.append({"line": line, "start": when, "kind": CIP_SCHEDULED,
                               "interval_h": interval, "notes": note,
                               "duration_h": row_dur, "source": "manprg",
                               "mo": r["mo"]})
        # 3b. cip_info scheduled (only without a manprg CIP row) + grid
        fallback_phase = None
        prod_rows = [r for r in by_line.get(line, [])
                     if r["kind"] in (RUNNING, QUEUED)]
        if prod_rows:
            fallback_phase = min(pd.Timestamp(r["start_dt"]) for r in prod_rows)
        for when, kind in project_cips(
                line, info, hz, dur, interval, committed=committed_starts,
                now=n, carry_h=carry_by_line.get(line),
                carry_anchor=getattr(hz, "config_anchor", None) or hz.anchor,
                fallback_phase=fallback_phase, warnings=state.warnings):
            # Clip to the horizon: a CIP starting at/after the end is not
            # drawable, and one straddling the end must not emit end_h >
            # hz.end_h (walkthrough finding 2026-08-17: two cip_projected
            # blocks ended at h509 on a 504h horizon).
            if when >= hz.end:
                continue
            end = min(when + timedelta(hours=dur), hz.end)
            blocks.append({
                "block_id": _bid("cip", line, when),
                "block_type": "cip",
                "line_id": lid,
                "line_name": line,
                "start_h": round(_hours(when, hz.anchor), 3),
                "end_h": round(_hours(end, hz.anchor), 3),
                "label": "CIP" if kind == CIP_SCHEDULED else "CIP (projected)",
                "order_id": "",
                "sku": "",
                "sku_description": note,
                "qty_kg": 0.0,
                "locked": kind == CIP_SCHEDULED,
                "attrs": f"current_state:{kind}",
            })
            state.cips.append({"line": line, "start": when, "kind": kind,
                               "interval_h": interval, "notes": note,
                               "duration_h": dur, "source": "cip_info"})

    df = pd.DataFrame(blocks, columns=CALENDAR_COLUMNS) if blocks else \
        pd.DataFrame(columns=CALENDAR_COLUMNS)
    if len(df):
        # keep only what intersects the horizon; the past is already history
        df = df[(df["end_h"] > 0.0) & (df["start_h"] < hz.end_h)].copy()

    # ── CIP/production merge: split production around CIP windows ─────────
    # Production, trial and CIP blocks were collected independently above;
    # the CIP projection must not overlap real MOs. CIPs win (never split);
    # a production MO spanning a CIP is split, its remainder PUSHED after the
    # clean (the MO keeps its hours — audit C09: the old merge deleted 139 h
    # of committed work on the snapshot), and the queue behind it shifts.
    if len(df):
        merged: list[dict] = []
        for line in sorted(set(df["line_name"])):
            line_df = df[df["line_name"] == line]
            prods = [dict(r) for _, r in line_df[
                line_df["block_type"].isin(["production", "trial"])].iterrows()]
            cips_ = [dict(r) for _, r in line_df[line_df["block_type"] == "cip"].iterrows()]
            clipped, kept_cips = _clip_prod_around_cips(
                prods, cips_, warnings=state.warnings, line=line)
            merged.extend(clipped)
            merged.extend(kept_cips)
        df = pd.DataFrame(merged, columns=CALENDAR_COLUMNS) if merged else \
            pd.DataFrame(columns=CALENDAR_COLUMNS)
        df = df.sort_values(["line_name", "start_h"]).reset_index(drop=True)
        _sync_after_merge(state, df, hz)
    state.blocks = df
    return state


def _is_trial_row(r: dict) -> bool:
    return str(r.get("item", "")).strip().upper() == TRIAL_ITEM


def _carryover_by_line(initial_states: pd.DataFrame | None) -> dict[str, float]:
    """line -> carryover_run_hours_since_last_cip_at_t0 from initial_states."""
    out: dict[str, float] = {}
    if initial_states is None or not len(initial_states):
        return out
    col = "carryover_run_hours_since_last_cip_at_t0"
    if col not in initial_states.columns or "line_name" not in initial_states.columns:
        return out
    for _, r in initial_states.iterrows():
        v = pd.to_numeric(pd.Series([r.get(col)]), errors="coerce").iloc[0]
        if pd.isna(v) or float(v) < 0:
            continue
        out[str(r.get("line_name", "")).strip().upper()] = float(v)
    return out


def _sync_after_merge(state: CurrentState, df: pd.DataFrame, hz: Horizon) -> None:
    """Pushed pieces move the queue: refresh free hours and the row dicts.

    line_free_h / line_running_free_h and the running/queued dicts were
    computed BEFORE the CIP merge (C09); after pushing, the truth is the
    merged pieces. Values only ever move LATER.
    """
    if not len(df):
        return
    work = df[df["block_type"].isin(["production", "trial"])]
    if not len(work):
        return
    for line, grp in work.groupby("line_name"):
        line = str(line)
        end_all = float(grp["end_h"].max())
        if end_all > state.line_free_h.get(line, float("-inf")):
            state.line_free_h[line] = round(end_all, 3)
        run = grp[grp["attrs"].astype(str).str.contains(
            f"current_state:{RUNNING}", regex=False)]
        if len(run):
            end_run = float(run["end_h"].max())
            if end_run > state.line_running_free_h.get(line, float("-inf")):
                state.line_running_free_h[line] = round(end_run, 3)
    span: dict[tuple[str, str, str], tuple[float, float]] = {}
    for _, b in work.iterrows():
        attrs = str(b.get("attrs") or "")
        kind = RUNNING if f"current_state:{RUNNING}" in attrs else QUEUED
        key = (str(b["line_name"]), str(b["order_id"]), kind)
        s, e = float(b["start_h"]), float(b["end_h"])
        lo, hi = span.get(key, (s, e))
        span[key] = (min(lo, s), max(hi, e))
    anchor = pd.Timestamp(hz.anchor)
    for r in state.running:
        sp = span.get((r["line"], r["mo"], RUNNING))
        if sp is None:
            continue
        new_end = anchor + timedelta(hours=sp[1])
        if new_end > pd.Timestamp(r["est_end"]):
            r["pushed_by_cip_h"] = round(
                (new_end - pd.Timestamp(r["est_end"])).total_seconds() / 3600.0, 3)
            r["est_end"] = new_end.to_pydatetime()
    for r in state.queued:
        sp = span.get((r["line"], r["mo"], QUEUED))
        if sp is None:
            continue
        new_start = anchor + timedelta(hours=sp[0])
        new_end = anchor + timedelta(hours=sp[1])
        if new_end > pd.Timestamp(r["placed_end"]) or new_start > pd.Timestamp(r["placed_start"]):
            r["pushed_by_cip_h"] = round(
                (new_end - pd.Timestamp(r["placed_end"])).total_seconds() / 3600.0, 3)
            r["placed_start"] = new_start
            r["placed_end"] = new_end


def _block(r: dict, line: str, lid: int, start, end, kind: str,
           *, locked: bool, anchor: datetime) -> dict:
    label = r["item"] or r["mo"]
    # manprg TRIALS pseudo-MOs ARE the plant's trial reservations (user
    # 2026-08-14: "the trials will show up on those files" — trials.csv is
    # not a source). A trial is blocked production time, never tonnage.
    is_trial = _is_trial_row(r)
    return {
        "block_id": _bid("mo", line, r["mo"], start),
        "block_type": "trial" if is_trial else "production",
        "line_id": lid,
        "line_name": line,
        "start_h": round(_hours(start, anchor), 3),
        "end_h": round(_hours(end, anchor), 3),
        "label": "TRIAL" if is_trial else label,
        "order_id": r["mo"],
        "sku": r["item"],
        "sku_description": r["designation"],
        "qty_kg": None if is_trial else r["qty_kg"],
        "locked": locked,
        "attrs": f"current_state:{kind};pct={r['completion_pct']}",
    }


def _is_running_block(b: dict) -> bool:
    return f"current_state:{RUNNING}" in str(b.get("attrs") or "")


def _clip_prod_around_cips(
    blocks: list[dict],
    cip_blocks: list[dict],
    *,
    warnings: list[str],
    line: str,
) -> tuple[list[dict], list[dict]]:
    """Split production blocks around CIP windows so nothing overlaps.

    CIP windows are non-negotiable (never split, never moved here). A
    production block that spans a CIP is split into pieces and the work
    after the clean is PUSHED by the clean's duration — the MO keeps every
    hour it needs (fix CA-4 / audit C09: the old merge subtracted the CIP
    from the MO and dropped a tail the CIP overlapped). Blocks are processed
    in start order and a pushed block pushes everything queued behind it
    (start = max(own start, previous piece end)). A block shaped by a CIP —
    split, or merely pushed because a CIP overlapped its start — carries the
    'split' marker; a pushed one also 'pushed=<h>'.

    The one exception: a RUNNING block (in progress now) whose START a CIP
    window overlaps cannot be pushed — the plant is producing — so that CIP
    window is dropped with a warning (a stale/misplaced clean), as before.

    Returns (clipped_production_blocks, kept_cip_blocks).
    """
    ordered = sorted(blocks, key=lambda b: (float(b["start_h"]), float(b["end_h"])))
    if not cip_blocks:
        return ordered, list(cip_blocks)
    cips = sorted(
        (dict(c) for c in cip_blocks),
        key=lambda c: (float(c["start_h"]), float(c["end_h"])),
    )
    dropped_cip_ids = set()
    out: list[dict] = []
    chain_cursor: float | None = None   # end of the previous block's last piece
    for b in ordered:
        s, e = float(b["start_h"]), float(b["end_h"])
        need = e - s                    # hours this block must keep
        orig_end = e
        if chain_cursor is not None and s < chain_cursor and not _is_running_block(b):
            s = chain_cursor            # the queue shifted behind a pushed block
        cursor = s
        remaining = need
        pieces: list[dict] = []
        shaped = False
        for c in cips:
            if c.get("block_id") in dropped_cip_ids:
                continue
            cs_, ce_ = float(c["start_h"]), float(c["end_h"])
            if ce_ <= cursor or cs_ >= cursor + remaining:
                continue  # CIP before/after the remaining work
            if cs_ <= cursor:
                if _is_running_block(b) and not pieces:
                    # a clean cannot start inside an MO that is running now
                    warnings.append(
                        f"{line}: CIP {c.get('label', 'CIP')} "
                        f"({cs_:.0f}h–{ce_:.0f}h) overlaps the start of running "
                        f"MO {b.get('order_id')} — keeping the MO, dropping "
                        "the CIP window")
                    dropped_cip_ids.add(c.get("block_id"))
                    continue
                cursor = ce_             # overlapped start: pushed, no piece
                shaped = True
                continue
            piece = dict(b)
            piece["start_h"] = round(cursor, 3)
            piece["end_h"] = round(cs_, 3)
            pieces.append(piece)
            remaining -= cs_ - cursor
            cursor = ce_
            shaped = True
        if remaining > 1e-9:
            piece = dict(b)
            piece["start_h"] = round(cursor, 3)
            piece["end_h"] = round(cursor + remaining, 3)
            pieces.append(piece)
        if pieces and float(pieces[-1]["end_h"]) > orig_end + 1e-9:
            pushed = float(pieces[-1]["end_h"]) - orig_end
            shaped = True
        else:
            pushed = 0.0
        if shaped:
            for p in pieces:
                tok = (p.get("attrs", "") + ";split").strip(";")
                if pushed > 1e-9:
                    tok += f";pushed={pushed:g}"
                p["attrs"] = tok
        # Pro-rate the MO's tonnage across its split pieces by duration.
        # dict(b) used to copy the FULL MO qty into every fragment, so one
        # MO split around a CIP counted twice in every adherence/coverage
        # number downstream (measured 2026-08-14: 17 split MOs, 1,535t of
        # phantom production; one SKU showed 1764% adherence).
        qty = b.get("qty_kg")
        if len(pieces) > 1 and isinstance(qty, (int, float)) and qty and not pd.isna(qty):
            total_h = sum(
                float(p["end_h"]) - float(p["start_h"]) for p in pieces)
            if total_h > 0:
                for p in pieces:
                    frac = (float(p["end_h"]) - float(p["start_h"])) / total_h
                    p["qty_kg"] = round(qty * frac, 2)
                # keep the exact MO total: dump rounding drift on the largest
                drift = round(qty - sum(p["qty_kg"] for p in pieces), 2)
                if drift:
                    big = max(pieces,
                              key=lambda p: float(p["end_h"]) - float(p["start_h"]))
                    big["qty_kg"] = round(big["qty_kg"] + drift, 2)
        out.extend(pieces)
        if pieces:
            chain_cursor = float(pieces[-1]["end_h"])
    kept = [c for c in cips if c.get("block_id") not in dropped_cip_ids]
    return out, kept
