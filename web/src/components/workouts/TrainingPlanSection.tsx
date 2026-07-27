import { useState } from "react"
import type { TrainingPlan, PlanWeek } from "@/lib/api"
import { useGoals } from "@/hooks/useGoals"
import { useWorkouts } from "@/hooks/useWorkouts"
import { usePlans, usePlanWeeks, useStartPlan } from "@/hooks/usePlans"
import { todayLocalDateString } from "@/lib/format"
import { PlanWeekDays } from "@/components/workouts/PlanWeekDays"
import { WeeklyPlanCard } from "@/components/workouts/WeeklyPlanCard"
import { StartPlanDialog } from "@/components/workouts/StartPlanDialog"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/ui/empty-state"
import { Target } from "lucide-react"

const PHASE_BLURBS: Record<PlanWeek["phase"], string> = {
  base: "Building aerobic volume",
  build: "Adding race-specific intensity",
  peak: "Sharpening — highest intensity, trimmed volume",
  taper: "Volume drops sharply so you arrive fresh",
}

function PlanGroup({ plan, defaultExpanded }: { plan: TrainingPlan; defaultExpanded: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [showAllWeeks, setShowAllWeeks] = useState(false)
  const { data } = usePlanWeeks(plan.id, { weeksBack: 4, weeksForward: 8 })
  const { data: workouts } = useWorkouts()
  const today = todayLocalDateString()

  const current = data?.weeks.find((w) => w.isCurrentWeek)
  const others = data?.weeks.filter((w) => !w.isCurrentWeek) ?? []

  const weekWorkouts = (workouts ?? []).filter(
    (w) => current && w.scheduledDate >= current.weekStart && w.scheduledDate <= addDays(current.weekStart, 6),
  )

  const daysUntilRace = plan.goalTargetDate
    ? Math.max(0, Math.round((new Date(plan.goalTargetDate + "T00:00:00").getTime() - new Date(today + "T00:00:00").getTime()) / 86_400_000))
    : null

  return (
    <div className="border-border rounded-lg border">
      <button
        className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2 text-sm font-medium">
          <span>{expanded ? "▼" : "▶"}</span>
          <span>{plan.goalName}</span>
        </div>
        {daysUntilRace != null && (
          <span className="text-muted-foreground text-xs">
            {daysUntilRace} days · {Math.floor(daysUntilRace / 7)} wks
          </span>
        )}
      </button>

      {expanded && (
        <div className="border-border flex flex-col gap-3 border-t p-3">
          {!data || !current ? (
            <div className="text-muted-foreground text-xs">Loading…</div>
          ) : (
            <>
              {/* Context first — a phase name and a bare number meant nothing on their
                  own, which is what made this panel unreadable. */}
              <div className="flex flex-col gap-0.5">
                <div className="text-sm font-medium">
                  This week ·{" "}
                  <span className="capitalize">{current.phase}</span>
                  {current.isDeload && " · Deload"}
                </div>
                <div className="text-muted-foreground text-xs">{PHASE_BLURBS[current.phase]}</div>
                <div className="text-xs">
                  <span className="text-muted-foreground">Run volume: </span>
                  <span className="tabular-nums">
                    {current.actualMi?.toFixed(1) ?? "0.0"} of {current.targetMi.toFixed(1)} mi
                  </span>
                  {current.frozen && <span className="text-hale-hot"> · frozen (readiness)</span>}
                </div>
              </div>

              <PlanWeekDays week={current} workouts={weekWorkouts} todayIso={today} />

              {/* Two planners run at once and they are not the same plan — say so once,
                  here, rather than leaving the per-day source tags to imply it. */}
              <div className="text-hale-faint text-[11px] leading-relaxed">
                Sessions tagged <span className="text-muted-foreground">Garmin</span> come from your
                Garmin adaptive plan; <span className="text-muted-foreground">HALE</span> ones are
                generated here. The volume figure above is HALE's own weekly budget for this phase —
                it doesn't drive Garmin's suggestions.
              </div>

              {others.length > 0 && (
                <div className="flex flex-col gap-2">
                  <button
                    className="text-hale-faint hover:text-foreground self-start text-[11px]"
                    onClick={() => setShowAllWeeks(!showAllWeeks)}
                  >
                    {showAllWeeks ? "▼ Hide" : "▶ Show"} other weeks ({others.length})
                  </button>
                  {showAllWeeks && others.map((w) => <WeeklyPlanCard key={w.weekStart} week={w} />)}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function addDays(iso: string, n: number): string {
  return new Date(new Date(iso + "T00:00:00").getTime() + n * 86_400_000).toISOString().slice(0, 10)
}

export function TrainingPlanSection() {
  const { data: goals } = useGoals()
  const { data: plans } = usePlans()
  const startPlan = useStartPlan()
  const [dialogOpen, setDialogOpen] = useState(false)

  if (!goals || !plans) return null

  const planGoalIds = new Set(plans.map((p) => p.goalId))
  const eligibleGoals = goals.filter((g) => g.goalType === "race" && g.status === "active" && !planGoalIds.has(g.id))

  if (plans.length === 0 && eligibleGoals.length === 0) {
    return (
      <EmptyState
        icon={Target}
        title="No active race goal yet"
        message="Add a race goal on the Goals tab, then start a training plan for it here."
      />
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Training Plan</h2>
        {eligibleGoals.length > 0 && (
          <Button variant="outline" size="sm" className="h-7" onClick={() => setDialogOpen(true)}>
            Start a Plan
          </Button>
        )}
      </div>

      {plans.map((p) => (
        <PlanGroup key={p.id} plan={p} defaultExpanded={plans.length === 1} />
      ))}

      <StartPlanDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        eligibleGoals={eligibleGoals}
        starting={startPlan.isPending}
        onStart={(goalId) => {
          startPlan.mutate(goalId, { onSuccess: () => setDialogOpen(false) })
        }}
      />
    </div>
  )
}
