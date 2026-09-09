# Rate dialect A/B — 20260909-0927

Scenario F, budgets 240/600 s, sequential, identical staged inputs.
Frame: anchor 2026-09-09 00:00:00, 504 h; net demand 4,254,583 kg; capacity bound flat 4,930,671 kg / measured 4,657,896 kg; capable-row rate sources {'model': 383, 'measured_recent': 287, 'measured_alltime': 94}.

## overnight_score v1 (flat-bound convention, comparable to nightly leaderboards)

| arm | composite | fill | changeovers | campaign | on_time | gap % | wall s |
|---|---|---|---|---|---|---|---|
| flat | 69.37 | 69.66 | 67.86 | 73.95 | 67.04 | 45.5 | 855.5 |
| calc | 71.21 | 69.87 | 71.68 | 81.66 | 63.38 | 44.4 | 855.1 |
| measured | 70.14 | 73.05 | 65.95 | 73.54 | 67.33 | 38.6 | 855.1 |

## Re-pricing: solver-placed hours valued under each dialect (kg)

| arm | placed h | planned kg | at flat | at calc | at measured | promise error vs measured |
|---|---|---|---|---|---|---|
| flat | 3,106 | 3,071,877 | 3,071,877 | 2,979,103 | 2,904,432 | +5.8% |
| calc | 3,103 | 3,047,585 | 3,141,259 | 3,047,585 | 3,012,755 | +1.2% |
| measured | 3,177 | 3,184,997 | 3,304,461 | 3,200,128 | 3,184,970 | +0.0% |

## Same schedules scored with the measured-rate capacity bound

| arm | composite | fill |
|---|---|---|
| flat | 69.37 | 69.66 |
| calc | 71.21 | 69.87 |
| measured | 70.14 | 73.05 |
