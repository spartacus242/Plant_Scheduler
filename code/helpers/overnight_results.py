# helpers/overnight_results.py — reader for the overnight optimizer's output.
#
# The overnight batch (a separate engine) writes data/optimizer/:
#   latest.json              — {"generation", "leaderboard", "brief"} pointer
#                              + optional additive "published" rows: each
#                              published candidate with ITS OWN generation's
#                              baseline and same-generation delta
#   <gen_id>/leaderboard.json — the generation's candidates + scores + guards
#   <gen_id>/state.json      — the batch's phase ledger for that generation
#                              (staged/arms_running/arms_done/consolidated/
#                              closed/published) with a heartbeat `updated`;
#                              how a crashed night is told from a quiet one
#   brief.md                 — deterministic morning summary
# Home (chip row) and Generate (results table) both render from THIS loader,
# so the chip and the table can never disagree. Pure module — no Streamlit —
# every rule is unit-testable against tests/fixtures/optimizer/.
#
# Anything missing, corrupt or schema-violating reads as "no generation"
# (NOT_SET): a half-written batch must degrade to silence, never take a
# page down.

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# OK flips to STALE at 26h: a nightly batch plus the same drift allowance
# the data-health cadences give the daily feeds.
FRESH_H = 26.0

NOT_SET = "not_set"
OK = "ok"
STALE = "stale"

_PUBLISHED_LABEL = {"best": "★ best", "runner_up": "runner-up"}

# The batch's per-generation phase ledger (scripts/overnight_batch.py
# write_state). `published` is the only terminal phase; a generation newer
# than latest.json's that never reached it is an unfinished night —
# "running" while its heartbeat is fresh, "interrupted" once the heartbeat
# has been silent for STALL_H (an arm takes ~50 min at full budget; the 5am
# wait heart-beats every 5 min; the 2026-08-22 reboot left a 16h silence).
STATE_FILE = "state.json"
PHASE_PUBLISHED = "published"
PHASE_UNKNOWN = "unknown"
STALL_H = 2.0


def optimizer_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "optimizer"


def _read_json(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def load_latest(data_dir: Path) -> dict | None:
    """The latest.json pointer, or None when absent / corrupt / incomplete."""
    raw = _read_json(optimizer_dir(data_dir) / "latest.json")
    if raw is None:
        return None
    if not isinstance(raw.get("generation"), str):
        return None
    if not isinstance(raw.get("leaderboard"), str):
        return None
    return raw


def _resolve(rel: str, data_dir: Path) -> Path:
    # latest.json carries paths relative to data/optimizer/; tolerate
    # data-dir-relative ones ("optimizer/<gen>/leaderboard.json") too.
    p = optimizer_dir(data_dir) / rel
    return p if p.exists() else Path(data_dir) / rel


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _score_ok(score: Any) -> bool:
    return isinstance(score, dict) and _num(score.get("composite"))


# -- the additive latest.json "published" block ------------------------------

def published_entries(latest: dict | None) -> list[dict]:
    """Validated rows of latest.json's "published" block. [] for older
    writers (key absent) and for malformed rows — schema violations degrade
    to the same-generation fallback, never take a page down."""
    raw = (latest or {}).get("published")
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        if not isinstance(r.get("generation"), str):
            continue
        if not _num(r.get("composite")):
            continue
        rows.append(r)
    return rows


def _best_published(entries: list[dict]) -> dict | None:
    """The row the chip leads with: the "best" tag, else highest composite.
    Only rows carrying an honest same-generation delta qualify — a published
    candidate whose own generation had no baseline must never borrow
    another generation's."""
    usable = [r for r in entries if _num(r.get("delta_same_gen"))]
    if not usable:
        return None
    for r in usable:
        if r.get("tag") == "best":
            return r
    return max(usable, key=lambda r: float(r["composite"]))


def _gen_stamp(gen_id: str) -> datetime | None:
    """The <YYYYMMDD-HHMM> prefix every gen_id carries, or None."""
    try:
        return datetime.strptime(str(gen_id)[:13], "%Y%m%d-%H%M")
    except ValueError:
        return None


def board_baseline_composite(board: dict | None) -> float | None:
    """A leaderboard's OWN board-baseline composite (never another
    generation's), or None when its board had no comparable fill region."""
    base = (board or {}).get("board_baseline")
    score = base.get("overnight_score") if isinstance(base, dict) else None
    if _score_ok(score):
        return float(score["composite"])
    return None


def all_within_noise(board: dict | None, spread: float | None) -> bool:
    """True when EVERY candidate's same-generation delta vs the board
    baseline sits inside ±spread — the ranking is then a tie, not a win."""
    base = board_baseline_composite(board)
    if base is None or spread is None:
        return False
    deltas: list[float] = []
    for c in (board or {}).get("candidates") or []:
        score = c.get("overnight_score")
        if not _score_ok(score):
            return False
        deltas.append(abs(float(score["composite"]) - base))
    return bool(deltas) and all(d <= float(spread) for d in deltas)


def _valid_leaderboard(board: Any) -> bool:
    if not isinstance(board, dict):
        return False
    if not isinstance(board.get("generation"), str):
        return False
    cands = board.get("candidates")
    if not isinstance(cands, list) or not cands:
        return False
    for c in cands:
        if not isinstance(c, dict) or not isinstance(c.get("run_id"), str):
            return False
        if not _score_ok(c.get("overnight_score")):
            return False
    return True


def load_leaderboard(data_dir: Path) -> dict | None:
    """The generation latest.json points at, or None unless it validates."""
    latest = load_latest(data_dir)
    if latest is None:
        return None
    board = _read_json(_resolve(latest["leaderboard"], data_dir))
    return board if _valid_leaderboard(board) else None


def load_brief(data_dir: Path) -> str | None:
    """brief.md text (via the pointer, falling back to the fixed name)."""
    latest = load_latest(data_dir) or {}
    rel = latest.get("brief")
    path = _resolve(str(rel) if rel else "brief.md", data_dir)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def parse_created(board: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(board.get("created")))
    except ValueError:
        return None


def age_hours(created: datetime, now: datetime | None = None) -> float:
    """Hours since `created`; naive/aware mismatches settle on local naive."""
    if now is None:
        now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
    if created.tzinfo is not None and now.tzinfo is None:
        created = created.astimezone().replace(tzinfo=None)
    elif created.tzinfo is None and now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    return (now - created).total_seconds() / 3600.0


def classify_age(age_h: float | None) -> str:
    """OK under FRESH_H; STALE beyond it or when the timestamp is unreadable."""
    if age_h is None:
        return STALE
    return OK if age_h < FRESH_H else STALE


# -- the phase ledger: an unfinished night ------------------------------------

def load_state(data_dir: Path, gen_id: str) -> dict | None:
    """<gen_id>/state.json, or None when absent / corrupt / phase-less."""
    raw = _read_json(optimizer_dir(data_dir) / str(gen_id) / STATE_FILE)
    if raw is None or not isinstance(raw.get("phase"), str):
        return None
    return raw


@dataclass(frozen=True)
class NightState:
    generation: str
    phase: str                   # batch phase, or "unknown" (no state.json)
    updated: datetime | None     # last heartbeat
    age_h: float | None
    arms_done: int
    arms_planned: int | None
    interrupted: bool            # heartbeat silent >= STALL_H (or unreadable)


def _parse_iso(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def unfinished_night(data_dir: Path,
                     now: datetime | None = None) -> NightState | None:
    """The newest generation that never reached the `published` phase AND
    is newer than the one latest.json points at (an older crash that a
    later night superseded is history, not an alert). A generation with a
    leaderboard but no state.json predates phase tracking: its batch wrote
    candidates and never finalized — reported with phase "unknown" and the
    leaderboard's mtime as its last sign of life."""
    opt = optimizer_dir(data_dir)
    latest = load_latest(data_dir)
    floor = _gen_stamp(latest["generation"]) if latest else None
    try:
        dirs = [p for p in opt.iterdir() if p.is_dir()]
    except OSError:
        return None
    newest: tuple[datetime, Path] | None = None
    for p in dirs:
        stamp = _gen_stamp(p.name)
        if stamp is None or not (p / "leaderboard.json").exists():
            continue
        if floor is not None and stamp <= floor:
            continue
        state = load_state(data_dir, p.name)
        if state is not None and state.get("phase") == PHASE_PUBLISHED:
            continue
        if newest is None or stamp > newest[0]:
            newest = (stamp, p)
    if newest is None:
        return None
    _stamp, p = newest
    state = load_state(data_dir, p.name)
    if state is None:
        board = _read_json(p / "leaderboard.json") or {}
        cands = board.get("candidates")
        arms = len(cands) if isinstance(cands, list) else 0
        try:
            updated: datetime | None = datetime.fromtimestamp(
                (p / "leaderboard.json").stat().st_mtime)
        except OSError:
            updated = None
        phase, planned = PHASE_UNKNOWN, None
    else:
        updated = _parse_iso(state.get("updated"))
        phase = str(state["phase"])
        arms = int(state["arms_done"]) if _num(state.get("arms_done")) else 0
        planned = (int(state["arms_planned"])
                   if _num(state.get("arms_planned")) else None)
    age = age_hours(updated, now) if updated is not None else None
    return NightState(generation=p.name, phase=phase, updated=updated,
                      age_h=age, arms_done=arms, arms_planned=planned,
                      interrupted=age is None or age >= STALL_H)


def resume_command(gen_id: str) -> str:
    """What to run from the repo root to finish an interrupted night."""
    return f"python scripts/overnight_batch.py --resume {gen_id}"


def night_text(night: NightState) -> str:
    """One sentence for the chip / section: what the unfinished night is."""
    arms = (f"{night.arms_done}/{night.arms_planned}" if night.arms_planned
            else f"{night.arms_done}")
    if night.interrupted:
        silent = (f"silent {_fmt_age(night.age_h)}" if night.age_h is not None
                  else "no readable heartbeat")
        return (f"night {night.generation} interrupted in phase "
                f"{night.phase} after {arms} arm(s), {silent} — nothing "
                "published; resume it")
    return (f"tonight's batch running: {night.phase}, {arms} arm(s) done")


# -- Small pure formatters (shared by the Home chip and the Generate table) --

def published_label(candidate: dict) -> str:
    return _PUBLISHED_LABEL.get(candidate.get("published") or "", "")


def budgets_text(candidate: dict) -> str:
    b = candidate.get("budgets") or {}
    p1, p2 = b.get("pass1_s"), b.get("pass2_s")
    if p1 is None and p2 is None:
        return "—"
    return f"{int(p1 or 0)}s + {int(p2 or 0)}s"


def guards_text(candidate: dict) -> str:
    """'OK', or the violated guards ('1 overlap · CIP ✗'). '—' when absent."""
    g = candidate.get("guards")
    if not isinstance(g, dict):
        return "—"
    issues: list[str] = []
    try:
        overlaps = int(g.get("overlaps", 0) or 0)
    except (TypeError, ValueError):
        overlaps = 0
    if overlaps:
        issues.append(f"{overlaps} overlap{'s' if overlaps != 1 else ''}")
    for key, label in (("pins_ok", "pins"), ("lock_ok", "lock"), ("cip_ok", "CIP")):
        if g.get(key) is False:
            issues.append(f"{label} ✗")
    return " · ".join(issues) if issues else "OK"


def _fmt_age(age_h: float) -> str:
    return f"{age_h:.0f}h" if age_h < 48 else f"{age_h / 24:.0f}d"


@dataclass(frozen=True)
class OvernightSummary:
    state: str                      # NOT_SET | OK | STALE
    detail: str                     # one line for the Home chip row
    generation: str | None = None
    created: datetime | None = None
    age_h: float | None = None
    n_runs: int = 0
    best: dict | None = None        # latest gen's highest-composite candidate
    # the chip trio — ALWAYS same-generation: the published best vs its own
    # generation's board when latest.json carries the "published" block,
    # else the latest generation's best vs that generation's own baseline
    best_composite: float | None = None
    board_composite: float | None = None
    delta_vs_board: float | None = None
    noise_runs: int | None = None
    noise_spread: float | None = None
    published: tuple = ()           # candidates tagged best / runner_up
    board: dict | None = None       # the full validated leaderboard
    published_best: dict | None = None  # "published" row the chip used
    # the newest night that never finalized (newer than the published
    # generation): interrupted (heartbeat silent) or still running
    night: NightState | None = None


def _with_night(detail: str, night: NightState | None) -> str:
    if night is None:
        return detail
    lead = ("⚠ " if night.interrupted else "☾ ") + night_text(night)
    return f"{lead} · {detail}"


def summarize(data_dir: Path, now: datetime | None = None) -> OvernightSummary:
    """Everything the UI surfaces need, from one validated read.

    The delta is always SAME-GENERATION — generations are never mixed. The
    2026-08-21 brief once compared a 1505-generation candidate against the
    0500 generation's board (+4.54 when the honest delta was +0.44)."""
    night = unfinished_night(data_dir, now)
    board = load_leaderboard(data_dir)
    if board is None:
        return OvernightSummary(
            state=NOT_SET,
            detail=_with_night(
                "no overnight generation yet — the batch writes "
                "data/optimizer/", night),
            night=night)

    cands = board["candidates"]
    best = max(cands, key=lambda c: c["overnight_score"]["composite"])

    # fallback trio: latest generation's best vs its OWN baseline
    best_comp = float(best["overnight_score"]["composite"])
    board_comp = board_baseline_composite(board)
    delta = best_comp - board_comp if board_comp is not None else None

    # the published block wins when present: its rows already carry each
    # candidate's own-generation delta
    pub = _best_published(published_entries(load_latest(data_dir)))
    age_note = ""
    if pub is not None:
        best_comp = float(pub["composite"])
        delta = float(pub["delta_same_gen"])
        bb = pub.get("board_baseline_composite")
        board_comp = float(bb) if _num(bb) else None
        if pub.get("generation") != board["generation"]:
            # published best from an older generation than latest — say so
            stamp = _gen_stamp(pub["generation"])
            pub_age = age_hours(stamp, now) if stamp is not None else None
            age_note = (f", {_fmt_age(pub_age)} ago"
                        if pub_age is not None and pub_age >= 0
                        else ", earlier generation")

    noise = board.get("noise_floor") or {}
    spread = noise.get("spread_composite")
    spread = float(spread) if isinstance(spread, (int, float)) else None
    runs = noise.get("runs")
    runs = int(runs) if isinstance(runs, int) else None

    created = parse_created(board)
    age_h = age_hours(created, now) if created is not None else None
    state = classify_age(age_h)

    if pub is not None:
        delta_txt = f" ({delta:+.1f} vs its board{age_note})"
    elif delta is not None:
        delta_txt = f" ({delta:+.1f} vs board)"
    else:
        delta_txt = ""
    bits = [f"{len(cands)} runs", f"best {best_comp:.1f}" + delta_txt]
    if spread is not None:
        bits.append(f"noise ±{spread:.1f}")
    detail = " · ".join(bits)
    if state == OK:
        detail += " — review"
    else:
        detail = (f"{_fmt_age(age_h)} old — " if age_h is not None
                  else "no readable timestamp — ") + detail

    published = tuple(sorted(
        (c for c in cands if c.get("published") in _PUBLISHED_LABEL),
        key=lambda c: c.get("published") != "best"))
    return OvernightSummary(
        state=state, detail=_with_night(detail, night),
        generation=board["generation"],
        created=created, age_h=age_h, n_runs=len(cands), best=best,
        best_composite=best_comp, board_composite=board_comp,
        delta_vs_board=delta, noise_runs=runs, noise_spread=spread,
        published=published, board=board, published_best=pub, night=night)
