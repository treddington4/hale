import { useEffect, useState } from "react"
import type { Run, RunUpdate } from "@/lib/api"
import { activityFamily, isDistanceActivity, RUN_TYPES, STRENGTH_TYPES } from "@/lib/runs"
import { useGear } from "@/hooks/useGear"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Button } from "@/components/ui/button"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"

function emptyForm(run: Run | null) {
  return {
    type: run?.type ?? "Easy",
    tempF: run?.tempF != null ? String(run.tempF) : "",
    weatherCondition: run?.weatherCondition ?? "",
    isTreadmill: run?.isTreadmill ?? false,
    rpe: run?.rpe != null ? String(run.rpe) : "",
    notes: run?.notes ?? "",
    gearId: run?.gearId ?? "",
  }
}

// Same Ride-vs-everything-else split as the backend's auto-assignment
// (stats._gear_kind_for_activity) — this app is primarily a running app, so
// "bike" only applies to Ride and everything else wears shoes.
function gearKindForActivity(activityType: string): "shoe" | "bike" {
  return activityType === "Ride" ? "bike" : "shoe"
}

// Ports openEditModal() — "Run type" only makes sense for a run, session type
// (Full Body/Upper Body/...) only for strength, and temperature/weather/treadmill
// only apply to a distance activity. Only fields actually shown get sent on save
// — sending type: null for an activity with no type dropdown would silently
// clear any existing override.
export function EditRunDialog({
  open,
  onOpenChange,
  run,
  onSave,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  run: Run | null
  onSave: (body: RunUpdate) => void
}) {
  const [form, setForm] = useState(() => emptyForm(run))
  const { data: gear } = useGear()

  useEffect(() => {
    if (open) setForm(emptyForm(run))
  }, [open, run])

  if (!run) return null

  const family = activityFamily(run.activityType)
  const isDistance = isDistanceActivity(run.activityType)
  const typeOptions = family === "run" ? RUN_TYPES : family === "strength" ? STRENGTH_TYPES : null
  const gearOptions = isDistance
    ? (gear ?? []).filter((g) => g.kind === gearKindForActivity(run.activityType) && !g.retiredDate)
    : []

  function save() {
    const body: RunUpdate = {
      rpe: form.rpe === "" ? null : Number(form.rpe),
      notes: form.notes,
    }
    if (typeOptions) body.type = form.type
    if (isDistance) {
      body.isTreadmill = form.isTreadmill
      body.tempF = form.isTreadmill ? null : form.tempF === "" ? null : Number(form.tempF)
      body.weatherCondition = form.isTreadmill ? null : form.weatherCondition
      body.gearId = form.gearId || null
    }
    onSave(body)
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{run.name}</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          {typeOptions && (
            <div className="flex flex-col gap-1.5">
              <Label>{family === "run" ? "Run type" : "Session type"}</Label>
              <Select value={form.type} onValueChange={(v) => setForm({ ...form, type: v })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {typeOptions.map((t) => (
                    <SelectItem key={t} value={t}>
                      {t}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          {isDistance && (
            <>
              <div className="flex flex-col gap-1.5">
                <Label>Temperature (°F)</Label>
                <Input
                  type="number"
                  placeholder="e.g. 72"
                  value={form.tempF}
                  onChange={(e) => setForm({ ...form, tempF: e.target.value })}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Weather condition</Label>
                <Input
                  placeholder="e.g. Clear, humid"
                  value={form.weatherCondition}
                  onChange={(e) => setForm({ ...form, weatherCondition: e.target.value })}
                />
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={form.isTreadmill}
                  onChange={(e) => setForm({ ...form, isTreadmill: e.target.checked })}
                />
                Treadmill run (not outdoors)
              </label>
              {gearOptions.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <Label>Gear</Label>
                  <Select value={form.gearId || "__none"} onValueChange={(v) => setForm({ ...form, gearId: v === "__none" ? "" : v })}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__none">Unassigned</SelectItem>
                      {gearOptions.map((g) => (
                        <SelectItem key={g.id} value={g.id}>
                          {g.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </>
          )}
          <div className="flex flex-col gap-1.5">
            <Label>Perceived effort (RPE, 1-10)</Label>
            <Input
              type="number"
              min={1}
              max={10}
              value={form.rpe}
              onChange={(e) => setForm({ ...form, rpe: e.target.value })}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Notes</Label>
            <Textarea value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </div>
          <Button onClick={save}>Save</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
