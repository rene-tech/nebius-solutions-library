export const SHARED_CONTEXT_KEYS = ["project", "cluster", "region", "from", "to", "timezone"] as const;
const sharedContextKeySet = new Set<string>(SHARED_CONTEXT_KEYS);

export function sharedContextParams(input: URLSearchParams): URLSearchParams {
  const output = new URLSearchParams();
  input.forEach((value, key) => {
    if (sharedContextKeySet.has(key) && value.length <= 256) output.set(key, value);
  });
  return output;
}
