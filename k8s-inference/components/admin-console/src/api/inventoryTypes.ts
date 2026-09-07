export interface SnapshotStartupMeasurement {
  clock: string;
  n: number;
  median_seconds: number;
  min_seconds: number | null;
  max_seconds: number | null;
  cache: string;
}

export interface ModelInventoryItem {
  model_id: string;
  display_name: string;
  family: string;
  availability: string;
  reason: string;
  configured: boolean;
  serving_enabled: boolean;
  serving_state: string | null;
  batch_readiness: string | null;
  ready_replicas: number | null;
  desired_replicas: number | null;
  runtime_image_digest: string | null;
  gpu_snapshot: "verified" | "candidate" | "unsupported" | "unavailable" | "not-reported";
  snapshot_reason: string;
  snapshot_evidence_scope?: string;
  snapshot_selectable?: boolean;
  snapshot_bundle_ids?: string[];
  snapshot_normal_startup?: SnapshotStartupMeasurement | null;
  snapshot_restore_startup?: SnapshotStartupMeasurement | null;
  management_path: string | null;
}

export interface ModelInventory {
  items: ModelInventoryItem[];
  total: number;
  configured: number;
  not_deployed: number;
  scientific_projection_available: boolean;
}
