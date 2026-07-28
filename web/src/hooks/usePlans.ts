import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api } from "@/lib/api"

export function usePlans() {
  return useQuery({ queryKey: ["plans"], queryFn: api.plans })
}

export function usePlanWeeks(planId: string, goalId: string, opts?: { weeksBack?: number; weeksForward?: number }) {
  return useQuery({
    queryKey: ["planWeeks", planId, goalId, opts?.weeksBack, opts?.weeksForward],
    queryFn: () => api.planWeeks(planId, goalId, opts),
  })
}

export function useAddPlanGoal() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ goalId, role }: { goalId: string; role?: "primary" | "supporting" }) =>
      api.addPlanGoal(goalId, role),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["plans"] }),
  })
}
