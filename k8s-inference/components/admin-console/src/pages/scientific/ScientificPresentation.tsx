import type {
  ScientificAccessGate,
  ScientificAdmissionState,
  ScientificAttemptState,
  ScientificEvidenceMeasurement,
  ScientificFastStartObservation,
  ScientificRunState,
  ScientificStageState,
} from "../../api/scientificTypes";

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

export function formatScientificMeasurement(measurement: ScientificEvidenceMeasurement): string {
  if (measurement.value === null) return "—";
  const formatted = measurement.unit === "count" || measurement.unit === "bytes"
    ? integer.format(measurement.value)
    : number.format(measurement.value);
  const suffix = {
    seconds: "s",
    "gpu-seconds": " GPU-s",
    bytes: " B",
    count: "",
  }[measurement.unit];
  return `${formatted}${suffix}`;
}

export function ScientificMeasurement({ value, compact = false }: { value: ScientificEvidenceMeasurement; compact?: boolean }) {
  const detail = value.reason ?? `${value.evidence} from ${value.source}`;
  const formatted = formatScientificMeasurement(value);
  return (
    <span
      className={`scientific-measurement scientific-measurement--${value.evidence}${compact ? " scientific-measurement--compact" : ""}`}
      title={detail}
      aria-label={value.reason ? `${formatted}, ${value.evidence}. ${value.reason}` : undefined}
    >
      {formatted}
      <span className="scientific-measurement__evidence"> {value.evidence}</span>
    </span>
  );
}

export function ScientificMetricCard({ label, value, detail }: { label: string; value: ScientificEvidenceMeasurement; detail?: string }) {
  return (
    <article className="metric-card">
      <span className="metric-card__label">{label}</span>
      <strong className="metric-card__value"><ScientificMeasurement value={value} /></strong>
      <span className="metric-card__detail">{detail ?? value.reason ?? value.source}</span>
    </article>
  );
}

type ScientificStatus = ScientificRunState | ScientificStageState | ScientificAttemptState | ScientificAdmissionState | ScientificAccessGate["state"] | "qualified" | "candidate" | "blocked" | "unknown";

export function ScientificStatusChip({ state, label, reason }: { state: ScientificStatus; label?: string; reason?: string }) {
  return (
    <span className={`scientific-state scientific-state--${state}`} title={reason} aria-label={reason ? `${label ?? state}: ${reason}` : label ?? state}>
      <span className="scientific-state__dot" aria-hidden="true" />
      {label ?? state}
    </span>
  );
}

export function AccessGate({ access, compact = false }: { access: ScientificAccessGate; compact?: boolean }) {
  return (
    <div className={compact ? "scientific-access scientific-access--compact" : "scientific-access"}>
      <ScientificStatusChip state={access.state} label={`${access.profile} · ${access.state}`} reason={access.gate} />
      <span className="secondary-line scientific-secondary">{access.gate}</span>
      {access.receipt_digest ? <code className="scientific-digest" title={access.receipt_digest}>receipt {access.receipt_digest.slice(0, 15)}…</code> : null}
      {access.authorization ? (
        <span className="secondary-line scientific-secondary">
          Use {access.authorization.use_authorization_status} · execution {access.authorization.execution_authorization_status}
          {compact ? null : ` · ${access.authorization.asset_id}${access.authorization.asset_namespace ? ` in ${access.authorization.asset_namespace}` : ""}`}
        </span>
      ) : null}
      {access.profile === "academic" ? (
        <span className="secondary-line scientific-secondary">
          {access.request_time_license_receipt_required ? "Request-time licence receipt required" : "No request-time licence receipt"}
          {compact ? null : ` · ${access.formal_license_status} (advisory only)`}
        </span>
      ) : null}
      {access.alternative ? (
        <span className="scientific-alternative">
          Alternative: <strong>{access.alternative.display_name}</strong> — {access.alternative.reason}
        </span>
      ) : null}
    </div>
  );
}

export function FastStartTier({ observation }: { observation: ScientificFastStartObservation }) {
  return (
    <div className="scientific-fast-start">
      <span className={`scientific-tier scientific-tier--${observation.evidence}`}>{observation.tier}</span>
      <span className="secondary-line scientific-secondary">{observation.evidence}: {observation.reason}</span>
    </div>
  );
}

export function shortDigest(value: string | null): string {
  if (value === null) return "Unavailable";
  return value.length > 18 ? `${value.slice(0, 14)}…${value.slice(-4)}` : value;
}
