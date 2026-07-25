import { useQuery, keepPreviousData } from "@tanstack/react-query"
import { api, type RunsQuery } from "@/lib/api"
import { mergeDuplicateRuns } from "@/lib/runs"

// Phase 0.5 windowed /api/runs (see main.py get_runs) — defaults to the last 90
// days server-side; pass `all: true` for true all-time totals. Result is merged
// client-side the same way regardless of range (see lib/runs.ts).
export function useRuns(query: RunsQuery = {}, enabled = true) {
  return useQuery({
    queryKey: ["runs", query],
    queryFn: () => api.runs(query),
    select: mergeDuplicateRuns,
    staleTime: 5 * 60_000,
    // Keep showing the previous range's data (with isFetching:true) while a
    // new range loads, instead of the query resetting to undefined and every
    // chart built on it unmounting — see ChartCanvas's `loading` prop, which
    // fades the old chart instead of it disappearing then popping back in.
    placeholderData: keepPreviousData,
    enabled,
  })
}

// Home's exact stat-strip numbers (see HomePage.tsx's useStatStrip) need true
// all-time totals ("Avg pace (all)", all-time run count) — a windowed default
// would silently understate them. Kept as its own hook (rather than a default
// argument) so every call site states its intent explicitly.
export function useAllRuns() {
  return useRuns({ all: true })
}
