import { useState } from "react"
import type { TrainingPlan, PlanGoalRef, PlanWeek } from "@/lib/api"
import { useGoals } from "@/hooks/useGoals"
import { useWorkouts } from "@/hooks/useWorkouts"
import { usePlans, usePlanWeeks, useAddPlanGoal } from "@/hooks/usePlans"
import { todayLocalDateString } from "@/lib/format"
import { PlanWeekDays } from "@/components/workouts/PlanWeekDays"
import { WeeklyPlanCard } from "@/components/workouts/WeeklyPlanCard"
import { StartPlanDialog } from "@/components/workouts/StartPlanDialog"
import { PlanScheduleSettings } from "@/components/workouts/PlanScheduleSettings"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/ui/empty-state"
import { Target } from "lucide-react"
import { cn } from "@/lib/utils"

const PHASE_BLURBS: Record<PlanWeek["phase"], string> = {
  base: "Building aerobic volume",
  build: "Adding race-specific intensity",
  peak: "Sharpening — highest intensity, trimmed volume",
  taper: "Volume drops sharply so you arrive fresh",
}

// P21 (docs/P21_CONCURRENT_GOALS.md): one plan, several goals possible, each in a role.
// One collapsible section per attached goal — same visual shape P20 shipped per-plan,
// now keyed by goal within the single plan.
function GoalGroup({ plan, goalRef, defaultExpanded }: { plan: TrainingPlan; goalRef: PlanGoalRef; defaultExpanded: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [showAllWeeks, setShowAllWeeks] = useState(false)
  const { data } = usePlanWeeks(plan.id, goalRef.goalId, { weeksBack: 4, weeksForward: 8 })
  const { data: workouts } = useWorkouts()
  const today = todayLocalDateString()

  const current = data?.weeks.find((w) => w.isCurrentWeek)
  const others = data?.weeks.filter((w) => !w.isCurrentWeek) ?? []

  const weekWorkouts = (workouts ?? []).filter(
    (w) => current && w.scheduledDate >= current.weekStart && w.scheduledDate <= addDays(current.weekStart, 6),
  )

  // Real prescribed distance where one exists, the HR-derived estimate otherwise — the
  // whole point being that an HR+duration plan has no distance of its own to total up.
  const runWorkouts = weekWorkouts.filter((w) => w.activityType === "Run")
  const scheduledMi = runWorkouts.reduce(
    (sum, w) => sum + (w.targetDistanceMi ?? w.estimatedDistance?.distanceMi ?? 0),
    0,
  )
  const hasEstimates = runWorkouts.some((w) => !w.targetDistanceMi && w.estimatedDistance)

  const daysUntilRace = goalRef.goalTargetDate
    ? Math.max(0, Math.round((new Date(goalRef.goalTargetDate + "T00:00:00").getTime() - new Date(today + "T00:00:00").getTime()) / 86_400_000))
    : null

  return (
    <div className="border-border rounded-lg border">
      <button
        className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2 text-sm font-medium">
          <span>{expanded ? "▼" : "▶"}</span>
          <span>{goalRef.goalName}</span>
          <span
            className={cn(
              "rounded border px-1.5 py-0.5 text-[10px] font-normal capitalize",
              goalRef.role === "primary"
                ? "border-hale-hot/40 text-hale-hot"
                : "border-border text-muted-foreground",
            )}
          >
            {goalRef.role}
          </span>
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
                {!goalRef.isActivePeriodizationGoal && (
                  // A supporting goal — or a primary goal no longer live (completed/
                  // expired) — isn't what the nightly generator is actually working
                  // toward this week. Say so, rather than implying a consistency that
                  // doesn't exist: this goal's own numbers are an honest replay of what
                  // IT alone would prescribe, not what's really scheduled.
                  <div className="text-hale-faint text-[11px]">
                    Not currently driving real generation — showing this goal's own projection.
                  </div>
                )}
                <div className="text-xs">
                  <span className="text-muted-foreground">This calendar week: </span>
                  <span className="tabular-nums">
                    {current.actualMi?.toFixed(1) ?? "0.0"} of {current.targetMi.toFixed(1)} mi
                  </span>
                  {current.frozen && <span className="text-hale-hot"> · frozen (readiness)</span>}
                </div>
                {/* The calendar figure resets to 0.0 every Monday, which is accurate and
                    useless — "0.0 of 25.0" while 24 miles were run in the past seven days
                    reads as being far behind. The rolling number is what answers "where
                    am I actually at"; the calendar one still drives the ramp. */}
                <div className="text-xs">
                  <span className="text-muted-foreground">Last 7 days: </span>
                  <span className="tabular-nums">{data.last7DaysMi.toFixed(1)} mi</span>
                </div>
                {/* The two planners can disagree, and by how much is the single most
                    useful thing this panel can tell you — Garmin prescribing well above
                    HALE's ramp ceiling is a real signal, not a display quirk. */}
                {scheduledMi > 0 && (
                  <div className="text-xs">
                    <span className="text-muted-foreground">Scheduled this week: </span>
                    <span className="tabular-nums">
                      {hasEstimates && "~"}
                      {scheduledMi.toFixed(1)} mi
                    </span>
                    {current.targetMi > 0 && (
                      <span
                        className={cn(
                          "ml-1",
                          scheduledMi > current.targetMi * 1.15 ? "text-hale-hot" : "text-muted-foreground",
                        )}
                      >
                        ({scheduledMi > current.targetMi ? "+" : ""}
                        {Math.round(((scheduledMi - current.targetMi) / current.targetMi) * 100)}% vs budget)
                      </span>
                    )}
                  </div>
                )}
              </div>

              <PlanWeekDays week={current} workouts={weekWorkouts} todayIso={today} />

              {/* Garmin revises the same day's suggestion repeatedly, so a stale copy is
                  actively misleading rather than merely old — it shows a duration the
                  Garmin app no longer agrees with. Real case: HALE displayed 76min while
                  the app said 55min, with nothing on screen to hint the number was
                  a day out of date. */}
              {plan.garminPlan.isStale && (
                <div className="text-hale-hot text-[11px] leading-relaxed">
                  Garmin sessions below may be out of date — last read{" "}
                  {plan.garminPlan.ageHours != null
                    ? `${Math.round(plan.garminPlan.ageHours)}h ago`
                    : "never"}
                  {plan.garminPlan.inCooldown
                    ? `. Garmin is rate-limiting HALE (${plan.garminPlan.consecutiveFailures} failed attempts), so it can't refresh yet — check the Garmin app for today's actual session.`
                    : ". Check the Garmin app for today's actual session."}
                </div>
              )}

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
  const { data: plan, isPending: planPending } = usePlans()
  const addPlanGoal = useAddPlanGoal()
  const [dialogOpen, setDialogOpen] = useState(false)

  // planPending, not just `!plan` — plan is legitimately `null` once loaded with no
  // active plan, but `undefined` while still loading. Without this, eligibleGoals below
  // would briefly treat every already-attached goal as still eligible on first render,
  // since attachedGoalIds reads plan?.goals before the query has resolved.
  if (!goals || planPending) return null

  const attachedGoalIds = new Set((plan?.goals ?? []).map((g) => g.goalId))
  const eligibleGoals = goals.filter((g) => g.goalType === "race" && g.status === "active" && !attachedGoalIds.has(g.id))
  const hasPrimary = (plan?.goals ?? []).some((g) => g.role === "primary")

  if (!plan && eligibleGoals.length === 0) {
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
            {plan ? "Add a Goal" : "Start a Plan"}
          </Button>
        )}
      </div>

      {plan?.goals.map((goalRef) => (
        <GoalGroup key={goalRef.goalId} plan={plan} goalRef={goalRef} defaultExpanded={plan.goals.length === 1} />
      ))}

      {plan && <PlanScheduleSettings plan={plan} />}

      <StartPlanDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        eligibleGoals={eligibleGoals}
        starting={addPlanGoal.isPending}
        hasPrimary={hasPrimary}
        onStart={(goalId, role) => {
          addPlanGoal.mutate({ goalId, role }, { onSuccess: () => setDialogOpen(false) })
        }}
      />
    </div>
  )
}
