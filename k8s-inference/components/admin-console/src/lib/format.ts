import type { AdminMeasurement } from "../api/types";

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

export function formatMeasurement(measurement: AdminMeasurement): string {
  if (measurement.value === null) return "—";
  const value = measurement.unit === "count" ? integer.format(measurement.value) : number.format(measurement.value);
  const units: Record<string, string> = {
    count: "",
    ratio: "%",
    seconds: "s",
    "requests/second": "/s",
    "tokens/second": " tok/s",
    bytes: " B",
    "gpu-seconds": " GPU-s",
  };
  const rendered = measurement.unit === "ratio" ? number.format(measurement.value * 100) : value;
  return `${rendered}${units[measurement.unit] ?? ` ${measurement.unit}`}`;
}

export function formatTimestamp(value: string | null): string {
  if (!value) return "Not observed";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Invalid timestamp" : date.toLocaleString();
}
