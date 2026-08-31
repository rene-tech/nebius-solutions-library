import type { AdminMeasurement } from "../api/types";
import { formatMeasurement } from "../lib/format";

export function Measurement({ value, compact = false }: { value: AdminMeasurement; compact?: boolean }) {
  const unavailable = value.value === null;
  return (
    <span
      className={`measurement measurement--${value.state}${compact ? " measurement--compact" : ""}`}
      title={unavailable ? value.reason ?? "Unavailable" : `${value.source}${value.state === "estimated" ? "; estimated" : ""}`}
    >
      {formatMeasurement(value)}
      {value.state === "estimated" ? <span className="measurement__qualifier"> estimated</span> : null}
      {unavailable ? <span className="sr-only">. {value.reason ?? "Unavailable"}</span> : null}
    </span>
  );
}

export function MetricCard({ label, value, detail }: { label: string; value: AdminMeasurement; detail?: string }) {
  return (
    <article className="metric-card">
      <span className="metric-card__label">{label}</span>
      <strong className="metric-card__value"><Measurement value={value} /></strong>
      <span className="metric-card__detail">{detail ?? value.reason ?? value.source}</span>
    </article>
  );
}
