# P2 Dispatch 4 — Constraint probe-verification pass (charter §5)

**Branch:** `develop` · **Owner:** Director · **Executor:** Claude Code (opus) · **Status:** spec — Claude executes, Director verifies before "done".

## Why

The charter's §5 table lists every constraint the solver claims to enforce. Rows 4 (NoOverlap), 5 (CIP spacing) are already covered by contract tests C3/C6; rows 1, 2, 3, 6, 7, 8, 9 have **no automated probe**. The goal: every row gets a probe that asserts the constraint holds on a real solve output — so a future solver change that silently violates a constraint turns the suite red.

## Context (verified 2026-08-13, do not re-derive)

- Contract-test pattern: `tests/test_solver_contracts.py` — reads the newest solved work dir under `data/_scenario_work/` (fixture `_solved_work_dirs()`), asserts on artifacts, skips cleanly when no work dir exists. Contracts C1–C7 are listed in its header.
- `code/solver/validate_schedule.py` already implements checks that can be reused/wired:
  - produced-vs-bounds (`qmin <= produced <= qmax`, lines ~51-56)
  - CIP spacing check
  - `check_changeover_timing(changeovers_path, ...)` — gaps between consecutive runs on a line respect `changeovers.csv` setup_hours (lines ~176-206)
- `data/_scenario_work/` holds previously solved runs (newest wins). If none exists or all are stale (no solve in the last 7 days), generate ONE fresh solve first: `env -u PYTHONPATH .venv/Scripts/python.exe scripts/check_solver_current_state.py` (or the single-phase CLI with a 120 s budget) — a real solve takes 60–600 s; do NOT put solves inside tests.
- Suite: `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` → currently **165 passed / 0 failed / 8 deselected**.
- ⚠️ `.py` files are CRLF — preserve line endings, ASCII-only edits, no reformatting.

## Scope (exactly this)

### A. New probe module `tests/test_constraint_probes.py`
Map to charter §5 rows (name them P1..P9 for traceability):

| Probe | Charter row | What it asserts on the solved schedule |
|---|---|---|
| P1 | 1 min run time | every production block's duration ≥ `min_run_hours` (read from `flowstate.toml [scheduler]`) or the documented 50%-of-qty_min rule (replicate the logic from `model_builder.py:258-266`, do not import the solver) |
| P2 | 2 batch size | `qmin <= produced <= qmax` for every order (wire the validator's bounds check or assert directly) |
| P3 | 3 capability | every scheduled (line, sku) pair has `capable == 1` in `capabilities_rates.csv` |
| P4 | 4 NoOverlap | already covered by C3 — **skip, no new probe** (add a comment pointing at C3) |
| P5 | 5 CIP max deadline | every CIP ends before (last clean end + max_cip_hrs) using `line_cip_hrs.csv` + `cip_info.csv` `PreviousCIP` (the hard compliance deadline, `model_builder.py:829-830`) |
| P6 | 6 changeovers | gaps between consecutive runs on a line ≥ `changeovers.csv` setup_hours (wire `validate_schedule.check_changeover_timing`) |
| P7 | 7 max lines per order | no order scheduled on more than `max_lines_per_order` (flowstate.toml, default 2) lines |
| P8 | 8 due dates | every scheduled order ends by its due + 1 h (the hard cap, `model_builder.py:192-193`); late orders may exist only where the cap allows |
| P9 | 9 demand total | total produced kg across the schedule ≤ total demand kg per SKU (no phantom tonnage; small tolerance for rounding, document the tolerance) |

Rules:
1. Same pattern as contracts: newest solved work dir, assert on artifacts (`schedule_phase2.csv`, work-dir inputs), skip cleanly when no work dir.
2. Read config from `flowstate.toml` where the solver does; never hard-code values.
3. Where the validator already has a check, REUSE it (import from `code/solver/validate_schedule.py`) instead of duplicating.
4. If a probe finds a real violation in the solved output: record it as a finding (work dir, violation detail) and make the probe assert the CURRENT behavior only if it is defensible; otherwise leave the probe asserting the CORRECT contract and let it fail — then report loudly in the final summary. Do NOT silently weaken a probe to make it pass. Do NOT fix solver bugs in this dispatch — report them.

### B. Ensure a real solve exists
5. If `data/_scenario_work/` has no usable solved dir, run one solve (see Context) so the probes have artifacts to assert on.

### C. Suite + report
6. Run the full suite — must be green or the failures must be exactly the documented probe findings.
7. Report: probes added (names), findings (violations found, if any), solver-bug candidates for a follow-up dispatch, pytest result.

## Verification (Director runs before "done")

1. `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -q` → 0 failures beyond documented findings.
2. New probes run against the newest solved work dir (not skipped).
3. No solver code changed; no commits; no pushes.
4. Report lists every charter §5 row and its coverage status.

## Out of scope

- Changing solver behaviour, weights, or constraints.
- The changeover data fix (separate dispatch — needs VIF data).
- Any UI work.
