import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api/client";
import type { UserStorage } from "../../api/userTypes";
import { testEnvelope } from "../../test/accessFixtures";
import { UserStoragePanel } from "./UserStoragePanel";

const storage: UserStorage = {
  state: "ready", mode: "tenant", quota_bytes: 5_000_000_000,
  bucket_name: "customer-bucket", endpoint: "https://storage.example.test",
  region: "test", access_key_id: "public-key-id",
  expires_at: "2026-12-01T00:00:00Z",
};
afterEach(() => vi.restoreAllMocks());

describe("User storage", () => {
  it("shows metadata without fetching the secret, then reveals and hides it explicitly", async () => {
    const request = vi.spyOn(api, "envelopeRequest").mockResolvedValue(testEnvelope({
      ...storage, secret_access_key: "fixture-secret",
    }));
    render(<UserStoragePanel userId="alice" storage={storage} canReveal />);
    expect(screen.getByText("5 GB")).toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Show S3 credentials" }));
    expect((await screen.findByLabelText("S3 connection details") as HTMLTextAreaElement).value)
      .toContain("fixture-secret");
    expect(request).toHaveBeenCalledWith("/users/alice/storage/credentials", { method: "POST" });
    fireEvent.click(screen.getByRole("button", { name: "Hide credentials" }));
    expect(screen.queryByLabelText("S3 connection details")).not.toBeInTheDocument();
  });

  it("does not offer credential disclosure to viewers", () => {
    render(<UserStoragePanel userId="alice" storage={storage} canReveal={false} />);
    expect(screen.queryByRole("button", { name: "Show S3 credentials" })).not.toBeInTheDocument();
  });

  it("does not offer disclosure while provisioning or when excluded", () => {
    render(<UserStoragePanel userId="alice" storage={{ ...storage, state: "disabled" }} canReveal />);
    expect(screen.getByText("Storage is disabled for this tenant or user.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show S3 credentials" })).not.toBeInTheDocument();
  });

  it("shows a retrieval error without rendering secret fields", async () => {
    vi.spyOn(api, "envelopeRequest").mockRejectedValue(new Error("Storage is not ready"));
    render(<UserStoragePanel userId="alice" storage={storage} canReveal />);
    fireEvent.click(screen.getByRole("button", { name: "Show S3 credentials" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Storage is not ready");
    expect(screen.queryByLabelText("S3 connection details")).not.toBeInTheDocument();
  });
});
