import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../api/client";
import { testEnvelope, testSession } from "../test/accessFixtures";
import { LoginPage, SessionBoundary, useSession } from "./SessionContext";

describe("operator login", () => {
  it("clears the personal operator credential immediately after submit and never stores it", async () => {
    const transient = "fs2_operator_test_" + "z".repeat(40);
    const login = vi.fn().mockResolvedValue(undefined);
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    render(<LoginPage busy={false} error={null} onLogin={login} />);
    const input = screen.getByLabelText("Personal operator credential");
    fireEvent.change(input, { target: { value: transient } });
    fireEvent.submit(input.closest("form")!);
    expect(input).toHaveValue("");
    await waitFor(() => expect(login).toHaveBeenCalledWith(transient, undefined));
    expect(storageSpy).not.toHaveBeenCalled();
    expect(window.location.href).not.toContain(transient);
  });

  it("explains that shared bootstrap, inference, and MCP credentials cannot sign in", () => {
    render(<LoginPage busy={false} error={new AdminApiError("operator credential is invalid", 401, null, "operator_credential_invalid")} onLogin={async () => undefined} />);
    expect(screen.getByText("operator credential is invalid")).toBeInTheDocument();
    expect(screen.getByText(/Use the personal operator credential issued for your principal/)).toBeInTheDocument();
    expect(screen.getByText(/Bootstrap, inference, and MCP credentials cannot create an operator session/)).toBeInTheDocument();
  });

  it("moves from unauthenticated to cookie-backed session and back through logout", async () => {
    vi.spyOn(adminApi, "session").mockRejectedValue(new AdminApiError("operator session required", 401, null));
    const create = vi.spyOn(adminApi, "createSession").mockResolvedValue(testEnvelope(testSession));
    const remove = vi.spyOn(adminApi, "deleteSession").mockResolvedValue(undefined);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    function Protected() {
      const { session, logout } = useSession();
      return <div><p>Signed in as {session.principal.display_name}</p><button onClick={() => void logout()} type="button">Sign out now</button></div>;
    }

    render(<QueryClientProvider client={queryClient}><SessionBoundary><Protected /></SessionBoundary></QueryClientProvider>);
    const credential = "fs2_operator_boundary_" + "b".repeat(40);
    fireEvent.change(await screen.findByLabelText("Personal operator credential"), { target: { value: credential } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Signed in as Admin operator")).toBeInTheDocument();
    expect(create).toHaveBeenCalledWith(credential);
    fireEvent.click(screen.getByRole("button", { name: "Sign out now" }));
    expect(await screen.findByRole("heading", { name: "Operator sign in" })).toBeInTheDocument();
    expect(remove).toHaveBeenCalledOnce();
  });

  it("returns to sign in with an explicit notice when a data request reports session expiry", async () => {
    vi.spyOn(adminApi, "session").mockResolvedValue(testEnvelope(testSession));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><SessionBoundary><p>Protected console</p></SessionBoundary></QueryClientProvider>);
    expect(await screen.findByText("Protected console")).toBeInTheDocument();

    window.dispatchEvent(new Event("fs2:operator-session-expired"));

    expect(await screen.findByRole("heading", { name: "Operator sign in" })).toBeInTheDocument();
    expect(screen.getByText("Your operator session expired. Sign in again to continue.")).toBeInTheDocument();
  });
});

afterEach(() => vi.restoreAllMocks());
