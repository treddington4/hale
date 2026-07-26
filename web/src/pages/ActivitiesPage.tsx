import { useEffect, useMemo, useState } from "react"
import { useSearchParams, useNavigate } from "react-router-dom"
import { useActivities } from "@/hooks/useActivities"
import { useHrFloor } from "@/hooks/useHrFloor"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { api, type Run, type RunUpdate } from "@/lib/api"
import { currentFilterRange, toDateInputValue, todayMidnight, addDays, type FilterMode } from "@/lib/dates"
import { FilterBar, type FilterState } from "@/components/activities/FilterBar"
import { ActivityCard } from "@/components/activities/ActivityCard"
import { EditRunDialog } from "@/components/activities/EditRunDialog"
import { EmptyState } from "@/components/ui/empty-state"
import { Skeleton } from "@/components/ui/skeleton"
import { Activity } from "lucide-react"

function useUpdateRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: RunUpdate }) => api.updateRun(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  })
}

export function ActivitiesPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()

  const [filter, setFilter] = useState<FilterState>(() => {
    const initialMode = (searchParams.get("filter") as FilterMode | null) ?? "rolling7"
    const today = todayMidnight()
    return {
      mode: initialMode,
      anchor: today,
      customStart: addDays(today, -29),
      customEnd: today,
      activityType: "all",
    }
  })
  const [expandedId, setExpandedId] = useState<string | null>(searchParams.get("run"))
  const [editingRun, setEditingRun] = useState<Run | null>(null)

  const { start, end } = currentFilterRange(filter.mode, filter.anchor, filter.customStart, filter.customEnd)
  const runsQuery = useActivities(
    filter.mode === "all" ? { all: true } : { start: toDateInputValue(start), end: toDateInputValue(end) },
  )
  const hrFloor = useHrFloor()
  const updateRun = useUpdateRun()

  // Scroll the linked run into view once its card is actually in the DOM (mirrors
  // navigateTo()'s requestAnimationFrame scroll in the legacy app).
  useEffect(() => {
    if (!expandedId || !runsQuery.data) return
    const el = document.getElementById(`run-card-${expandedId}`)
    if (el) requestAnimationFrame(() => el.scrollIntoView({ behavior: "smooth", block: "start" }))
  }, [expandedId, runsQuery.data])

  const activityTypeCounts = useMemo(() => {
    if (!runsQuery.data) return []
    const counts: Record<string, number> = {}
    runsQuery.data.forEach((r) => {
      const t = r.activityType || "Run"
      counts[t] = (counts[t] || 0) + 1
    })
    return Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([type, count]) => ({ type, count }))
  }, [runsQuery.data])

  const filteredRuns = useMemo(() => {
    if (!runsQuery.data) return []
    if (filter.activityType === "all") return runsQuery.data
    return runsQuery.data.filter((r) => (r.activityType || "Run") === filter.activityType)
  }, [runsQuery.data, filter.activityType])

  return (
    <div className="flex flex-col gap-4">
      <FilterBar state={filter} onChange={setFilter} activityTypeCounts={activityTypeCounts} />

      {!runsQuery.data ? (
        <Skeleton className="h-64 w-full" />
      ) : filteredRuns.length === 0 ? (
        <EmptyState icon={Activity} title="No activities in this range" />
      ) : (
        <div className="flex flex-col gap-3">
          {filteredRuns.map((run) => (
            <ActivityCard
              key={run.id}
              run={run}
              hrFloor={hrFloor}
              isOpen={expandedId === run.id}
              onToggle={() => setExpandedId(expandedId === run.id ? null : run.id)}
              onEdit={() => setEditingRun(run)}
              onAskCoach={() =>
                navigate("/chat", {
                  state: {
                    prefillActivityId: run.id,
                    prefillText: `Tell me about my ${run.name || run.activityType || "activity"} on ${run.date}.`,
                  },
                })
              }
            />
          ))}
        </div>
      )}

      <EditRunDialog
        open={editingRun != null}
        onOpenChange={(open) => !open && setEditingRun(null)}
        run={editingRun}
        onSave={(body) => editingRun && updateRun.mutate({ id: editingRun.id, body })}
      />
    </div>
  )
}
