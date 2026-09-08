import type { AdminMeasurement } from "./types";

export interface CapacityPoolSummary {
  pool_id: string;
  gpu_type: string | null;
  capacity_type: string;
  resource_name: string;
  nodes: number;
  ready_nodes: number;
  not_ready_nodes: number;
  cordoned_nodes: number;
  total_gpus: AdminMeasurement;
  allocated_gpus: AdminMeasurement;
  schedulable_free_gpus: AdminMeasurement;
  starting_workers: AdminMeasurement;
  ready_workers: AdminMeasurement;
  configured_min_nodes: number | null;
  configured_max_nodes: number | null;
  configured_additional_nodes: AdminMeasurement;
}
export interface CapacitySummary {
  observed_at: string;
  pools: CapacityPoolSummary[];
  gpu_utilization_percent: AdminMeasurement;
  loaded_idle_gpus: AdminMeasurement;
  pending_customer_runs: AdminMeasurement;
  oldest_pending_seconds: AdminMeasurement;
  pending_batch_shards: AdminMeasurement;
  starting_gpu_workers: AdminMeasurement;
  notes: string[];
}
