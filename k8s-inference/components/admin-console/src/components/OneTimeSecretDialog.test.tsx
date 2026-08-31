import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { testKey } from "../test/accessFixtures";
import { OneTimeSecretDialog } from "./OneTimeSecretDialog";

afterEach(() => vi.restoreAllMocks());

describe("one-time API-key disclosure", () => {
  it("copies only on explicit request and clears the credential on dismiss", async () => {
    const transient = "test-only-" + "s".repeat(48);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    const consoleSpy = vi.spyOn(console, "log").mockImplementation(() => undefined);

    function Wrapper() {
      const [open, setOpen] = useState(true);
      return open ? <OneTimeSecretDialog disclosure={{ key: testKey, secret: transient }} onDismiss={() => setOpen(false)} /> : <p>Credential cleared</p>;
    }

    render(<Wrapper />);
    expect(screen.getByDisplayValue(transient)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy key" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(transient));
    fireEvent.click(screen.getByRole("button", { name: "I have stored it" }));
    expect(screen.queryByDisplayValue(transient)).not.toBeInTheDocument();
    expect(screen.getByText("Credential cleared")).toBeInTheDocument();
    expect(storageSpy).not.toHaveBeenCalled();
    expect(consoleSpy).not.toHaveBeenCalledWith(expect.stringContaining(transient));
    expect(window.location.href).not.toContain(transient);
  });

  it("dismisses and clears on Escape", () => {
    const transient = "test-only-" + "e".repeat(48);
    const dismiss = vi.fn();
    render(<OneTimeSecretDialog disclosure={{ key: testKey, secret: transient }} onDismiss={dismiss} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(dismiss).toHaveBeenCalledOnce();
  });

  it("keeps keyboard focus inside the disclosure dialog", () => {
    const transient = "test-only-" + "f".repeat(48);
    render(<OneTimeSecretDialog disclosure={{ key: testKey, secret: transient }} onDismiss={() => undefined} />);
    const close = screen.getByRole("button", { name: "Dismiss one-time API key" });
    const stored = screen.getByRole("button", { name: "I have stored it" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(stored).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(close).toHaveFocus();
  });
});
