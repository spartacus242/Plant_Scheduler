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
#   * Qty made >= Fct qty (or Left<=0)  -> COMPLETED, dropped (don't render).
#   * Qty made > 0, not complete        -> RUNNING. Locked; the solver may not
#                                          move it. It runs to its ESTIMATED
#                                          end: pro-rata remaining work from
#                                          now, never earlier than now.
#   * Qty made empty/0                  -> QUEUED. Placed sequentially by start
#                                          date after the line's running MO,
#                                          unlocked (the solver may reshuffle).
#
# `Qty made` EMPTY means "not started" — it is not a zero-production fact.
#
# CIP synthesis: cip_info gives PreviousCIP, ScheduledCIP and MaxHoursBetweenCIP
# (120 or 144 per line — authoritative, it overrides [cip] interval_h). We draw
# the scheduled CIP, then space further CIPs at MaxHoursBetweenCIP from the last
# known one until the end of the horizon, so weeks 2-3 are not silently CIP-free.
#
# Output is a plain calendar_blocks frame (CALENDAR_COLUMNS) in ANCHOR HOURS,
# so it can be handed straight to the Gantt, saved, or used as the solver's
# initial state. Everything is pure: pass `now` to test without a clock.

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


@dataclass
class CurrentState:
    """Ground-truth calendar plus the accounting of how it was derived."""

    blocks: pd.DataFrame
    running: list[dict] = field(default_factory=list)
    queued: list[dict] = field(default_factory=list)
    completed: list[dict] = field(default_factory=list)
    cips: list[dict] = field(default_factory=list)
    line_free_h: dict[str, float] = field(default_factory=dict)
    # End of the RUNNING MO only (queued MOs are solver orders when
    # current_mo.csv is produced; the gate must not double-count them).
    line_running_free_h: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "running": len(self.running),
            "queued": len(self.queued),
            "completed_dropped": len(self.completed),
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
    """Resolve a line_id from lines.csv; fall back to the P09=0 convention."""
    name = str(line or "").strip().upper()
    if lines is not None and len(lines) and "line_name" in lines.columns:
        hit = lines[lines["line_name"].astype(str).str.strip().str.upper() == name]
        if len(hit):
            try:
                return int(hit.iloc[0]["line_id"])
            except (TypeError, ValueError):
                pass
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

def classify_rows(frame: pd.DataFrame | None) -> list[dict]:
    """Tag every manprg row running / queued / completed / cip."""
    out: list[dict] = []
    if frame is None or not len(frame):
        return out
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
        started = (not _is_blank(made_raw)) and made > 0
        complete = fct > 0 and (made >= fct or left <= 0)
        if item.upper() == "CIP":
            kind = "cip"
        elif complete:
            kind = "completed"
        elif started:
            kind = RUNNING
        else:
            kind = QUEUED
        pct = round(made / fct * 100.0, 1) if fct > 0 else 0.0
        out.append({
            "line": str(r.get("line", "")).strip().upper(),
            "mo": _code(r.get("mo")),
            "item": item,
            "designation": str(r.get("designation", "")).strip(),
            "start_dt": pd.Timestamp(start_dt),
            "hours": hours,
            "fct_cas": fct,
            "made_cas": made,
            "left_cas": left,
            "qty_kg": _num(r.get("fct_kg")),
            "completion_pct": pct,
            "kind": kind,
            "started": started,
        })
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
# CIP projection
# --------------------------------------------------------------------------

def project_cips(line: str, info, hz: Horizon, dur: float,
                 interval: float) -> list[tuple[datetime, str]]:
    """Scheduled CIP + CIPs spaced at MaxHoursBetweenCIP across the horizon.

    `ScheduledCIP` is not maintained after the CIP happens, so it can sit in the
    past or even before `PreviousCIP` (seen on P15: scheduled 08-05 23:00 vs
    previous 08-06 17:03). A stale one is ignored rather than drawn behind the
    anchor — and it must not seed the projection either, or every later CIP
    inherits the wrong phase.
    """
    out: list[tuple[datetime, str]] = []
    last: datetime | None = None
    if info is not None:
        if info.previous_cip is not None:
            last = pd.Timestamp(info.previous_cip).to_pydatetime()
        if info.scheduled_cip is not None:
            sched = pd.Timestamp(info.scheduled_cip).to_pydatetime()
            stale = sched < hz.anchor or (last is not None and sched < last)
            if not stale:
                out.append((sched, CIP_SCHEDULED))
                if last is None or sched > last:
                    last = sched
    if last is None:
        last = hz.anchor
    if interval <= 0:
        return out
    nxt = last + timedelta(hours=interval)
    guard = 0
    while nxt < hz.end and guard < 64:
        guard += 1
        if all(abs((nxt - s).total_seconds()) > 3600 for s, _ in out):
            out.append((nxt, CIP_PROJECTED))
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
) -> CurrentState:
    """Ground-truth calendar for the horizon, in anchor hours.

    Either pass already-parsed `manprg` / `cips` results (tests, reuse from the
    page) or the file paths to read them from.
    """
    n = now or hz.now
    if manprg is None:
        manprg = read_manprg(list(manprg_paths or []))
    if cips is None:
        cips = read_cip_info(cip_path) if cip_path else CipInfoResult()

    state = CurrentState(blocks=pd.DataFrame(columns=CALENDAR_COLUMNS))
    state.warnings.extend(manprg.warnings)
    state.warnings.extend(cips.warnings)

    rows = classify_rows(manprg.frame)
    dur = cip_hours(cfg)
    blocks: list[dict] = []

    by_line: dict[str, list[dict]] = {}
    for r in rows:
        by_line.setdefault(r["line"], []).append(r)

    for line in sorted(by_line):
        lid = line_id_for(line, lines)
        line_rows = sorted(by_line[line], key=lambda r: r["start_dt"])
        cursor = pd.Timestamp(n)

        # 1. the running MO — locked, runs to its estimated end
        running = [r for r in line_rows if r["kind"] == RUNNING]
        if len(running) > 1:
            # only the latest start can really be running; the rest are stale
            running.sort(key=lambda r: r["start_dt"])
            for stale in running[:-1]:
                state.warnings.append(
                    f"{line}: MO {stale['mo']} started but superseded by "
                    f"{running[-1]['mo']} — treated as completed")
                stale["kind"] = "completed"
            running = running[-1:]
        for r in running:
            end = _estimated_end(r, n)
            # A long-running MO can have started days before the anchor. Keep
            # the block INSIDE the window (a start_h of -191 renders nowhere on
            # a rolling Gantt) but record the true start so nothing is lost.
            true_start = pd.Timestamp(r["start_dt"])
            shown_start = max(true_start, pd.Timestamp(hz.anchor))
            blk = _block(r, line, lid, shown_start, end, RUNNING,
                         locked=True, anchor=hz.anchor)
            blk["attrs"] += f";started={true_start:%Y-%m-%dT%H:%M}"
            if shown_start > true_start:
                blk["attrs"] += ";clamped_to_anchor"
            blocks.append(blk)
            state.running.append({**r, "est_end": end,
                                  "shown_start": shown_start.to_pydatetime()})
            cursor = max(cursor, pd.Timestamp(end))
            # Running-only free hour (queued MOs are solver orders now)
            state.line_running_free_h[line] = round(
                _hours(max(pd.Timestamp(end), pd.Timestamp(n)), hz.anchor), 3)
        if not running:
            state.line_running_free_h[line] = round(
                _hours(max(pd.Timestamp(n), pd.Timestamp(hz.anchor)),
                       hz.anchor), 3)

        # 2. queued MOs — sequential, no overlap, keep manprg's intended order
        for r in line_rows:
            if r["kind"] != QUEUED:
                continue
            start = max(pd.Timestamp(r["start_dt"]), cursor)
            hours = r["hours"] if r["hours"] > 0 else 1.0
            end = start + timedelta(hours=hours)
            blocks.append(_block(r, line, lid, start, end, QUEUED,
                                 locked=False, anchor=hz.anchor))
            state.queued.append({**r, "placed_start": start, "placed_end": end})
            cursor = end

        state.completed.extend([r for r in line_rows if r["kind"] == "completed"])
        state.line_free_h[line] = round(_hours(cursor, hz.anchor), 3)

    # 3. CIP — from cip_info (authoritative), plus the CIP pseudo-MOs in manprg
    cip_lines = set(by_line) | set(cips.by_line)
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
        for when, kind in project_cips(line, info, hz, dur, interval):
            end = when + timedelta(hours=dur)
            note = (info.notes if info and info.notes
                    and info.notes.upper() != "NULL" else "")
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
                               "interval_h": interval, "notes": note})

    df = pd.DataFrame(blocks, columns=CALENDAR_COLUMNS) if blocks else \
        pd.DataFrame(columns=CALENDAR_COLUMNS)
    if len(df):
        # keep only what intersects the horizon; the past is already history
        df = df[(df["end_h"] > 0.0) & (df["start_h"] < hz.end_h)].copy()

    # ── CIP/production merge: split production around CIP windows ─────────
    # Production and CIP blocks were collected independently above; the CIP
    # projection must not overlap real MOs. CIPs win (never split); a
    # production MO spanning a CIP is split into before/after pieces. A CIP
    # that would swallow an MO whole is dropped with a warning instead.
    if len(df):
        merged: list[dict] = []
        for line in sorted(set(df["line_name"])):
            line_df = df[df["line_name"] == line]
            prods = [dict(r) for _, r in line_df[line_df["block_type"] == "production"].iterrows()]
            cips = [dict(r) for _, r in line_df[line_df["block_type"] == "cip"].iterrows()]
            clipped, kept_cips = _clip_prod_around_cips(
                prods, cips, warnings=state.warnings, line=line)
            merged.extend(clipped)
            merged.extend(kept_cips)
        df = pd.DataFrame(merged, columns=CALENDAR_COLUMNS) if merged else \
            pd.DataFrame(columns=CALENDAR_COLUMNS)
        df = df.sort_values(["line_name", "start_h"]).reset_index(drop=True)
    state.blocks = df
    return state


def _block(r: dict, line: str, lid: int, start, end, kind: str,
           *, locked: bool, anchor: datetime) -> dict:
    label = r["item"] or r["mo"]
    # manprg TRIALS pseudo-MOs ARE the plant's trial reservations (user
    # 2026-08-14: "the trials will show up on those files" — trials.csv is
    # not a source). A trial is blocked production time, never tonnage.
    is_trial = str(r["item"]).strip().upper() == "TRIALS"
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


def _clip_prod_around_cips(
    blocks: list[dict],
    cip_blocks: list[dict],
    *,
    warnings: list[str],
    line: str,
) -> tuple[list[dict], list[dict]]:
    """Split production blocks around CIP windows so nothing overlaps.

    CIP windows are non-negotiable (never split, never moved here); a
    production block that spans a CIP is split into before/after pieces.
    If a CIP window swallows a production block entirely, the production
    MO is kept (it is real work from manprg) and the CIP window is dropped
    with a warning — production can move a CIP at the required frequency,
    but it cannot delete an in-flight/queued MO.

    Returns (clipped_production_blocks, kept_cip_blocks).
    """
    if not cip_blocks:
        return blocks, list(cip_blocks)
    cips = sorted(
        (dict(c) for c in cip_blocks),
        key=lambda c: (float(c["start_h"]), float(c["end_h"])),
    )
    dropped_cip_ids = set()
    out: list[dict] = []
    for b in blocks:
        s, e = float(b["start_h"]), float(b["end_h"])
        cursor = s
        placed_any = False
        for c in cips:
            cs_, ce_ = float(c["start_h"]), float(c["end_h"])
            if ce_ <= cursor or cs_ >= e:
                continue  # CIP before/after this production piece
            if cs_ <= cursor and ce_ >= e:
                # CIP swallows the whole remaining production
                warnings.append(
                    f"{line}: CIP {c.get('label', 'CIP')} "
                    f"({cs_:.0f}h–{ce_:.0f}h) covers MO {b.get('order_id')} "
                    "— keeping the MO, dropping the CIP window")
                dropped_cip_ids.add(c.get("block_id"))
                out.append(dict(b))  # MO survives whole; CIP dropped
                placed_any = True
                cursor = e
                break
            if cs_ > cursor:
                piece = dict(b)
                piece["start_h"] = round(cursor, 3)
                piece["end_h"] = round(cs_, 3)
                piece["attrs"] = (b.get("attrs", "") + ";split").strip(";")
                out.append(piece)
                placed_any = True
            cursor = max(cursor, ce_)
            if cursor >= e:
                break
        if cursor < e:
            piece = dict(b)
            piece["start_h"] = round(cursor, 3)
            piece["end_h"] = round(e, 3)
            piece["attrs"] = (b.get("attrs", "") + ";split").strip(";") \
                if placed_any else b.get("attrs", "")
            out.append(piece)
    kept = [c for c in cips if c.get("block_id") not in dropped_cip_ids]
    return out, kept
