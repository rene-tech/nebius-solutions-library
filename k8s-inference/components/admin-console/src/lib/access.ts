import type { AccessMeasurement, OperatorRole } from "../api/accessTypes";

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

export function rolePermits(actual: OperatorRole, required: OperatorRole): boolean {
  const order: Record<OperatorRole, number> = { viewer: 0, operator: 1, admin: 2 };
  return order[actual] >= order[required];
}

export function formatAccessMeasurement(measurement: AccessMeasurement): string {
  if (measurement.value === null) return "—";
  const suffix: Record<string, string> = {
    tokens: " tokens",
    "gpu-seconds": " GPU-s",
    count: "",
  };
  return `${number.format(measurement.value)}${suffix[measurement.unit] ?? ` ${measurement.unit}`}`;
}

export function measurementDescription(measurement: AccessMeasurement): string | undefined {
  if (measurement.state === "available") return undefined;
  return measurement.reason ?? measurement.state;
}

export function splitCsv(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

export function optionalNumber(value: string): number | null {
  if (!value.trim()) return null;
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue) || numberValue <= 0) throw new Error("Numeric limits must be greater than zero");
  return numberValue;
}

export function optionalInteger(value: string): number | null {
  const parsed = optionalNumber(value);
  if (parsed !== null && !Number.isInteger(parsed)) throw new Error("This limit must be a whole number");
  return parsed;
}

export function optionalIso(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) throw new Error("Expiry must be a valid date and time");
  if (date.valueOf() <= Date.now()) throw new Error("Expiry must be in the future");
  return date.toISOString();
}

export function datetimeLocal(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  const local = new Date(date.valueOf() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}
