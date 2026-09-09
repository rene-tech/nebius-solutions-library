/** Actual retained exchanges, separate from logical operations and their results. */
export interface DebugBody {
  encoding: "utf-8" | "base64";
  data: string;
  content_type: string | null;
  observed_bytes: number;
  complete: boolean;
  redacted: boolean;
}

export interface DebugExchangeSummary {
  id: string;
  source: "public" | "upstream";
  request_id: string | null;
  operation_id: string | null;
  operation_attempt: number | null;
  upstream_attempt: number | null;
  started_at: string;
  completed_at: string | null;
  tenant_id: string | null;
  principal_id: string | null;
  token_id: string | null;
  model_id: string | null;
  mcp_tool: string | null;
  endpoint: string;
  method: string;
  http_status: number | null;
  error_type: string | null;
  disconnected: boolean;
  request_observed_bytes: number;
  response_observed_bytes: number;
  request_complete: boolean;
  response_complete: boolean;
  request_redacted: boolean;
  response_redacted: boolean;
}

export interface DebugExchange extends Omit<
  DebugExchangeSummary,
  | "request_observed_bytes"
  | "response_observed_bytes"
  | "request_complete"
  | "response_complete"
  | "request_redacted"
  | "response_redacted"
> {
  error_detail: string | null;
  query_string: string;
  request_headers: [string, string][];
  response_headers: [string, string][];
  request_body: DebugBody;
  response_body: DebugBody;
}

export interface DebugExchangeList {
  items: DebugExchangeSummary[];
  next_cursor: string | null;
}
