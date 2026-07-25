import { useQuery, keepPreviousData } from "@tanstack/react-query"
import { api, type RunsQuery } from "@/lib/api"

export function useWellness(query: RunsQuery & { days?: number } = { days: 30 }, enabled = true) {
  return useQuery({
    queryKey: ["wellness", query],
    queryFn: () => api.wellness(query),
    placeholderData: keepPreviousData,
    enabled,
  })
}

export function useMetrics(query: RunsQuery & { days?: number } = { days: 180 }, enabled = true) {
  return useQuery({
    queryKey: ["metrics", query],
    queryFn: () => api.metrics(query),
    placeholderData: keepPreviousData,
    enabled,
  })
}
