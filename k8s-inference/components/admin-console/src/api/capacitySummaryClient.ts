import { envelopeRequest } from "./client";
import type { CapacitySummary } from "./capacitySummaryTypes";

export const capacitySummaryApi = {
  get: (query: URLSearchParams, signal?: AbortSignal) =>
    envelopeRequest<CapacitySummary>("/capacity/summary", { query, signal }),
};
