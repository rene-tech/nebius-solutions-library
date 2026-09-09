import { envelopeRequest } from "./client";
import { appQuery } from "./appsClient";
import type { DebugExchange, DebugExchangeList } from "./requestDebugTypes";

function requestPath(appId?: string) {
  return appId ? `/apps/${encodeURIComponent(appId)}/requests` : "/requests";
}

export const requestDebugApi = {
  list: (
    appId: string | undefined,
    context: URLSearchParams,
    filters: { operation_id?: string; cursor?: string } = {},
    signal?: AbortSignal,
  ) =>
    envelopeRequest<DebugExchangeList>(requestPath(appId), {
      query: appQuery(context, { ...filters, limit: "50" }),
      signal,
    }),
  detail: (
    appId: string | undefined,
    exchangeId: string,
    context: URLSearchParams,
    signal?: AbortSignal,
  ) =>
    envelopeRequest<DebugExchange>(
      `${requestPath(appId)}/${encodeURIComponent(exchangeId)}`,
      {
        query: appQuery(context),
        signal,
      },
    ),
};
