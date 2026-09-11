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
   * anchor_iso_week): order-id -W<k> labels read the REAL ISO week k weeks
   * after that anchor week (53-week years included). */
  demand_base_iso_week?: number | null;
  /** Optional: the demand file's anchor DATE ("YYYY-MM-DD ..."); pins the
   * base week's year outright (else it is inferred as the ISO year whose
   * week <base> lies closest to the planning anchor). */
  demand_anchor?: string | null;
  /** Per-line MaxHoursBetweenCIP (cip_info) for re-forecasting later
   * projected cleans after a planner-inserted CIP; default when a line is
   * missing. */
  cip_interval_h?: Record<string, number>;
  cip_interval_default_h?: number;
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

/** Supply-timeline tables from helpers.calendar_io.build_stock_payload
 * (contract 2026-09-01 §5, snake_case like kpis.co_pairs). Python gates
 * receipts and shifts every hour into THIS page's anchor frame; the client
 * (utils/stockRisk) only builds curves and verdicts from these tables. */
export interface StockArgs {
  feed_state: "ok" | "missing" | "stale" | "empty";
  as_of: { stock_rm: string; stock_pkg: string; po: string };   // display stamps
  receipts_window_end_h: number;
  rules: { min_days_after_delivery: number; lead_measured_from: "block_start" | "depletion";
           dependent_frac_floor: number; hard_block: boolean };
  opening: Record<string, number>;
  tracked: string[];
  in_house: string[];
  units: Record<string, string>;
  designations: Record<string, string>;
  snapshot_h: Record<string, number>;
  receipts: Record<string, { ready_h: number; qty: number; po8: string; tier: "erp" | "appt";
                              receipt_date: string; label: string }[]>;
  sku_needs: Record<string, { kg_per_case: number;
                              items: { item: string; per_case: number; unit: string; alts: string[] }[] }>;
  cases_left: Record<string, number>;      // order_id -> cases_left for running MOs (from manprg), may be {}
}

export interface SandboxArgs {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  capabilities: Record<string, Record<string, number>>;
  changeovers: Record<string, Record<string, number>>;
  demandTargets: DemandTarget[];
  lines: LineInfo[];
  holdingArea: ScheduleBlock[];
  /** Demand orders the planner removed from holding with a card's "×"
   * (session-scoped; Python keeps the list and hands it back on every
   * mount): the auto-derivation skips them until the adherence table's
   * "+" brings the card back. */
  holdingDismissed?: string[];
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
  /** Supply timeline payload (helpers.calendar_io.build_stock_payload).
   * null/absent = no stock surfaces at all (`stock_enabled = !!args.stock`):
   * read-only mounts and an old report cache render exactly as before. */
  stock?: StockArgs | null;
  /** Block id to focus on mount (Reconcile finding -> ?focus=<block_id>):
   * seeds highlightSku from that block's sku and scrolls it into view. */
  focusBlock?: string | null;
  config: SandboxConfig;
}

export interface SandboxState {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  holdingArea: ScheduleBlock[];
  /** Order ids dismissed from holding (see SandboxArgs.holdingDismissed). */
  holdingDismissed: string[];
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
  /** Mean capable-line rate for this SKU (kg/h), CLIENT-ONLY: kpi.ts sets
   * it, the server rows (compute_adherence) do not carry it — the "+"
   * holding button prices from utils/rates.meanCapableRate instead (fix
   * FE / audit ui-8: `missing / (avg_rate_kgph || 1)` capped at 24 h gave
   * every '+' a 24 h card before the first edit). */
  avg_rate_kgph?: number;
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
