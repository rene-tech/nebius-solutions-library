import type { ModelState } from "../api/types";

export function StatusChip({ state, reason }: { state: ModelState; reason: string }) {
  return (
    <span className={`status-chip status-chip--${state}`} title={reason} aria-label={`${state}: ${reason}`}>
      <span aria-hidden="true" className="status-chip__dot" />
      {state}
    </span>
  );
}
