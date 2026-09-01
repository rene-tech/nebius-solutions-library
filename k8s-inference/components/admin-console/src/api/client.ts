import type {
  AdminCapacity,
  AdminContextData,
  AdminEnvelope,
  AdminModelDetail,
  AdminModelList,
  AdminObservability,
  AdminOperationDetail,
  AdminOperationList,
  AdminOverview,
  ModelState,
  OperationState,
} from "./types";
import type {
  AdminApiKey,
  AdminApiKeyCreateInput,
  AdminApiKeyDisclosure,
  AdminApiKeyPolicyPatchInput,
  AdminApiKeyRotateInput,
  ApiKeyList,
  AuditList,
  OperatorPrincipal,
  OperatorPrincipalCreateInput,
  OperatorPrincipalPatchInput,
  OperatorSession,
  PrincipalList,
} from "./accessTypes";
import type {
  ConfigurationDiff,
  ConfigurationPlan,
  ConfigurationProposal,
  ConfigurationRevision,
  ConfigurationValidation,
  ReconcileRequest,
  ReconciliationStatus,
  RollbackPlan,
  RollbackRequest,
} from "./configurationTypes";
import { sharedContextParams } from "../lib/search";

const API_PREFIX = "/admin/api/v1";
const queryValueMaximum: Readonly<Record<string, number>> = {
  limit: 3,
  state: 11,
  search: 128,
  cursor: 512,
  tenant_id: 120,
  model_id: 128,
  principal_id: 200,
  api_key_prefix: 64,
  status: 10,
  error_code: 64,
  operation_id: 36,
};

export class AdminApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly requestId: string | null,
    readonly code: string | null = null,
  ) {
    super(message);
    this.name = "AdminApiError";
  }
}

function boundedParams(input?: URLSearchParams, extra?: Record<string, string | undefined>) {
  const output = sharedContextParams(input ?? new URLSearchParams());
  Object.entries(extra ?? {}).forEach(([key, value]) => {
    const maximum = queryValueMaximum[key];
    if (value && maximum !== undefined && value.length <= maximum) output.set(key, value);
  });
  return output;
}

function isEnvelope(value: unknown): value is AdminEnvelope<unknown> {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return Boolean(candidate.meta && typeof candidate.meta === "object" && "data" in candidate);
}

async function problemFor(response: Response): Promise<AdminApiError> {
  let requestId = response.headers.get("x-request-id");
  let code: string | null = null;
  const fallback: Record<number, string> = {
    400: "The admin request was rejected.",
    401: "Authentication was rejected.",
    403: "Your operator role does not permit this action.",
    404: "The requested admin resource was not found.",
    409: "The requested change conflicts with current state.",
    422: "The admin request failed validation.",
    429: "The admin service is rate limiting requests.",
    502: "The admin gateway returned an invalid response.",
    503: "The admin service is temporarily unavailable.",
  };
  let message = fallback[response.status] ?? `Admin API request failed (${response.status}).`;
  try {
    const payload: unknown = await response.json();
    if (payload && typeof payload === "object") {
      const candidate = payload as Record<string, unknown>;
      const nested = candidate.error && typeof candidate.error === "object"
        ? candidate.error as Record<string, unknown>
        : null;
      const detail = candidate.detail ?? nested?.message;
      const responseCode = candidate.code ?? nested?.type;
      const bodyRequestId = candidate.request_id;
      if (typeof detail === "string" && detail.length > 0 && detail.length <= 320) message = detail;
      if (typeof responseCode === "string" && responseCode.length > 0 && responseCode.length <= 64) code = responseCode;
      if (!requestId && typeof bodyRequestId === "string" && bodyRequestId.length <= 64) requestId = bodyRequestId;
    }
  } catch {
    // A bounded generic error is safer than reflecting an arbitrary response.
  }
  return new AdminApiError(message, response.status, requestId, code);
}

export interface ModelQuery {
  state?: ModelState;
  search?: string;
  limit?: number;
}

export interface OperationQuery {
  cursor?: string;
  tenantId?: string;
  modelId?: string;
  principalId?: string;
  apiKeyPrefix?: string;
  status?: OperationState;
  errorCode?: string;
  limit?: number;
}

function boundedLimit(value: number | undefined, maximum: number, fallback: number): string {
  if (value === undefined || !Number.isInteger(value)) return String(fallback);
  return String(Math.min(Math.max(value, 1), maximum));
}

interface EnvelopeRequest {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: URLSearchParams;
  body?: unknown;
  authorization?: string;
  signal?: AbortSignal;
  notifySessionExpiry?: boolean;
}

async function envelopeRequest<T>(path: string, options: EnvelopeRequest = {}): Promise<AdminEnvelope<T>> {
  const query = options.query?.toString() ?? "";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.authorization) headers.Authorization = options.authorization;
  const response = await fetch(`${API_PREFIX}${path}${query ? `?${query}` : ""}`, {
    method: options.method ?? "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  if (!response.ok) {
    if (response.status === 401 && options.notifySessionExpiry !== false && typeof window !== "undefined") {
      window.dispatchEvent(new Event("fs2:operator-session-expired"));
    }
    throw await problemFor(response);
  }
  const payload: unknown = await response.json();
  if (!isEnvelope(payload)) {
    throw new AdminApiError("Admin API returned an incompatible envelope", 502, null);
  }
  return payload as AdminEnvelope<T>;
}

async function request<T>(
  path: string,
  context?: URLSearchParams,
  extra?: Record<string, string | undefined>,
  signal?: AbortSignal,
): Promise<AdminEnvelope<T>> {
  return envelopeRequest<T>(path, { query: boundedParams(context, extra), signal });
}

function accessQuery(tenantId?: string, limit = 200): URLSearchParams {
  const query = new URLSearchParams({ limit: String(Math.min(Math.max(limit, 1), 1000)) });
  if (tenantId && tenantId.length <= 120) query.set("tenant_id", tenantId);
  return query;
}

async function noContentRequest(path: string, method: "DELETE", signal?: AbortSignal): Promise<void> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) throw await problemFor(response);
}

export const adminApi = {
  session: (signal?: AbortSignal) =>
    envelopeRequest<OperatorSession>("/session", { signal, notifySessionExpiry: false }),
  createSession: (bootstrapToken: string, principalId?: string, signal?: AbortSignal) =>
    envelopeRequest<OperatorSession>("/session", {
      method: "POST",
      authorization: `Bearer ${bootstrapToken}`,
      body: principalId ? { principal_id: principalId } : undefined,
      signal,
      notifySessionExpiry: false,
    }),
  deleteSession: (signal?: AbortSignal) => noContentRequest("/session", "DELETE", signal),
  context: (context: URLSearchParams, signal?: AbortSignal) =>
    request<AdminContextData>("/context", context, undefined, signal),
  overview: (context: URLSearchParams, signal?: AbortSignal) =>
    request<AdminOverview>("/overview", context, undefined, signal),
  models: (context: URLSearchParams, filters: ModelQuery = {}, signal?: AbortSignal) =>
    request<AdminModelList>("/models", context, {
      limit: boundedLimit(filters.limit, 256, 200),
      state: filters.state,
      search: filters.search,
    }, signal),
  model: (modelId: string, context: URLSearchParams, signal?: AbortSignal) =>
    request<AdminModelDetail>(`/models/${encodeURIComponent(modelId)}`, context, undefined, signal),
  operations: (context: URLSearchParams, filters: OperationQuery = {}, signal?: AbortSignal) =>
    request<AdminOperationList>("/operations", context, {
      limit: boundedLimit(filters.limit, 200, 100),
      cursor: filters.cursor,
      tenant_id: filters.tenantId,
      model_id: filters.modelId,
      principal_id: filters.principalId,
      api_key_prefix: filters.apiKeyPrefix,
      status: filters.status,
      error_code: filters.errorCode,
    }, signal),
  operation: (operationId: string, context: URLSearchParams, signal?: AbortSignal) =>
    request<AdminOperationDetail>(
      `/operations/${encodeURIComponent(operationId)}`,
      context,
      undefined,
      signal,
    ),
  capacity: (context: URLSearchParams, signal?: AbortSignal) =>
    request<AdminCapacity>("/capacity", context, undefined, signal),
  observability: (
    context: URLSearchParams,
    selectors?: { modelId?: string; operationId?: string },
    signal?: AbortSignal,
  ) => request<AdminObservability>(
    "/observability",
    context,
    { model_id: selectors?.modelId, operation_id: selectors?.operationId },
    signal,
  ),
  configuration: (signal?: AbortSignal) =>
    envelopeRequest<ConfigurationRevision>("/configuration", { signal }),
  configurationDiff: (proposal: ConfigurationProposal, signal?: AbortSignal) =>
    envelopeRequest<ConfigurationDiff>("/configuration:diff", {
      method: "POST",
      body: proposal,
      signal,
    }),
  validateConfiguration: (proposal: ConfigurationProposal, signal?: AbortSignal) =>
    envelopeRequest<ConfigurationValidation>("/configuration:validate", {
      method: "POST",
      body: proposal,
      signal,
    }),
  planConfiguration: (proposal: ConfigurationProposal, signal?: AbortSignal) =>
    envelopeRequest<ConfigurationPlan>("/configuration:plan", {
      method: "POST",
      body: proposal,
      signal,
    }),
  reconcileConfiguration: (payload: ReconcileRequest, signal?: AbortSignal) =>
    envelopeRequest<ReconciliationStatus>("/configuration:reconcile", {
      method: "POST",
      body: payload,
      signal,
    }),
  reconciliationStatus: (reconciliationId: string, signal?: AbortSignal) =>
    envelopeRequest<ReconciliationStatus>(
      `/configuration/reconciliations/${encodeURIComponent(reconciliationId)}`,
      { signal },
    ),
  rollbackConfiguration: (payload: RollbackRequest, signal?: AbortSignal) =>
    envelopeRequest<RollbackPlan>("/configuration:rollback", {
      method: "POST",
      body: payload,
      signal,
    }),
  principals: (tenantId?: string, signal?: AbortSignal) =>
    envelopeRequest<PrincipalList>("/principals", { query: accessQuery(tenantId), signal }),
  createPrincipal: (payload: OperatorPrincipalCreateInput, signal?: AbortSignal) =>
    envelopeRequest<OperatorPrincipal>("/principals", { method: "POST", body: payload, signal }),
  updatePrincipal: (principalId: string, payload: OperatorPrincipalPatchInput, signal?: AbortSignal) =>
    envelopeRequest<OperatorPrincipal>(`/principals/${encodeURIComponent(principalId)}`, {
      method: "PATCH",
      body: payload,
      signal,
    }),
  keys: (tenantId?: string, signal?: AbortSignal) =>
    envelopeRequest<ApiKeyList>("/keys", { query: accessQuery(tenantId), signal }),
  issueKey: (payload: AdminApiKeyCreateInput, signal?: AbortSignal) =>
    envelopeRequest<AdminApiKeyDisclosure>("/keys", { method: "POST", body: payload, signal }),
  updateKey: (tokenId: string, payload: AdminApiKeyPolicyPatchInput, signal?: AbortSignal) =>
    envelopeRequest<AdminApiKey>(`/keys/${encodeURIComponent(tokenId)}`, {
      method: "PATCH",
      body: payload,
      signal,
    }),
  rotateKey: (tokenId: string, payload: AdminApiKeyRotateInput, signal?: AbortSignal) =>
    envelopeRequest<AdminApiKeyDisclosure>(`/keys/${encodeURIComponent(tokenId)}:rotate`, {
      method: "POST",
      body: payload,
      signal,
    }),
  revokeKey: (tokenId: string, signal?: AbortSignal) =>
    envelopeRequest<AdminApiKey>(`/keys/${encodeURIComponent(tokenId)}`, {
      method: "DELETE",
      signal,
    }),
  audit: (tenantId?: string, limit = 200, signal?: AbortSignal) =>
    envelopeRequest<AuditList>("/audit", { query: accessQuery(tenantId, limit), signal }),
};
