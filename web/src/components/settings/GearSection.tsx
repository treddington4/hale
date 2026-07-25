import { useMemo, useState } from "react"
import type { Gear, GearInput } from "@/lib/api"
import { useGear, useGearMutations } from "@/hooks/useGear"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { GearFormDialog } from "./GearFormDialog"

function todayIso() {
  return new Date().toISOString().slice(0, 10)
}

function WearBar({ pct }: { pct: number | null }) {
  if (pct == null) return null
  const clamped = Math.max(0, Math.min(100, pct))
  const color = pct >= 100 ? "var(--hale-hot)" : pct >= 85 ? "var(--hale-gold)" : "var(--hale-good)"
  return (
    <div className="bg-background mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
      <div className="h-full rounded-full" style={{ width: `${clamped}%`, background: color }} />
    </div>
  )
}

function GearRow({
  g,
  onEdit,
  onRetire,
  onDelete,
  indent,
}: {
  g: Gear
  onEdit: () => void
  onRetire: () => void
  onDelete: () => void
  indent?: boolean
}) {
  const retired = !!g.retiredDate
  return (
    <div className={`border-border flex items-start justify-between gap-3 border-t py-2.5 first:border-t-0 ${indent ? "pl-4" : ""}`}>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className={`truncate text-sm font-medium ${retired ? "text-muted-foreground line-through" : ""}`}>{g.name}</span>
          {g.isDefault && !retired && (
            <span className="bg-secondary text-hale-good rounded px-1.5 py-0.5 text-[10px] font-medium tracking-wide uppercase">
              Default
            </span>
          )}
          {retired && (
            <span className="bg-secondary text-muted-foreground rounded px-1.5 py-0.5 text-[10px] font-medium tracking-wide uppercase">
              Retired
            </span>
          )}
        </div>
        <div className="text-muted-foreground mt-0.5 font-mono text-xs">
          {g.totalMiles.toFixed(1)} mi{g.replaceAtMi ? ` / ${g.replaceAtMi} mi` : ""}
        </div>
        <WearBar pct={g.wearPct} />
      </div>
      <div className="flex shrink-0 items-center gap-3 pt-0.5">
        <Button variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onEdit}>
          Edit
        </Button>
        {!retired && (
          <Button variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onRetire}>
            Retire
          </Button>
        )}
        <Button variant="link" size="sm" className="text-hale-hot h-auto p-0 text-xs" onClick={onDelete}>
          Delete
        </Button>
      </div>
    </div>
  )
}

export function GearSection() {
  const { data: gear } = useGear()
  const { createGear, updateGear, deleteGear } = useGearMutations()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<Gear | null>(null)
  const [newKind, setNewKind] = useState<"shoe" | "bike" | "bike_component">("shoe")

  const { shoes, bikes, componentsByBike } = useMemo(() => {
    const list = gear ?? []
    const shoes = list.filter((g) => g.kind === "shoe")
    const bikes = list.filter((g) => g.kind === "bike")
    const componentsByBike = new Map<string, Gear[]>()
    list
      .filter((g) => g.kind === "bike_component")
      .forEach((c) => {
        const key = c.parentGearId || ""
        componentsByBike.set(key, [...(componentsByBike.get(key) ?? []), c])
      })
    return { shoes, bikes, componentsByBike }
  }, [gear])

  function openNew(kind: "shoe" | "bike" | "bike_component") {
    setEditing(null)
    setNewKind(kind)
    setDialogOpen(true)
  }

  function openEdit(g: Gear) {
    setEditing(g)
    setDialogOpen(true)
  }

  function handleSave(body: GearInput) {
    if (editing) updateGear.mutate({ id: editing.id, body })
    else createGear.mutate(body)
  }

  if (!gear) return null

  return (
    <>
      <Card className="gap-2">
        <div className="mb-1 flex items-center justify-between">
          <div className="text-sm font-bold">Gear</div>
        </div>
        <div className="text-hale-faint pb-1 text-xs">
          Shoes and bikes auto-assign to new runs/rides once you mark one as default — mileage updates automatically
          as you sync.
        </div>

        <div>
          <div className="text-muted-foreground mt-2 mb-1 text-xs font-semibold tracking-wide uppercase">Shoes</div>
          {shoes.length === 0 && <div className="text-hale-faint py-1 text-xs">No shoes yet.</div>}
          {shoes.map((g) => (
            <GearRow
              key={g.id}
              g={g}
              onEdit={() => openEdit(g)}
              onRetire={() => updateGear.mutate({ id: g.id, body: { retiredDate: todayIso() } })}
              onDelete={() => deleteGear.mutate(g.id)}
            />
          ))}
          <Button variant="outline" size="sm" className="mt-2" onClick={() => openNew("shoe")}>
            + Add shoe
          </Button>
        </div>

        <div>
          <div className="text-muted-foreground mt-4 mb-1 text-xs font-semibold tracking-wide uppercase">Bikes</div>
          {bikes.length === 0 && <div className="text-hale-faint py-1 text-xs">No bikes yet.</div>}
          {bikes.map((b) => (
            <div key={b.id}>
              <GearRow
                g={b}
                onEdit={() => openEdit(b)}
                onRetire={() => updateGear.mutate({ id: b.id, body: { retiredDate: todayIso() } })}
                onDelete={() => deleteGear.mutate(b.id)}
              />
              {(componentsByBike.get(b.id) ?? []).map((c) => (
                <GearRow
                  key={c.id}
                  g={c}
                  indent
                  onEdit={() => openEdit(c)}
                  onRetire={() => updateGear.mutate({ id: c.id, body: { retiredDate: todayIso() } })}
                  onDelete={() => deleteGear.mutate(c.id)}
                />
              ))}
            </div>
          ))}
          <div className="mt-2 flex gap-2">
            <Button variant="outline" size="sm" onClick={() => openNew("bike")}>
              + Add bike
            </Button>
            <Button variant="outline" size="sm" onClick={() => openNew("bike_component")} disabled={!bikes.length}>
              + Add component
            </Button>
          </div>
        </div>
      </Card>

      <GearFormDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        gear={editing}
        defaultKind={newKind}
        bikes={bikes}
        onSave={handleSave}
      />
    </>
  )
}
