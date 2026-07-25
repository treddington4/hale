import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api, type GearInput } from "@/lib/api"

// Shared by Settings' gear management section and Home's wear widget — same
// pattern as useGoals, one query key so a write from Settings is immediately
// reflected on Home without a manual refetch.
export function useGear() {
  return useQuery({ queryKey: ["gear"], queryFn: api.gear })
}

export function useGearMutations() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ["gear"] })

  const createGear = useMutation({
    mutationFn: (body: GearInput) => api.createGear(body),
    onSuccess: invalidate,
  })
  const updateGear = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<GearInput> }) => api.updateGear(id, body),
    onSuccess: invalidate,
  })
  const deleteGear = useMutation({
    mutationFn: (id: string) => api.deleteGear(id),
    onSuccess: invalidate,
  })

  return { createGear, updateGear, deleteGear }
}
