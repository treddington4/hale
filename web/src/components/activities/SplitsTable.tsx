import type { RunSplit } from "@/lib/runs"
import { paceStr } from "@/lib/format"
import { gapSecPerMi } from "@/lib/gap"
import { isPlausibleHR } from "@/hooks/useHrFloor"

// Per-split GAP still uses the gain-only shortcut (gapSecPerMi) — unlike
// RunCard's whole-run badge (see gap.ts's avgGapFromRoutePoints), routeMetrics
// points don't carry a cumulative distance, so there's no reliable way to
// bucket them into mile splits client-side. Carries a smaller version of the
// same overstatement bias as the whole-run figure did before that fix.
export function SplitsTable({ splits, hrFloor }: { splits: RunSplit[]; hrFloor: number }) {
  const paces = splits.map((s) => s.paceSecPerMi || 0)
  const maxPace = Math.max(...paces)
  const minPace = Math.min(...splits.map((s) => s.paceSecPerMi || Infinity))

  return (
    <div className="grid grid-cols-7 gap-x-2 gap-y-1.5 text-xs">
      <div className="text-muted-foreground col-span-7 grid grid-cols-7 gap-x-2 pb-1">
        <span>Mi</span>
        <span>Pace</span>
        <span>Elev</span>
        <span>HR</span>
        <span>Max</span>
        <span>Cad</span>
        <span>GAP</span>
      </div>
      {splits.map((s, i) => {
        const w = maxPace > minPace ? ((s.paceSecPerMi ?? 0) - minPace) / (maxPace - minPace) : 0.5
        const gap = gapSecPerMi(s.paceSecPerMi, s.elevGainFt, 1)
        return (
          <div key={i} className="col-span-7 grid grid-cols-7 items-center gap-x-2">
            <span className="text-muted-foreground">{s.mile}</span>
            <div className="bg-background relative h-4 overflow-hidden rounded">
              <div
                className="bg-primary/60 absolute inset-y-0 left-0"
                style={{ width: `${20 + (1 - w) * 80}%` }}
              />
              <span className="relative px-1 tabular-nums">{paceStr(s.paceSecPerMi)}</span>
            </div>
            <span>{s.elevGainFt != null ? `${Math.round(s.elevGainFt)}ft` : "--"}</span>
            <span>{isPlausibleHR(s.avgHR, hrFloor) ? s.avgHR : "--"}</span>
            <span className="text-muted-foreground">{isPlausibleHR(s.maxHR, hrFloor) ? s.maxHR : "--"}</span>
            <span className="text-muted-foreground">{s.avgCadence != null ? Math.round(s.avgCadence) : "--"}</span>
            <span className="text-hale-cold">{gap ? paceStr(gap) : "--"}</span>
          </div>
        )
      })}
    </div>
  )
}
