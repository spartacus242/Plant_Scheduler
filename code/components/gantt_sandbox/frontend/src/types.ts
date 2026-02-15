// types.ts — Shared types for the Gantt sandbox component.
// These match the Python-side data contracts in helpers/sandbox_engine.py.

export interface ScheduleBlock {
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

/** Data sent from Python to React */
export interface SandboxData {
  schedule: ScheduleBlock[];
  cipWindows: ScheduleBlock[];
  capabilities: Record<string, Record<string, number>>; // line_name -> sku -> rate
  changeovers: Record<string, Record<string, number>>; // from_sku -> to_sku -> hours
  demandTargets: DemandTarget[];
  lines: LineInfo[];
  holdingArea: ScheduleBlock[];
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
