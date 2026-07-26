import { useState } from "react"
import { useNavigate } from "react-router-dom"
import type { Workout } from "@/lib/api"
import { useWorkouts, useRecoverySessions, useRecoveryTools, useWorkoutMutations } from "@/hooks/useWorkouts"
import { todayLocalDateString } from "@/lib/format"
import { WorkoutCard } from "@/components/workouts/WorkoutCard"
import { RecoverySessionCard } from "@/components/workouts/RecoverySessionCard"
import { WorkoutFormDialog } from "@/components/workouts/WorkoutFormDialog"
import { NewWorkoutDialog } from "@/components/workouts/NewWorkoutDialog"
import { WorkoutsCalendar } from "@/components/workouts/WorkoutsCalendar"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/ui/empty-state"
import { Dumbbell } from "lucide-react"

type Item =
  | ({ _kind: "workout" } & Workout)
  | ({ _kind: "recovery" } & import("@/lib/api").RecoverySession)

export function WorkoutsPage() {
  const navigate = useNavigate()
  const { data: workouts } = useWorkouts()
  const { data: recoverySessions } = useRecoverySessions()
  const { data: recoveryTools } = useRecoveryTools()
  const { createWorkout, updateWorkout, deleteWorkout, updateRecoveryStatus, deleteRecoverySession } =
    useWorkoutMutations()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [newWorkoutOpen, setNewWorkoutOpen] = useState(false)
  const [editingWorkout, setEditingWorkout] = useState<Workout | null>(null)
  // Calendar is the default view (confirmed with the user) — List stays available
  // via the toggle for anyone who prefers a flat chronological scan.
  const [view, setView] = useState<"calendar" | "list">("calendar")

  if (!workouts || !recoverySessions || !recoveryTools) {
    return <Skeleton className="h-64 w-full" />
  }

  const toolsById = Object.fromEntries(recoveryTools.map((t) => [t.id, t]))

  // Single date-ordered list, not two separate sections — a recovery session
  // scheduled between two workouts should show where it actually falls
  // chronologically (ports the unified-list decision from app.js).
  const items: Item[] = [
    ...workouts.map((w) => ({ ...w, _kind: "workout" as const })),
    ...recoverySessions.map((s) => ({ ...s, _kind: "recovery" as const })),
  ].sort((a, b) => a.scheduledDate.localeCompare(b.scheduledDate))

  const today = todayLocalDateString()
  const upcoming = items.filter((i) => i.status === "planned" && i.scheduledDate >= today)
  // Completed items older than today drop out of view — the underlying Run (for a
  // workout) still lives on in Activities; this just stops Past growing forever.
  const past = items.filter(
    (i) => !(i.status === "planned" && i.scheduledDate >= today) && !(i.status === "completed" && i.scheduledDate < today),
  )

  function renderItem(item: Item) {
    if (item._kind === "workout") {
      return (
        <WorkoutCard
          key={item.id}
          workout={item}
          onEdit={() => {
            setEditingWorkout(item)
            setDialogOpen(true)
          }}
          onDelete={() => deleteWorkout.mutate(item.id)}
          onCritique={
            item.linkedRunId
              ? () =>
                  navigate("/chat", {
                    state: {
                      prefillActivityId: item.linkedRunId,
                      prefillText: `Critique my ${item.activityType || "workout"} from ${item.scheduledDate}.`,
                    },
                  })
              : undefined
          }
        />
      )
    }
    return (
      <RecoverySessionCard
        key={item.id}
        session={item}
        tool={toolsById[item.toolId]}
        onComplete={() => updateRecoveryStatus.mutate({ id: item.id, status: "completed" })}
        onSkip={() => updateRecoveryStatus.mutate({ id: item.id, status: "skipped" })}
        onDelete={() => deleteRecoverySession.mutate(item.id)}
      />
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-3">
        <Button variant="outline" onClick={() => setNewWorkoutOpen(true)}>
          + New Workout
        </Button>
        <div className="flex rounded-md border p-0.5">
          <Button
            variant={view === "calendar" ? "secondary" : "ghost"}
            size="sm"
            className="h-7"
            onClick={() => setView("calendar")}
          >
            Calendar
          </Button>
          <Button
            variant={view === "list" ? "secondary" : "ghost"}
            size="sm"
            className="h-7"
            onClick={() => setView("list")}
          >
            List
          </Button>
        </div>
      </div>

      {view === "calendar" ? (
        <WorkoutsCalendar items={items} renderItem={renderItem} />
      ) : (
        <>
          <div>
            <h2 className="mb-3 text-sm font-semibold">Upcoming</h2>
            {upcoming.length ? (
              <div className="flex flex-col gap-3">{upcoming.map(renderItem)}</div>
            ) : (
              <EmptyState icon={Dumbbell} title="Nothing scheduled" message="Ask the coach in Chat, or add one here." />
            )}
          </div>

          {past.length > 0 && (
            <div>
              <h2 className="mb-3 text-sm font-semibold">Past</h2>
              <div className="flex flex-col gap-3">{past.map(renderItem)}</div>
            </div>
          )}
        </>
      )}

      <NewWorkoutDialog
        open={newWorkoutOpen}
        onOpenChange={setNewWorkoutOpen}
        onCustom={() => {
          setEditingWorkout(null)
          setDialogOpen(true)
        }}
      />

      <WorkoutFormDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        workout={editingWorkout}
        onSave={(body) => {
          if (editingWorkout) updateWorkout.mutate({ id: editingWorkout.id, body })
          else createWorkout.mutate(body)
        }}
      />
    </div>
  )
}
