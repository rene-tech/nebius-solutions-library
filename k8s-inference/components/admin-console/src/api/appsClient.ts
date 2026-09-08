import { envelopeRequest } from "./client";
import type {
  AppSummary,
  AppList,
  AppCreate,
  AppMetrics,
  AppLogs,
  AppContainers,
  AppRun,
  AppRuns,
  AppSettings,
  AppSettingsUpdate,
  AppUsage,
} from "./appsTypes";
import { sharedContextParams } from "../lib/search";

export function appQuery(
  context: URLSearchParams,
  extra: Record<string, string | undefined> = {},
) {
  const query = sharedContextParams(context);
  Object.entries(extra).forEach(([key, value]) => {
    if (value) query.set(key, value);
  });
  return query;
}
const appPath = (id: string) => `/apps/${encodeURIComponent(id)}`;
export const appsApi = {
  list: (
    context: URLSearchParams,
    filters: { search?: string; cursor?: string } = {},
    signal?: AbortSignal,
  ) =>
    envelopeRequest<AppList>("/apps", {
      query: appQuery(context, filters),
      signal,
    }),
  detail: (id: string, context: URLSearchParams, signal?: AbortSignal) =>
    envelopeRequest<AppSummary>(appPath(id), {
      query: appQuery(context),
      signal,
    }),
  create: (body: AppCreate, signal?: AbortSignal) =>
    envelopeRequest<AppSummary>("/apps", { method: "POST", body, signal }),
  runs: (
    id: string,
    context: URLSearchParams,
    filters: { status?: string; principal_id?: string; cursor?: string } = {},
    signal?: AbortSignal,
  ) =>
    envelopeRequest<AppRuns>(`${appPath(id)}/runs`, {
      query: appQuery(context, { ...filters, limit: "100" }),
      signal,
    }),
  run: (
    id: string,
    operationId: string,
    context: URLSearchParams,
    signal?: AbortSignal,
  ) =>
    envelopeRequest<AppRun>(
      `${appPath(id)}/runs/${encodeURIComponent(operationId)}`,
      { query: appQuery(context), signal },
    ),
  settings: (id: string, context: URLSearchParams, signal?: AbortSignal) =>
    envelopeRequest<AppSettings>(`${appPath(id)}/settings`, {
      query: appQuery(context),
      signal,
    }),
  updateSettings: (
    id: string,
    body: AppSettingsUpdate,
    context: URLSearchParams,
    signal?: AbortSignal,
  ) =>
    envelopeRequest<AppSettings>(`${appPath(id)}/settings`, {
      method: "PATCH",
      body,
      query: appQuery(context),
      signal,
    }),
  usage: (id: string, context: URLSearchParams, signal?: AbortSignal) =>
    envelopeRequest<AppUsage>(`${appPath(id)}/usage`, {
      query: appQuery(context),
      signal,
    }),
  metrics: (id: string, context: URLSearchParams, signal?: AbortSignal) =>
    envelopeRequest<AppMetrics>(`${appPath(id)}/metrics`, {
      query: appQuery(context),
      signal,
    }),
  logs: (
    id: string,
    context: URLSearchParams,
    filters: {
      search?: string;
      pod?: string;
      container?: string;
      cursor?: string;
    } = {},
    signal?: AbortSignal,
  ) =>
    envelopeRequest<AppLogs>(`${appPath(id)}/logs`, {
      query: appQuery(context, { ...filters, limit: "100" }),
      signal,
    }),
  containers: (id: string, context: URLSearchParams, signal?: AbortSignal) =>
    envelopeRequest<AppContainers>(`${appPath(id)}/containers`, {
      query: appQuery(context),
      signal,
    }),
};
