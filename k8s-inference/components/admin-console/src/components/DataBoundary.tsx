import type { ReactNode } from "react";
import type { AdminEnvelope } from "../api/types";
import { AdminApiError } from "../api/client";

interface Props<T> {
  data: AdminEnvelope<T> | undefined;
  error: Error | null;
  pending: boolean;
  empty?: boolean;
  children: (value: AdminEnvelope<T>) => ReactNode;
}

export function DataBoundary<T>({ data, error, pending, empty, children }: Props<T>) {
  if (pending) {
    return <div className="state-panel state-panel--loading" role="status">Loading current fleet data…</div>;
  }
  if (error) {
    const forbidden = error instanceof AdminApiError && error.status === 403;
    return (
      <div className="state-panel state-panel--error" role="alert">
        <strong>{forbidden ? "Operator access required" : "This view is unavailable"}</strong>
        <span>{error.message}</span>
        {error instanceof AdminApiError && error.requestId ? <code>Request {error.requestId}</code> : null}
      </div>
    );
  }
  if (!data) {
    return <div className="state-panel" role="status">No response was published.</div>;
  }
  if (empty) {
    return <div className="state-panel">No resources match the selected context.</div>;
  }

  const impaired = data.meta.sources.filter((source) => source.state !== "available");
  return (
    <>
      {impaired.length || data.meta.warnings.length ? (
        <div className="freshness-notice" role="status">
          <strong>{impaired.some((source) => source.state === "unavailable") ? "Partial data" : "Stale data"}</strong>
          <span>
            {[...impaired.map((source) => `${source.id}: ${source.reason ?? source.state}`), ...data.meta.warnings.map((warning) => warning.message)].join(" · ")}
          </span>
        </div>
      ) : null}
      {children(data)}
    </>
  );
}
