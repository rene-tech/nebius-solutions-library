import type { AdminEnvelope, ValueState } from "./types";

export type OperatorRole = "viewer" | "operator" | "admin";
export type PrincipalKind = "human" | "service";
export type ApiKeyState = "active" | "expired" | "revoked" | "rotated";

export interface OperatorPrincipal {
  id: string;
  subject: string;
  display_name: string;
  kind: PrincipalKind;
  role: OperatorRole;
  tenant_id: string | null;
  enabled: boolean;
  created_at: string;
  created_by: string;
  updated_at: string;
  disabled_at: string | null;
}

export interface OperatorSession {
  id: string;
  principal: OperatorPrincipal;
  created_at: string;
  expires_at: string;
  last_seen_at: string;
  revoked_at: string | null;
}

export interface AccessMeasurement {
  value: number | null;
  unit: string;
  state: ValueState;
  reason: string | null;
}

export interface ModalityUsage {
  modality: string;
  direction: "input" | "output";
  unit: string;
  amount: number;
}

export interface AdminApiKeyUsage {
  terminal_operations: number;
  estimated_gpu_seconds: AccessMeasurement;
  input_tokens: AccessMeasurement;
  output_tokens: AccessMeasurement;
  token_reported_operations: number;
  modality_reported_operations: number;
  modality_units: ModalityUsage[];
  modality_state: ValueState;
  modality_reason: string | null;
}

export interface AdminApiKey {
  id: string;
  name: string | null;
  prefix: string;
  fingerprint: string | null;
  principal_id: string;
  tenant_id: string;
  scopes: string[];
  models: string[];
  state: ApiKeyState;
  expires_at: string | null;
  last_used_at: string | null;
  request_budget: number | null;
  requests_used: number;
  gpu_seconds_budget: number | null;
  gpu_seconds_used: number;
  gpu_seconds_reserved: number;
  max_concurrency: number;
  rate_limit_requests: number | null;
  rate_window_seconds: number | null;
  rate_window_started_at: string | null;
  rate_window_requests: number;
  rotation_parent_id: string | null;
  rotated_at: string | null;
  created_at: string;
  created_by: string;
  revoked_at: string | null;
  usage: AdminApiKeyUsage;
}

export interface AdminApiKeyDisclosure {
  key: AdminApiKey;
  secret: string;
}

export interface AuditEvent {
  id: number;
  occurred_at: string;
  actor: string;
  tenant_id: string | null;
  token_id: string | null;
  action: string;
  target_type: string;
  target_id: string;
  outcome: string;
  detail: Record<string, unknown>;
}

export interface PrincipalList {
  items: OperatorPrincipal[];
}

export interface ApiKeyList {
  items: AdminApiKey[];
}

export interface AuditList {
  items: AuditEvent[];
}

export interface OperatorPrincipalCreateInput {
  subject: string;
  display_name: string;
  kind: PrincipalKind;
  role: OperatorRole;
  tenant_id: string | null;
}

export interface OperatorPrincipalPatchInput {
  display_name?: string;
  role?: OperatorRole;
  enabled?: boolean;
}

export interface AdminApiKeyCreateInput {
  name: string;
  principal_id: string;
  tenant_id: string;
  scopes: string[];
  models: string[];
  expires_at?: string | null;
  request_budget?: number | null;
  gpu_seconds_budget?: number | null;
  max_concurrency: number;
  rate_limit_requests?: number | null;
  rate_window_seconds?: number | null;
}

export interface AdminApiKeyPolicyPatchInput {
  name?: string;
  scopes?: string[];
  models?: string[];
  expires_at?: string | null;
  request_budget?: number | null;
  gpu_seconds_budget?: number | null;
  max_concurrency?: number;
  rate_limit_requests?: number | null;
  rate_window_seconds?: number | null;
}

export interface AdminApiKeyRotateInput {
  name?: string;
  expires_at?: string | null;
}

export type SessionEnvelope = AdminEnvelope<OperatorSession>;
