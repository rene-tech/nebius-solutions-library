export const SHARED_CONTEXT_KEYS = ["project", "cluster", "region", "from", "to", "timezone"] as const;
const sharedContextKeySet = new Set<string>(SHARED_CONTEXT_KEYS);
const sharedContextMaximum = {
  project: 128,
  cluster: 128,
  region: 64,
  from: 256,
  to: 256,
  timezone: 64,
} as const satisfies Record<(typeof SHARED_CONTEXT_KEYS)[number], number>;

export function sharedContextParams(input: URLSearchParams): URLSearchParams {
  const output = new URLSearchParams();
  input.forEach((value, key) => {
    if (!sharedContextKeySet.has(key)) return;
    const maximum = sharedContextMaximum[key as keyof typeof sharedContextMaximum];
    if (value.length <= maximum) output.set(key, value);
  });
  return output;
}
