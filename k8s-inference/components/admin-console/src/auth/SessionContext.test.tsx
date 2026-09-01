import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../api/client";
import { testEnvelope, testSession } from "../test/accessFixtures";
import { LoginPage, SessionBoundary, useSession } from "./SessionContext";

describe("operator login", () => {
  it("clears the bootstrap credential immediately after submit and never stores it", async () => {
    const transient = "bootstrap-test-" + "z".repeat(40);
    const login = vi.fn().mockResolvedValue(undefined);
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    render(<LoginPage busy={false} error={null} onLogin={login} />);
    const input = screen.getByLabelText("Bootstrap access token");
    fireEvent.change(input, { target: { value: transient } });
    fireEvent.submit(input.closest("form")!);
    expect(input).toHaveValue("");
    await waitFor(() => expect(login).toHaveBeenCalledWith(transient, undefined));
    expect(storageSpy).not.toHaveBeenCalled();
    expect(window.location.href).not.toContain(transient);
  });

  it("explains that an inference key cannot satisfy admin authentication", () => {
    render(<LoginPage busy={false} error={new AdminApiError("admin authentication is required", 401, null, "authentication_error")} onLogin={async () => undefined} />);
    expect(screen.getByText("admin authentication is required")).toBeInTheDocument();
    expect(screen.getByText(/Use the admin bootstrap token configured for this cluster/)).toBeInTheDocument();
    expect(screen.getByText(/Inference API keys and MCP tokens cannot create an operator session/)).toBeInTheDocument();
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
    const token = "boundary-test-" + "b".repeat(40);
    fireEvent.change(await screen.findByLabelText("Bootstrap access token"), { target: { value: token } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Signed in as Admin operator")).toBeInTheDocument();
    expect(create).toHaveBeenCalledWith(token, undefined);
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
