import { useQuery } from "@tanstack/react-query"
import { api } from "@/lib/api"

export function useDailyCoachReport() {
  return useQuery({ queryKey: ["dailyCoachReport"], queryFn: api.dailyCoachReport })
}
