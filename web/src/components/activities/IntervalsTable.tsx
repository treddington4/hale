import type { IntervalRep, RecoveryRep } from "@/lib/activities"
import { paceStr } from "@/lib/format"
import { isPlausibleHR } from "@/hooks/useHrFloor"

const SEGMENT_STYLE: Record<string, { label: string; color: string }> = {
  warmup: { label: "Warmup", color: "#8B93A1" },
  work: { label: "Work", color: "rgb(255,107,53)" },
  recovery: { label: "Recovery", color: "rgb(76,201,240)" },
  cooldown: { label: "Cooldown", color: "#8B93A1" },
}

export function IntervalsTable({
  intervals,
  recovery,
  hrFloor,
}: {
  intervals: IntervalRep[]
  recovery: RecoveryRep[]
  hrFloor: number
}) {
  const workReps = intervals.filter((iv) => iv.segment === "work")
  const hasRecovery = recovery.length > 0
  const recoveryByRep = Object.fromEntries(recovery.map((r) => [r.repIndex, r]))

  const avgDur = workReps.length
    ? Math.round(workReps.reduce((s, r) => s + (r.durationSec || 0), 0) / workReps.length)
    : null

  let workIdx = 0

  return (
    <div className="flex flex-col gap-1.5 text-xs">
      {workReps.length > 0 && (
        <div className="text-hale-faint mb-1">
          {workReps.length} work reps · avg {avgDur}s each
        </div>
      )}
      <div className={`text-muted-foreground grid gap-x-2 pb-1 ${hasRecovery ? "grid-cols-7" : "grid-cols-6"}`}>
        <span>Segment</span>
        <span>Pace</span>
        <span>Time</span>
        <span>HR</span>
        <span>Max</span>
        <span>Cad</span>
        {hasRecovery && <span>Recovery</span>}
      </div>
      {intervals.map((iv, i) => {
        const style = SEGMENT_STYLE[iv.segment] || { label: iv.segment, color: "#8B93A1" }
        if (iv.segment === "work") workIdx++
        const rec = iv.segment === "work" ? recoveryByRep[workIdx] : null
        return (
          <div
            key={i}
            className={`grid items-center gap-x-2 py-0.5 ${hasRecovery ? "grid-cols-7" : "grid-cols-6"}`}
            style={{
              borderLeft: `2px solid ${style.color}`,
              background: iv.segment === "work" ? `${style.color}0F` : "transparent",
              paddingLeft: 4,
            }}
          >
            <span style={{ color: style.color, fontWeight: iv.segment === "work" ? 700 : 400 }}>
              {style.label}
              {iv.segment === "work" ? ` ${workIdx}` : ""}
            </span>
            <span>{paceStr(iv.paceSecPerMi)}/mi</span>
            <span className="text-muted-foreground">{iv.durationSec ?? "--"}s</span>
            <span>{isPlausibleHR(iv.avgHR, hrFloor) ? iv.avgHR : "--"}</span>
            <span className="text-muted-foreground">{isPlausibleHR(iv.maxHR, hrFloor) ? iv.maxHR : "--"}</span>
            <span className="text-muted-foreground">{iv.avgCadence != null ? Math.round(iv.avgCadence) : "--"}</span>
            {hasRecovery &&
              (rec ? (
                rec.recoverySec != null ? (
                  <span className="text-hale-cold">{Math.round(rec.recoverySec)}s</span>
                ) : (
                  <span className="text-hale-faint" title="Didn't drop 20bpm before the next rep started">
                    —
                  </span>
                )
              ) : (
                <span />
              ))}
          </div>
        )
      })}
    </div>
  )
}
