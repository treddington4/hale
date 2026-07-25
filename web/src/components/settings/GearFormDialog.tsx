import { useEffect, useState } from "react"
import type { Gear, GearInput, GearKind } from "@/lib/api"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Button } from "@/components/ui/button"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"

const KIND_LABELS: Record<GearKind, string> = {
  shoe: "Shoe",
  bike: "Bike",
  bike_component: "Bike component",
}

function todayIso() {
  return new Date().toISOString().slice(0, 10)
}

function emptyForm(gear: Gear | null, defaultKind: GearKind) {
  return {
    name: gear?.name ?? "",
    kind: gear?.kind ?? defaultKind,
    parentGearId: gear?.parentGearId ?? "",
    startDate: gear?.startDate ?? todayIso(),
    replaceAtMi: gear?.replaceAtMi != null ? String(gear.replaceAtMi) : "",
    isDefault: gear?.isDefault ?? false,
    notes: gear?.notes ?? "",
  }
}

export function GearFormDialog({
  open,
  onOpenChange,
  gear,
  defaultKind,
  bikes,
  onSave,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  gear: Gear | null
  defaultKind: GearKind
  bikes: Gear[]
  onSave: (body: GearInput) => void
}) {
  const [form, setForm] = useState(() => emptyForm(gear, defaultKind))

  useEffect(() => {
    if (open) setForm(emptyForm(gear, defaultKind))
  }, [open, gear, defaultKind])

  const isEdit = !!gear
  const isComponent = form.kind === "bike_component"

  function save() {
    if (isComponent && !form.parentGearId) return
    const body: GearInput = {
      name: form.name || "Untitled gear",
      kind: form.kind,
      parentGearId: isComponent ? form.parentGearId : null,
      startDate: form.startDate || todayIso(),
      replaceAtMi: form.replaceAtMi ? Number(form.replaceAtMi) : null,
      isDefault: isComponent ? false : form.isDefault,
      notes: form.notes || null,
    }
    onSave(body)
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit Gear" : "New Gear"}</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label>Name</Label>
            <Input
              placeholder="e.g. Pegasus 41"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Kind</Label>
            <Select
              value={form.kind}
              onValueChange={(v) => setForm({ ...form, kind: v as GearKind, isDefault: false })}
              disabled={isEdit}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(KIND_LABELS).map(([v, label]) => (
                  <SelectItem key={v} value={v}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {isEdit && <div className="text-hale-faint text-xs">Kind can't change after creation — delete and re-add instead.</div>}
          </div>

          {isComponent && (
            <div className="flex flex-col gap-1.5">
              <Label>Bike</Label>
              <Select value={form.parentGearId} onValueChange={(v) => setForm({ ...form, parentGearId: v })}>
                <SelectTrigger>
                  <SelectValue placeholder="Select a bike" />
                </SelectTrigger>
                <SelectContent>
                  {bikes.map((b) => (
                    <SelectItem key={b.id} value={b.id}>
                      {b.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!bikes.length && <div className="text-hale-faint text-xs">Add a bike first.</div>}
            </div>
          )}

          <div className="flex flex-col gap-1.5">
            <Label>{isComponent ? "Installed" : "Start date"}</Label>
            <Input type="date" value={form.startDate} onChange={(e) => setForm({ ...form, startDate: e.target.value })} />
            {isComponent && (
              <div className="text-hale-faint text-xs">Mileage counts from this date on the bike's own rides.</div>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Replace at (mi, optional)</Label>
            <Input
              type="number"
              step="1"
              placeholder="e.g. 400"
              value={form.replaceAtMi}
              onChange={(e) => setForm({ ...form, replaceAtMi: e.target.value })}
            />
          </div>

          {!isComponent && (
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.isDefault}
                onChange={(e) => setForm({ ...form, isDefault: e.target.checked })}
              />
              Default {form.kind === "shoe" ? "shoe" : "bike"} — new{" "}
              {form.kind === "shoe" ? "runs" : "rides"} get assigned to this automatically
            </label>
          )}

          <div className="flex flex-col gap-1.5">
            <Label>Notes</Label>
            <Textarea value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </div>

          <Button onClick={save} disabled={isComponent && !form.parentGearId}>
            {isEdit ? "Save" : "Add Gear"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
