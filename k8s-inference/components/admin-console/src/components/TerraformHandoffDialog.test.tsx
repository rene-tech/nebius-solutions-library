import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { terraformHandoff } from "../test/configurationFixtures";
import { TerraformHandoffDialog } from "./TerraformHandoffDialog";

afterEach(() => vi.restoreAllMocks());

describe("one-time Terraform handoff", () => {
  it("copies explicitly and clears tfvars without storage, URL or console disclosure", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    const consoleSpy = vi.spyOn(console, "log").mockImplementation(() => undefined);

    function Wrapper() {
      const [open, setOpen] = useState(true);
      return open ? <TerraformHandoffDialog handoff={terraformHandoff} onDismiss={() => setOpen(false)} /> : <p>Handoff cleared</p>;
    }

    render(<Wrapper />);
    const documentField = screen.getByRole("textbox", { name: "Deterministic tfvars JSON" });
    expect(documentField).toHaveValue(terraformHandoff.tfvars_json);
    fireEvent.click(screen.getByRole("button", { name: "Copy tfvars" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(terraformHandoff.tfvars_json));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("textbox", { name: "Deterministic tfvars JSON" })).not.toBeInTheDocument();
    expect(screen.getByText("Handoff cleared")).toBeInTheDocument();
    expect(storageSpy).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(window.location.href).not.toContain(terraformHandoff.tfvars_sha256);
    expect(consoleSpy).not.toHaveBeenCalled();
  });
});
