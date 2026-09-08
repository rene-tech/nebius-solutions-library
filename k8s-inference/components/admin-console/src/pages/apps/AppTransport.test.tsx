import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type {
  ObservedTransport,
  ObservedTransportUsage,
} from "../../api/appsTypes";
import { AppRunTransport, AppTransportUsage } from "./AppTransport";

const observation: ObservedTransport = {
  request_id: "exchange-one",
  started_at: "2026-09-08T12:00:00Z",
  completed_at: "2026-09-08T12:00:03Z",
  endpoint: "/mcp",
  method: "POST",
  transport: "mcp",
  http_status: 200,
  response_duration_seconds: 3,
  request_bytes: 0,
  response_bytes: 12,
  request_bytes_observed: 0,
  response_bytes_observed: 12,
  request_complete: true,
  response_complete: true,
  disconnected: false,
  error_type: null,
  tenant_id: "tenant-one",
  principal_id: "owner",
  token_id: "token-identity-not-secret",
  model_id: "model-one",
  operation_id: "operation-one",
  mcp_tool: "invoke_model",
  mcp_is_error: true,
};
const usage: ObservedTransportUsage = {
  request_count: 3,
  completed_response_count: 2,
  incomplete_response_count: 1,
  successful_http_count: 2,
  failed_http_count: 1,
  mcp_tool_error_count: 1,
  request_bytes: 0,
  response_bytes: null,
  request_bytes_known_count: 3,
  response_bytes_known_count: 2,
  average_response_duration_seconds: 3,
  maximum_response_duration_seconds: 4,
  first_observed_at: "2026-09-08T12:00:00Z",
  last_observed_at: "2026-09-08T12:00:03Z",
  status_classes: { "2xx": 2, "5xx": 1 },
  time_bucket_seconds: 60,
  requests_over_time: [
    {
      timestamp: "2026-09-08T11:59:00Z",
      request_count: null,
      status_classes: null,
    },
    {
      timestamp: "2026-09-08T12:00:00Z",
      request_count: 3,
      status_classes: { "2xx": 2, "5xx": 1 },
    },
  ],
};

describe("actual request observations", () => {
  it("does not infer historical endpoint, method or bytes from an operation", () => {
    render(<AppRunTransport observations={[]} />);
    expect(
      screen.getByText(
        /Historical endpoint, method, tool, duration and byte sizes are not inferred/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
  it("shows actual HTTP200 and MCP tool error independently, with meaningful zero bytes", () => {
    render(<AppRunTransport observations={[observation]} />);
    expect(screen.getByText("200")).toBeInTheDocument();
    expect(screen.getByText("MCP tool error")).toBeInTheDocument();
    expect(screen.getByText("invoke_model")).toBeInTheDocument();
    expect(screen.getByText("0 B / 12 B")).toBeInTheDocument();
    expect(
      screen.queryByText("token-identity-not-secret"),
    ).not.toBeInTheDocument();
  });
  it("labels interrupted duration and incomplete bytes explicitly", () => {
    render(
      <AppRunTransport
        observations={[
          { ...observation, response_complete: false, response_bytes: null },
        ]}
      />,
    );
    expect(
      screen.getByText("Incomplete response; elapsed until interruption"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Observed partial payload: 0 \/ 12 B/),
    ).toBeInTheDocument();
    expect(screen.getByText("0 B / —")).toBeInTheDocument();
  });
  it("keeps transport coverage and request count separate from historical logical runs", () => {
    render(<AppTransportUsage usage={usage} />);
    expect(screen.getByText("3 / 3 bodies fully observed")).toBeInTheDocument();
    expect(screen.getByText("2 / 3 bodies fully observed")).toBeInTheDocument();
    expect(
      screen.getByText(/Unrecorded historical traffic is not reconstructed/),
    ).toBeInTheDocument();
    expect(screen.getByText("MCP tool errors")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /Actual HTTP responses over time/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Unobserved buckets remain gaps/),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Actual HTTP response classes"),
    ).toHaveTextContent("5xx: 1");
  });
  it("does not call an unobserved transport window zero historical traffic", () => {
    render(
      <AppTransportUsage
        usage={{ ...usage, request_count: 0, first_observed_at: null }}
      />,
    );
    expect(
      screen.getByText(
        /Historical request counts, byte sizes and response latency are unavailable/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("HTTP 2xx / 3xx")).not.toBeInTheDocument();
  });
});
