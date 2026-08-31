import type {
  AutoscalingConfiguration,
  PlatformConfiguration,
  TerraformHandoff,
} from "../api/configurationTypes";

export const EDITABLE_AUTOSCALING_FIELDS = [
  "min_replicas",
  "max_replicas",
  "target_queue_depth",
  "polling_interval_seconds",
  "cooldown_seconds",
] as const satisfies readonly (keyof AutoscalingConfiguration)[];

export type EditableAutoscalingField = (typeof EDITABLE_AUTOSCALING_FIELDS)[number];

const forbiddenKeyParts = ["api_key", "apikey", "credential", "password", "secret", "token"];

function unsafeKey(value: unknown, path = "$"): string | null {
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const found = unsafeKey(value[index], `${path}[${index}]`);
      if (found) return found;
    }
    return null;
  }
  if (!value || typeof value !== "object") return null;
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    const normalized = key.toLowerCase().replaceAll("-", "_");
    if (forbiddenKeyParts.some((part) => normalized.includes(part))) return `${path}.${key}`;
    const found = unsafeKey(item, `${path}.${key}`);
    if (found) return found;
  }
  return null;
}

export function handoffSafetyProblem(handoff: TerraformHandoff): string | null {
  if (!/^[a-z0-9][a-z0-9._-]*\.tfvars\.json$/.test(handoff.tfvars_filename)) {
    return "Terraform handoff filename failed the browser safety policy.";
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(handoff.tfvars_json);
  } catch {
    return "Terraform handoff is not valid JSON.";
  }
  const forbidden = unsafeKey(parsed) ?? unsafeKey(handoff.variables);
  if (forbidden) return `Terraform handoff contains a forbidden secret-bearing key at ${forbidden}.`;
  if (!handoff.forbidden_browser_actions.includes("terraform.apply")) {
    return "Terraform handoff did not preserve the no-browser-apply boundary.";
  }
  return null;
}

export function localConfigurationProblem(configuration: PlatformConfiguration): string | null {
  for (const model of Object.values(configuration.models)) {
    const { min_replicas: min, max_replicas: max, target_queue_depth: depth, polling_interval_seconds: poll, cooldown_seconds: cooldown } = model.autoscaling;
    if (!Number.isInteger(min) || min < 0 || min > 10_000) return `${model.model_id}: minimum replicas must be between 0 and 10,000.`;
    if (!Number.isInteger(max) || max < 0 || max > 10_000) return `${model.model_id}: maximum replicas must be between 0 and 10,000.`;
    if (max < min) return `${model.model_id}: maximum replicas must be greater than or equal to minimum replicas.`;
    if (!Number.isInteger(depth) || depth < 1 || depth > 100_000) return `${model.model_id}: target queue depth must be between 1 and 100,000.`;
    if (!Number.isInteger(poll) || poll < 1 || poll > 60) return `${model.model_id}: polling interval must be between 1 and 60 seconds.`;
    if (!Number.isInteger(cooldown) || cooldown < 5 || cooldown > 86_400) return `${model.model_id}: cooldown must be between 5 and 86,400 seconds.`;
  }
  return null;
}

export function sameConfiguration(left: PlatformConfiguration, right: PlatformConfiguration): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}
