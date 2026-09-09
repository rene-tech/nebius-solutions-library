import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestDebugApi } from "../../api/requestDebugClient";
import type {
  DebugBody,
  DebugExchange,
  DebugExchangeSummary,
} from "../../api/requestDebugTypes";
import { appsApi } from "../../api/appsClient";
import { AdminApiError } from "../../api/client";
import { testEnvelope } from "../../test/accessFixtures";
import { AppRunsTab } from "./AppRunsTab";
import { DebugBodyView } from "./RequestDebugExchange";
import { GlobalRequestDebugLog, RequestDebugLog } from "./RequestDebugLog";

const requestBody: DebugBody = {
  encoding: "utf-8",
  data: '{"invalid":"technical-fixture"}',
  content_type: "application/json",
  observed_bytes: 31,
  complete: true,
  redacted: false,
};
const errorBody =
  '{"detail":[{"loc":["body","sequence"],"msg":"Field required","type":"missing"}]}';
const exchange: DebugExchange = {
  id: "exchange-one",
  source: "upstream",
  request_id: "request-one",
  operation_id: "run-one",
  operation_attempt: 2,
  upstream_attempt: 4,
  started_at: "2026-09-09T00:00:01.123456Z",
  completed_at: "2026-09-09T00:00:01.654321Z",
  tenant_id: "test-tenant",
  principal_id: "test-user",
  token_id: "key-id-only",
  model_id: "test-model",
  mcp_tool: null,
  endpoint: "/v1/predict",
  method: "POST",
  http_status: 422,
  error_type: "HTTPStatusError",
  error_detail: "Upstream returned 422: required field sequence is missing",
  disconnected: false,
  query_string: "validate=true&label=%3Cfixture%3E",
  request_headers: [
    ["content-type", "application/json"],
    ["authorization", "[REDACTED]"],
  ],
  response_headers: [
    ["content-type", "application/json"],
    ["x-trace", "one"],
    ["x-trace", "two"],
  ],
  request_body: requestBody,
  response_body: {
    encoding: "utf-8",
    data: errorBody,
    content_type: "application/json",
    observed_bytes: errorBody.length,
    complete: true,
    redacted: false,
  },
};

function summary(value: DebugExchange = exchange): DebugExchangeSummary {
  const {
    query_string,
    request_headers,
    response_headers,
    request_body,
    response_body,
    error_detail,
    ...metadata
  } = value;
  // The production list deliberately contains none of the destructured payload fields.
  void [query_string, request_headers, response_headers, error_detail];
  return {
    ...metadata,
    request_observed_bytes: request_body.observed_bytes,
    response_observed_bytes: response_body.observed_bytes,
    request_complete: request_body.complete,
    response_complete: response_body.complete,
    request_redacted: request_body.redacted,
    response_redacted: response_body.redacted,
  };
}

function WindowChange() {
  const navigate = useNavigate();
  return (
    <button
      onClick={() =>
        navigate("?from=2026-09-08T00:00:00Z&to=2026-09-08T01:00:00Z")
      }
    >
      Change fixed window
    </button>
  );
}

function renderPanel(element: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[
          "/admin/apps/app-one/runs?from=2026-09-09T00:00:00Z&to=2026-09-09T01:00:00Z",
        ]}
      >
        <WindowChange />
        {element}
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, client };
}

beforeEach(() => {
  vi.spyOn(requestDebugApi, "list").mockResolvedValue(
    testEnvelope({ items: [summary()], next_cursor: null }),
  );
  vi.spyOn(requestDebugApi, "detail").mockResolvedValue(testEnvelope(exchange));
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("actual request debug viewer", () => {
  it("does not fetch payloads until expansion and shows the exact upstream 422 body and metadata", async () => {
    renderPanel(<RequestDebugLog appId="app-one" operationId="run-one" />);
    expect(await screen.findByText("HTTP 422")).toBeInTheDocument();
    expect(screen.getByText("Model upstream")).toBeInTheDocument();
    expect(screen.getByText("Operation attempt 2")).toBeInTheDocument();
    expect(screen.getByText("Upstream attempt 4")).toBeInTheDocument();
    expect(requestDebugApi.detail).not.toHaveBeenCalled();
    expect(screen.queryByText(errorBody)).not.toBeInTheDocument();
    expect(requestDebugApi.list).toHaveBeenCalledWith(
      "app-one",
      expect.any(URLSearchParams),
      { operation_id: "run-one", cursor: undefined },
      expect.any(AbortSignal),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Inspect exchange exchange-one" }),
    );
    expect(
      await screen.findByLabelText("Response body content"),
    ).toHaveTextContent(errorBody);
    expect(screen.getByLabelText("Error detail")).toHaveTextContent(
      exchange.error_detail!,
    );
    expect(screen.getByLabelText("Query string")).toHaveTextContent(
      exchange.query_string,
    );
    expect(screen.getByText("2026-09-09T00:00:01.123456Z")).toBeInTheDocument();
    expect(screen.getByText("key-id-only")).toBeInTheDocument();
    expect(
      within(
        screen.getByRole("table", { name: "Response headers" }),
      ).getAllByText("x-trace"),
    ).toHaveLength(2);
    expect(requestDebugApi.detail).toHaveBeenCalledWith(
      "app-one",
      "exchange-one",
      expect.any(URLSearchParams),
      expect.any(AbortSignal),
    );
  });

  it("shows a rejected public request without an operation even when App runs are empty", async () => {
    vi.spyOn(appsApi, "runs").mockResolvedValue(
      testEnvelope({ items: [], next_cursor: null }),
    );
    vi.mocked(requestDebugApi.list).mockResolvedValue(
      testEnvelope({
        items: [
          summary({
            ...exchange,
            source: "public",
            operation_id: null,
            operation_attempt: null,
            upstream_attempt: null,
          }),
        ],
        next_cursor: null,
      }),
    );
    renderPanel(<AppRunsTab appId="app-one" />);
    expect(
      await screen.findByText(
        "No runs in the selected time window and filters.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("No operation")).toBeInTheDocument();
    expect(screen.getByText("HTTP 422")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Inspect exchange exchange-one" }),
    ).toBeInTheDocument();
    expect(requestDebugApi.list).toHaveBeenCalledWith(
      "app-one",
      expect.any(URLSearchParams),
      { operation_id: undefined, cursor: undefined },
      expect.any(AbortSignal),
    );
  });

  it("labels historical missing capture, without invented zero requests or success", async () => {
    vi.mocked(requestDebugApi.list).mockResolvedValue(
      testEnvelope({ items: [], next_cursor: null }),
    );
    renderPanel(<RequestDebugLog appId="app-one" operationId="historic-run" />);
    expect(
      await screen.findByText(/No request\/response capture recorded/),
    ).toHaveTextContent("this does not mean there were no requests");
    expect(screen.queryByText("HTTP 200")).not.toBeInTheDocument();
    expect(screen.queryByText("0 requests")).not.toBeInTheDocument();
    expect(requestDebugApi.detail).not.toHaveBeenCalled();
  });

  it("keeps unobserved HTTP status and identities explicit on a disconnected exchange", async () => {
    const value: DebugExchange = {
      ...exchange,
      operation_id: null,
      operation_attempt: null,
      upstream_attempt: null,
      request_id: null,
      tenant_id: null,
      principal_id: null,
      token_id: null,
      model_id: null,
      completed_at: null,
      http_status: null,
      error_type: "ConnectionError",
      error_detail: "Connection closed before headers",
      disconnected: true,
    };
    vi.mocked(requestDebugApi.list).mockResolvedValue(
      testEnvelope({ items: [summary(value)], next_cursor: null }),
    );
    vi.mocked(requestDebugApi.detail).mockResolvedValue(testEnvelope(value));
    renderPanel(<RequestDebugLog />);
    expect(await screen.findByText("HTTP not observed")).toBeInTheDocument();
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Inspect exchange exchange-one" }),
    );
    expect(await screen.findByLabelText("Error detail")).toHaveTextContent(
      "Connection closed before headers",
    );
    expect(screen.getAllByText("Not attributed")).toHaveLength(4);
    expect(screen.queryByText("HTTP 0")).not.toBeInTheDocument();
  });

  it("renders response and header markup as literal text, never HTML", async () => {
    const markup =
      '<img src=x onerror="window.fixture=1"><script>fixture()</script>';
    vi.mocked(requestDebugApi.detail).mockResolvedValue(
      testEnvelope({
        ...exchange,
        response_headers: [["x-fixture", markup]],
        response_body: {
          ...exchange.response_body,
          data: markup,
          content_type: "text/html",
        },
      }),
    );
    const { container } = renderPanel(<RequestDebugLog appId="app-one" />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Inspect exchange exchange-one",
      }),
    );
    expect(
      await screen.findByLabelText("Response body content"),
    ).toHaveTextContent(markup);
    expect(container.querySelector("img,script")).toBeNull();
  });

  it("shows base64, incomplete and redacted bodies without pretending displayed length is wire bytes", async () => {
    const body: DebugBody = {
      encoding: "base64",
      data: "AAH/",
      content_type: "application/octet-stream",
      observed_bytes: 2048,
      complete: false,
      redacted: true,
    };
    renderPanel(<DebugBodyView label="Response body" body={body} />);
    expect(screen.getByLabelText("Response body content")).toHaveTextContent(
      "AAH/",
    );
    expect(screen.getByText("2,048")).toBeInTheDocument();
    expect(screen.getByText("Partial / incomplete")).toBeInTheDocument();
    expect(screen.getByText("Redacted")).toBeInTheDocument();
    expect(
      screen.getByText(/Binary body displayed as base64/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Only observed bytes are shown/),
    ).toBeInTheDocument();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    fireEvent.click(
      screen.getByRole("button", { name: "Copy response body (base64)" }),
    );
    expect(await screen.findByText("Base64 copied.")).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith("AAH/");
  });

  it.each([
    [true, false, "Empty body (0 bytes observed)."],
    [false, false, "No body bytes retained."],
    [true, true, "No body content retained after redaction."],
  ])(
    "distinguishes empty capture complete=%s redacted=%s",
    (complete, redacted, label) => {
      renderPanel(
        <DebugBodyView
          label="Request body"
          body={{
            ...requestBody,
            data: "",
            observed_bytes: 0,
            complete,
            redacted,
            content_type: null,
          }}
        />,
      );
      expect(screen.getByText(label)).toBeInTheDocument();
      expect(screen.getByText("Not observed")).toBeInTheDocument();
    },
  );

  it("downloads the full retained exchange as JSON, not an executable content-type", async () => {
    const create = vi.fn().mockReturnValue("blob:test-only");
    const revoke = vi.fn();
    vi.stubGlobal(
      "URL",
      class extends URL {
        static createObjectURL = create;
        static revokeObjectURL = revoke;
      },
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(this.download).toBe("request-debug-exchange-one.json");
        expect(this.href).toBe("blob:test-only");
      });
    renderPanel(<RequestDebugLog appId="app-one" />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Inspect exchange exchange-one",
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Download exchange JSON" }),
    );
    expect(click).toHaveBeenCalledOnce();
    const blob = create.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("application/json");
    const reader = new FileReader();
    const text = new Promise<string>((resolve) => {
      reader.onload = () => resolve(String(reader.result));
    });
    reader.readAsText(blob);
    expect(JSON.parse(await text)).toEqual(exchange);
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:test-only"));
  });

  it("drops payload query data after collapse instead of preloading other rows", async () => {
    const { client } = renderPanel(<RequestDebugLog appId="app-one" />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Inspect exchange exchange-one",
      }),
    );
    expect(
      await screen.findByLabelText("Response body content"),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Collapse exchange exchange-one" }),
    );
    await waitFor(() =>
      expect(
        client.getQueryData([
          "request-debug-exchange",
          "app-one",
          "exchange-one",
        ]),
      ).toBeUndefined(),
    );
    expect(requestDebugApi.detail).toHaveBeenCalledOnce();
  });

  it("uses independent cursor paging and resets it for a different selected window", async () => {
    vi.mocked(requestDebugApi.list).mockResolvedValue(
      testEnvelope({ items: [summary()], next_cursor: "older-cursor" }),
    );
    renderPanel(<RequestDebugLog appId="app-one" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Older requests" }),
    );
    await waitFor(() =>
      expect(requestDebugApi.list).toHaveBeenLastCalledWith(
        "app-one",
        expect.any(URLSearchParams),
        { operation_id: undefined, cursor: "older-cursor" },
        expect.any(AbortSignal),
      ),
    );
    fireEvent.click(screen.getByText("Change fixed window"));
    await waitFor(() =>
      expect(requestDebugApi.list).toHaveBeenLastCalledWith(
        "app-one",
        expect.any(URLSearchParams),
        { operation_id: undefined, cursor: undefined },
        expect.any(AbortSignal),
      ),
    );
    expect(vi.mocked(requestDebugApi.list).mock.lastCall![1].get("from")).toBe(
      "2026-09-08T00:00:00Z",
    );
  });

  it("keeps global requests collapsed and does not infer an App from missing identity", async () => {
    renderPanel(<GlobalRequestDebugLog />);
    expect(requestDebugApi.list).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Show all request logs" }),
    );
    expect(await screen.findByText("HTTP 422")).toBeInTheDocument();
    expect(requestDebugApi.list).toHaveBeenCalledWith(
      undefined,
      expect.any(URLSearchParams),
      { operation_id: undefined, cursor: undefined },
      expect.any(AbortSignal),
    );
    expect(
      screen.getByText(/All Apps and requests with no App attribution/),
    ).toBeInTheDocument();
  });

  it("isolates detail errors instead of describing missing payloads as an empty success", async () => {
    vi.mocked(requestDebugApi.detail).mockRejectedValue(
      new AdminApiError("Capture store unavailable", 503, "request-error"),
    );
    renderPanel(<RequestDebugLog appId="app-one" />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Inspect exchange exchange-one",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Capture store unavailable",
    );
    expect(screen.getByText("HTTP 422")).toBeInTheDocument();
    expect(screen.queryByText(/Empty body/)).not.toBeInTheDocument();
  });
});
