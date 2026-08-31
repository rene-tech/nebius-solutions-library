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

  it("drops overlong context values before they reach a request or link", () => {
    expect(sharedContextParams(new URLSearchParams({ project: "x".repeat(257) })).toString()).toBe("");
  });
});
