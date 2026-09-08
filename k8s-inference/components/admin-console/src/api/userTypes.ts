import type { AdminApiKey, PrincipalKind } from "./accessTypes";
import type { AdminMeasurement } from "./types";

export interface UserSettings {
  display_name: string;
  kind: PrincipalKind | null;
  team: string | null;
  enabled: boolean;
  academic_eligible: boolean | null;
  app_ids: string[] | null;
}
export interface InferenceUser extends UserSettings {
  id: string;
  tenant_id: string;
  principal_id: string;
  source: "configured" | "existing-key-owner";
  created_at: string;
  updated_at: string;
}
export interface UserUsage {
  requests: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  pending: number;
  running: number;
  scientific_requests: number;
  last_request_at: string | null;
  scheduler_occupied_gpu_seconds: AdminMeasurement;
  active_gpu_seconds: AdminMeasurement;
  occupied_idle_gpu_seconds: AdminMeasurement;
  input_tokens: AdminMeasurement;
  output_tokens: AdminMeasurement;
  attribution: string;
  request_series: { at: string; requests: number }[];
  bucket_seconds: number;
}
export interface UserRow extends InferenceUser {
  key_count: number;
  active_key_count: number;
  usage: UserUsage;
}
export interface UserList {
  items: UserRow[];
  limit: number;
  truncated: boolean;
  apps: UserAppChoice[];
}
export interface UserAppChoice {
  app_id: string;
  display_name: string;
  public_model_id: string;
  academic_required: boolean;
}
export interface UserDetail {
  user: UserRow;
  keys: AdminApiKey[];
  apps: UserAppChoice[];
  policy_note: string;
}
export interface UserCreate extends UserSettings {
  tenant_id: string;
  principal_id: string;
  kind: PrincipalKind;
}
export type UserPatch = Partial<UserSettings>;
