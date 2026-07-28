import { useState } from "react"
import type { TrainingPlan } from "@/lib/api"
import { useUpdatePlan } from "@/hooks/usePlans"
import { Label } from "@/components/ui/label"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// 0=Mon..6=Sun, matching generator.WEEKDAY_SKELETON's own indexing (Python's
// date.weekday()). Deliberately not JS's Sunday-first getDay() — the wire format is
// the backend's, and converting in two places is how the two drift apart.
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

export function PlanScheduleSettings({ plan }: { plan: TrainingPlan }) {
  const updatePlan = useUpdatePlan()
  const [open, setOpen] = useState(false)

  // null = "not declared", which is a real state distinct from "every day selected":
  // it means the generator uses its default skeleton rather than an explicit choice.
  const availableDays = plan.availableDays
  const [hoursDraft, setHoursDraft] = useState<string>(
    plan.weeklyHoursCap != null ? String(plan.weeklyHoursCap) : "",
  )

  function toggleDay(d: number) {
    const current = availableDays ?? [0, 1, 2, 3, 4, 5, 6]
    const next = current.includes(d) ? current.filter((x) => x !== d) : [...current, d].sort((a, b) => a - b)
    updatePlan.mutate({ planId: plan.id, availableDays: next })
  }

  function saveHours() {
    const trimmed = hoursDraft.trim()
    if (trimmed === "") {
      updatePlan.mutate({ planId: plan.id, weeklyHoursCap: null })
      return
    }
    const n = Number(trimmed)
    if (!Number.isFinite(n) || n <= 0 || n > 168) return
    updatePlan.mutate({ planId: plan.id, weeklyHoursCap: n })
  }

  // Weekday work window, applied Mon-Fri. Deliberately one window rather than a
  // per-day editor: the schedule here is fixed Mon-Fri, and a seven-row time grid for
  // a value that's the same five times is friction with no information gain. A genuinely
  // varying week would need the per-day form the API already accepts.
  const wd = plan.timeBudget.find((d) => d.weekday === 0)
  const [workStart, setWorkStart] = useState(wd?.workStart ?? "")
  const [workEnd, setWorkEnd] = useState(wd?.workEnd ?? "")

  function saveWork(start: string, end: string) {
    if (!start || !end) {
      updatePlan.mutate({ planId: plan.id, workSchedule: null })
      return
    }
    const schedule: Record<string, { start: string; end: string; breakHours: number }> = {}
    for (let d = 0; d <= 4; d++) schedule[String(d)] = { start, end, breakHours: 1 }
    updatePlan.mutate({ planId: plan.id, workSchedule: schedule })
  }

  const budget = plan.timeBudget.filter((d) => d.freeHours != null)
  const weeklyTrainable = budget.reduce((s, d) => s + (d.capHours ?? 0), 0)
  const longest = plan.hours?.longestSessionHours ?? null
  // The constraint that actually binds: a long session has to fit *some* day, and as a
  // marathon long run grows past a weekday's window only the weekend days remain.
  const daysFittingLongest =
    longest != null ? budget.filter((d) => (d.capHours ?? 0) >= longest).length : null

  return (
    <div className="border-border rounded-lg border">
      <button
        className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left text-sm font-medium"
        onClick={() => setOpen(!open)}
      >
        <span>
          {open ? "▼" : "▶"} Schedule &amp; availability
        </span>
        <span className="text-muted-foreground text-xs">
          {availableDays ? `${availableDays.length} day${availableDays.length === 1 ? "" : "s"}/wk` : "every day"}
          {plan.weeklyHoursCap != null && ` · ${plan.weeklyHoursCap}h cap`}
        </span>
      </button>

      {open && (
        <div className="border-border flex flex-col gap-4 border-t p-3">
          <div className="flex flex-col gap-1.5">
            <Label>Training days</Label>
            <div className="flex flex-wrap gap-1">
              {DAYS.map((label, d) => {
                const on = availableDays ? availableDays.includes(d) : true
                return (
                  <button
                    key={d}
                    onClick={() => toggleDay(d)}
                    disabled={updatePlan.isPending}
                    className={cn(
                      "rounded border px-2 py-1 text-xs",
                      on ? "border-hale-hot/40 text-hale-hot" : "border-border text-muted-foreground",
                    )}
                  >
                    {label}
                  </button>
                )
              })}
            </div>
            <p className="text-hale-faint text-[11px] leading-relaxed">
              Days you're not available become rest days. Your long run moves to the latest
              available day rather than being dropped.
            </p>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Long run day</Label>
            <div className="flex flex-wrap gap-1">
              {DAYS.map((label, d) => (
                <button
                  key={d}
                  onClick={() =>
                    updatePlan.mutate({ planId: plan.id, longRunDay: plan.longRunDay === d ? null : d })
                  }
                  disabled={updatePlan.isPending}
                  className={cn(
                    "rounded border px-2 py-1 text-xs",
                    plan.longRunDay === d
                      ? "border-hale-hot/40 text-hale-hot"
                      : "border-border text-muted-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
            <p className="text-hale-faint text-[11px] leading-relaxed">
              {plan.longRunDay == null
                ? "Using the default (Saturday). Pick a day to pin it, or tap the selected day again to clear."
                : "Tap the selected day again to go back to the default."}
            </p>
            {/* Informational only. A derived multi-week average must never override an
                explicit day choice, so this never moves the run — it just says what
                your own data shows. Absent entirely when there aren't enough nights. */}
            {plan.typicalWakeTime && (
              <p className="text-hale-faint text-[11px] leading-relaxed">
                You typically wake around {plan.typicalWakeTime}. HALE flags a long run
                that would cut that short, but never moves it for you.
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Work hours (Mon–Fri)</Label>
            <div className="flex items-center gap-2">
              <Input
                type="time" className="h-8 w-28" value={workStart}
                onChange={(e) => setWorkStart(e.target.value)}
                onBlur={() => saveWork(workStart, workEnd)}
              />
              <span className="text-muted-foreground text-xs">to</span>
              <Input
                type="time" className="h-8 w-28" value={workEnd}
                onChange={(e) => setWorkEnd(e.target.value)}
                onBlur={() => saveWork(workStart, workEnd)}
              />
            </div>
            <p className="text-hale-faint text-[11px] leading-relaxed">
              Assumes an hour for lunch. This is the one piece HALE can't work out on its
              own — your sleep comes from Garmin, your work schedule doesn't.
            </p>
          </div>

          {budget.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <Label>When you can actually train</Label>
              <div className="overflow-x-auto">
                <table className="w-full text-xs tabular-nums">
                  <thead className="text-hale-faint">
                    <tr>
                      <th className="py-0.5 pr-2 text-left font-normal">Day</th>
                      <th className="py-0.5 pr-2 text-left font-normal">Awake</th>
                      <th className="py-0.5 pr-2 text-right font-normal">Free</th>
                      <th className="py-0.5 text-right font-normal">Trainable</th>
                    </tr>
                  </thead>
                  <tbody className="text-muted-foreground">
                    {budget.map((d) => (
                      <tr key={d.weekday}>
                        <td className="py-0.5 pr-2">{DAYS[d.weekday]}</td>
                        <td className="py-0.5 pr-2">
                          {d.wakeTime}–{d.bedTime}
                        </td>
                        <td className="py-0.5 pr-2 text-right">{d.freeHours?.toFixed(1)}h</td>
                        <td
                          className={cn(
                            "py-0.5 text-right",
                            longest != null && (d.capHours ?? 0) >= longest && "text-hale-hot",
                          )}
                        >
                          {d.capHours?.toFixed(1)}h
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-hale-faint text-[11px] leading-relaxed">
                Awake window is measured from your Garmin sleep data. <em>Free</em> is that
                minus work — real arithmetic. <em>Trainable</em> is a rough third of free
                time, capped at 4h: an estimate of what's sustainable, not a target, since
                free time also has to cover meals, family and winding down.
              </p>
              <p className="text-muted-foreground text-xs">
                About {weeklyTrainable.toFixed(1)}h a week could go to training.
              </p>
              {/* The genuinely binding constraint, and the reason this table exists. */}
              {longest != null && daysFittingLongest != null && (
                <p className={cn("text-xs", daysFittingLongest === 0 && "text-hale-hot")}>
                  {daysFittingLongest === 0
                    ? `This week's longest session (${longest.toFixed(1)}h) doesn't fit any day's window.`
                    : `This week's longest session is ${longest.toFixed(1)}h — it fits ${daysFittingLongest} of your ${budget.length} days (highlighted).`}
                </p>
              )}
            </div>
          )}

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="weekly-hours-cap">Weekly hours available</Label>
            <div className="flex items-center gap-2">
              <Input
                id="weekly-hours-cap"
                type="number"
                min={0}
                max={168}
                step={0.5}
                className="h-8 w-24"
                placeholder="none"
                value={hoursDraft}
                onChange={(e) => setHoursDraft(e.target.value)}
                onBlur={saveHours}
              />
              <span className="text-muted-foreground text-xs">hours/week</span>
            </div>
            {/* Stated plainly because the honest default here is "no number at all" —
                this is the athlete's real-life bandwidth, which nothing in their
                training history can tell us. */}
            <p className="text-hale-faint text-[11px] leading-relaxed">
              Optional. What you can realistically train in a week, including strength.
              Leave blank for no cap — HALE won't invent one from your history, since how
              much you <em>have</em> trained isn't how much you're <em>able</em> to.
            </p>

            {plan.hours && plan.hours.capHours != null && plan.hours.demandHours != null && (
              <div className="text-xs">
                <span className={plan.hours.isOverCap ? "text-hale-hot" : "text-muted-foreground"}>
                  This week's HALE sessions: {plan.hours.demandHours.toFixed(1)}h of{" "}
                  {plan.hours.capHours.toFixed(1)}h
                  {plan.hours.isOverCap && ` · over by ${plan.hours.overBy?.toFixed(1)}h`}
                </span>
                {/* Never silently treated as zero — the total is a floor while any
                    session has no derivable duration. */}
                {plan.hours.unknownSessions > 0 && (
                  <span className="text-hale-faint">
                    {" "}
                    (+{plan.hours.unknownSessions} session
                    {plan.hours.unknownSessions === 1 ? "" : "s"} with no set duration — this
                    total is a minimum)
                  </span>
                )}
                {plan.hours.isOverCap && (
                  // The design doc is explicit that a conflict is surfaced, never
                  // auto-resolved by quietly shrinking the primary goal's long run.
                  <p className="text-hale-faint mt-0.5 leading-relaxed">
                    HALE won't shrink your key sessions to fit. Raise the cap, drop a
                    supporting goal, or move a race date.
                  </p>
                )}
                <p className="text-hale-faint mt-0.5 leading-relaxed">
                  Counts HALE's own prescribed sessions only — not your Garmin plan's,
                  since those overlap on the same days.
                </p>
              </div>
            )}

            {/* Two planners genuinely disagree and by how much is the useful signal —
                so they're shown side by side rather than summed (summing them would
                triple-count days where all three prescribe something). */}
            {plan.hours && Object.keys(plan.hours.bySource).length > 0 && (
              <div className="text-xs">
                <div className="text-muted-foreground mb-0.5">This week, by planner:</div>
                {Object.entries(plan.hours.bySource)
                  .sort(([a], [b]) => a.localeCompare(b))
                  .map(([src, v]) => (
                    <div key={src} className="text-muted-foreground">
                      <span className="capitalize">{src === "generator" ? "HALE" : src}</span>:{" "}
                      <span className="tabular-nums">{v.hours.toFixed(1)}h</span>
                      {v.unknownSessions > 0 && (
                        <span className="text-hale-faint">
                          {" "}
                          (+{v.unknownSessions} without a set duration)
                        </span>
                      )}
                    </div>
                  ))}
                <p className="text-hale-faint mt-0.5 leading-relaxed">
                  You do one of these per day, not all — so these aren't added together.
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
