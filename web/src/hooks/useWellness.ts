import { useQuery } from "@tanstack/react-query"
import { api, type RunsQuery } from "@/lib/api"

export function useWellness(query: RunsQuery & { days?: number } = { days: 30 }) {
  return useQuery({ queryKey: ["wellness", query], queryFn: () => api.wellness(query) })
}

export function useMetrics(query: RunsQuery & { days?: number } = { days: 180 }) {
  return useQuery({ queryKey: ["metrics", query], queryFn: () => api.metrics(query) })
}
