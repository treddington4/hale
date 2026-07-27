import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api } from "@/lib/api"

export function usePlans() {
  return useQuery({ queryKey: ["plans"], queryFn: api.plans })
}

export function usePlanWeeks(planId: string, opts?: { weeksBack?: number; weeksForward?: number }) {
  return useQuery({
    queryKey: ["planWeeks", planId, opts?.weeksBack, opts?.weeksForward],
    queryFn: () => api.planWeeks(planId, opts),
  })
}

export function useStartPlan() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (goalId: string) => api.startPlan(goalId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["plans"] }),
  })
}
