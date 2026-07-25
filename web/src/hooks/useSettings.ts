import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api, type SyncSource, type CoachPersonality } from "@/lib/api"

export function useStravaStatus() {
  return useQuery({ queryKey: ["stravaStatus"], queryFn: api.stravaStatus })
}

export function useGarminStatus() {
  return useQuery({ queryKey: ["garminStatus"], queryFn: api.garminStatus })
}

export function useHevyStatus() {
  return useQuery({ queryKey: ["hevyStatus"], queryFn: api.hevyStatus })
}

export function useSyncMeta() {
  return useQuery({ queryKey: ["syncMeta"], queryFn: api.syncMeta })
}

export function useConnections() {
  return useQuery({ queryKey: ["connections"], queryFn: api.connections })
}

export function useRouteDiagnostics() {
  return useQuery({ queryKey: ["routeDiagnostics"], queryFn: api.routeDiagnostics })
}

export function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: api.config })
}

export function useProfile() {
  return useQuery({ queryKey: ["profile"], queryFn: api.profile })
}

export function useUpdateProfile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: Partial<import("@/lib/api").Profile>) => api.updateProfile(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["profile"] }),
  })
}

export function useTokens() {
  return useQuery({ queryKey: ["tokens"], queryFn: api.tokens })
}

// One-shot fetch on mount, auto-poll only while genuinely "running" — the React-
// idiomatic equivalent of legacy's checkBacklogOnce()/pollBacklogStatus() pair.
// TanStack Query's refetchInterval callback decides per-fetch whether to keep
// going, based on the data it just received — there's no manual setTimeout
// bookkeeping, and no way to reintroduce the unconditional-poll flashing bug
// that pair was written to fix (see PLAN.md / git history), since a query with
// no active observers simply doesn't refetch at all.
export function useSyncStatus(source: SyncSource) {
  return useQuery({
    queryKey: ["syncStatus", source],
    queryFn: () => api.syncStatus(source),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1000 : false),
  })
}

export function useBacklogStatus(source: SyncSource) {
  return useQuery({
    queryKey: ["backlogStatus", source],
    queryFn: () => api.backlogStatus(source),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1500 : false),
  })
}

export function useSettingsMutations() {
  const qc = useQueryClient()

  const manualSync = useMutation({
    mutationFn: (source: SyncSource) => api.manualSync(source),
    onSuccess: (_result, source) => qc.invalidateQueries({ queryKey: ["syncStatus", source] }),
  })
  const backlogSync = useMutation({
    mutationFn: (source: SyncSource) => api.backlogSync(source),
    onSuccess: (_result, source) => qc.invalidateQueries({ queryKey: ["backlogStatus", source] }),
  })
  const saveGarminConnection = useMutation({
    mutationFn: ({ username, password }: { username: string; password: string }) =>
      api.saveGarminConnection(username, password),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["garminStatus"] })
      qc.invalidateQueries({ queryKey: ["connections"] })
    },
  })
  const deleteConnection = useMutation({
    mutationFn: (provider: string) => api.deleteConnection(provider),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["garminStatus"] })
      qc.invalidateQueries({ queryKey: ["hevyStatus"] })
      qc.invalidateQueries({ queryKey: ["connections"] })
    },
  })
  const saveHevyConnection = useMutation({
    mutationFn: (apiKey: string) => api.saveHevyConnection(apiKey),
    onSuccess: (result) => {
      if (result.ok) {
        qc.invalidateQueries({ queryKey: ["hevyStatus"] })
        qc.invalidateQueries({ queryKey: ["connections"] })
      }
    },
  })
  const setCoachPersonality = useMutation({
    mutationFn: (personality: CoachPersonality) => api.setCoachPersonality(personality),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["coachPersonality"] }),
  })
  const garminImport = useMutation({
    mutationFn: (file: File) => api.garminImport(file),
    onSuccess: (result) => {
      if (result.ok && result.summary.activitiesImported > 0) {
        qc.invalidateQueries({ queryKey: ["runs"] })
      }
    },
  })
  const createToken = useMutation({
    mutationFn: (name: string) => api.createToken(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tokens"] }),
  })
  const deleteToken = useMutation({
    mutationFn: (id: string) => api.deleteToken(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tokens"] }),
  })

  return {
    manualSync, backlogSync, saveGarminConnection, saveHevyConnection, deleteConnection, setCoachPersonality, garminImport,
    createToken, deleteToken,
  }
}
