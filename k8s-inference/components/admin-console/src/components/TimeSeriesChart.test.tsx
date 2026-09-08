import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  chartSegments,
  chartTimeLabel,
  chartValue,
  TimeSeriesChart,
} from "./TimeSeriesChart";

describe("observed time series", () => {
  it("uses readable binary memory units without turning unknown values into zero", () => {
    expect(chartValue(70_237_814_784, "bytes")).toBe("65.41 GiB");
    expect(chartValue(1024, "bytes")).toBe("1 KiB");
    expect(chartValue(0, "bytes")).toBe("0 bytes");
    expect(chartValue(null, "bytes")).toBe("—");
    expect(chartValue(Number.NaN, "bytes")).toBe("—");
  });
  it("distinguishes multi-day endpoints and uses the declared UTC timezone", () => {
    const start = Date.parse("2026-09-01T13:04:00Z");
    const end = Date.parse("2026-09-08T13:04:00Z");
    expect(chartTimeLabel(start, start, end)).not.toBe(
      chartTimeLabel(end, start, end),
    );
    expect(chartTimeLabel(start, start, end)).toBe(
      new Intl.DateTimeFormat(undefined, {
        timeZone: "UTC",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }).format(start),
    );
    expect(chartTimeLabel(Number.NaN, start, end)).toBe("—");
  });
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
