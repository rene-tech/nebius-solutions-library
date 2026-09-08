import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { modelDeploymentMutationCapabilitiesFixture } from "../../test/modelDeploymentFixtures";
import { ModelDeploymentForm } from "./ModelDeploymentForm";

describe("CPU-only managed App form", () => {
  it("shows CPU/RAM resources and preserves an explicit zero when enabled", () => {
    const option = structuredClone(modelDeploymentMutationCapabilitiesFixture.configuration_options[0]!);
    option.model_ref = option.default_spec.modelRef = "phenoage";
    option.scale_to_zero_qualified = false;
    option.scale_to_zero_warning = "Scale-to-zero is not yet benchmark-qualified.";
    option.default_spec.placement = {
      poolRefs: ["batch-cpu"], acceleratorsPerReplica: 0, topologyPolicy: "Any",
      cpuResources: { cpuMillis: 1000, memoryBytes: 268435456 },
    };
    option.default_spec.availability.minReplicas = 0;
    option.default_spec.lifecycle.desiredState = "Disabled";
    option.pool_choices = [{
      ...option.pool_choices[0]!, pool_ref: "batch-cpu", accelerator_class: "CPU",
      accelerators_per_node: 0, maximum_replicas: 14,
    }];
    function Harness() {
      const [spec, setSpec] = useState(option.default_spec);
      return <ModelDeploymentForm name="phenoage" namespace="fs2-models" spec={spec}
        configurationOption={option} disabled={false} identityLocked={false}
        onChange={setSpec} onNameChange={() => undefined} onNamespaceChange={() => undefined} />;
    }
    render(<Harness />);
    expect(screen.getByText(/1 CPU cores and 256 MiB requested per worker/)).toBeInTheDocument();
    expect(screen.getByText(/CPU\/RAM capacity/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Accelerators per replica")).not.toBeInTheDocument();
    expect(screen.getByText(option.scale_to_zero_warning)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Desired state"), { target: { value: "Enabled" } });
    expect(screen.getByLabelText("Hot floor")).toHaveValue(0);
    expect(screen.getByLabelText("Hot floor")).toHaveAttribute("min", "0");
  });
});
