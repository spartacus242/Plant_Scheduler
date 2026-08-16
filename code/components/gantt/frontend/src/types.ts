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
  totalChangeovers: number;
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
