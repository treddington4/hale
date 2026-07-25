import { useQuery, keepPreviousData } from "@tanstack/react-query"
import { api, type RunsQuery } from "@/lib/api"

export function useSteps(query: RunsQuery & { days?: number } = { days: 30 }, enabled = true) {
  return useQuery({
    queryKey: ["steps", query],
    queryFn: () => api.steps(query),
    placeholderData: keepPreviousData,
    enabled,
  })
}
