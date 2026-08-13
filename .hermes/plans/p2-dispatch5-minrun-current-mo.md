# P2 Dispatch 5 — Apply the min-run floor to current-state MOs (user-approved)

**Branch:** `develop` · **Owner:** Director · **Executor:** Claude Code (opus) · **Status:** spec — Claude executes, Director verifies before "done".
**User approval:** "apply the 4 hr floor" (2026-08-13).

## The defect (dispatch 4 finding 1, verified by Director)

`code/solver/model_builder.py:236-242` — current-state MOs on their locked line get `present == 1` forced, then hit `continue`, which skips the run-bound block at :251-274. The comment at :256-258 explicitly intends current MOs to keep "the global `min_run_hours`" floor — the code never applies it. Result: committed work (MOs already in VIF, running/queued) can be chopped into 1–3 h stubs.

## The fix (exactly this)

Restructure the current-MO handling so the min-run floor applies, without regressing the other current-MO semantics:

1. **Keep** the locked-line forcing: current MO on `locked_line` → `present == 1`; on any other line → `present == 0`, `run_h == 0` (the :236-242 else branch).
2. **Do NOT `continue`** for the locked-line case — fall through to the run-bound block so the existing current-MO floor rule at :257-258 (`min_run = min(max_len, max(1, P.min_run_hours))`) and the per-segment minimums (:269-274) apply.
3. **Do NOT apply the capability zero-out (:247-249) to current-state MOs.** manprg is ground truth — the plant physically runs those (line, sku) pairs; if the capabilities table disagrees, the table is wrong and the app's capability-check surfaces it. A current MO must never be silently zeroed by the table. (Finding 2 is thereby resolved by construction: the capability gate stays unreachable for current MOs, and the diagnostic path already exists elsewhere.)
4. Leave TRIALS handling and everything else untouched. The present-but-0-kg TRIALS oddity (finding 3) is out of scope — report it as still-open.
5. Preserve CRLF in `model_builder.py`; ASCII-only edits.

Implementation note: the natural shape is to keep the early branch for *non-locked* lines only, and let locked current MOs fall through, with the min-run rule at :257-258 already distinguishing current vs normal orders. Do not invent new config keys; `min_run_hours = 4` stays the value.

## Verification (mandatory, Director re-runs)

1. Real solve: generate a fresh work dir (single-phase, current-state scenario, 120–300 s budget; check `code/solver/phase2_scheduler.py --help` for relax/cross-week flags and prefer a solve that stays at relax level 0 or 1 — if it escalates, record the level reached and say so). The solve must be **feasible** with the floor applied.
2. The fresh solve's schedule must contain **no current-MO block shorter than 4 h** (re-run the P1c probe against it).
3. Remove the `xfail(strict=True)` marker from `test_p1c_current_mo_blocks_meet_the_min_run_floor` — it must now PASS. Do not weaken the assertion.
4. Probes P6 (changeover gaps) and P8 (due dates) must now RUN against the fresh solve instead of skipping (they skipped because the old newest work dir was relax-3/cross-week). If they still skip, explain exactly why in the report.
5. Full suite: `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` → 0 failed (skips allowed with reasons).
6. No commits, no pushes. Report: code diff summary, relax level of the solve, probe results, suite result, remaining open items (finding 3 TRIALS oddity; capability-diagnostic note).

## Out of scope

- Capability gating changes, TRIALS behaviour, weights, changeover data, UI.
- `changeovers.csv` data (already fixed by the user — new file verified 2026-08-13: ffs_change alive, no demand SKUs missing).
