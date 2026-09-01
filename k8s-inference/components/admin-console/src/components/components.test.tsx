import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AdminEnvelope, AdminMeasurement, ModelState } from "../api/types";
import { AdminApiError } from "../api/client";
import { DataBoundary } from "./DataBoundary";
import { Measurement } from "./Measurement";
import { StatusChip } from "./StatusChip";

const meta: AdminEnvelope<unknown>["meta"] = {
  schema_version: "fs2.admin-api/v1",
  generated_at: "2026-08-30T08:00:00Z",
  context: {
    project: "fixture-project",
    cluster: "fixture-cluster",
    region: "fixture-region",
    from_at: "2026-08-30T07:00:00Z",
    to_at: "2026-08-30T08:00:00Z",
    timezone: "UTC",
  },
  sources: [],
  warnings: [],
};

describe("status and missing-data semantics", () => {
  it.each<ModelState>(["hot", "loading", "queued", "cold", "unhealthy", "unsupported", "unknown"])(
    "renders %s with text and reason rather than color alone",
    (state) => {
      render(<StatusChip state={state} reason={`${state} fixture reason`} />);
      expect(screen.getByLabelText(`${state}: ${state} fixture reason`)).toHaveTextContent(state);
    },
  );

  it("renders unavailable numbers as an em dash and preserves the reason", () => {
    const measurement: AdminMeasurement = {
      value: null,
      unit: "tokens/second",
      state: "unavailable",
      source: "prometheus",
      reason: "token counters are not instrumented",
    };
    render(<Measurement value={measurement} />);
    expect(screen.getByText("—")).toHaveAccessibleDescription("token counters are not instrumented");
    expect(screen.getByText(/token counters are not instrumented/)).toHaveClass("sr-only");
  });

  it("keeps unaffected content visible when one source is unavailable", () => {
    const envelope: AdminEnvelope<{ value: string }> = {
      meta: {
        ...meta,
        sources: [{ id: "prometheus", state: "unavailable", observed_at: null, age_seconds: null, reason: "timeout" }],
      },
      data: { value: "durable result" },
    };
    render(<DataBoundary data={envelope} error={null} pending={false}>{({ data }) => <p>{data.value}</p>}</DataBoundary>);
    expect(screen.getByText("Partial data")).toBeInTheDocument();
    expect(screen.getByText("durable result")).toBeInTheDocument();
  });

  it("provides the shared loading, error and empty states used by every data route", () => {
    const { rerender } = render(
      <DataBoundary<{ value: string }> data={undefined} error={null} pending>{() => null}</DataBoundary>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading current fleet data");

    rerender(
      <DataBoundary<{ value: string }>
        data={undefined}
        error={new AdminApiError("operator policy does not permit this request", 403, "request-role", "permission_denied")}
        pending={false}
      >{() => null}</DataBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Operator role insufficient");
    expect(screen.getByRole("alert")).toHaveTextContent("Request request-role");

    rerender(
      <DataBoundary data={{ meta, data: { value: "unused" } }} error={null} pending={false} empty>{() => null}</DataBoundary>,
    );
    expect(screen.getByText("No resources match the selected context.")).toBeInTheDocument();
  });
});
