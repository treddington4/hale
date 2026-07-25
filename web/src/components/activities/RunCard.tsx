import * as React from "react"
import { Timer, Heart, Footprints, Mountain, Zap, Gauge, Flame, Snowflake, Thermometer, Droplet, ChevronDown, ChevronUp, Pencil } from "lucide-react"
import type { Run } from "@/lib/api"
import { TYPE_COLORS } from "@/lib/runs"
import { withAlpha } from "@/lib/color"
import { paceStr, timeStr, tempColor, isPlausiblePace } from "@/lib/format"
import { gapSecPerMi, avgGapFromRoutePoints } from "@/lib/gap"
import { isPlausibleHR } from "@/hooks/useHrFloor"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { SplitsTable } from "./SplitsTable"
import { IntervalsTable } from "./IntervalsTable"
import { ExerciseSetsTable } from "./ExerciseSetsTable"
import { MiniMap } from "./MiniMap"

function MiniStat({ icon: Icon, children }: { icon: React.ComponentType<{ className?: string }>; children: React.ReactNode }) {
  return (
    <div className="text-muted-foreground flex items-center gap-1 text-xs">
      <Icon className="size-3.5" />
      {children}
    </div>
  )
}

export function RunCard({
  run,
  hrFloor,
  isOpen,
  onToggle,
  onEdit,
}: {
  run: Run
  hrFloor: number
  isOpen: boolean
  onToggle: () => void
  onEdit: () => void
}) {
  const type = run.type || "Easy"
  const typeColor = TYPE_COLORS[type] || "#8B93A1"
  // Prefer the real per-point route data (accounts for actual descents, not just
  // total climbing — see gap.ts's avgGapFromRoutePoints comment for why the
  // gain-only shortcut below overstates GAP); fall back to it only when there's
  // no usable GPS route (treadmill runs, or a run synced without route data).
  const gap =
    isPlausiblePace(run.avgPaceSecPerMi, run.distanceMi) && run.distanceMi
      ? (avgGapFromRoutePoints(run.routeMetrics) ??
          (run.elevGainFt != null ? gapSecPerMi(run.avgPaceSecPerMi, run.elevGainFt, run.distanceMi) : null))
      : null

  const hasWeather = run.tempF != null || run.heatIndexF != null || run.wetBulbF != null
  const hasDynamics =
    run.verticalOscillationMm != null ||
    run.groundContactTimeMs != null ||
    run.verticalRatioPct != null ||
    run.strideLengthM != null ||
    run.avgPowerWatts != null

  const hasExerciseSets = run.exerciseSets != null && run.exerciseSets.length > 0
  const route: [number, number][] | null =
    run.route && run.route.length > 1
      ? run.route
      : run.routeMetrics.length > 1
        ? run.routeMetrics.map((p) => [p.lat, p.lon])
        : null

  return (
    <Card id={`run-card-${run.id}`} className="gap-2 border-l-4" style={{ borderLeftColor: tempColor(run.tempF) }}>
      <button className="flex w-full items-start justify-between gap-3 text-left" onClick={onToggle}>
        <div>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium">{run.name}</span>
            <span
              className="rounded border px-1.5 py-0.5 text-[10px]"
              style={{ color: typeColor, borderColor: withAlpha(typeColor, 0.33), background: withAlpha(typeColor, 0.09) }}
            >
              {type}
            </span>
            {run.isTreadmill && (
              <span className="border-border bg-secondary text-muted-foreground rounded border px-1.5 py-0.5 text-[10px]">
                Treadmill
              </span>
            )}
            {run.mergedSources && (
              <span
                className="rounded border px-1.5 py-0.5 text-[10px]"
                style={{ color: "#B98CE0", borderColor: withAlpha("#B98CE0", 0.33), background: withAlpha("#B98CE0", 0.09) }}
                title={`Same run synced from both ${run.mergedSources.join(" and ")} — merged into one card`}
              >
                🔗 {run.mergedSources.map((s) => s[0].toUpperCase() + s.slice(1)).join(" + ")}
              </span>
            )}
          </div>
          <div className="text-muted-foreground mt-0.5 text-xs">
            {new Date(run.date + "T00:00:00").toLocaleDateString(undefined, {
              weekday: "short",
              month: "short",
              day: "numeric",
            })}
            {run.startTime ? ` · ${run.startTime}` : ""}
          </div>
        </div>
        <div className="text-right">
          {run.distanceMi ? (
            <>
              <div className="font-mono text-lg font-bold tabular-nums">{run.distanceMi.toFixed(2)} mi</div>
              <div className="text-muted-foreground font-mono text-xs tabular-nums">
                {isPlausiblePace(run.avgPaceSecPerMi, run.distanceMi) ? paceStr(run.avgPaceSecPerMi) : "--:--"}/mi
              </div>
            </>
          ) : (
            <div className="font-mono text-lg font-bold tabular-nums">{timeStr(run.movingTimeSec)}</div>
          )}
        </div>
      </button>

      <div className="flex flex-wrap gap-x-3 gap-y-1">
        <MiniStat icon={Timer}>{timeStr(run.movingTimeSec)}</MiniStat>
        {isPlausibleHR(run.avgHR, hrFloor) && (
          <MiniStat icon={Heart}>
            {run.avgHR}
            {isPlausibleHR(run.maxHR, hrFloor) ? ` / ${run.maxHR}` : ""} bpm
          </MiniStat>
        )}
        {run.avgCadence != null && <MiniStat icon={Footprints}>{Math.round(run.avgCadence)} spm</MiniStat>}
        {run.elevGainFt != null && <MiniStat icon={Mountain}>{Math.round(run.elevGainFt)} ft</MiniStat>}
        {gap != null && (
          <MiniStat icon={Zap}>
            <span className="text-hale-cold">GAP {paceStr(gap)}</span>
          </MiniStat>
        )}
        {run.rpe != null && <MiniStat icon={Gauge}>RPE {run.rpe}</MiniStat>}
      </div>

      {hasWeather && (
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {run.tempF != null && (
            <MiniStat icon={run.tempF >= 75 ? Flame : Snowflake}>
              <span style={{ color: tempColor(run.tempF) }}>
                {Math.round(run.tempF)}°F{run.weatherCondition ? ` · ${run.weatherCondition}` : ""}
              </span>
            </MiniStat>
          )}
          {run.heatIndexF != null && Math.round(run.heatIndexF) !== Math.round(run.tempF ?? 0) && (
            <MiniStat icon={Thermometer}>
              <span style={{ color: tempColor(run.heatIndexF) }}>HI {Math.round(run.heatIndexF)}°F</span>
            </MiniStat>
          )}
          {run.wetBulbF != null && <MiniStat icon={Droplet}>WB {Math.round(run.wetBulbF)}°F</MiniStat>}
        </div>
      )}

      {hasDynamics && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
          {run.groundContactTimeMs != null && (
            <span className="text-muted-foreground">GCT {Math.round(run.groundContactTimeMs)}ms</span>
          )}
          {run.verticalOscillationMm != null && (
            <span className="text-muted-foreground">VO {(run.verticalOscillationMm / 10).toFixed(1)}cm</span>
          )}
          {run.verticalRatioPct != null && (
            <span className="text-muted-foreground">VR {run.verticalRatioPct.toFixed(1)}%</span>
          )}
          {run.strideLengthM != null && (
            <span className="text-muted-foreground">Stride {(run.strideLengthM * 3.28084).toFixed(1)}ft</span>
          )}
          {run.avgPowerWatts != null && (
            <span className="text-muted-foreground">{Math.round(run.avgPowerWatts)}W</span>
          )}
        </div>
      )}

      <div className="flex items-center justify-between">
        <Button
          variant="link"
          size="sm"
          className="h-auto gap-1 p-0"
          onClick={(e) => {
            e.stopPropagation()
            onEdit()
          }}
        >
          <Pencil className="size-3" /> edit
        </Button>
        <button onClick={onToggle} className="text-muted-foreground">
          {isOpen ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
        </button>
      </div>

      {isOpen && (
        <div className="border-border mt-1 border-t pt-3">
          {hasExerciseSets ? (
            <ExerciseSetsTable sets={run.exerciseSets!} />
          ) : (
            <>
              {type === "Interval" && run.intervals.length > 0 ? (
                <IntervalsTable intervals={run.intervals} recovery={run.recovery} hrFloor={hrFloor} />
              ) : run.splits && run.splits.length > 0 ? (
                <SplitsTable splits={run.splits} hrFloor={hrFloor} />
              ) : null}
              <div className="mt-3">
                <MiniMap route={route} />
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  )
}
