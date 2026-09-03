import { useQuery } from "@tanstack/react-query";
import { adminApi } from "../../api/client";

export function useScientificCapabilities(context: URLSearchParams) {
  const contextKey = context.toString();
  return useQuery({
    queryKey: ["admin-scientific-capabilities", contextKey],
    queryFn: ({ signal }) => adminApi.scientificCapabilities(context, signal),
    retry: false,
    staleTime: 30_000,
  });
}
