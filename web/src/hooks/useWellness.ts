import { useQuery } from "@tanstack/react-query"
import { api } from "@/lib/api"

export function useWellness(days = 30) {
  return useQuery({ queryKey: ["wellness", days], queryFn: () => api.wellness(days) })
}

export function useMetrics(days = 180) {
  return useQuery({ queryKey: ["metrics", days], queryFn: () => api.metrics(days) })
}
