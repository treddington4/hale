import type { RecoverySession, RecoveryTool } from "@/lib/api"
import { WORKOUT_STATUS_COLORS } from "@/lib/workouts"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"

// Coach-recommended only (see coach.py's RecoveryTool docstring on why manual
// creation isn't built yet) — cards only offer status/delete actions, no edit.
export function RecoverySessionCard({
  session,
  tool,
  onComplete,
  onSkip,
  onDelete,
  preview = false,
}: {
  session: RecoverySession
  tool?: RecoveryTool
  onComplete?: () => void
  onSkip?: () => void
  onDelete?: () => void
  preview?: boolean
}) {
  const toolName = tool ? tool.name : "Recovery tool"
  return (
    <Card className="gap-2">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="text-muted-foreground">
          {session.scheduledDate} · {toolName}
        </span>
        {!preview && <span style={{ color: WORKOUT_STATUS_COLORS[session.status] }}>{session.status}</span>}
      </div>
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="text-muted-foreground">Session</span>
        <span>
          Level {session.level} · {session.durationMin} min{session.zoneBoost ? " · Zone boost" : ""}
        </span>
      </div>
      {session.rationale && (
        <div className="flex items-baseline justify-between gap-3 text-sm">
          <span className="text-muted-foreground">Why</span>
          <span className="text-right font-normal whitespace-pre-line">{session.rationale}</span>
        </div>
      )}
      {!preview && (
        <div className="mt-1 flex gap-3">
          {session.status === "planned" && (
            <>
              <Button variant="link" size="sm" className="h-auto p-0" onClick={onComplete}>
                Mark Done
              </Button>
              <Button variant="link" size="sm" className="h-auto p-0" onClick={onSkip}>
                Skip
              </Button>
            </>
          )}
          <Button
            variant="link"
            size="sm"
            className="text-hale-hot h-auto p-0"
            onClick={() => {
              if (window.confirm("Delete this recovery session? This can't be undone.")) {
                onDelete?.()
              }
            }}
          >
            Delete
          </Button>
        </div>
      )}
    </Card>
  )
}
