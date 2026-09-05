# helpers/solver_rules.py — the solver rulebook: every rule that shapes a
# solve, exposed knob or not, as structured data the UI renders and tests
# assert on.
#
# Audited 2026-08-19 against code/solver/model_builder.py,
# code/solver/phase2_scheduler.py, code/solver/data_loader.py and the
# Scenario F staging in helpers/scenario_runner.py. Every "where" points at
# the real source; every value is either resolved live from flowstate.toml
# (via the "config" dotted key) or stated as the hard-coded constant it is.
# If you change a rule in the solver, change its row here — the rulebook
# lying to a planner is worse than no rulebook.

from __future__ import annotations

from typing import Any

GROUP_KNOB = "Adjustable knob"
GROUP_FIXED = "Fixed rule"

# ui: how adjustable this is today / could be.
#   exposed      — already a control in the app
#   easy         — a toml key today; a UI knob would be a small, safe change
#   needs care   — tunable in principle, but interacts with other rules
#   don't expose — safety/structural; changing it belongs in code review
_RULES: list[dict[str, Any]] = [
    # ── Adjustable knobs (custom scenario builder / run controls) ─────────
    dict(
        id="makespan_weight", group=GROUP_KNOB, ui="exposed",
        name="Makespan weight",
        config="objective.makespan_weight", default=1,
        where="model_builder.py balanced branch; flowstate.toml [objective]",
        planner="How much finishing the whole plan earlier is worth. "
                "BALANCED MODE ONLY — min-changeovers and spread-load use "
                "fixed multipliers and ignore it.",
    ),
    dict(
        id="changeover_weight", group=GROUP_KNOB, ui="exposed",
        name="Changeover weight (global multiplier)",
        config="objective.changeover_weight", default=100,
        where="model_builder.py balanced branch; flowstate.toml [objective]",
        planner="Multiplier on the weighted changeover cost. BALANCED MODE "
                "ONLY — min-changeovers hard-codes x100, spread-load x1.",
    ),
    dict(
        id="idle_weight", group=GROUP_KNOB, ui="exposed",
        name="Idle-time penalty",
        config="objective.idle_weight", default=0,
        where="model_builder.py (every objective mode); [objective]",
        planner="Cost per idle hour on a line, measured from the line's "
                "availability gate to its last run (since 2026-09-03 — "
                "before, idle BEFORE the first run was free, so the "
                "cheapest plan always started late). 0 switches idle "
                "tracking off entirely. Works in every objective mode.",
    ),
    dict(
        id="cip_defer_weight", group=GROUP_KNOB, ui="exposed",
        name="CIP deferral reward",
        config="objective.cip_defer_weight", default=10,
        where="model_builder.py (every objective mode); [objective]",
        planner="Reward per hour a CIP starts LATER, so cleans drift toward "
                "their legal deadline instead of hour 0. The max CIP "
                "interval itself stays hard. Works in every mode.",
    ),
    dict(
        id="late_weight", group=GROUP_KNOB, ui="exposed",
        name="Lateness penalty (per hour)",
        config="objective.late_weight", default=200,
        where="model_builder.py soft due-end branch; [objective]",
        planner="Cost per HOUR an order finishes past its due window -- the "
                "smooth tiebreaker inside a week. Under the shipped default "
                "due_week_policy = hard it is active only from relax level "
                "2 (soft due dates). Under the opt-in soft due weeks "
                "(2026-09-04 #2) every demand order carries it at every "
                "relax level, underneath the kg-week price "
                "late_kg_week_weight (which does the real work there: 200/h "
                "on a 168 h week is 33,600 vs 200,000 per tonne-week). "
                "Ignored in cross-week mode.",
    ),
    dict(
        id="week_deviation_weight", group=GROUP_KNOB, ui="exposed",
        name="Early-start deviation cost (per hour)",
        config="objective.week_deviation_weight", default=40,
        where="model_builder.py week_dev (default + cross_week branches); "
              "[objective]",
        planner="Cost per HOUR an order STARTS before its own due_start "
                "(cross-week mode: per hour outside AZAP's week, both "
                "sides). Since 2026-09-04 this is only the smooth tiebreaker "
                "under the kg-week price early_kg_week_weight (40/h vs "
                "50 per kg-week in the pass-2 currency: a 1,000 kg/h line "
                "pulled one week early pays 50,000 per hour of production "
                "from the kg-week term and 40 from this one).",
    ),
    dict(
        id="due_week_policy", group=GROUP_KNOB, ui="easy",
        name="Due-week policy (hard walls / soft weeks)",
        config="scheduler.due_week_policy", default="hard",
        where="flowstate.toml [scheduler] due_week_policy; "
              "model_builder.py effective_due_end / soft_due_weeks; "
              "independent_validator.py DUE_WINDOW (late finish)",
        planner="hard (the shipped default): a demand order must finish "
                "inside its week -- due_end + 1 is a wall at relax levels "
                "0-1, level 2 prices lateness per hour; pre-building an "
                "earlier week stays allowed (early-fill policy). soft is "
                "OPT-IN: the plant asked for it (decision 2026-09-04 #2: "
                "\"Treat the week numbers on the demand_plan_summary.csv as "
                "preferential order, but not necessarily the hard borders "
                "... if a sku has tons scheduled in week 1 and week 3, and "
                "it makes sense to combine all kgs for that sku in one run "
                "in week 2, then that should be explored.\"), but measured "
                "2026-09-04 at 600 s it delivered 38 % less on-time kg than "
                "hard (2.22 / 2.06 vs 3.59 / 3.36 kt) with 463 / 602 t "
                "late, so it ships off. Under soft a demand order's week is "
                "a PREFERENCE in both directions -- it may be pre-built (the "
                "early-fill policy) and it may finish late anywhere inside "
                "the horizon; every kg in the wrong week is priced per kg per "
                "whole week of deviation (late_kg_week_weight / "
                "early_kg_week_weight), so two runs of one SKU merge into one "
                "campaign exactly when the changeovers saved are worth more "
                "than the deviation. All demand tonnage still comes first: "
                "late production always beats no production. Committed MOs, "
                "trials, the line gate, committed windows and the horizon end "
                "stay hard. The validator reports a late finish as ERROR "
                "under hard, WARN (trade-off) under soft. If you opt in, "
                "late 400,000 / early 100,000 is the only tested price pair "
                "that kept every late order within one week.",
    ),
    dict(
        id="late_kg_week_weight", group=GROUP_KNOB, ui="easy",
        name="Late kg-week price (soft weeks only)",
        config="scheduler.late_kg_week_weight", default=200_000,
        where="flowstate.toml [scheduler] late_kg_week_weight; "
              "data_loader.Params; model_builder.py dev_kg_coefficients / "
              "kg-week deviation block",
        planner="Applies ONLY under the opt-in due_week_policy = soft (the "
                "shipped default hard builds no lateness terms). "
                "Cost of ONE TONNE finishing ONE WEEK LATE, in fill units "
                "where 1 kg of fill = 1,000 units (one 'fill-equivalent kg', "
                "kg-eq). The SAME fraction of a kg's fill value is charged "
                "in every solver pass (v2 pricing, 2026-09-04: pass 2 pays "
                "200 per kg-week against fill ~1,000 per kg, pass 1 pays "
                "200,000 against fill 1,000,000 per kg). Arithmetic: one FFS "
                "change under the Scenario F weights = (base 5 + ffs 600) x "
                "100 x K(33) = 1,996,500 pass-2 units ~ 2,000 kg-eq, so the "
                "default 200,000 = 200 kg-eq per tonne-week = 20 % of the "
                "tonne's fill value per week late: 10 t one week late ~ one "
                "FFS change, 30 t ~ three, 100 t ~ ten. Decision rule: a "
                "small run (<= 10 t) may slip one week to save ONE major "
                "changeover or to join a larger same-SKU run; a large order "
                "(>= 30 t) does not move a week for fewer than several major "
                "changeovers saved; it PRE-BUILDS instead whenever capacity "
                "allows (early costs 1/4 as much); it never leaves tonnage "
                "unmade to avoid lateness (late beats never up to 4 weeks "
                "late: 4 x 20 % < 100 %). Graded in whole weeks (1 h into "
                "the next week counts as a week — the plant's unit); "
                "late_weight (per hour) breaks ties inside a week. 0 = "
                "lateness free (still allowed). 400,000 (with early "
                "100,000) is the only tested setting that kept every late "
                "order within one week.",
    ),
    dict(
        id="early_kg_week_weight", group=GROUP_KNOB, ui="easy",
        name="Early kg-week price (soft weeks)",
        config="scheduler.early_kg_week_weight", default=50_000,
        where="flowstate.toml [scheduler] early_kg_week_weight; "
              "data_loader.Params; model_builder.py kg-week deviation "
              "block",
        planner="Cost of ONE TONNE produced ONE WEEK EARLY (inventory), same "
                "currency as late_kg_week_weight; active under BOTH due-week "
                "policies (pre-building is allowed either way). Default "
                "50,000 = 1/4 of "
                "the late price = 50 kg-eq per tonne-week = 5 % of the "
                "tonne's fill value per week early (50 per kg-week in pass "
                "2, 50,000 in pass 1). Inventory is cheaper than a missed "
                "week. Decision rule: on a contested hour the nearer week "
                "wins by 5 % per week on top of the 1 %/week fill gradient "
                "(the 280480 pattern: W1's order fills before W2's of the "
                "same SKU — what the fixed-48h rule got for free); a later "
                "week's order is pulled INTO an earlier campaign when tonnes "
                "x weeks early x 50,000 < the changeover saved (up to ~40 "
                "t-weeks per FFS change); three weeks early still costs only "
                "15 % of a kg's fill value, so idle capacity is still filled "
                "with later weeks (fill beats the price).",
    ),
    dict(
        id="pass2_makespan_weight", group=GROUP_KNOB, ui="easy",
        name="Pass-2 makespan weight (Scenario F)",
        config="scheduler.pass2_makespan_weight", default=1,
        where="flowstate.toml [scheduler] pass2_makespan_weight (optional "
              "key); data_loader.Params; model_builder.py pass-2 "
              "fill-exchange Minimize branch",
        planner="Coefficient of the plan's makespan in the pass-2 objective "
                "ONLY (Minimize co x 100 x K + makespan x W + ... - fill). "
                "Default 1 = a pure tiebreaker: one hour of shorter plan is "
                "worth 1 unit ~ 0.001 kg of fill, so tails are never shaved "
                "for cosmetics. 1 kg-eq ~ 1,000 pass-2 units: a weight of "
                "100,000 makes one hour of shorter plan worth 0.1 t of fill, "
                "1,000,000 makes it 1 t (an hour of a 1,000 kg/h line). "
                "Pass 1 and the non-fill modes keep objective.makespan_weight.",
    ),
    dict(
        id="cip_flex_weight", group=GROUP_KNOB, ui="exposed",
        name="CIP flexibility scale",
        config="objective.cip_flex_weight", default=20,
        where="model_builder.py (W_cip scaling); [objective]",
        planner="CIP-flex mode only: percent the CIP-deferral reward is "
                "scaled to, so a CIP can be pulled EARLIER to absorb a "
                "changeover. The line's max CIP interval always stays hard.",
    ),
    dict(
        # Promoted from fixed rule 2026-08-19: the old hardcoded over_sum*50
        # claimed "5%" but missed the x1000 production scaling — it was
        # really ~0.005%. Now an honest percentage; 0.0 default preserves
        # the near-zero behavior every tuned run actually had.
        id="over_target_reward_pct", group=GROUP_KNOB, ui="exposed",
        name="Over-target reward (Scenario F)",
        config="objective.over_target_reward_pct", default=0.0,
        where="model_builder.py soft_demand branch (over_sum coefficient); "
              "[objective] over_target_reward_pct",
        planner="Soft-demand fill only: reward for kg ABOVE an order's "
                "target (toward its max), as a % of the per-kg value of "
                "real demand. 0 = never overproduce on purpose. 5 = the "
                "solver treats 100 kg of overrun as worth 5 kg of real "
                "demand — a nudge to top up spare line-time, NOT a "
                "fulfillment gain: fulfillment is capped at target; the "
                "extra kg build inventory. Kg up to target always pay full "
                "weight, so meeting ALL targets still beats over-filling "
                "any one order; qty_max stays the hard wall.",
    ),
    dict(
        id="topload_weight", group=GROUP_KNOB, ui="exposed",
        name="Topload change cost",
        config="changeover.topload_weight", default=50,
        where="model_builder.py weighted CO cost; [changeover]",
        planner="Cost of a topload (carton/format) change between adjacent "
                "runs. Feeds every objective mode AND the Scenario F greedy "
                "seed's campaign grouping.",
    ),
    dict(
        id="ffs_weight", group=GROUP_KNOB, ui="exposed",
        name="FFS change cost",
        config="changeover.ffs_weight", default=10,
        where="model_builder.py weighted CO cost; [changeover]",
        planner="Cost of a form-fill-seal film change. Every mode + the F "
                "greedy seed.",
    ),
    dict(
        id="ttp_weight", group=GROUP_KNOB, ui="exposed",
        name="TTP change cost",
        config="changeover.ttp_weight", default=10,
        where="model_builder.py weighted CO cost; [changeover]",
        planner="Cost of a tray/thermoform change. Every mode + the F "
                "greedy seed.",
    ),
    dict(
        id="casepacker_weight", group=GROUP_KNOB, ui="exposed",
        name="Casepacker change cost",
        config="changeover.casepacker_weight", default=10,
        where="model_builder.py weighted CO cost; [changeover]",
        planner="Cost of a case-packer pattern change. Every mode + the F "
                "greedy seed.",
    ),
    dict(
        id="base_changeover_weight", group=GROUP_KNOB, ui="exposed",
        name="Base changeover cost",
        config="changeover.base_changeover_weight", default=5,
        where="model_builder.py weighted CO cost; [changeover]",
        planner="Flat cost charged for ANY SKU switch, on top of the "
                "per-machine costs. Every mode + the F greedy seed.",
    ),
    dict(
        id="conv_org_weight", group=GROUP_KNOB, ui="exposed",
        name="Conventional→organic cost",
        config="changeover.conv_org_weight", default=30,
        where="model_builder.py weighted CO cost; [changeover]",
        planner="Extra cost for a conventional→organic switch. Waived when "
                "a CIP lands between the two runs (the clean absorbs it).",
    ),
    dict(
        id="cinn_weight", group=GROUP_KNOB, ui="exposed",
        name="Cinnamon switch cost",
        config="changeover.cinn_weight", default=20,
        where="model_builder.py weighted CO cost; [changeover]",
        planner="Extra cost for switching out of cinnamon (carry-over "
                "risk). Waived when a CIP lands between the runs.",
    ),
    dict(
        id="flavor_weight", group=GROUP_KNOB, ui="exposed",
        name="Added-flavor cost",
        config="changeover.flavor_weight", default=5,
        where="model_builder.py weighted CO cost; [changeover]",
        planner="Cost per flavor ADDED at a switch (negative = reward for "
                "removing flavors). Pair cost never goes below 0.",
    ),
    dict(
        id="cip_req_weight", group=GROUP_KNOB, ui="exposed",
        name="Required-CIP violation cost",
        config="changeover.cip_req_weight", default=150,
        where="model_builder.py weighted CO cost + CIP-window waiver; "
              "data_loader setup floor; [changeover]",
        planner="Chemical/labor cost of the clean a cip_req_after SKU pair "
                "(protein hygiene) forces. The clean's HOURS are modeled as "
                "setup time (the pair's setup floor is the CIP duration), so "
                "the solver leaves the slot and the fill draws the CIP into "
                "it; landing the transition on an existing CIP window costs "
                "nothing. Keep this modest — time is the real deterrent.",
    ),
    dict(
        id="min_run_hours", group=GROUP_KNOB, ui="exposed",
        name="Minimum run length",
        config="scheduler.min_run_hours", default=4,
        where="model_builder.py run-bound block; [scheduler] min_run_hours",
        planner="Hard floor: any run a line gets must last at least this "
                "many hours (each segment of a CIP-split run too). Stops "
                "the solver scattering 1-hour stubs. Committed MOs are "
                "exempt when their remaining kg physically can't support it.",
    ),
    dict(
        id="min_run_pct_of_qty", group=GROUP_KNOB, ui="exposed",
        name="Minimum run share of order",
        config="scheduler.min_run_pct_of_qty", default=0.5,
        where="model_builder.py run-bound block; [scheduler] "
              "min_run_pct_of_qty",
        planner="Hard-demand floor (A–E): a line assigned to an order must "
                "run at least this fraction of the hours the order's "
                "minimum quantity needs — the minimum AFTER the window "
                "clamp (impossible-demand rule), so a clamped order can "
                "still be placed. At 0.5, splitting across 2 lines forces "
                "each to take a meaningful share — no token 1h helpers. "
                "NOT applied under soft demand (Scenario F): partial fills "
                "are the point there, so only min_run_hours holds "
                "(2026-09-03).",
    ),
    dict(
        id="max_lines_per_order", group=GROUP_KNOB, ui="exposed",
        name="Max lines per order",
        config="scheduler.max_lines_per_order", default=3,
        where="model_builder.py (sum(present) <= mlpo); [scheduler] "
              "max_lines_per_order",
        planner="Cap on how many lines may run the SAME ORDER. Honest "
                "detail: the cap is per ORDER (one demand row = one "
                "SKU-week), NOT per SKU — the same SKU appearing in two "
                "different week-orders may legally run on more lines in "
                "total.",
    ),
    dict(
        id="time_limit", group=GROUP_KNOB, ui="exposed",
        name="Solve budget",
        config="scheduler.time_limit", default=120,
        where="phase2_scheduler.py; [scheduler] time_limit; UI budget picker",
        planner="CP-SAT seconds per solve attempt (each relax level and "
                "each two-pass pass gets its own budget). The runner kills "
                "the whole subprocess at 4x budget + 60s.",
    ),
    dict(
        id="solver_random_seed", group=GROUP_KNOB, ui="easy",
        name="CP-SAT random seed",
        config="scheduler.solver_random_seed", default="absent (CP-SAT default)",
        where="phase2_scheduler.py apply_solver_seed (every solve pass); "
              "[scheduler] solver_random_seed",
        planner="Fixed seed for CP-SAT's randomized search. Same model + "
                "different seed explores a DIFFERENT search path, so two "
                "runs stop reproducing each other — the overnight batch's "
                "seed arms use this to diversify starting points. It never "
                "changes what is feasible, only where the search looks "
                "first. Absent = CP-SAT's own default seed.",
    ),
    dict(
        id="objective_mode", group=GROUP_KNOB, ui="exposed",
        name="Objective mode",
        value="balanced / min-changeovers / spread-load",
        where="phase2_scheduler.py --objective; UI scenario picker",
        planner="Which formula the solver optimizes. Balanced honours "
                "makespan_weight/changeover_weight; the other two use fixed "
                "multipliers (see the fixed rules below).",
    ),
    dict(
        id="cross_week", group=GROUP_KNOB, ui="exposed",
        name="Cross-week mode",
        value="off by default",
        where="phase2_scheduler.py --cross-week; UI checkbox",
        planner="AZAP's week becomes a priced preference instead of a hard "
                "wall, and the solve runs single-phase over the whole "
                "horizon so campaigns can merge across the week boundary.",
    ),
    dict(
        id="cip_flex", group=GROUP_KNOB, ui="exposed",
        name="CIP flexibility mode",
        value="off by default",
        where="phase2_scheduler.py --cip-flex; UI checkbox",
        planner="Lets a CIP be pulled EARLIER to absorb a changeover by "
                "scaling down the deferral reward. Never moves the hard "
                "max-interval deadline.",
    ),
    dict(
        id="dns_trim", group=GROUP_KNOB, ui="exposed",
        name="Stock policy: component-blocked SKUs",
        value="on by default (F run)",
        where="pages/generate.py F run checkbox; helpers/agent_policy.py",
        planner="DO-NOT-SCHEDULE SKUs get qty_min→0 and qty_max capped at "
                "what components actually support, before the solve.",
    ),

    # ── Fixed rules — structure, safety, hard-coded economics ─────────────
    dict(
        id="week_grid", group=GROUP_FIXED, ui="don't expose",
        name="Week grid",
        value="demand-derived weeks; deviation graded in 168 h steps",
        where="model_builder.py week_frame / WEEK_STEP_H",
        planner="The solver's weeks come from the demand file's due windows "
                "(the staged frame — a stub first week when the horizon "
                "opens mid-week), not from a Monday constant. The kg-week "
                "deviation prices count whole 168 h steps from an order's own "
                "due_start (early) or due_end + 1 (late). A 4-week horizon "
                "(horizon_weeks = 4 / 672 h) is supported end to end: staging "
                "pro-rates the partial fourth week, the model's CIP slots and "
                "week steps follow the horizon.",
    ),
    dict(
        id="week0_fill_start", group=GROUP_FIXED, ui="needs care",
        name="Early-fill policy (how early next-week demand may be pre-built)",
        config="scheduler.early_fill_hours", default="unbounded",
        where="flowstate.toml [scheduler] early_fill_hours; "
              "model_builder.py effective_due_start; "
              "independent_validator.py DUE_WINDOW / EARLY_BEFORE_COMMITTED",
        planner="Plant decision 2026-09-04: a later week's demand may be "
                "produced as far back into earlier weeks as free capacity "
                "allows ('unbounded'), but never before an already "
                "scheduled MO — the line's availability gate, committed "
                "windows and every current-state MO on the line are hard "
                "floors. Set an integer (e.g. 48) to cap how many hours "
                "before its own due_start any demand order may start. "
                "Either way the right week is a soft preference: the fill "
                "week gradient and week_deviation_weight (per early hour) "
                "serve the nearest due week first when capacity is short.",
    ),
    dict(
        id="week_stitch", group=GROUP_FIXED, ui="needs care",
        name="Week-boundary stitch",
        value="max 1h gap",
        where="model_builder.py MAX_GAP_W0_W1_HOURS",
        planner="On a line running both weeks, week-1 work must start "
                "within 1 hour of the week-0 tail — no idle wall between "
                "weeks. Committed MOs are exempt; off in cross-week mode.",
    ),
    dict(
        id="relax_ladder", group=GROUP_FIXED, ui="don't expose",
        name="Auto-relax ladder",
        value="hard → relax demand → +soft due dates → +ignore changeovers",
        where="phase2_scheduler.py RELAX_LADDER",
        planner="When a level is INFEASIBLE the solver retries one level "
                "softer and reports which level produced the plan. "
                "Min-changeovers never escalates past soft due dates — it "
                "would contradict itself by dropping changeovers. Level 3 "
                "('ignore changeovers') drops only the changeover PRICE; "
                "the setup HOURS between different SKUs stay hard "
                "(2026-09-03 — before, level 3 wrote zero-gap schedules). "
                "Under the default soft due-week policy (2026-09-04 #2) "
                "level 2's 'soft due dates' is already in force for demand "
                "orders at level 0, so level 2 only still relaxes committed "
                "MOs' windows.",
    ),
    dict(
        id="producible_zeroing", group=GROUP_FIXED, ui="don't expose",
        name="Impossible-demand clamp",
        value="qty_min clamped to provable window capacity",
        where="model_builder.py _producible_kg_in_window",
        planner="An order asking for more than its capable lines can "
                "physically make inside its window (gates and the UNION of "
                "downtime subtracted, capable == 1 lines only, at most "
                "max_lines_per_order of them) is clamped to that ceiling "
                "and reported 'short of minimum' — it never turns the whole "
                "solve INFEASIBLE. Committed MOs and trials are never "
                "clamped. (Capable/union/max-lines tightened 2026-09-03: "
                "before, non-capable lines and double-counted downtime "
                "inflated the bound ~4x and 105 t of unmakeable demand was "
                "reported as a solver shortfall.)",
    ),
    dict(
        id="dead_pair_pruning", group=GROUP_FIXED, ui="don't expose",
        name="Dead-pair pruning",
        value="automatic",
        where="model_builder.py dead_pairs",
        planner="A (line, order) pair whose whole window is eaten by the "
                "availability gate or downtime is removed from the model "
                "up front — pure speed, no schedule it could have joined.",
    ),
    dict(
        id="availability_gate", group=GROUP_FIXED, ui="don't expose",
        name="Line availability gate",
        value="hard at every relax level",
        where="model_builder.py availability floor; initial_states.csv",
        planner="Nothing may be scheduled on a line before its "
                "available_from hour (the end of the MO it is running "
                "now). This is plant fact, not preference.",
    ),
    dict(
        id="cip_max_interval", group=GROUP_FIXED, ui="don't expose",
        name="CIP max interval (food safety)",
        config="cip.interval_h", default=120,
        where="data/reference/line_cip_hrs.csv per line; [cip] interval_h "
              "fallback; model_builder.py CIP block",
        planner="A line may never run longer than its max CIP interval "
                "without a clean. HARD at every relax level; cip_flex can "
                "pull a CIP earlier but NEVER later than this.",
    ),
    dict(
        id="cip_duration", group=GROUP_FIXED, ui="easy",
        name="CIP duration",
        config="cip.duration_h", default=6,
        where="[cip] duration_h; model_builder.py CIP intervals",
        planner="Every solver-placed CIP blocks the line for this long. A "
                "CIP is longer than any changeover, so a CIP between two "
                "SKUs absorbs the changeover for free.",
    ),
    dict(
        id="cip_max_count", group=GROUP_FIXED, ui="don't expose",
        name="Max solver CIPs per line",
        value="ceil((horizon + carry) / interval) + 1 slots",
        where="model_builder.py CIP slot count (fix SB-2)",
        planner="The model creates enough CIP slots per line for the whole "
                "horizon at the line's interval plus one spare (2026-09-03; "
                "before, a hard-coded 3 silently capped a 504 h line). A "
                "4-week (672 h) horizon at 120 h intervals gets 7 slots.",
    ),
    dict(
        id="cip_absorb", group=GROUP_FIXED, ui="don't expose",
        name="CIP absorbs allergen switches",
        value="conv→org + cinnamon penalties waived",
        where="model_builder.py cip_absorbable",
        planner="When a CIP lands between two runs, the line is fully "
                "clean, so the conventional→organic and cinnamon penalties "
                "for that switch are refunded.",
    ),
    dict(
        id="co_fallback", group=GROUP_FIXED, ui="don't expose",
        name="Unknown changeover fallback",
        value="0h setup, full machine-change penalty",
        where="model_builder.py (setup.get default 0, machine_changes "
              "default all-1)",
        planner="A SKU pair missing from changeovers.csv costs zero setup "
                "TIME but the full weighted penalty (all four machines "
                "assumed to change) — the solver avoids unknown switches "
                "without inventing hours. EXCEPTION (2026-09-03): the same "
                "SKU following itself is never a changeover — cost 0, time "
                "0 — whatever the matrix says; before, a missing (X,X) row "
                "charged the full default and same-SKU campaigns were "
                "split by foreign SKUs.",
    ),
    dict(
        id="setup_rounding", group=GROUP_FIXED, ui="don't expose",
        name="Setup hours rounded UP",
        value="0.25→1h, 1.25→2h, 2.5→3h",
        where="solver/changeover_cache.py round_setup_hours",
        planner="The model plans in whole hours, so every changeover "
                "standard is rounded UP to the next hour: the reserved slot "
                "may be longer than the standard, never shorter. Before "
                "2026-09-03 the plant's quarter-hour standards (13,998 of "
                "54,988 matrix rows) were rounded down by 15 minutes.",
    ),
    dict(
        id="initial_sku_co", group=GROUP_FIXED, ui="don't expose",
        name="First-run changeover + long-shutdown extra",
        value="from initial SKU; +4h default after long shutdown",
        where="model_builder.py first_flags; initial_states.csv "
              "long_shutdown_extra_setup_hours",
        planner="The first order on a line pays the changeover from the "
                "SKU the line is holding now (unless CLEAN) — its setup "
                "TIME and, since 2026-09-03, its weighted COST too (same "
                "formula and CIP waivers as any other switch; before, the "
                "opening format change was free and the solver spent it on "
                "the most expensive switch of the line). Plus extra setup "
                "hours if the line is coming back from a flagged long "
                "shutdown.",
    ),
    dict(
        id="min_co_multipliers", group=GROUP_FIXED, ui="don't expose",
        name="Min-changeovers mode multipliers",
        value="weighted CO x100 (flat count x10000); makespan coeff 1",
        where="model_builder.py min-changeovers branches",
        planner="In min-changeovers mode the changeover term dominates by "
                "construction; makespan_weight and changeover_weight are "
                "ignored.",
    ),
    dict(
        id="spread_multipliers", group=GROUP_FIXED, ui="don't expose",
        name="Spread-load mode multipliers",
        value="busiest line x1000; weighted CO x1; makespan coeff 1",
        where="model_builder.py spread-load branches",
        planner="Spread-load minimizes the busiest line's hours above all; "
                "changeovers are a light tiebreaker. makespan_weight and "
                "changeover_weight are ignored.",
    ),
    dict(
        id="fill_dominance", group=GROUP_FIXED, ui="don't expose",
        name="Production dominates preferences",
        value="production kg x1000 vs preference terms",
        where="model_builder.py maximize branch",
        planner="PASS 1 of fill/maximize modes: one kilogram up to target "
                "is worth ~1,000 x 1,000 = 1,000,000 objective units, so "
                "under the Scenario F weights (changeover x300) a topload "
                "change (455) is worth 0.137 kg, an FFS change (605) 0.18 kg, "
                "a TTP+casepacker switch (30) 0.009 kg (TTP alone, 10: "
                "0.003 kg), an idle hour 0.000003 kg — NOT the 46 / 61 / 3 kg "
                "an older comment claimed (off by x333). "
                "Pass 1 therefore never gives up fill for fewer "
                "changeovers — and, the honest flip side, it does not "
                "minimize changeovers at all; that is pass 2's job (see the "
                "two-pass rules). It also buys a 4 h stub with a full "
                "changeover rather than leave the hours idle.",
    ),
    dict(
        id="week_gradient", group=GROUP_FIXED, ui="don't expose",
        name="Week-proximity gradient",
        value="+10 per week earlier (on a 1000 base, ≈ +1%/week)",
        where="model_builder.py _w1()",
        planner="When demand exceeds free capacity, contested hours serve "
                "the NEAREST due week first — each week earlier pays about "
                "1% more per kg. Strong enough to order weeks, far too "
                "weak to starve total fill.",
    ),
    dict(
        id="cur_mo_priority", group=GROUP_FIXED, ui="don't expose",
        name="Committed MO priority",
        value="10,000:1 per kg in fill mode (10:1 otherwise)",
        where="model_builder.py maximize branch",
        planner="A committed MO's kilogram outweighs a new demand kilogram "
                "by four orders of magnitude — the solver trims demand, "
                "never the plant's committed work. MOs are locked to their "
                "line and can be split/trimmed but never dropped.",
    ),
    dict(
        id="two_pass_epsilon", group=GROUP_FIXED, ui="easy",
        name="Two-pass fill give-back (Scenario F)",
        config="scheduler.two_pass_epsilon_pct", default=1.0,
        where="phase2_scheduler.py TWO_PASS_EPS; [scheduler] "
              "two_pass_epsilon_pct",
        planner="Pass 1 maximizes fill; pass 2 minimizes changeovers while "
                "keeping at least (100% - this) of pass 1's TOTAL fill "
                "score. Honest detail (audit 2026-09-03): on its own this "
                "floor is fungible — pass 2 could spend the whole 1% on "
                "anything in its objective (it trimmed 100 t to 99 t to end "
                "1 h earlier, and deleted a 2 t sole-line order). It is now "
                "backed by the per-order floors and the fill exchange rate "
                "below; the percentage remains a global safety net.",
    ),
    dict(
        id="pass2_order_floors", group=GROUP_FIXED, ui="don't expose",
        name="Pass-2 per-order floors (Scenario F)",
        value="produced >= min(pass-1 produced, target) per order",
        where="model_builder.py build_model(order_floors=...); "
              "phase2_scheduler.py two-pass block",
        planner="Pass 2 may re-sequence and merge runs but may never delete "
                "an order pass 1 filled or shave its tail below target: "
                "every order keeps at least what pass 1 made, up to its "
                "target (2026-09-03).",
    ),
    dict(
        id="pass2_fill_exchange_rate", group=GROUP_FIXED, ui="needs care",
        name="Pass-2 fill exchange rate (Scenario F)",
        value="K = 33 under the F weights (one FFS change ≈ 2 h of a "
              "1,000 kg/h line)",
        where="model_builder.py default_fill_exchange_rate / "
              "build_model(fill_exchange_rate=...)",
        planner="Pass 2 keeps fill IN its objective: it minimizes "
                "changeover load x 100 x K minus the fill score, so it gives "
                "up a kilogram only when the changeover it saves is worth "
                "more. Arithmetic: an FFS change costs base 5 + ffs 600 = "
                "605 -> 60,500 pass-2 units; the plant values it at about "
                "2 h of a 1,000 kg/h line = 2,000 kg x ~1,010 fill units/kg "
                "= 2,020,000; K = 2,020,000 / 60,500 ≈ 33. So a topload "
                "change (455) ≈ 1.5 t, a full change (1080) ≈ 3.5 t, a "
                "TTP-only switch (10) ≈ 33 kg; a makespan hour is worth "
                "0.001 kg — tails are never shaved for cosmetics.",
    ),
    dict(
        id="cur_mo_bounds", group=GROUP_FIXED, ui="don't expose",
        name="Committed-MO quantity bracket",
        value="rate x floor(remaining/rate) .. rate x ceil(remaining/rate)",
        where="data_loader.py _parse_current_mo",
        planner="A running/queued MO's remaining kg is produced in whole "
                "hours of its line's rate, so its bounds bracket the "
                "remaining kg at the hour below and the hour above "
                "(2026-09-03 — pinning it exactly made Scenario E "
                "structurally INFEASIBLE unless the kg happened to be a "
                "whole multiple of the rate). The raw remaining kg is kept "
                "for the write-back report.",
    ),
    dict(
        id="demand_validation", group=GROUP_FIXED, ui="don't expose",
        name="Demand / capabilities input validation",
        value="bad row -> named ValueError, never a silent INFEASIBLE",
        where="data_loader.py _parse_demand, Data.load (capabilities)",
        planner="A negative or non-numeric target, lower_pct > upper_pct "
                "or a line_id that is not a number stops the run with the "
                "offending row named (2026-09-03). Before, one bad row made "
                "every relax level INFEASIBLE with no message, or a text "
                "line_id silently became P09. A target of exactly 0 (a "
                "fully covered week) is legitimate. A repeated MO number "
                "gets a distinct order id and a warning.",
    ),
    dict(
        id="horizon", group=GROUP_FIXED, ui="easy",
        name="Planning horizon",
        config="scheduler.horizon_hours", default=336,
        where="[scheduler] horizon_hours (wins) or horizon_weeks x 168",
        planner="How far the solver plans. Orders due past the horizon "
                "cannot be produced at all — the horizon must cover the "
                "demand file. horizon_weeks = 4 (672 h) is supported end to "
                "end (plant decision 2026-09-04 #2: look 3-4 weeks out so a "
                "SKU's week-1 and week-3 tonnage can be consolidated); "
                "staging pro-rates a partial last week and defers the rest.",
    ),
    dict(
        id="budget_ceiling", group=GROUP_FIXED, ui="don't expose",
        name="Subprocess kill ceiling",
        value="4 x time_limit + 60s",
        where="helpers/scenario_runner.py run_scenario",
        planner="The whole solver run (all relax levels, both passes) is "
                "killed at this wall-clock ceiling so a hung solve can "
                "never freeze the app.",
    ),
    dict(
        id="workers", group=GROUP_FIXED, ui="don't expose",
        name="Search workers",
        value="8",
        where="phase2_scheduler.py num_search_workers",
        planner="CP-SAT runs 8 parallel search workers per solve.",
    ),
    dict(
        id="warm_start_gates", group=GROUP_FIXED, ui="don't expose",
        name="Warm-start trust gates",
        value="same inputs (md5) + relax level <= current",
        where="phase2_scheduler.py input_signature; scenario_runner "
              "prev_schedule carry",
        planner="Yesterday's schedule seeds today's search ONLY when the "
                "inputs are byte-identical and it was solved at least as "
                "strictly — stale or looser hints starve the search "
                "instead of helping it.",
    ),
    dict(
        id="f_staging", group=GROUP_FIXED, ui="don't expose",
        name="Scenario F staging",
        value="committed plan = blocked time; fill starts at the tail",
        where="helpers/scenario_runner.py _overlay_fill",
        planner="The committed plan (running+queued MOs, trials, projected "
                "CIPs, planner pins) becomes fixed blocked windows; per "
                "line the fill region starts where committed work ends, "
                "with the last committed SKU as the changeover base. The "
                "plant's own plan is never rewritten.",
    ),
    dict(
        id="now_floor", group=GROUP_FIXED, ui="don't expose",
        name="Now-floor (Scenario F)",
        value="no new block may start in the past",
        where="helpers/scenario_runner.py _overlay_fill now_floor",
        planner="If a line's committed tail ended yesterday, fill starts "
                "NOW, not in the past. Only applied when 'now' falls "
                "inside the horizon (replays on old snapshots untouched).",
    ),
    dict(
        id="cip_standdown", group=GROUP_FIXED, ui="don't expose",
        name="Solver CIP stand-down (Scenario F)",
        value="staged max_cip_hrs = 100,000",
        where="helpers/plan_fill.py SOLVER_CIP_INTERVAL_STANDDOWN_H; "
              "_overlay_fill step 3",
        planner="In F the projected layer-1 CIPs (from cip_info at each "
                "line's real interval) carry the cleans, so the solver's "
                "own CIP generation is switched off by setting its "
                "interval absurdly high. Dirty-time is still bounded — by "
                "the projected CIPs, not the model.",
    ),
    dict(
        id="demand_netting", group=GROUP_FIXED, ui="don't expose",
        name="Demand netting (Scenario F)",
        value="committed production credits its SKU-week; surplus carries "
              "forward",
        where="helpers/demand_coverage.py; _overlay_fill step 4",
        planner="What committed MOs already produce is subtracted from the "
                "demand plan before the solve — a covered week is never "
                "re-planned, and pre-built surplus rolls into later weeks.",
    ),
    dict(
        id="trials_blocked", group=GROUP_FIXED, ui="don't expose",
        name="Trials are blocked hours",
        value="never solver orders",
        where="helpers/scenario_runner.py (manprg TRIALS → downtimes)",
        planner="Trial reservations block their line-time; the solver "
                "plans around them. In A–E a trial can also be a pinned "
                "block with fixed line, start and end.",
    ),
    dict(
        id="greedy_seed", group=GROUP_FIXED, ui="don't expose",
        name="Greedy warm start (Scenario F)",
        value="format-aware, same [changeover] weights + min_run_hours",
        where="helpers/scenario_runner.py _greedy_seed; helpers/greedy_fill",
        planner="A dense greedy fill is built with the SAME changeover "
                "economics and min-run floor the solver optimizes, and "
                "handed to CP-SAT as a starting point. It only steers the "
                "search — it cannot change what is feasible.",
    ),
    dict(
        id="line_rates", group=GROUP_FIXED, ui="easy",
        name="Line rates source",
        config="scheduler.use_sku_rates", default=False,
        where="data_loader.py; line_rates.csv vs capabilities_rates.csv",
        planner="By default every SKU on a line runs at the line's flat "
                "monthly rate (line_rates.csv). use_sku_rates=true keeps "
                "per-SKU rates instead. Produced kg uses the rate rounded "
                "to whole kg/h.",
    ),
    dict(
        id="cur_mo_min_run_exempt", group=GROUP_FIXED, ui="don't expose",
        name="Committed-MO min-run exemption",
        value="floor only when remaining kg supports it",
        where="model_builder.py is_current branch",
        planner="A committed MO whose remaining work is smaller than the "
                "min-run floor is not forced to fake extra hours, and its "
                "CIP-split segments have no per-segment floor (a forced "
                "clean can leave a short leg).",
    ),
    dict(
        id="inert_params", group=GROUP_FIXED, ui="don't expose",
        name="Inert parameters (honesty note)",
        value="changeover_penalty, stale_threshold_days, stale_setup_extra_h",
        where="data_loader.py Params",
        planner="These exist in the config dataclass but are never read by "
                "the model — changing them does nothing. Kept only so old "
                "configs load.",
    ),
    dict(
        id="scorecard_co_fallbacks", group=GROUP_FIXED, ui="don't expose",
        name="Scorecard changeover fallbacks (not a solve rule)",
        value="default_co_hours_recipe/format/base = 1.0 / 2.0 / 0.5",
        where="[scorecard]; helpers/scorecard_engine.py",
        planner="These estimate changeover HOURS when scoring a calendar "
                "that lacks a standards row. They shape the SCORE, never "
                "the solver's plan — listed here so nobody hunts for them "
                "in the solver.",
    ),
]


def solver_rules(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The rulebook with live values resolved from a loaded flowstate.toml.

    Returns copies — callers may mutate freely. Rules carrying a "config"
    dotted key get their current toml value (or the stated default); rules
    with a literal "value" keep it verbatim.
    """
    cfg = cfg or {}
    out: list[dict[str, Any]] = []
    for rule in _RULES:
        row = dict(rule)
        dotted = row.get("config")
        if dotted:
            section, _, key = dotted.partition(".")
            sect = cfg.get(section) or {}
            if key in sect:
                row["value"] = sect[key]
            else:
                row["value"] = f"{row.get('default')} (default)"
        out.append(row)
    return out


def render_solver_rulebook(cfg: dict[str, Any] | None = None) -> None:
    """Streamlit expander: the full rulebook, knobs and fixed rules apart."""
    import pandas as pd
    import streamlit as st

    rules = solver_rules(cfg)

    def _frame(group: str) -> pd.DataFrame:
        rows = [
            {
                "Rule": r["name"],
                "Current value": str(r.get("value", "")),
                "What it means for the plan": r["planner"],
                "Lives in": r["where"],
                "UI knob?": r["ui"],
            }
            for r in rules
            if r["group"] == group
        ]
        return pd.DataFrame(rows)

    with st.expander("Solver rulebook — every rule the solver follows"):
        st.caption(
            "Audited from code/solver/ and the scenario staging. Knobs are "
            "adjustable in the custom scenario builder below (or "
            "flowstate.toml); fixed rules are structural or safety and "
            "change only in code."
        )
        st.markdown(f"**Adjustable knobs ({GROUP_KNOB})**")
        st.dataframe(_frame(GROUP_KNOB), hide_index=True,
                     use_container_width=True)
        st.markdown(f"**Fixed rules ({GROUP_FIXED})**")
        st.dataframe(_frame(GROUP_FIXED), hide_index=True,
                     use_container_width=True)
