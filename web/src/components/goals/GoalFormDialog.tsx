import { useEffect, useMemo, useState } from "react"
import type { Goal, GoalInput, GoalType } from "@/lib/api"
import { useAllRuns } from "@/hooks/useActivities"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Button } from "@/components/ui/button"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"

const GOAL_TYPE_LABELS: Record<GoalType, string> = {
  race: "Race",
  consistency: "Consistency",
  distance_target: "Distance target",
}

function emptyForm(goal: Goal | null) {
  const type: GoalType = goal?.goalType ?? "race"
  return {
    name: goal?.name ?? "",
    goalType: type,
    activityTypes: goal?.activityTypes ?? ["Run"],
    priority: String(goal?.priority ?? 0),
    notes: goal?.notes ?? "",
    targetDate: type === "race" ? (goal?.targetDate ?? "") : "",
    raceMi: type === "race" ? String(goal?.targetValue ?? "") : "",
    consistencyUnit: goal?.targetUnit ?? "runs_per_week",
    consistencyValue: type === "consistency" ? String(goal?.targetValue ?? "") : "",
    distanceMi: type === "distance_target" ? String(goal?.targetValue ?? "") : "",
    distanceStart: goal?.startDate ?? "",
    distanceDeadline: type === "distance_target" ? (goal?.targetDate ?? "") : "",
  }
}

export function GoalFormDialog({
  open,
  onOpenChange,
  goal,
  onSave,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  goal: Goal | null
  onSave: (body: GoalInput) => void
}) {
  const [form, setForm] = useState(() => emptyForm(goal))
  const allRunsQuery = useAllRuns()

  useEffect(() => {
    if (open) setForm(emptyForm(goal))
  }, [open, goal])

  // Data-driven from actual run history (not a fixed enum) — matches legacy's
  // openGoalModal(), which unions ["Run","Ride","Swim"] with every distinct
  // activityType already seen in synced runs.
  const knownTypes = useMemo(() => {
    const set = new Set(["Run", "Ride", "Swim"])
    ;(allRunsQuery.data ?? []).forEach((r) => set.add(r.activityType || "Run"))
    return Array.from(set)
  }, [allRunsQuery.data])

  const isEdit = !!goal

  function toggleActivityType(t: string) {
    setForm((f) => ({
      ...f,
      activityTypes: f.activityTypes.includes(t) ? f.activityTypes.filter((x) => x !== t) : [...f.activityTypes, t],
    }))
  }

  function save() {
    const body: GoalInput = {
      goalType: form.goalType,
      name: form.name || "Untitled goal",
      activityTypes: form.activityTypes.length ? form.activityTypes : ["Run"],
      notes: form.notes,
      priority: Number(form.priority) || 0,
    }
    if (form.goalType === "race") {
      body.targetDate = form.targetDate || null
      body.targetValue = Number(form.raceMi) || null
      body.targetUnit = "miles"
    } else if (form.goalType === "consistency") {
      body.targetUnit = form.consistencyUnit
      body.targetValue = Number(form.consistencyValue) || null
    } else {
      body.targetValue = Number(form.distanceMi) || null
      body.targetUnit = "miles"
      body.startDate = form.distanceStart || null
      body.targetDate = form.distanceDeadline || null
    }
    onSave(body)
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit Goal" : "New Goal"}</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label>Name</Label>
            <Input
              placeholder="e.g. Manchester City Marathon"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Goal type</Label>
            <Select value={form.goalType} onValueChange={(v) => setForm({ ...form, goalType: v as GoalType })}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(GOAL_TYPE_LABELS).map(([v, label]) => (
                  <SelectItem key={v} value={v}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Activity types</Label>
            <div className="flex flex-col gap-1">
              {knownTypes.map((t) => (
                <label key={t} className="flex cursor-pointer items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.activityTypes.includes(t)}
                    onChange={() => toggleActivityType(t)}
                  />
                  {t}
                </label>
              ))}
            </div>
          </div>

          {form.goalType === "race" && (
            <>
              <div className="flex flex-col gap-1.5">
                <Label>Race date</Label>
                <Input
                  type="date"
                  value={form.targetDate}
                  onChange={(e) => setForm({ ...form, targetDate: e.target.value })}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Race distance (mi)</Label>
                <Input
                  type="number"
                  step="0.1"
                  placeholder="e.g. 26.2"
                  value={form.raceMi}
                  onChange={(e) => setForm({ ...form, raceMi: e.target.value })}
                />
              </div>
            </>
          )}

          {form.goalType === "consistency" && (
            <div className="flex flex-col gap-1.5">
              <Label>Target</Label>
              <Select
                value={form.consistencyUnit}
                onValueChange={(v) => setForm({ ...form, consistencyUnit: v })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="runs_per_week">Runs per week</SelectItem>
                  <SelectItem value="miles_per_week">Miles per week</SelectItem>
                </SelectContent>
              </Select>
              <Input
                type="number"
                step="0.1"
                placeholder="e.g. 3"
                className="mt-1.5"
                value={form.consistencyValue}
                onChange={(e) => setForm({ ...form, consistencyValue: e.target.value })}
              />
            </div>
          )}

          {form.goalType === "distance_target" && (
            <>
              <div className="flex flex-col gap-1.5">
                <Label>Target distance (mi)</Label>
                <Input
                  type="number"
                  step="1"
                  placeholder="e.g. 500"
                  value={form.distanceMi}
                  onChange={(e) => setForm({ ...form, distanceMi: e.target.value })}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Start date</Label>
                <Input
                  type="date"
                  value={form.distanceStart}
                  onChange={(e) => setForm({ ...form, distanceStart: e.target.value })}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Deadline (optional)</Label>
                <Input
                  type="date"
                  value={form.distanceDeadline}
                  onChange={(e) => setForm({ ...form, distanceDeadline: e.target.value })}
                />
              </div>
            </>
          )}

          <div className="flex flex-col gap-1.5">
            <Label>Priority</Label>
            <Input
              type="number"
              step="1"
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value })}
            />
            <div className="text-hale-faint text-xs">Lower shows first when more than one goal is active</div>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Notes</Label>
            <Textarea value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </div>

          <Button onClick={save}>{isEdit ? "Save" : "Create Goal"}</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
