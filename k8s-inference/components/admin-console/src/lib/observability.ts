import type { AdminObservabilityComponent } from "../api/types";

const credentialLikeParameter = /^(?:access_?token|api_?key|authorization|bearer|credential|password|principal|tenant|user)$/i;

export function verifiedObservabilityLaunch(component: AdminObservabilityComponent): string | null {
  if (!component.launch.enabled || !component.launch.url) return null;
  try {
    const parsed = new URL(component.launch.url);
    if (
      parsed.protocol !== "https:" ||
      parsed.username !== "" ||
      parsed.password !== "" ||
      parsed.hash !== "" ||
      component.launch.url.length > 2048
    ) return null;
    if ([...parsed.searchParams.keys()].some((key) => credentialLikeParameter.test(key))) return null;
    if (component.id !== "grafana" && !/(?:^|\/)grafana(?:\/|$)/i.test(parsed.pathname)) return null;
    return component.launch.url;
  } catch {
    return null;
  }
}

export function isUuid(value: string | null): value is string {
  return Boolean(value && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value));
}
