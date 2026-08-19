# helpers/overnight_results.py — reader for the overnight optimizer's output.
#
# The overnight batch (a separate engine) writes data/optimizer/:
#   latest.json              — {"generation", "leaderboard", "brief"} pointer
#   <gen_id>/leaderboard.json — the generation's candidates + scores + guards
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
import math
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


def _score_ok(score: Any) -> bool:
    if not isinstance(score, dict):
        return False
    comp = score.get("composite")
    # NaN/inf must fail here, not just non-numbers: json.dump writes a bare
    # NaN for float('nan'), so an arm that failed to score would otherwise
    # poison max() and print "best nan" under a green chip.
    return (isinstance(comp, (int, float)) and not isinstance(comp, bool)
            and math.isfinite(comp))


def _candidate_ok(c: Any) -> bool:
    return (isinstance(c, dict) and isinstance(c.get("run_id"), str)
            and _score_ok(c.get("overnight_score")))


def _valid_leaderboard(board: Any) -> bool:
    return (isinstance(board, dict)
            and isinstance(board.get("generation"), str)
            and isinstance(board.get("candidates"), list)
            and any(_candidate_ok(c) for c in board["candidates"]))


def load_leaderboard(data_dir: Path) -> dict | None:
    """The generation latest.json points at, or None unless it validates.

    Unscorable arms are dropped rather than sinking the whole board: one
    engine-side misscore shouldn't hide the night's other runs.
    """
    latest = load_latest(data_dir)
    if latest is None:
        return None
    board = _read_json(_resolve(latest["leaderboard"], data_dir))
    if not _valid_leaderboard(board):
        return None
    return dict(board,
                candidates=[c for c in board["candidates"] if _candidate_ok(c)])


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
    best: dict | None = None        # highest-composite candidate
    best_composite: float | None = None
    board_composite: float | None = None
    delta_vs_board: float | None = None
    noise_runs: int | None = None
    noise_spread: float | None = None
    published: tuple = ()           # candidates tagged best / runner_up
    board: dict | None = None       # the full validated leaderboard


def summarize(data_dir: Path, now: datetime | None = None) -> OvernightSummary:
    """Everything the UI surfaces need, from one validated read."""
    board = load_leaderboard(data_dir)
    if board is None:
        return OvernightSummary(
            state=NOT_SET,
            detail="no overnight generation yet — the batch writes data/optimizer/")

    cands = board["candidates"]
    best = max(cands, key=lambda c: c["overnight_score"]["composite"])
    best_comp = float(best["overnight_score"]["composite"])

    base = board.get("board_baseline") or {}
    board_comp = None
    if _score_ok(base.get("overnight_score")):
        board_comp = float(base["overnight_score"]["composite"])
    delta = best_comp - board_comp if board_comp is not None else None

    noise = board.get("noise_floor") or {}
    spread = noise.get("spread_composite")
    spread = float(spread) if isinstance(spread, (int, float)) else None
    runs = noise.get("runs")
    runs = int(runs) if isinstance(runs, int) else None

    created = parse_created(board)
    age_h = age_hours(created, now) if created is not None else None
    state = classify_age(age_h)

    bits = [f"{len(cands)} runs", f"best {best_comp:.1f}"
            + (f" ({delta:+.1f} vs board)" if delta is not None else "")]
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
        state=state, detail=detail, generation=board["generation"],
        created=created, age_h=age_h, n_runs=len(cands), best=best,
        best_composite=best_comp, board_composite=board_comp,
        delta_vs_board=delta, noise_runs=runs, noise_spread=spread,
        published=published, board=board)
