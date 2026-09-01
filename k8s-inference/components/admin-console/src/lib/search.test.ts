import { describe, expect, it } from "vitest";
import { sharedContextParams } from "./search";

describe("shared context navigation", () => {
  it("keeps fleet context but drops page-specific and credential-like values", () => {
    const source = new URLSearchParams({
      project: "project-fixture",
      cluster: "cluster-fixture",
      region: "us-north1",
      timezone: "UTC",
      status: "loading",
      cursor: "opaque-page-cursor",
      token: "must-not-propagate",
    });
    expect(sharedContextParams(source).toString()).toBe(
      "project=project-fixture&cluster=cluster-fixture&region=us-north1&timezone=UTC",
    );
  });

  it("uses the backend's exact context bounds before values reach a request or link", () => {
    const accepted = new URLSearchParams({
      project: "p".repeat(128),
      cluster: "c".repeat(128),
      region: "r".repeat(64),
      timezone: "t".repeat(64),
    });
    expect(sharedContextParams(accepted).toString()).toBe(accepted.toString());

    expect(sharedContextParams(new URLSearchParams({
      project: "p".repeat(129),
      cluster: "c".repeat(129),
      region: "r".repeat(65),
      timezone: "t".repeat(65),
    })).toString()).toBe("");
  });
});
