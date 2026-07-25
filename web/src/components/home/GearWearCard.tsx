import { useNavigate } from "react-router-dom"
import { useGear } from "@/hooks/useGear"
import { Card } from "@/components/ui/card"

function wearColor(pct: number) {
  if (pct >= 100) return "var(--hale-hot)"
  if (pct >= 85) return "var(--hale-gold)"
  return "var(--hale-good)"
}

// Degrades to nothing (not an empty-state card) until the user has set up at
// least one gear item with a replace-at-mi threshold in Settings — matches
// WellnessCards' own pattern for an optional, not-yet-configured data source.
export function GearWearCard() {
  const { data: gear } = useGear()
  const navigate = useNavigate()

  const tracked = (gear ?? [])
    .filter((g) => !g.retiredDate && g.replaceAtMi != null && g.wearPct != null)
    .sort((a, b) => (b.wearPct ?? 0) - (a.wearPct ?? 0))

  if (!tracked.length) return null

  return (
    <div className="mt-6">
      <h2 className="mb-3 text-sm font-semibold">Gear Wear</h2>
      <Card
        onClick={() => navigate("/settings")}
        className="hover:border-muted-foreground/40 cursor-pointer gap-3 transition-colors"
      >
        {tracked.slice(0, 4).map((g) => (
          <div key={g.id}>
            <div className="flex items-center justify-between text-sm">
              <span className="font-medium">{g.name}</span>
              <span className="text-muted-foreground font-mono text-xs">
                {g.totalMiles.toFixed(0)} / {g.replaceAtMi} mi
              </span>
            </div>
            <div className="bg-background mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
              <div
                className="h-full rounded-full"
                style={{ width: `${Math.min(100, g.wearPct ?? 0)}%`, background: wearColor(g.wearPct ?? 0) }}
              />
            </div>
            {(g.wearPct ?? 0) >= 100 && (
              <div className="text-hale-hot mt-1 text-xs">Past recommended replacement mileage</div>
            )}
          </div>
        ))}
      </Card>
    </div>
  )
}
