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

export function useUpdatePlan() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ planId, ...body }: Parameters<typeof api.updatePlan>[1] & { planId: string }) =>
      api.updatePlan(planId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["plans"] })
      // Availability and long-run day change what each week's sessions look like, so the
      // cached week series is stale the moment either is saved.
      qc.invalidateQueries({ queryKey: ["planWeeks"] })
    },
  })
}
