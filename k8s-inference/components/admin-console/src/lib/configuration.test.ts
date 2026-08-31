import { describe, expect, it } from "vitest";
import { configuration, terraformHandoff } from "../test/configurationFixtures";
import { handoffSafetyProblem, localConfigurationProblem } from "./configuration";

describe("configuration fail-closed helpers", () => {
  it("rejects nested secret-bearing keys before a handoff enters UI state", () => {
    const unsafe = structuredClone(terraformHandoff);
    unsafe.variables = { approved: { api_key: "must-not-render" } };
    unsafe.tfvars_json = JSON.stringify(unsafe.variables);
    expect(handoffSafetyProblem(unsafe)).toContain("forbidden secret-bearing key");
    expect(handoffSafetyProblem(terraformHandoff)).toBeNull();
  });

  it("rejects unsafe filenames and a missing no-browser-apply marker", () => {
    const unsafeFilename = { ...terraformHandoff, tfvars_filename: "../configuration.tfvars.json" };
    expect(handoffSafetyProblem(unsafeFilename)).toContain("filename failed");
    const missingMarker = { ...terraformHandoff, forbidden_browser_actions: ["cloud.mutate", "kubernetes.patch"] };
    expect(handoffSafetyProblem(missingMarker)).toContain("no-browser-apply boundary");
  });

  it("rejects invalid autoscaling bounds locally while leaving zero minimum valid", () => {
    const valid = structuredClone(configuration);
    expect(localConfigurationProblem(valid)).toBeNull();
    valid.models["qwen3-8b"].autoscaling.max_replicas = -1;
    expect(localConfigurationProblem(valid)).toContain("maximum replicas");
  });
});
