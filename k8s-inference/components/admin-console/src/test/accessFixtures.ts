import type {
  AdminApiKey,
  AuditEvent,
  OperatorPrincipal,
  OperatorSession,
} from "../api/accessTypes";
import type { AdminEnvelope } from "../api/types";

export const testPrincipal: OperatorPrincipal = {
  id: "00000000-0000-0000-0000-000000000001",
  subject: "admin@example.test",
  display_name: "Admin operator",
  kind: "human",
  role: "admin",
  tenant_id: null,
  enabled: true,
  created_at: "2026-08-30T08:00:00Z",
  created_by: "bootstrap",
  updated_at: "2026-08-30T08:00:00Z",
  disabled_at: null,
};

export const tenantPrincipal: OperatorPrincipal = {
  ...testPrincipal,
  id: "8bcedf23-947a-46c8-9936-35293b15792a",
  subject: "agent-a",
  display_name: "Agent A",
  kind: "service",
  role: "operator",
  tenant_id: "tenant-a",
};

export const testSession: OperatorSession = {
  id: "ba0ec598-d8d8-4b92-92ce-d289a34b49f8",
  principal: testPrincipal,
  created_at: "2026-08-30T08:00:00Z",
  expires_at: "2026-08-30T16:00:00Z",
  last_seen_at: "2026-08-30T09:00:00Z",
  revoked_at: null,
};

export const testKey: AdminApiKey = {
  id: "c19da908-cb0e-421c-b849-4cb50086ec65",
  name: "Agent A key",
  prefix: "fs2_pat_c19da908cb0e",
  fingerprint: "d".repeat(64),
  principal_id: "agent-a",
  tenant_id: "tenant-a",
  scopes: ["inference.invoke", "mcp.invoke"],
  models: ["qwen3-8b"],
  state: "active",
  expires_at: null,
  last_used_at: null,
  request_budget: 100,
  requests_used: 4,
  gpu_seconds_budget: 500,
  gpu_seconds_used: 5,
  gpu_seconds_reserved: 1,
  max_concurrency: 2,
  rate_limit_requests: 10,
  rate_window_seconds: 60,
  rate_window_started_at: "2026-08-30T08:59:00Z",
  rate_window_requests: 2,
  rotation_parent_id: null,
  rotated_at: null,
  created_at: "2026-08-30T08:00:00Z",
  created_by: "admin@example.test",
  revoked_at: null,
  usage: {
    terminal_operations: 4,
    estimated_gpu_seconds: { value: 5, unit: "gpu-seconds", state: "estimated", reason: "admission reservation accounting" },
    input_tokens: { value: 40, unit: "tokens", state: "available", reason: null },
    output_tokens: { value: 20, unit: "tokens", state: "available", reason: null },
    token_reported_operations: 4,
    modality_reported_operations: 0,
    modality_units: [],
    modality_state: "unavailable",
    modality_reason: "runtime modality reporting is incomplete",
  },
};

export const testAudit: AuditEvent = {
  id: 12,
  occurred_at: "2026-08-30T09:00:00Z",
  actor: "admin@example.test",
  tenant_id: "tenant-a",
  token_id: testKey.id,
  action: "token.issue",
  target_type: "token",
  target_id: testKey.id,
  outcome: "succeeded",
  detail: { principal_id: "agent-a" },
};

export function testEnvelope<T>(data: T): AdminEnvelope<T> {
  return {
    meta: {
      schema_version: "fs2.admin-api/v1",
      generated_at: "2026-08-30T09:00:00Z",
      context: {
        project: "project-test",
        cluster: "cluster-test",
        region: "region-test",
        from_at: "2026-08-30T08:00:00Z",
        to_at: "2026-08-30T09:00:00Z",
        timezone: "UTC",
      },
      sources: [],
      warnings: [],
    },
    data,
  };
}
