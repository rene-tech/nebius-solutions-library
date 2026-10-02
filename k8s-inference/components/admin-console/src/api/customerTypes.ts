import type { UserRow } from "./userTypes";

export interface CloudObservation {
  resource_id: string;
  project_id: string;
  kind: "endpoint" | "bucket";
  name: string;
  observed_at: string;
  observation_state?: "available" | "stale" | "unavailable";
  state?: string;
  image?: string;
  platform?: string;
  preset?: string;
  url?: string | null;
  buckets?: string[];
  is_workbench?: boolean;
  size_bytes?: number | null;
  quota_bytes?: number | null;
  purpose?: string | null;
  managed_by?: string | null;
  workbench_id?: string | null;
  tenant_id?: string | null;
  mounted_by?: string[];
  protected?: boolean;
  state_filesystem_id?: string | null;
  persistent_state?: boolean;
}

export interface Workbench {
  id: string;
  tenant_id: string;
  principal_ids: string[];
  name: string;
  management: "managed" | "customer";
  endpoint_id: string;
  project_id: string;
  bucket_name: string | null;
  state_filesystem_id?: string | null;
  protected: boolean;
  protection_reason: string;
  desired_release: string | null;
  revision: number;
  observation: CloudObservation | null;
  observation_state: "available" | "stale" | "unavailable";
  operations: { id: string; kind: string; state: string; created_at: string; error_code: string | null }[];
}

export interface Customer {
  tenant_id: string;
  profile: { display_name: string; purpose: string; archived: boolean };
  users: UserRow[];
  model_usage: { model_id: string; requests: number; succeeded: number; failed: number; in_progress: number; users: number; last_request_at: string }[];
  requests: number;
  active_keys: number;
  last_request_at: string | null;
  buckets: { bucket_id: string; bucket_name: string; owner_key: string; mode: string; quota_bytes: number; region: string; endpoint: string; observation: CloudObservation | null; observation_state: string }[];
  workbenches: Workbench[];
}

export interface CustomerList {
  items: Customer[];
  customer?: Customer;
  truncated: boolean;
  inventory_available: boolean;
  inventory_error: string | null;
  lifecycle_executor_available: boolean;
  releases?: Record<string, string>;
  attribution: string;
}

export interface WorkbenchInventory {
  items: CloudObservation[];
  last_error: string | null;
}
