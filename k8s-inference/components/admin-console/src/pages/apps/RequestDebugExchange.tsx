import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { requestDebugApi } from "../../api/requestDebugClient";
import type { DebugBody, DebugExchange } from "../../api/requestDebugTypes";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";

function Headers({
  label,
  items,
}: {
  label: string;
  items: [string, string][];
}) {
  return (
    <div>
      <h5>{label}</h5>
      {items.length ? (
        <div className="table-frame">
          <table className="resource-table request-debug-headers">
            <caption className="sr-only">{label}</caption>
            <thead>
              <tr>
                <th>Name</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {items.map(([name, value], index) => (
                <tr key={index}>
                  <th scope="row">{name}</th>
                  <td>
                    <code>{value}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="supporting-copy">No header entries retained.</p>
      )}
    </div>
  );
}

export function DebugBodyView({
  label,
  body,
}: {
  label: string;
  body: DebugBody;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  const isReference = body.capture_mode === "artifact_reference";
  const reference = body.artifact_reference;
  async function copy() {
    try {
      await navigator.clipboard.writeText(
        isReference ? JSON.stringify(reference, null, 2) : body.data,
      );
      setNotice(
        isReference
          ? "Artifact reference copied."
          : body.encoding === "base64"
            ? "Base64 copied."
            : "Body copied.",
      );
    } catch {
      setNotice(
        isReference
          ? "Copy unavailable. Select the reference metadata or download the exchange JSON."
          : "Copy unavailable. Select the text or download the exchange.",
      );
    }
  }
  return (
    <section className="request-debug-body" aria-label={label}>
      <div className="section-heading">
        <h5>{label}</h5>
        <button
          type="button"
          className="button"
          onClick={() => void copy()}
          disabled={isReference && !reference}
        >
          {isReference
            ? `Copy ${label.toLowerCase()} reference`
            : `Copy ${label.toLowerCase()}${body.encoding === "base64" ? " (base64)" : ""}`}
        </button>
      </div>
      <dl className="definition-grid">
        <div>
          <dt>Content type</dt>
          <dd>{body.content_type ?? "Not observed"}</dd>
        </div>
        <div>
          <dt>{isReference ? "Capture mode" : "Encoding"}</dt>
          <dd>{isReference ? "Artifact reference (metadata only)" : body.encoding}</dd>
        </div>
        <div>
          <dt>Observed bytes</dt>
          <dd>{body.observed_bytes.toLocaleString()}</dd>
        </div>
        <div>
          <dt>Capture</dt>
          <dd>
            {isReference
              ? body.complete ? "Stream complete" : "Stream incomplete"
              : body.complete ? "Complete" : "Partial / incomplete"}
          </dd>
        </div>
        <div>
          <dt>Redaction</dt>
          <dd>{body.redacted ? "Redacted" : "Not redacted"}</dd>
        </div>
      </dl>
      {!body.complete ? (
        <p className="inline-notice">
          {isReference
            ? "Artifact stream incomplete; reference metadata does not prove complete delivery."
            : "Only observed bytes are shown; this is not a complete body."}
        </p>
      ) : null}
      {body.redacted ? (
        <p className="supporting-copy">
          Sensitive content was redacted. Observed byte counts describe the
          exchange, not the displayed text.
        </p>
      ) : null}
      {!isReference && body.encoding === "base64" ? (
        <p className="supporting-copy">
          Binary body displayed as base64; no content is executed or opened.
        </p>
      ) : null}
      {isReference ? (
        <div aria-label={`${label} artifact reference`}>
          <p className="supporting-copy">
            Original artifact bytes are not embedded in this debug exchange.
            Use the authorized artifact download workflow to retrieve the file;
            copying or exporting this exchange returns reference metadata only.
          </p>
          {body.data ? <p className="supporting-copy">{body.data}</p> : null}
          {reference ? (
            <dl className="definition-grid">
              <div>
                <dt>Artifact ID</dt>
                <dd><code>{reference.artifact_id}</code></dd>
              </div>
              <div>
                <dt>Declared artifact bytes</dt>
                <dd>{reference.size_bytes.toLocaleString()}</dd>
              </div>
              <div>
                <dt>Delivered bytes</dt>
                <dd>{reference.delivered_bytes.toLocaleString()}</dd>
              </div>
              <div>
                <dt>Expected SHA-256</dt>
                <dd><code>{reference.sha256}</code></dd>
              </div>
              <div>
                <dt>Observed SHA-256</dt>
                <dd><code>{reference.observed_sha256}</code></dd>
              </div>
              <div>
                <dt>Delivery verification</dt>
                <dd>
                  {reference.verified && body.complete
                    ? "Verified size and SHA-256"
                    : "Not verified — incomplete or mismatched stream"}
                </dd>
              </div>
            </dl>
          ) : (
            <p className="inline-notice">
              Artifact reference metadata is unavailable. No inline body was retained.
            </p>
          )}
        </div>
      ) : body.data ? (
        <pre className="request-debug-payload" aria-label={`${label} content`}>
          {body.data}
        </pre>
      ) : (
        <p className="supporting-copy">
          {body.redacted
            ? "No body content retained after redaction."
            : body.complete && body.observed_bytes === 0
              ? "Empty body (0 bytes observed)."
              : "No body bytes retained."}
        </p>
      )}
      {notice ? <p role="status">{notice}</p> : null}
    </section>
  );
}

function download(exchange: DebugExchange) {
  // Export the retained representation, including explicit base64/redaction flags.
  // Never open HTML/XML bodies in a browser tab or label them as safe documents.
  const blob = new Blob([JSON.stringify(exchange, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `request-debug-${encodeURIComponent(exchange.id)}.json`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function RequestDebugExchange({
  appId,
  exchangeId,
}: {
  appId?: string;
  exchangeId: string;
}) {
  const { params } = useAdminTimeWindow();
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["request-debug-exchange", appId ?? "global", exchangeId],
    queryFn: ({ signal }) =>
      requestDebugApi.detail(appId, exchangeId, params, signal),
    // Bodies are loaded only for this expanded row, not retained after collapse.
    gcTime: 0,
  });
  return (
    <DataBoundary
      data={query.data}
      error={query.error}
      pending={query.isPending}
      loadingLabel="Loading captured request and response…"
    >
      {({ data }) => (
        <div className="request-debug-detail page-stack">
          <div className="section-heading">
            <h4>Captured exchange</h4>
            <button
              type="button"
              className="button"
              onClick={() => {
                try {
                  download(data);
                  setDownloadError(null);
                } catch {
                  setDownloadError(
                    "Download unavailable. Copy the retained body text instead.",
                  );
                }
              }}
            >
              Download exchange JSON
            </button>
          </div>
          {downloadError ? <p role="alert">{downloadError}</p> : null}
          {[data.request_body, data.response_body].some(
            (body) => body.capture_mode === "artifact_reference",
          ) ? (
            <p className="supporting-copy">
              Exchange JSON includes artifact-reference metadata, not the original
              artifact bytes.
            </p>
          ) : null}
          <dl className="definition-grid request-debug-identities">
            <div>
              <dt>Exchange ID</dt>
              <dd>{data.id}</dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>
                {data.source === "public" ? "Public API" : "Model upstream"}
              </dd>
            </div>
            <div>
              <dt>Request ID</dt>
              <dd>{data.request_id ?? "Not recorded"}</dd>
            </div>
            <div>
              <dt>Operation ID</dt>
              <dd>{data.operation_id ?? "No operation"}</dd>
            </div>
            <div>
              <dt>Operation attempt</dt>
              <dd>{data.operation_attempt ?? "Not recorded"}</dd>
            </div>
            <div>
              <dt>Upstream attempt</dt>
              <dd>{data.upstream_attempt ?? "Not recorded"}</dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd>
                <time dateTime={data.started_at}>{data.started_at}</time>
              </dd>
            </div>
            <div>
              <dt>Completed</dt>
              <dd>
                {data.completed_at ? (
                  <time dateTime={data.completed_at}>{data.completed_at}</time>
                ) : (
                  "Not observed"
                )}
              </dd>
            </div>
            <div>
              <dt>Tenant</dt>
              <dd>{data.tenant_id ?? "Not attributed"}</dd>
            </div>
            <div>
              <dt>User / principal</dt>
              <dd>{data.principal_id ?? "Not attributed"}</dd>
            </div>
            <div>
              <dt>API key ID</dt>
              <dd>{data.token_id ?? "Not attributed"}</dd>
            </div>
            <div>
              <dt>Model route</dt>
              <dd>{data.model_id ?? "Not attributed"}</dd>
            </div>
            <div>
              <dt>MCP tool</dt>
              <dd>{data.mcp_tool ?? "Not recorded"}</dd>
            </div>
            <div>
              <dt>Method / endpoint</dt>
              <dd>
                {data.method} {data.endpoint}
              </dd>
            </div>
            <div>
              <dt>HTTP status</dt>
              <dd>{data.http_status ?? "Not observed"}</dd>
            </div>
            <div>
              <dt>Error type</dt>
              <dd>{data.error_type ?? "No transport error recorded"}</dd>
            </div>
            <div>
              <dt>Semantic outcome</dt>
              <dd>{data.semantic_outcome ?? "Unavailable"}</dd>
            </div>
            <div>
              <dt>Admission stage</dt>
              <dd>{data.admission_stage ?? "Unavailable"}</dd>
            </div>
            <div>
              <dt>Protocol / tool error</dt>
              <dd>
                {data.semantic_error_type ?? "No semantic error recorded"}
                {data.jsonrpc_error_code !== null
                  ? ` · JSON-RPC ${data.jsonrpc_error_code}`
                  : ""}
              </dd>
            </div>
            <div>
              <dt>Disconnected</dt>
              <dd>{data.disconnected ? "Yes" : "No"}</dd>
            </div>
          </dl>
          {data.error_detail !== null ? (
            <div>
              <h5>Error detail</h5>
              <pre className="request-debug-payload" aria-label="Error detail">
                {data.error_detail}
              </pre>
            </div>
          ) : null}
          <div>
            <h5>Query string</h5>
            {data.query_string ? (
              <pre className="request-debug-payload" aria-label="Query string">
                {data.query_string}
              </pre>
            ) : (
              <p>No query string retained.</p>
            )}
          </div>
          <Headers label="Request headers" items={data.request_headers} />
          <DebugBodyView
            key={`${data.id}-request`}
            label="Request body"
            body={data.request_body}
          />
          <Headers label="Response headers" items={data.response_headers} />
          <DebugBodyView
            key={`${data.id}-response`}
            label="Response body"
            body={data.response_body}
          />
        </div>
      )}
    </DataBoundary>
  );
}
