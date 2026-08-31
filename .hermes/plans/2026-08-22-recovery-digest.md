# Recovery digest — 2026-08-22

Written after the Claude desktop app was uninstalled/reinstalled (~01:00 on
2026-08-22; the machine also rebooted at 00:47). The app's session list was
reset, but **nothing was lost**: every transcript, every commit, and the memory
notes are intact on disk. This file is the map back to all of it.

## Where the chats are

`C:\Users\jbdil\.claude\projects\C--Users-jbdil-Flowstate-Plant-Scheduler\<session-id>.jsonl`

| Session id | Span | Size | What it covers |
|---|---|---|---|
| `71f460ab-ab30-4ad2-b891-3f662d107fa2` | Aug 13 → Aug 19 16:34 | 24 MB, 62 turns | Knowledge-base review → P1–P4 build → Scenario F → netting ledger → E2E walkthrough → planner batch → "Continuously Optimize" design |
| `c84ce759-1a5e-43ff-8115-7261c5fc516f` | Aug 18 → Aug 21 16:59 | 5.7 MB, 17 turns | Continuation: overnight optimizer build, Task Scheduler, seed diversity, W35 label, master-file ledger, Stock Check ISO weeks, wall-clock downtimes |
| `4677a035-a46a-440a-8dbd-b445fde8996f` | Aug 19 17:02 | 0.4 MB, 1 turn | The Hermes overnight-monitor bot prompt (`2026-08-19-overnight-monitor-bot-prompt.md`) |

Older Aug 12–13 sessions are in the same folder. To reopen one from a terminal
in the repo: `claude --resume <session-id>`. The desktop app's own index lived
in `%APPDATA%\Claude` and was recreated empty by the reinstall.

Memory notes (8, indexed by `MEMORY.md`) are intact:
`…\C--Users-jbdil-Flowstate-Plant-Scheduler\memory\`.

## Repo state at recovery

- `develop` @ `689714f` == `origin/develop` — everything is pushed. 219 commits
  ahead of `main` (main frozen by design). PR #5 was open per the last session
  (`gh` is logged out after the reinstall, so not re-verified).
- Suite re-run 2026-08-22 10:45: **528 passed, 7 skipped, 8 deselected** — the
  same count the last session closed on.
- **Uncommitted, data only** (your Aug 21 "roll to today, rebuild from live"):
  `flowstate.toml` planning_start_date 08-19 → 08-21; `data/calendar_blocks.csv`;
  `data/versions/naive_demand_plan/{calendar_blocks.csv,metadata.json}`.
  Commit as a `data: live board state` commit when convenient, or leave.
- **Untracked:** `scripts/fs-live-pull-task.bat` (+ its `.log`) — the wrapper the
  `flowstate-live-pull` Task Scheduler job runs every 30 min;
  `.hermes/plans/2026-08-19-overnight-monitor-bot-prompt.md`; this file.
- **Unmerged branch:** `claude/eager-pike-71ad96` (`79d0597`, Aug 18, "sweep the
  collapsed-expander grid trap across every page" — adds `helpers/st_compat.py`
  with `deferred_dataframe()`). Never merged: develop has no `st_compat.py`; the
  E2E batch (`1e23f27`) fixed only the Home data-status instance with `st.table`.
  It now conflicts with develop. Checked out in worktree
  `.claude/worktrees/peaceful-spence-f16d43`. Revisit if the empty-grid trap bites
  on another page; otherwise delete.

## Latest edits — newest first

**Aug 21 afternoon (16:37–16:56) — the last three merges** `8d8a4a7` `c2714b2` `689714f`
- `2ccbfc4` ledger pro-rates committed kg across ISO weeks by time-overlap share
  (was dumping a block's whole tonnage into its midpoint's week → phantom 1.91M kg W35)
- `d399ef7` master-file architecture: week cards are board-only headlines; kg
  already made by completed MOs is a separate "+Y% already made" addend
- `3cedd36` `94e92cb` Stock Check: week_index↔ISO mapping anchored to the demand
  plan's source.json (the WW07 bug = unanchored `week_label` fallback); dropdown
  floored at current week, (year, week) sort; DQ tab says where to fix
- `d5fba42` `5786722` `330282b` wall-clock downtime store: outages stored as
  absolute datetimes, every consumer reads through the store, `downtimes.csv`
  removed from the nightly bridge pull so planner edits survive
- `db1f96f` test: Gantt downtime overlay invariant across a simulated roll

**Aug 21 midday** `66a05af`
- `76691e2` week axis: labels never painted over (the missing-W35 bug),
  boundaries on true ISO Mondays; `1fc2ac3` week cards show board vs
  committed/made split + freshness caption
- `230118b` supervised runs (budget overrides) finalize immediately, no 5am wait;
  `e28c409` real-generations regression pins its latest.json pointer

**Aug 21 morning** `53510c2` `1c3a059`
- `6b81731` CP-SAT random_seed wired end-to-end; `1f00a75` seed-diversity
  portfolio (champion_n1/n2, seed_2/3, cold_start, chained_best) + `--pass1-s`/
  `--pass2-s` budget flags; `8c33cce` chained arm re-solves the donor's exact
  staged problem; `738d259` `9992188` same-generation deltas only in chip,
  leaderboard, brief, publish notes

**Aug 19 evening** `d6a962b` `718154c`
- Overnight batch optimizer Phase 1: `39d0836` overnight_score v1 (frozen
  composite 40/30/15/15), `25c0714` batch engine + Task Scheduler registration,
  `ce416a1` `579de86` Home chip + Generate leaderboard, `26ca106` own work root
  `data/_overnight_work`, `cb944d9` no "best nan", `718154c` under-filled arms
  stay on the leaderboard but never publish
- `f465be5` `d2515f5` re-forecast running MO ends from the ACTUAL observed rate;
  `38ba75a` Gantt tooltip says when the actual rate moved the end

**Aug 19 daytime**
- `e567759` accurate DnD landing + translucent snap ghost
- `a79b7ce` Data Files split into planner inputs vs auto-maintained; freshness in
  every header (AZAP file removed from the list)
- `83ea56b` `aebc9ba` over_target_reward_pct honest 0–5 % knob (default off);
  planner-facing live solver narration
- `1e23f27` 16 review fixes (on-grid placement, holding adoption, overlay guard,
  read-only gating, unknown-pair `?` chips); `9683c50` PROGRESS.md updated
- `1d9a1fe` `e3d03fa` `2755ddf` blank-space SKU picker; solver rulebook + every
  weight verified; CIP count reported-only; timestamped scenario versions +
  auto-evict; Compare/Generate caching; Export button in Lock & Export

**Aug 17–18** weekly scorecard breakdown, live solver progress bar, time-frame
reconciliation on promote/load, holding = OFFICIAL board, planner batch on the
Gantt (fill buttons, immovable MOs, week cards above the chart), true full-width
layout, drag auto-scroll.

`PROGRESS.md` (last updated `9683c50`, Aug 19) has the narrative through item 11
(planner batch); the Aug 19 PM → Aug 21 work above is not yet in it.

## Overnight optimizer — what happened last night

- Task **"Flowstate Overnight Optimizer"** is registered (daily 19:00, next run
  tonight). **"flowstate-live-pull"** runs every 30 min, last result 0.
- The 19:00 run on Aug 21 staged gen `20260821-1900-5d421b01` against the
  corrected ledger and finished all six full-budget arms (600/2400 s):

  | arm | composite | fill | co | campaign | on_time |
  |---|---|---|---|---|---|
  | **seed_2** | **69.38** | 72.85 | 71.34 | 74.6 | 50.97 |
  | champion_n2 | 67.72 | 71.66 | 69.19 | 71.69 | 50.32 |
  | chained_best (from seed_2) | 67.60 | 73.09 | 68.08 | 72.28 | 47.33 |
  | seed_3 | 67.39 | 72.04 | 68.2 | 73.54 | 47.22 |
  | cold_start | 66.54 | 73.0 | 63.73 | 71.03 | 50.45 |
  | champion_n1 | 65.54 | 70.98 | 67.61 | 65.6 | 46.84 |

  Noise spread 2.18 (n=2). `board_baseline` is null — "the official board has no
  comparable fill region". Candidates are on disk under that gen's `candidates/`.
- **The 00:47 reboot killed the process while it was sleeping until 05:00**
  (Task Scheduler last result `0xC000013A`). So: no `champion_5am`, nothing
  published, no brief; `data/optimizer/latest.json` still points at the 10:47
  supervised test gen, which is what the Home chip / leaderboard show.
- Gap surfaced: the batch has no resume / consolidate-only mode, so a reboot
  mid-wait loses the night. Tonight's run will start a fresh generation.
- The Hermes bot profile `flowstate_overnight_monitor` is running (processes up
  since 10:27).

## Where the last session ended

All five Aug-21 issues fixed, merged, verified live, pushed: master-file
architecture restored (W34 "59.6 % on board", W35 "36.2 % on board", holding
column 2 → 20 cards), Stock Check weeks, DQ "where to fix", wall-clock downtimes
(P11 ends 2026-08-24 00:00 restored), downtimes.csv out of the bridge pull.
Expectation at sign-off: "tomorrow's brief will be the first fully honest one" —
that brief is the one the reboot ate.
