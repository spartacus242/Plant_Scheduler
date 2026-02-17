// types.ts — Shared types for the Gantt sandbox component.

export interface ScheduleBlock {
  id: string; // unique block ID (generated client-side)
  line_id: number;
  line_name: string;
  order_id: string;
  sku: string;
  start_hour: number;
  end_hour: number;
  run_hours: number;
  is_trial: boolean;
  block_type: "sku" | "cip" | "trial";
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

/** Data sent from Python to React via args */
export interface SandboxArgs {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  capabilities: Record<string, Record<string, number>>; // line -> sku -> rate
  changeovers: Record<string, Record<string, number>>; // from -> to -> hours
  demandTargets: DemandTarget[];
  lines: LineInfo[];
  holdingArea: ScheduleBlock[];
  config: SandboxConfig;
}

/** State sent from React back to Python */
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
