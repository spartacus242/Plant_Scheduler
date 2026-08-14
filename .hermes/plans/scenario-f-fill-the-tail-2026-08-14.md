# Scenario F — committed plan fixed, fill the tail (design, 2026-08-14)

**Status:** APPROVED DIRECTION (user process description 2026-08-14 + two decisions below). Build next.
**Supersedes** Scenario E as the daily driver once verified; E remains available as a diagnostic ("what would a full re-optimization look like").

## The user's process (verbatim intent)

> The calendar should start with what is in manprg and the cip_info files. …
> This is our ground truth. Those are MOs that will be produced by our team,
> and CIPs performed, when they choose to produce them. We're not worried
> about changing the order of those or quantities … By laying those out we
> get an estimated end datetime for all lines from which we can start placing
> orders from the demand planning. CIPs should be shown based on the
> previously performed CIP and the max hrs between CIP, propagated forward
> through the 3 week window. Then optimize placement of demand_plan_summary
> orders, trying to produce the requested tonnage on the correct ISO week.
> Never leave large blocks of idle time at the end of an order or week …
> Once this has been working, 2 weeks of MOs live in manprg and we just
> continuously fill weeks 3–5.

## Decisions (user, 2026-08-14)

1. **CIP vs committed MO collision → CIP splits the MO** (the existing
   current-state rebuild behaviour: CIP wins, MO splits into before/after on
   the same clock; a CIP that would swallow an MO whole is dropped with a
   warning). No MO sliding, no reordering, no re-quantifying — `mo_changes`
   is NOT part of Scenario F.
2. **Completed MOs shown greyed + immovable by default** (toggle to hide).
3. **Trials come from manprg TRIALS rows only** (already shipped): blocked
   hours, zero tonnage, never solver orders. trials.csv is gone.
4. **ISO weeks everywhere in display** (already shipped): internal W0/1/2
   stays for math, W33/W34/W35 at every surface.

## Architecture

### Layer 1 — ground truth (NOT solver-negotiable)
`helpers.current_state.build_current_state` already produces exactly this:
running MO (locked, estimated end) + queued MOs (manprg order, sequential)
+ TRIALS as trial blocks + CIPs projected from PreviousCIP at the line's
max interval, MOs split around CIPs. Work items:
- extend CIP projection through the FULL horizon (verify it reaches 504h);
- keep completed MOs as greyed/locked blocks instead of dropping them
  (counts already reported);
- per-line **free-from hour** = end of the last committed block.

### Layer 2 — demand fill (the only thing the solver optimizes)
A single-phase solve where:
- every Layer-1 block enters as a FIXED interval (downtime-style windows for
  the solver: committed MOs, CIPs, trials all block their line-time; no
  current_mo.csv orders at all — the entire dispatch-5 adjustable-tonnage
  machinery is bypassed in F);
- orders = demand_plan_summary only, less what the committed MOs already
  produce this horizon (subtract committed kg per SKU/week from the demand
  targets — the plant is already making it; do NOT double-plan it);
- objective = min changeovers (dominant, per charter) + idle penalty
  (raise objective.idle_weight from 0 so tails pack) + week-deviation
  preference (produce on the requested ISO week, pull-forward onto tails
  allowed and encouraged when a week is full);
- CIP intervals stay hard for the fill region (a fill block cannot push a
  line past max dirty-time; the Layer-1 CIP projection provides the anchors);
- stock policy: the agent's DNS trim continues to apply to the fill demand.

### Honesty rules carried over
- An order that cannot reach its ISO-week tonnage is reported short
  (produced_vs_bounds / adherence), never silently dropped.
- Every input the agent touches is logged via the work_dir_patch seam.
- The scorecard comparison for F proposals must be windowed to the fill
  region (the committed layer is identical on both sides by construction) —
  this also resolves the cross-horizon composite unfairness.

## Why this beats Scenario E as the daily driver
- The committed layer cannot be "relaxed away": it is calendar, not model.
  The relax ladder only ever touches fill demand.
- mo_changes/VIF write-back friction disappears from the daily loop (the
  plant's own plan is never rewritten by the tool).
- Solve size shrinks dramatically (fill orders only, fixed windows), so
  solves are faster and land at low relax levels with changeovers enforced.

## Build plan (next session)
1. `helpers/plan_fill.py`: Layer-1 assembly (reuse build_current_state) →
   fixed-window export for the solver work dir (blocks → downtimes-style
   rows) + committed-kg subtraction from demand targets (per SKU × ISO week).
2. Scenario F entry in scenario_runner (single-phase, no current_mo.csv,
   patched demand, idle_weight override).
3. `agent_propose.py --scenario F` becomes the default agent action.
4. Calendar: greyed completed MOs + "Start of day" rebuild defaults to the
   Layer-1 view.
5. Tests: committed-kg subtraction math; fixed-window export; a probe that
   asserts NO committed block moved between input and proposal.
6. A/B vs Scenario E on the live dataset; browser-verify; user review.
