import { useState } from "react"
import type { Goal } from "@/lib/api"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Button } from "@/components/ui/button"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"

// Goal picker restricted to race goals not already on the plan — phase/deload/ramp math
// is weeks-until-race-date and has no defined analog for a consistency or distance_target
// goal (enforced again server-side; this is just keeping the picker honest).
//
// P21 (docs/P21_CONCURRENT_GOALS.md): the app now has at most ONE active plan, serving
// multiple goals in a role. `hasPrimary` tells this dialog whether the plan already has a
// primary goal — when it does, a newly-added goal needs an explicit role choice (default
// "supporting") rather than silently displacing the existing primary.
export function StartPlanDialog({
  open, onOpenChange, eligibleGoals, onStart, starting, hasPrimary,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  eligibleGoals: Goal[]
  onStart: (goalId: string, role?: "primary" | "supporting") => void
  starting: boolean
  hasPrimary: boolean
}) {
  const [goalId, setGoalId] = useState<string>("")
  const [role, setRole] = useState<"primary" | "supporting">("supporting")

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{hasPrimary ? "Add a Goal to Your Plan" : "Start a Plan"}</DialogTitle>
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
          {hasPrimary && (
            <div className="flex flex-col gap-1.5">
              <Label>Role</Label>
              <Select value={role} onValueChange={(v) => setRole(v as "primary" | "supporting")}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="supporting">Supporting — shares the budget, doesn't drive periodization</SelectItem>
                  <SelectItem value="primary">Primary — takes over what the plan trains toward</SelectItem>
                </SelectContent>
              </Select>
            </div>
          )}
          <Button
            disabled={!goalId || starting}
            onClick={() => {
              onStart(goalId, hasPrimary ? role : undefined)
              setGoalId("")
              setRole("supporting")
            }}
          >
            {starting ? "Saving…" : "Save"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
