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
}

export interface DemandTarget {
  order_id: string;
  sku: string;
  qty_min: number;
  qty_max: number;
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
}

/** One SKU-pair changeover classification, precomputed server-side by
 * helpers/scorecard_engine.gantt_kpis so severity/hour rules live in Python
 * only. Key format in ServerKpis.co_pairs: "FROM|TO". */
export interface CoPairInfo {
  recipe: number;
  format: number;
  hours: number;
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
  kpis?: ServerKpis | null;
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
