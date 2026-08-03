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
}

export interface SandboxConfig {
  planning_anchor: string;
  cip_duration_h: number;
  min_run_hours: number;
  horizon_hours: number;
}

export interface SandboxArgs {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  capabilities: Record<string, Record<string, number>>;
  changeovers: Record<string, Record<string, number>>;
  demandTargets: DemandTarget[];
  lines: LineInfo[];
  holdingArea: ScheduleBlock[];
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
