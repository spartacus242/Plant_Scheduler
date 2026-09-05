# solver_tiny — deterministic solver fixture (inputs only)

Written 2026-09-03 (fix T-1 / audit finding tests-1). `tests/conftest.py`
solves this directory ONCE per pytest session (in-process `build_model` +
`CpSolver`, relax level 0, 2 workers, <= 15 s, seed 7) into a tmp work dir
and `tests/test_solver_contracts.py` / `tests/test_constraint_probes.py`
assert their INPUT -> OUTPUT contracts on that artifact instead of on
"whatever solved last" under `data/_scenario_work/`.

Geometry (anchor Mon 2026-09-07, horizon 336 h, hard demand, `use_sku_rates`):

| line | rate kg/h | capable | initial SKU | CIP carry / interval | fixed windows |
|---|---|---|---|---|---|
| L1 (0) | 1000 | A B C | A | 60 / 120 (first clean due by h60) | current MO `MO1` (A, 12,500 kg remaining, locked, due [0,47]) |
| L2 (1) | 800 | A B C D | CLEAN | 0 / 100 | downtime [40,60) Maintenance |
| L3 (2) | 500 | B C D (NOT A) | B | 0 / 120 | committed CIP window [100,106) |

Orders: A-W0 30 t, B-W0 24 t, C-W0 16 t, RUSH-C 8 t due [0,71] (priority 1),
A-W1 30 t, B-W1 16 t, D-W1 20 t (weeks [0,167] / [168,335], 90-110 %).
`allow_week1_in_week0 = false` so a week-1 order may not start before h168.

Changeovers: A<->B, A<->D, C<->D major 4 h; A<->C 1.25 h TTP-only (the
loader rounds setup TIME up to 2 h, SA-4 / C36); B->C 2.5 h with
`cip_req_after = 1` (floored at the 6 h CIP duration); B<->D 1 h.

Hand-derived facts the tests pin: MO1 bracket at whole hours of 1000 kg/h ->
qty_min 12,000 / qty_max 13,000 (C61); RUSH-C 8,000 kg must end by h72;
L2 has no production inside [40,60); nothing runs on (L3, A) or (L1, D).

Regenerate with `scratchpad/fixes/T/gen_solver_tiny.py` (kept with the fix
notes); any change here must update the hand derivations in
`tests/test_fix_T.py`.
