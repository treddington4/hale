import { useQuery } from "@tanstack/react-query"
import { api, type RunsQuery } from "@/lib/api"

export function useSteps(query: RunsQuery & { days?: number } = { days: 30 }) {
  return useQuery({ queryKey: ["steps", query], queryFn: () => api.steps(query) })
}
