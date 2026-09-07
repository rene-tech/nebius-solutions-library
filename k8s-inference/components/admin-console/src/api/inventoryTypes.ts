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
  management_path: string | null;
}

export interface ModelInventory {
  items: ModelInventoryItem[];
  total: number;
  configured: number;
  not_deployed: number;
  scientific_projection_available: boolean;
}
