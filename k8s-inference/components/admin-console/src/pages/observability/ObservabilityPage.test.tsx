import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminModelList, AdminOperationList } from "../../api/types";
import { adminApi } from "../../api/client";
import { observabilityFixture } from "../../test/capacityObservabilityFixtures";
import { testEnvelope } from "../../test/accessFixtures";
import { ObservabilityPage } from "./ObservabilityPage";

const operationId = "10f61fc4-4211-4bb8-a058-b11a8c078520";
const modelOptions = {
  items: [{ identity: { id: "qwen3-8b", display_name: "Qwen3 8B" } }],
  total: 1,
} as AdminModelList;
const operationOptions = { items: [{ id: operationId, model_id: "qwen3-8b" }], next_cursor: null } as AdminOperationList;

afterEach(() => vi.restoreAllMocks());

function prepare() {
  vi.spyOn(adminApi, "models").mockResolvedValue(testEnvelope(modelOptions));
  vi.spyOn(adminApi, "operations").mockResolvedValue(testEnvelope(operationOptions));
}

function renderPage(entry = "/admin/observability") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[entry]}><ObservabilityPage /></MemoryRouter></QueryClientProvider>);
}

describe("Observability page", () => {
  it("separates installed, health, data and launch state while keeping raw stores private", async () => {
    prepare();
    vi.spyOn(adminApi, "observability").mockResolvedValue(testEnvelope(observabilityFixture));
    renderPage();

    expect(await screen.findByRole("link", { name: /Open Grafana/ })).toHaveAttribute("href", "https://console.example.test/admin/observability/grafana/");
    expect(screen.getByRole("link", { name: /Open Prometheus/ })).toHaveAttribute("href", "https://console.example.test/admin/observability/grafana/explore");
    expect(screen.getByRole("link", { name: /Open Loki/ })).toHaveAttribute("href", expect.stringContaining("/grafana/explore"));
    expect(screen.getByRole("link", { name: /Open OpenTelemetry/ })).toHaveAttribute("href", "https://console.example.test/admin/observability/grafana/dashboards");
    expect(screen.getAllByText("Absent").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Unknown").length).toBeGreaterThan(0);
    expect(screen.getAllByText("0 items/second")).toHaveLength(2);
    expect(screen.getAllByText("—")).toHaveLength(2);
  });

  it("suppresses malicious or raw launch destinations even when enabled is asserted", async () => {
    prepare();
    const adversarial = structuredClone(observabilityFixture);
    const prometheus = adversarial.components.find((item) => item.id === "prometheus")!;
    prometheus.launch = { enabled: true, url: "https://console.example.test/admin/observability/grafana/explore?api_key=leak", reason: null };
    const loki = adversarial.components.find((item) => item.id === "loki")!;
    loki.launch = { enabled: true, url: "https://loki.example.test/ready", reason: null };
    const otel = adversarial.components.find((item) => item.id === "otel")!;
    otel.launch = { enabled: true, url: "https://otel.example.test/metrics", reason: null };
    vi.spyOn(adminApi, "observability").mockResolvedValue(testEnvelope(adversarial));
    renderPage();

    await screen.findByRole("link", { name: /Open Grafana/ });
    expect(screen.queryByRole("link", { name: /Open Prometheus/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open Loki/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open OpenTelemetry/ })).not.toBeInTheDocument();
    expect(screen.getAllByText(/Launch suppressed/)).toHaveLength(3);
  });

  it("sends only server-published model and operation identifiers as context", async () => {
    prepare();
    const observability = vi.spyOn(adminApi, "observability").mockResolvedValue(testEnvelope(observabilityFixture));
    renderPage(`/admin/observability?project=project-test&model_id=qwen3-8b&operation_id=${operationId}&token=must-not-flow&principal=also-private`);

    await screen.findByRole("link", { name: /Open Grafana/ });
    await waitFor(() => expect(observability).toHaveBeenCalled());
    const [context, selectors] = observability.mock.calls.at(-1)!;
    expect(context.toString()).toBe("project=project-test");
    expect(selectors).toEqual({ modelId: "qwen3-8b", operationId });
    expect(document.body.textContent).not.toContain("must-not-flow");
    expect(screen.getByRole("link", { name: /Open Grafana/ }).getAttribute("href")).not.toContain("must-not-flow");
  });
});
