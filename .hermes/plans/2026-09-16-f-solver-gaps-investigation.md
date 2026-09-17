# Scenario F "fill the tail": why the board has 4-h runs and empty lines, and what to change

Run 2026-09-16 09:23, staged time_limit 1200 s per pass. Wall time was about 41 min (pass 1 1,200 s + anchor + pass 2 1,200 s, 09:23:58-10:04:10), not 20 min. Five finders reported, and every finding went through a refute check and a planner-impact check. Where a verifier corrected a number, the corrected number is used below.

## 0. Symptom baseline (verified counts)

| Symptom | Board value |
|---|---|
| Fill blocks of exactly 4 h | 26 (27 production blocks incl. committed) |
| Fill blocks under 8 h | 39 = 184 h, 159.9 t, 131.4 t counting toward target (43 blocks if 8 h itself counts) |
| Idle after the gates | 1,240 h of 5,227 free h (the solver KPI shows 1,019 h / 79.6 % because it measures first-to-last block) |
| Orders under qty_min | 27 orders, 376.9 t (W0 4.7 / W1 123.6 / W2 164.4 / W3 84.2 t); 505.3 t short of target |
| Over target (earns 0) | 101.7 t |

The 26 four-hour blocks break down as follows:
- **16 second-line slivers:** the order's main run is on another line.
- **5 CIP-split segments:** same SKU on both sides of a committed clean.
- **3 W3 stubs.**
- **2 token sole runs of short W1 orders:** 280581-W1 on P09 h118-122 and 280587-W1 on P09 h216-220.

The 39 blocks under 8 h: 18 second-line slivers, 7 CIP-split pieces, 11 W3 stubs (4 built early, 7 in their own window) and 3 sole-line W0-W2 blocks. Source: slivers-2 table, reproduced independently by its refute verifier.

---

## 1. Root causes, ranked by how much of the circled symptoms each explains

The rows overlap, so the columns do not add up.

| # | Cause | Type | 4-h blocks (of 26) | Idle h (of 1,240) | qty_min shortfall (of 377 t) |
|---|---|---|---|---|---|
| 1 | Pass 1 never starts from the greedy seed, so its search is starved | Solver defect (3 bugs) | 21 disappear when fixed (measured) | 245-264 h fillable with the rest of the plan fixed; the fix alone nets 113 h | fix cuts it to 197-200 t |
| 2 | Soft demand removes every share floor on a second line | Design choice (2026-09-03) | Permits 16; 3 of the 5 left after fix 1 | 0 | 0 (slivers hold 43.5 t counted) |
| 3 | A CIP-split order fences off the gap between its two segments | Model rule | 5 | 99 h locked | Blocks e.g. 280103-W2 +64 t on P18 |
| 4 | W3 is a 2-day stub pro-rated to 2/7 | Staging choice (fix C20) | 3 (5 incl. W3 pieces of #2/#3) | Starves the P16-P22 tail (upper bound) | 84 t W3 |
| 5 | Horizon holds less demand than line capacity | Plant reality | 0 | ~820-900 h (66-72 %) | 0 |
| 6 | W1 small-format lines saturated, P11 down to Sep 27 | Plant reality, partly allocation | 1 (280581-W1 token run) | 0 W1 windows of 4 h or more | ~100 t (at least 22 t recoverable by search) |

### Solver defects and design choices

**1. Pass 1 never starts from the seed, so LNS builds the board out of 4-h pieces.** Found by seed-1/2/3/4, corroborated by slivers-3 and idle-1.

- **Mechanism.** The greedy seed has 3.70 Mt, 0 four-hour blocks and 7 blocks under 8 h. Three stacked defects make it unusable, so pass 1's first solution came at 28.6 s with 0 kg. From there every 4-h piece of any below-target order is an immediate improvement, and 1,200 s is not enough to consolidate the pieces. Pass 1 ended at 3.66 Mt, below its own seed. Pass 2 then spent its budget on pass 1's unfinished fill job: +180.7 M units (~179 t-eq) of fill, co_load down only 8.3 %.
- **Defect (a), seed-1: infeasible hint value.**
  - warm_start.py:119-121 (build_hint_plan) hints `seg_b_start = seg_b_end = 0` for every present pair without a split.
  - model_builder.py:1071-1079 requires `seg_b_start >= avail_l` whenever `present` is true, not only when `seg_b_present` is.
  - All 14 lines are gated (10-167 h), so the fixed-hint anchor is INFEASIBLE in 0.5 s ("linear: never in domain").
- **Defect (b), seed-2: greedy seed breaks the setup rules.**
  - greedy_fill.py:175-176 starts each line's first block at the gate with no setup from the line's held SKU and no long-shutdown extra.
  - greedy_fill.py:198 sets `tail_sku` in placement order, not time order.
  - Result: 8 rows break model_builder.py:1255-1265. Seven lines start 1-2 h too early at the gate (P10 167->169, P13 10->11, P14 105->106, P17 74->75, P20 92->93, P21 161->163, P22 152->154). P18 has a 1-h gap where 2 h is needed (280686->570261, 1.75 h rounded up).
  - The minimal repair costs 12,073 kg (0.33 %).
- **Defect (c), seed-3: pass 1 has no anchor solve.**
  - The pass-1 solver (phase2_scheduler.py:2495-2498) gets only the partial 15,960-var hint.
  - CP-SAT 9.15 does not adopt a partial hint even when it is feasible. First solution "#1 30.49s best:-0"; still 0 kg with hint_conflict_limit 100,000.
  - It adopts a complete one: "#1 30.68s ... complete_hint" at 3,687,723 kg.
  - Pass 2 already has exactly this anchor (phase2_scheduler.py:2650-2681).
  - Production's 0-kg start therefore has two causes together: the hint is infeasible (a+b) and it is partial (c).
- **Measured effect** (seed-4: all three fixed, pass 1 only, 300 s, 8 workers, production params, one run, pass 2 not run on top):

| Metric | Board (2,400 s, both passes) | Anchored pass 1 @300 s |
|---|---|---|
| Placed kg | 3,825,533 | 4,052,141 (+227 t) |
| Blocks exactly 4 h / under 8 h | 26 / 39 | 5 / 18 |
| Short of qty_min | 27 orders / 376.9 t | 23-24 orders / 196.8-200.5 t |
| Short of target | 505.3 t | 303.1 t |
| Shortfall by week W0/W1/W2/W3 | 5 / 124 / 164 / 84 t | 11 / 102 / 66 / 17 t |
| Over target (zero value) | 102 t | 126 t |
| co_load | 37,690 | 33,985 |
| Idle after gates | 1,240 h | 1,127 h |
| Pass-1 curve at 60 / 120 / 300 s | 0.50 / 1.56 / 2.33 Mt | 3.70 / 3.94 / 4.05 Mt |

  - **4-h blocks by line (board -> fixed):** P09 8->0 (the h216-251 cluster is gone), P12 4->1, P15 5->2, P17 2->0.
  - **Idle moves rather than disappears:** P18 130->3, P19 234->122, P20 117->46, P11 136->95, P17 135->104, but P14 85->127, P16 127->199, P22 73->153.
- **Proof the slivers are search residue, not the objective's optimum.**
  - *slivers-3.* On P15 [186,207), 4 slivers are strictly dominated by 20 h of 280351-W2 (the same SKU as the next block): +6.8 t counted, 5->1 transitions. On P09 [216,253), the feasible alternative (280103-W2 seg_a from 232 plus 280587-W1 at 216-225) gives +10.2 t counted and 7->2 transitions. Two net-negative slivers also survived pass 2: 280480-W2 on P09 h452-456 (0 counted kg) and 120532-W3 on P12 h454-458.
  - *idle-1.* With the whole plan fixed, freeing 19 (line, short order) pairs is OPTIMAL in 4-8 s and adds +305.7 t raw (+271.3 t toward target) in 264 h.
  - *idle-4.* On P09, 280581-W1 into h253-286 with 280103-W2 moved back to P18 h393-439 nets +22.4 t. The search never found this two-move.

**2. Soft demand removes every share floor on a second line.** Found by slivers-1 and slivers-5.

- **Mechanism.**
  - model_builder.py:858-859 sets `min_run_from_pct = 0` under soft demand, so `min_run = max(1, min_run_hours = 4)` (L866-869).
  - `sum(present) <= mlpo = 2` (L1018) then lets any capable line carry 4 h of any order.
  - This is intended: solver_rules.py:318-332 (2026-09-03) says "partial fills are the point".
  - The greedy seed never does this, because greedy_fill.py:57/163-164 floors every line at 50 % of qmin hours.
- **Effect.** It explains why 18 second-line slivers (16 of the 26 four-hour blocks) are allowed at all: 76 h, 58.3 t, of which 43.5 t counted and 14.8 t over target.
  - In pass 1 each sliver with counted kg earns about 8,500:1 to 1.9e6:1 its changeover cost.
  - After fix 1, 3 of the 5 remaining 4-h blocks are second-line pieces; the other 2 are same-line splits.
- **Not the cause: switch prices.** Pass 1's near-free switch price (0.003-0.19 kg) is real but is not what keeps slivers on the board. The board is pass 2's plan, and pass 2 already prices an FFS switch at about 2 t-eq and a TTP swap at 33 kg-eq. currency-1 and currency-2 were refuted by both lenses.

**3. A CIP-split order fences off the gap between its segments.** Found by idle-2 (mechanism confirmed; proposed fix partly refuted).

- **Mechanism.**
  - The pairwise ordering (model_builder.py:1305-1326) places every other order entirely before or after `eff_end`, which is `seg_b_end` when the order is split. No order can sit between seg_a and seg_b.
  - seg_b only needs `seg_b_start >= seg_a_end` (L648-650) and 4 h of run.
- **Board.**
  - P18: 280452-W2 runs 288-311 plus a 4-h stub at 389-393, locking 311-383 (72 h).
  - P14: 280478-W2 has a 4-h stub at 288-292 plus 325-382, locking 292-319 (27 h).
  - With the plan fixed, probes into those gaps are INFEASIBLE with the stub and FEASIBLE without it (P18 280103-W2 311-356: +64.1 t).
  - The P14 stub is all over-target kg. The P18 stub carries 2,032 counted kg, which stretching seg_a from 311 to 313 would capture.
- **Share.** CIP-split pieces are 5 of the 26 four-hour blocks. Most are legitimate zero-cost pieces; these two lock 99 h.

### Staging choice that shapes the tail

**4. W3 is a 2-day stub pro-rated to 2/7.** Found by horizon-2 and horizon-6 (both confirmed with corrections), plus slivers-11 and currency-5.

- **Mechanism.** plan_fill.py:440-467 multiplies ISO-41 qty_target, qty_min and qty_max by 48/168, taking the staged W3 to 509.9 t of 1,840.6 t source (1,330.7 t deferred).
- **Small orders.** 35 W3 orders, 21 under 10 t, 9 under 5 t (median 8.6 t). A 4-7 h run *is* the whole order. W3 accounts for 14 of the 39 blocks under 8 h and 6 of P12's 7 short blocks.
- **Early W3 stubs.** The 4 early ones (120536-W3 on P12 at h156, 280451-W3 at h313, 280572-W3 at h398, 280436-W3 at h409) exist because `early_fill_hours = "unbounded"` (plant decision). Each pays 5 % per week early.
- **Tail demand.** The ISO-41 remainder has 1,043 t / 701 h of SKUs capable on P16-P22, against 902 h idle there.
  - On this board the W3 cap binds on only 2 of 35 orders (136.6 t of W3 room under qty_max went unused), so the stub's share of today's idle is an upper bound.

### Plant reality

**5. The horizon holds less demand than the lines can run.** Found by horizon-1 (the headline survives; the share was corrected by the LP check).

- **Demand vs capacity.** Weekly demand of 1.70-1.84 Mt is about 1,300-1,500 h at best capable rates, against about 2,250 h of line capacity per week.
- **Idle that no in-horizon order can use.** An LP relaxation with the plan freed meets every qty_min and qty_target in 3,570 of 5,227 h. The remaining target shortfall of 403.6 t is only about 340-421 h. So about **820-900 h (66-72 %) of idle** cannot be filled to target by any in-horizon demand. The finder's "at least 958 h / 77 %" holds only for the plan as it stands.
- **With the plan frozen** (idle-1/idle-3 buckets), the 1,240 h split into:

| Bucket | Hours |
|---|---|
| Fillable | 245 |
| Locked by a CIP split | 99 |
| Capable short kg run out | 472 (P19 185, P17 73, P20 67, P11 62, ...) |
| No capable short order | 308 (P16 123, P21 82, P22 64, W3 tails on P11/P10 39) |
| Gaps under 4 h | 115 |

- **Room that earns nothing.** Up to qty_max another 501-628 t would legally fit, but it earns 0 (`over_target_reward_pct = 0`): unrewarded, not absent.

**6. W1 small formats are saturated, partly by allocation.** Found by idle-4, corrected by its verifiers and the horizon-1 LP verifier.

- **Saturation.**
  - P09, P11, P12, P13, P14 and P15 have zero W1 idle windows of 4 h or more.
  - P11 is down until h264 (Sep 27). P13 runs at 94.5 %, P15 at 93.1 %, P09 at 91.5 %.
- **Shortfall.** W1 orders capable only on those lines are about 100 t short of qty_min: 280581-W1 51.3, 280578-W1 16.2, 280572-W1 11.0, 280612-W1 9.5, 280345-W1 9.3 and 280580-W1 3.0 t, plus 280104-W0 4.7 t.
- **Not all of this is fixed capacity.**
  - The P09 two-move above recovers 22.4 t (a search miss).
  - 129 h of W1 time on these lines carry early W2/W3 orders.
  - The LP shows 1,119 run-h of SKUs that can also run on P16-P22 sitting on P09-P15 before h288, while P16-P22 are idle. This is not proven feasible with setups and integer runs.
  - The P15 swap (280612-W1 for 280351-W2) loses at current prices (775 vs 630 kg/h). That is a price outcome, not a defect.

### Reporting blind spots (why the scorecard did not flag any of this; they move no block)

- **Short runs.** Both the block count (scorecard_engine.py:1605) and the short_campaign_count that feeds the score (:1620, :2049-2050) use a strict `<` against short_run_h 4.0, which equals min_run_hours 4. Result: short_campaign_count 0 and campaigns 100/100 on a board with 26 four-hour blocks.
- **Idle KPI.** compute_idle_kpis (phase2_scheduler.py:1261) uses span = last_end - first_start, so it reports 1,019 h instead of 1,240 h. Per line it can err either way: P19 shows 10 h against 234 h, but P09 shows 32 h against 22 h because committed windows inside the span count as idle.
- **Service score.** The scorecard judges the full ISO-41 week against a plan given 2/7 of it. That makes 30 of the 46 unfilled orders and 36 of the 68 late orders artefacts.
- **Changeover score.** It counts every SKU transition as a recipe change, so a TTP-only swap is 1/4 of a topload there but 1/46 in the solver.

---

## 2. Change list (prioritised)

**No config-only change fixes the board.** Every config-only lever the finders tested (mlpo 1 or 3, min_run_hours 8, idle_weight, changeover_weight, base_changeover_weight, early_kg_week_weight, early_fill_hours, time-limit split) was refuted; see section 3. The only config-only items are reporting fixes, grouped under C6a.

### DO FIRST: make pass 1 start from the seed (code; ship C1-C3 together)

**C1. warm_start.py:119-121.**
- **Change.** In build_hint_plan, for a present pair without a split hint `seg_b_start = seg_b_end = seg_a_end` instead of 0. Leave the EMPTY dict at L187-190 alone: absent pairs are not gated, and the all-absent anchor is OPTIMAL.
- **Effect.** The hint stops breaking model_builder.py:1071-1079 on every gated line.
- **Risk.** None; a hint is advice. It also affects every other scenario that uses apply_warm_start.
- **Verify.** A fixed-hint anchor (strategy cleared, 1 worker, `fix_variables_to_their_hinted_value=True`) returns FEASIBLE/OPTIMAL on the repaired seed: 2.6-2.7 s, 3,687,723 kg. The script is scratchpad/seed/seed_exp.py in anchor mode.

**C2. greedy_fill.py:175-176 and :198** (alternative: scenario_runner.py:1520-1525).
- **Change at L175-176.** At a fresh segment that begins at the gate, charge `setup_h(initial_sku, sku) + long_shutdown_extra`, mirroring model_builder.py:1255-1265. Alternatively, start the first free segment at gate + that setup in `_greedy_seed`.
- **Change at L198.** Take the setup gap from the block physically to the left of the placement, not from `st.tail_sku`, and re-check the gap owed to the block on its right.
- **Effect.** The 8 violating rows become feasible. The seed loses at most 1-2 h per affected line (at most 12 t).
- **Stopgap.** The finder's 12 t trim (scratchpad/seed/seed_repair.py) is equivalent but not a production fix.
- **Verify.** scratchpad/seed/seed_check.py reports 0 violations and the anchor is OPTIMAL. Add a regression test: build the model on a gated fixture, apply the greedy seed plus warm start, and assert the anchor is FEASIBLE.

**C3. phase2_scheduler.py: add a pass-1 anchor** after the warm-start block (about L2465) and before the pass-1 CpSolver (L2495).
- **Change.** Copy the pass-2 pattern at L2650-2681:
  1. Back up and clear `search_strategy` (C80).
  2. Solve with ANCHOR_WORKERS, `max_time_in_seconds = min(120, tl)`, `fix_variables_to_their_hinted_value = True`.
  3. On FEASIBLE/OPTIMAL, call `ClearHints()` and install `resp.solution` as the complete hint, then log the seed's kg and co_load.
  4. Otherwise log loudly: "seed violates the model; pass 1 runs cold".
  5. Restore the strategy.
- **Optional.** Apply `repair_hint=False` and `ignore_subsolvers 'fixed'` as pass 2 does (not measured on pass 1).
- **Expected effect on this board** (C1+C2+C3, measured at 300 s, pass 1 only):
  - First pass-1 solution about 3.69 Mt at about 31 s (today 0 kg at 28.6 s).
  - 4.05 Mt at 300 s.
  - 4-h blocks 26 -> 5, blocks under 8 h 39 -> 18.
  - qty_min shortfall 377 -> 197-200 t; orders under qty_min 27 -> 23-24.
  - co_load 37,690 -> 33,985.
  - Idle 1,240 -> 1,127 h, redistributed (P14/P16/P22 get worse).
- **Risk.**
  - One stochastic run, pass 1 only. Pass 2 on top was never measured and could re-fragment some runs.
  - Over-target kg rise 102 -> 126 t.
  - If the strategy is not cleared, the C80 CHECK crash; the pass-2 guard pattern covers it.
- **Verify.**
  1. Re-run Scenario F on the same snapshot (copy of data/_scenario_work/F, production staging, 1200 s per pass).
  2. In solver_progress.json, the first pass-1 solution carries about 3.69 Mt at about 30 s.
  3. From schedule_phase2.csv / produced_vs_bounds.csv, count exactly-4-h blocks (expect about 5), blocks under 8 h (about 18), orders under qty_min (about 23) and idle per line.
  4. Pass-2 final placed kg must be at least the board's 3,825,533 kg.

### THEN

**C4. Post-pass-2 fill-repair neighbourhood solve** (phase2_scheduler.py, after the pass-2 adopt block, about L2700-2790; code pattern in scratchpad/idle/probe_extra.py build()).
- **Change.** Fix every assignment of the adopted plan. Free only three kinds of (line, order) pairs:
  - (a) orders below target on lines that have an idle window of at least min_run_hours;
  - (b) CIP-split stub segments whose gap could host a short order (P18 280452-W2, P14 280478-W2), letting the sibling segment stretch;
  - (c) early-filled W2/W3 assignments in W1 time on P09/P12/P15 where a W1 order is short (enables the 280581-W1 / 280103-W2 two-move).
- **Solve.** 30-60 s, 2 workers, pass-1 objective, then re-run the independent validator.
- **Effect measured on today's board** ((a) only, plan fixed, OPTIMAL in 4-8 s):
  - +305.7 t raw / +271.3 t toward target in 264 h; orders under qty_min 27 -> 18.
  - co_load +4,210 (at most about 7 FFS-equivalents, about 14 t-eq).
  - Idle closed on P11, P12, P17, P18, P19 and P20; about 980 h remain. P14, P16 and P21 are untouched.
  - Dropping the stubs as well gives +309.8 t.
  - The two-move adds about +22.4 t for 280581-W1 (W1 orders short of qty_min 10 -> 9).
- **Expect a smaller gain after C1-C3** (the seeded pass 1 already fills P18/P19/P20); not measured.
- **Risk.**
  - Removes none of the 4-h blocks, because they are fixed.
  - Adds 1 independent-validator CIP_REQ_MISSING (P12 280605->280436, gap [403,409)); the board already has 2.
  - DEMAND_BOUNDS warnings 29 -> 37.
  - About 34 t of the gain lands over target.
  - 280605-W2 stays short.
- **Verify.** Run on a copy of the work dir after C1-C3. Compare produced_vs_bounds.csv, idle after gates by line and validator output against the C3 run.

**C5. Split-only share floor under soft demand** (slivers-5; model_builder.py:856-869 and :1018, about 15 lines).
- **Change.**
  - Per order, `n_lines_o = sum(present[(l,o)])` (already built at :1018), plus a BoolVar `split_o <=> n_lines_o >= 2`.
  - Enforce `run_h[key] >= ceil(pct x qmin_clamped / r)` with `OnlyEnforceIf([present[key], split_o])`, instead of zeroing it at :858-859.
  - Set pct = 0.25 for F only, via the F overrides (scenario_runner.py:345; override keys at :110-111/229/240), so Scenarios A-E keep `min_run_pct_of_qty = 0.5`.
  - Update solver_rules.py:318-332 and tests/test_fix_SA.py::test_soft_demand_applies_only_min_run_hours_so_partial_fills_split.
- **Effect on today's slivers.**
  - At 0.25, 14 of the 18 second-line slivers become infeasible. Survivors: 120532-W3 on P12, 280572-W0 on P13, 280490-W0 on P16, 280592-W0 on P17. At 0.5, all 18.
  - No W3 stub, CIP-split piece or sole-line partial fill is touched, and no order can become unplaceable.
  - Every real split already meets its floor (e.g. 280592-W1 on P21 runs 20 h against a 10-h floor at 0.5; 280324-W2 on P22 118 h against 65 h).
- **Seed compatibility.** The greedy seed's 0.5 floor on every line is stricter, so the C1-C3 hint stays feasible. No change to greedy_fill is needed unless pct is above 0.5.
- **Risk.** The 43.5 t counted in slivers comes back only if the search consolidates, which is why this change comes after C1-C3. The solver may answer with 8-10 h second pieces instead of none. It does nothing for idle.
- **Verify.** Rebuild the model (about 2 s) and confirm the new constraints. Solve and check four things:
  - blocks under 8 h whose order is on 2 lines: 0-4;
  - no order at 0 kg that had kg in the C3 run;
  - placed kg not below the C3 run;
  - exactly-4-h blocks below the C3 run's 5.
- **Soft alternative** (slivers-6, partly refuted on impact). A per-extra-line cost X added to pass 1 (secondary, :2280) and pass 2 (:2321).
  - X = 5 t-eq removes 16 of 18 slivers on first-order arithmetic.
  - The safe band is narrow: 5.0-6.09 t-eq once W0 kg carry their 1.03 weight.
  - It leaves the two 5,816-kg P17 pieces and was never solved.

**C6. Reporting fixes (trust; the board does not change).**
- **C6a, config-only.** Raise `[scorecard] short_run_h` in flowstate.toml:130 (it is staged into the work toml at L50). With the current strict `<`:
  - 6.0 gives short_campaign_count 0 -> 24.
  - 8.0 gives 30, equal to cap_short_runs 30, so that part scores 0 and campaigns land at about 50.
  - The code alternative is `<=` at scorecard_engine.py:1605 and :1620 with 4.0 kept, which gives 21.
  - Risk: saved scorecards are not comparable across the change.
  - Verify: re-score version 26_09_16_09_23.
- **C6b.** In compute_idle_kpis (phase2_scheduler.py:1225-1300, span at :1261), set span = H - gate - committed windows after the gate. The KPI becomes 1,240 h on this board, equal to the board. Verify by recomputing on schedule_phase2.csv.
- **C6c.** In score_service (scorecard_engine.py, about L1813-1874), pro-rate a partially covered last week by covered_h/168 as rebase_demand does. Verifier recompute: orders_unfilled 46 -> ~19, orders_late 68 -> ~41; confirm by re-running the scorecard. Drop this if C7 is adopted, since the frames then agree anyway.
- **C6d.** Report idle as "fillable" vs "demand-limited" (capability x due window x mlpo bound), so about 820-900 h is not read as a solver failure.

### LATER (each needs a planner decision or a measurement first)

**C7. Stage the full ISO-41 target with a pro-rated floor** (horizon-2; plan_fill.py:440-467 clipped branch).
- **Change.** Keep the full-week `qty_target`/`qty_max`, write an explicit `qty_min = frac x lower_pct x full`, and blank lower_pct/upper_pct (data_loader.py:634-646 prefers the pct columns).
- **Effect.**
  - Model size unchanged (100,808 vars).
  - +1.33 Mt of eligible demand for the same 114 orders; the 21 W3 orders under 10 t grow 3.5x, which should clear most of P12's W3 stubs.
  - Up to about 1,100 h of tail idle becomes fillable (701 h on P16-P22). This is an upper bound: the cap binds on only 2 of 35 orders today.
- **Risk.**
  - Reverses the documented C20 pro-rating rule (solver_rules.py L711, plan_fill.py:443-448).
  - Pre-builds inventory up to 2.5 weeks early; excess_inventory_kg must be shown.
  - The next run's ledger must credit the pre-built kg (not verified).
  - Pays only after C1-C3.
- **Verify.** build_size.py shows unchanged size. Then a 1200-s run on the same snapshot: W3 produced (today 424 t), idle on P16-P22, excess inventory.

**C8. Sequence hint experiment** (seed-6).
- **Evidence.** With presence plus the seed's block end hours fixed, 1 worker reaches 3.79 Mt in 30-60 s, against 3.66 Mt for pass 1 at 1,200 s on 8 workers. Fixing presence alone gives 1.67-1.72 Mt.
- **Experiment.** Hint the `succ_`/`first_` booleans from the seed's time order (model_builder.py:1231, :1332-1335) as a hint only, with presence free and pass 2 on top.
- **Caveat.** Never fix the sequence: that would keep the seed's 1,488 h idle and its 38-40 orders under qty_min.
- **Verify.** The same 60-s single-worker probe, with rows saved.

**C9. Re-tune the time budget only after C1-C3** (seed-5, refuted on impact).
- **Measure first.** Pass 1 from the seed beyond 300 s has not been measured.
- **Why not cut pass 2 now.** After its co_load minimum (34,655 at 666 s), pass 2 still bought 61.6 M units (~61 t-eq) of fill.

**C10. 4-week horizon** (horizon-4): defer.
- **If tried.** Set both `horizon_weeks = 4` and `horizon_hours = 672` (hours wins), and add 504.0 to `LEGACY_HORIZON_BOUNDARIES` (downtime_horizon.py:38).
- **Cost.** +51 % vars (151,838) on a hand-built staging; the audit's real staging path gave +66 %.
- **Projected effect.** Total idle about 1,960 h over 672 h: it moves to the new tail.
- **No evidence under the hard policy.** Only soft-policy benchmarks exist, where the search got worse (first incumbent 111.8 s, 113 solutions).

---

## 3. What NOT to change, and why

| Lever | Why not (evidence) |
|---|---|
| `min_run_hours = 8` (slivers-7) | Makes 5 W3 orders that produce today unplaceable (120451, 120453, 120536, 280377, 280379: 16.4 t lost). Forbids the 7 zero-cost CIP-split pieces. Turns 4-h second-line slivers into 8-h ones. A clamp `min(min_run_hours, floor(qty_max/r))` brings the small W3 stubs back at 4 h. |
| `max_lines_per_order = 1` (slivers-8) | No per-order path (model_builder.py:473-477, :1018). Sliver targets (6.8-177 t) overlap real splits (39-280 t), so no tonnage threshold separates them. Global 1 strips the smaller piece of 8 real splits, about 334 t (at least 100 t lost after re-placement, estimated). |
| `max_lines_per_order = 3` (idle-6, refuted by both lenses) | 280351-W2 reaches target inside mlpo 2 via P15+P19; the seed had it on P19 at 157 t. Mlpo 2 is also the repo-root plant default (flowstate.toml:13). Raising it opens a third sliver line for all 114 orders. |
| Plain pct floor (delete the bypass at model_builder.py:858-859, `min_run_pct_of_qty = 0.25`) | Contradicts the 2026-09-03 soft-demand rule (solver_rules.py:318-332). Leaves sole-line holes empty: 280581-W1 could not use any P09 hole under 18 h. Use the split-only variant C5. |
| `changeover_weight = 3,300,000` in pass 1 (currency-1 R1, refuted by both; currency-6) | The board is pass 2's plan, which already prices switches in that currency, and the slivers survived it. A dearer pass-1 switch leaves more holes empty. It drops fill-first (model_builder.py:2289-2291). Removing the epsilon and order floors risks tonnage. |
| `base_changeover_weight` 5 -> 460 with K pinned (currency-2 R2, refuted by both) | The premise "slivers are the priced optimum" is wrong: relocation moves dominate (e.g. 280581-W1 on P09 h118-122 sits next to 280584-W1, which is 18.6 t short). Only 12 of 32 slivers stop paying, with 17.8 t at risk. An FFS switch becomes 3.5 t-eq, breaking "one FFS change = 2 h of a 1,000 kg/h line" (flowstate.toml:62-63, solver_rules.py:658-674). |
| Raising `early_kg_week_weight` (currency-5; pricing half of idle-4) | A 2-week-early kg only turns negative at 500,000 or more (50 %/week). The P15 280612-W1 swap flips only there and still loses about 6 t net. Contradicts data_loader.py:120-127 and the 2026-09-04 decision (flowstate.toml:59-76). |
| Bounding `early_fill_hours` (e.g. 168; slivers-11) | Reverses the plant decision of 2026-09-04 (flowstate.toml:30-40, unbounded early fill, right week soft). At 168 h it would move only 120536-W3 and leave more idle. |
| `idle_weight = 0` or a gap-based idle term (currency-3/-4, refuted on impact) | The whole idle term is worth 3.8 kg-eq today; removing it changes nothing. The 101.7 t over-fill is a neutral tie, not idle-driven. A gap-based term with unpriced tails would push idle into one big tail block and reward slivers that sit inside holes. |
| Negative `over_target_reward_pct` | Would shorten long runs (e.g. 280480-W1's 64/72 h) and add idle. Short orders already earn 1e6 units per kg, so it creates no new incentive to serve them. |
| Deleting the class a+b slivers outright (slivers-2 expected effect) | Loses 61.5 t of counted fill. Conflicts with unbounded early fill for the 4 early W3 stubs. |
| Pricing switches at setup hours x rate (slivers-9) | 35 of 39 short blocks stay net-positive (+110.5 t). It double-counts setup time already reserved as a physical gap (model_builder.py:1303-1327). |
| Scheduling other orders between split segments (idle-2 option b) or a 1e6 split price (option a) | Option (b) breaks the per-order successor/changeover web (model_builder.py:1327-1360). Option (a) frees only P14's 27 h, because the P18 stub carries about 2e9 units of counted fill. Use the C4 repair neighbourhood instead. |
| Fill-only ISO-42 rows due at H-1 (horizon-5, refuted on impact) | +53 % vars. Ties with the W3 stubs, since `priority` is never read by model_builder. The larger soft-week model lost 38 % tonnage (flowstate.toml:47-54). Does nothing for the W2 idle, which is search residue. |
| Budget 600/600 now (seed-5) | Pass 2 bought about 61 t-eq after 666 s. Pass 1 from the seed is unmeasured beyond 300 s. The "same 20-min wall" premise is wrong: today's wall is about 41 min. |
| Treating about 960 h of idle as settled plant reality (idle-3/horizon-1 "no solver change", refuted on impact) | At least 245-264 h are fillable by search in seconds. The routing and LP evidence shows part of the "format" idle is allocation. |

Confirmed not defects, so leave them:
- dead_pairs (36 correct);
- the producible-window clamp (never fired);
- stock-policy caps (none present);
- the changeover matrix (complete; setups 0.25-3.75 h);
- K = 33 (derives as documented);
- the late 200,000 / early 50,000 prices (same fraction in both passes);
- `due_week_policy = "hard"` (0 late starts);
- the committed layer (0 fill overlaps in any tested plan).

---

## 4. Open questions for the planner

1. **What is a "too short" run?** A 4-h run is 5.8 t on P17/P18 but about 1.8-3 t on P09/P12/P15. This sets `short_run_h` (C6a) and the split floor (C5: 0.25 or 0.5 of an order's minimum hours).
2. **When may an order split across two lines?** Is it acceptable only when the second line carries a real share (C5), or never for orders under N t?
3. **May the solver pre-build the full ISO-41 week (up to 1.33 Mt) into W2/W3 idle as inventory (C7)?** This reverses the C20 pro-rating rule. Where must the resulting excess inventory be shown?
4. **Is about 820-900 h of idle on P16-P22 acceptable plant reality for a 3-week horizon?** The alternative is looking further out (4-week horizon: slower search, no hard-policy benchmark).
5. **Can SKUs that also run on P16-P22 move off P09-P15 in W1?** That would make room for the about 100 t of small-format W1 shortfall (280581-W1 51 t, 280578-W1 16 t, ...), even at extra changeovers or a slower rate. Is P11 really down until h264 (Sep 27)?
6. **Is a TTP-only swap (0.18 h average) nearly free for the plant, as the solver prices it (33 kg-eq), or about a quarter of a topload change, as the scorecard counts it?** This decides whether any per-switch overhead is ever wanted; it is not recommended now.
7. **Interim option until C1-C3 ship:** is the greedy seed board preferable to the solver board? It has 0 four-hour blocks and 3.70 Mt, but about 1,488 h idle and 38-40 orders under qty_min (381-393 t short).
8. **Should the service score judge the stub week at its pro-rated share (C6c)?** If so, orders_unfilled 46 -> ~19 and late 68 -> ~41 on this board.
