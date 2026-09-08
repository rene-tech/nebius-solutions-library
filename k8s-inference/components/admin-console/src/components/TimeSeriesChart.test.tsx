import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { chartSegments, chartValue, TimeSeriesChart } from "./TimeSeriesChart";

describe("observed time series", () => {
  it("preserves real zero and breaks missing intervals instead of drawing through them", () => {
    const points = [
      { at: "2026-09-08T00:00:00Z", value: 0 },
      { at: "2026-09-08T00:01:00Z", value: null },
      { at: "2026-09-08T00:02:00Z", value: 5 },
    ];
    const segments = chartSegments(
      { id: "cpu", label: "CPU", points },
      () => 1,
      (value) => value,
    );
    expect(segments).toEqual(["1,0", "1,5"]);
    expect(chartValue(null, "s")).toBe("—");
    expect(chartValue(0, "s")).toBe("0 s");
  });
  it("shows unavailable source reason, not a synthetic flat line", () => {
    render(
      <TimeSeriesChart
        title="GPU utilization"
        unit="%"
        state="unavailable"
        reason="No device observations"
        source="prometheus"
        aggregation="mean across devices"
        series={[]}
        summary={{ average: null, maximum: null }}
      />,
    );
    expect(screen.getByText("No device observations")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByText("0 %")).not.toBeInTheDocument();
  });
  it("renders an accessible real sample series with average/peak and data alternative", () => {
    render(
      <TimeSeriesChart
        title="Ready workers"
        unit="workers"
        state="available"
        reason={null}
        source="kubernetes"
        aggregation="sum of ready replicas"
        series={[
          {
            id: "ready",
            label: "Ready",
            points: [
              { at: "2026-09-08T00:00:00Z", value: 1 },
              { at: "2026-09-08T00:01:00Z", value: 2 },
            ],
          },
        ]}
        summary={{ average: 1.5, maximum: 2 }}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(
      /Ready workers, workers/,
    );
    expect(screen.getByText("1.5 workers")).toBeInTheDocument();
    expect(screen.getByText("View observed samples")).toBeInTheDocument();
  });
});
