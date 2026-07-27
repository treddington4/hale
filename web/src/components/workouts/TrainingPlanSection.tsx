import { useState } from "react"
import type { TrainingPlan } from "@/lib/api"
import { useGoals } from "@/hooks/useGoals"
import { usePlans, usePlanWeeks, useStartPlan } from "@/hooks/usePlans"
import { WeeklyPlanCard } from "@/components/workouts/WeeklyPlanCard"
import { StartPlanDialog } from "@/components/workouts/StartPlanDialog"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/ui/empty-state"
import { Target } from "lucide-react"

function PlanGroup({ plan, defaultExpanded }: { plan: TrainingPlan; defaultExpanded: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const { data } = usePlanWeeks(plan.id, { weeksBack: 4, weeksForward: 8 })

  return (
    <div className="border-border rounded-lg border">
      <button
        className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2 text-sm font-medium">
          <span>{expanded ? "▼" : "▶"}</span>
          <span>{plan.goalName}</span>
          {plan.goalTargetDate && <span className="text-muted-foreground text-xs">{plan.goalTargetDate}</span>}
        </div>
        {!plan.isActivePeriodizationGoal && (
          <span className="text-muted-foreground text-[11px]">Not currently driving training</span>
        )}
      </button>

      {expanded && (
        <div className="border-border flex flex-col gap-2 border-t p-3">
          {/* Future weeks assume every prior week hits its own target exactly — see
              generator.project_week_series's docstring for why that's the only honest
              way to preview a week the ramp hasn't reached yet. */}
          <p className="text-muted-foreground text-[11px] italic">
            Future weeks assume every prior week hits its target — actuals will differ.
          </p>
          {!data ? (
            <div className="text-muted-foreground text-xs">Loading…</div>
          ) : (
            data.weeks.map((w) => <WeeklyPlanCard key={w.weekStart} week={w} />)
          )}
        </div>
      )}
    </div>
  )
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
        <h2 className="text-sm font-semibold">Training Plans</h2>
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
