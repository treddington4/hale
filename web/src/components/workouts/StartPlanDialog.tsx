import { useState } from "react"
import type { Goal } from "@/lib/api"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Button } from "@/components/ui/button"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"

// Goal picker restricted to race goals with no plan yet — phase/deload/ramp math is
// weeks-until-race-date and has no defined analog for a consistency or distance_target
// goal (enforced again server-side; this is just keeping the picker honest).
export function StartPlanDialog({
  open, onOpenChange, eligibleGoals, onStart, starting,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  eligibleGoals: Goal[]
  onStart: (goalId: string) => void
  starting: boolean
}) {
  const [goalId, setGoalId] = useState<string>("")

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Start a Plan</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label>Goal</Label>
            <Select value={goalId} onValueChange={setGoalId}>
              <SelectTrigger>
                <SelectValue placeholder="Choose a race goal" />
              </SelectTrigger>
              <SelectContent>
                {eligibleGoals.map((g) => (
                  <SelectItem key={g.id} value={g.id}>
                    {g.name} {g.targetDate ? `— ${g.targetDate}` : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            disabled={!goalId || starting}
            onClick={() => {
              onStart(goalId)
              setGoalId("")
            }}
          >
            {starting ? "Starting…" : "Start"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
