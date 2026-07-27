import * as React from "react"
import type { PlanWeek } from "@/lib/api"
import { cn } from "@/lib/utils"

const PHASE_LABELS: Record<PlanWeek["phase"], string> = {
  base: "Base", build: "Build", peak: "Peak", taper: "Taper",
}

function Pill({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={cn("border-border bg-secondary rounded border px-1.5 py-0.5 text-[10px]", className)}>
      {children}
    </span>
  )
}

// One week of a plan's target-vs-actual. Projected weeks get a dashed border and a
// muted style — they're previews built by chaining each week's target off the last,
// not commitments, and rendering them identically to real weeks would overstate how
// much is actually known about a week 6 out.
export function WeeklyPlanCard({ week }: { week: PlanWeek }) {
  const targetLabel = `${week.targetMi.toFixed(1)} mi`
  const actualLabel = week.actualMi == null ? "—" : `${week.actualMi.toFixed(1)} mi`

  return (
    <div
      className={cn(
        "flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-[13px]",
        week.isProjection ? "border-border/60 border-dashed" : "border-border",
        week.isCurrentWeek && "bg-secondary/50",
      )}
    >
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1.5">
          <span className={cn("font-medium", week.isProjection && "text-muted-foreground")}>{week.weekStart}</span>
          {week.isCurrentWeek && <Pill className="border-hale-hot/40 text-hale-hot">This week</Pill>}
          <Pill>{PHASE_LABELS[week.phase]}</Pill>
          {week.isDeload && <Pill>Deload</Pill>}
          {/* Distinct from a P21 planned-interruption badge (e.g. a honeymoon), which
              means "reduced on purpose" — frozen means readiness capped this week,
              a different signal that must never share a badge with it. */}
          {week.frozen && <Pill className="border-hale-hot/40 text-hale-hot">Frozen</Pill>}
          {week.isProjection && <Pill className="text-muted-foreground">Projected</Pill>}
        </div>
        <div className={cn("text-muted-foreground", week.isProjection && "italic")}>
          {/* Spelled out rather than a bare "x / y" — the unlabelled form read as an
              unexplained pair of numbers, which is what made this panel confusing. */}
          {week.isProjection ? (
            <>planned {targetLabel}</>
          ) : (
            <>
              ran {actualLabel} of {targetLabel}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
