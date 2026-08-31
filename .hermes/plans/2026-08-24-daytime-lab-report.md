# Daytime Optimizer Lab — 2026-08-24

42 arms in one afternoon (14:19–18:05), four supervised `--no-publish` runs on a stable frame
(board baseline 66.93–66.96, net demand 3.84M kg, capacity bound 4.82M kg). Question: what setup
finds good schedules *consistently*? Steering: `data/optimizer/steering.json` (params `{}` = champion
replicate arms; `pass2_s: 1` = pass-1-only "screen" arms — 1s pass-2 goes UNKNOWN, pass-1 kept).

## Tier results (composite, overnight_score v1)

| Tier | n | mean | sd | med | max | craters (<45) | min/arm | best per compute-h |
|---|---|---|---|---|---|---|---|---|
| FULL 120/300 (gens 1418+1504) | 18 | 53.1 | 14.7 | 55.3 | 72.28 | **6** | 6.9 | 34.7 |
| SCREEN 120 (gen 1633) | 12 | 59.5 | 6.8 | 60.9 | 69.79 | 0 | 2.4 | **145.0** |
| **SCREEN 240 (gen 1706)** | 12 | **69.0** | 7.9 | 70.8 | **75.65** | 0 | 4.6 | 82.9 |
| NIGHT 600/2400 (3 nights hist.) | 18 | 67.8 | 1.0 | 67.8 | 69.64 | 0 | 50.5 | 4.6 |

- 240s screens beat the historical full-budget night mean **unpolished**, in 1/11th the time per arm.
- Paired 120→240 screen: **+9.46 mean, 11/12 wins** — almost all fill (+17.4) and on_time (+19.8).
- Paired full-vs-screen at pass1=120: pass-2 added co polish (+9.6) but was **net −11.5** today and
  introduced every crater's final freeze. Pass-2 at 300s is not safe as configured.
- Bootstrap best-of-N from the 240 tier: N=6 → 74.6, N=10 → 75.1 (saturates at sample max 75.65;
  true tail unknown). Current 6-arm night: 69.0. A 10h night fits **~131** screen-240 arms vs 11.9 champion arms.

## Crater mechanism (forensics verdict)

- ~~Pass-2 gutting fill via a net-score floor~~ **REFUTED** (0.9): `prod_score` is pure capped
  production (model_builder.py:1445-46); pass-2 hard-holds ≥99% of it (phase2_scheduler.py:1950-51).
- **CONFIRMED primary (0.85): pass-1 ramp-start latency.** First incumbent lands 28–70s (once 186s);
  at 120s an unlucky draw is pre-ramp — either relax-ladder escalation burns 120s/level (walls
  262/288/382s, gaps 578–1186) or a barely-started level-0 ramp gets **frozen** by pass-2
  (gaps 14–28 = trivial convergence over a near-empty plan). Craters = 7–12 new blocks / 237–529 new h
  vs healthy 39–64 / 2100–2550 h. **240s clears the cliff** (0 craters, min fill 44).
- CPU starvation (llama-server 15:45–~16:30): amplifier only — 3 of 6 craters predate it.
- In-flight crater detector: `gap_pct_end` > 100 or wall ≠ ~433s. Wishlist: persist relax_level +
  pass-2 adoption into scorecards.

## What died today

- **chained_best: 0-for-8 lifetime** (mean −6.05 vs donor, worst −26.4). Strictly dominated — the slot
  should become another independent draw.
- **Seed identity**: within-label range (26.0) ≈ between-label range (29.6). seed_2 was 72.28 then
  56.32 same-day. Seeds = independent draws, not identities.
- **n=2 noise floors**: the 120/300 tier's "floor" read 27–30 because replicates straddle the crater
  mode. Bimodal tiers need replicate counts, not max−min.

## Tonight (steered, preflight GO)

11 arms from 19:00:02, done ~03:12, publish ~05:52: 6 defaults @600/2400 + `split_600_600` +
`split_1500_1500` + `p1_only_600` (600/1 full-budget screen) + seeds 5/7 @600/2400.
Answers pending: pass-2's marginal value at safe pass-1 budgets (600/600 vs 600/2400 vs 600/1),
whether 1500/1500 beats 600/2400, and the 240-tier story replicated at 600s.

## Proposed engine redesign (needs code — tomorrow's decision)

**Screen-many → polish-few funnel:**
1. Night = ~N×240s pass-1 screens (N≈40–100), scored by overnight_score as-is.
2. Discard sub-floor screens (fill floor), rank the rest.
3. Polish top 2–3 with a **locked-fill pass-2 mode** (floor from the donor schedule, skip pass-1
   re-solve) — does not exist yet; the chained mechanism re-explores and is the wrong tool.
4. Keep 2 champion replicates for the noise floor; retire chained_best; keep publish rails unchanged.
Open question tonight answers first: how much composite does pass-2 polish add at safe budgets —
if `split_600_600` ≈ `600/2400`, polish is cheap; if `p1_only_600` ≈ either, polish is optional.

---

# Night results (2026-08-24 19:00 → 08-25 05:50) — appended 08-25 morning

**Published: champion_n1 76.96, honest same-gen Δ +9.92 (record; prior best +1.05); runner-up 76.09.**
Frame was friendlier (capacity bound +11.7% vs 08-23, board weakened to 67.04) — most of the 76-78
level is headroom + softer baseline. Replicate spread at 600/2400: 0.87 (tightest ever measured), but
same-frame seed draws spread 7.04 — variance is draw-driven, ~8× nondeterminism.

**Budget verdict (post-midnight gen, all guards clean):** 600/600 → 67.93 (1221s), 1500/1500 → 68.44,
600/2400 draws → 65.18 / 72.22. No composite budget effect distinguishable from draw variance;
600/600 matches at 40% of the wall. BUT `p1_only_600` (600/1) collapsed to fill 37.18 — in this draw
pass-2 added *placement*, not just polish (n=1; afternoon 240s screens hit fill 75-83 without pass-2,
so treat as tail evidence, not a mean).

**CIP-guard bombshell (forensics CONFIRMED, walk reproduced exactly):** the night's two best schedules
(cold_start **78.24**, seed_2 **77.98**) were publish-blocked by **phantom violations**. The 20:16 live
pull moved P15's clean from ScheduledCIP (drawn as a block) into PreviousCIP (history, never drawn);
the guard's calendar-only clock (seeded "clean at anchor", scorecard_engine.py:863) then read 161.6h
dirty where the true clock since the 17:38 clean was ≤144.0 — zero real violations; champions passed
on staging vintage (staged pre-pull) and were 18 filled hours from tripping too.
**Minimal fix:** seed the overdue walk's per-line `last_cip_end` with `max(0, hours(PreviousCIP − anchor))`
via `cip_import.read_cip_info` (scorecard_engine.py:863 and :831). Do NOT materialize PreviousCIP as a
block (overlaps committed MOs). Also: line_cip_hrs.csv lacks P18-P22 (fallback coincidentally matches).

**Publish pooling flaw bit again, both directions:** floor (69.50) set by a stale-frame fill barred
nothing wrongly this time, but both slots went to stale-frame raw composites (+9.92 vs board) over the
fresh-frame arm at +15.47-vs-its-board; the published calendar was solved against 3.86M kg demand and
re-based into a frame now carrying 5.70M kg (horizon roll pulled in 37 orders).

# Night 4 (2026-08-27 → 08-28): δ priced, study complete

Published: champion_n1 71.71 (+19.65); runner-up d_240_2400_c 70.91 (+21.03 — 4th straight night the
Δ-own ordering differs from raw-composite publish). Boards: 52.06 / 49.88 (decay series
67.04 → 56.96/55.51 → 53.5/51.55 → 52.06/49.88).

**δ experiment (4 paired arms, same frame): 240/600 loses to 240/2400 by +3.41/+3.69/+4.74/+6.78,
mean +4.66 — decisively above the ≤1 unlock threshold. The wide cheap-arm portfolio is dead.**
Decomposition: mean fill δ only +0.93; changeover δ +7.06 — pass-2 at 600s doesn't have time to
polish. Bonus finding: champion_5am at full 600/2400 (66.29) landed BELOW all four 240/2400 arms
(69.52–70.91, spread 1.39) in the same generation. chained 1-for-12.

## FINAL SYNTHESIS (5 nights + daytime lab, ~120 arms)

1. **Optimal known arm: 240/2400** — pass-1 flat 240–600 (n=6 across 2 nights), pass-2 must stay
   full (δ +4.66 at 600s), wall 44 min vs 51 (+14% arms/night).
2. **Arm count is saturated** — fixed-init spread σ≈0.8–1.4; doubling arms buys +0.1–0.4 (sub-noise).
   Retire the seed lottery framing: replicates and cold cover it.
3. **chained: 1-for-12** — drop; cold_start stays (led/co-led 3 of 5 nights, beats champion mean).
4. **Publish by Δ-vs-own-board** — 4 straight nights of ordering flips; boards drift ~2/night so raw
   composites confound board decay with optimizer lift.
5. **The real frontier is BOARD FRESHNESS, not the portfolio.** Published candidates beat the live
   board by +15 to +21 every night while the board decays ~1.5–2/day (staleness tax) plus
   horizon-roll demand cliffs. Every portfolio tweak above is worth ≤1 point; a promoted board is
   worth ~20. The bottleneck moved from optimization to ADOPTION (Promote cadence) — surfacing
   Δ-own on Home/Compare and making promotion easier/more trusted is the highest-value work left.

## Build shortlist for today (in value order)
1. **CIP-guard PreviousCIP seed fix** — one function, unblocks legal 78-class schedules immediately.
   [BUILT 2026-08-25, verified live night-2: zero phantoms across two mid-batch cip_info changes.]
2. **Per-generation fill floor + rank publish by Δ-vs-own-board** — the pooling flaw distorted 2 nights.
3. **Funnel mode**: N×240s screens → fill filter → polish winners (locked-fill pass-2); retire
   chained_best (0-for-9, mean −6.0 vs donor); keep 2 replicates for the floor.
   [REVISED 2026-08-26 — see Night 2: the screen thesis was frame-confounded.]
4. Persist relax_level + pass-2 adoption per candidate (crater/ladder diagnosis without archaeology).

---

# Night 2 (2026-08-25 → 08-26): the screen thesis corrected

Published: **cold_start 71.96, Δ-own +15.00 (new record)**; runner-up champion_n1 70.17 (+13.21).
CIP-guard fix live: 22/22 arms guards-clean despite two mid-batch cip_info changes that would have
produced phantoms under the old code. chained_best 0-for-10 (67.84 vs donor 71.96). cold_start has
now topped the generation two nights straight (cold mean 70.47 vs champion mean 69.61 across 5 nights).

**The frame regime flipped at the 08-25 midnight roll**: cap/demand went from ~1.25 (slack — all
daytime lab + night-1 data) to 0.89–0.94 (overloaded — everything since). The 16-screen sweep
(best 64.25, mean 55.8, 2 fill-collapses + 1 subprocess-timeout crash) lost to the defaults — and the
frame defense fails a same-frame control: **champion_5am ran inside the screens' own generation and
beat their best by 5.05 while also beating them per compute-hour** (Δ-own +13.79 vs screens' +0.28).
Decomposition: 75% of the gap is FILL, not changeover polish — on tight frames pass-2 is placement,
not polish (corroborated night-1: p1_only_600 fill 37.2 vs split_600_600 fill 69.8 at equal pass-1).
Retro-check: even on the slack frame, night-1's six full arms (min 76.09) all beat the best daytime
screen (75.65) — the lab's screen headline compared composites across different boards.

**Build #3 (locked-fill polish) WEAKENED**: its premise — screens find fill cheaply and lack only
polish — is inverted on tight frames, where fill is exactly what screens lack. Locked-fill polish
would freeze the fill deficit in.

**Night 3 settles budget-vs-pass-2** (steering only): 2× 240/2400 (short pass-1 + full pass-2 — the
free approximation of the funnel's polish, since pass-2 hard-holds ≥99% of prod score), 2× 600/2400
in-gen anchors, 4× 240/1, 2× 600/1, 2× 1200/1. Decision rule: 240/2400 within noise (σ≈1.8) of the
anchors → "short-pass-1 champion" portfolio survives (more arms/night at equal quality); 240/2400
clearly below → pass-1 budget is binding, keep 6×600/2400 and spend build effort on #2 instead
(still live: raw cross-gen ranking picked +13.21 over +13.79 for tonight's runner-up slot).
