// types.ts — Shared types for the multi-type plant calendar Gantt.

export type BlockType =
  | "sku"
  | "cip"
  | "trial"
  | "maintenance"
  | "contractor"
  | "line_down";

export interface ScheduleBlock {
  id: string;
  line_id: number;
  line_name: string;
  order_id: string;
  sku: string;
  sku_description?: string;
  start_hour: number;
  end_hour: number;
  run_hours: number;
  is_trial: boolean;
  block_type: BlockType;
  label?: string;
  locked?: boolean;
  completion_pct?: number;
  cases_left?: number;
  qty_kg?: number;
  /** Provenance flags (e.g. current_state:completed) — round-tripped. */
  attrs?: string;
  /** Completed manprg history: greyed, immovable, no drag. */
  completed?: boolean;
  /** Planner-pinned: fixed for the solver (attrs token 'pinned'). Immovable
   * like an MO until unpinned in the block popup. */
  pinned?: boolean;
}

export interface DemandTarget {
  order_id: string;
  sku: string;
  qty_min: number;
  qty_max: number;
  /** Due window (hour offsets) - drives waterfall crediting of committed-MO
   * production that carries no matching demand order id. */
  due_start_hour?: number;
  due_end_hour?: number;
}

export interface LineInfo {
  line_id: number;
  line_name: string;
  /** Group a double line's sides belong to, e.g. "P17" for P17A/P17B. */
  line_group?: string;
  /** "A" / "B" on a Bossar double line; empty or absent on single lines. */
  side?: string;
  is_double?: boolean;
}

export interface SandboxConfig {
  planning_anchor: string;
  cip_duration_h: number;
  min_run_hours: number;
  horizon_hours: number;
  /** Hour offset of the 2-week lock boundary; blocks starting before it are
   * committed to the plant and refuse drag/resize/edit. null/absent = no lock. */
  locked_through_h?: number | null;
  /** ISO week of the demand file's anchor (demand_plan.source.json
   * anchor_iso_week): order-id -W<k> labels read W(base+k). */
  demand_base_iso_week?: number | null;
}

/** One SKU-pair changeover classification, precomputed server-side by
 * helpers/scorecard_engine.gantt_kpis so severity/hour rules live in Python
 * only. Key format in ServerKpis.co_pairs: "FROM|TO". */
export interface CoPairInfo {
  recipe: number;
  format: number;
  hours: number;
  /** machine touched by this transition (0/1) — per-week chips */
  tl?: number;
  ffs?: number;
  cp?: number;
  ttp?: number;
  /** a CIP is required between these SKUs (0/1) — satisfied (and the whole
   * transition waived) when a CIP block sits fully in the gap. */
  cip_req?: number;
}

/** Canonical KPI payload from helpers/scorecard_engine.gantt_kpis (Python is
 * the source of truth). Rendered verbatim until the user edits, then kpi.ts
 * recomputes live with the same rules via co_pairs/co_default. */
export interface ServerKpis {
  adherence: AdherenceRow[];
  pct_adherence: number;
  orders_met: number;
  orders_total: number;
  changeovers: {
    recipe_changes: number;
    format_changes: number;
    total_co_hours: number;
    sku_transitions: number;
  };
  per_line_changeovers: Record<string, number>;
  co_pairs: Record<string, CoPairInfo>;
  co_default: CoPairInfo;
  /** NON-BOARD credit per order: made kg from completed MOs the board
   * hides — never committed-MO kg (those ARE board blocks and count from
   * the board; see build_ledger_from_data(made_only=True)). */
  covered_by_order?: Record<string, number>;
}

export interface SandboxArgs {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  capabilities: Record<string, Record<string, number>>;
  changeovers: Record<string, Record<string, number>>;
  demandTargets: DemandTarget[];
  lines: LineInfo[];
  holdingArea: ScheduleBlock[];
  /** line/side name -> [startHour, endHour) windows where it is down. */
  sideDowntime?: Record<string, number[][]>;
  /** sku -> pack format string (e.g. "6X12X90") from sku_info. */
  skuFormats?: Record<string, string>;
  /** Canonical KPI payload from helpers.scorecard_engine.gantt_kpis —
   * rendered verbatim until the user edits, then kpi.ts recomputes live. */
  kpis?: ServerKpis | null;
  /** Blank-space SKU picker: demand-plan SKUs each line can run (capable==1),
   * built by helpers.calendar_io.build_line_capable_skus. */
  lineCapableSkus?: Record<string, { sku: string; rate: number }[]>;
  /** "FROM|TO" -> changeover-type bitmask (helpers.calendar_io.build_co_flags;
   * bit order in utils/skuPicker.CO_FLAG_BITS). The table only covers
   * demand-plan pairs: a missing pair is UNKNOWN (renders "?"), not clean. */
  coFlags?: Record<string, number>;
  /** sku -> designation from sku_info (demand SKUs only) for picker rows. */
  skuDescriptions?: Record<string, string>;
  config: SandboxConfig;
}

export interface SandboxState {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  holdingArea: ScheduleBlock[];
  lastAction: string;
}

export interface AdherenceRow {
  order_id: string;
  sku: string;
  qty_min: number;
  qty_max: number;
  scheduled_qty: number;
  pct_adherence: number;
  status: "MET" | "UNDER" | "OVER";
  /** Mean capable-line rate for this SKU (kg/h) — used by the "+" holding button. */
  avg_rate_kgph: number;
}

export interface KpiData {
  pctAdherence: number;
  ordersMet: number;
  ordersTotal: number;
  /** Flat SKU transitions — same number as the scorecard's sku_transitions. */
  totalChangeovers: number;
  recipeChanges: number;
  formatChanges: number;
  totalCoHours: number;
  perLineChangeovers: Record<string, number>;
  overlaps: string[];
}

/** Non-production blocks that live in cipWindows and skip capability checks. */
export function isWindowBlock(blockType: string): boolean {
  return (
    blockType === "cip" ||
    blockType === "maintenance" ||
    blockType === "contractor" ||
    blockType === "line_down"
  );
}
