import type { Workout, PlanWeek } from "@/lib/api"
import { WORKOUT_TYPE_LABELS } from "@/lib/workouts"
import { cn } from "@/lib/utils"

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

// Where a scheduled session came from. This app runs two planners at once — Garmin's
// adaptive plan and HALE's own generator — and they are NOT the same plan. Showing a
// week's sessions without saying which produced each one is what made the plan panel
// unreadable: a weekly volume target from one system sitting above workouts from
// the other, with nothing indicating they were unrelated.
const SOURCE_LABELS: Record<string, string> = {
  garmin: "Garmin",
  generator: "HALE",
  coach: "Coach",
  hevy: "Hevy",
}

function SourceTag({ source }: { source: string }) {
  const label = SOURCE_LABELS[source]
  if (!label) return null
  return (
    <span className="border-border bg-secondary text-muted-foreground rounded border px-1 py-0.5 text-[10px]">
      {label}
    </span>
  )
}

function dayOffset(weekStart: string, scheduledDate: string): number {
  const a = new Date(weekStart + "T00:00:00")
  const b = new Date(scheduledDate + "T00:00:00")
  return Math.round((b.getTime() - a.getTime()) / 86_400_000)
}

export function PlanWeekDays({
  week,
  workouts,
  todayIso,
}: {
  week: PlanWeek
  workouts: Workout[]
  todayIso: string
}) {
  // Bucket by weekday rather than listing chronologically, so an empty day reads as
  // "nothing scheduled" instead of silently not existing.
  const byDay: Workout[][] = [[], [], [], [], [], [], []]
  for (const w of workouts) {
    const i = dayOffset(week.weekStart, w.scheduledDate)
    if (i >= 0 && i < 7) byDay[i].push(w)
  }

  return (
    <div className="flex flex-col">
      {byDay.map((dayWorkouts, i) => {
        const dateIso = new Date(new Date(week.weekStart + "T00:00:00").getTime() + i * 86_400_000)
          .toISOString()
          .slice(0, 10)
        const isToday = dateIso === todayIso
        return (
          <div
            key={i}
            className={cn(
              "border-border/50 flex gap-3 border-b py-1.5 text-[13px] last:border-b-0",
              isToday && "bg-secondary/40 -mx-2 rounded px-2",
            )}
          >
            <div className={cn("text-muted-foreground w-11 shrink-0 tabular-nums", isToday && "text-hale-hot font-semibold")}>
              {DAY_NAMES[i]} {dateIso.slice(8)}
            </div>
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              {dayWorkouts.length === 0 ? (
                <span className="text-hale-faint">—</span>
              ) : (
                dayWorkouts.map((w) => (
                  <div key={w.id} className="flex flex-wrap items-center gap-1.5">
                    <span className={cn(w.status === "completed" && "text-hale-good")}>
                      {WORKOUT_TYPE_LABELS[w.workoutType] ?? w.workoutType}
                      {w.workoutType !== "rest" && w.workoutType !== "cross_train" && w.activityType
                        ? ` (${w.activityType})`
                        : ""}
                    </span>
                    {w.targetDistanceMi ? (
                      <span className="text-muted-foreground">{w.targetDistanceMi} mi</span>
                    ) : w.targetDurationSec ? (
                      <span className="text-muted-foreground">{Math.round(w.targetDurationSec / 60)} min</span>
                    ) : null}
                    <SourceTag source={w.source} />
                    {w.status !== "planned" && (
                      <span className="text-hale-faint text-[10px]">{w.status}</span>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
