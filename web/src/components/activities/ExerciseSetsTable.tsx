import type { ExerciseSet } from "@/lib/activities"
import { timeStr } from "@/lib/format"

// Groups consecutive same-exercise sets under one running set-number (set 1, 2,
// 3...) rather than repeating the exercise name per row — matches how Hevy/most
// strength apps group a lift's working sets together.
const SET_TYPE_LABELS: Record<string, string> = { warmup: "W", dropset: "D", failure: "F" }

export function ExerciseSetsTable({ sets }: { sets: ExerciseSet[] }) {
  let setNum = 0
  let prevExercise: string | null = null
  let prevSupersetGroup: string | null | undefined = undefined

  return (
    <div className="flex flex-col gap-1.5 text-xs">
      <div className="text-muted-foreground grid grid-cols-5 gap-x-2 pb-1">
        <span>Set</span>
        <span>Exercise</span>
        <span>Reps</span>
        <span>Weight</span>
        <span>Duration</span>
      </div>
      {sets.map((s, i) => {
        setNum = s.exercise === prevExercise ? setNum + 1 : 1
        prevExercise = s.exercise
        const amount = s.reps != null ? `${s.reps}` : s.durationSec != null ? "hold" : "--"
        const isNewSupersetGroup = s.supersetGroup != null && s.supersetGroup !== prevSupersetGroup
        prevSupersetGroup = s.supersetGroup
        const typeLabel = s.setType && SET_TYPE_LABELS[s.setType] ? SET_TYPE_LABELS[s.setType] + " " : ""
        return (
          <div key={i}>
            {isNewSupersetGroup && <div className="text-hale-cold mb-1 text-[11px]">⇄ Superset</div>}
            <div
              className="grid grid-cols-5 items-center gap-x-2"
              style={s.supersetGroup != null ? { borderLeft: "2px solid var(--hale-cold)", paddingLeft: 4 } : undefined}
            >
              <span className="text-muted-foreground">{setNum}</span>
              <span>
                {typeLabel && (
                  <span className="border-border bg-secondary mr-1 rounded border px-1 text-[10px]" title={s.setType ?? undefined}>
                    {typeLabel.trim()}
                  </span>
                )}
                {s.exercise}
              </span>
              <span>{amount}</span>
              <span>{s.weightLb != null ? `${Math.round(s.weightLb)} lb` : "--"}</span>
              <span className="text-muted-foreground">{s.durationSec != null ? timeStr(s.durationSec) : "--"}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
