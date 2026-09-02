import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi } from "../../api/client";
import { browserFixture } from "../../test/browserFixtures";
import { AcademicAssetsPage } from "./AcademicAssetsPage";

afterEach(() => vi.restoreAllMocks());

function renderPage() {
  vi.spyOn(adminApi, "academicAssets").mockResolvedValue(structuredClone(browserFixture("/admin/api/v1/academic-assets")) as never);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/admin/academic-assets?project=p1"]}>
        <Routes><Route path="/admin/academic-assets" element={<main><AcademicAssetsPage /></main>} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("academic asset readiness", () => {
  it("renders both licensed models with the canonical tenant-private delivery", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Licensed academic asset readiness" })).toBeInTheDocument();
    expect(screen.getByText("tenant-academic")).toBeInTheDocument();
    expect(screen.getByText("fs2-academic-poc")).toBeInTheDocument();
    expect(screen.getByText("academic-assets-runtime-rwx")).toBeInTheDocument();
    expect(screen.getByText("No institution bound")).toBeInTheDocument();

    const alphaFold = screen.getByRole("row", { name: /AlphaFold3 \(native\)/ });
    const bindCraft = screen.getByRole("row", { name: /BindCraft \(native PyRosetta\)/ });
    expect(within(alphaFold).getByText("/opt/fs2/academic/alphafold3")).toBeInTheDocument();
    expect(within(bindCraft).getByText("/opt/fs2/academic/pyrosetta-bindcraft")).toBeInTheDocument();
    expect(within(alphaFold).getAllByText("Not embedded in image")).toHaveLength(1);
    expect(within(alphaFold).getByText("74d0258616917c…33ff")).toBeInTheDocument();
  });

  it("states that formal institutional licence acceptance is separate and pending", async () => {
    renderPage();

    expect(await screen.findByText("Formal acceptance pending: institutional licence acceptance is a separate step.")).toBeInTheDocument();
    expect(screen.getByText(/No named representative has yet bound a named institution/)).toBeInTheDocument();
    expect(screen.getAllByText("FormalAcceptancePending")).toHaveLength(3);
    expect(screen.queryByText("FormalAcceptanceRecorded")).not.toBeInTheDocument();
  });

  it("never presents operational readiness as formal licence acceptance", async () => {
    renderPage();

    await screen.findByRole("heading", { name: "Licensed academic asset readiness" });
    const bindCraft = screen.getByRole("row", { name: /BindCraft \(native PyRosetta\)/ });

    expect(within(bindCraft).getByText("RuntimeReady")).toBeInTheDocument();
    expect(within(bindCraft).getByText("TenantCacheReady")).toBeInTheDocument();
    expect(within(bindCraft).getByText("ArtifactVerified")).toBeInTheDocument();
    expect(within(bindCraft).getByText("Granted")).toBeInTheDocument();
    expect(within(bindCraft).getByText("Authorized")).toBeInTheDocument();
    expect(within(bindCraft).getByText("FormalAcceptancePending")).toBeInTheDocument();
    expect(within(bindCraft).getByText(/Formal acceptance pending: no named representative has bound an institution/)).toBeInTheDocument();
    expect(within(bindCraft).getByText("No acceptance receipt digest")).toBeInTheDocument();
    expect(within(bindCraft).getByText(/Independent alternative/)).toHaveTextContent("open-binder");
  });

  it("has no automated accessibility violations", async () => {
    const { container } = renderPage();
    await screen.findByRole("heading", { name: "Licensed academic asset readiness" });
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
